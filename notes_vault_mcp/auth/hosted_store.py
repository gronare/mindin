from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from notes_vault_mcp.auth import READ_SCOPE, WRITE_SCOPE, auth_dir
from notes_vault_mcp.auth.pages import stamp
from notes_vault_mcp.config import private_dir

AccountStatus = Literal["invited", "active", "suspended"]
STATUSES: tuple[AccountStatus, ...] = ("invited", "active", "suspended")
KEY_PREFIX = "nvm_"
ADMIN_TOKEN_PREFIX = "nva_"
KEY_ID_LENGTH = 12
SESSION_TTL = 12 * 3600
LOGIN_TTL = 600
LAST_USED_RESOLUTION = 60
UNUSED_CLIENT_TTL = 24 * 3600
WEBDAV_KEY_NAME = "Obsidian (WebDAV)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    sub TEXT UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('invited', 'active', 'suspended')),
    status_source TEXT NOT NULL DEFAULT 'admin',
    status_changed_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    last_login_at INTEGER,
    plan_status TEXT NOT NULL DEFAULT 'active' CHECK (plan_status IN ('active', 'inactive')),
    plan_until INTEGER,
    plan_set_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admin_tokens (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    scopes TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'api',
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS api_keys_account ON api_keys(account_id);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    csrf TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    registered_at INTEGER NOT NULL,
    last_token_at INTEGER,
    document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    code_hash TEXT PRIMARY KEY,
    expires_at REAL NOT NULL,
    document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
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
);
CREATE TABLE IF NOT EXISTS devices (
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
);
"""


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def now() -> int:
    return int(time.time())


def scope_label(scopes: str) -> str:
    return "read and write" if WRITE_SCOPE in scopes.split() else "read"


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    sub: str | None
    name: str
    status: AccountStatus
    status_source: str
    created_at: int
    last_login_at: int | None
    status_changed_at: int = 0
    plan_status: str = "active"
    plan_until: int | None = None
    plan_set_at: int = 0

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class KeyGrant:
    account: Account
    key_id: str
    name: str
    scopes: list[str]


def account_of(row: sqlite3.Row | None) -> Account | None:
    if row is None:
        return None
    return Account(
        id=row["id"],
        email=row["email"],
        sub=row["sub"],
        name=row["name"],
        status=row["status"],
        status_source=row["status_source"],
        created_at=row["created_at"],
        last_login_at=row["last_login_at"],
        status_changed_at=row["status_changed_at"],
        plan_status=row["plan_status"],
        plan_until=row["plan_until"],
        plan_set_at=row["plan_set_at"],
    )


class HostedStore:
    def __init__(self, directory: Path) -> None:
        self.path = private_dir(directory) / "hosted.sqlite"
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def account(self, ref: str) -> Account | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? OR email = ?", (ref, ref.lower())).fetchone()
        return account_of(row)

    def account_by_id(self, account_id: str) -> Account | None:
        with self.connect() as connection:
            return account_of(connection.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())

    def accounts_page(self, after: str | None, limit: int) -> list[Account]:
        with self.connect() as connection:
            if after:
                rows = connection.execute(
                    "SELECT * FROM accounts WHERE rowid > (SELECT rowid FROM accounts WHERE id = ?)"
                    " ORDER BY rowid LIMIT ?",
                    (after, limit + 1),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM accounts ORDER BY rowid LIMIT ?", (limit + 1,)).fetchall()
        return [account for account in map(account_of, rows) if account is not None]

    def set_plan(self, account_id: str, status: str, until: int | None, at: int) -> Account | None:
        with self.connect() as connection:
            changed = connection.execute(
                "UPDATE accounts SET plan_status = ?, plan_until = ?, plan_set_at = ? WHERE id = ?",
                (status, until, at, account_id),
            ).rowcount
        return self.account_by_id(account_id) if changed else None

    def last_seen(self, account_id: str) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(moment) AS seen FROM ("
                " SELECT last_login_at AS moment FROM accounts WHERE id = ?"
                " UNION ALL SELECT MAX(last_used_at) FROM api_keys WHERE account_id = ?"
                " UNION ALL SELECT MAX(issued_at) FROM tokens WHERE account_id = ?)",
                (account_id, account_id, account_id),
            ).fetchone()
        return row["seen"]

    def create_admin_token(self, name: str, scope: str) -> str:
        token_id = secrets.token_hex(KEY_ID_LENGTH // 2)
        token = f"{ADMIN_TOKEN_PREFIX}{token_id}{secrets.token_urlsafe(32)}"
        with self.connect() as connection:
            connection.execute("DELETE FROM admin_tokens WHERE name = ?", (name,))
            connection.execute(
                "INSERT INTO admin_tokens(id, name, scope, token_hash, created_at) VALUES(?, ?, ?, ?, ?)",
                (token_id, name, scope, digest(token), now()),
            )
        return token

    def admin_token_scope(self, token: str) -> str | None:
        if not token.startswith(ADMIN_TOKEN_PREFIX) or len(token) < len(ADMIN_TOKEN_PREFIX) + KEY_ID_LENGTH + 16:
            return None
        token_id = token[len(ADMIN_TOKEN_PREFIX) : len(ADMIN_TOKEN_PREFIX) + KEY_ID_LENGTH]
        with self.connect() as connection:
            row = connection.execute("SELECT scope, token_hash FROM admin_tokens WHERE id = ?", (token_id,)).fetchone()
        return row["scope"] if row and hmac.compare_digest(row["token_hash"], digest(token)) else None

    def revoke_admin_token(self, name: str) -> bool:
        with self.connect() as connection:
            return connection.execute("DELETE FROM admin_tokens WHERE name = ?", (name,)).rowcount > 0

    def accounts(self) -> list[Account]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM accounts ORDER BY created_at, email").fetchall()
        return [account for account in map(account_of, rows) if account is not None]

    def invite(self, email: str) -> tuple[Account, bool]:
        address = email.strip().lower()
        if "@" not in address:
            raise ValueError(f"{email!r} is not an email address")
        existing = self.account(address)
        if existing is not None:
            return existing, False
        moment = now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO accounts(id, email, status, status_changed_at, created_at) VALUES(?, ?, 'invited', ?, ?)",
                ("acc_" + secrets.token_urlsafe(16), address, moment, moment),
            )
        created = self.account(address)
        assert created is not None
        return created, True

    def set_status(self, ref: str, status: AccountStatus, source: str = "admin") -> Account | None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        account = self.account(ref)
        if account is None:
            return None
        with self.connect() as connection:
            connection.execute(
                "UPDATE accounts SET status = ?, status_source = ?, status_changed_at = ? WHERE id = ?",
                (status, source, now(), account.id),
            )
        return self.account(account.id)

    def create_key(self, account_id: str, name: str, scopes: list[str], purpose: str = "api") -> str:
        key_id = secrets.token_hex(KEY_ID_LENGTH // 2)
        key = f"{KEY_PREFIX}{key_id}{secrets.token_urlsafe(32)}"
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO api_keys(id, account_id, name, key_hash, scopes, purpose, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (key_id, account_id, name.strip()[:64] or "unnamed", digest(key), " ".join(scopes), purpose, now()),
            )
        return key

    def key_grant(self, key: str) -> KeyGrant | None:
        if not key.startswith(KEY_PREFIX) or len(key) < len(KEY_PREFIX) + KEY_ID_LENGTH + 16:
            return None
        key_id = key[len(KEY_PREFIX) : len(KEY_PREFIX) + KEY_ID_LENGTH]
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM api_keys WHERE id = ? AND revoked_at IS NULL", (key_id,)).fetchone()
            if row is None or not hmac.compare_digest(row["key_hash"], digest(key)):
                return None
            moment = now()
            if row["last_used_at"] is None or moment - row["last_used_at"] >= LAST_USED_RESOLUTION:
                connection.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (moment, key_id))
            account = account_of(
                connection.execute("SELECT * FROM accounts WHERE id = ?", (row["account_id"],)).fetchone()
            )
        if account is None:
            return None
        return KeyGrant(account=account, key_id=key_id, name=row["name"], scopes=row["scopes"].split())

    def keys(self, account_id: str) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT id, name, scopes, purpose, created_at, last_used_at FROM api_keys"
                " WHERE account_id = ? AND revoked_at IS NULL ORDER BY created_at, rowid",
                (account_id,),
            ).fetchall()

    def revoke_key(self, account_id: str, key_id: str) -> bool:
        with self.connect() as connection:
            return (
                connection.execute(
                    "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND account_id = ? AND revoked_at IS NULL",
                    (now(), key_id, account_id),
                ).rowcount
                > 0
            )

    def revoke_purpose(self, account_id: str, purpose: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE account_id = ? AND purpose = ? AND revoked_at IS NULL",
                (now(), account_id, purpose),
            )

    def account_for_sub(self, sub: str) -> Account | None:
        with self.connect() as connection:
            return account_of(connection.execute("SELECT * FROM accounts WHERE sub = ?", (sub,)).fetchone())

    def create_session(self, account_id: str) -> str:
        session = secrets.token_urlsafe(32)
        moment = now()
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at < ?", (moment,))
            connection.execute(
                "INSERT INTO sessions(id_hash, account_id, csrf, created_at, expires_at) VALUES(?, ?, ?, ?, ?)",
                (digest(session), account_id, secrets.token_urlsafe(24), moment, moment + SESSION_TTL),
            )
        return session

    def session(self, session: str) -> tuple[Account, str] | None:
        if not session:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT account_id, csrf FROM sessions WHERE id_hash = ? AND expires_at >= ?",
                (digest(session), now()),
            ).fetchone()
            if row is None:
                return None
            account = account_of(
                connection.execute("SELECT * FROM accounts WHERE id = ?", (row["account_id"],)).fetchone()
            )
        return (account, row["csrf"]) if account else None

    def end_session(self, session: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE id_hash = ?", (digest(session),))

    def save_client(self, client_id: str, client_name: str, document: str) -> None:
        moment = now()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM clients WHERE last_token_at IS NULL AND registered_at < ?", (moment - UNUSED_CLIENT_TTL,)
            )
            connection.execute(
                "INSERT OR REPLACE INTO clients(client_id, client_name, registered_at, document) VALUES(?, ?, ?, ?)",
                (client_id, client_name, moment, document),
            )

    def client_document(self, client_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT document FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return row["document"] if row else None

    def save_request(self, request_id: str, payload: dict[str, Any], ttl: int) -> None:
        moment = now()
        with self.connect() as connection:
            connection.execute("DELETE FROM requests WHERE created_at < ?", (moment - ttl,))
            connection.execute(
                "INSERT INTO requests(id, created_at, document) VALUES(?, ?, ?)",
                (request_id, moment, json.dumps(payload)),
            )

    def load_request(self, request_id: str, ttl: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT document FROM requests WHERE id = ? AND created_at >= ?", (request_id, now() - ttl)
            ).fetchone()
        return json.loads(row["document"]) if row else None

    def drop_request(self, request_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM requests WHERE id = ?", (request_id,))

    def save_code(self, code: str, expires_at: float, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM codes WHERE expires_at < ?", (time.time(),))
            connection.execute(
                "INSERT INTO codes(code_hash, expires_at, document) VALUES(?, ?, ?)",
                (digest(code), expires_at, json.dumps(payload)),
            )

    def load_code(self, code: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT document FROM codes WHERE code_hash = ?", (digest(code),)).fetchone()
        return json.loads(row["document"]) if row else None

    def take_code(self, code: str) -> bool:
        with self.connect() as connection:
            return connection.execute("DELETE FROM codes WHERE code_hash = ?", (digest(code),)).rowcount > 0

    def save_token(self, token: str, kind: str, grant: dict[str, Any], ttl: int) -> int:
        issued_at = now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO tokens(token_hash, kind, grant_id, client_id, account_id, scopes, resource, issued_at,"
                " expires_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest(token),
                    kind,
                    grant["grant_id"],
                    grant["client_id"],
                    grant["account_id"],
                    " ".join(grant["scopes"]),
                    grant["resource"],
                    issued_at,
                    issued_at + ttl,
                ),
            )
            connection.execute(
                "UPDATE clients SET last_token_at = ? WHERE client_id = ?", (issued_at, grant["client_id"])
            )
        return issued_at + ttl

    def load_token(self, token: str, kind: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM tokens WHERE token_hash = ? AND kind = ? AND revoked = 0", (digest(token), kind)
            ).fetchone()

    def revoke_token(self, token: str) -> bool:
        with self.connect() as connection:
            return (
                connection.execute(
                    "UPDATE tokens SET revoked = 1 WHERE token_hash = ? AND revoked = 0", (digest(token),)
                ).rowcount
                > 0
            )

    def revoke_grant(self, token: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE tokens SET revoked = 1 WHERE grant_id IN (SELECT grant_id FROM tokens WHERE token_hash = ?)",
                (digest(token),),
            )

    def save_device(self, device_code: str, user_code: str, record: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM devices WHERE expires_at < ?", (record["created_at"] - LOGIN_TTL,))
            connection.execute(
                "INSERT INTO devices(device_hash, user_code, client_id, client_name, name, scopes, interval,"
                " created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest(device_code),
                    user_code,
                    record["client_id"],
                    record["client_name"],
                    record["name"],
                    " ".join(record["scopes"]),
                    record["interval"],
                    record["created_at"],
                    record["expires_at"],
                ),
            )

    def device(self, device_code: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM devices WHERE device_hash = ?", (digest(device_code),)).fetchone()

    def device_by_user_code(self, user_code: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM devices WHERE user_code = ? AND status = 'pending'", (user_code,)
            ).fetchone()

    def update_device(self, device_hash: str, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE devices SET {assignments} WHERE device_hash = ?", (*fields.values(), device_hash)
            )

    def drop_device(self, device_hash: str) -> bool:
        with self.connect() as connection:
            return connection.execute("DELETE FROM devices WHERE device_hash = ?", (device_hash,)).rowcount > 0


class ApiKeyCredentials:
    def __init__(self, store: HostedStore, allowed: Callable[[Account, list[str]], list[str]] | None = None) -> None:
        self.store = store
        self.allowed = allowed or (lambda _account, scopes: scopes)

    def issue(self, subject: str, label: str = "") -> str:
        account = self.store.account_for_sub(subject)
        if account is None or not account.active:
            raise PermissionError("no active account for this subject")
        self.store.revoke_purpose(account.id, "webdav")
        return self.store.create_key(account.id, WEBDAV_KEY_NAME, [READ_SCOPE, WRITE_SCOPE], purpose="webdav")

    def lookup(self, secret: str) -> tuple[str, bool] | None:
        grant = self.store.key_grant(secret)
        if grant is None or not grant.account.active or not grant.account.sub:
            return None
        scopes = self.allowed(grant.account, grant.scopes)
        return (grant.account.sub, WRITE_SCOPE in scopes) if scopes else None


def command_accounts(args: argparse.Namespace) -> int:
    store = HostedStore(auth_dir())
    action = args.accounts_command
    if action == "list":
        accounts = store.accounts()
        if not accounts:
            print("notes-vault-mcp: no accounts yet; `notes-vault-mcp accounts invite <email>` adds one")
        for account in accounts:
            keys = len(store.keys(account.id))
            signed_in = stamp(account.last_login_at) if account.last_login_at else "never signed in"
            print(f"{account.id}  {account.email}  {account.status}  {keys} keys  {signed_in}")
        return 0
    if action == "invite":
        try:
            account, created = store.invite(args.email)
        except ValueError as exc:
            print(f"notes-vault-mcp: {exc}", file=sys.stderr)
            return 2
        verb = "invited" if created else "already has an account,"
        print(f"notes-vault-mcp: {account.email} {verb} status {account.status}")
        return 0
    status: AccountStatus = "suspended" if action == "suspend" else "active"
    account = store.set_status(args.account, status)
    if account is None:
        print(f"notes-vault-mcp: no account {args.account!r}", file=sys.stderr)
        return 1
    print(f"notes-vault-mcp: {account.email} is {account.status}")
    return 0


def command_admin(args: argparse.Namespace) -> int:
    store = HostedStore(auth_dir())
    if args.admin_command == "revoke":
        if not store.revoke_admin_token(args.name):
            print(f"notes-vault-mcp: no admin token named {args.name!r}", file=sys.stderr)
            return 1
        print(f"notes-vault-mcp: revoked the admin token {args.name}")
        return 0
    print(store.create_admin_token(args.name, args.scope))
    return 0
