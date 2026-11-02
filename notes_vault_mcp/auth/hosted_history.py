from __future__ import annotations

import contextlib
import difflib
import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from notes_vault_mcp.auth.hosted_store import HostedStore
from notes_vault_mcp.backends import MUST_NOT_EXIST, NotFound, VersionConflict
from notes_vault_mcp.backends.sealed import SealedBackend
from notes_vault_mcp.shared import Actor, SharedBackend, acting_as

HISTORY_PREFIX = "history"
BLOB_FOLDER = "blobs/"
PAGE_ROWS = 50
RESTORABLE = ("updated", "deleted")


def history_key(data_key: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"notes-vault-mcp history v1").derive(data_key)


@dataclass(frozen=True)
class Change:
    id: int
    path: str
    from_path: str | None
    action: str
    author_email: str
    author: str | None
    client: str
    at: int
    before: str | None
    after: str | None
    restored_from: int | None

    @property
    def restorable(self) -> bool:
        return self.action in RESTORABLE and self.before is not None


class History:
    def __init__(
        self,
        store: HostedStore,
        vault_id: str,
        blobs: SealedBackend,
        hash_key: bytes,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.vault_id = vault_id
        self.blobs = blobs
        self.hash_key = hash_key
        self.clock = clock

    def blob_id(self, data: bytes) -> str:
        return hmac.new(self.hash_key, data, hashlib.sha256).hexdigest()

    def keep(self, data: bytes | None) -> str | None:
        if data is None:
            return None
        blob = self.blob_id(data)
        with contextlib.suppress(VersionConflict):
            self.blobs.put_checked(BLOB_FOLDER + blob, data, MUST_NOT_EXIST)
        return blob

    def content(self, blob: str | None) -> bytes | None:
        if blob is None:
            return None
        try:
            return self.blobs.get_bytes(BLOB_FOLDER + blob)[0]
        except NotFound:
            return None

    def record(
        self,
        actor: Actor,
        action: str,
        path: str,
        before: bytes | None,
        after: bytes | None,
        from_path: str | None = None,
    ) -> None:
        before_id, after_id = self.keep(before), self.keep(after)
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO vault_changes(vault_id, path, from_path, action, author_email, author, client, at,"
                " before, after) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.vault_id,
                    self.blobs.object_key(path),
                    self.blobs.object_key(from_path) if from_path else None,
                    action,
                    actor.email,
                    actor.account_id,
                    actor.client[:120],
                    int(self.clock()),
                    before_id,
                    after_id,
                ),
            )

    def change_of(self, row: object) -> Change:
        return Change(
            id=row["id"],
            path=self.blobs.path_of(row["path"]) or "(unreadable path)",
            from_path=self.blobs.path_of(row["from_path"]) if row["from_path"] else None,
            action=row["action"],
            author_email=row["author_email"],
            author=row["author"],
            client=row["client"],
            at=row["at"],
            before=row["before"],
            after=row["after"],
            restored_from=row["restored_from"],
        )

    def changes(self, limit: int = PAGE_ROWS, before_id: int | None = None) -> list[Change]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM vault_changes WHERE vault_id = ? AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
                (self.vault_id, before_id, before_id, limit),
            ).fetchall()
        return [self.change_of(row) for row in rows]

    def change(self, change_id: int) -> Change | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM vault_changes WHERE vault_id = ? AND id = ?", (self.vault_id, change_id)
            ).fetchone()
        return self.change_of(row) if row else None

    def last_change(self) -> Change | None:
        found = self.changes(limit=1)
        return found[0] if found else None

    def diff(self, change: Change) -> list[str]:
        before = text_of(self.content(change.before))
        after = text_of(self.content(change.after))
        old_name = change.from_path or change.path
        return list(
            difflib.unified_diff(
                before.splitlines(), after.splitlines(), f"a/{old_name}", f"b/{change.path}", lineterm="", n=3
            )
        )

    def restore(self, change: Change, actor: Actor, backend: SharedBackend) -> bool:
        data = self.content(change.before) if change.restorable else None
        if data is None:
            raise NotFound(f"no previous version of {change.path}")
        if backend.previous(change.path) == data:
            return False
        with acting_as(actor):
            backend.put_bytes(change.path, data)
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE vault_changes SET restored_from = ? WHERE id = (SELECT MAX(id) FROM vault_changes"
                " WHERE vault_id = ? AND path = ?)",
                (change.id, self.vault_id, self.blobs.object_key(change.path)),
            )
        return True


def text_of(data: bytes | None) -> str:
    if data is None:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return f"(a binary file of {len(data)} bytes)"
