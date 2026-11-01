from __future__ import annotations

import contextlib
import fcntl
import importlib
import json
import math
import os
import queue
import signal
import socket
import struct
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, BinaryIO

from notes_vault_mcp.config import env

HEADER = struct.Struct(">II")
LENGTH = struct.Struct(">I")
DEFAULT_BATCH = 16
DEFAULT_MAX_TOKENS = 256
MAX_DEFAULT_THREADS = 4
CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")
PROTOCOL = 1
QUERY, PASSAGES = 0, 1
POLL_SECONDS = 0.2
DEFAULT_LOADER = "notes_vault_mcp.embed_worker:fastembed_model"


def package_version() -> str:
    try:
        return metadata.version("notes-vault-mcp")
    except metadata.PackageNotFoundError:
        return ""


@contextlib.contextmanager
def machine_lock(path: Path) -> Iterator[None]:
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_exactly(stream: BinaryIO, size: int) -> bytes | None:
    data = b""
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            return None
        data += part
    return data


def read_message(stream: BinaryIO) -> bytes | None:
    header = read_exactly(stream, LENGTH.size)
    if header is None:
        return None
    return read_exactly(stream, LENGTH.unpack(header)[0])


def write_message(stream: BinaryIO, payload: bytes) -> None:
    stream.write(LENGTH.pack(len(payload)) + payload)
    stream.flush()


def vectors_reply(rows: Any) -> bytes:
    import numpy

    matrix = numpy.asarray(rows, dtype=numpy.float32)
    return HEADER.pack(*matrix.shape) + matrix.tobytes()


def error_reply(message: str) -> bytes:
    return HEADER.pack(0, 0) + message.encode("utf-8")


@dataclass(frozen=True)
class EmbedSettings:
    batch: int
    threads: int
    max_tokens: int


def cgroup_cores(cpu_max: Path = CGROUP_CPU_MAX) -> int | None:
    try:
        quota, period = cpu_max.read_text(encoding="utf-8").split()[:2]
        return max(1, math.ceil(int(quota) / int(period)))
    except (OSError, ValueError):
        return None


def usable_cores() -> int:
    cores = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    limit = cgroup_cores()
    return min(cores or 1, limit or cores or 1)


