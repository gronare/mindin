from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

SCHEMA_KEY = ".vault/schema.yml"
PREVIOUS_SCHEMA_KEY = ".vault/schema.previous.yml"
MUST_NOT_EXIST = "*"
NOTE_SUFFIXES = (".md", ".base")
FETCH_WORKERS = 16


class VaultError(Exception):
    pass


class NotFound(VaultError):
    pass


class VersionConflict(VaultError):
    pass


class LimitReached(VaultError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Entry:
    key: str
    version: str
    mtime: datetime
    size: int

    @property
    def is_dir(self) -> bool:
        return self.key.endswith("/")


@runtime_checkable
class VaultBackend(Protocol):
    def list(self) -> list[Entry]: ...

    def get(self, key: str) -> tuple[str, str]: ...

    def put(self, key: str, text: str, expected_version: str | None = None) -> str: ...

    def delete(self, key: str) -> None: ...

    def move(self, src: str, dst: str) -> None: ...

    # The raw side, for WebDAV: every object under a prefix (directories as keys ending in "/"), bytes in and out.
    def list_all(self, prefix: str = "") -> list[Entry]: ...

    def get_bytes(self, key: str) -> tuple[bytes, str]: ...

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str: ...

    def mkdir(self, key: str) -> None: ...


def delete_checked(backend: VaultBackend, key: str, expected_version: str) -> None:
    checked = getattr(backend, "delete_checked", None)
    if checked is not None:
        checked(key, expected_version)
        return
    try:
        current = backend.get_bytes(key)[1]
    except NotFound:
        current = None
    if current != expected_version:
        raise VersionConflict(f"{key} changed since it was read (expected {expected_version})")
    # A write landing between this check and the delete is lost: only a backend with delete_checked closes that window.
    backend.delete(key)


def is_vault_key(key: str) -> bool:
    if key == SCHEMA_KEY:
        return True
    if any(segment.startswith(".") for segment in key.split("/")):
        return False
    return key.endswith(NOTE_SUFFIXES)


def make_backend() -> tuple[VaultBackend, str]:
    from mindin.config import NO_BACKEND, ConfigError, local_root, s3_settings, vault_id

    root = local_root()
    if root is not None:
        from mindin.backends.local import LocalBackend

        return LocalBackend(root), vault_id(str(root))

    settings = s3_settings()
    if settings is not None:
        from mindin.backends.s3 import S3Backend

        seed = settings["endpoint"] + settings["bucket"] + settings["prefix"]
        return S3Backend(**settings), vault_id(seed)

    raise ConfigError(NO_BACKEND)


def make_subject_backend(subject: str, prefix: str = "") -> tuple[VaultBackend, str]:
    # One vault per subject under the configured storage: a sub-folder of the local root, or a deeper S3 prefix.
    from mindin.config import NO_BACKEND, ConfigError, local_root, s3_settings, vault_id

    root = local_root()
    if root is not None:
        from mindin.backends.local import LocalBackend

        path = root.joinpath(*(part for part in (prefix.strip("/"), subject) if part))
        return LocalBackend(path), vault_id(str(path))

    settings = s3_settings()
    if settings is not None:
        from mindin.backends.s3 import S3Backend

        combined = "/".join(part for part in (settings["prefix"].strip("/"), prefix.strip("/"), subject) if part)
        seed = settings["endpoint"] + settings["bucket"] + combined
        return S3Backend(**{**settings, "prefix": combined}), vault_id(seed)

    raise ConfigError(NO_BACKEND)
