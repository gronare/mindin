from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from notes_vault_mcp.config import ConfigError, env

log = logging.getLogger(__name__)

MINILM = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MPNET = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
MIN_SIMILARITY = {MINILM: 0.25, MPNET: 0.35}
UNKNOWN_MODEL_SIMILARITY = 0.3
IDLE_SECONDS = 300.0
WARM_SECONDS = IDLE_SECONDS / 2
WARM_TEXT = "warm"
QUERY_WAIT_SECONDS = 3.0
PASSAGES_WAIT_SECONDS = 900.0
HELLO_WAIT_SECONDS = 5.0
SPAWN_WAIT_SECONDS = 30.0
STOP_WAIT_SECONDS = 10.0
SOCKET_PATH_MAX = 100
EXTRA_MODULES = ("fastembed", "numpy")
ENABLED_VALUES = {"1", "on", "true", "yes"}

_reported: set[str] = set()
_shared: dict[tuple[str, str], SharedEmbedder] = {}
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


def model_name() -> str:
    return (env("VAULT_SEMANTIC_MODEL") or "").strip() or MPNET


def extra_installed() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in EXTRA_MODULES)


def posix_sockets() -> bool:
    return hasattr(socket, "AF_UNIX") and importlib.util.find_spec("fcntl") is not None


def _report_once(key: str, message: str) -> None:
    if key not in _reported:
        _reported.add(key)
        log.warning(message)


def embed_address(url: str) -> tuple[str, int]:
    parts = urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        port = None
    if parts.scheme != "tcp" or not parts.hostname or port is None:
        raise ConfigError(f"notes-vault-mcp: VAULT_EMBED_URL takes tcp://host:port, not {url!r}")
    return parts.hostname, port


def configured_embedder(cache: Path) -> Embedder | None:
    if not semantic_requested():
        return None
    url = (env("VAULT_EMBED_URL") or "").strip()
    if url:
        return remote_embedder(cache, url)
    if not extra_installed():
        _report_once(
            "extra",
            "notes-vault-mcp: VAULT_SEMANTIC is on but the semantic extra is not installed; searching by words "
            "only. Install notes-vault-mcp[semantic] to search by meaning.",
        )
        return None
    if not posix_sockets():
        _report_once("posix", "notes-vault-mcp: search by meaning needs Unix sockets; searching by words only.")
        return None
    model = model_name()
    with _shared_lock:
        if (str(cache), model) not in _shared:
            _shared[(str(cache), model)] = SharedEmbedder(cache, model)
        return _shared[(str(cache), model)]


def remote_embedder(cache: Path, url: str) -> Embedder | None:
    address = embed_address(url)
    if importlib.util.find_spec("numpy") is None:
        _report_once(
            "numpy",
            "notes-vault-mcp: VAULT_EMBED_URL is set but numpy is not installed; searching by words only. "
            "Install notes-vault-mcp[semantic] to search by meaning.",
        )
        return None
    model = model_name()
    with _shared_lock:
        if (url, model) not in _shared:
            _shared[(url, model)] = RemoteEmbedder(cache, address, model, env("VAULT_EMBED_TOKEN"))
        return _shared[(url, model)]


def keep_warm(embedder: Embedder | None) -> None:
    if isinstance(embedder, SharedEmbedder):
        embedder.keep_warm()


