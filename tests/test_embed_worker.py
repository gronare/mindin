from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy
import pytest
from fake_models import concept_vector

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
