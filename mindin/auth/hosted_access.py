from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from mindin.auth import auth_dir
from mindin.auth.database import Row
from mindin.auth.hosted_store import Account, HostedStore
from mindin.auth.hosted_vaults import SUPPORT_TABLES, Vault
from mindin.auth.hosted_web import Browser, esc
from mindin.auth.pages import stamp
from mindin.backends import NotFound

GRANT_CHOICES = {"hour": ("one hour", 3600), "day": ("one day", 86400), "week": ("seven days", 7 * 86400)}
LOG_ROWS = 50


class NoGrant(PermissionError):
    pass


class SupportAccess:
    def __init__(self, store: HostedStore, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock
        with store.connect() as connection:
            store.database.create(connection, ";\n".join(SUPPORT_TABLES))

    def grant(self, vault: Vault, seconds: int) -> int:
        moment = int(self.clock())
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO vault_support_grants(vault_id, account_id, granted_at, expires_at) VALUES(?, ?, ?, ?)"
                " ON CONFLICT (vault_id) DO UPDATE SET account_id = excluded.account_id,"
                " granted_at = excluded.granted_at, expires_at = excluded.expires_at",
                (vault.id, vault.account_id, moment, moment + seconds),
            )
        return moment + seconds

    def revoke(self, account_id: str, vault_id: str) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "DELETE FROM vault_support_grants WHERE vault_id = ? AND account_id = ?", (vault_id, account_id)
            )

    def grant_until(self, vault_id: str) -> int | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM vault_support_grants WHERE vault_id = ? AND expires_at > ?",
                (vault_id, int(self.clock())),
            ).fetchone()
        return row["expires_at"] if row else None

    def admit(self, vault: Vault, operator: str, reason: str, action: str) -> None:
        operator, reason = " ".join(operator.split()), " ".join(reason.split())
        if not operator or not reason:
            raise NoGrant("an operator and a reason are required")
        with self.store.connect() as connection:
            live = connection.execute(
                "SELECT 1 FROM vault_support_grants WHERE vault_id = ? AND expires_at > ?",
                (vault.id, int(self.clock())),
            ).fetchone()
            if live is None:
                raise NoGrant(f"the vault {vault.slug} has no support grant, or the grant has expired")
            connection.execute(
                "INSERT INTO vault_access(account_id, vault_id, at, operator, reason, action) VALUES(?, ?, ?, ?, ?, ?)",
                (vault.account_id, vault.id, int(self.clock()), operator, reason, action),
            )

    def log(self, account_id: str, limit: int = LOG_ROWS) -> list[Row]:
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT vault_access.at, vault_access.operator, vault_access.reason, vault_access.action,"
                " vaults.name AS vault_name FROM vault_access LEFT JOIN vaults ON vaults.id = vault_access.vault_id"
                " WHERE vault_access.account_id = ? ORDER BY vault_access.at DESC, vault_access.id DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()


class SupportDesk:
    def __init__(self, browser: Browser, access: SupportAccess) -> None:
        self.browser = browser
        self.access = access

    def section(self, account: Account, csrf: str) -> str:
        vaults = self.browser.store.vaults(account.id)
        granted = [(vault, self.access.grant_until(vault.id)) for vault in vaults]
        live = "".join(
            f'<li class="row"><span>{esc(vault.name)}: open to support until {esc(stamp(until))}</span>'
            f'<form method="post" action="{esc(self.browser.local("/account/support/end"))}">'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
            f'<input type="hidden" name="vault" value="{esc(vault.slug)}">'
            f'<button type="submit" class="btn secondary small">End now</button></form></li>'
            for vault, until in granted
            if until
        )
        offered = [vault for vault, until in granted if not until]
        form = ""
        if offered:
            vault_choices = "".join(
                f'<option value="{esc(vault.slug)}">{esc(vault.name)}</option>' for vault in offered
            )
            durations = "".join(
                f'<option value="{name}"{" selected" if name == "day" else ""}>{label}</option>'
                for name, (label, _) in GRANT_CHOICES.items()
            )
            form = (
                f'<form class="row" method="post" action="{esc(self.browser.local("/account/support"))}">'
                f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
                f'<label class="field">Vault<select name="vault">{vault_choices}</select></label>'
                f'<label class="field">For<select name="duration">{durations}</select></label>'
                '<button type="submit" class="btn secondary">Allow support access</button></form>'
            )
        rows = "".join(
            f'<tr><td class="nowrap">{esc(stamp(row["at"]))}</td><td>{esc(row["vault_name"] or "a deleted vault")}</td>'
            f"<td>{esc(row['operator'])}</td><td>{esc(row['reason'])}</td><td>{esc(row['action'])}</td></tr>"
            for row in self.access.log(account.id)
        )
        log = (
            '<div class="tablewrap"><table><thead><tr><th>When</th><th>Vault</th><th>Who</th><th>Why</th><th>What</th>'
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
            if rows
            else '<p class="small muted">Nobody from support has opened your vaults.</p>'
        )
        granted_list = f'<ul class="plain stack">{live}</ul>' if live else ""
        return (
            '<div class="box"><h2>Support access</h2><p class="small muted">Support opens a vault only through a grant '
            "you give here, for one vault and a set time, and every look is logged.</p>"
            f"{granted_list}{form}{log}</div>"
        )

    async def grant(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, _, form = submitted
        choice = GRANT_CHOICES.get(form.get("duration", ""))
        vault = self.browser.store.own_vault(account.id, form.get("vault", ""))
        if choice is not None and vault is not None:
            self.access.grant(vault, choice[1])
        return RedirectResponse(self.browser.local("/account/settings"), status_code=303)

    async def end(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, _, form = submitted
        vault = self.browser.store.own_vault(account.id, form.get("vault", ""))
        if vault is not None:
            self.access.revoke(account.id, vault.id)
        return RedirectResponse(self.browser.local("/account/settings"), status_code=303)

    def routes(self, prefix: str, limited: Callable) -> list[Route]:
        return [
            Route(prefix + "/account/support", endpoint=limited(self.grant), methods=["POST"]),
            Route(prefix + "/account/support/end", endpoint=limited(self.end), methods=["POST"]),
        ]


def chosen_vault(store: HostedStore, account: Account, ref: str | None) -> Vault | None:
    if ref:
        return store.own_vault(account.id, ref)
    vaults = store.vaults(account.id)
    return vaults[0] if len(vaults) == 1 else None


def command_support(args: argparse.Namespace) -> int:
    from mindin.auth.hosted_keys import sealed_vaults_from_env

    store = HostedStore(auth_dir())
    account = store.account(args.account)
    if account is None:
        print(f"mindin: no account {args.account!r}", file=sys.stderr)
        return 1
    vault = chosen_vault(store, account, args.vault)
    if vault is None:
        print(f"mindin: name one of {account.email}'s vaults with --vault <slug>", file=sys.stderr)
        return 1
    action = "list files" if args.support_command == "files" else f"read {args.path}"
    sealed = sealed_vaults_from_env(store)
    try:
        SupportAccess(store).admit(vault, args.operator, args.reason, action)
    except NoGrant as exc:
        print(f"mindin: refused for {account.email}: {exc}", file=sys.stderr)
        return 1
    backend, _ = sealed.for_vault(vault.id)
    if args.support_command == "files":
        for entry in backend.list_all():
            print(entry.key)
        return 0
    try:
        data, _ = backend.get_bytes(args.path)
    except NotFound:
        print(f"mindin: no such file: {args.path}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(data)
    sys.stdout.flush()
    return 0
