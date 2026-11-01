from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from notes_vault_mcp.config import private_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_tokens (
    token_hash TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    issued_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS personal_tokens_subject ON personal_tokens(subject);
"""


class DavCredentials(Protocol):
    def issue(self, subject: str, label: str = "") -> str: ...

    def lookup(self, secret: str) -> tuple[str, bool] | None: ...


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PersonalTokens:
    # One live token per subject, for clients that cannot do OAuth (Obsidian's Remotely Save over WebDAV).
    # Only the hash is stored; issuing a new token revokes the previous one.
    def __init__(self, directory: Path) -> None:
        self.path = private_dir(directory) / "personal.sqlite"
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def issue(self, subject: str, label: str = "") -> str:
        token = secrets.token_urlsafe(32)
        with self.connect() as connection:
            connection.execute("UPDATE personal_tokens SET revoked = 1 WHERE subject = ? AND revoked = 0", (subject,))
            connection.execute(
                "INSERT INTO personal_tokens(token_hash, subject, label, issued_at) VALUES(?, ?, ?, ?)",
                (token_hash(token), subject, label, int(time.time())),
            )
        return token

    def subject_for(self, token: str) -> str | None:
        if not token:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT subject FROM personal_tokens WHERE token_hash = ? AND revoked = 0", (token_hash(token),)
            ).fetchone()
        return str(row["subject"]) if row else None

    def lookup(self, secret: str) -> tuple[str, bool] | None:
        subject = self.subject_for(secret)
        return (subject, True) if subject else None

    def revoke(self, subject: str) -> int:
        with self.connect() as connection:
            return connection.execute(
                "UPDATE personal_tokens SET revoked = 1 WHERE subject = ? AND revoked = 0", (subject,)
            ).rowcount
