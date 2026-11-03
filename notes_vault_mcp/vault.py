from __future__ import annotations

import hashlib
import logging
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Literal, Protocol

import yaml

from notes_vault_mcp import provision
from notes_vault_mcp.backends import VaultBackend, VaultError, make_backend, make_subject_backend
from notes_vault_mcp.changes import NULL_BUS, Announcing, Change, ChangeBus, Kind, Publisher, forget_listing
from notes_vault_mcp.config import cache_dir, env
from notes_vault_mcp.embedding import Embedder, configured_embedder
from notes_vault_mcp.index import Index, index_fingerprint, remove_index
from notes_vault_mcp.schema import Schema, deep_merge, default_schema_data, load_schema
from notes_vault_mcp.shared import SHARED_PRESET, SharedRules, shared_backend

SUBJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
IDLE_SECONDS = 1800.0
SNAPSHOT_SECONDS = 600.0
SWEEP_SECONDS = 3600.0
INDEX_STEM_RE = re.compile(r"[0-9a-f]{12}")
Vectoring = Literal["off", "wait", "background"]
BackendFactory = Callable[[str], tuple[VaultBackend, str]]
log = logging.getLogger(__name__)


class Vault:
    def __init__(
        self,
        backend: VaultBackend,
        schema: Schema,
        index_path: Path | None = None,
        index: Index | None = None,
        embedder: Embedder | None = None,
        background: bool = False,
        schema_path: str | None = None,
        settled: Callable[[], None] | None = None,
    ):
        self.backend = backend
        self.settled = settled
        self.schema = schema
        self.schema_path = schema_path
        self.schema_version: str | None = None
        self.index_path = index_path
        self.embedder = embedder
        self.background = background
        self.rules: SharedRules | None = None
        self._index = index
        self._stale = False
        self._schema_changed = False
        self._catching_up = threading.Lock()

    @property
    def index(self) -> Index:
        if self._index is None:
            if self.index_path is None:
                raise RuntimeError("the vault has no index path")
            self._index = Index(
                self.index_path,
                self.backend,
                self.schema.languages,
                embedder=self.embedder,
                background=self.background,
                on_settled=self.settled,
            )
        return self._index

    @property
    def index_opened(self) -> bool:
        return self._index is not None

    def reload_schema(self) -> None:
        fresh = load_schema(self.backend, self.schema_path)
        relanguaged = fresh.languages != self.schema.languages
        self.schema = fresh
        if relanguaged and self._index is not None:
            self.close()
            self.index.rebuild()

    def mark_stale(self, listing: bool = True) -> None:
        if listing:
            forget_listing(self.backend)
        self._stale = True

    def mark_schema_changed(self) -> None:
        self.mark_stale()
        self._schema_changed = True

    def catch_up(self) -> None:
        if not self._schema_changed:
            return
        with self._catching_up:
            if self._schema_changed:
                self._schema_changed = False
                self.reload_schema()

    def sync(self, force: bool = False) -> int:
        self.catch_up()
        force, self._stale = force or self._stale, False
        count = self.index.sync(force=force)
        seen = self.index.schema_version
        if seen is not None and seen != self.schema_version:
            self.reload_schema()
            self.schema_version = seen
        return count

    def start_embedding(self) -> None:
        if self.embedder is not None and self.index.vectors is not None:
            self.index.vectors.refresh()

    def close(self) -> None:
        if self._index is not None:
            self._index.close()
            self._index = None


def open_vault(cache: Path | None = None, vectors: Vectoring = "off") -> Vault:
    backend, identifier = make_backend()
    schema_path = env("VAULT_SCHEMA")
    schema = load_schema(backend, schema_path)
    folder = cache or cache_dir()
    return Vault(
        backend=backend,
        schema=schema,
        schema_path=schema_path,
        index_path=folder / f"{identifier}.sqlite",
        embedder=configured_embedder(folder) if vectors != "off" else None,
        background=vectors == "background",
    )


class NoSubject(VaultError):
    pass


