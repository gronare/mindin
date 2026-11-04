from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindin import notes, provision, tasks
from mindin.backends.local import LocalBackend
from mindin.cli import main
from mindin.snapshot import build_snapshot, log_lines
from mindin.vault import Vault, open_vault

LOG = "Log/orchard-log.md"


def run_json(capsys: pytest.CaptureFixture, *argv: str) -> dict:
    assert main(list(argv)) == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


def rows(snapshot: dict) -> dict[str, dict]:
    return {row["key"]: row for row in snapshot["notes"]}


def test_the_snapshot_carries_the_board_and_leaves_out_archive_logs_and_references(vault: Vault):
    found = build_snapshot(vault)
    board = rows(found)
    assert set(board) == {
        "Areas/orchard.md",
        "Areas/workshop.md",
        "Projects/orchard-draft.md",
        "Projects/orchard-fresh.md",
        "Projects/workshop-stale.md",
    }
    assert board["Areas/orchard.md"]["role"] == "system" and board["Projects/orchard-fresh.md"]["role"] == "task"
    assert board["Projects/orchard-fresh.md"]["paths"] == ["~/projects/orchard/app/javascript"]
    assert board["Projects/orchard-fresh.md"]["claim"] is None
    assert found["layout"] == "default" and found["log_key_template"] == "Log/{repo}-log.md"
    assert found["synced_at"] and found["etag"]
    assert found["openings"]["Projects/orchard-fresh.md"].startswith("Bokningswidget ska renderas i checkout")
    assert "Projects/workshop-stale.md" in found["openings"]


def test_log_lines_are_parsed_with_their_links(vault: Vault):
    logged = build_snapshot(vault)["logs"]
    assert list(logged) == [LOG]
    assert logged[LOG][-1] == {
        "date": "2026-08-15",
        "text": "Release-grinden mäter A/B | commits: e4f5a6b | [[orchard]]",
        "links": ["orchard"],
    }


def test_only_the_last_fifty_dated_lines_of_a_log_are_kept():
    body = "Intro\n\n" + "\n".join(f"- [2026-01-01] line {n} [[a|A]] [[b#x]] [[a]]" for n in range(60))
    parsed = log_lines(body)
    assert len(parsed) == 50 and parsed[0]["text"].startswith("line 10")
    assert parsed[0]["links"] == ["a", "b"]


def test_a_matching_etag_answers_unchanged(vault: Vault):
    etag = build_snapshot(vault)["etag"]
    assert build_snapshot(vault, since=etag) == {"unchanged": True, "etag": etag}
    assert "notes" in build_snapshot(vault, since="stale")


def test_the_etag_changes_after_a_claim_and_a_log_line(vault: Vault):
    first = build_snapshot(vault)["etag"]
    tasks.claim_note(vault, "Projects/orchard-fresh.md", "devbox", "t1", repo="orchard")
    claimed = build_snapshot(vault)
    assert claimed["etag"] != first
    assert rows(claimed)["Projects/orchard-fresh.md"]["claim"]["machine"] == "devbox"
    notes.log_append(vault, "orchard", "Byggde lunden", commits=("abc1234",))
    logged = build_snapshot(vault)
    assert logged["etag"] != claimed["etag"]
    assert logged["logs"][LOG][-1]["text"].startswith("Byggde lunden")


def test_the_etag_holds_across_a_fresh_index(vault: Vault, tmp_path: Path):
    again = open_vault(cache=tmp_path / "other-cache")
    try:
        again.index.sync(force=True)
        assert build_snapshot(again)["etag"] == build_snapshot(vault)["etag"]
    finally:
        again.close()


def test_a_layout_without_a_log_folder_has_no_log_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    folder = tmp_path / "simple"
    provision.initialize(LocalBackend(folder), preset="simple")
    monkeypatch.setenv("VAULT_PATH", str(folder))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    found = run_json(capsys, "snapshot", "--json")
    assert found["layout"] == "simple" and found["log_key_template"] is None and found["logs"] == {}


def test_the_cli_snapshot_and_since(vault: Vault, capsys):
    found = run_json(capsys, "snapshot", "--json")
    assert set(rows(found)) == set(rows(build_snapshot(vault)))
    unchanged = run_json(capsys, "snapshot", "--json", "--since", found["etag"])
    assert unchanged == {"unchanged": True, "etag": found["etag"]}


def test_log_path_answers_several_repos_in_one_object(vault: Vault, capsys):
    assert run_json(capsys, "log", "path", "--repo", "orchard") == {"path": "Log/orchard-log.md"}
    assert run_json(capsys, "log", "path", "--repo", "orchard", "--repo", "frame") == {
        "orchard": "Log/orchard-log.md",
        "frame": "Log/frame-log.md",
    }
