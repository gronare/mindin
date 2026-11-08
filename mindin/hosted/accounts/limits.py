from __future__ import annotations

import logging
import mimetypes
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mindin.backends import Entry, LimitReached

if TYPE_CHECKING:
    from mindin.extensions import Extension
    from mindin.hosted.accounts.store import Account, HostedStore
    from mindin.hosted.accounts.vaults import Vault

USAGE_TTL = 60.0
KNOWN_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".base": "text/yaml",
    ".yml": "text/yaml",
    ".yaml": "text/yaml",
    ".canvas": "application/json",
    ".json": "application/json",
    ".css": "text/css",
    ".csv": "text/csv",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".avif": "image/avif",
    ".pdf": "application/pdf",
}
BUILT_IN_TYPES = mimetypes.MimeTypes(filenames=())
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StorageLimits:
    account_bytes: int | None = None
    file_bytes: int | None = None
    content_types: tuple[str, ...] = ()

    def allows_type(self, content_type: str) -> bool:
        if not self.content_types:
            return True
        major = content_type.partition("/")[0] + "/*"
        return content_type in self.content_types or major in self.content_types


def size_text(count: int) -> str:
    for unit, factor in (("GB", 1000**3), ("MB", 1000**2), ("kB", 1000)):
        if count >= factor:
            return f"{count / factor:.1f}".rstrip("0").rstrip(".") + f" {unit}"
    return f"{count} bytes"


def content_type_of(key: str) -> str:
    dot = key.rfind(".")
    suffix = key[dot:].lower() if dot > key.rfind("/") else ""
    return KNOWN_TYPES.get(suffix) or BUILT_IN_TYPES.guess_type("file" + suffix)[0] or "application/octet-stream"


def refusal(limits: StorageLimits, key: str, size: int) -> LimitReached | None:
    content_type = content_type_of(key)
    if not limits.allows_type(content_type):
        allowed = ", ".join(limits.content_types)
        return LimitReached(
            f"{key} is refused: this server stores only {allowed}, and the file is {content_type}", 415, key
        )
    if limits.file_bytes is not None and size > limits.file_bytes:
        return LimitReached(
            f"{key} is refused: it is {size_text(size)}, and a file is at most {size_text(limits.file_bytes)}"
            " on this server",
            413,
            key,
        )
    return None


def full(account_bytes: int, used: int, adding: int) -> LimitReached:
    return LimitReached(
        f"the files take {size_text(adding)}, and the account's storage of {size_text(account_bytes)}"
        f" has {size_text(max(account_bytes - used, 0))} free ({size_text(used)} used)",
        507,
    )