def _positive(name: str, default: int) -> int:
    try:
        value = int(env(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def embed_settings() -> EmbedSettings:
    return EmbedSettings(
        batch=_positive("VAULT_EMBED_BATCH", DEFAULT_BATCH),
        threads=_positive("VAULT_EMBED_THREADS", min(max(usable_cores() // 2, 1), MAX_DEFAULT_THREADS)),
        max_tokens=_positive("VAULT_EMBED_MAX_TOKENS", DEFAULT_MAX_TOKENS),
    )


def shortest_first(texts: list[str], size: int) -> Iterator[list[int]]:
    order = sorted(range(len(texts)), key=lambda position: len(texts[position]))
    for start in range(0, len(order), size):
        yield order[start : start + size]


class FastembedModel:
    def __init__(self, model: str, folder: str, settings: EmbedSettings | None = None) -> None:
        from fastembed import TextEmbedding

        self.settings = settings or embed_settings()
        self.embedding = TextEmbedding(
            model, cache_dir=folder, threads=self.settings.threads, enable_cpu_mem_arena=False
        )
        tokenizer = self.embedding.model.tokenizer
        truncation = tokenizer.truncation or {}
        if truncation.get("max_length", self.settings.max_tokens + 1) > self.settings.max_tokens:
            tokenizer.enable_truncation(
                max_length=self.settings.max_tokens, direction=truncation.get("direction", "right")
            )

    def embed(self, texts: list[str]) -> Any:
        rows: list[Any] = [None] * len(texts)
        for positions in shortest_first(texts, self.settings.batch):
            part = [texts[position] for position in positions]
            for position, row in zip(positions, self.embedding.embed(part, batch_size=len(part)), strict=True):
                rows[position] = row
        return rows


def fastembed_model(model: str, folder: str) -> FastembedModel:
    return FastembedModel(model, folder)


def load_function(spec: str) -> Any:
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


class Job:
    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.reply = b""
        self.done = threading.Event()


class Worker:
    def __init__(
        self, sock_path: Path, lock_path: Path, folder: str, model: str, idle_seconds: float, loader: str
    ) -> None:
        self.sock_path = sock_path
        self.lock_path = lock_path
        self.folder = folder
        self.model_name = model
        self.idle_seconds = idle_seconds
        self.loader = loader
        self.model: Any = None
        self.model_lock = threading.Lock()
        self.lanes: dict[int, queue.Queue[Job]] = {QUERY: queue.Queue(), PASSAGES: queue.Queue()}
        self.open_connections = 0
        self.state_lock = threading.Lock()
        self.last_used = time.monotonic()
        self.stopping = threading.Event()
        self.listener: socket.socket | None = None

    def hello(self) -> bytes:
        payload = {"model": self.model_name, "protocol": PROTOCOL, "version": package_version(), "pid": os.getpid()}
        return json.dumps(payload).encode("utf-8")

    def bind(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.sock_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            listener.bind(str(self.sock_path))
        finally:
            os.umask(old)
        listener.listen(64)
        listener.settimeout(POLL_SECONDS)
        self.listener = listener

    def serve(self) -> None:
        for lane in self.lanes.values():
            threading.Thread(target=self._embed_loop, args=(lane,), name="embed", daemon=True).start()
        while True:
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                if self.stopping.is_set() or self._idle():
                    break
                continue
            if self.stopping.is_set():
                connection.close()
                break
            with self.state_lock:
                self.open_connections += 1
            threading.Thread(target=self._answer, args=(connection,), daemon=True).start()
        self.listener.close()
        with machine_lock(self.lock_path), contextlib.suppress(FileNotFoundError):
            if not answers(self.sock_path):
                self.sock_path.unlink()

    def _idle(self) -> bool:
        with self.state_lock:
            return not self.open_connections and time.monotonic() - self.last_used >= self.idle_seconds

    def _answer(self, connection: socket.socket) -> None:
        try:
            with connection, connection.makefile("rwb") as stream:
                write_message(stream, self.hello())
                while (request := read_message(stream)) is not None:
                    message = json.loads(request)
                    if message.get("op") == "shutdown":
                        self.stopping.set()
                        write_message(stream, b"{}")
                        return
                    write_message(stream, self.submit(message))
        except OSError:
            pass
        finally:
            with self.state_lock:
                self.open_connections -= 1
                self.last_used = time.monotonic()

    def submit(self, message: dict) -> bytes:
        job = Job([str(text) for text in message.get("texts") or []])
        self.lanes[QUERY if message.get("op") == "query" else PASSAGES].put(job)
        job.done.wait()
        return job.reply

    def _loaded(self) -> Any:
        with self.model_lock:
            if self.model is None:
                self.model = load_function(self.loader)(self.model_name, self.folder)
            return self.model

    def _embed_loop(self, lane: queue.Queue[Job]) -> None:
        while True:
            job = lane.get()
            try:
                job.reply = vectors_reply(self._loaded().embed(job.texts))
            except Exception as exc:
                job.reply = error_reply(f"{type(exc).__name__}: {exc}")
            job.done.set()


def answers(sock_path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(sock_path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def daemonize(worker: Worker, pid_path: Path) -> None:
    ready_read, ready_write = os.pipe()
    if os.fork():
        os.close(ready_write)
        os._exit(0 if os.read(ready_read, 1) else 1)
    os.close(ready_read)
    os.setsid()
    worker.bind()
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    os.write(ready_write, b"1")
    os.close(ready_write)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    # stdout now points nowhere; prints from libraries go to the log on stderr instead.
    sys.stdout = sys.stderr
    signal.signal(signal.SIGTERM, lambda *_: worker.stopping.set())
    try:
        worker.serve()
    finally:
        with contextlib.suppress(OSError):
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
    os._exit(0)


def main() -> None:
    sock_path, lock_path, pid_path, folder, model, idle, loader = sys.argv[1:8]
    worker = Worker(Path(sock_path), Path(lock_path), folder, model, float(idle), loader)
    daemonize(worker, Path(pid_path))


if __name__ == "__main__":
    main()
