from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from mindin import provision
from mindin.backends import VaultBackend, VaultError, make_backend, make_subject_backend
from mindin.changes import NULL_BUS, Announcing, Change, ChangeBus, Kind, Publisher, forget_listing
from mindin.config import cache_dir, env
from mindin.embedding import Embedder, configured_embedder
from mindin.index import Index, remove_index
from mindin.schema import Schema, deep_merge, default_schema_data, load_schema
from mindin.shared import SharedRules, shared_backend
from mindin.vault_cache import IndexSweep, Leaving, SnapshotSchedule, VaultCache
from mindin.vault_cache import subject_key as subject_key

IDLE_SECONDS = 1800.0
SNAPSHOT_SECONDS = 600.0
SWEEP_SECONDS = 3600.0
Vectoring = Literal["off", "wait", "background"]
BackendFactory = Callable[[str], tuple[VaultBackend, str]]


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


@dataclass(frozen=True)
class Reachable:
    subject: str
    slug: str
    name: str
    organisation: str = ""


class Reach(Protocol):
    def around(self, claims: dict[str, Any], subject: str) -> list[Reachable]: ...


class VaultResolver(Protocol):
    def current(self) -> Vault: ...

    def using(self) -> AbstractContextManager[Vault]: ...

    def around(self) -> list[Reachable]: ...

    def using_named(self, slug: str) -> AbstractContextManager[Vault]: ...

    def using_reached(self, reached: Reachable) -> AbstractContextManager[Vault]: ...

    @property
    def spans_vaults(self) -> bool: ...

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

    def around(self) -> list[Reachable]:
        return []

    def using_named(self, slug: str) -> AbstractContextManager[Vault]:
        raise NoVault(f"this server serves one vault; leave vault out instead of naming {slug!r}")

    def using_reached(self, reached: Reachable) -> AbstractContextManager[Vault]:
        return self.using_named(reached.slug)

    @property
    def spans_vaults(self) -> bool:
        return False

    @property
    def instruction_schema(self) -> Schema:
        return self.vault.schema

    @property
    def searches_by_meaning(self) -> bool:
        return self.vault.embedder is not None

    def close(self) -> None:
        self.vault.close()


class NoVault(VaultError):
    pass


class Unconfigured:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def current(self) -> Vault:
        raise NoVault(self.reason)

    def using(self) -> AbstractContextManager[Vault]:
        raise NoVault(self.reason)

    def around(self) -> list[Reachable]:
        return []

    def using_named(self, slug: str) -> AbstractContextManager[Vault]:
        raise NoVault(self.reason)

    def using_reached(self, reached: Reachable) -> AbstractContextManager[Vault]:
        raise NoVault(self.reason)

    @property
    def spans_vaults(self) -> bool:
        return False

    @property
    def instruction_schema(self) -> Schema:
        return Schema(default_schema_data())

    @property
    def searches_by_meaning(self) -> bool:
        return False

    def close(self) -> None:
        pass


class VaultChanges:
    def __init__(self, open_vaults: VaultCache, gone: Callable[[str], None]) -> None:
        self.open_vaults = open_vaults
        self.gone = gone
        self.origin = secrets.token_hex(8)
        self.bus: ChangeBus = NULL_BUS
        self._publisher: Publisher | None = None

    def use(self, bus: ChangeBus) -> None:
        self.bus = bus
        if bus is not NULL_BUS:
            self._publisher = Publisher(bus)
        bus.listen(self.apply)

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
            for vault in self.open_vaults.vaults():
                vault.mark_stale()
            return
        if change.kind == "gone":
            self.gone(change.vault_id)
            return
        found = self.open_vaults.get(change.vault_id)
        if found is None:
            return
        if change.kind == "schema":
            found.mark_schema_changed()
        else:
            found.mark_stale()

    def close(self) -> None:
        if self._publisher is not None:
            self._publisher.close()
            self._publisher = None
        self.bus.close()