class Snapshots(Protocol):
    def save(self, subject: str, index_path: Path) -> bool: ...

    def restore(self, subject: str, index_path: Path) -> bool: ...


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
        self._subjects: dict[str, str] = {}
        self._in_use: dict[str, int] = {}
        self._opening: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._instruction_schema: Schema | None = None
        self.backend_factory: BackendFactory = self.plain_backend
        self.identify: Callable[[str], str] = lambda subject: self.plain_backend(subject)[1]
        self.origin = secrets.token_hex(8)
        self.bus: ChangeBus = NULL_BUS
        self._publisher: Publisher | None = None
        self.snapshots: Snapshots | None = None
        self.snapshot_seconds = SNAPSHOT_SECONDS
        self._snapshotting: ThreadPoolExecutor | None = None
        self._queued: set[str] = set()
        self._queued_lock = threading.Lock()
        self._saved_at: dict[str, float] = {}
        self._saved_print: dict[str, str] = {}
        self._timers: dict[str, threading.Timer] = {}
        self.live_identifiers: Callable[[], set[str]] | None = None
        self._sweeping = threading.Event()
        self._sweeper: threading.Thread | None = None
        self._closed = threading.Event()

    def plain_backend(self, subject: str) -> tuple[VaultBackend, str]:
        return make_subject_backend(subject_key(subject), self.prefix)

    def use_backends(self, factory: BackendFactory, identify: Callable[[str], str] | None = None) -> None:
        with self._lock:
            if self._open or self.backend_factory != self.plain_backend:
                raise VaultError("the storage of subject vaults is chosen once, before any vault opens")
            self.backend_factory = factory
            if identify is not None:
                self.identify = identify

    def use_bus(self, bus: ChangeBus) -> None:
        self.bus = bus
        if bus is not NULL_BUS:
            self._publisher = Publisher(bus)
        bus.listen(self.apply)

    def use_snapshots(self, snapshots: Snapshots, every_seconds: float = SNAPSHOT_SECONDS) -> None:
        self.snapshots = snapshots
        self.snapshot_seconds = every_seconds
        self._snapshotting = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vault-snapshots")

    def use_sweep(self, live_identifiers: Callable[[], set[str]], every_seconds: float = SWEEP_SECONDS) -> None:
        self.live_identifiers = live_identifiers
        self._sweeper = threading.Thread(
            target=self._sweep_loop, args=(every_seconds,), name="vault-index-sweep", daemon=True
        )
        self._sweeper.start()

    def _sweep_loop(self, every_seconds: float) -> None:
        while not self._closed.is_set():
            self.sweep()
            self._sweeping.wait(every_seconds)
            self._sweeping.clear()

    def sweep(self) -> list[str]:
        if self.live_identifiers is None:
            return []
        try:
            found = {path.name.partition(".")[0] for path in self.cache.glob("*.sqlite*")}
            local = {stem for stem in found if INDEX_STEM_RE.fullmatch(stem)}
            if not local:
                return []
            live = self.live_identifiers()
            with self._lock:
                opened = {self.identify(subject) for subject in self._subjects.values()}
            orphaned = sorted(local - live - opened)
            for identifier in orphaned:
                remove_index(self.cache, identifier)
            return orphaned
        except Exception:
            log.exception("notes-vault-mcp: could not sweep the local indexes of deleted vaults")
            return []

    def snapshot_later(self, subject: str, index_path: Path, now: bool = False) -> None:
        if self._snapshotting is None:
            return
        with self._queued_lock:
            if subject in self._queued:
                return
            wait = 0.0 if now else self._saved_at.get(subject, -math.inf) + self.snapshot_seconds - self.clock()
            if wait > 0:
                if subject not in self._timers and not self._closed.is_set():
                    timer = threading.Timer(wait, self.snapshot_later, (subject, index_path, True))
                    timer.daemon = True
                    self._timers[subject] = timer
                    timer.start()
                return
            timer = self._timers.pop(subject, None)
            if timer is not None:
                timer.cancel()
            self._queued.add(subject)
        try:
            self._snapshotting.submit(self._snapshot, subject, index_path)
        except RuntimeError:
            with self._queued_lock:
                self._queued.discard(subject)

    def _snapshot(self, subject: str, index_path: Path) -> None:
        with self._queued_lock:
            self._queued.discard(subject)
        try:
            if self.snapshots is None or not index_path.exists():
                return
            fingerprint = index_fingerprint(index_path)
            if fingerprint == self._saved_print.get(subject):
                return
            if self.snapshots.save(subject, index_path):
                with self._queued_lock:
                    self._saved_at[subject] = self.clock()
                    self._saved_print[subject] = fingerprint
        except Exception:
            log.exception("notes-vault-mcp: could not store the index snapshot of a vault")

    def _restore(self, subject: str, index_path: Path) -> None:
        if self.snapshots is None or index_path.exists():
            return
        try:
            if self.snapshots.restore(subject, index_path):
                with self._queued_lock:
                    self._saved_at[subject] = self.clock()
                    self._saved_print[subject] = index_fingerprint(index_path)
        except Exception:
            log.exception("notes-vault-mcp: could not restore the index snapshot of a vault; indexing it afresh")

    def publish(self, subject: str, kind: Kind) -> None:
        if self._publisher is not None:
            self._publisher.publish(Change(subject, kind, self.origin))

    def drain(self) -> None:
        if self._publisher is not None:
            self._publisher.drain()

    def apply(self, change: Change) -> None:
        if change.origin == self.origin:
            return
        if change.kind == "all":
            with self._lock:
                opened = [vault for vault, _ in self._open.values()]
            for vault in opened:
                vault.mark_stale()
            return
        if change.kind == "gone":
            self.discard(change.vault_id)
            remove_index(self.cache, self.identify(change.vault_id))
            return
        with self._lock:
            found = self._open.get(subject_key(change.vault_id))
        if found is None:
            return
        if change.kind == "schema":
            found[0].mark_schema_changed()
        else:
            found[0].mark_stale()

    def wrote(self, subject: str) -> None:
        with self._lock:
            opened = self._open.get(subject_key(subject))
        if opened is not None:
            opened[0].mark_stale(listing=False)

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
        vault = self._acquire(subject, hold=True)
        try:
            yield vault
        finally:
            with self._lock:
                self._in_use[key] -= 1
                if not self._in_use[key]:
                    del self._in_use[key]
                self._open[key] = (vault, self.clock())

    def refresh(self, subject: str) -> None:
        with self._lock:
            opened = self._open.get(subject_key(subject))
        if opened is not None:
            opened[0].reload_schema()

    def for_subject(self, subject: str) -> Vault:
        return self._acquire(subject, hold=False)

    # The global lock only guards the table of open vaults; opening one waits on that vault's own gate.
    def _acquire(self, subject: str, hold: bool) -> Vault:
        key = subject_key(subject)
        with self._lock:
            leaving = self._evict()
            vault = self._take(key, hold)
            gate = None if vault is not None else self._opening.setdefault(key, threading.Lock())
        self._leave(leaving)
        if gate is not None:
            with gate:
                with self._lock:
                    vault = self._take(key, hold)
                if vault is None:
                    opened = self._open_vault(subject)
                    with self._lock:
                        self._open[key] = (opened, self.clock())
                        self._subjects[key] = subject
                        vault = self._take(key, hold)
        assert vault is not None
        vault.catch_up()
        return vault

    def _take(self, key: str, hold: bool) -> Vault | None:
        opened = self._open.get(key)
        if opened is None:
            return None
        self._open[key] = (opened[0], self.clock())
        if hold:
            self._in_use[key] = self._in_use.get(key, 0) + 1
        return opened[0]

    def _open_vault(self, subject: str) -> Vault:
        backend, identifier = self.backend_factory(subject)
        shared = shared_backend(backend)
        if shared is not None and provision.is_empty(backend):
            provision.initialize(shared.inner, preset=SHARED_PRESET)
        elif provision.is_empty(backend):
            provision.initialize(backend, schema_template=self.schema_template, welcome={"DAV_URL": self.dav_url})
        announced = backend if self.bus is NULL_BUS else Announcing(backend, lambda: self.publish(subject, "write"))
        index_path = self.cache / f"{identifier}.sqlite"
        self._restore(subject, index_path)
        vault = Vault(
            backend=announced,
            schema=load_schema(backend),
            index_path=index_path,
            embedder=self.embedder,
            background=self.background,
            settled=None if self.snapshots is None else lambda: self.snapshot_later(subject, index_path),
        )
        vault.rules = shared.rules if shared is not None else None
        return vault

    def discard(self, subject: str) -> None:
        key = subject_key(subject)
        with self._lock:
            opened = self._open.pop(key, None)
            self._subjects.pop(key, None)
        if opened is not None:
            opened[0].close()

    def _evict(self) -> list[tuple[str, Vault]]:
        now = self.clock()
        leaving: list[tuple[str, Vault]] = []
        for key, (vault, last_used) in list(self._open.items()):
            if now - last_used > self.idle_seconds and not self._in_use.get(key):
                del self._open[key]
                leaving.append((self._subjects.pop(key, key), vault))
        return leaving

    def _leave(self, leaving: list[tuple[str, Vault]]) -> None:
        for subject, vault in leaving:
            indexed = vault.index_opened
            vault.close()
            if indexed and vault.index_path is not None:
                self.snapshot_later(subject, vault.index_path, now=True)
        if leaving:
            self._sweeping.set()

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
            leaving = [(self._subjects.get(key, key), vault) for key, (vault, _) in self._open.items()]
            self._open.clear()
            self._subjects.clear()
        self._leave(leaving)
        self._closed.set()
        self._sweeping.set()
        with self._queued_lock:
            timers, self._timers = list(self._timers.values()), {}
        for timer in timers:
            timer.cancel()
        if self._snapshotting is not None:
            self._snapshotting.shutdown(wait=True)
        if self._publisher is not None:
            self._publisher.close()
            self._publisher = None
        self.bus.close()
