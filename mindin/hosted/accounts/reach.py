from __future__ import annotations

from typing import Any

from mindin.hosted.accounts.store import HostedStore, not_searched
from mindin.hosted.accounts.vaults import Vault, reached, vault_of
from mindin.hosted.config import MACHINE_KEY_PURPOSE
from mindin.hosted.database import Connection
from mindin.vault import Reachable

ACCOUNT_REACH = "account"
SPANNING_PURPOSES = (None, MACHINE_KEY_PURPOSE)
ALSO_REACHES = (
    "It also reaches every other vault of yours: your own, the shared vaults you belong to and your organisations' "
    "vaults, when a tool names one. Without a name it reads and writes the main vault."
)


class AccountReach:
    def __init__(self, store: HostedStore) -> None:
        self.store = store

    def around(self, claims: dict[str, Any], subject: str) -> list[Reachable]:
        account_id = claims.get("account")
        if not account_id or claims.get("key_purpose") not in SPANNING_PURPOSES:
            return []
        with self.store.connect() as connection:
            main = vault_of(connection.execute("SELECT * FROM vaults WHERE id = ?", (subject,)).fetchone())
            if main is None:
                return []
            organisations = organisation_names(connection, account_id)
            off = not_searched(connection, account_id)
            everything = reached(connection, [account_id])
            if claims.get("reach") == ACCOUNT_REACH:
                others = [vault for vault in everything if vault.account_id == account_id]
                others += [vault for vault in everything if vault.account_id != account_id]
            else:
                others = [vault for vault in everything if vault.account_id == account_id][:1]
                others += [vault for vault in everything if vault.id in organisations]
        found: dict[str, Reachable] = {main.id: reachable(main, organisations, main=True)}
        for vault in others:
            found.setdefault(vault.id, reachable(vault, organisations, searched=vault.id not in off))
        return list(found.values()) if len(found) > 1 else []


def reachable(vault: Vault, organisations: dict[str, str], main: bool = False, searched: bool = True) -> Reachable:
    return Reachable(vault.id, vault.slug, vault.name, organisations.get(vault.id, ""), main, searched, vault.shared)


def organisation_names(connection: Connection, account_id: str) -> dict[str, str]:
    rows = connection.execute(
        "SELECT organisation_vaults.vault_id, organisations.name FROM organisation_members"
        " JOIN organisations ON organisations.id = organisation_members.organisation_id"
        " JOIN organisation_vaults ON organisation_vaults.organisation_id = organisations.id"
        " WHERE organisation_members.account_id = ?",
        (account_id,),
    ).fetchall()
    return {row["vault_id"]: row["name"] for row in rows}
