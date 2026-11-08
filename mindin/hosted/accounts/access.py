from __future__ import annotations

import time
from collections.abc import Callable

from mindin.hosted.accounts.store import Account, HostedStore
from mindin.hosted.accounts.vaults import Vault
from mindin.hosted.database import Row

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
