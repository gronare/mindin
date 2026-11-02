from __future__ import annotations

import re
import secrets
import sqlite3
import time
import unicodedata
from dataclasses import dataclass

from notes_vault_mcp.auth.database import add_column, has_table

VAULT_ID_PREFIX = "vlt_"
SLUG_MAX = 40
SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
RESERVED_SLUGS = frozenset({"new"})
FIRST_VAULT_NAME = "Personal"
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


def slugify(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:SLUG_MAX].strip("-")


def clean_name(raw: str) -> str:
    return "".join(character for character in " ".join(raw.split()) if character.isprintable())[:NAME_MAX]


def valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.fullmatch(slug)) and slug not in RESERVED_SLUGS


def slug_taken(connection: sqlite3.Connection, slug: str) -> bool:
    return connection.execute("SELECT 1 FROM vaults WHERE slug = ?", (slug,)).fetchone() is not None


def free_slug(connection: sqlite3.Connection, base: str) -> str:
    stem = (slugify(base) or "vault")[: SLUG_MAX - 4].strip("-") or "vault"
    candidate, number = stem, 1
    while not valid_slug(candidate) or slug_taken(connection, candidate):
        number += 1
        candidate = f"{stem}-{number}"
    return candidate


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


def add_first_vault(connection: sqlite3.Connection, account_id: str, email: str, created_at: int) -> None:
    slug = free_slug(connection, email.partition("@")[0])
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
        "SELECT id, email, created_at FROM accounts WHERE id NOT IN (SELECT account_id FROM vaults) ORDER BY rowid"
    ).fetchall()
    for row in unvaulted:
        slug = free_slug(connection, row["email"].partition("@")[0])
        connection.execute(
            "INSERT INTO vaults(id, account_id, name, slug, created_at) VALUES(?, ?, ?, ?, ?)",
            (row["id"], row["id"], FIRST_VAULT_NAME, slug, row["created_at"]),
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


MIGRATIONS = (to_vaults, to_shared)


def reaches(connection: sqlite3.Connection, vault: Vault, account_id: str) -> bool:
    if vault.account_id == account_id:
        return True
    if not vault.shared:
        return False
    found = connection.execute(
        "SELECT 1 FROM vault_members WHERE vault_id = ? AND account_id = ?", (vault.id, account_id)
    ).fetchone()
    return found is not None


def accept_invitations(connection: sqlite3.Connection, account_id: str, emails: tuple[str, ...]) -> int:
    if not emails:
        return 0
    marks = ", ".join("?" for _ in emails)
    rows = connection.execute(
        "SELECT vault_invitations.vault_id FROM vault_invitations"
        " JOIN vaults ON vaults.id = vault_invitations.vault_id"
        f" WHERE vault_invitations.email IN ({marks}) AND vaults.account_id != ?",
        (*emails, account_id),
    ).fetchall()
    moment = int(time.time())
    for row in rows:
        connection.execute(
            "INSERT INTO vault_members(vault_id, account_id, joined_at) VALUES(?, ?, ?)"
            " ON CONFLICT (vault_id, account_id) DO NOTHING",
            (row["vault_id"], account_id, moment),
        )
    connection.execute(f"DELETE FROM vault_invitations WHERE email IN ({marks})", emails)
    return len(rows)


def invited(connection: sqlite3.Connection, emails: tuple[str, ...]) -> bool:
    if not emails:
        return False
    marks = ", ".join("?" for _ in emails)
    return (
        connection.execute(f"SELECT 1 FROM vault_invitations WHERE email IN ({marks})", emails).fetchone() is not None
    )
