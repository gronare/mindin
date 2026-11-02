from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from notes_vault_mcp.auth import DEFAULT_SUBJECT_PREFIX, auth_dir
from notes_vault_mcp.auth.hosted_history import HISTORY_PREFIX, History, history_key
from notes_vault_mcp.auth.hosted_store import Account, HostedStore
from notes_vault_mcp.auth.hosted_vaults import DATA_KEY_TABLES, Vault, has_table
from notes_vault_mcp.backends import Entry, VaultBackend, VaultError, is_vault_key, make_subject_backend
from notes_vault_mcp.backends.local import LocalBackend
from notes_vault_mcp.backends.sealed import DATA_KEY_BYTES, NONCE_BYTES, SealedBackend, Storage, new_data_key
from notes_vault_mcp.config import ConfigError, cache_dir, env
from notes_vault_mcp.shared import SharedBackend, SharedRules

MASTER_KEY_ENV = "VAULT_HOSTED_MASTER_KEY"
PREVIOUS_KEYS_ENV = "VAULT_HOSTED_MASTER_KEY_PREVIOUS"
NO_MASTER_KEY = (
    f"notes-vault-mcp: an HTTP server needs {MASTER_KEY_ENV}, the base64 master key that wraps every vault's "
    "data key (`notes-vault-mcp master-key generate` makes one). Keep it outside the bucket: losing it loses "
    "every vault."
)
UNKNOWN_KEY_ID = (
    "notes-vault-mcp: {count} vault keys are wrapped under master key {key_id}, which neither "
    f"{MASTER_KEY_ENV} nor {PREVIOUS_KEYS_ENV} holds; refusing to start with the wrong master key."
)


@dataclass(frozen=True)
class MasterKey:
    secret: bytes = field(repr=False)

    @property
    def key_id(self) -> str:
        return hmac.new(self.secret, b"notes-vault-mcp master key id", hashlib.sha256).hexdigest()[:16]

    @classmethod
    def parse(cls, raw: str, name: str = MASTER_KEY_ENV) -> MasterKey:
        try:
            secret = base64.b64decode(raw.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ConfigError(f"notes-vault-mcp: {name} is not base64") from exc
        if len(secret) != 32:
            raise ConfigError(f"notes-vault-mcp: {name} must decode to 32 bytes, not {len(secret)}")
        return cls(secret)

    @classmethod
    def generate(cls) -> MasterKey:
        return cls(os.urandom(32))

    def encoded(self) -> str:
        return base64.b64encode(self.secret).decode("ascii")


def _wrap_context(vault_id: str, key_id: str) -> bytes:
    return f"notes-vault-mcp wrap v1|{vault_id}|{key_id}".encode()


@dataclass(frozen=True)
class Keyring:
    current: MasterKey
    previous: tuple[MasterKey, ...] = ()

    @classmethod
    def from_env(cls) -> Keyring:
        raw = env(MASTER_KEY_ENV)
        if not raw:
            raise ConfigError(NO_MASTER_KEY)
        older = (env(PREVIOUS_KEYS_ENV) or "").replace(",", " ").split()
        return cls(MasterKey.parse(raw), tuple(MasterKey.parse(item, PREVIOUS_KEYS_ENV) for item in older))

    @property
    def key_ids(self) -> set[str]:
        return {key.key_id for key in (self.current, *self.previous)}

    def by_id(self, key_id: str) -> MasterKey:
        for key in (self.current, *self.previous):
            if key.key_id == key_id:
                return key
        raise VaultError(f"master key {key_id} is not configured")

    def wrap(self, vault_id: str, data_key: bytes) -> tuple[str, bytes]:
        key_id = self.current.key_id
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self.current.secret).encrypt(nonce, data_key, _wrap_context(vault_id, key_id))
        return key_id, nonce + sealed

    def unwrap(self, vault_id: str, key_id: str, wrapped: bytes) -> bytes:
        nonce, sealed = wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:]
        try:
            data_key = AESGCM(self.by_id(key_id).secret).decrypt(nonce, sealed, _wrap_context(vault_id, key_id))
        except InvalidTag as exc:
            raise VaultError(f"the data key of {vault_id} does not unwrap under master key {key_id}") from exc
        if len(data_key) != DATA_KEY_BYTES:
            raise VaultError(f"the data key of {vault_id} is malformed")
        return data_key