def socket_path(cache: Path) -> Path:
    path = cache / "embed.sock"
    if len(str(path).encode("utf-8")) <= SOCKET_PATH_MAX:
        return path
    digest = hashlib.sha1(str(cache).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"notes-vault-mcp-{digest}.sock"


def _normalized(rows: Any) -> Any:
    import numpy

    matrix = numpy.asarray(rows, dtype=numpy.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = numpy.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


class Connection:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.stream = sock.makefile("rwb")
        self.hello: dict = {}

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.stream.close()
        with contextlib.suppress(OSError):
            self.sock.close()


class SharedEmbedder:
    def __init__(
        self,
        cache: Path,
        model: str = MPNET,
        idle_seconds: float = IDLE_SECONDS,
        loader: str | None = None,
        query_wait: float = QUERY_WAIT_SECONDS,
    ) -> None:
        from notes_vault_mcp.embed_worker import DEFAULT_LOADER

        self.model_id = model
        self.min_similarity = MIN_SIMILARITY.get(model, UNKNOWN_MODEL_SIMILARITY)
        self.cache = cache
        self.sock_path = socket_path(cache)
        self.lock_path = cache / "embed.lock"
        self.pid_path = cache / "embed.pid"
        self.log_path = cache / "embed.log"
        self.folder = cache / "models"
        self.idle_seconds = idle_seconds
        self.loader = loader or DEFAULT_LOADER
        self.query_wait = query_wait
        self.worker_pid: int | None = None
        self.warm_stop = threading.Event()

    def keep_warm(self, every: float = WARM_SECONDS) -> None:
        def warm() -> None:
            while not self.warm_stop.is_set():
                with contextlib.suppress(Exception):
                    self.query(WARM_TEXT)
                self.warm_stop.wait(every)

        threading.Thread(target=warm, name="vault-embed-warm", daemon=True).start()

    def passages(self, texts: Sequence[str]) -> Any:
        return _normalized(self._request("passages", list(texts), PASSAGES_WAIT_SECONDS))

    def query(self, text: str) -> Any | None:
        try:
            rows = self._request("query", [text], self.query_wait)
        except TimeoutError:
            return None
        return _normalized(rows)[0]

    def _request(self, op: str, texts: list[str], wait: float) -> Any:
        from notes_vault_mcp.embed_worker import read_message, write_message

        for _ in range(2):
            connection = self._connect()
            try:
                connection.sock.settimeout(wait)
                write_message(connection.stream, json.dumps(self._message(op, texts)).encode("utf-8"))
                reply = read_message(connection.stream)
            except TimeoutError:
                raise
            except OSError:
                reply = None
            finally:
                connection.close()
            if reply is not None:
                return _rows(reply)
        raise EmbeddingError("the embedding process went away before it answered")

    def _message(self, op: str, texts: list[str]) -> dict:
        return {"op": op, "texts": texts}

    def _matches(self, hello: dict) -> bool:
        from notes_vault_mcp.embed_worker import PROTOCOL, package_version

        expected = {"model": self.model_id, "protocol": PROTOCOL, "version": package_version()}
        return all(hello.get(key) == value for key, value in expected.items())

    def _open(self) -> Connection | None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(HELLO_WAIT_SECONDS)
        try:
            sock.connect(str(self.sock_path))
        except OSError:
            sock.close()
            return None
        return greeted(sock)

    def _connect(self) -> Connection:
        connection = self._open()
        if connection is None or not self._matches(connection.hello):
            if connection is not None:
                connection.close()
            connection = self._start()
        self.worker_pid = connection.hello.get("pid")
        return connection

    def _start(self) -> Connection:
        from notes_vault_mcp.embed_worker import machine_lock

        self.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        with machine_lock(self.lock_path):
            connection = self._open()
            if connection is not None and not self._matches(connection.hello):
                self._stop(connection)
                connection = None
            if connection is None:
                self._spawn()
                connection = self._open()
        if connection is None or not self._matches(connection.hello):
            raise EmbeddingError(f"the embedding process did not start; its log is {self.log_path}")
        return connection

    def _stop(self, connection: Connection) -> None:
        from notes_vault_mcp.embed_worker import answers, read_message, write_message

        with contextlib.suppress(OSError):
            write_message(connection.stream, json.dumps({"op": "shutdown"}).encode("utf-8"))
            read_message(connection.stream)
        connection.close()
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        while answers(self.sock_path) and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = connection.hello.get("pid")
        if answers(self.sock_path) and isinstance(pid, int):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)

    def _spawn(self) -> None:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "notes_vault_mcp.embed_worker",
            str(self.sock_path),
            str(self.lock_path),
            str(self.pid_path),
            str(self.folder),
            self.model_id,
            str(self.idle_seconds),
            self.loader,
        ]
        with open(self.log_path, "ab") as log_file:
            subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                timeout=SPAWN_WAIT_SECONDS,
                check=False,
            )


class RemoteEmbedder(SharedEmbedder):
    def __init__(
        self,
        cache: Path,
        address: tuple[str, int],
        model: str = MPNET,
        token: str | None = None,
        query_wait: float = QUERY_WAIT_SECONDS,
    ) -> None:
        super().__init__(cache, model, query_wait=query_wait)
        self.address = address
        self.token = token

    @property
    def url(self) -> str:
        host, port = self.address
        return f"tcp://[{host}]:{port}" if ":" in host else f"tcp://{host}:{port}"

    def _matches(self, hello: dict) -> bool:
        from notes_vault_mcp.embed_worker import PROTOCOL

        return hello.get("model") == self.model_id and hello.get("protocol") == PROTOCOL

    def _open(self) -> Connection | None:
        try:
            sock = socket.create_connection(self.address, timeout=HELLO_WAIT_SECONDS)
        except OSError:
            return None
        return greeted(sock)

    def _connect(self) -> Connection:
        connection = self._open()
        if connection is None:
            raise EmbeddingError(f"no embedding worker answers at {self.url}")
        if not self._matches(connection.hello):
            connection.close()
            raise EmbeddingError(
                f"the embedding worker at {self.url} runs {connection.hello.get('model')!r}, protocol "
                f"{connection.hello.get('protocol')!r}; this server needs {self.model_id!r}"
            )
        if self.token:
            self._authenticate(connection)
        return connection

    def _authenticate(self, connection: Connection) -> None:
        from notes_vault_mcp.embed_worker import read_message, write_message

        try:
            write_message(connection.stream, json.dumps({"op": "auth", "token": self.token}).encode("utf-8"))
            reply = read_message(connection.stream)
        except OSError:
            reply = None
        if reply != b"{}":
            connection.close()
            refused = _refusal(reply) if reply else "the embedding worker closed the connection"
            raise EmbeddingError(f"the embedding worker at {self.url} refused this server: {refused}")


def greeted(sock: socket.socket) -> Connection | None:
    from notes_vault_mcp.embed_worker import read_message

    connection = Connection(sock)
    try:
        raw = read_message(connection.stream)
        connection.hello = json.loads(raw) if raw else {}
    except (OSError, ValueError):
        raw = None
    if not raw:
        connection.close()
        return None
    return connection


def _refusal(reply: bytes) -> str:
    from notes_vault_mcp.embed_worker import HEADER

    return reply[HEADER.size :].decode("utf-8", "replace")


def _rows(reply: bytes) -> Any:
    import numpy

    from notes_vault_mcp.embed_worker import HEADER, VECTOR_TYPE

    rows, dims = HEADER.unpack_from(reply)
    if not rows:
        raise EmbeddingError(reply[HEADER.size :].decode("utf-8", "replace"))
    matrix = numpy.frombuffer(reply, dtype=VECTOR_TYPE, offset=HEADER.size).reshape(rows, dims)
    return matrix.astype(numpy.float32, copy=False)
