from __future__ import annotations

from typing import Any

from mindin.hosted.accounts.vaults import Vault
from mindin.shared import Actor

ALLOWED = "allowed"
DENIED = "denied"
SHARING = (ALLOWED, DENIED)


def domain_of(email: str) -> str:
    return email.strip().lower().rpartition("@")[2].rstrip(".")


def matching(connection: Any, emails: tuple[str, ...]) -> list[tuple[str, str]]:
    found = []
    for email in emails:
        row = connection.execute(
            "SELECT organisation_id FROM organisation_domains WHERE domain = ? AND verified_at IS NOT NULL",
            (domain_of(email),),
        ).fetchone()
        if row is not None:
            found.append((row["organisation_id"], email.strip().lower()))
    return found


def on_a_domain(connection: Any, emails: tuple[str, ...]) -> bool:
    return any(not departed(connection, org_id, emails) for org_id, _ in matching(connection, emails))


def departed(connection: Any, organisation_id: str, emails: tuple[str, ...], account_id: str | None = None) -> bool:
    cleaned = [email.strip().lower() for email in emails]
    marks = ", ".join("?" for _ in cleaned) or "''"
    return (
        connection.execute(
            "SELECT 1 FROM organisation_departures WHERE organisation_id = ?"
            f" AND (former_account = ? OR email IN ({marks}))",
            (organisation_id, account_id, *cleaned),
        ).fetchone()
        is not None
    )


def at_verified_domain(connection: Any, organisation_id: str, email: str) -> bool:
    return any(found == organisation_id for found, _ in matching(connection, (email,)))


def domain_address(connection: Any, emails: tuple[str, ...]) -> str:
    return next((email for organisation_id, email in matching(connection, emails)), "")


def inside(connection: Any, organisation_id: str, email: str) -> bool:
    if departed(connection, organisation_id, (email,)):
        return False
    member = connection.execute(
        "SELECT 1 FROM organisation_members JOIN accounts ON accounts.id = organisation_members.account_id"
        " WHERE organisation_members.organisation_id = ? AND accounts.email = ?",
        (organisation_id, email),
    ).fetchone()
    return member is not None or any(found == organisation_id for found, _ in matching(connection, (email,)))


def sharing_refusal(connection: Any, vault: Vault, email: str) -> str:
    owning = connection.execute(
        "SELECT organisations.* FROM organisation_vaults JOIN organisations"
        " ON organisations.id = organisation_vaults.organisation_id WHERE organisation_vaults.vault_id = ?",
        (vault.id,),
    ).fetchone()
    if owning is not None:
        if inside(connection, owning["id"], email):
            return ""
        return (
            f"{email} is outside {owning['name']}, and an organisation's vaults are only for its members. "
            f"An admin of {owning['name']} can add them to the organisation instead."
        )
    rows = connection.execute(
        "SELECT organisations.* FROM organisation_members JOIN organisations"
        " ON organisations.id = organisation_members.organisation_id"
        " WHERE organisation_members.account_id = ? AND organisations.external_sharing = ?"
        " ORDER BY organisations.created_at",
        (vault.account_id, DENIED),
    ).fetchall()
    for row in rows:
        if not inside(connection, row["id"], email):
            return (
                f"{row['name']} does not let its members share vaults with people outside it, and {email} is "
                f"outside. An admin of {row['name']} can allow it on the organisation's page."
            )
    return ""


def locked_out(connection: Any, vault_id: str, actor: Actor) -> str:
    row = connection.execute(
        "SELECT vaults.name, organisation_vaults.locked FROM organisation_vaults"
        " JOIN vaults ON vaults.id = organisation_vaults.vault_id WHERE organisation_vaults.vault_id = ?",
        (vault_id,),
    ).fetchone()
    if row is None or not row["locked"] or actor.account_id is None:
        return ""
    editors = connection.execute(
        "SELECT accounts.id, accounts.email FROM organisation_editors"
        " JOIN accounts ON accounts.id = organisation_editors.account_id WHERE organisation_editors.vault_id = ?"
        " ORDER BY organisation_editors.added_at, accounts.email",
        (vault_id,),
    ).fetchall()
    if any(editor["id"] == actor.account_id for editor in editors):
        return ""
    names = ", ".join(editor["email"] for editor in editors)
    who = f"its editors ({names})" if names else "its editors, and it has none yet"
    return (
        f"{row['name']} is locked: only {who} change it. Nothing was written. "
        "Write the change up as a suggestion for an editor instead."
    )