class VaultKeys:
    def __init__(self, store: HostedStore, keyring: Keyring) -> None:
        self.store = store
        self.keyring = keyring
        with store.connect() as connection:
            for statement in DATA_KEY_TABLES:
                connection.execute(statement)

    def data_key(self, vault_id: str) -> bytes:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT key_id, wrapped FROM vault_data_keys WHERE vault_id = ?", (vault_id,)
            ).fetchone()
            if row is None:
                key_id, wrapped = self.keyring.wrap(vault_id, new_data_key())
                connection.execute(
                    "INSERT OR IGNORE INTO vault_data_keys(vault_id, key_id, wrapped, created_at) VALUES(?, ?, ?, ?)",
                    (vault_id, key_id, wrapped, int(time.time())),
                )
                row = connection.execute(
                    "SELECT key_id, wrapped FROM vault_data_keys WHERE vault_id = ?", (vault_id,)
                ).fetchone()
        return self.keyring.unwrap(vault_id, row["key_id"], bytes(row["wrapped"]))

    def has_key(self, vault_id: str) -> bool:
        with self.store.connect() as connection:
            row = connection.execute("SELECT 1 FROM vault_data_keys WHERE vault_id = ?", (vault_id,)).fetchone()
        return row is not None

    def counts(self) -> dict[str, int]:
        with self.store.connect() as connection:
            rows = connection.execute("SELECT key_id, COUNT(*) AS n FROM vault_data_keys GROUP BY key_id").fetchall()
        return {row["key_id"]: row["n"] for row in rows}

    def verify(self) -> None:
        for key_id, count in self.counts().items():
            if key_id not in self.keyring.key_ids:
                raise ConfigError(UNKNOWN_KEY_ID.format(count=count, key_id=key_id))

    def rewrap(self) -> tuple[int, int]:
        current = self.keyring.current.key_id
        with self.store.connect() as connection:
            rows = connection.execute("SELECT vault_id, key_id, wrapped FROM vault_data_keys").fetchall()
        rewrapped = 0
        for row in rows:
            if row["key_id"] == current:
                continue
            data_key = self.keyring.unwrap(row["vault_id"], row["key_id"], bytes(row["wrapped"]))
            key_id, wrapped = self.keyring.wrap(row["vault_id"], data_key)
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE vault_data_keys SET key_id = ?, wrapped = ?, rewrapped_at = ?"
                    " WHERE vault_id = ? AND key_id = ?",
                    (key_id, wrapped, int(time.time()), row["vault_id"], row["key_id"]),
                )
            rewrapped += 1
        return rewrapped, len(rows) - rewrapped

    def shred(self, vault_id: str) -> bool:
        with self.store.connect() as connection:
            return connection.execute("DELETE FROM vault_data_keys WHERE vault_id = ?", (vault_id,)).rowcount > 0


@dataclass(frozen=True)
class VaultFacts:
    notes: int
    files: int
    size: int
    written_at: float | None


def facts_of(entries: list[Entry]) -> VaultFacts:
    files = [entry for entry in entries if not entry.is_dir]
    written = max((entry.mtime.timestamp() for entry in files), default=None)
    return VaultFacts(
        notes=sum(1 for entry in files if is_vault_key(entry.key) and entry.key.endswith(".md")),
        files=len(files),
        size=sum(entry.size for entry in files),
        written_at=written,
    )


class SealedVaults:
    def __init__(self, store: HostedStore, keys: VaultKeys, prefix: str = DEFAULT_SUBJECT_PREFIX) -> None:
        self.store = store
        self.keys = keys
        self.prefix = prefix.strip("/")

    def __call__(self, subject: str) -> tuple[VaultBackend, str]:
        from notes_vault_mcp.vault import NoSubject

        vault = self.store.vault(subject)
        if vault is None:
            raise NoSubject("no vault for this subject")
        backend, identifier = self.for_vault(vault.id)
        if not vault.shared:
            return backend, identifier
        return SharedBackend(backend, SharedRules(vault.name), self.history(vault.id)), identifier

    def storage(self, vault_id: str) -> tuple[Storage, str]:
        inner, identifier = make_subject_backend(vault_id, self.prefix)
        return cast(Storage, inner), identifier

    def history_storage(self, vault_id: str) -> Storage:
        inner, _ = make_subject_backend(vault_id, "/".join(part for part in (self.prefix, HISTORY_PREFIX) if part))
        return cast(Storage, inner)

    def history(self, vault_id: str) -> History:
        data_key = self.keys.data_key(vault_id)
        blobs = SealedBackend(self.history_storage(vault_id), data_key)
        return History(self.store, vault_id, blobs, history_key(data_key))

    def for_vault(self, vault_id: str) -> tuple[SealedBackend, str]:
        inner, identifier = self.storage(vault_id)
        return SealedBackend(inner, self.keys.data_key(vault_id)), identifier

    def facts(self, vault_id: str) -> VaultFacts:
        if not self.keys.has_key(vault_id):
            return VaultFacts(0, 0, 0, None)
        return facts_of(self.for_vault(vault_id)[0].list_all())


