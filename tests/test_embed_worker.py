from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import numpy
import pytest
from fake_models import concept_vector

from notes_vault_mcp import embed_worker
from notes_vault_mcp.embedding import SharedEmbedder

TESTS = Path(__file__).parent
LOADER = "fake_models:concept_model"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until(condition, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(TESTS), os.environ.get("PYTHONPATH", "")]))
    folder = tmp_path / "cache"
    folder.mkdir()
    return folder


@pytest.fixture
def make(cache: Path):
    made: list[SharedEmbedder] = []

    def build(model: str = "fake-1", idle_seconds: float = 60, query_wait: float = 3.0) -> SharedEmbedder:
        embedder = SharedEmbedder(cache, model, idle_seconds=idle_seconds, loader=LOADER, query_wait=query_wait)
        made.append(embedder)
        return embedder

    yield build
    pids = {embedder.worker_pid for embedder in made if embedder.worker_pid}
    with contextlib.suppress(OSError, ValueError):
        pids.add(int((cache / "embed.pid").read_text()))
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


def test_two_clients_share_one_worker(make):
    first, second = make(), make()
    assert numpy.allclose(first.query("oauth token"), concept_vector("oauth token"))
    assert numpy.allclose(second.passages(["backup", "kebbie"]), [concept_vector("backup"), concept_vector("kebbie")])
    assert first.worker_pid == second.worker_pid
    assert alive(first.worker_pid)


START_SCRIPT = """
import sys
from pathlib import Path
from notes_vault_mcp.embedding import SharedEmbedder
embedder = SharedEmbedder(Path(sys.argv[1]), "fake-1", loader="fake_models:concept_model")
embedder.query("oauth")
print(embedder.worker_pid)
"""


