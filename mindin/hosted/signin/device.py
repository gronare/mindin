from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

from mcp.shared.auth import OAuthClientInformationFull
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mindin.hosted.accounts.reach import ALSO_REACHES
from mindin.hosted.accounts.store import Account, HostedStore
from mindin.hosted.accounts.vaults import Vault
from mindin.hosted.config import MACHINE_KEY_PURPOSE, READ_SCOPE, SCOPES, WRITE_SCOPE, AuthConfig
from mindin.hosted.database import Row
from mindin.hosted.pages.html import error_box, esc
from mindin.hosted.pages.web import NO_STORE, Browser, Limits, client_address, main_vault_choice, oauth_error, page

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLI_CLIENT_ID = "cli"
CLI_CLIENT_NAME = "A command-line tool"
DEVICE_TTL = 600
DEVICE_INTERVAL = 5
SLOW_DOWN_STEP = 5
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
USER_CODE_LENGTH = 8

ClientLookup = Callable[[str], Awaitable[OAuthClientInformationFull | None]]


def new_user_code() -> str:
    return "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))


def normalize_user_code(raw: str) -> str:
    return "".join(character for character in raw.upper() if character in USER_CODE_ALPHABET)[:USER_CODE_LENGTH]


def shown(user_code: str) -> str:
    return f"{user_code[:4]}-{user_code[4:]}"


def machine_name(raw: object, fallback: str) -> str:
    text = " ".join(str(raw or "").split())
    return "".join(character for character in text if character.isprintable())[:64] or fallback


