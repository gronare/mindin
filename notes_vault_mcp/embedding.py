from __future__ import annotations

import importlib.util
import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from notes_vault_mcp.config import env

log = logging.getLogger(__name__)

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MIN_SIMILARITY = 0.25
IDLE_SECONDS = 300.0
QUERY_WAIT_SECONDS = 3.0
WORKER_EXIT_SECONDS = 5.0
EXTRA_MODULES = ("fastembed", "numpy")
ENABLED_VALUES = {"1", "on", "true", "yes"}

_missing_reported = False
_shared: dict[Path, FastEmbedder] = {}
_shared_lock = threading.Lock()


class Embedder(Protocol):
    model_id: str
    min_similarity: float

    def passages(self, texts: Sequence[str]) -> Any: ...

    def query(self, text: str) -> Any | None: ...


class EmbeddingError(RuntimeError):
    pass


def semantic_requested() -> bool:
    return (env("VAULT_SEMANTIC") or "").strip().lower() in ENABLED_VALUES


def extra_installed() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in EXTRA_MODULES)


def _report_missing() -> None:
    global _missing_reported
    if _missing_reported:
        return
    _missing_reported = True
    log.warning(
        "notes-vault-mcp: VAULT_SEMANTIC is on but the semantic extra is not installed; searching by words only. "
        "Install notes-vault-mcp[semantic] to search by meaning."
    )


def configured_embedder(cache: Path) -> Embedder | None:
    if not semantic_requested():
        return None
    if not extra_installed():
        _report_missing()
        return None
    folder = cache / "models"
    with _shared_lock:
        if folder not in _shared:
            _shared[folder] = FastEmbedder(folder)
        return _shared[folder]


def _normalized(rows: Any) -> Any:
    import numpy

    matrix = numpy.asarray(rows, dtype=numpy.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class WorkerModel:
    def __init__(self, folder: Path, model: str = MODEL_NAME) -> None:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "notes_vault_mcp.embed_worker", str(folder), model],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def embed(self, texts: Sequence[str]) -> Any:
        import numpy

        from notes_vault_mcp.embed_worker import HEADER, read_message, write_message

        try:
            write_message(self.process.stdin, json.dumps(list(texts)).encode("utf-8"))
        except OSError as exc:
            raise EmbeddingError(f"the embedding worker is gone: {exc}") from exc
        reply = read_message(self.process.stdout)
        if reply is None:
            raise EmbeddingError(f"the embedding worker exited with {self.process.wait()}")
        rows, dims = HEADER.unpack_from(reply)
        if not rows:
            raise EmbeddingError(reply[HEADER.size :].decode("utf-8", "replace"))
        return numpy.frombuffer(reply, dtype=numpy.float32, offset=HEADER.size).reshape(rows, dims)

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=WORKER_EXIT_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


class FastEmbedder:
    model_id = MODEL_NAME
    min_similarity = MIN_SIMILARITY

    def __init__(
        self,
        folder: Path,
        idle_seconds: float = IDLE_SECONDS,
        load: Callable[[Path], Any] = WorkerModel,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.folder = folder
        self.idle_seconds = idle_seconds
        self._load = load
        self._clock = clock
        self._model: Any = None
        self._last_used = 0.0
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def passages(self, texts: Sequence[str]) -> Any:
        with self._lock:
            return self._embed(list(texts))

    def query(self, text: str) -> Any | None:
        if not self._lock.acquire(timeout=QUERY_WAIT_SECONDS):
            return None
        try:
            return self._embed([text])[0]
        finally:
            self._lock.release()

    def _embed(self, texts: list[str]) -> Any:
        if self._model is None:
            self._model = self._load(self.folder)
        try:
            rows = self._model.embed(texts)
        except Exception:
            self._drop()
            raise
        self._last_used = self._clock()
        self._arm()
        return _normalized(rows)

    def _arm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.idle_seconds, self.release_if_idle)
        self._timer.daemon = True
        self._timer.start()

    def _drop(self) -> None:
        model, self._model = self._model, None
        close = getattr(model, "close", None)
        if close is not None:
            close()

    def release_if_idle(self) -> bool:
        with self._lock:
            if self._model is None or self._clock() - self._last_used < self.idle_seconds:
                return False
            self._drop()
        return True
