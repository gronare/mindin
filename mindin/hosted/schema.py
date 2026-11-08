from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from mindin.hosted.accounts.vaults import FIRST_VAULT_NAME, rewrite_slugs
from mindin.hosted.database import add_column, has_table

DEFAULT_REMOVAL_DAYS = 30

STORE_TABLES = (
    """CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    sub TEXT UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('invited', 'active', 'suspended')),
    status_source TEXT NOT NULL DEFAULT 'operator',
    status_changed_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    last_login_at INTEGER
)""",
    """CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    scopes TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'api',
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked_at INTEGER
)""",
    "CREATE INDEX IF NOT EXISTS api_keys_account ON api_keys(account_id)",
    """CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    csrf TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    registered_at INTEGER NOT NULL,
    last_token_at INTEGER,
    document TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    document TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS codes (
    code_hash TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    document TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    scopes TEXT NOT NULL,
    resource TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
)""",
    """CREATE TABLE IF NOT EXISTS devices (
    device_hash TEXT PRIMARY KEY,
    user_code TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL,
    client_name TEXT NOT NULL,
    name TEXT NOT NULL,
    scopes TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    account_id TEXT,
    granted TEXT,
    interval INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_poll_at REAL
)""",
)
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
VAULT_COLUMNS = (
    ("api_keys", "vault_id", "TEXT REFERENCES vaults(id)"),
    ("tokens", "vault_id", "TEXT REFERENCES vaults(id)"),
    ("devices", "vault_id", "TEXT REFERENCES vaults(id)"),
    ("devices", "requested_vault", "TEXT"),
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
SUPPORT_REQUEST_TABLES = (
    """CREATE TABLE IF NOT EXISTS vault_support_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    vault_id TEXT REFERENCES vaults(id),
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_at INTEGER NOT NULL,
    answer TEXT CHECK (answer IN ('approved', 'declined', 'withdrawn')),
    answered_at INTEGER
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS vault_support_requests_pending ON vault_support_requests(account_id)
    WHERE answer IS NULL""",
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
ACCOUNT_SLUG_VAULTS = """CREATE TABLE vaults_by_account (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'personal' CHECK (kind IN ('personal', 'shared'))
)"""
IDENTITY_TABLES = (
    """CREATE TABLE IF NOT EXISTS identities (
    method TEXT NOT NULL,
    subject TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    email TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    public_key BLOB,
    sign_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    PRIMARY KEY (method, subject)
)""",
    "CREATE INDEX IF NOT EXISTS identities_account ON identities(account_id)",
    "CREATE INDEX IF NOT EXISTS identities_email ON identities(email)",
    """CREATE TABLE IF NOT EXISTS ceremonies (
    key_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    document TEXT NOT NULL,
    expires_at INTEGER NOT NULL
)""",
)
RATE_TABLES = (
    """CREATE TABLE IF NOT EXISTS rate_limit_hits (
    scope TEXT NOT NULL,
    address TEXT NOT NULL,
    slot INTEGER NOT NULL,
    hits INTEGER NOT NULL,
    PRIMARY KEY (scope, address, slot)
)""",
)
LEASE_TABLES = (
    """CREATE TABLE IF NOT EXISTS periodic_leases (
    name TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    held_until INTEGER NOT NULL
)""",
)
ORGANISATION_TABLES = (
    f"""CREATE TABLE IF NOT EXISTS organisations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    removal TEXT NOT NULL DEFAULT 'keep' CHECK (removal IN ('keep', 'export-and-delete')),
    removal_days INTEGER NOT NULL DEFAULT {DEFAULT_REMOVAL_DAYS},
    external_sharing TEXT NOT NULL DEFAULT 'denied' CHECK (external_sharing IN ('allowed', 'denied')),
    created_at INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS organisation_domains (
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    domain TEXT NOT NULL COLLATE NOCASE,
    token TEXT NOT NULL,
    verified_at INTEGER,
    added_at INTEGER NOT NULL,
    checked_at INTEGER,
    checked_records INTEGER,
    PRIMARY KEY (organisation_id, domain)
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS organisation_domains_verified ON organisation_domains(domain)
    WHERE verified_at IS NOT NULL""",
    """CREATE TABLE IF NOT EXISTS organisation_members (
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    source TEXT NOT NULL,
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (organisation_id, account_id)
)""",
    "CREATE INDEX IF NOT EXISTS organisation_members_account ON organisation_members(account_id)",
    """CREATE TABLE IF NOT EXISTS organisation_invitations (
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    sent_at INTEGER NOT NULL,
    invited_by TEXT,
    mailed_at INTEGER,
    PRIMARY KEY (organisation_id, account_id)
)""",
    "CREATE INDEX IF NOT EXISTS organisation_invitations_account ON organisation_invitations(account_id)",
    """CREATE TABLE IF NOT EXISTS organisation_vaults (
    vault_id TEXT PRIMARY KEY REFERENCES vaults(id),
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    layout TEXT NOT NULL DEFAULT 'wiki',
    locked INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS organisation_editors (
    vault_id TEXT NOT NULL REFERENCES vaults(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    added_at INTEGER NOT NULL,
    PRIMARY KEY (vault_id, account_id)
)""",
    """CREATE TABLE IF NOT EXISTS organisation_departures (
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    email TEXT NOT NULL COLLATE NOCASE,
    former_account TEXT NOT NULL,
    removed_at INTEGER NOT NULL,
    delete_after INTEGER,
    source TEXT,
    told_at INTEGER,
    erased_at INTEGER,
    kept_at INTEGER,
    PRIMARY KEY (organisation_id, email)
)""",
)
IDENTITY_BACKFILL = (
    "INSERT INTO identities(method, subject, account_id, email, label, created_at)"
    " SELECT 'oidc', sub, id, email, email, created_at FROM accounts WHERE sub IS NOT NULL"
    " ON CONFLICT (method, subject) DO NOTHING"
)


class MigrationError(RuntimeError):
    pass


def run(connection: sqlite3.Connection, statements: Sequence[str]) -> None:
    for statement in statements:
        connection.execute(statement)


def count(connection: sqlite3.Connection, sql: str) -> int:
    return int(connection.execute(sql).fetchone()[0])


# An existing account's vault takes the account's id, so its storage prefix, index and key wrapping stay as they are.
def to_vaults(connection: sqlite3.Connection) -> None:
    run(connection, STORE_TABLES)
    run(connection, VAULT_TABLES)
    for table, column, declaration in VAULT_COLUMNS:
        add_column(connection, table, column, declaration)
    unvaulted = connection.execute(
        "SELECT id, created_at FROM accounts WHERE id NOT IN (SELECT account_id FROM vaults) ORDER BY created_at, id"
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
        run(connection, SUPPORT_TABLES[:1])
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
    run(connection, DATA_KEY_TABLES)
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
    run(connection, SHARED_TABLES)


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


def to_opened_tables(connection: sqlite3.Connection) -> None:
    run(connection, STORE_TABLES)
    if not has_table(connection, "identities"):
        run(connection, IDENTITY_TABLES)
        connection.execute(IDENTITY_BACKFILL)
        connection.execute("DROP TABLE IF EXISTS logins")
    run(connection, IDENTITY_TABLES)
    run(connection, DATA_KEY_TABLES)
    run(connection, SUPPORT_TABLES)
    run(connection, RATE_TABLES)
    run(connection, LEASE_TABLES)


def to_organisations(connection: sqlite3.Connection) -> None:
    run(connection, ORGANISATION_TABLES)


def to_support_requests(connection: sqlite3.Connection) -> None:
    run(connection, SUPPORT_REQUEST_TABLES)


STEPS = (to_vaults, to_shared, to_account_slugs, to_opened_tables, to_organisations, to_support_requests)
