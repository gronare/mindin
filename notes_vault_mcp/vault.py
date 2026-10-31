from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Literal, Protocol

import yaml

from notes_vault_mcp import provision
from notes_vault_mcp.backends import VaultBackend, VaultError, make_backend, make_subject_backend
from notes_vault_mcp.config import cache_dir, env
from notes_vault_mcp.embedding import Embedder, configured_embedder
from notes_vault_mcp.index import Index
from notes_vault_mcp.schema import Schema, deep_merge, default_schema_data, load_schema

SUBJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
IDLE_SECONDS = 1800.0
Vectoring = Literal["off", "wait", "background"]


class Vault:
    def __init__(
        self,
        backend: VaultBackend,
        schema: Schema,
        index_path: Path | None = None,
        index: Index | None = None,
        embedder: Embedder | None = None,
        background: bool = False,
    ):
        self.backend = backend
        self.schema = schema
        self.index_path = index_path
        self.embedder = embedder
        self.background = background
        self._index = index

    @property
    def index(self) -> Index:
        if self._index is None:
            if self.index_path is None:
                raise RuntimeError("the vault has no index path")
            self._index = Index(
                self.index_path, self.backend, self.schema.languages, embedder=self.embedder, background=self.background
            )
        return self._index

    @property
    def index_opened(self) -> bool:
        return self._index is not None

    def close(self) -> None:
        if self._index is not None:
            self._index.close()
            self._index = None


def open_vault(cache: Path | None = None, vectors: Vectoring = "off") -> Vault:
    backend, identifier = make_backend()
    schema = load_schema(backend, env("VAULT_SCHEMA"))
    folder = cache or cache_dir()
    return Vault(
        backend=backend,
        schema=schema,
        index_path=folder / f"{identifier}.sqlite",
        embedder=configured_embedder(folder) if vectors != "off" else None,
        background=vectors == "background",
    )


class NoSubject(VaultError):
    pass


class VaultResolver(Protocol):
    def current(self) -> Vault: ...

    def using(self) -> AbstractContextManager[Vault]: ...

    @property
    def instruction_schema(self) -> Schema: ...

    @property
    def searches_by_meaning(self) -> bool: ...

    def close(self) -> None: ...


class SingleVault:
    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def current(self) -> Vault:
        return self.vault

    @contextmanager
    def using(self) -> Iterator[Vault]:
        yield self.vault

    @property
    def instruction_schema(self) -> Schema:
        return self.vault.schema

    @property
    def searches_by_meaning(self) -> bool:
        return self.vault.embedder is not None

    def close(self) -> None:
        self.vault.close()


def subject_key(subject: str) -> str:
    # The subject names a folder, so anything outside a plain identifier is hashed rather than trusted as a path.
    return subject if SUBJECT_RE.fullmatch(subject) else hashlib.sha1(subject.encode("utf-8")).hexdigest()[:16]


class SubjectVaults:
    # One vault per authenticated subject under a common prefix, provisioned on first use and closed when idle.
    def __init__(
        self,
        cache: Path | None = None,
        prefix: str = "users",
        dav_url: str = "",
        schema_template: str = "schema.yml",
        idle_seconds: float = IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        vectors: Vectoring = "off",
    ) -> None:
        self.cache = cache or cache_dir()
        self.embedder = configured_embedder(self.cache) if vectors != "off" else None
        self.background = vectors == "background"
        self.prefix = prefix.strip("/")
        self.dav_url = dav_url
        self.schema_template = schema_template
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._open: dict[str, tuple[Vault, float]] = {}
        self._in_use: dict[str, int] = {}
        self._lock = threading.Lock()
        self._instruction_schema: Schema | None = None

    def _current_subject(self) -> str:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is None or not token.subject:
            raise NoSubject("no authenticated subject on this request")
        return token.subject

    def current(self) -> Vault:
        return self.for_subject(self._current_subject())

    @contextmanager
    def using(self) -> Iterator[Vault]:
        with self.using_subject(self._current_subject()) as vault:
            yield vault

    @contextmanager
    def using_subject(self, subject: str) -> Iterator[Vault]:
        key = subject_key(subject)
        with self._lock:
            vault = self._touch(key)
            self._in_use[key] = self._in_use.get(key, 0) + 1
        try:
            yield vault
        finally:
            with self._lock:
                self._in_use[key] -= 1
                if not self._in_use[key]:
                    del self._in_use[key]
                self._open[key] = (vault, self.clock())

    def for_subject(self, subject: str) -> Vault:
        with self._lock:
            return self._touch(subject_key(subject))

    def _touch(self, key: str) -> Vault:
        self._evict()
        opened = self._open.get(key)
        vault = opened[0] if opened else self._open_vault(key)
        self._open[key] = (vault, self.clock())
        return vault

    def _open_vault(self, key: str) -> Vault:
        backend, identifier = make_subject_backend(key, self.prefix)
        if provision.is_empty(backend):
            provision.initialize(backend, schema_template=self.schema_template, welcome={"DAV_URL": self.dav_url})
        return Vault(
            backend=backend,
            schema=load_schema(backend),
            index_path=self.cache / f"{identifier}.sqlite",
            embedder=self.embedder,
            background=self.background,
        )

    def _evict(self) -> None:
        now = self.clock()
        for key, (vault, last_used) in list(self._open.items()):
            if now - last_used > self.idle_seconds and not self._in_use.get(key):
                vault.close()
                del self._open[key]

    @property
    def open_subjects(self) -> list[str]:
        return sorted(self._open)

    @property
    def searches_by_meaning(self) -> bool:
        return self.embedder is not None

    @property
    def instruction_schema(self) -> Schema:
        if self._instruction_schema is None:
            override = yaml.safe_load(provision.template(self.schema_template)) or {}
            self._instruction_schema = Schema(deep_merge(default_schema_data(), override))
        return self._instruction_schema

    def close(self) -> None:
        with self._lock:
            for vault, _ in self._open.values():
                vault.close()
            self._open.clear()
