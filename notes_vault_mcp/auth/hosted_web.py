from __future__ import annotations

import hmac
import html
import logging
import secrets
import time
from collections.abc import Callable
from urllib.parse import quote, urlsplit

from mcp.server.auth.provider import construct_redirect_uri
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from notes_vault_mcp.auth import READ_SCOPE, WRITE_SCOPE, AuthConfig
from notes_vault_mcp.auth.builtin import PAGE, PAGE_HEADERS, stamp
from notes_vault_mcp.auth.cimd import is_loopback, is_metadata_url
from notes_vault_mcp.auth.hosted_store import LOGIN_TTL, SESSION_TTL, Account, HostedStore, digest, scope_label
from notes_vault_mcp.auth.upstream import Upstream, UpstreamError

logger = logging.getLogger(__name__)

SESSION_COOKIE = "nvm_session"
LOGIN_COOKIE = "nvm_login"
REQUEST_TTL = 900
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
EXTRA_STYLE = """
input[type=text] { display: block; width: 100%; box-sizing: border-box; margin-top: .35rem;
  padding: .55rem .6rem; font-size: 1rem; border: 1px solid #cbc8c0; border-radius: 6px; }
fieldset label { font-family: inherit; }
button.secondary { color: #1d1c1a; background: #fff; border: 1px solid #cbc8c0; margin-top: .5rem; }
button.inline { width: auto; padding: .3rem .7rem; font-size: .9rem; }
table { width: 100%; border-collapse: collapse; margin-bottom: 1.25rem; font-size: .9rem; }
th, td { text-align: left; padding: .4rem .3rem; border-bottom: 1px solid #e2e0da; vertical-align: top; }
code, .key { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
.key { display: block; margin-bottom: 1.25rem; padding: .6rem .75rem; background: #f6f5f2;
  border: 1px solid #e2e0da; border-radius: 6px; }
.warning { margin-bottom: 1.25rem; padding: .6rem .75rem; color: #6b4a00; background: #fdf5e2;
  border: 1px solid #f0e0b5; border-radius: 6px; }
h2 { margin: 1.5rem 0 .5rem; font-size: 1rem; }
ul { margin: 0 0 1.25rem; padding-left: 1.2rem; color: #56534d; }
dl { margin: 0 0 1.25rem; } dt { color: #56534d; font-size: .85rem; } dd { margin: 0 0 .5rem; }
"""
WIDE_STYLE = "main { max-width: 40rem; }\n"


def esc(text: object) -> str:
    return html.escape(str(text))


def page(heading: str, lead: str, body: str = "", status_code: int = 200, wide: bool = False) -> HTMLResponse:
    style = EXTRA_STYLE + (WIDE_STYLE if wide else "")
    document = PAGE.format(heading=esc(heading), lead=esc(lead), body=body).replace("</style>", style + "</style>", 1)
    return HTMLResponse(document, status_code=status_code, headers=PAGE_HEADERS)


def oauth_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status_code, headers=NO_STORE)


