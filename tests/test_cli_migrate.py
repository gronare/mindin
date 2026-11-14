from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mindin.cli import main
from mindin.hosted.schema import STEPS

STORE_TABLES_ONLY = 3


@pytest.fixture
def auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "auth"))
    return tmp_path / "auth"


def version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def stored_at(auth: Path, number: int) -> Path:
    auth.mkdir(parents=True)
    path = auth / "hosted.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        for step in STEPS[:number]:
            step(connection)
        connection.execute(f"PRAGMA user_version = {number}")
    return path


def test_migrate_makes_a_fresh_store_at_this_release(auth: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["migrate"]) == 0
    assert capsys.readouterr().out == f"store at version {len(STEPS)} (was 0)\n"
    assert version(auth / "hosted.sqlite") == len(STEPS)


def test_migrate_runs_the_steps_an_older_store_lacks(auth: Path, capsys: pytest.CaptureFixture[str]):
    path = stored_at(auth, STORE_TABLES_ONLY)
    assert main(["migrate"]) == 0
    assert capsys.readouterr().out == f"store at version {len(STEPS)} (was {STORE_TABLES_ONLY})\n"
    assert version(path) == len(STEPS)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'vault_feed'").fetchone()


def test_a_second_migrate_changes_nothing(auth: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["migrate"]) == 0
    path = auth / "hosted.sqlite"
    before = path.read_bytes()
    capsys.readouterr()
    assert main(["migrate"]) == 0
    assert capsys.readouterr().out == f"store at version {len(STEPS)} (was {len(STEPS)})\n"
    assert path.read_bytes() == before


def test_migrate_refuses_a_store_newer_than_this_release(auth: Path, capsys: pytest.CaptureFixture[str]):
    path = stored_at(auth, len(STEPS))
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {len(STEPS) + 1}")
    before = path.read_bytes()
    assert main(["migrate"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"at version {len(STEPS) + 1}, and this mindin knows versions up to {len(STEPS)}" in captured.err
    assert path.read_bytes() == before


def test_migrate_reports_a_failing_step_and_keeps_the_store(
    auth: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    path = stored_at(auth, STORE_TABLES_ONLY)

    def breaks(connection: sqlite3.Connection) -> None:
        raise RuntimeError("step broke")

    monkeypatch.setattr("mindin.hosted.accounts.store.STEPS", (*STEPS, breaks))
    assert main(["migrate"]) == 1
    assert "step broke" in capsys.readouterr().err
    assert version(path) == STORE_TABLES_ONLY
