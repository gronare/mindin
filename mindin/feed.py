from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

import anyio

from mindin.backends import FETCH_WORKERS, SCHEMA_KEY, NotFound, VaultBackend, VaultError, is_vault_key
from mindin.frontmatter import FrontmatterError, parse

if TYPE_CHECKING:
    from mindin.vault import Vault

Action = Literal["write", "delete", "schema"]
INCLUDES = ("frontmatter", "body")
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_WAIT_SECONDS = 50.0
FOLLOW_WAIT_SECONDS = 3600.0
LISTING_SECONDS = 20.0


@dataclass(frozen=True)
class Written:
    key: str
    action: Action
    version: str | None = None


@dataclass(frozen=True)
class Changed:
    key: str
    action: Action
    version: str | None
    seq: int


@dataclass(frozen=True)
class Page:
    cursor: str
    reset: bool
    changed: list[Changed]
    more: bool = False


def in_feed(key: str) -> bool:
    return key == SCHEMA_KEY or (key.endswith(".md") and is_vault_key(key))


def action_of(key: str, action: Action) -> Action:
    return "schema" if key == SCHEMA_KEY and action == "write" else action


def page_of(rows: Sequence[Changed], limit: int, head: int, cursor: Callable[[int], str]) -> Page:
    more = len(rows) > limit
    kept = list(rows[:limit])
    last = kept[-1].seq if more else max([head, *(row.seq for row in kept)])
    return Page(cursor(last), False, kept, more)


class Waiter:
    """One waiting request: woken from any thread, awaited on its event loop."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        closed: Callable[[Waiter], None] | None = None,
        ending: Callable[[], bool] = lambda: False,
    ) -> None:
        self.loop = loop
        self._event = asyncio.Event()
        self._closed = closed
        self.ending = ending

    def wake(self) -> None:
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self._event.set)

    async def wait(self, timeout: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._event.wait(), max(timeout, 0.0))
        self._event.clear()

    def close(self) -> None:
        if self._closed is not None:
            self._closed(self)
            self._closed = None


class PacedWaiter(Waiter):
    """Waits for the next listing a throttled backend allows, since nothing there tells of a change."""

    def __init__(self, loop: asyncio.AbstractEventLoop, due: Callable[[], float]) -> None:
        super().__init__(loop)
        self.due = due

    async def wait(self, timeout: float) -> None:
        await super().wait(min(timeout, max(self.due() - time.time(), 1.0)))


class Wakers:
    """The requests waiting on each vault, woken by a write here or a change heard on the bus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting: dict[str, set[Waiter]] = {}
        self.closing = False

    def waiter(self, key: str, loop: asyncio.AbstractEventLoop) -> Waiter:
        waiter = Waiter(loop, lambda done: self._drop(key, done), lambda: self.closing)
        with self._lock:
            self._waiting.setdefault(key, set()).add(waiter)
        return waiter

    def _drop(self, key: str, waiter: Waiter) -> None:
        with self._lock:
            found = self._waiting.get(key)
            if found is not None:
                found.discard(waiter)
                if not found:
                    del self._waiting[key]

    def wake(self, key: str) -> None:
        with self._lock:
            found = list(self._waiting.get(key, ()))
        for waiter in found:
            waiter.wake()

    def wake_all(self) -> None:
        with self._lock:
            found = [waiter for waiters in self._waiting.values() for waiter in waiters]
        for waiter in found:
            waiter.wake()

    def close(self) -> None:
        self.closing = True
        self.wake_all()

    def count(self, key: str | None = None) -> int:
        with self._lock:
            if key is not None:
                return len(self._waiting.get(key, ()))
            return sum(len(waiters) for waiters in self._waiting.values())


def relevant(root: str, path: str, is_directory: bool) -> bool:
    if not path:
        return False
    relative = os.path.relpath(path, root)
    if relative == "." or relative.startswith(".."):
        return False
    key = relative.replace(os.sep, "/")
    if is_directory:
        return not any(segment.startswith(".") for segment in key.split("/"))
    return in_feed(key)


class FolderEvents:
    """The platform's file events for one vault folder, watched only while a request waits on it."""

    WATCHED = frozenset({"created", "modified", "deleted", "moved"})

    def __init__(self, root: Path, observer: Any) -> None:
        self.root = str(root)
        self.observer = observer
        self._lock = threading.Lock()
        self._waiting: set[Waiter] = set()
        self._watch: Any = None

    def waiter(self, loop: asyncio.AbstractEventLoop) -> Waiter:
        waiter = Waiter(loop, self._drop)
        with self._lock:
            self._waiting.add(waiter)
            if self._watch is None:
                self._watch = self.observer.schedule(self, self.root, recursive=True)
        return waiter

    def _drop(self, waiter: Waiter) -> None:
        with self._lock:
            self._waiting.discard(waiter)
            if not self._waiting and self._watch is not None:
                with contextlib.suppress(KeyError, OSError):
                    self.observer.unschedule(self._watch)
                self._watch = None

    def dispatch(self, event: Any) -> None:
        if event.event_type not in self.WATCHED or (event.is_directory and event.event_type == "modified"):
            return
        paths = (event.src_path, getattr(event, "dest_path", "") or "")
        if not any(relevant(self.root, str(path), event.is_directory) for path in paths):
            return
        with self._lock:
            found = list(self._waiting)
        for waiter in found:
            waiter.wake()


_folders: dict[str, FolderEvents] = {}
_folders_lock = threading.Lock()
_observer: Any = None


