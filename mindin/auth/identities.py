from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from mindin.auth import Registration
from mindin.auth.hosted_orgs import domain_address, join_by_domain, on_a_domain
from mindin.auth.hosted_store import Account, HostedStore, account_of, digest, now
from mindin.auth.hosted_vaults import accept_invitations, add_first_vault, invited
from mindin.auth.registration import Registrant
from mindin.extensions import refusal

Method = Literal["passkey", "email", "github", "oidc", "forwarded"]
Refusal = Literal["", "unverified", "uninvited", "refused"]
Removal = Literal["removed", "last", "missing"]
METHOD_NAMES: dict[str, str] = {
    "passkey": "Passkey",
    "email": "Email link",
    "github": "GitHub",
    "oidc": "Single sign-on",
    "forwarded": "Company sign-in",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
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
);
CREATE INDEX IF NOT EXISTS identities_account ON identities(account_id);
CREATE INDEX IF NOT EXISTS identities_email ON identities(email);
CREATE TABLE IF NOT EXISTS ceremonies (
    key_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    binding_hash TEXT NOT NULL,
    document TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
"""
BACKFILL = (
    "INSERT INTO identities(method, subject, account_id, email, label, created_at)"
    " SELECT 'oidc', sub, id, email, email, created_at FROM accounts WHERE sub IS NOT NULL"
    " ON CONFLICT (method, subject) DO NOTHING"
)


@dataclass(frozen=True)
class Proof:
    method: Method
    subject: str
    emails: tuple[str, ...] = ()
    label: str = ""
    name: str = ""


@dataclass(frozen=True)
class Outcome:
    account: Account | None
    refusal: Refusal = ""
    reason: str = ""


@dataclass(frozen=True)
class WayIn:
    method: str
    subject: str
    label: str
    created_at: int
    last_used_at: int | None

    @property
    def method_name(self) -> str:
        return METHOD_NAMES.get(self.method, self.method)


@dataclass(frozen=True)
class Passkey:
    account_id: str
    public_key: bytes
    sign_count: int


def addresses(emails: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = (email.strip().lower() for email in emails)
    return tuple(dict.fromkeys(email for email in cleaned if "@" in email))


class Identities:
    def __init__(self, store: HostedStore) -> None:
        self.store = store
        with store.connect() as connection:
            fresh = "identities" not in store.database.tables(connection)
            store.database.create(connection, SCHEMA)
            if fresh:
                connection.execute(BACKFILL)
                connection.execute("DROP TABLE IF EXISTS logins")

    def sign_in(self, proof: Proof, registration: Registration) -> Outcome:
        emails = addresses(proof.emails)
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT account_id FROM identities WHERE method = ? AND subject = ?", (proof.method, proof.subject)
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE identities SET last_used_at = ? WHERE method = ? AND subject = ?",
                    (now(), proof.method, proof.subject),
                )
                returning = self._signed_in(connection, row["account_id"], proof.name)
                if returning is not None and returning.active:
                    join_by_domain(connection, returning.id, (returning.email.lower(), *emails))
                    accept_invitations(connection, returning.id, (returning.email.lower(),))
                return Outcome(returning)
            if not emails:
                return Outcome(None, "unverified")
            account = self._by_email(connection, emails)
            if account is None:
                welcomed = invited(connection, emails) or on_a_domain(connection, emails)
                registrant = Registrant(proof.method, proof.subject, emails, proof.label, welcomed)
                if registration != "open" and not registrant.invited:
                    return Outcome(None, "uninvited")
                refused = refusal(self.store.extensions, registrant)
                if refused:
                    return Outcome(None, "refused", refused)
                account = self._create(connection, domain_address(connection, emails) or emails[0])
                for extension in self.store.extensions:
                    extension.registered(connection, account, registrant)
            if account.status == "suspended":
                return Outcome(account)
            self._attach(connection, account.id, proof, emails)
            if account.status == "invited":
                connection.execute(
                    "UPDATE accounts SET status = 'active', status_source = 'invite', status_changed_at = ?"
                    " WHERE id = ?",
                    (now(), account.id),
                )
            join_by_domain(connection, account.id, tuple(dict.fromkeys((account.email.lower(), *emails))))
            accept_invitations(connection, account.id, tuple(dict.fromkeys((*emails, account.email.lower()))))
            return Outcome(self._signed_in(connection, account.id, proof.name))

    def registering(self, email: str) -> str:
        """Why an email link for this address would not make an account, or "" when it would or one exists."""
        emails = addresses((email,))
        with self.store.connect() as connection:
            if self._by_email(connection, emails) is not None:
                return ""
            welcomed = invited(connection, emails) or on_a_domain(connection, emails)
            registrant = Registrant("email", emails[0], emails, emails[0], welcomed)
            return refusal(self.store.extensions, registrant)

    def account_for(self, method: str, subject: str) -> Account | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT accounts.* FROM identities JOIN accounts ON accounts.id = identities.account_id"
                " WHERE identities.method = ? AND identities.subject = ?",
                (method, subject),
            ).fetchone()
        return account_of(row)

    def has_email(self, email: str) -> bool:
        with self.store.connect() as connection:
            return self._by_email(connection, addresses((email,))) is not None

    def invited(self, email: str) -> bool:
        with self.store.connect() as connection:
            emails = addresses((email,))
            return invited(connection, emails) or on_a_domain(connection, emails)

    def join_by_domain(self, account: Account) -> None:
        with self.store.connect() as connection:
            join_by_domain(connection, account.id, (account.email.lower(),))

    def link(self, account_id: str, proof: Proof) -> bool:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT account_id FROM identities WHERE method = ? AND subject = ?", (proof.method, proof.subject)
            ).fetchone()
            if row is not None:
                return row["account_id"] == account_id
            self._attach(connection, account_id, proof, addresses(proof.emails))
        return True

    def ways_in(self, account_id: str) -> list[WayIn]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT method, subject, label, created_at, last_used_at FROM identities WHERE account_id = ?"
                " ORDER BY created_at, rowid",
                (account_id,),
            ).fetchall()
        return [
            WayIn(row["method"], row["subject"], row["label"], row["created_at"], row["last_used_at"]) for row in rows
        ]

    def remove(self, account_id: str, method: str, subject: str) -> Removal:
        with self.store.connect() as connection:
            # The no-op update row-locks the account, so a concurrent removal waits and then counts what this one left.
            connection.execute("UPDATE accounts SET status = status WHERE id = ?", (account_id,))
            deleted = connection.execute(
                "DELETE FROM identities WHERE account_id = ? AND method = ? AND subject = ?"
                " AND (SELECT COUNT(*) FROM identities WHERE account_id = ?) > 1",
                (account_id, method, subject, account_id),
            ).rowcount
            if deleted:
                return "removed"
            exists = connection.execute(
                "SELECT 1 FROM identities WHERE account_id = ? AND method = ? AND subject = ?",
                (account_id, method, subject),
            ).fetchone()
        return "last" if exists else "missing"

    def add_passkey(self, account_id: str, credential_id: str, public_key: bytes, sign_count: int, label: str) -> bool:
        try:
            with self.store.connect() as connection:
                connection.execute(
                    "INSERT INTO identities(method, subject, account_id, label, public_key, sign_count, created_at)"
                    " VALUES('passkey', ?, ?, ?, ?, ?, ?)",
                    (credential_id, account_id, label, public_key, sign_count, now()),
                )
        except self.store.database.IntegrityError:
            return False
        return True

    def passkey(self, credential_id: str) -> Passkey | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT account_id, public_key, sign_count FROM identities WHERE method = 'passkey' AND subject = ?",
                (credential_id,),
            ).fetchone()
        return Passkey(row["account_id"], bytes(row["public_key"]), row["sign_count"]) if row else None

    def passkey_used(self, credential_id: str, sign_count: int) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE identities SET sign_count = ? WHERE method = 'passkey' AND subject = ?",
                (sign_count, credential_id),
            )

    def passkey_ids(self, account_id: str) -> list[str]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT subject FROM identities WHERE method = 'passkey' AND account_id = ?", (account_id,)
            ).fetchall()
        return [row["subject"] for row in rows]

    def begin(self, kind: str, key: str, binding: str, document: dict[str, Any], ttl: int) -> None:
        moment = now()
        with self.store.connect() as connection:
            connection.execute("DELETE FROM ceremonies WHERE expires_at < ?", (moment,))
            connection.execute(
                "INSERT INTO ceremonies(key_hash, kind, binding_hash, document, expires_at) VALUES(?, ?, ?, ?, ?)",
                (digest(key), kind, digest(binding) if binding else "", json.dumps(document), moment + ttl),
            )

    def take(self, kind: str, key: str) -> tuple[dict[str, Any], str] | None:
        if not key:
            return None
        with self.store.connect() as connection:
            row = connection.execute(
                "DELETE FROM ceremonies WHERE key_hash = ? AND kind = ? RETURNING document, binding_hash, expires_at",
                (digest(key), kind),
            ).fetchone()
        if row is None or row["expires_at"] < now():
            return None
        return json.loads(row["document"]), row["binding_hash"]

    def peek(self, kind: str, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT document FROM ceremonies WHERE key_hash = ? AND kind = ? AND expires_at >= ?",
                (digest(key), kind, now()),
            ).fetchone()
        return json.loads(row["document"]) if row else None

    def _by_email(self, connection: sqlite3.Connection, emails: tuple[str, ...]) -> Account | None:
        marks = ", ".join("?" for _ in emails)
        row = connection.execute(f"SELECT * FROM accounts WHERE email IN ({marks})", emails).fetchone()
        if row is None:
            row = connection.execute(
                f"SELECT accounts.* FROM identities JOIN accounts ON accounts.id = identities.account_id"
                f" WHERE identities.email IN ({marks}) LIMIT 1",
                emails,
            ).fetchone()
        return account_of(row)

    def _create(self, connection: sqlite3.Connection, email: str) -> Account:
        moment = now()
        account_id = "acc_" + secrets.token_urlsafe(16)
        connection.execute(
            "INSERT INTO accounts(id, email, status, status_source, status_changed_at, created_at)"
            " VALUES(?, ?, 'active', 'registration', ?, ?)",
            (account_id, email, moment, moment),
        )
        add_first_vault(connection, account_id, moment)
        created = account_of(connection.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())
        assert created is not None
        return created

    def _attach(self, connection: sqlite3.Connection, account_id: str, proof: Proof, emails: tuple[str, ...]) -> None:
        connection.execute(
            "INSERT INTO identities(method, subject, account_id, email, label, created_at, last_used_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?)",
            (proof.method, proof.subject, account_id, emails[0] if emails else "", proof.label, now(), now()),
        )

    def _signed_in(self, connection: sqlite3.Connection, account_id: str, name: str) -> Account | None:
        connection.execute(
            "UPDATE accounts SET sub = COALESCE(sub, ?), last_login_at = ?,"
            " name = CASE WHEN ? = '' THEN name ELSE ? END"
            " WHERE id = ?",
            (secrets.token_hex(16), now(), name, name, account_id),
        )
        return account_of(connection.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())
