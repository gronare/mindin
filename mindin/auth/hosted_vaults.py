from __future__ import annotations

import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

from mindin.auth.database import Connection, Row

if TYPE_CHECKING:
    from mindin.auth import AuthConfig

VAULT_ID_PREFIX = "vlt_"
SLUG_MAX = 32
SLUG_RE = re.compile(r"[a-z][a-z0-9]{0,31}")
RESERVED_SLUGS = frozenset({"new"})
FIRST_VAULT_NAME = "Personal"
FIRST_VAULT_SLUG = "notes"
FALLBACK_SLUG = "vault"
NAME_MAX = 64
PERSONAL = "personal"
SHARED = "shared"
KINDS = (PERSONAL, SHARED)
OWNER = "owner"
MEMBER = "member"


@dataclass(frozen=True)
class Vault:
    id: str
    account_id: str
    name: str
    slug: str
    created_at: int
    kind: str = PERSONAL

    @property
    def shared(self) -> bool:
        return self.kind == SHARED


def vault_of(row: Row | None) -> Vault | None:
    if row is None:
        return None
    return Vault(
        id=row["id"],
        account_id=row["account_id"],
        name=row["name"],
        slug=row["slug"],
        created_at=row["created_at"],
        kind=row["kind"],
    )


def vault_resource_url(config: AuthConfig, slug: str) -> str:
    return f"{config.resource_url}?vault={quote(slug, safe='')}"


def slugify(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "", folded).lstrip("0123456789")[:SLUG_MAX]


def clean_name(raw: str) -> str:
    return "".join(character for character in " ".join(raw.split()) if character.isprintable())[:NAME_MAX]


def valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.fullmatch(slug)) and slug not in RESERVED_SLUGS


def free_slug(taken: set[str], base: str) -> str:
    stem = slugify(base) or FALLBACK_SLUG
    candidate, number = stem, 1
    while not valid_slug(candidate) or candidate in taken:
        number += 1
        candidate = stem[: SLUG_MAX - len(str(number))] + str(number)
    return candidate


def holders(connection: Connection, vault_id: str) -> list[str]:
    rows = connection.execute(
        "SELECT account_id FROM vaults WHERE id = ? UNION SELECT account_id FROM vault_members WHERE vault_id = ?",
        (vault_id, vault_id),
    ).fetchall()
    return [row[0] for row in rows]


def reached(connection: Connection, account_ids: list[str]) -> list[Vault]:
    if not account_ids:
        return []
    marks = ", ".join("?" for _ in account_ids)
    rows = connection.execute(
        f"SELECT * FROM vaults WHERE account_id IN ({marks}) OR (kind = 'shared' AND id IN"
        f" (SELECT vault_id FROM vault_members WHERE account_id IN ({marks}))) ORDER BY created_at, rowid",
        (*account_ids, *account_ids),
    ).fetchall()
    return [vault for vault in map(vault_of, rows) if vault is not None]


def taken_slugs(connection: Connection, account_ids: list[str], other_than: str = "") -> set[str]:
    return {vault.slug for vault in reached(connection, account_ids) if vault.id != other_than}


def clash(connection: Connection, account_ids: list[str], slug: str, other_than: str = "") -> Vault | None:
    return next(
        (vault for vault in reached(connection, account_ids) if vault.slug == slug and vault.id != other_than), None
    )


def insert_vault(
    connection: Connection,
    vault_id: str,
    account_id: str,
    name: str,
    slug: str,
    created_at: int,
    kind: str = PERSONAL,
) -> None:
    if kind not in KINDS:
        raise ValueError(f"a vault is {' or '.join(KINDS)}")
    connection.execute(
        "INSERT INTO vaults(id, account_id, name, slug, created_at, kind) VALUES(?, ?, ?, ?, ?, ?)",
        (vault_id, account_id, name, slug, created_at, kind),
    )


def new_vault_id() -> str:
    return VAULT_ID_PREFIX + secrets.token_urlsafe(16)


def add_first_vault(connection: Connection, account_id: str, created_at: int) -> None:
    slug = free_slug(taken_slugs(connection, [account_id]), FIRST_VAULT_SLUG)
    insert_vault(connection, new_vault_id(), account_id, FIRST_VAULT_NAME, slug, created_at)


def rewrite_slugs(connection: Connection) -> None:
    rows = connection.execute("SELECT * FROM vaults ORDER BY created_at, rowid").fetchall()
    every = [vault for vault in map(vault_of, rows) if vault is not None]
    first: dict[str, str] = {}
    for vault in every:
        first.setdefault(vault.account_id, vault.id)
    for vault in every:
        if valid_slug(vault.slug):
            continue
        base = FIRST_VAULT_SLUG if first[vault.account_id] == vault.id else vault.name
        slug = free_slug(taken_slugs(connection, holders(connection, vault.id), vault.id), base)
        connection.execute("UPDATE vaults SET slug = ? WHERE id = ?", (slug, vault.id))


def reaches(connection: Connection, vault: Vault, account_id: str) -> bool:
    if vault.account_id == account_id:
        return True
    if not vault.shared:
        return False
    found = connection.execute(
        "SELECT 1 FROM vault_members WHERE vault_id = ? AND account_id = ?", (vault.id, account_id)
    ).fetchone()
    return found is not None


@dataclass(frozen=True)
class Waiting:
    vault: Vault
    clash: Vault


def accept_invitations(connection: Connection, account_id: str, emails: tuple[str, ...]) -> list[Waiting]:
    if not emails:
        return []
    marks = ", ".join("?" for _ in emails)
    rows = connection.execute(
        "SELECT DISTINCT vaults.* FROM vault_invitations JOIN vaults ON vaults.id = vault_invitations.vault_id"
        f" WHERE vault_invitations.email IN ({marks}) AND vaults.account_id != ? ORDER BY vaults.created_at",
        (*emails, account_id),
    ).fetchall()
    moment = int(time.time())
    waiting: list[Waiting] = []
    for vault in [vault for vault in map(vault_of, rows) if vault is not None]:
        found = clash(connection, [account_id], vault.slug, vault.id)
        if found is not None:
            waiting.append(Waiting(vault, found))
            continue
        connection.execute(
            "INSERT INTO vault_members(vault_id, account_id, joined_at) VALUES(?, ?, ?)"
            " ON CONFLICT (vault_id, account_id) DO NOTHING",
            (vault.id, account_id, moment),
        )
        connection.execute(
            f"DELETE FROM vault_invitations WHERE vault_id = ? AND email IN ({marks})", (vault.id, *emails)
        )
    return waiting


def invited(connection: Connection, emails: tuple[str, ...]) -> bool:
    if not emails:
        return False
    marks = ", ".join("?" for _ in emails)
    return (
        connection.execute(f"SELECT 1 FROM vault_invitations WHERE email IN ({marks})", emails).fetchone() is not None
    )