def test_processes_starting_at_once_get_one_worker(make, cache: Path):
    starters = [
        subprocess.Popen([sys.executable, "-c", START_SCRIPT, str(cache)], stdout=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    pids = {int(starter.communicate(timeout=60)[0]) for starter in starters}
    assert len(pids) == 1
    here = make()
    here.query("oauth")
    assert {here.worker_pid} == pids


def test_the_worker_outlives_the_process_that_started_it(make, cache: Path):
    done = subprocess.run(
        [sys.executable, "-c", START_SCRIPT, str(cache)], capture_output=True, text=True, check=True, timeout=60
    )
    started = int(done.stdout)
    assert alive(started)
    later = make()
    later.query("oauth")
    assert later.worker_pid == started


def test_the_worker_exits_when_idle_and_the_next_need_starts_another(make):
    embedder = make(idle_seconds=0.5)
    embedder.query("oauth")
    first = embedder.worker_pid
    assert wait_until(lambda: not alive(first))
    assert not embedder.sock_path.exists()
    embedder.query("oauth")
    assert embedder.worker_pid != first


def test_a_crashed_worker_is_restarted_by_the_next_client(make):
    embedder = make()
    embedder.query("oauth")
    crashed = embedder.worker_pid
    os.kill(crashed, signal.SIGKILL)
    assert wait_until(lambda: not alive(crashed))
    assert embedder.sock_path.exists()
    assert numpy.allclose(embedder.query("backup"), concept_vector("backup"))
    assert embedder.worker_pid != crashed


def test_a_client_for_another_model_restarts_the_worker(make):
    first = make("fake-1")
    first.query("oauth")
    old = first.worker_pid
    second = make("fake-2")
    second.query("oauth")
    assert second.worker_pid != old
    assert wait_until(lambda: not alive(old))


OLD_WORKER = """
from notes_vault_mcp import embed_worker
embed_worker.package_version = lambda: "0.0.0-old"
embed_worker.main()
"""


def test_a_worker_of_another_version_is_restarted(make, cache: Path):
    embedder = make()
    command = [str(embedder.sock_path), str(embedder.lock_path), str(embedder.pid_path), str(cache), "fake-1", "60"]
    subprocess.run([sys.executable, "-c", OLD_WORKER, *command, LOADER], check=True, timeout=30)
    old = int(embedder.pid_path.read_text())
    embedder.query("oauth")
    assert embedder.worker_pid != old
    assert wait_until(lambda: not alive(old))


def test_a_query_behind_a_slow_embed_goes_by_words(make, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_EMBED_DELAY", "2")
    embedder = make(query_wait=0.3)
    embedder.query("quick")
    bulk = threading.Thread(target=embedder.passages, args=(["slow", "texts"],))
    bulk.start()
    time.sleep(0.2)
    started = time.monotonic()
    assert embedder.query("oauth") is None
    assert time.monotonic() - started < 1.5
    bulk.join()


def test_a_query_jumps_ahead_of_queued_embeds(make, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_EMBED_DELAY", "1")
    embedder = make(query_wait=5)
    embedder.query("quick")
    bulks = [threading.Thread(target=embedder.passages, args=([f"slow {n}"],)) for n in range(3)]
    for bulk in bulks:
        bulk.start()
        time.sleep(0.05)
    started = time.monotonic()
    assert embedder.query("quick") is not None
    assert time.monotonic() - started < 2.0
    for bulk in bulks:
        bulk.join()


def test_a_query_is_answered_while_a_slow_embed_runs(make, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_EMBED_DELAY", "3")
    embedder = make(query_wait=1.0)
    embedder.query("quick")
    bulk = threading.Thread(target=embedder.passages, args=(["slow", "texts"],))
    bulk.start()
    time.sleep(0.3)
    assert numpy.allclose(embedder.query("quick"), concept_vector("quick"))
    bulk.join()


class FakeTokenizer:
    def __init__(self) -> None:
        self.truncation = {"max_length": 512, "direction": "right"}

    def enable_truncation(self, max_length: int, direction: str) -> None:
        self.truncation = {"max_length": max_length, "direction": direction}


class FakeTextEmbedding:
    made: list[FakeTextEmbedding] = []

    def __init__(self, model: str, **options) -> None:
        self.options = options
        self.batches: list[list[str]] = []
        self.model = types.SimpleNamespace(tokenizer=FakeTokenizer())
        FakeTextEmbedding.made.append(self)

    def embed(self, texts: list[str], batch_size: int):
        assert len(texts) <= batch_size
        self.batches.append(texts)
        return [numpy.array([float(len(text))]) for text in texts]


@pytest.fixture
def fake_fastembed(monkeypatch: pytest.MonkeyPatch):
    FakeTextEmbedding.made.clear()
    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeTextEmbedding))
    return FakeTextEmbedding.made


def test_the_embedding_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_EMBED_BATCH", "4")
    monkeypatch.setenv("VAULT_EMBED_THREADS", "3")
    monkeypatch.setenv("VAULT_EMBED_MAX_TOKENS", "128")
    assert embed_worker.embed_settings() == embed_worker.EmbedSettings(batch=4, threads=3, max_tokens=128)


def test_threads_default_to_half_the_usable_cores_capped(monkeypatch: pytest.MonkeyPatch):
    for name in ("VAULT_EMBED_BATCH", "VAULT_EMBED_THREADS", "VAULT_EMBED_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(embed_worker, "usable_cores", lambda: 64)
    assert embed_worker.embed_settings() == embed_worker.EmbedSettings(
        batch=embed_worker.DEFAULT_BATCH, threads=embed_worker.MAX_DEFAULT_THREADS, max_tokens=256
    )
    monkeypatch.setattr(embed_worker, "usable_cores", lambda: 4)
    assert embed_worker.embed_settings().threads == 2
    monkeypatch.setattr(embed_worker, "usable_cores", lambda: 1)
    assert embed_worker.embed_settings().threads == 1


@pytest.mark.parametrize(
    ("cpu_max", "expected"), [("250000 100000\n", 3), ("50000 100000\n", 1), ("max 100000\n", None)]
)
def test_a_container_cpu_limit_counts_as_the_cores(tmp_path: Path, cpu_max: str, expected: int | None):
    path = tmp_path / "cpu.max"
    path.write_text(cpu_max, encoding="utf-8")
    assert embed_worker.cgroup_cores(path) == expected


def test_an_unusable_setting_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_EMBED_BATCH", "many")
    monkeypatch.setenv("VAULT_EMBED_MAX_TOKENS", "0")
    settings = embed_worker.embed_settings()
    assert settings.batch == embed_worker.DEFAULT_BATCH and settings.max_tokens == embed_worker.DEFAULT_MAX_TOKENS


def test_the_settings_reach_fastembed(fake_fastembed, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_EMBED_THREADS", "2")
    monkeypatch.setenv("VAULT_EMBED_MAX_TOKENS", "200")
    embed_worker.fastembed_model("some-model", "/models")
    (made,) = fake_fastembed
    assert made.options == {"cache_dir": "/models", "threads": 2, "enable_cpu_mem_arena": False}
    assert made.model.tokenizer.truncation == {"max_length": 200, "direction": "right"}


def test_passages_go_to_the_model_in_batches_within_the_cap(fake_fastembed):
    settings = embed_worker.EmbedSettings(batch=8, threads=1, max_tokens=256)
    model = embed_worker.FastembedModel("some-model", "/models", settings)
    texts = ["x" * ((position * 7) % 23 + 1) for position in range(37)]
    rows = model.embed(texts)
    batches = fake_fastembed[0].batches
    assert len(batches) == 5 and max(len(batch) for batch in batches) == 8
    assert [float(row[0]) for row in rows] == [float(len(text)) for text in texts]
