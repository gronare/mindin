from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from notes_vault_mcp.backends import Entry, VaultBackend

Kind = Literal["write", "schema", "gone", "all"]
Listener = Callable[["Change"], None]
PENDING_CHANGES = 1024
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Change:
    """What changed outside this server's requests: a vault's files, its layout, the vault itself, or every vault."""

    vault_id: str
    kind: Kind
    origin: str


class ChangeBus(Protocol):
    def publish(self, change: Change) -> None: ...

    def listen(self, listener: Listener) -> None: ...

    def close(self) -> None: ...


class NullBus:
    def publish(self, change: Change) -> None:
        pass

    def listen(self, listener: Listener) -> None:
        pass

    def close(self) -> None:
        pass


NULL_BUS = NullBus()


class LocalBus:
    """Hands every change to every listener in this process, on the publisher's thread."""

    def __init__(self) -> None:
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()

    def publish(self, change: Change) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(change)
            except Exception:
                log.exception("notes-vault-mcp: a change listener failed on %s", change)

    def listen(self, listener: Listener) -> None:
        with self._lock:
            self._listeners.append(listener)

    def close(self) -> None:
        with self._lock:
            self._listeners.clear()


class Publisher:
    """Hands changes to a bus on one background thread, so a slow or failing bus never holds up or fails a write."""

    def __init__(self, bus: ChangeBus, capacity: int = PENDING_CHANGES) -> None:
        self.bus = bus
        self._queue: queue.Queue[Change | None] = queue.Queue(capacity)
        self._thread = threading.Thread(target=self._run, name="vault-changes", daemon=True)
        self._thread.start()

    def publish(self, change: Change) -> None:
        try:
            self._queue.put_nowait(change)
        except queue.Full:
            log.warning("notes-vault-mcp: the change bus is behind; dropping %s", change)

    def _run(self) -> None:
        while True:
            change = self._queue.get()
            try:
                if change is None:
                    return
                self.bus.publish(change)
            except Exception:
                log.exception("notes-vault-mcp: could not publish %s", change)
            finally:
                self._queue.task_done()

    def drain(self) -> None:
        self._queue.join()

    def close(self, timeout: float = 5.0) -> None:
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            log.warning("notes-vault-mcp: the change bus is behind; closing with changes unsent")
            return
        self._thread.join(timeout)


class Announcing:
    """A vault's backend that calls `changed` after every write that went through."""

    def __init__(self, inner: VaultBackend, changed: Callable[[], None]) -> None:
        self.inner = inner
        self._changed = changed

    def changed(self) -> None:
        try:
            self._changed()
        except Exception:
            log.exception("notes-vault-mcp: could not announce a write")

    def list(self) -> list[Entry]:
        return self.inner.list()

    def list_all(self, prefix: str = "") -> list[Entry]:
        return self.inner.list_all(prefix)

    def get(self, key: str) -> tuple[str, str]:
        return self.inner.get(key)

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        return self.inner.get_bytes(key)

    def put(self, key: str, text: str, expected_version: str | None = None) -> str:
        version = self.inner.put(key, text, expected_version)
        self.changed()
        return version

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        version = self.inner.put_bytes(key, data, content_type)
        self.changed()
        return version

    def put_checked(
        self, key: str, data: bytes, expected_version: str | None = None, content_type: str | None = None
    ) -> str:
        put_checked = self.inner.put_checked
        if content_type is None:
            version = put_checked(key, data, expected_version)
        else:
            version = put_checked(key, data, expected_version, content_type)
        self.changed()
        return version

    def delete(self, key: str) -> None:
        self.inner.delete(key)
        self.changed()

    def move(self, src: str, dst: str) -> None:
        self.inner.move(src, dst)
        self.changed()

    def mkdir(self, key: str) -> None:
        self.inner.mkdir(key)
        self.changed()

    def forget_listing(self) -> None:
        forget_listing(self.inner)


def forget_listing(backend: VaultBackend) -> None:
    forget = getattr(backend, "forget_listing", None)
    if forget is not None:
        forget()