VAULT_ROWS = (
    "api_keys",
    "tokens",
    "devices",
    "vault_support_grants",
    "vault_members",
    "vault_invitations",
    "vault_changes",
)


def empty_storage(inner: VaultBackend) -> list[Entry]:
    entries = inner.list_all("")
    for entry in entries:
        if not entry.is_dir:
            inner.delete(entry.key)
    if isinstance(inner, LocalBackend):
        shutil.rmtree(inner.root, ignore_errors=True)
    return entries


def delete_vault(sealed: SealedVaults, vault: Vault, cache: Path) -> int:
    sealed.keys.shred(vault.id)
    inner, identifier = sealed.storage(vault.id)
    entries = empty_storage(inner)
    if vault.shared:
        empty_storage(sealed.history_storage(vault.id))
    for path in cache.glob(f"{identifier}.sqlite*"):
        path.unlink(missing_ok=True)
    with sealed.store.connect() as connection:
        for table in VAULT_ROWS:
            if has_table(connection, table):
                connection.execute(f'DELETE FROM "{table}" WHERE vault_id = ?', (vault.id,))
        connection.execute("DELETE FROM vaults WHERE id = ?", (vault.id,))
    return sum(1 for entry in entries if not entry.is_dir)


class StillShared(VaultError):
    pass


def handed_over_first(store: HostedStore, account: Account) -> None:
    kept = store.shared_with_others(account.id)
    if kept:
        names = ", ".join(vault.name for vault in kept)
        raise StillShared(
            f"{account.email} owns shared vaults with other members ({names}): hand each over or delete it"
        )


def delete_account(sealed: SealedVaults, account: Account, cache: Path) -> int:
    handed_over_first(sealed.store, account)
    deleted = sum(delete_vault(sealed, vault, cache) for vault in sealed.store.vaults(account.id))
    with sealed.store.connect() as connection:
        for table in account_tables(connection):
            connection.execute(f'DELETE FROM "{table}" WHERE account_id = ?', (account.id,))
        connection.execute("DELETE FROM accounts WHERE id = ?", (account.id,))
    return deleted


def account_tables(connection: sqlite3.Connection) -> list[str]:
    tables = [row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return [
        table
        for table in tables
        if any(column["name"] == "account_id" for column in connection.execute(f'PRAGMA table_info("{table}")'))
    ]


def sealed_vaults_from_env(store: HostedStore | None = None) -> SealedVaults:
    store = store or HostedStore(auth_dir())
    keys = VaultKeys(store, Keyring.from_env())
    keys.verify()
    return SealedVaults(store, keys, env("VAULT_SUBJECT_PREFIX") or DEFAULT_SUBJECT_PREFIX)


def command_master_key(args: argparse.Namespace) -> int:
    if args.master_key_command == "generate":
        print(MasterKey.generate().encoded())
        return 0
    store = HostedStore(auth_dir())
    keys = VaultKeys(store, Keyring.from_env())
    if args.master_key_command == "status":
        current = keys.keyring.current.key_id
        print(f"notes-vault-mcp: current master key {current}")
        for key_id, count in sorted(keys.counts().items()):
            state = "current" if key_id == current else "previous" if key_id in keys.keyring.key_ids else "MISSING"
            print(f"{key_id}  {count} vault keys  {state}")
        return 0
    try:
        rewrapped, kept = keys.rewrap()
    except VaultError as exc:
        print(f"notes-vault-mcp: {exc}", file=sys.stderr)
        return 1
    current = keys.keyring.current.key_id
    print(f"notes-vault-mcp: rewrapped {rewrapped} vault keys under {current}, {kept} already were")
    return 0


def command_delete_account(args: argparse.Namespace) -> int:
    store = HostedStore(auth_dir())
    account = store.account(args.account)
    if account is None:
        print(f"notes-vault-mcp: no account {args.account!r}", file=sys.stderr)
        return 1
    if not args.yes:
        print(
            f"notes-vault-mcp: deleting {account.email} destroys every vault it owns for good; repeat with --yes",
            file=sys.stderr,
        )
        return 2
    try:
        deleted = delete_account(sealed_vaults_from_env(store), account, cache_dir())
    except StillShared as exc:
        print(f"notes-vault-mcp: {exc}", file=sys.stderr)
        return 1
    print(f"notes-vault-mcp: deleted {account.email}: its vaults' data keys, {deleted} objects and their indexes")
    return 0
