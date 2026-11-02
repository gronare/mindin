from __future__ import annotations

import json
import math
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy
import pytest
from fake_models import concept_vector

from notes_vault_mcp import embed_worker
from notes_vault_mcp.cli import main
from notes_vault_mcp.config import ConfigError
from notes_vault_mcp.embed_worker import LENGTH, Worker, read_message, write_message
from notes_vault_mcp.embedding import EmbeddingError, RemoteEmbedder, configured_embedder, embed_address
from notes_vault_mcp.vault import open_vault

LOADER = "fake_models:concept_model"
Start = Callable[..., tuple[str, int]]


@pytest.fixture
def start_worker() -> Iterator[Start]:
    running: list[tuple[Worker, threading.Thread]] = []

    def start(model: str = "fake-1", token: str | None = None) -> tuple[str, int]:
        worker = Worker(None, None, "", model, math.inf, LOADER, token)
        address = worker.bind_tcp("127.0.0.1", 0)
        thread = threading.Thread(target=worker.serve, daemon=True)
        thread.start()
        running.append((worker, thread))
        return address

    yield start
    for worker, thread in running:
        worker.stopping.set()
        thread.join(5)


def remote(tmp_path: Path, address: tuple[str, int], model: str = "fake-1", token: str | None = None):
    return RemoteEmbedder(tmp_path / "cache", address, model, token)


def test_a_remote_worker_embeds_and_no_local_worker_starts(start_worker: Start, tmp_path: Path):
    embedder = remote(tmp_path, start_worker())
    assert numpy.allclose(embedder.query("oauth token"), concept_vector("oauth token"))
    assert numpy.allclose(embedder.passages(["backup", "kebbie"]), [concept_vector("backup"), concept_vector("kebbie")])
    assert embedder.worker_pid is None
    assert not (tmp_path / "cache").exists()


def test_a_server_with_an_embed_url_indexes_through_the_remote_worker(
    start_worker: Start, vault_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    host, port = start_worker()
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("VAULT_SEMANTIC", "on")
    monkeypatch.setenv("VAULT_SEMANTIC_MODEL", "fake-1")
    monkeypatch.setenv("VAULT_EMBED_URL", f"tcp://{host}:{port}")
    vault = open_vault(vectors="wait")
    try:
        assert isinstance(vault.embedder, RemoteEmbedder)
        vault.index.sync(force=True)
        assert vault.index.vectors is not None and vault.index.vectors.count() > 0
    finally:
        vault.close()
    assert not list((tmp_path / "cache").glob("embed.*"))


def test_the_embed_url_must_be_tcp_with_a_host_and_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert embed_address("tcp://[::1]:7400") == ("::1", 7400)
    for wrong in ("http://models:7400", "tcp://models", "tcp://:7400", "models:7400"):
        with pytest.raises(ConfigError):
            embed_address(wrong)
    monkeypatch.setenv("VAULT_SEMANTIC", "on")
    monkeypatch.setenv("VAULT_EMBED_URL", "unix:///tmp/embed.sock")
    with pytest.raises(ConfigError):
        configured_embedder(tmp_path)


def test_a_listening_worker_refuses_to_shut_down_over_the_network(start_worker: Start, tmp_path: Path):
    address = start_worker()
    with socket.create_connection(address, timeout=5) as sock, sock.makefile("rwb") as stream:
        read_message(stream)
        write_message(stream, json.dumps({"op": "shutdown"}).encode())
        assert b"SIGTERM" in (read_message(stream) or b"")
    assert numpy.allclose(remote(tmp_path, address).query("backup"), concept_vector("backup"))


def test_another_model_is_refused_without_stopping_the_worker(start_worker: Start, tmp_path: Path):
    address = start_worker()
    with pytest.raises(EmbeddingError, match="fake-1"):
        remote(tmp_path, address, model="fake-2").passages(["backup"])
    assert numpy.allclose(remote(tmp_path, address).query("backup"), concept_vector("backup"))


def test_a_worker_with_a_token_answers_only_clients_that_carry_it(start_worker: Start, tmp_path: Path):
    address = start_worker(token="s3cret")
    with pytest.raises(EmbeddingError, match="VAULT_EMBED_TOKEN"):
        remote(tmp_path, address).query("backup")
    with pytest.raises(EmbeddingError, match="VAULT_EMBED_TOKEN"):
        remote(tmp_path, address, token="wrong").query("backup")
    assert numpy.allclose(remote(tmp_path, address, token="s3cret").query("backup"), concept_vector("backup"))


def test_an_oversized_message_closes_the_connection(start_worker: Start, tmp_path: Path):
    address = start_worker()
    with socket.create_connection(address, timeout=5) as sock, sock.makefile("rwb") as stream:
        read_message(stream)
        stream.write(LENGTH.pack(embed_worker.MAX_REMOTE_MESSAGE + 1))
        stream.flush()
        assert stream.read(1) == b""
    assert numpy.allclose(remote(tmp_path, address).query("backup"), concept_vector("backup"))


def test_listening_beyond_loopback_needs_a_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    monkeypatch.delenv("VAULT_EMBED_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="VAULT_EMBED_TOKEN"):
        embed_worker.listen("0.0.0.0", 0, "fake-1", "", None, LOADER)
    with pytest.raises(ConfigError, match="VAULT_EMBED_TOKEN"):
        embed_worker.listen("localhost", 0, "fake-1", "", None, LOADER)
    assert main(["embed-worker", "--listen", "10.0.0.5:7400"]) == 2
    assert "VAULT_EMBED_TOKEN" in capsys.readouterr().err
    assert main(["embed-worker", "--listen", "7400"]) == 2
    assert embed_worker.loopback("127.0.0.1") and embed_worker.loopback("::1")


def test_a_listening_worker_greets_without_its_pid(start_worker: Start):
    with socket.create_connection(start_worker(), timeout=5) as sock, sock.makefile("rwb") as stream:
        hello = json.loads(read_message(stream) or b"{}")
    assert hello["model"] == "fake-1" and "pid" not in hello


def test_before_the_token_a_worker_reads_only_a_small_message(start_worker: Start, tmp_path: Path):
    address = start_worker(token="orchard-key")
    with socket.create_connection(address, timeout=5) as sock, sock.makefile("rwb") as stream:
        read_message(stream)
        stream.write(LENGTH.pack(embed_worker.MAX_UNAUTHENTICATED_MESSAGE + 1))
        stream.flush()
        assert stream.read(1) == b""
    big = ["workshop frame " * 400] * 4
    assert len(json.dumps(big)) > embed_worker.MAX_UNAUTHENTICATED_MESSAGE
    rows = remote(tmp_path, address, token="orchard-key").passages(big)
    assert numpy.allclose(rows[0], concept_vector(big[0]))


def test_a_silent_connection_is_closed_after_the_connection_timeout(
    start_worker: Start, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(embed_worker, "CONNECTION_SECONDS", 0.2)
    address = start_worker(token="orchard-key")
    with socket.create_connection(address, timeout=5) as sock, sock.makefile("rwb") as stream:
        read_message(stream)
        assert stream.read(1) == b""
