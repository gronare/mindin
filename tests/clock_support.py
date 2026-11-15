from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from time import time as world_time

PINNED = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

__all__ = ["PINNED", "PinnedTime", "committed_at", "environment", "pinned", "world_time"]


class PinnedTime:
    def __init__(self, real: Callable[[], float]) -> None:
        self.real = real
        self.offset = 0.0

    def __call__(self) -> float:
        return self.real() + self.offset

    def at(self, moment: datetime) -> None:
        self.offset = moment.timestamp() - self.real()


def committed_at(moment: float) -> dict[str, str]:
    stamp = f"@{int(moment)} +0000"
    return {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


@contextmanager
def environment(**values: str) -> Iterator[None]:
    before = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    time.tzset()
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        time.tzset()


@contextmanager
def pinned(moment: datetime = PINNED, name: str = "UTC") -> Iterator[PinnedTime]:
    clock = PinnedTime(world_time)
    clock.at(moment)
    with environment(TZ=name):
        time.time = clock
        try:
            yield clock
        finally:
            time.time = world_time
