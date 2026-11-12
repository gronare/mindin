from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

from mcp.server.auth.provider import construct_redirect_uri
from mcp.server.transport_security import RequestBodyLimitMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import request_response
from starlette.types import ASGIApp

from mindin.hosted.accounts.reach import ALSO_REACHES
from mindin.hosted.accounts.store import SESSION_TTL, Account, HostedStore, digest
from mindin.hosted.accounts.vaults import Vault
from mindin.hosted.config import READ_SCOPE, WRITE_SCOPE, AuthConfig
from mindin.hosted.pages.html import error_box, esc, hidden_field
from mindin.hosted.pages.templates import APP_PAGE, APP_SCRIPT, PAGE, PAGE_HEADERS, stamp
from mindin.hosted.signin.cimd import is_loopback, is_metadata_url

logger = logging.getLogger(__name__)

SESSION_COOKIE = "nvm_session"
LOGIN_COOKIE = "nvm_login"
REQUEST_TTL = 900
RECENT_SIGN_IN = 600
FORM_BODY_LIMIT = 64 * 1024
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
EXTRA_STYLE = """
input[type=text], input[type=email] { display: block; width: 100%; box-sizing: border-box; margin-top: .35rem;
  padding: .55rem .6rem; font-size: 1rem; border: 1px solid #cbc8c0; border-radius: 6px; }
fieldset label { font-family: inherit; }
button.secondary { color: #1d1c1a; background: #fff; border: 1px solid #cbc8c0; margin-top: .5rem; }
.methods { display: grid; gap: 1.25rem; }
.methods > * + * button { color: #1d1c1a; background: #fff; border: 1px solid #cbc8c0; }
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


def page(heading: str, lead: str, body: str = "", status_code: int = 200, wide: bool = False) -> HTMLResponse:
    style = EXTRA_STYLE + (WIDE_STYLE if wide else "")
    document = PAGE.format(heading=esc(heading), lead=esc(lead), body=body).replace("</style>", style + "</style>", 1)
    return HTMLResponse(document, status_code=status_code, headers=PAGE_HEADERS)


def app_page(config: AuthConfig, title: str, body: str, on: str = "vaults", status_code: int = 200) -> HTMLResponse:
    local = config.path_prefix
    current = ' class="on" aria-current="page"'
    nav = "".join(
        f'<a href="{esc(local + path)}"{current if name == on else ""}>{label}</a>'
        for name, label, path in (("vaults", "Vaults", "/account"), ("account", "Account", "/account/settings"))
    )
    document = APP_PAGE.format(
        title=esc(title), host=esc(config.public_host), nav=nav, body=body, script=esc(local + "/account/page.js")
    )
    return HTMLResponse(document, status_code=status_code, headers=PAGE_HEADERS)


async def app_script(_: Request) -> Response:
    return Response(APP_SCRIPT, media_type="text/javascript", headers={"Cache-Control": "public, max-age=3600"})


def main_vault_choice(vaults: list[Vault], default: Vault | None) -> str:
    chosen = default if default is not None and default in vaults else vaults[0]
    radios = "".join(
        f'<label><input type="radio" name="vault" value="{esc(vault.slug)}"{" checked" if vault == chosen else ""}>'
        f" {esc(vault.name)} ({esc(vault.slug)})</label>"
        for vault in vaults
    )
    return f"<fieldset><legend>Main vault</legend>{radios}</fieldset>"


def oauth_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status_code, headers=NO_STORE)


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


BLOCKED_KEYS = 10_000


class RateCounter:
    """Counts hits per scope and address in the account store, one row per fixed slot of a window's length."""

    def __init__(self, store: HostedStore) -> None:
        self.store = store

    def hit(self, scope: str, key: str, limit: int, seconds: float, moment: float) -> bool:
        address = digest(key)
        slot = int(moment // seconds)
        weight = 1.0 - (moment - slot * seconds) / seconds
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT slot, hits FROM rate_limit_hits WHERE scope = ? AND address = ? AND slot IN (?, ?)",
                (scope, address, slot - 1, slot),
            ).fetchall()
            counted = {row["slot"]: row["hits"] for row in rows}
            if counted.get(slot - 1, 0) * weight + counted.get(slot, 0) >= limit:
                return False
            connection.execute(
                "INSERT INTO rate_limit_hits(scope, address, slot, hits) VALUES(?, ?, ?, 1)"
                " ON CONFLICT (scope, address, slot) DO UPDATE SET hits = rate_limit_hits.hits + 1",
                (scope, address, slot),
            )
        return True

    def forget_before(self, scope: str, slot: int) -> None:
        with self.store.connect() as connection:
            connection.execute("DELETE FROM rate_limit_hits WHERE scope = ? AND slot < ?", (scope, slot))


