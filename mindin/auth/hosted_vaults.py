from __future__ import annotations

import re
import secrets
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

from mindin.auth.database import add_column, has_table

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

VAULT_TABLES = (
    """CREATE TABLE IF NOT EXISTS vaults (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS vaults_account ON vaults(account_id)",
)
DATA_KEY_TABLES = (
    """CREATE TABLE IF NOT EXISTS vault_data_keys (
    vault_id TEXT PRIMARY KEY REFERENCES vaults(id),
    key_id TEXT NOT NULL,
    wrapped BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    rewrapped_at INTEGER
)""",
)
SUPPORT_TABLES = (
    """CREATE TABLE IF NOT EXISTS vault_support_grants (
    vault_id TEXT PRIMARY KEY REFERENCES vaults(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    granted_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS vault_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    vault_id TEXT,
    at INTEGER NOT NULL,
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    action TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS vault_access_account ON vault_access(account_id, at)",
)
SHARED_TABLES = (
    """CREATE TABLE IF NOT EXISTS vault_members (
    vault_id TEXT NOT NULL REFERENCES vaults(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (vault_id, account_id)
)""",
    "CREATE INDEX IF NOT EXISTS vault_members_account ON vault_members(account_id)",
    """CREATE TABLE IF NOT EXISTS vault_invitations (
    vault_id TEXT NOT NULL REFERENCES vaults(id),
    email TEXT NOT NULL COLLATE NOCASE,
    invited_by TEXT NOT NULL,
    link_hash TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (vault_id, email)
)""",
    """CREATE TABLE IF NOT EXISTS vault_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_id TEXT NOT NULL REFERENCES vaults(id),
    path TEXT NOT NULL,
    from_path TEXT,
    action TEXT NOT NULL CHECK (action IN ('created', 'updated', 'moved', 'deleted')),
    author_email TEXT NOT NULL,
    author TEXT,
    client TEXT NOT NULL,
    at INTEGER NOT NULL,
    before TEXT,
    after TEXT,
    restored_from INTEGER
)""",
    "CREATE INDEX IF NOT EXISTS vault_changes_vault ON vault_changes(vault_id, id)",
)
VAULT_COLUMNS = (
    ("api_keys", "vault_id", "TEXT REFERENCES vaults(id)"),
    ("tokens", "vault_id", "TEXT REFERENCES vaults(id)"),
    ("devices", "vault_id", "TEXT REFERENCES vaults(id)"),
    ("devices", "requested_vault", "TEXT"),
)


class MigrationError(RuntimeError):
    pass


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


def vault_of(row: sqlite3.Row | None) -> Vault | None:
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


def holders(connection: sqlite3.Connection, vault_id: str) -> list[str]:
    rows = connection.execute(
        "SELECT account_id FROM vaults WHERE id = ? UNION SELECT account_id FROM vault_members WHERE vault_id = ?",
        (vault_id, vault_id),
    ).fetchall()
    return [row[0] for row in rows]


def reached(connection: sqlite3.Connection, account_ids: list[str]) -> list[Vault]:
    if not account_ids:
        return []
    marks = ", ".join("?" for _ in account_ids)
    rows = connection.execute(
        f"SELECT * FROM vaults WHERE account_id IN ({marks}) OR (kind = 'shared' AND id IN"
        f" (SELECT vault_id FROM vault_members WHERE account_id IN ({marks}))) ORDER BY created_at, rowid",
        (*account_ids, *account_ids),
    ).fetchall()
    return [vault for vault in map(vault_of, rows) if vault is not None]


def taken_slugs(connection: sqlite3.Connection, account_ids: list[str], other_than: str = "") -> set[str]:
    return {vault.slug for vault in reached(connection, account_ids) if vault.id != other_than}


def clash(connection: sqlite3.Connection, account_ids: list[str], slug: str, other_than: str = "") -> Vault | None:
    return next(
        (vault for vault in reached(connection, account_ids) if vault.slug == slug and vault.id != other_than), None
    )


def insert_vault(
    connection: sqlite3.Connection,
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


def add_first_vault(connection: sqlite3.Connection, account_id: str, created_at: int) -> None:
    slug = free_slug(taken_slugs(connection, [account_id]), FIRST_VAULT_SLUG)
    insert_vault(connection, new_vault_id(), account_id, FIRST_VAULT_NAME, slug, created_at)


def count(connection: sqlite3.Connection, sql: str) -> int:
    return int(connection.execute(sql).fetchone()[0])


# An existing account's vault takes the account's id, so its storage prefix, index and key wrapping stay as they are.
def to_vaults(connection: sqlite3.Connection) -> None:
    for statement in VAULT_TABLES:
        connection.execute(statement)
    for table, column, declaration in VAULT_COLUMNS:
        add_column(connection, table, column, declaration)
    unvaulted = connection.execute(
        "SELECT id, created_at FROM accounts WHERE id NOT IN (SELECT account_id FROM vaults) ORDER BY rowid"
    ).fetchall()
    for row in unvaulted:
        connection.execute(
            "INSERT INTO vaults(id, account_id, name, slug, created_at) VALUES(?, ?, ?, ?, ?)",
            (row["id"], row["id"], FIRST_VAULT_NAME, "-" + row["id"], row["created_at"]),
        )
    connection.execute("UPDATE api_keys SET vault_id = account_id WHERE vault_id IS NULL")
    connection.execute("UPDATE tokens SET vault_id = account_id WHERE vault_id IS NULL")
    if has_table(connection, "vault_keys"):
        move_data_keys(connection)
    if has_table(connection, "support_grants"):
        for statement in SUPPORT_TABLES[:1]:
            connection.execute(statement)
        # SQLite cannot parse ON CONFLICT after a SELECT without a WHERE, hence WHERE true.
        connection.execute(
            "INSERT INTO vault_support_grants(vault_id, account_id, granted_at, expires_at)"
            " SELECT account_id, account_id, granted_at, expires_at FROM support_grants WHERE true"
            " ON CONFLICT (vault_id) DO UPDATE SET account_id = excluded.account_id,"
            " granted_at = excluded.granted_at, expires_at = excluded.expires_at"
        )
        connection.execute("DROP TABLE support_grants")
    if has_table(connection, "vault_access"):
        add_column(connection, "vault_access", "vault_id", "TEXT")
        connection.execute("UPDATE vault_access SET vault_id = account_id WHERE vault_id IS NULL")


def move_data_keys(connection: sqlite3.Connection) -> None:
    for statement in DATA_KEY_TABLES:
        connection.execute(statement)
    # SQLite cannot parse ON CONFLICT after a SELECT without a WHERE, hence WHERE true.
    connection.execute(
        "INSERT INTO vault_data_keys(vault_id, key_id, wrapped, created_at, rewrapped_at)"
        " SELECT account_id, key_id, wrapped, created_at, rewrapped_at FROM vault_keys WHERE true"
        " ON CONFLICT (vault_id) DO NOTHING"
    )
    before = count(connection, "SELECT COUNT(*) FROM vault_keys")
    moved = count(
        connection,
        "SELECT COUNT(*) FROM vault_keys JOIN vault_data_keys ON vault_data_keys.vault_id = vault_keys.account_id"
        " AND vault_data_keys.key_id = vault_keys.key_id AND vault_data_keys.wrapped = vault_keys.wrapped",
    )
    if moved != before:
        raise MigrationError(f"only {moved} of {before} vault keys arrived intact; nothing was changed")
    connection.execute("DROP TABLE vault_keys")


def to_shared(connection: sqlite3.Connection) -> None:
    add_column(connection, "vaults", "kind", "TEXT NOT NULL DEFAULT 'personal' CHECK (kind IN ('personal', 'shared'))")
    for statement in SHARED_TABLES:
        connection.execute(statement)


ACCOUNT_SLUG_VAULTS = """CREATE TABLE vaults_by_account (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'personal' CHECK (kind IN ('personal', 'shared'))
)"""


def to_account_slugs(connection: sqlite3.Connection) -> None:
    connection.execute(ACCOUNT_SLUG_VAULTS)
    connection.execute(
        "INSERT INTO vaults_by_account(id, account_id, name, slug, created_at, kind)"
        " SELECT id, account_id, name, slug, created_at, kind FROM vaults ORDER BY rowid"
    )
    connection.execute("DROP TABLE vaults")
    connection.execute("ALTER TABLE vaults_by_account RENAME TO vaults")
    connection.execute("CREATE INDEX IF NOT EXISTS vaults_account ON vaults(account_id)")
    rewrite_slugs(connection)
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS vaults_account_slug ON vaults(account_id, slug)")
    connection.execute("UPDATE tokens SET revoked = 1 WHERE resource LIKE '%/v/%'")


def rewrite_slugs(connection: sqlite3.Connection) -> None:
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


MIGRATIONS = (to_vaults, to_shared, to_account_slugs)


def reaches(connection: sqlite3.Connection, vault: Vault, account_id: str) -> bool:
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


def accept_invitations(connection: sqlite3.Connection, account_id: str, emails: tuple[str, ...]) -> list[Waiting]:
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


def invited(connection: sqlite3.Connection, emails: tuple[str, ...]) -> bool:
    if not emails:
        return False
    marks = ", ".join("?" for _ in emails)
    return (
        connection.execute(f"SELECT 1 FROM vault_invitations WHERE email IN ({marks})", emails).fetchone() is not None
    )
