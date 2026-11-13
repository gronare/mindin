from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from remote_support import Served
from test_remote import isolated, note_on_server, remote, served, server  # noqa: F401

from mindin.cli import main
from mindin.vault import Vault

KEY = "Projects/orchard-fresh.md"
NEW_TASK = (
    "---\ntitle: Orchard gate\ndate: 2026-09-01\nupdated: 2026-09-01\ntags: [orchard]\nstatus: active\n"
    'kind: task\narea: "[[orchard]]"\n---\n\nFix the gate.\n'
)


def run(
    capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch, *argv: str, stdin: str = ""
) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def round_trip(capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch, stored: str) -> None:
    code, out, err = run(capsys, monkeypatch, "read", KEY, "--json")
    assert code == 0, err
    read = json.loads(out)
    assert read["text"] == stored and read["etag"]
    changed = read["text"].replace("hämta slots", "hämta lediga slots")
    code, out, err = run(capsys, monkeypatch, "write", KEY, "--etag", read["etag"], stdin=changed)
    assert code == 0, err
    written = json.loads(out)
    assert written["path"] == KEY and written["etag"] not in ("", read["etag"])
    code, _, err = run(capsys, monkeypatch, "write", KEY, "--etag", read["etag"], stdin=changed)
    assert code == 1 and "etag" in err and "\n" not in err.strip()
    code, _, err = run(capsys, monkeypatch, "write", "Projects/orchard-gate.md", "--etag", "*", stdin=NEW_TASK)
    assert code == 1 and "does not follow the task template" in err


def test_read_json_and_write_with_its_etag_on_a_local_vault(
    vault: Vault, vault_dir: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    round_trip(capsys, monkeypatch, (vault_dir / KEY).read_text(encoding="utf-8"))
    assert "hämta lediga slots" in (vault_dir / KEY).read_text(encoding="utf-8")


def test_read_json_and_write_with_its_etag_through_a_server(
    remote: Served,  # noqa: F811
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    round_trip(capsys, monkeypatch, remote.read(KEY))
    assert "hämta lediga slots" in note_on_server(remote, KEY)[1]


def test_changes_follow_a_write_through_a_server(
    remote: Served,  # noqa: F811
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    code, out, err = run(capsys, monkeypatch, "changes", "--json")
    assert code == 0, err
    since = json.loads(out)["cursor"]
    etag = json.loads(run(capsys, monkeypatch, "read", KEY, "--json")[1])["etag"]
    text = remote.read(KEY).replace("hämta slots", "hämta lediga slots")
    assert run(capsys, monkeypatch, "write", KEY, "--etag", etag, stdin=text)[0] == 0
    code, out, err = run(capsys, monkeypatch, "changes", "--json", "--since", since, "--wait", "5", "--body")
    assert code == 0, err
    found = json.loads(out)
    assert [(change["key"], change["action"]) for change in found["changes"]] == [(KEY, "write")]
    assert "lediga slots" in found["changes"][0]["note"]["body"]


def test_about_lists_write():
    from mindin.cli import about, build_parser

    assert "write" in about(build_parser())["capabilities"]
