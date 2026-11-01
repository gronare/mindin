from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from notes_vault_mcp.auth import auth_dir
from notes_vault_mcp.auth.hosted_store import Account, HostedStore
from notes_vault_mcp.auth.hosted_web import Browser, esc
from notes_vault_mcp.auth.pages import stamp
from notes_vault_mcp.backends import NotFound

ACCESS_TABLES = """
CREATE TABLE IF NOT EXISTS support_grants (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id),
    granted_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS vault_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    at INTEGER NOT NULL,
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    action TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vault_access_account ON vault_access(account_id, at);
"""
GRANT_CHOICES = {"hour": ("one hour", 3600), "day": ("one day", 86400), "week": ("seven days", 7 * 86400)}
LOG_ROWS = 50


class NoGrant(PermissionError):
    pass


class SupportAccess:
    def __init__(self, store: HostedStore, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock
        with store.connect() as connection:
            connection.executescript(ACCESS_TABLES)

    def grant(self, account_id: str, seconds: int) -> int:
        moment = int(self.clock())
        with self.store.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO support_grants(account_id, granted_at, expires_at) VALUES(?, ?, ?)",
                (account_id, moment, moment + seconds),
            )
        return moment + seconds

    def revoke(self, account_id: str) -> None:
        with self.store.connect() as connection:
            connection.execute("DELETE FROM support_grants WHERE account_id = ?", (account_id,))

    def grant_until(self, account_id: str) -> int | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM support_grants WHERE account_id = ? AND expires_at > ?",
                (account_id, int(self.clock())),
            ).fetchone()
        return row["expires_at"] if row else None

    def admit(self, account_id: str, operator: str, reason: str, action: str) -> None:
        operator, reason = " ".join(operator.split()), " ".join(reason.split())
        if not operator or not reason:
            raise NoGrant("an operator and a reason are required")
        with self.store.connect() as connection:
            live = connection.execute(
                "SELECT 1 FROM support_grants WHERE account_id = ? AND expires_at > ?",
                (account_id, int(self.clock())),
            ).fetchone()
            if live is None:
                raise NoGrant("the account has not granted support access, or the grant has expired")
            connection.execute(
                "INSERT INTO vault_access(account_id, at, operator, reason, action) VALUES(?, ?, ?, ?, ?)",
                (account_id, int(self.clock()), operator, reason, action),
            )

    def log(self, account_id: str, limit: int = LOG_ROWS) -> list[sqlite3.Row]:
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT at, operator, reason, action FROM vault_access WHERE account_id = ? ORDER BY at DESC, id DESC"
                " LIMIT ?",
                (account_id, limit),
            ).fetchall()


class SupportDesk:
    def __init__(self, browser: Browser, access: SupportAccess) -> None:
        self.browser = browser
        self.access = access

    def section(self, account: Account, csrf: str) -> str:
        until = self.access.grant_until(account.id)
        if until:
            state = (
                f"<p>Support can open your vault until {esc(stamp(until))}.</p>"
                f'<form method="post" action="{esc(self.browser.local("/account/support/end"))}">'
                f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
                '<button type="submit" class="secondary">End support access now</button></form>'
            )
        else:
            choices = "".join(
                f'<label><input type="radio" name="duration" value="{name}"{" checked" if name == "day" else ""}>'
                f" {label}</label>"
                for name, (label, _) in GRANT_CHOICES.items()
            )
            state = (
                "<p>Nobody who runs this service can open your vault unless you allow it here.</p>"
                f'<form method="post" action="{esc(self.browser.local("/account/support"))}">'
                f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
                f"<fieldset><legend>Let support open my vault for</legend>{choices}</fieldset>"
                '<button type="submit" class="secondary">Allow support access</button></form>'
            )
        rows = "".join(
            f"<tr><td>{esc(stamp(row['at']))}</td><td>{esc(row['operator'])}</td><td>{esc(row['reason'])}</td>"
            f"<td>{esc(row['action'])}</td></tr>"
            for row in self.access.log(account.id)
        )
        log = (
            "<table><thead><tr><th>When</th><th>Who</th><th>Why</th><th>What</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            if rows
            else "<p>Nobody from support has opened your vault.</p>"
        )
        return f"<h2>Support access</h2>{state}<h2>Support access log</h2>{log}"

    async def grant(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        account, _, form = submitted
        choice = GRANT_CHOICES.get(form.get("duration", ""))
        if choice is not None:
            self.access.grant(account.id, choice[1])
        return RedirectResponse(self.browser.local("/account"), status_code=303)

    async def end(self, request: Request) -> Response:
        submitted = await self.browser.account_form(request)
        if isinstance(submitted, Response):
            return submitted
        self.access.revoke(submitted[0].id)
        return RedirectResponse(self.browser.local("/account"), status_code=303)

    def routes(self, prefix: str, limited: Callable) -> list[Route]:
        return [
            Route(prefix + "/account/support", endpoint=limited(self.grant), methods=["POST"]),
            Route(prefix + "/account/support/end", endpoint=limited(self.end), methods=["POST"]),
        ]


def command_support(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth.hosted_keys import sealed_vaults_from_env

    store = HostedStore(auth_dir())
    account = store.account(args.account)
    if account is None:
        print(f"notes-vault-mcp: no account {args.account!r}", file=sys.stderr)
        return 1
    action = "list files" if args.support_command == "files" else f"read {args.path}"
    sealed = sealed_vaults_from_env(store)
    try:
        SupportAccess(store).admit(account.id, args.operator, args.reason, action)
    except NoGrant as exc:
        print(f"notes-vault-mcp: refused for {account.email}: {exc}", file=sys.stderr)
        return 1
    backend, _ = sealed.for_account(account.id)
    if args.support_command == "files":
        for entry in backend.list_all():
            print(entry.key)
        return 0
    try:
        data, _ = backend.get_bytes(args.path)
    except NotFound:
        print(f"notes-vault-mcp: no such file: {args.path}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(data)
    sys.stdout.flush()
    return 0
