from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from mindin.index import index_fingerprint, remove_index

if TYPE_CHECKING:
    from mindin.vault import Snapshots, Vault

SUBJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
INDEX_STEM_RE = re.compile(r"[0-9a-f]{12}")
Leaving = list[tuple[str, "Vault"]]
log = logging.getLogger(__name__)


def subject_key(subject: str) -> str:
    # The subject names a folder, so anything outside a plain identifier is hashed rather than trusted as a path.
    return subject if SUBJECT_RE.fullmatch(subject) else hashlib.sha1(subject.encode("utf-8")).hexdigest()[:16]


class VaultCache:
    def __init__(self, idle_seconds: float, clock: Callable[[], float], on_leave: Callable[[Leaving], None]) -> None:
        self.idle_seconds = idle_seconds
        self.clock = clock
        self.on_leave = on_leave
        self.lock = threading.Lock()
        self._open: dict[str, tuple[Vault, float]] = {}
        self._subjects: dict[str, str] = {}
        self._in_use: dict[str, int] = {}
        self._opening: dict[str, threading.Lock] = {}

    # The global lock only guards the table of open vaults; opening one waits on that vault's own gate.
    def acquire(self, subject: str, hold: bool, open_vault: Callable[[str], Vault]) -> Vault:
        key = subject_key(subject)
        with self.lock:
            leaving = self._evict()
            vault = self._take(key, hold)
            gate = None if vault is not None else self._opening.setdefault(key, threading.Lock())
        self.on_leave(leaving)
        if gate is not None:
            with gate:
                with self.lock:
                    vault = self._take(key, hold)
                if vault is None:
                    opened = open_vault(subject)
                    with self.lock:
                        self._open[key] = (opened, self.clock())
                        self._subjects[key] = subject
                        vault = self._take(key, hold)
        assert vault is not None
        return vault

    def release(self, subject: str, vault: Vault) -> None:
        key = subject_key(subject)
        with self.lock:
            self._in_use[key] -= 1
            if not self._in_use[key]:
                del self._in_use[key]
            self._open[key] = (vault, self.clock())

    def get(self, subject: str) -> Vault | None:
        with self.lock:
            opened = self._open.get(subject_key(subject))
        return None if opened is None else opened[0]

    def vaults(self) -> list[Vault]:
        with self.lock:
            return [vault for vault, _ in self._open.values()]

    def subjects(self) -> list[str]:
        with self.lock:
            return list(self._subjects.values())

    @property
    def keys(self) -> list[str]:
        return sorted(self._open)

    @property
    def empty(self) -> bool:
        return not self._open

    def discard(self, subject: str) -> None:
        key = subject_key(subject)
        with self.lock:
            opened = self._open.pop(key, None)
            self._subjects.pop(key, None)
        if opened is not None:
            opened[0].close()

    def clear(self) -> Leaving:
        with self.lock:
            leaving = [(self._subjects.get(key, key), vault) for key, (vault, _) in self._open.items()]
            self._open.clear()
            self._subjects.clear()
        return leaving

    def _take(self, key: str, hold: bool) -> Vault | None:
        opened = self._open.get(key)
        if opened is None:
            return None
        self._open[key] = (opened[0], self.clock())
        if hold:
            self._in_use[key] = self._in_use.get(key, 0) + 1
        return opened[0]

    def _evict(self) -> Leaving:
        now = self.clock()
        leaving: Leaving = []
        for key, (vault, last_used) in list(self._open.items()):
            if now - last_used > self.idle_seconds and not self._in_use.get(key):
                del self._open[key]
                leaving.append((self._subjects.pop(key, key), vault))
        return leaving


class SnapshotSchedule:
    def __init__(self, snapshots: Snapshots, every_seconds: float, clock: Callable[[], float]) -> None:
        self.snapshots = snapshots
        self.every_seconds = every_seconds
        self.clock = clock
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vault-snapshots")
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._saved_at: dict[str, float] = {}
        self._saved_print: dict[str, str] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._closed = threading.Event()

    def later(self, subject: str, index_path: Path, now: bool = False) -> None:
        with self._lock:
            if subject in self._queued:
                return
            wait = 0.0 if now else self._saved_at.get(subject, -math.inf) + self.every_seconds - self.clock()
            if wait > 0:
                if subject not in self._timers and not self._closed.is_set():
                    timer = threading.Timer(wait, self.later, (subject, index_path, True))
                    timer.daemon = True
                    self._timers[subject] = timer
                    timer.start()
                return
            timer = self._timers.pop(subject, None)
            if timer is not None:
                timer.cancel()
            self._queued.add(subject)
        try:
            self.executor.submit(self._save, subject, index_path)
        except RuntimeError:
            with self._lock:
                self._queued.discard(subject)

    def _save(self, subject: str, index_path: Path) -> None:
        with self._lock:
            self._queued.discard(subject)
        try:
            if not index_path.exists():
                return
            fingerprint = index_fingerprint(index_path)
            if fingerprint == self._saved_print.get(subject):
                return
            if self.snapshots.save(subject, index_path):
                self._saved(subject, fingerprint)
        except Exception:
            log.exception("mindin: could not store the index snapshot of a vault")

    def restore(self, subject: str, index_path: Path) -> None:
        if index_path.exists():
            return
        try:
            if self.snapshots.restore(subject, index_path):
                self._saved(subject, index_fingerprint(index_path))
        except Exception:
            log.exception("mindin: could not restore the index snapshot of a vault; indexing it afresh")

    def _saved(self, subject: str, fingerprint: str) -> None:
        with self._lock:
            self._saved_at[subject] = self.clock()
            self._saved_print[subject] = fingerprint

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            timers, self._timers = list(self._timers.values()), {}
        for timer in timers:
            timer.cancel()
        self.executor.shutdown(wait=True)


class IndexSweep:
    def __init__(self, cache: Path, opened: Callable[[], set[str]]) -> None:
        self.cache = cache
        self.opened = opened
        self.live_identifiers: Callable[[], set[str]] | None = None
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, live_identifiers: Callable[[], set[str]], every_seconds: float) -> None:
        self.live_identifiers = live_identifiers
        self._thread = threading.Thread(target=self._loop, args=(every_seconds,), name="vault-index-sweep", daemon=True)
        self._thread.start()

    def _loop(self, every_seconds: float) -> None:
        while not self._closed.is_set():
            self.sweep()
            self._wake.wait(every_seconds)
            self._wake.clear()

    def sweep(self) -> list[str]:
        if self.live_identifiers is None:
            return []
        try:
            found = {path.name.partition(".")[0] for path in self.cache.glob("*.sqlite*")}
            local = {stem for stem in found if INDEX_STEM_RE.fullmatch(stem)}
            if not local:
                return []
            orphaned = sorted(local - self.live_identifiers() - self.opened())
            for identifier in orphaned:
                remove_index(self.cache, identifier)
            return orphaned
        except Exception:
            log.exception("mindin: could not sweep the local indexes of deleted vaults")
            return []

    def wake(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._closed.set()
        self._wake.set()