def error_box(message: str) -> str:
    return f'<p class="error">{esc(message)}</p>' if message else ""


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class Window:
    def __init__(self, limit: int, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.seconds = seconds
        self.clock = clock
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        moment = self.clock()
        if len(self._hits) > 10_000:
            self._hits = {name: hits for name, hits in self._hits.items() if moment - hits[-1] < self.seconds}
        hits = [hit for hit in self._hits.get(key, []) if moment - hit < self.seconds]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(moment)
        self._hits[key] = hits
        return True


class Limits:
    def __init__(self) -> None:
        self.sign_in = Window(20, 60)
        self.callback = Window(20, 60)
        self.device_start = Window(10, 60)
        self.device_lookup = Window(5, 60)
        self.token_misses = Window(30, 60)


class Browser:
    def __init__(self, config: AuthConfig, store: HostedStore, upstream: Upstream, limits: Limits) -> None:
        self.config = config
        self.store = store
        self.upstream = upstream
        self.limits = limits
        self.secure = config.public_url.startswith("https://")
        self.cookie_path = config.path_prefix or "/"

    def local(self, path: str) -> str:
        return f"{self.config.path_prefix}{path}"

    def signed_in(self, request: Request) -> tuple[Account, str] | None:
        return self.store.session(request.cookies.get(SESSION_COOKIE, ""))

    async def require_session(self, request: Request, return_to: str) -> tuple[Account, str] | Response:
        found = self.signed_in(request)
        if found is None:
            return await self.start_sign_in(request, return_to)
        account, _ = found
        if not account.active:
            return self.no_access(account.email, suspended=True)
        return found

    async def start_sign_in(self, request: Request, return_to: str) -> Response:
        if not self.limits.sign_in.allow(client_address(request)):
            return page("Slow down", "Too many sign-ins from here. Try again in a minute.", status_code=429)
        state, nonce, verifier, binding = (secrets.token_urlsafe(32) for _ in range(4))
        try:
            target = await self.upstream.authorization_url(state, nonce, verifier)
        except UpstreamError as exc:
            logger.warning("hosted sign-in: %s", exc)
            return page("Sign-in is unavailable", "The identity provider cannot be reached.", status_code=502)
        self.store.save_login(state, binding, nonce, verifier, return_to)
        response = RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})
        self.set_cookie(response, LOGIN_COOKIE, binding, LOGIN_TTL)
        return response

    def set_cookie(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name, value, max_age=max_age, path=self.cookie_path, secure=self.secure, httponly=True, samesite="lax"
        )

    async def callback(self, request: Request) -> Response:
        params = request.query_params
        state = params.get("state", "")
        login = self.store.take_login(state) if state else None
        binding = request.cookies.get(LOGIN_COOKIE, "")
        if login is None or not binding or not hmac.compare_digest(login["binding_hash"], digest(binding)):
            return page("Sign-in expired", "Start again from the page you came from.", status_code=400)
        if params.get("error"):
            return page("Sign-in cancelled", "The identity provider did not sign you in.", status_code=400)
        if not self.limits.callback.allow(client_address(request)):
            return page("Slow down", "Too many sign-ins from here. Try again in a minute.", status_code=429)
        try:
            identity = await self.upstream.identity(params.get("code", ""), login["verifier"], login["nonce"])
        except UpstreamError as exc:
            logger.warning("hosted sign-in: %s", exc)
            return page("Sign-in failed", "The identity provider did not confirm the sign-in.", status_code=502)
        account = self.store.sign_in(identity.sub, identity.email, identity.email_verified, identity.name)
        if account is None:
            return self.no_access(identity.email, verified=identity.email_verified)
        if not account.active:
            return self.no_access(account.email, suspended=True)
        response = RedirectResponse(self.safe_return(login["return_to"]), status_code=303)
        self.set_cookie(response, SESSION_COOKIE, self.store.create_session(account.id), SESSION_TTL)
        response.delete_cookie(LOGIN_COOKIE, path=self.cookie_path)
        return response

    def safe_return(self, target: str) -> str:
        parts = urlsplit(target)
        local = not parts.scheme and not parts.netloc and target.startswith(self.local("/"))
        return target if local and "\\" not in target else self.local("/account")

    def no_access(self, email: str, suspended: bool = False, verified: bool = True) -> Response:
        if suspended:
            lead = f"The account for {email} is suspended. Ask the person who runs this vault."
        elif not verified:
            lead = f"Your identity provider has not verified {email}. Verify it there, then sign in again."
        else:
            lead = f"{email} has no account on this vault. Ask the person who runs it to invite this address."
        return page("No access", lead, status_code=403)

    def csrf_ok(self, form_value: object, csrf: str) -> bool:
        return isinstance(form_value, str) and hmac.compare_digest(form_value, csrf)

    async def consent_page(self, request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        pending = self.store.load_request(request_id, REQUEST_TTL) if request_id else None
        if pending is None:
            return self.expired()
        session = await self.require_session(request, self.local(f"/login?request={quote(request_id)}"))
        if isinstance(session, Response):
            return session
        account, csrf = session
        return self.render_consent(request_id, pending, account, csrf)

    def render_consent(self, request_id: str, pending: dict, account: Account, csrf: str, error: str = "") -> Response:
        redirect_host = urlsplit(pending["redirect_uri"]).hostname or pending["redirect_uri"]
        origin = (
            f"Identified by {esc(pending['client_id'])}."
            if is_metadata_url(pending["client_id"])
            else "It registered itself with this server; its name is its own claim."
        )
        warning = (
            '<p class="warning">This app runs on a computer and returns to a local address. '
            "Approve only if you started it yourself just now.</p>"
            if is_loopback(pending["redirect_uri"])
            else ""
        )
        write_choice = (
            '<label><input type="radio" name="access" value="write"> read and write</label>'
            if WRITE_SCOPE in pending["scopes"]
            else ""
        )
        body = f"""{error_box(error)}{warning}
<dl><dt>Signed in as</dt><dd>{esc(account.email)}</dd>
<dt>Client</dt><dd>{origin}</dd>
<dt>Returns to</dt><dd><code>{esc(redirect_host)}</code></dd></dl>
<form method="post" action="{esc(self.local("/login"))}">
<input type="hidden" name="request" value="{esc(request_id)}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<fieldset><legend>Grant this client</legend>
<label><input type="radio" name="access" value="read" checked> read</label>
{write_choice}
</fieldset>
<button type="submit" name="decision" value="approve">Approve</button>
<button type="submit" name="decision" value="deny" class="secondary">Deny</button>
</form>"""
        return page("Vault access", f"{pending['client_name']} is asking to reach your vault.", body)

    async def consent_submit(self, request: Request, mint_code: Callable[[dict, list[str], Account], str]) -> Response:
        form = await request.form()
        request_id = str(form.get("request") or "")
        pending = self.store.load_request(request_id, REQUEST_TTL) if request_id else None
        if pending is None:
            return self.expired()
        session = self.signed_in(request)
        if session is None or not session[0].active or not self.csrf_ok(form.get("csrf"), session[1]):
            return page("Not signed in", "Open the link from the client again.", status_code=403)
        account, _ = session
        self.store.drop_request(request_id)
        if form.get("decision") != "approve":
            target = construct_redirect_uri(
                pending["redirect_uri"],
                error="access_denied",
                error_description="the user denied access",
                state=pending["state"],
                iss=self.config.issuer,
            )
            return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})
        scopes = [READ_SCOPE]
        if form.get("access") == "write" and WRITE_SCOPE in pending["scopes"]:
            scopes.append(WRITE_SCOPE)
        code = mint_code(pending, scopes, account)
        target = construct_redirect_uri(
            pending["redirect_uri"], code=code, state=pending["state"], iss=self.config.issuer
        )
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    def expired(self) -> Response:
        return page(
            "Nothing to approve",
            "This login link has expired or was already used. Start the connection again from the client.",
            status_code=400,
        )

    async def account_page(self, request: Request) -> Response:
        session = await self.require_session(request, self.local("/account"))
        if isinstance(session, Response):
            return session
        account, csrf = session
        return self.render_account(account, csrf)

    def render_account(self, account: Account, csrf: str, notice: str = "", status_code: int = 200) -> Response:
        rows = "".join(
            f"<tr><td>{esc(key['name'])}</td><td>{esc(scope_label(key['scopes']))}</td>"
            f"<td>{esc(stamp(key['created_at']))}</td><td>{esc(stamp(key['last_used_at']))}</td><td>"
            f'<form method="post" action="{esc(self.local("/account/keys/revoke"))}">'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<input type="hidden" name="key" value="{esc(key["id"])}">'
            f'<button type="submit" class="secondary inline" aria-label="Revoke {esc(key["name"])}">Revoke</button>'
            "</form></td></tr>"
            for key in self.store.keys(account.id)
        )
        keys = (
            "<table><thead><tr><th>Name</th><th>Access</th><th>Made</th><th>Last used</th><th></th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            if rows
            else "<p>No keys yet.</p>"
        )
        mcp = esc(self.config.resource_url)
        device = esc(f"{self.config.public_url.rstrip('/')}/device")
        body = f"""{notice}
<dl><dt>Signed in as</dt><dd>{esc(account.email)}</dd></dl>
<h2>Connect</h2>
<ul>
<li>claude.ai, Claude Desktop and mobile: add a custom connector with the URL <code>{mcp}</code>.</li>
<li>Claude Code, Codex, Cursor: add <code>{mcp}</code> as a remote MCP server; each signs in here the first time.</li>
<li>A machine over ssh: a tool that uses the device flow shows a short code; enter it at <code>{device}</code>.</li>
<li>A client that only takes a header: make a key below and send <code>Authorization: Bearer &lt;key&gt;</code>.</li>
<li>Obsidian's Remotely Save: WebDAV at <code>{esc(self.config.dav_url)}</code>, your email as the username
and a key as the password.</li>
</ul>
<h2>Keys</h2>
{keys}
<form method="post" action="{esc(self.local("/account/keys"))}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<label>New key, named after where it will live
<input type="text" name="name" maxlength="64" required placeholder="laptop">
</label>
<fieldset><legend>Access</legend>
<label><input type="radio" name="access" value="read" checked> read</label>
<label><input type="radio" name="access" value="write"> read and write</label>
</fieldset>
<button type="submit">Make a key</button>
</form>
<form method="post" action="{esc(self.local("/logout"))}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<button type="submit" class="secondary">Sign out</button>
</form>"""
        return page(
            "Your vault", "Who you are, your keys, and how to connect.", body, status_code=status_code, wide=True
        )

    async def account_form(self, request: Request) -> tuple[Account, str, dict] | Response:
        form = await request.form()
        session = self.signed_in(request)
        if session is None or not session[0].active or not self.csrf_ok(form.get("csrf"), session[1]):
            return page("Not signed in", "Open your account page again.", status_code=403)
        return session[0], session[1], {key: str(value) for key, value in form.items()}

    async def create_key(self, request: Request) -> Response:
        submitted = await self.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, csrf, form = submitted
        name = " ".join(form.get("name", "").split())[:64]
        if not name:
            return self.render_account(account, csrf, error_box("Give the key a name."), status_code=400)
        scopes = [READ_SCOPE, WRITE_SCOPE] if form.get("access") == "write" else [READ_SCOPE]
        key = self.store.create_key(account.id, name, scopes)
        notice = (
            f'<p class="warning">The key {esc(name)} is shown once. Copy it now.</p>'
            f'<code class="key" aria-label="the new key">{esc(key)}</code>'
        )
        return self.render_account(account, csrf, notice)

    async def revoke_key(self, request: Request) -> Response:
        submitted = await self.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, _, form = submitted
        self.store.revoke_key(account.id, form.get("key", ""))
        return RedirectResponse(self.local("/account"), status_code=303)

    async def sign_out(self, request: Request) -> Response:
        submitted = await self.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        self.store.end_session(request.cookies.get(SESSION_COOKIE, ""))
        response = page("Signed out", "You are signed out of this vault.")
        response.delete_cookie(SESSION_COOKIE, path=self.cookie_path)
        return response
