from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from mindin.config import private_dir
from mindin.extensions import Extension, installed, supplied_database
from mindin.hosted.accounts.vaults import (
    MEMBER,
    OWNER,
    PERSONAL,
    SLUG_MAX,
    Vault,
    Waiting,
    accept_invitations,
    add_first_vault,
    clash,
    clean_name,
    free_slug,
    holders,
    insert_vault,
    new_vault_id,
    reaches,
    taken_slugs,
    valid_slug,
    vault_of,
)
from mindin.hosted.config import READ_SCOPE, WRITE_SCOPE
from mindin.hosted.database import Database, Row, SqliteDatabase
from mindin.hosted.orgs.rules import sharing_refusal
from mindin.hosted.schema import STEPS
from mindin.hosted.signin.registration import Registrant
from mindin.shared import Actor

AccountStatus = Literal["invited", "active", "suspended"]
STATUSES: tuple[AccountStatus, ...] = ("invited", "active", "suspended")
KEY_PREFIX = "nvm_"
KEY_ID_LENGTH = 12
SESSION_TTL = 12 * 3600
LOGIN_TTL = 600
LAST_USED_RESOLUTION = 60
UNUSED_CLIENT_TTL = 24 * 3600
WEBDAV_KEY_NAME = "Obsidian (WebDAV)"


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def drop_member(connection: Any, vault: Vault, account_id: str) -> bool:
    if account_id == vault.account_id:
        raise MembershipError("The owner cannot be removed; hand the vault over first.")
    moment = now()
    removed = connection.execute(
        "DELETE FROM vault_members WHERE vault_id = ? AND account_id = ?", (vault.id, account_id)
    ).rowcount
    connection.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE vault_id = ? AND account_id = ? AND revoked_at IS NULL",
        (moment, vault.id, account_id),
    )
    connection.execute(
        "UPDATE tokens SET revoked = 1 WHERE vault_id = ? AND account_id = ? AND revoked = 0", (vault.id, account_id)
    )
    connection.execute("DELETE FROM devices WHERE vault_id = ? AND account_id = ?", (vault.id, account_id))
    return removed > 0


def pass_vault(connection: Any, vault: Vault, account_id: str) -> Vault:
    if account_id == vault.account_id or not reaches(connection, vault, account_id):
        raise MembershipError("Only a member of this vault can become its owner.")
    if clash(connection, [account_id], vault.slug, vault.id) is not None:
        raise MembershipError(
            f"The new owner already reaches another vault at the address {vault.slug}; rename this vault first."
        )
    connection.execute("DELETE FROM vault_members WHERE vault_id = ? AND account_id = ?", (vault.id, account_id))
    connection.execute(
        "INSERT INTO vault_members(vault_id, account_id, joined_at) VALUES(?, ?, ?)"
        " ON CONFLICT (vault_id, account_id) DO NOTHING",
        (vault.id, vault.account_id, now()),
    )
    connection.execute("UPDATE vaults SET account_id = ? WHERE id = ?", (account_id, vault.id))
    connection.execute("DELETE FROM vault_support_grants WHERE vault_id = ?", (vault.id,))
    handed = vault_of(connection.execute("SELECT * FROM vaults WHERE id = ?", (vault.id,)).fetchone())
    assert handed is not None
    return handed


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

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class KeyGrant:
    account: Account
    vault: Vault
    key_id: str
    name: str
    scopes: list[str]
    purpose: str = "api"

    @property
    def role(self) -> str:
        return OWNER if self.vault.account_id == self.account.id else MEMBER


class VaultNameError(ValueError):
    pass


class MembershipError(ValueError):
    pass


def check_slug(slug: str) -> None:
    if not valid_slug(slug):
        raise VaultNameError(
            f"{slug or 'That'} cannot be an address: one word of lowercase letters a-z and digits, starting with a"
            f" letter, up to {SLUG_MAX}."
        )


def taken_by(slug: str, vault: Vault) -> str:
    return f"You already reach the vault {vault.name} at the address {slug}. Rename it, or choose another address."


