from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from mindin.extensions import Periodic

if TYPE_CHECKING:
    from mindin.hosted.accounts.store import HostedStore

logger = logging.getLogger(__name__)

Claim = Callable[[str, float], bool]


class Leases:
    def __init__(self, store: HostedStore, clock: Callable[[], float] = time.time, holder: str = "") -> None:
        self.store = store
        self.clock = clock
        self.holder = holder or secrets.token_urlsafe(12)

    def claim(self, name: str, seconds: float) -> bool:
        moment = int(self.clock())
        with self.store.connect() as connection:
            taken = connection.execute(
                "INSERT INTO periodic_leases(name, holder, held_until) VALUES(?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET holder = excluded.holder, held_until = excluded.held_until"
                " WHERE periodic_leases.held_until <= ?",
                (name, self.holder, moment + int(seconds), moment),
            ).rowcount
        return taken > 0


class PeriodicRunner:
    def __init__(self, work: Sequence[Periodic], claim: Claim | None = None) -> None:
        self.work = tuple(work)
        self.claim = claim
        self.stopping = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        self.stopping.clear()
        self.threads = [
            threading.Thread(target=self.loop, args=(item,), name=f"periodic-{item.name}", daemon=True)
            for item in self.work
        ]
        for thread in self.threads:
            thread.start()

    def tick(self, item: Periodic) -> bool:
        if self.claim is not None and not self.claim(item.name, item.seconds):
            return False
        item.run()
        return True

    def loop(self, item: Periodic) -> None:
        while not self.stopping.is_set():
            try:
                self.tick(item)
            except Exception:
                logger.exception("mindin: the periodic work %s failed", item.name)
            self.stopping.wait(item.seconds)

    def stop(self) -> None:
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=30)
        self.threads = []