class TokenResolver:
    def __init__(
        self, using_subject: Callable[[str], AbstractContextManager[Vault]], for_subject: Callable[[str], Vault]
    ) -> None:
        self.using_subject = using_subject
        self.for_subject = for_subject
        self.reach: Reach | None = None

    def subject(self) -> str:
        return self.token()[0]

    def token(self) -> tuple[str, dict[str, Any]]:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is None or not token.subject:
            raise NoSubject("no authenticated subject on this request")
        return token.subject, dict(token.claims or {})

    def current(self) -> Vault:
        return self.for_subject(self.subject())

    @contextmanager
    def using(self) -> Iterator[Vault]:
        with self.using_subject(self.subject()) as vault:
            yield vault

    def around(self) -> list[Reachable]:
        if self.reach is None:
            return []
        subject, claims = self.token()
        return self.reach.around(claims, subject)

    @contextmanager
    def using_named(self, slug: str) -> Iterator[Vault]:
        subject, claims = self.token()
        if slug == claims.get("vault"):
            found: Reachable | None = Reachable(subject, slug, str(claims.get("vault_name") or slug))
        else:
            found = next((reached for reached in self.around() if reached.slug == slug), None)
        if found is None:
            raise NoVault(f"this connection does not reach a vault {slug!r}; search names the vaults it reaches")
        with self.using_subject(found.subject) as vault:
            yield vault