@dataclass(frozen=True)
class Member:
    email: str
    role: str
    account_id: str | None
    since: int
    last_change_at: int | None = None

    @property
    def invited(self) -> bool:
        return self.account_id is None


@dataclass(frozen=True)
class Connection:
    client_id: str
    name: str
    scopes: str
    last_used_at: int
    live: bool


def client_label(client_id: str) -> str:
    return (urlsplit(client_id).hostname or client_id) if "://" in client_id else client_id


def account_of(row: Row | None) -> Account | None:
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
    )


class HostedStore:
    def __init__(
        self, directory: Path, extensions: Sequence[Extension] | None = None, database: Database | None = None
    ) -> None:
        chosen = installed() if extensions is None else extensions
        self.extensions = tuple(chosen)
        self.database = (
            database or supplied_database(chosen, directory) or SqliteDatabase(private_dir(directory) / "hosted.sqlite")
        )
        self.database.migrate(STEPS)
        with self.connect() as connection:
            for extension in chosen:
                extension.migrate(connection)

    def connect(self) -> AbstractContextManager[Any]:
        return self.database.connect()

    def account(self, ref: str) -> Account | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? OR email = ?", (ref, ref.lower())).fetchone()
        return account_of(row)

    def account_by_id(self, account_id: str) -> Account | None:
        with self.connect() as connection:
            return account_of(connection.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())

    def accounts(self) -> list[Account]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM accounts ORDER BY created_at, email").fetchall()
        return [account for account in map(account_of, rows) if account is not None]

    def invite(self, email: str, method: str = "operator", label: str = "") -> tuple[Account, bool]:
        address = email.strip().lower()
        if "@" not in address:
            raise ValueError(f"{email!r} is not an email address")
        existing = self.account(address)
        if existing is not None:
            return existing, False
        moment = now()
        account_id = "acc_" + secrets.token_urlsafe(16)
        registrant = Registrant(method, address, (address,), label or address, invited=True)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO accounts(id, email, status, status_changed_at, created_at) VALUES(?, ?, 'invited', ?, ?)",
                (account_id, address, moment, moment),
            )
            add_first_vault(connection, account_id, moment)
            made = account_of(connection.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())
            assert made is not None
            for extension in self.extensions:
                extension.registered(connection, made, registrant)
        created = self.account(address)
        assert created is not None
        return created, True

    def set_status(self, ref: str, status: AccountStatus, source: str = "operator") -> Account | None:
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

    def vaults(self, account_id: str) -> list[Vault]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM vaults WHERE account_id = ? ORDER BY created_at, rowid", (account_id,)
            ).fetchall()
        return [vault for vault in map(vault_of, rows) if vault is not None]

    def vault(self, vault_id: str) -> Vault | None:
        with self.connect() as connection:
            return vault_of(connection.execute("SELECT * FROM vaults WHERE id = ?", (vault_id,)).fetchone())

    def own_vault(self, account_id: str, ref: str) -> Vault | None:
        return next((vault for vault in self.vaults(account_id) if ref in (vault.slug, vault.id)), None)

    def vault_at(self, account_id: str, slug: str) -> Vault | None:
        return next((vault for vault in self.reachable_vaults(account_id) if vault.slug == slug), None)

    def reachable_vault(self, account_id: str, ref: str) -> Vault | None:
        return next((vault for vault in self.reachable_vaults(account_id) if ref in (vault.slug, vault.id)), None)

    def reaches(self, vault: Vault, account_id: str) -> bool:
        with self.connect() as connection:
            return reaches(connection, vault, account_id)

    def role(self, vault: Vault, account_id: str) -> str | None:
        if vault.account_id == account_id:
            return OWNER
        return MEMBER if self.reaches(vault, account_id) else None

    def shared_with(self, account_id: str) -> list[Vault]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT vaults.* FROM vault_members JOIN vaults ON vaults.id = vault_members.vault_id"
                " WHERE vault_members.account_id = ? AND vaults.account_id != ? ORDER BY vault_members.joined_at,"
                " vaults.rowid",
                (account_id, account_id),
            ).fetchall()
        return [vault for vault in map(vault_of, rows) if vault is not None]

    def reachable_vaults(self, account_id: str) -> list[Vault]:
        return self.vaults(account_id) + self.shared_with(account_id)

    def accept_invitations(self, account: Account) -> list[Waiting]:
        with self.connect() as connection:
            return accept_invitations(connection, account.id, (account.email.lower(),))

    def members(self, vault: Vault) -> list[Member]:
        with self.connect() as connection:
            changed = {
                row["author"]: row["at"]
                for row in connection.execute(
                    "SELECT author, MAX(at) AS at FROM vault_changes WHERE vault_id = ? GROUP BY author", (vault.id,)
                )
            }
            owner = account_of(
                connection.execute("SELECT * FROM accounts WHERE id = ?", (vault.account_id,)).fetchone()
            )
            rows = connection.execute(
                "SELECT accounts.id, accounts.email, vault_members.joined_at FROM vault_members"
                " JOIN accounts ON accounts.id = vault_members.account_id WHERE vault_members.vault_id = ?"
                " AND vault_members.account_id != ? ORDER BY vault_members.joined_at, accounts.email",
                (vault.id, vault.account_id),
            ).fetchall()
            pending = connection.execute(
                "SELECT email, created_at FROM vault_invitations WHERE vault_id = ? ORDER BY created_at, email",
                (vault.id,),
            ).fetchall()
        found = [Member(owner.email, OWNER, owner.id, vault.created_at, changed.get(owner.id))] if owner else []
        found += [Member(row["email"], MEMBER, row["id"], row["joined_at"], changed.get(row["id"])) for row in rows]
        found += [Member(row["email"], "invited", None, row["created_at"]) for row in pending]
        return found

    def others_in(self, vault: Vault) -> int:
        with self.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM vault_members WHERE vault_id = ? AND account_id != ?",
                    (vault.id, vault.account_id),
                ).fetchone()[0]
            )

    def shared_with_others(self, account_id: str) -> list[Vault]:
        return [vault for vault in self.vaults(account_id) if vault.shared and self.others_in(vault)]

    def invite_member(self, vault: Vault, email: str, invited_by: str, link_hash: str | None = None) -> str:
        address = " ".join(email.split()).lower()
        if not vault.shared:
            raise MembershipError("Only a shared vault takes members.")
        if "@" not in address or len(address) > 254:
            raise MembershipError("Enter an email address.")
        with self.connect() as connection:
            refusal = sharing_refusal(connection, vault, address)
            if refusal:
                raise MembershipError(refusal)
            known = account_of(connection.execute("SELECT * FROM accounts WHERE email = ?", (address,)).fetchone())
            if known is not None and reaches(connection, vault, known.id):
                raise MembershipError(f"{address} is already in this vault.")
            if connection.execute(
                "SELECT 1 FROM vault_invitations WHERE vault_id = ? AND email = ?", (vault.id, address)
            ).fetchone():
                raise MembershipError(f"{address} is already invited.")
            connection.execute(
                "INSERT INTO vault_invitations(vault_id, email, invited_by, link_hash, created_at)"
                " VALUES(?, ?, ?, ?, ?)",
                (vault.id, address, invited_by, link_hash, now()),
            )
        return address

    def withdraw(self, vault: Vault, email: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "DELETE FROM vault_invitations WHERE vault_id = ? AND email = ? RETURNING link_hash",
                (vault.id, email.strip().lower()),
            ).fetchone()
            if row is not None and row["link_hash"] and "ceremonies" in self.database.tables(connection):
                connection.execute("DELETE FROM ceremonies WHERE key_hash = ?", (row["link_hash"],))
        return row is not None

    def remove_member(self, vault: Vault, account_id: str) -> bool:
        with self.connect() as connection:
            return drop_member(connection, vault, account_id)

    def hand_over(self, vault: Vault, account_id: str) -> Vault:
        with self.connect() as connection:
            return pass_vault(connection, vault, account_id)

    def client_name(self, client_id: str) -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT client_name FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return row["client_name"] if row else client_label(client_id)

    def first_vault(self, account_id: str) -> Vault | None:
        found = self.vaults(account_id)
        return found[0] if found else None

    def create_vault(self, account_id: str, name: str, slug: str | None = None, kind: str = PERSONAL) -> Vault:
        cleaned = clean_name(name)
        if not cleaned:
            raise VaultNameError("Give the vault a name.")
        if slug is not None:
            check_slug(slug)
        vault_id = new_vault_id()
        with self.connect() as connection:
            address = slug if slug is not None else free_slug(taken_slugs(connection, [account_id]), cleaned)
            found = clash(connection, [account_id], address)
            if found is not None:
                raise VaultNameError(taken_by(address, found))
            insert_vault(connection, vault_id, account_id, cleaned, address, now(), kind)
        created = self.vault(vault_id)
        assert created is not None
        return created

    def rename_vault(self, vault_id: str, name: str | None = None, slug: str | None = None) -> Vault:
        vault = self.vault(vault_id)
        if vault is None:
            raise VaultNameError("There is no such vault.")
        cleaned = clean_name(name) if name is not None else vault.name
        if not cleaned:
            raise VaultNameError("Give the vault a name.")
        address = slug if slug is not None else vault.slug
        check_slug(address)
        with self.connect() as connection:
            if address != vault.slug:
                found = clash(connection, [vault.account_id], address, vault.id)
                if found is not None:
                    raise VaultNameError(taken_by(address, found))
                if clash(connection, holders(connection, vault.id), address, vault.id) is not None:
                    raise VaultNameError(
                        f"A member of {vault.name} already reaches another vault at the address {address}."
                        " Choose another address."
                    )
            connection.execute("UPDATE vaults SET name = ?, slug = ? WHERE id = ?", (cleaned, address, vault_id))
        renamed = self.vault(vault_id)
        assert renamed is not None
        return renamed

    def create_key(
        self, account_id: str, name: str, scopes: list[str], purpose: str = "api", vault_id: str | None = None
    ) -> str:
        vault = self.vault(vault_id) if vault_id else self.first_vault(account_id)
        if vault is None or not self.reaches(vault, account_id):
            raise ValueError("a key belongs to one of the account's vaults")
        key_id = secrets.token_hex(KEY_ID_LENGTH // 2)
        key = f"{KEY_PREFIX}{key_id}{secrets.token_urlsafe(32)}"
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO api_keys(id, account_id, vault_id, name, key_hash, scopes, purpose, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key_id,
                    account_id,
                    vault.id,
                    name.strip()[:64] or "unnamed",
                    digest(key),
                    " ".join(scopes),
                    purpose,
                    now(),
                ),
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
            vault = vault_of(connection.execute("SELECT * FROM vaults WHERE id = ?", (row["vault_id"],)).fetchone())
            if account is None or vault is None or not reaches(connection, vault, account.id):
                return None
        return KeyGrant(
            account=account,
            vault=vault,
            key_id=key_id,
            name=row["name"],
            scopes=row["scopes"].split(),
            purpose=row["purpose"],
        )

    def keys(self, account_id: str, vault_id: str | None = None) -> list[Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT id, vault_id, name, scopes, purpose, created_at, last_used_at FROM api_keys"
                " WHERE account_id = ? AND (? IS NULL OR vault_id = ?) AND revoked_at IS NULL"
                " ORDER BY created_at, rowid",
                (account_id, vault_id, vault_id),
            ).fetchall()

    def revoke_key(self, account_id: str, key_id: str, vault_id: str | None = None) -> bool:
        with self.connect() as connection:
            return (
                connection.execute(
                    "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND account_id = ? AND (? IS NULL OR vault_id = ?)"
                    " AND revoked_at IS NULL",
                    (now(), key_id, account_id, vault_id, vault_id),
                ).rowcount
                > 0
            )

    def revoke_purpose(self, vault_id: str, purpose: str, account_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE vault_id = ? AND purpose = ? AND account_id = ?"
                " AND revoked_at IS NULL",
                (now(), vault_id, purpose, account_id),
            )

    def connections(self, vault_id: str, account_id: str) -> list[Connection]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT tokens.client_id, tokens.kind, tokens.scopes, tokens.issued_at, tokens.expires_at,"
                " clients.client_name FROM tokens LEFT JOIN clients ON clients.client_id = tokens.client_id"
                " WHERE tokens.vault_id = ? AND tokens.account_id = ? AND tokens.revoked = 0"
                " ORDER BY tokens.issued_at",
                (vault_id, account_id),
            ).fetchall()
        moment = now()
        found: dict[str, Connection] = {}
        for row in rows:
            known = found.get(row["client_id"])
            live = row["kind"] == "refresh" and row["expires_at"] > moment
            found[row["client_id"]] = Connection(
                client_id=row["client_id"],
                name=row["client_name"] or client_label(row["client_id"]),
                scopes=row["scopes"] if live or known is None else known.scopes,
                last_used_at=row["issued_at"],
                live=live or (known is not None and known.live),
            )
        return sorted((item for item in found.values() if item.live), key=lambda item: item.last_used_at, reverse=True)

    def disconnect(self, vault_id: str, client_id: str, account_id: str) -> bool:
        with self.connect() as connection:
            return (
                connection.execute(
                    "UPDATE tokens SET revoked = 1 WHERE vault_id = ? AND client_id = ? AND account_id = ?"
                    " AND revoked = 0",
                    (vault_id, client_id, account_id),
                ).rowcount
                > 0
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
                "INSERT INTO clients(client_id, client_name, registered_at, document) VALUES(?, ?, ?, ?)"
                " ON CONFLICT (client_id) DO UPDATE SET client_name = excluded.client_name,"
                " registered_at = excluded.registered_at, last_token_at = NULL, document = excluded.document",
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
                "INSERT INTO tokens(token_hash, kind, grant_id, client_id, account_id, vault_id, scopes, resource,"
                " issued_at, expires_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest(token),
                    kind,
                    grant["grant_id"],
                    grant["client_id"],
                    grant["account_id"],
                    grant["vault_id"],
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

    def load_token(self, token: str, kind: str) -> Row | None:
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
                "INSERT INTO devices(device_hash, user_code, client_id, client_name, name, scopes, requested_vault,"
                " interval, created_at, expires_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest(device_code),
                    user_code,
                    record["client_id"],
                    record["client_name"],
                    record["name"],
                    " ".join(record["scopes"]),
                    record.get("vault"),
                    record["interval"],
                    record["created_at"],
                    record["expires_at"],
                ),
            )

    def device(self, device_code: str) -> Row | None:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM devices WHERE device_hash = ?", (digest(device_code),)).fetchone()

    def device_by_user_code(self, user_code: str) -> Row | None:
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

    def issue(self, subject: str, label: str = "", account_id: str | None = None) -> str:
        vault = self.store.vault(subject)
        account = self.store.account_by_id(account_id or (vault.account_id if vault else ""))
        if vault is None or account is None or not account.active or not self.store.reaches(vault, account.id):
            raise PermissionError("no active account for this vault")
        self.store.revoke_purpose(vault.id, "webdav", account.id)
        return self.store.create_key(
            account.id, WEBDAV_KEY_NAME, [READ_SCOPE, WRITE_SCOPE], purpose="webdav", vault_id=vault.id
        )

    def lookup(self, secret: str) -> tuple[str, bool] | None:
        grant = self.store.key_grant(secret)
        if grant is None or not grant.account.active or not grant.account.sub:
            return None
        scopes = self.allowed(grant.account, grant.scopes)
        return (grant.vault.id, WRITE_SCOPE in scopes) if scopes else None

    def actor(self, secret: str) -> Actor | None:
        grant = self.store.key_grant(secret)
        return Actor(grant.account.email, grant.account.id, grant.name) if grant else None
