from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable

from mindin.auth import auth_dir
from mindin.auth.database import Row
from mindin.auth.hosted_store import Account, HostedStore
from mindin.auth.hosted_vaults import Vault
from mindin.backends import NotFound

GRANT_CHOICES = {"hour": ("one hour", 3600), "day": ("one day", 86400), "week": ("seven days", 7 * 86400)}
LOG_ROWS = 50


class NoGrant(PermissionError):
    pass


class SupportAccess:
    def __init__(self, store: HostedStore, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

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