def folder_events(root: Path) -> FolderEvents:
    global _observer
    with _folders_lock:
        if _observer is None:
            from watchdog.observers import Observer

            _observer = Observer()
            _observer.daemon = True
            _observer.start()
        found = _folders.get(str(root))
        if found is None:
            found = _folders[str(root)] = FolderEvents(root, _observer)
        return found


class Feed(Protocol):
    def page(self, since: str | None, limit: int) -> Page: ...

    def waiter(self, loop: asyncio.AbstractEventLoop) -> Waiter: ...


class IndexFeed:
    """A vault without a server answers from its local index: a folder wakes on file events, a bucket on listings."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    @property
    def folder(self) -> Path | None:
        from mindin.backends.local import LocalBackend

        backend = self.vault.backend
        return backend.root if isinstance(backend, LocalBackend) else None

    def page(self, since: str | None, limit: int) -> Page:
        self.vault.sync(force=self.folder is not None)
        return self.vault.index.feed_page(since, limit)

    def waiter(self, loop: asyncio.AbstractEventLoop) -> Waiter:
        folder = self.folder
        if folder is not None:
            return folder_events(folder).waiter(loop)
        index = self.vault.index
        return PacedWaiter(loop, lambda: float(index.meta("last_sync") or 0.0) + LISTING_SECONDS)


class Recorder(Protocol):
    def record(self, vault_id: str, writes: Sequence[Written]) -> None: ...

    def page(self, vault_id: str, since: str | None, limit: int) -> Page: ...


class StoreFeed:
    """A hosted vault answers from the store's change table and wakes on this server's writes and the bus."""

    def __init__(self, recorder: Recorder, vault_id: str, wakers: Wakers) -> None:
        self.recorder = recorder
        self.vault_id = vault_id
        self.wakers = wakers

    def page(self, since: str | None, limit: int) -> Page:
        return self.recorder.page(self.vault_id, since, limit)

    def waiter(self, loop: asyncio.AbstractEventLoop) -> Waiter:
        return self.wakers.waiter(self.vault_id, loop)


def plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def entry(backend: VaultBackend, changed: Changed, include: str) -> dict:
    try:
        text, version = backend.get(changed.key)
    except NotFound:
        return {"key": changed.key, "action": "delete", "version": None, "note": None}
    except (VaultError, UnicodeDecodeError) as exc:
        unread = {"frontmatter": {}, "error": f"could not read: {exc}"}
        return {"key": changed.key, "action": "write", "version": changed.version, "note": unread}
    if changed.key == SCHEMA_KEY:
        return {"key": changed.key, "action": "schema", "version": version, "note": None}
    note: dict[str, Any]
    try:
        frontmatter, body = parse(text)
        note = {"frontmatter": plain(frontmatter)}
    except FrontmatterError as exc:
        body, note = text, {"frontmatter": {}, "error": str(exc)}
    if include == "body":
        note["body"] = body
    return {"key": changed.key, "action": "write", "version": version, "note": note}


def answer(vault: Vault, page: Page, include: str) -> dict:
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        changes = list(pool.map(lambda changed: entry(vault.backend, changed, include), page.changed))
    return {"cursor": page.cursor, "reset": page.reset, "more": page.more, "changes": changes}


def checked(limit: int | None, wait: float | None, include: str | None) -> tuple[int, float, str]:
    chosen = include or "frontmatter"
    if chosen not in INCLUDES:
        raise ValueError(f"include is one of {', '.join(INCLUDES)}, not {chosen!r}")
    return max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT)), max(0.0, float(wait or 0.0)), chosen


Opener = Callable[[], AbstractContextManager["Vault"]]


def read(opened: Opener, since: str | None, limit: int, include: str) -> dict:
    with opened() as vault:
        return answer(vault, vault.feed.page(since, limit), include)


def registered(opened: Opener, loop: asyncio.AbstractEventLoop) -> Waiter:
    with opened() as vault:
        return vault.feed.waiter(loop)


def settled(found: dict) -> bool:
    return bool(found["changes"]) or found["reset"]


async def changes(
    opened: Opener,
    since: str | None = None,
    limit: int | None = None,
    wait: float | None = None,
    include: str | None = None,
    longest: float = MAX_WAIT_SECONDS,
) -> dict:
    limit, wait, include = checked(limit, wait, include)
    wait = min(wait, longest)
    if wait <= 0:
        return await anyio.to_thread.run_sync(read, opened, since, limit, include)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    waiter = await anyio.to_thread.run_sync(registered, opened, loop)
    try:
        found = await anyio.to_thread.run_sync(read, opened, since, limit, include)
        while not settled(found) and not waiter.ending() and loop.time() < deadline:
            await waiter.wait(deadline - loop.time())
            found = await anyio.to_thread.run_sync(read, opened, since, limit, include)
        return found
    finally:
        waiter.close()


async def follow(
    opened: Opener, since: str | None = None, include: str | None = None, limit: int | None = None
) -> AsyncIterator[dict]:
    limit, _, include = checked(limit, 0, include)
    loop = asyncio.get_running_loop()
    waiter = await anyio.to_thread.run_sync(registered, opened, loop)
    try:
        cursor, first = since, True
        while True:
            found = await anyio.to_thread.run_sync(read, opened, cursor, limit, include)
            if first or settled(found):
                yield found
                first = False
            cursor = found["cursor"]
            if not found["more"]:
                await waiter.wait(FOLLOW_WAIT_SECONDS)
    finally:
        waiter.close()
