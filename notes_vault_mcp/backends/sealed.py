from __future__ import annotations

import base64
import binascii
import logging
import os
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from notes_vault_mcp.backends import Entry, NotFound, VaultBackend, VaultError, is_vault_key

MAGIC = b"nvm1"
NONCE_BYTES = 12
TAG_BYTES = 16
OVERHEAD = len(MAGIC) + NONCE_BYTES + TAG_BYTES
DATA_KEY_BYTES = 32
MAX_PATH_BYTES = 512
SEGMENT_CHARS = 200
SEALED_TYPE = "application/octet-stream"
NAME_CONTEXT = b"notes-vault-mcp name v1"
log = logging.getLogger(__name__)


class Unreadable(VaultError):
    pass


class Storage(VaultBackend, Protocol):
    def put_checked(
        self, key: str, data: bytes, expected_version: str | None = None, content_type: str = SEALED_TYPE
    ) -> str: ...


def _subkey(data_key: bytes, purpose: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=purpose).derive(data_key)


def new_data_key() -> bytes:
    return os.urandom(DATA_KEY_BYTES)


class SealedBackend:
    sealed = True

    def __init__(self, inner: Storage, data_key: bytes) -> None:
        if len(data_key) != DATA_KEY_BYTES:
            raise ValueError("a data key is 32 bytes")
        self.inner = inner
        self._contents = AESGCM(_subkey(data_key, b"notes-vault-mcp contents v1", 32))
        self._names = AESSIV(_subkey(data_key, b"notes-vault-mcp names v1", 64))

    def object_key(self, path: str) -> str:
        raw = path.encode("utf-8")
        if not raw:
            raise VaultError("refusing the vault root as a path")
        if len(raw) > MAX_PATH_BYTES:
            raise VaultError(f"a path is at most {MAX_PATH_BYTES} bytes in this vault")
        token = base64.urlsafe_b64encode(self._names.encrypt(raw, [NAME_CONTEXT])).decode("ascii").rstrip("=")
        return "/".join(token[start : start + SEGMENT_CHARS] for start in range(0, len(token), SEGMENT_CHARS))

    def path_of(self, object_key: str) -> str | None:
        token = object_key.replace("/", "")
        try:
            sealed = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            return self._names.decrypt(sealed, [NAME_CONTEXT]).decode("utf-8")
        except (binascii.Error, ValueError, InvalidTag, UnicodeDecodeError):
            return None

    def seal(self, path: str, data: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return MAGIC + nonce + self._contents.encrypt(nonce, data, MAGIC + path.encode("utf-8"))

    def unseal(self, path: str, blob: bytes) -> bytes:
        if not blob.startswith(MAGIC) or len(blob) < OVERHEAD:
            raise Unreadable(f"{path} is not sealed for this vault")
        nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_BYTES]
        try:
            return self._contents.decrypt(nonce, blob[len(MAGIC) + NONCE_BYTES :], MAGIC + path.encode("utf-8"))
        except InvalidTag as exc:
            raise Unreadable(f"{path} does not decrypt under this vault's key") from exc

    def list_all(self, prefix: str = "") -> list[Entry]:
        entries: list[Entry] = []
        foreign = 0
        for entry in self.inner.list_all(""):
            if entry.is_dir:
                continue
            path = self.path_of(entry.key)
            if path is None:
                foreign += 1
                continue
            if path.startswith(prefix):
                size = max(entry.size - OVERHEAD, 0)
                entries.append(Entry(key=path, version=entry.version, mtime=entry.mtime, size=size))
        if foreign:
            log.warning("notes-vault-mcp: %d objects under a sealed vault do not decrypt; skipping them", foreign)
        return sorted(entries, key=lambda entry: entry.key)

    def list(self) -> list[Entry]:
        return [entry for entry in self.list_all() if not entry.is_dir and is_vault_key(entry.key)]

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        try:
            blob, version = self.inner.get_bytes(self.object_key(key))
        except NotFound:
            raise NotFound(key) from None
        return self.unseal(key, blob), version

    def get(self, key: str) -> tuple[str, str]:
        data, version = self.get_bytes(key)
        return data.decode("utf-8"), version

    def put_checked(
        self, key: str, data: bytes, expected_version: str | None = None, content_type: str = SEALED_TYPE
    ) -> str:
        return self.inner.put_checked(self.object_key(key), self.seal(key, data), expected_version, SEALED_TYPE)

    def put(self, key: str, text: str, expected_version: str | None = None) -> str:
        return self.put_checked(key, text.encode("utf-8"), expected_version)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        return self.put_checked(key, data)

    def mkdir(self, key: str) -> None:
        folder = key.rstrip("/") + "/"
        self.inner.put_bytes(self.object_key(folder), self.seal(folder, b""), SEALED_TYPE)

    def delete(self, key: str) -> None:
        try:
            self.inner.delete(self.object_key(key))
        except NotFound:
            raise NotFound(key) from None

    def move(self, src: str, dst: str) -> None:
        if src.endswith("/"):
            raise VaultError(f"refusing to move a folder: {src}")
        data, _ = self.get_bytes(src)
        self.put_checked(dst, data)
        self.delete(src)