class Quotas:
    def __init__(
        self,
        store: HostedStore,
        extensions: Sequence[Extension] = (),
        sizes: Callable[[str], int] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.extensions = tuple(extensions)
        self.sizes = sizes
        self.clock = clock
        self._lock = threading.Lock()
        self._used: dict[str, tuple[float, int]] = {}

    def limits(self, account: Account) -> StorageLimits:
        chosen = StorageLimits()
        for extension in self.extensions:
            chosen = extension.storage_limits(account, chosen)
        return chosen

    def guard(self, vault: Vault, backend: Any) -> Any:
        if not self.extensions:
            return backend
        return LimitedBackend(backend, vault, self)

    def size_of(self, vault_id: str) -> int:
        with self._lock:
            cached = self._used.get(vault_id)
            if cached is not None and self.clock() - cached[0] < USAGE_TTL:
                return cached[1]
        counted = self.sizes(vault_id) if self.sizes is not None else 0
        with self._lock:
            self._used[vault_id] = (self.clock(), counted)
        return counted

    def used_elsewhere(self, owner: str, vault_id: str) -> int:
        return sum(self.size_of(vault.id) for vault in self.store.vaults(owner) if vault.id != vault_id)

    def forget(self, vault_id: str) -> None:
        with self._lock:
            self._used.pop(vault_id, None)

    def report_refusal(self, vault: Vault, refusal: LimitReached) -> LimitReached:
        owner = self.store.account_by_id(vault.account_id)
        for extension in self.extensions if owner is not None else ():
            try:
                extension.refused_write(owner, vault, refusal.key, refusal)
            except Exception:
                logger.exception("an extension failed on a refused write to %s", vault.id)
        return refusal


class LimitedBackend:
    def __init__(self, inner: Any, vault: Vault, quotas: Quotas) -> None:
        self.inner = inner
        self.vault = vault
        self.quotas = quotas

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def limits(self) -> StorageLimits:
        owner = self.quotas.store.account_by_id(self.vault.account_id)
        return self.quotas.limits(owner) if owner is not None else StorageLimits()

    def check(self, key: str, size: int) -> None:
        limits = self.limits()
        found = refusal(limits, key, size)
        if found is not None:
            raise self.report_refusal(found)
        if limits.account_bytes is None:
            return
        entries: list[Entry] = self.inner.list_all()
        here = sum(entry.size for entry in entries if not entry.is_dir)
        before = next((entry.size for entry in entries if entry.key == key), 0)
        if size <= before:
            return
        used = here + self.quotas.used_elsewhere(self.vault.account_id, self.vault.id)
        if used - before + size > limits.account_bytes:
            raise self.report_refusal(
                LimitReached(
                    f"{key} is refused: the account's storage of {size_text(limits.account_bytes)} is full"
                    f" ({size_text(used)} used). Delete files to make room; reading and export still work.",
                    507,
                    key,
                )
            )

    def report_refusal(self, found: LimitReached) -> LimitReached:
        return self.quotas.report_refusal(self.vault, found)

    def used(self) -> tuple[dict[str, int], int]:
        here = {entry.key: entry.size for entry in self.inner.list_all() if not entry.is_dir}
        return here, self.quotas.used_elsewhere(self.vault.account_id, self.vault.id)

    def preflight(self, sizes: dict[str, int]) -> list[LimitReached]:
        limits = self.limits()
        found = [refused for key, size in sizes.items() if (refused := refusal(limits, key, size)) is not None]
        if limits.account_bytes is None:
            return found
        here, elsewhere = self.used()
        after = sum(size for key, size in here.items() if key not in sizes) + sum(sizes.values()) + elsewhere
        if after > limits.account_bytes:
            found.append(full(limits.account_bytes, sum(here.values()) + elsewhere, sum(sizes.values())))
        return found

    def wrote(self, result: Any) -> Any:
        self.quotas.forget(self.vault.id)
        return result

    def put_checked(self, key: str, data: bytes, expected_version: str | None = None, *rest: Any) -> str:
        self.check(key, len(data))
        return self.wrote(self.inner.put_checked(key, data, expected_version, *rest))

    def put(self, key: str, text: str, expected_version: str | None = None) -> str:
        self.check(key, len(text.encode("utf-8")))
        return self.wrote(self.inner.put(key, text, expected_version))

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        self.check(key, len(data))
        return self.wrote(self.inner.put_bytes(key, data, content_type))

    def move(self, src: str, dst: str) -> None:
        found = refusal(self.limits(), dst, 0)
        if found is not None:
            raise self.report_refusal(found)
        self.inner.move(src, dst)

    def delete(self, key: str) -> None:
        self.wrote(self.inner.delete(key))

    def delete_checked(self, key: str, expected_version: str) -> None:
        self.wrote(self.inner.delete_checked(key, expected_version))

    def list(self) -> list[Entry]:
        return self.inner.list()

    def get(self, key: str) -> tuple[str, str]:
        return self.inner.get(key)

    def list_all(self, prefix: str = "") -> list[Entry]:
        return self.inner.list_all(prefix)

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        return self.inner.get_bytes(key)

    def mkdir(self, key: str) -> None:
        self.inner.mkdir(key)


def limits_document(quotas: Quotas, vault: Vault, owner: Account, upload_bytes: int) -> dict[str, Any]:
    limits = quotas.limits(owner)
    return {
        "vault": vault.slug,
        "kind": vault.kind,
        "file_bytes": limits.file_bytes,
        "account_bytes": limits.account_bytes,
        "content_types": list(limits.content_types),
        "used_bytes": sum(quotas.size_of(owned.id) for owned in quotas.store.vaults(owner.id)),
        "vault_bytes": quotas.size_of(vault.id),
        "upload_bytes": upload_bytes,
    }


def limits_of(document: dict[str, Any]) -> StorageLimits:
    return StorageLimits(
        account_bytes=document.get("account_bytes"),
        file_bytes=document.get("file_bytes"),
        content_types=tuple(document.get("content_types") or ()),
    )


def with_defaults(limits: StorageLimits, **defaults: Any) -> StorageLimits:
    """These limits, with each one left unset taken from defaults."""
    return replace(limits, **{name: value for name, value in defaults.items() if not getattr(limits, name)})
