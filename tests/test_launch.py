from __future__ import annotations

import json
from pathlib import Path

import pytest
from remote_support import Served, serve

from mindin import credentials, launch
from mindin.cli import main
from mindin.server import VERSION
from mindin.vault import SubjectVaults

URL = "https://vault.example.com"
PIN = "0.18.0"


class Recorder:
    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.asked: list[str] = []
        self.ran: list[list[str]] = []
        self.replaced: list[list[str]] = []

    def ask(self, url: str) -> str | None:
        self.asked.append(url)
        return self.answer

    def run(self, command: list[str]) -> int:
        self.ran.append(command)
        return 0

    def replace(self, program: str, argv: list[str]) -> None:
        self.replaced.append(argv[1:])


@pytest.fixture
def on_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_SERVER_URL", URL)
    monkeypatch.setattr(launch.shutil, "which", lambda name: f"/usr/bin/{name}")


def launched(recorder: Recorder, command: list[str], now: float = 1000.0) -> int:
    return launch.launch(command, PIN, recorder.run, recorder.ask, recorder.replace, lambda: now)


def test_without_a_server_the_pinned_version_runs_and_nothing_is_asked():
    recorder = Recorder("0.17.0")
    assert launched(recorder, ["hook", "session-start"]) == 0
    assert recorder.ran == [["hook", "session-start"]]
    assert recorder.asked == []
    assert recorder.replaced == []


def test_a_server_on_the_pinned_version_runs_it_here(on_server: None):
    recorder = Recorder(PIN)
    launched(recorder, ["serve"])
    assert recorder.ran == [["serve"]]
    assert recorder.replaced == []


def test_an_older_server_runs_its_own_version(on_server: None):
    recorder = Recorder("0.17.0")
    launched(recorder, ["hook", "stop"])
    assert recorder.ran == []
    assert recorder.replaced == [["mindin==0.17.0", "hook", "stop"]]


def test_a_newer_server_runs_the_pinned_version(on_server: None):
    recorder = Recorder("0.19.2")
    launched(recorder, ["serve"])
    assert recorder.ran == [["serve"]]
    assert recorder.replaced == []


def test_an_unreachable_server_runs_the_pinned_version(on_server: None):
    recorder = Recorder(None)
    launched(recorder, ["serve"])
    assert recorder.ran == [["serve"]]


def test_an_unreachable_server_runs_the_version_it_last_answered(on_server: None):
    launched(Recorder("0.17.0"), ["serve"], now=1000.0)
    recorder = Recorder(None)
    launched(recorder, ["serve"], now=1000.0 + launch.FRESH_SECONDS + 1)
    assert recorder.asked == [URL]
    assert recorder.replaced == [["mindin==0.17.0", "serve"]]


def test_the_answer_is_reused_while_fresh_then_asked_again(on_server: None):
    launched(Recorder("0.17.0"), ["serve"], now=1000.0)
    fresh = Recorder("0.18.0")
    launched(fresh, ["serve"], now=1000.0 + launch.FRESH_SECONDS - 1)
    assert fresh.asked == []
    assert fresh.replaced == [["mindin==0.17.0", "serve"]]
    stale = Recorder("0.18.0")
    launched(stale, ["serve"], now=1000.0 + launch.FRESH_SECONDS + 1)
    assert stale.asked == [URL]
    assert stale.ran == [["serve"]]
    assert json.loads(launch.cache_path().read_text())[URL]["version"] == "0.18.0"


def test_a_server_older_than_the_oldest_followed_version_says_so_in_one_line(on_server: None, capsys):
    assert launched(Recorder("0.13.0"), ["serve"]) == 1
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        f"mindin: {URL} runs mindin 0.13.0, older than {launch.OLDEST}, the oldest this plugin runs; upgrade the server"
    ]
    assert launched(Recorder("0.13.0"), ["hook", "session-start"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"vault: {URL} runs mindin 0.13.0, older than {launch.OLDEST}, the oldest this plugin runs; upgrade the server"
    ]


def test_without_uvx_an_older_server_is_refused_rather_than_run_newer(
    on_server: None, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    recorder = Recorder("0.17.0")
    assert launched(recorder, ["serve"]) == 1
    assert recorder.ran == [] and recorder.replaced == []
    assert "uvx" in capsys.readouterr().err


def test_an_answer_that_is_not_a_version_runs_the_pinned_version(on_server: None):
    recorder = Recorder("main")
    launched(recorder, ["serve"])
    assert recorder.ran == [["serve"]]
    assert not launch.cache_path().exists()


def test_the_probe_reads_the_version_a_signed_in_server_runs(vaults: SubjectVaults, storage: Path, tmp_path: Path):
    with serve(vaults, tmp_path) as served:
        assert launch.probe(served.url) is None
        signed_in(served)
        assert launch.probe(served.url) == VERSION


def test_the_launch_command_runs_the_rest_as_a_mindin_command(capsys):
    assert main(["launch", "about", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "mindin"


def signed_in(served: Served) -> None:
    credentials.save(served.url, {"key": served.key(), "scope": "vault:read", "name": "laptop"}, vault="notes")
