from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from mindin.config import local_root, s3_settings
from mindin.hosted.accounts.store import HostedStore

PROBE_SECONDS = 5.0
HealthCheck = Callable[[], bool]
log = logging.getLogger(__name__)


class Cached:
    """A readiness check whose answer is reused for a few seconds, so a busy probe costs one look per window."""

    def __init__(
        self, check: HealthCheck, seconds: float = PROBE_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.check = check
        self.seconds = seconds
        self.clock = clock
        self.__name__ = getattr(check, "__name__", repr(check))
        self._lock = threading.Lock()
        self._answer: tuple[float, bool] | None = None

    def __call__(self) -> bool:
        with self._lock:
            if self._answer is not None and self.clock() - self._answer[0] < self.seconds:
                return self._answer[1]
        try:
            answer = bool(self.check())
        except Exception:
            log.exception("mindin: the readiness check %s failed", self.__name__)
            answer = False
        with self._lock:
            self._answer = (self.clock(), answer)
        return answer


def account_store_check(store: HostedStore) -> HealthCheck:
    def account_store() -> bool:
        with store.connect() as connection:
            return connection.execute("SELECT 1").fetchone() is not None

    return account_store


def storage_check() -> HealthCheck:
    root = local_root()
    settings = s3_settings()
    if root is None and settings is not None:
        from mindin.backends.s3 import S3Backend

        bucket = S3Backend(**settings)

        def storage() -> bool:
            bucket.client.list_objects_v2(Bucket=bucket.bucket, Prefix=bucket.prefix, MaxKeys=1)
            return True

        return storage

    def storage() -> bool:
        if root is None:
            return False
        root.mkdir(parents=True, exist_ok=True)
        return os.access(root, os.W_OK)

    return storage


def built_in_checks(store: HostedStore, clock: Callable[[], float] = time.monotonic) -> list[HealthCheck]:
    return [Cached(account_store_check(store), clock=clock), Cached(storage_check(), clock=clock)]