class SubjectVaults:
    # One vault per authenticated subject under a common prefix, provisioned on first use and closed when idle.
    def __init__(
        self,
        cache: Path | None = None,
        prefix: str = "users",
        dav_url: str = "",
        schema_template: str = provision.DEFAULT_SCHEMA_TEMPLATE,
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
        self.clock = clock
        self._instruction_schema: Schema | None = None
        self.backend_factory: BackendFactory = self.plain_backend
        self.identify: Callable[[str], str] = lambda subject: self.plain_backend(subject)[1]
        self.open_vaults = VaultCache(idle_seconds, clock, self._leave)
        self.changes = VaultChanges(self.open_vaults, self._gone)
        self.snapshotting: SnapshotSchedule | None = None
        self.sweeping = IndexSweep(self.cache, self._opened_identifiers)
        self.resolver = TokenResolver(self.using_subject, self.for_subject)

    @property
    def idle_seconds(self) -> float:
        return self.open_vaults.idle_seconds

    @property
    def origin(self) -> str:
        return self.changes.origin

    @property
    def bus(self) -> ChangeBus:
        return self.changes.bus

    @property
    def reach(self) -> Reach | None:
        return self.resolver.reach

    @property
    def snapshots(self) -> Snapshots | None:
        return None if self.snapshotting is None else self.snapshotting.snapshots

    @property
    def live_identifiers(self) -> Callable[[], set[str]] | None:
        return self.sweeping.live_identifiers

    @live_identifiers.setter
    def live_identifiers(self, live: Callable[[], set[str]] | None) -> None:
        self.sweeping.live_identifiers = live

    def plain_backend(self, subject: str) -> tuple[VaultBackend, str]:
        return make_subject_backend(subject_key(subject), self.prefix)

    def use_backends(self, factory: BackendFactory, identify: Callable[[str], str] | None = None) -> None:
        with self.open_vaults.lock:
            if not self.open_vaults.empty or self.backend_factory != self.plain_backend:
                raise VaultError("the storage of subject vaults is chosen once, before any vault opens")
            self.backend_factory = factory
            if identify is not None:
                self.identify = identify

    def use_bus(self, bus: ChangeBus) -> None:
        self.changes.use(bus)

    def use_reach(self, reach: Reach) -> None:
        self.resolver.reach = reach

    def use_snapshots(self, snapshots: Snapshots, every_seconds: float = SNAPSHOT_SECONDS) -> None:
        self.snapshotting = SnapshotSchedule(snapshots, every_seconds, self.clock)

    def use_sweep(self, live_identifiers: Callable[[], set[str]], every_seconds: float = SWEEP_SECONDS) -> None:
        self.sweeping.start(live_identifiers, every_seconds)

    def sweep(self) -> list[str]:
        return self.sweeping.sweep()

    def _opened_identifiers(self) -> set[str]:
        return {self.identify(subject) for subject in self.open_vaults.subjects()}

    def snapshot_later(self, subject: str, index_path: Path, now: bool = False) -> None:
        if self.snapshotting is not None:
            self.snapshotting.later(subject, index_path, now)

    def publish(self, subject: str, kind: Kind) -> None:
        self.changes.publish(subject, kind)

    def drain(self) -> None:
        self.changes.drain()

    def apply(self, change: Change) -> None:
        self.changes.apply(change)

    def _gone(self, subject: str) -> None:
        self.discard(subject)
        remove_index(self.cache, self.identify(subject))

    def wrote(self, subject: str) -> None:
        opened = self.open_vaults.get(subject)
        if opened is not None:
            opened.mark_stale(listing=False)

    def current(self) -> Vault:
        return self.resolver.current()

    def using(self) -> AbstractContextManager[Vault]:
        return self.resolver.using()

    def around(self) -> list[Reachable]:
        return self.resolver.around()

    def using_named(self, slug: str) -> AbstractContextManager[Vault]:
        return self.resolver.using_named(slug)

    def using_reached(self, reached: Reachable) -> AbstractContextManager[Vault]:
        return self.using_subject(reached.subject)

    @property
    def spans_vaults(self) -> bool:
        return self.reach is not None

    @contextmanager
    def using_subject(self, subject: str) -> Iterator[Vault]:
        vault = self._acquire(subject, hold=True)
        try:
            yield vault
        finally:
            self.open_vaults.release(subject, vault)

    def refresh(self, subject: str) -> None:
        opened = self.open_vaults.get(subject)
        if opened is not None:
            opened.reload_schema()

    def for_subject(self, subject: str) -> Vault:
        return self._acquire(subject, hold=False)

    def _acquire(self, subject: str, hold: bool) -> Vault:
        vault = self.open_vaults.acquire(subject, hold, self._open_vault)
        vault.catch_up()
        return vault

    def _open_vault(self, subject: str) -> Vault:
        backend, identifier = self.backend_factory(subject)
        shared = shared_backend(backend)
        if shared is not None and provision.is_empty(backend):
            provision.initialize(shared.inner, preset=shared.layout)
        elif provision.is_empty(backend):
            provision.initialize(backend, schema_template=self.schema_template, welcome={"DAV_URL": self.dav_url})
        announced = backend if self.bus is NULL_BUS else Announcing(backend, lambda: self.publish(subject, "write"))
        index_path = self.cache / f"{identifier}.sqlite"
        schedule = self.snapshotting
        if schedule is not None:
            schedule.restore(subject, index_path)
        vault = Vault(
            backend=announced,
            schema=load_schema(backend),
            index_path=index_path,
            embedder=self.embedder,
            background=self.background,
            settled=None if schedule is None else lambda: schedule.later(subject, index_path),
        )
        vault.rules = shared.rules if shared is not None else None
        return vault

    def discard(self, subject: str) -> None:
        self.open_vaults.discard(subject)

    def _leave(self, leaving: Leaving) -> None:
        for subject, vault in leaving:
            indexed = vault.index_opened
            vault.close()
            if indexed and vault.index_path is not None:
                self.snapshot_later(subject, vault.index_path, now=True)
        if leaving:
            self.sweeping.wake()

    @property
    def open_subjects(self) -> list[str]:
        return self.open_vaults.keys

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
        self._leave(self.open_vaults.clear())
        self.sweeping.close()
        if self.snapshotting is not None:
            self.snapshotting.close()
        self.changes.close()
