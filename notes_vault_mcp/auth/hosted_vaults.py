from __future__ import annotations

import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

VAULT_ID_PREFIX = "vlt_"
SLUG_MAX = 40
SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
RESERVED_SLUGS = frozenset({"new"})
FIRST_VAULT_NAME = "Personal"
NAME_MAX = 64
SCHEMA_VERSION = 1

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


def vault_of(row: sqlite3.Row | None) -> Vault | None:
    if row is None:
        return None
    return Vault(
        id=row["id"], account_id=row["account_id"], name=row["name"], slug=row["slug"], created_at=row["created_at"]
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
    connection: sqlite3.Connection, vault_id: str, account_id: str, name: str, slug: str, created_at: int
) -> None:
    connection.execute(
        "INSERT INTO vaults(id, account_id, name, slug, created_at) VALUES(?, ?, ?, ?, ?)",
        (vault_id, account_id, name, slug, created_at),
    )


def new_vault_id() -> str:
    return VAULT_ID_PREFIX + secrets.token_urlsafe(16)


def add_first_vault(connection: sqlite3.Connection, account_id: str, email: str, created_at: int) -> None:
    slug = free_slug(connection, email.partition("@")[0])
    insert_vault(connection, new_vault_id(), account_id, FIRST_VAULT_NAME, slug, created_at)


def has_table(connection: sqlite3.Connection, table: str) -> bool:
    found = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return found is not None


def add_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    if not has_table(connection, table):
        return
    present = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
    if column not in present:
        connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {column} {declaration}')


def count(connection: sqlite3.Connection, sql: str) -> int:
    return int(connection.execute(sql).fetchone()[0])


def migrate(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if count(connection, "PRAGMA user_version") < SCHEMA_VERSION:
                to_vaults(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()


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
        insert_vault(connection, row["id"], row["id"], FIRST_VAULT_NAME, slug, row["created_at"])
    connection.execute("UPDATE api_keys SET vault_id = account_id WHERE vault_id IS NULL")
    connection.execute("UPDATE tokens SET vault_id = account_id WHERE vault_id IS NULL")
    if has_table(connection, "vault_keys"):
        move_data_keys(connection)
    if has_table(connection, "support_grants"):
        for statement in SUPPORT_TABLES[:1]:
            connection.execute(statement)
        connection.execute(
            "INSERT OR REPLACE INTO vault_support_grants(vault_id, account_id, granted_at, expires_at)"
            " SELECT account_id, account_id, granted_at, expires_at FROM support_grants"
        )
        connection.execute("DROP TABLE support_grants")
    if has_table(connection, "vault_access"):
        add_column(connection, "vault_access", "vault_id", "TEXT")
        connection.execute("UPDATE vault_access SET vault_id = account_id WHERE vault_id IS NULL")


def move_data_keys(connection: sqlite3.Connection) -> None:
    for statement in DATA_KEY_TABLES:
        connection.execute(statement)
    connection.execute(
        "INSERT OR IGNORE INTO vault_data_keys(vault_id, key_id, wrapped, created_at, rewrapped_at)"
        " SELECT account_id, key_id, wrapped, created_at, rewrapped_at FROM vault_keys"
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