class Window:
    def __init__(
        self,
        limit: int,
        seconds: float,
        clock: Callable[[], float] = time.monotonic,
        counter: RateCounter | None = None,
        scope: str = "",
    ) -> None:
        self.limit = limit
        self.seconds = seconds
        self.clock = clock
        self.counter = counter
        self.scope = scope
        self._hits: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}
        self._swept = 0

    def allow(self, key: str) -> bool:
        if self.counter is None:
            return self._allow_here(key)
        moment = self.clock()
        if self._blocked.get(key, 0.0) > moment:
            return False
        slot = int(moment // self.seconds)
        if slot != self._swept:
            self._swept = slot
            self._blocked = {name: until for name, until in self._blocked.items() if until > moment}
            self.counter.forget_before(self.scope, slot - 1)
        if self.counter.hit(self.scope, key, self.limit, self.seconds, moment):
            return True
        if len(self._blocked) < BLOCKED_KEYS:
            self._blocked[key] = (slot + 1) * self.seconds
        return False

    def _allow_here(self, key: str) -> bool:
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
    def __init__(self, store: HostedStore | None = None, clock: Callable[[], float] = time.time) -> None:
        self.counter = RateCounter(store) if store is not None else None
        self.clock = clock
        self.sign_in = self.window("sign-in", 20, 60)
        self.callback = self.window("callback", 20, 60)
        self.device_start = self.window("device-start", 10, 60)
        self.device_lookup = self.window("device-lookup", 5, 60)
        self.token_misses = self.window("token-misses", 30, 60)

    def window(self, scope: str, limit: int, seconds: float) -> Window:
        return Window(limit, seconds, self.clock, self.counter, scope)


class Browser:
    def __init__(
        self,
        config: AuthConfig,
        store: HostedStore,
        limits: Limits,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.clock = clock
        self.store = store
        self.limits = limits
        self.sections: list[Callable[[Account, str], str]] = []
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
        return RedirectResponse(
            self.local(f"/signin?next={quote(return_to, safe='')}"),
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    def set_cookie(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name, value, max_age=max_age, path=self.cookie_path, secure=self.secure, httponly=True, samesite="lax"
        )

    def start_session(self, request: Request, response: Response, account: Account) -> Response:
        self.store.end_session(request.cookies.get(SESSION_COOKIE, ""))
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
            lead = (
                "This sign-in came without a verified email address, so it cannot open an account. Use another way in."
            )
        else:
            lead = f"{email or 'This sign-in'} has no account on this vault. Ask the person who runs it for an invite."
        return page("No access", lead, status_code=403)

    def csrf_ok(self, form_value: object, csrf: str) -> bool:
        return isinstance(form_value, str) and hmac.compare_digest(form_value, csrf)

    def signed_in_at(self, request: Request) -> int | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT created_at FROM sessions WHERE id_hash = ?", (digest(request.cookies.get(SESSION_COOKIE, "")),)
            ).fetchone()
        return row["created_at"] if row else None

    def recent(self, request: Request) -> bool:
        started = self.signed_in_at(request)
        return started is not None and self.clock() - started <= RECENT_SIGN_IN

    def app(self, title: str, body: str, on: str = "vaults", status_code: int = 200) -> Response:
        return app_page(self.config, title, body, on, status_code)

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
        requested = pending.get("requested_vault")
        bound = None if requested is None else self.store.vault_at(account.id, requested)
        if requested is not None and bound is None:
            return page(
                "Not your vault", "The client asks for a vault this account does not own or belong to.", status_code=403
            )
        vaults = [bound] if bound else self.store.reachable_vaults(account.id)
        if not vaults:
            return page("No vault yet", "Make a vault on your account page first, then connect again.", status_code=400)
        if bound:
            vault_choice = f"<dt>Main vault</dt><dd>{esc(bound.name)} <code>{esc(bound.slug)}</code></dd>"
            vault_field = hidden_field("vault", bound.slug)
        else:
            vault_choice = ""
            vault_field = main_vault_choice(vaults, self.store.default_vault(account.id))
        body = f"""{error_box(error)}{warning}
<dl><dt>Signed in as</dt><dd>{esc(account.email)}</dd>
<dt>Client</dt><dd>{origin}</dd>
<dt>Returns to</dt><dd><code>{esc(redirect_host)}</code></dd>{vault_choice}</dl>
<p>{esc(ALSO_REACHES)}</p>
<form method="post" action="{esc(self.local("/login"))}">
<input type="hidden" name="request" value="{esc(request_id)}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
{vault_field}
<fieldset><legend>Grant this client</legend>
<label><input type="radio" name="access" value="read" checked> read</label>
{write_choice}
</fieldset>
<button type="submit" name="decision" value="approve">Approve</button>
<button type="submit" name="decision" value="deny" class="secondary">Deny</button>
</form>"""
        return page("Vault access", f"{pending['client_name']} is asking to reach your vaults.", body)

    async def consent_submit(
        self, request: Request, mint_code: Callable[[dict, list[str], Account, Vault], str]
    ) -> Response:
        form = await request.form()
        request_id = str(form.get("request") or "")
        pending = self.store.load_request(request_id, REQUEST_TTL) if request_id else None
        if pending is None:
            return self.expired()
        session = self.signed_in(request)
        if session is None or not session[0].active or not self.csrf_ok(form.get("csrf"), session[1]):
            return page("Not signed in", "Open the link from the client again.", status_code=403)
        account, _ = session
        vault = self.store.vault_at(account.id, str(form.get("vault") or ""))
        requested = pending.get("requested_vault")
        if form.get("decision") == "approve" and (vault is None or requested not in (None, vault.slug)):
            return self.render_consent(request_id, pending, account, session[1], "Choose one of your vaults.")
        self.store.drop_request(request_id)
        if form.get("decision") != "approve" or vault is None:
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
        code = mint_code(pending, scopes, account, vault)
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

    async def settings_page(self, request: Request) -> Response:
        session = await self.require_session(request, self.local("/account/settings"))
        if isinstance(session, Response):
            return session
        account, csrf = session
        return self.render_settings(account, csrf)

    def render_settings(self, account: Account, csrf: str, notice: str = "", status_code: int = 200) -> Response:
        body = f"""<div><h1>Account</h1><p class="lead">You and how you sign in. Keys and connections live
with each vault.</p></div>
{notice}
<dl class="kv"><dt>Signed in as</dt><dd>{esc(account.email)}</dd>
<dt>Member since</dt><dd>{esc(stamp(account.created_at)[:10])}</dd></dl>
{"".join(section(account, csrf) for section in self.sections)}
<form method="post" action="{esc(self.local("/logout"))}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<button type="submit" class="btn secondary">Sign out</button>
</form>"""
        return self.app("Account", body, on="account", status_code=status_code)

    async def account_form(self, request: Request) -> tuple[Account, str, dict] | Response:
        form = await request.form()
        session = self.signed_in(request)
        if session is None or not session[0].active or not self.csrf_ok(form.get("csrf"), session[1]):
            return page("Not signed in", "Open your account page again.", status_code=403)
        return session[0], session[1], {key: str(value) for key, value in form.items()}

    async def sign_out(self, request: Request) -> Response:
        submitted = await self.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        self.store.end_session(request.cookies.get(SESSION_COOKIE, ""))
        response = page("Signed out", "You are signed out of this vault.")
        response.delete_cookie(SESSION_COOKIE, path=self.cookie_path)
        return response


def limited(handler: Callable[[Request], Any]) -> ASGIApp:
    return RequestBodyLimitMiddleware(request_response(handler), FORM_BODY_LIMIT)