class DeviceFlow:
    def __init__(
        self,
        config: AuthConfig,
        store: HostedStore,
        clients: ClientLookup,
        browser: Browser,
        limits: Limits,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = store
        self.clients = clients
        self.browser = browser
        self.limits = limits
        self.clock = clock

    @property
    def verification_uri(self) -> str:
        return f"{self.config.public_url.rstrip('/')}/device"

    async def start(self, request: Request) -> Response:
        if not self.limits.device_start.allow(client_address(request)):
            return oauth_error("slow_down", "too many device authorizations from this address", 429)
        form = await request.form()
        client_id = str(form.get("client_id") or CLI_CLIENT_ID)
        client_name = CLI_CLIENT_NAME
        if client_id != CLI_CLIENT_ID:
            client = await self.clients(client_id)
            if client is None:
                return oauth_error("invalid_client", "unknown client_id", 401)
            if client.token_endpoint_auth_method != "none":
                return oauth_error("unauthorized_client", "the device grant is for public clients only")
            client_name = client.client_name or client_id
        requested = str(form.get("scope") or " ".join(SCOPES)).split()
        if not requested or not set(requested) <= set(SCOPES):
            return oauth_error("invalid_scope", f"scope must be among {' '.join(SCOPES)}")
        scopes = [scope for scope in SCOPES if scope in requested or scope == READ_SCOPE]
        device_code = secrets.token_urlsafe(32)
        moment = int(self.clock())
        record: dict[str, Any] = {
            "client_id": client_id,
            "client_name": client_name,
            "name": machine_name(form.get("name"), client_name),
            "scopes": scopes,
            "vault": str(form.get("vault") or "").strip().lower() or None,
            "interval": DEVICE_INTERVAL,
            "created_at": moment,
            "expires_at": moment + DEVICE_TTL,
        }
        for _ in range(5):
            user_code = new_user_code()
            try:
                self.store.save_device(device_code, user_code, record)
                break
            except self.store.database.IntegrityError:
                continue
        else:
            return oauth_error("temporarily_unavailable", "could not allocate a user code", 503)
        return JSONResponse(
            {
                "device_code": device_code,
                "user_code": shown(user_code),
                "verification_uri": self.verification_uri,
                "verification_uri_complete": f"{self.verification_uri}?user_code={shown(user_code)}",
                "expires_in": DEVICE_TTL,
                "interval": DEVICE_INTERVAL,
            },
            headers=NO_STORE,
        )

    async def token(self, request: Request, form: FormData) -> Response:
        device_code = str(form.get("device_code") or "")
        row = self.store.device(device_code) if device_code else None
        client_id = str(form.get("client_id") or CLI_CLIENT_ID)
        if row is None or row["client_id"] != client_id:
            if not self.limits.token_misses.allow(client_address(request)):
                return oauth_error("slow_down", "too many unknown device codes from this address", 429)
            return oauth_error("invalid_grant", "unknown device_code")
        moment = self.clock()
        if row["expires_at"] < moment:
            self.store.drop_device(row["device_hash"])
            return oauth_error("expired_token", "the device code expired; start again")
        if row["last_poll_at"] is not None and moment - row["last_poll_at"] < row["interval"]:
            self.store.update_device(row["device_hash"], last_poll_at=moment, interval=row["interval"] + SLOW_DOWN_STEP)
            return oauth_error("slow_down", f"poll at most every {row['interval'] + SLOW_DOWN_STEP} seconds")
        self.store.update_device(row["device_hash"], last_poll_at=moment)
        if row["status"] == "pending":
            return oauth_error("authorization_pending", "waiting for the user to approve the code")
        consumed = self.store.drop_device(row["device_hash"])
        if row["status"] != "approved" or not consumed:
            return oauth_error("access_denied", "the user denied the request")
        account = self.store.account(row["account_id"] or "")
        if account is None or not account.active:
            return oauth_error("access_denied", "the account is not active")
        vault = self.store.vault(row["vault_id"] or "")
        if vault is None or not self.store.reaches(vault, account.id):
            return oauth_error("access_denied", "the vault is gone")
        scopes = row["granted"].split()
        key = self.store.create_key(
            account.id, row["name"], scopes, purpose=MACHINE_KEY_PURPOSE, vault_id=vault.id, spans_account=True
        )
        return JSONResponse(
            {
                "access_token": key,
                "token_type": "Bearer",
                "scope": " ".join(scopes),
                "key_name": row["name"],
                "resource": self.config.resource_url,
                "vault": vault.slug,
                "vault_name": vault.name,
            },
            headers=NO_STORE,
        )

    async def page(self, request: Request) -> Response:
        user_code = normalize_user_code(request.query_params.get("user_code", ""))
        return_to = self.browser.local("/device") + (f"?user_code={quote(shown(user_code))}" if user_code else "")
        session = await self.browser.require_session(request, return_to)
        if isinstance(session, Response):
            return session
        account, csrf = session
        row = self.pending(user_code) if len(user_code) == USER_CODE_LENGTH else None
        if row is not None:
            return self.confirm(row, account, csrf)
        return self.entry(csrf, shown(user_code) if user_code else "")

    def pending(self, user_code: str) -> Row | None:
        row = self.store.device_by_user_code(user_code)
        return row if row is not None and row["expires_at"] >= self.clock() else None

    def entry(self, csrf: str, prefill: str = "", error: str = "", status_code: int = 200) -> Response:
        body = f"""{error_box(error)}
<form method="post" action="{esc(self.browser.local("/device"))}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<label>Code
<input type="text" name="user_code" value="{esc(prefill)}" autocomplete="one-time-code" autocapitalize="characters"
 spellcheck="false" maxlength="12" required autofocus>
</label>
<button type="submit" name="action" value="lookup">Continue</button>
</form>"""
        return page("Connect a device", "Enter the code the device shows.", body, status_code=status_code)

    def vault_choice(self, row: Row, account: Account) -> tuple[str, list[Vault]]:
        requested = row["requested_vault"]
        if requested:
            vault = self.store.reachable_vault(account.id, requested)
            return (requested, [vault]) if vault is not None and vault.slug == requested else (requested, [])
        return "", self.store.reachable_vaults(account.id)

    def vault_field(self, account: Account, requested: str, vaults: list[Vault]) -> tuple[str, str]:
        if requested:
            vault = vaults[0]
            return (
                f"<dt>Main vault</dt><dd>{esc(vault.name)} <code>{esc(vault.slug)}</code></dd>",
                f'<input type="hidden" name="vault" value="{esc(vault.slug)}">',
            )
        return "", main_vault_choice(vaults, self.store.default_vault(account.id))

    def confirm(self, row: Row, account: Account, csrf: str) -> Response:
        requested, vaults = self.vault_choice(row, account)
        if not vaults:
            lead = (
                f"The device asks for the vault {requested}, which is not one of yours."
                if requested
                else "You have no vault yet. Make one on your account page first."
            )
            return page("Connect a device", lead, status_code=403 if requested else 400)
        write_choice = (
            '<label><input type="radio" name="access" value="write" checked> read and write</label>'
            if WRITE_SCOPE in row["scopes"].split()
            else ""
        )
        read_checked = "" if write_choice else " checked"
        vault_fact, vault_field = self.vault_field(account, requested, vaults)
        body = f"""<p class="warning">Approve only if you started this yourself, just now, on a machine you use.</p>
<dl><dt>Code</dt><dd><code>{esc(shown(row["user_code"]))}</code></dd>
<dt>Asked by</dt><dd>{esc(row["client_name"])}</dd>
<dt>The key will be named</dt><dd>{esc(row["name"])}</dd>{vault_fact}</dl>
<p>{esc(ALSO_REACHES)}</p>
<form method="post" action="{esc(self.browser.local("/device"))}">
<input type="hidden" name="csrf" value="{esc(csrf)}">
<input type="hidden" name="user_code" value="{esc(row["user_code"])}">
{vault_field}
<fieldset><legend>Access</legend>
<label><input type="radio" name="access" value="read"{read_checked}> read</label>
{write_choice}
</fieldset>
<button type="submit" name="action" value="approve">Approve</button>
<button type="submit" name="action" value="deny" class="secondary">Deny</button>
</form>"""
        return page("Connect a device", "A device asks for a key to your vaults.", body)

    async def submit(self, request: Request) -> Response:
        form = await request.form()
        session = self.browser.signed_in(request)
        if session is None or not session[0].active or not self.browser.csrf_ok(form.get("csrf"), session[1]):
            return page("Not signed in", "Open the device page again.", status_code=403)
        account, csrf = session
        user_code = normalize_user_code(str(form.get("user_code") or ""))
        row = self.pending(user_code) if len(user_code) == USER_CODE_LENGTH else None
        if row is None:
            if not self.limits.device_lookup.allow(account.id):
                return self.entry(csrf, error="Too many wrong codes. Wait a minute.", status_code=429)
            return self.entry(csrf, error="That code is unknown or has expired.", status_code=400)
        action = form.get("action")
        if action == "lookup":
            return self.confirm(row, account, csrf)
        if action == "deny":
            self.store.update_device(row["device_hash"], status="denied", account_id=account.id)
            return page("Denied", "The device gets no key. You can close this page.")
        requested, vaults = self.vault_choice(row, account)
        vault = next((vault for vault in vaults if vault.slug == form.get("vault")), None)
        if action != "approve" or vault is None:
            return self.confirm(row, account, csrf)
        scopes = [READ_SCOPE]
        if form.get("access") == "write" and WRITE_SCOPE in row["scopes"].split():
            scopes.append(WRITE_SCOPE)
        self.store.update_device(
            row["device_hash"], status="approved", account_id=account.id, granted=" ".join(scopes), vault_id=vault.id
        )
        return page("Approved", f"Go back to {row['name']}; it picks up its key to {vault.name} within a few seconds.")
