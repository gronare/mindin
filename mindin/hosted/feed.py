from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from mindin.backends import VaultBackend
from mindin.backends.sealed import SealedBackend
from mindin.changes import Announcing
from mindin.feed import Changed, Page, Written, in_feed, page_of
from mindin.hosted.accounts.store import HostedStore

KEEP_ROWS = 10_000
KEEP_SECONDS = 30 * 86400
PRUNE_EVERY = 100

log = logging.getLogger(__name__)


class ChangeFeed:
    """Every hosted vault's writes in the order they landed, numbered per vault, paths sealed under the vault's key."""

    def __init__(
        self,
        store: HostedStore,
        names: Callable[[str], SealedBackend],
        clock: Callable[[], float] = time.time,
        keep_rows: int = KEEP_ROWS,
        keep_seconds: float = KEEP_SECONDS,
    ) -> None:
        self.store = store
        self.names = names
        self.clock = clock
        self.keep_rows = keep_rows
        self.keep_seconds = keep_seconds
        self._lost: set[str] = set()
        self._lost_lock = threading.Lock()

    def record(self, vault_id: str, writes: Sequence[Written]) -> None:
        if not writes:
            return
        for attempt in (1, 2):
            try:
                self.reset_if_lost(vault_id)
                self.write_rows(vault_id, writes)
                return
            except Exception:
                log.exception("mindin: could not record a write in the change feed (attempt %d)", attempt)
        with self._lost_lock:
            self._lost.add(vault_id)
        try:
            self.reset_if_lost(vault_id)
        except Exception:
            log.exception("mindin: could not mark the change feed for a reset; the next page marks it")

    # Pruning through head + 1, not head, is what makes a cursor taken at the head before the lost write answer reset.
    def reset_if_lost(self, vault_id: str) -> None:
        with self._lost_lock:
            if vault_id not in self._lost:
                return
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO vault_feed_heads(vault_id, seq, pruned_through) VALUES(?, 1, 1)"
                " ON CONFLICT (vault_id) DO UPDATE"
                " SET seq = vault_feed_heads.seq + 1, pruned_through = vault_feed_heads.seq + 1",
                (vault_id,),
            )
        with self._lost_lock:
            self._lost.discard(vault_id)

    # The head row's lock holds a vault's writers in line, so a sequence number commits only after every lower one.
    def write_rows(self, vault_id: str, writes: Sequence[Written]) -> None:
        sealer = self.names(vault_id)
        at = int(self.clock())
        with self.store.connect() as connection:
            for written in writes:
                seq = connection.execute(
                    "INSERT INTO vault_feed_heads(vault_id, seq, pruned_through) VALUES(?, 1, 0)"
                    " ON CONFLICT (vault_id) DO UPDATE SET seq = vault_feed_heads.seq + 1 RETURNING seq",
                    (vault_id,),
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO vault_feed(vault_id, seq, path, action, version, at) VALUES(?, ?, ?, ?, ?, ?)",
                    (vault_id, seq, sealer.object_key(written.key), written.action, written.version, at),
                )
                if seq % PRUNE_EVERY == 0:
                    self.prune_in(connection, vault_id)

    def prune(self, vault_id: str) -> int:
        with self.store.connect() as connection:
            return self.prune_in(connection, vault_id)

    def prune_in(self, connection: Any, vault_id: str) -> int:
        crowded = connection.execute(
            "SELECT seq FROM vault_feed WHERE vault_id = ? ORDER BY seq DESC LIMIT 1 OFFSET ?",
            (vault_id, self.keep_rows),
        ).fetchone()
        aged = connection.execute(
            "SELECT MAX(seq) FROM vault_feed WHERE vault_id = ? AND at < ?",
            (vault_id, int(self.clock() - self.keep_seconds)),
        ).fetchone()
        cut = max(crowded[0] if crowded else 0, aged[0] or 0)
        if not cut:
            return 0
        connection.execute("DELETE FROM vault_feed WHERE vault_id = ? AND seq <= ?", (vault_id, cut))
        connection.execute(
            "UPDATE vault_feed_heads SET pruned_through = ? WHERE vault_id = ? AND pruned_through < ?",
            (cut, vault_id, cut),
        )
        return cut

    def page(self, vault_id: str, since: str | None, limit: int) -> Page:
        self.reset_if_lost(vault_id)
        with self.store.connect() as connection:
            head_row = connection.execute(
                "SELECT seq, pruned_through FROM vault_feed_heads WHERE vault_id = ?", (vault_id,)
            ).fetchone()
            head, pruned = (head_row[0], head_row[1]) if head_row else (0, 0)
            after = int(since) if since is not None and since.isdigit() else None
            if after is None or after > head or after < pruned:
                return Page(str(head), True, [])
            if after == head:
                return Page(str(head), False, [])
            rows = connection.execute(
                "SELECT path, seq, action, version FROM ("
                " SELECT path, seq, action, version, ROW_NUMBER() OVER (PARTITION BY path ORDER BY seq DESC) AS nth"
                " FROM vault_feed WHERE vault_id = ? AND seq > ?) latest WHERE nth = 1 ORDER BY seq LIMIT ?",
                (vault_id, after, limit + 1),
            ).fetchall()
        sealer = self.names(vault_id)
        changed = [Changed(key, row[2], row[3], row[1]) for row in rows if (key := sealer.path_of(row[0])) is not None]
        return page_of(changed, limit, head, str)


def recording(feed: ChangeFeed, vault_id: str, backend: VaultBackend) -> Announcing:
    return Announcing(backend, lambda writes: feed.record(vault_id, [found for found in writes if in_feed(found.key)]))
