from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from notes_vault_mcp.auth.hosted_access import SupportAccess
from notes_vault_mcp.auth.hosted_keys import Keyring, MasterKey, VaultKeys
from notes_vault_mcp.auth.hosted_store import SCHEMA, HostedStore
from notes_vault_mcp.auth.identities import Identities

FIXTURE = Path(__file__).parent / "fixtures" / "hosted_schema.json"
LEGACY_TABLES = """
CREATE TABLE vault_keys (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id),
    key_id TEXT NOT NULL,
    wrapped BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    rewrapped_at INTEGER
);
CREATE TABLE support_grants (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id),
    granted_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE vault_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    at INTEGER NOT NULL,
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    action TEXT NOT NULL
);
INSERT INTO accounts(id, email, sub, status, status_changed_at, created_at)
    VALUES('acc_orchardkeeper00000001', 'ann@example.com', 'sub-ann', 'active', 1, 1);
INSERT INTO vault_keys(account_id, key_id, wrapped, created_at) VALUES('acc_orchardkeeper00000001', 'k1', x'00', 1);
INSERT INTO support_grants(account_id, granted_at, expires_at) VALUES('acc_orchardkeeper00000001', 1, 4102444800);
"""


def opened(folder: Path) -> HostedStore:
    store = HostedStore(folder, extensions=())
    VaultKeys(store, Keyring(MasterKey.generate()))
    Identities(store)
    SupportAccess(store)
    return store


def dump(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    return {"user_version": version, "objects": [list(row) for row in rows]}


def schemas(folder: Path) -> dict[str, object]:
    fresh = opened(folder / "fresh")
    legacy = folder / "legacy"
    legacy.mkdir(parents=True)
    with sqlite3.connect(legacy / "hosted.sqlite") as connection:
        connection.executescript(SCHEMA + LEGACY_TABLES)
    migrated = opened(legacy)
    return {"fresh": dump(fresh.database.path), "migrated": dump(migrated.database.path)}


def test_the_store_lays_out_the_same_tables_as_the_released_schema(tmp_path: Path):
    assert schemas(tmp_path) == json.loads(FIXTURE.read_text())


def test_registering_a_client_again_replaces_it_and_forgets_its_last_token(tmp_path: Path):
    store = HostedStore(tmp_path, extensions=())
    store.save_client("https://app.example", "App", '{"v": 1}')
    with store.connect() as connection:
        connection.execute("UPDATE clients SET last_token_at = 5 WHERE client_id = 'https://app.example'")
    store.save_client("https://app.example", "App two", '{"v": 2}')
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM clients").fetchone()
    assert (row["client_name"], row["document"], row["last_token_at"]) == ("App two", '{"v": 2}', None)


def test_granting_support_again_moves_the_expiry(tmp_path: Path):
    store = HostedStore(tmp_path, extensions=())
    account, _ = store.invite("ann@example.com")
    vault = store.first_vault(account.id)
    clock = iter([100.0, 200.0, 250.0])
    access = SupportAccess(store, clock=lambda: next(clock))
    access.grant(vault, 60)
    access.grant(vault, 600)
    assert access.grant_until(vault.id) == 800
