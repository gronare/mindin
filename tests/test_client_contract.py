from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from remote_support import Served, serve
from test_cli_hooks import feed, head_sha, make_repo, prompts, transcript_of
from test_cli_output import in_a_terminal

from mindin import credentials, hooks, human, notes, provision, views
from mindin.backends.local import LocalBackend
from mindin.cli import main
from mindin.client import where
from mindin.frontmatter import parse
from mindin.snapshot import build_snapshot
from mindin.terminal import terminal
from mindin.vault import SubjectVaults, Vault, open_vault

PLACES = ("local", "server")
MISSING_NOTE = {
    "local": "mindin: no such note: Projects/nowhere.md",
    "server": "mindin: Error executing tool read_file: Projects/nowhere.md",
}


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for name in ("VAULT_STOP_HOOK", "VAULT_STOP_HOOK_PROMPTS", "VAULT_STOP_HOOK_COMMITS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def server(vaults: SubjectVaults, storage: Path, tmp_path: Path) -> Iterator[Served]:
    with serve(vaults, tmp_path) as running:
        yield running


@dataclass
class Place:
    name: str
    folder: Path
    served: Served | None

    def write(self, key: str, text: str) -> None:
        if self.served is not None:
            self.served.write(key, text)
        (self.folder / key).parent.mkdir(parents=True, exist_ok=True)
        (self.folder / key).write_text(text, encoding="utf-8")

    def read(self, key: str) -> str:
        return self.served.read(key) if self.served is not None else (self.folder / key).read_text(encoding="utf-8")

    def mirror(self, key: str) -> None:
        if self.served is not None:
            self.write(key, self.served.read(key))


def open_place(
    name: str, folder: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Place:
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / f"{name}-cache"))
    if name == "local":
        monkeypatch.setenv("VAULT_PATH", str(folder))
        return Place(name, folder, None)
    served: Served = request.getfixturevalue("server")
    served.seed(folder)
    credentials.save(served.url, {"key": served.key(), "scope": "vault:read vault:write", "name": "laptop"})
    monkeypatch.setenv("VAULT_SERVER_URL", served.url)
    return Place(name, folder, served)


@pytest.fixture(params=PLACES)
def place_name(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def place(
    place_name: str, vault_dir: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Place:
    return open_place(place_name, vault_dir, request, monkeypatch, tmp_path)


def reference(folder: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Vault:
    with monkeypatch.context() as patched:
        patched.delenv("VAULT_SERVER_URL", raising=False)
        patched.setenv("VAULT_PATH", str(folder))
        patched.setenv("VAULT_CACHE_DIR", str(tmp_path / "reference-cache"))
        vault = open_vault()
    vault.index.sync(force=True)
    return vault


def expected_session_start(vault: Vault, repo: Path) -> str:
    return hooks.render_session_start(notes.context(vault, **where(str(repo))).as_dict(), str(repo)) + "\n"


def run_out(capsys: pytest.CaptureFixture, *argv: str) -> str:
    assert main(list(argv)) == 0, capsys.readouterr().err
    return capsys.readouterr().out


def hook_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, payload: dict, event: str) -> str:
    feed(monkeypatch, payload)
    return run_out(capsys, "hook", event)


def system_note(repo: Path, kind: str = "system") -> str:
    return (
        "---\ntitle: frame\ndate: 2020-01-01\nupdated: 2020-01-01\n"
        f"tags: [frame]\nstatus: active\nkind: {kind}\npath: {repo}\n---\n\nKärnan.\n"
    )


def test_session_start_prints_what_the_vault_holds(
    place: Place, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    place.write("Areas/frame.md", system_note(repo))
    printed = hook_output(monkeypatch, capsys, {"cwd": str(repo)}, "session-start")
    assert printed == expected_session_start(reference(place.folder, monkeypatch, tmp_path), repo)
    assert "commits since the note — Areas/frame.md" in printed and "First commit" in printed


def test_session_start_prints_what_the_vault_holds_on_the_para_layout(
    place_name: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    folder = tmp_path / "para"
    provision.initialize(LocalBackend(folder), preset="para")
    place = open_place(place_name, folder, request, monkeypatch, tmp_path)
    repo = make_repo(tmp_path / "repos", "frame")
    place.write("Areas/frame.md", system_note(repo, kind="area"))
    task = (
        "---\ntitle: Frame the grove\ndate: @@\nupdated: @@\ntags: [work]\nstatus: active\nkind: task\n"
        'area: "[[frame]]"\n---\n\n## Outcome\n'
    ).replace("@@", date.today().isoformat())
    place.write("Projects/frame-grove.md", task)
    logged = run_out(capsys, "log", "append", "--cwd", str(repo), "--line", "Ramade in lunden")
    assert json.loads(logged) == {"path": "Resources/Logs/frame-log.md"}
    place.mirror("Resources/Logs/frame-log.md")
    printed = hook_output(monkeypatch, capsys, {"cwd": str(repo)}, "session-start")
    assert printed == expected_session_start(reference(folder, monkeypatch, tmp_path), repo)
    assert "### Areas/frame.md" in printed and "Projects/frame-grove.md" in printed
    assert "## log — frame" in printed and "Ramade in lunden" in printed


def test_stop_asks_for_unlogged_commits_and_unsaved_prompts_once(
    place: Place, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    transcript = str(transcript_of(tmp_path / "t.jsonl", prompts("p1", "p2", "p3")))
    reason = json.loads(
        hook_output(monkeypatch, capsys, {"cwd": str(repo), "session_id": "s1", "transcript_path": transcript}, "stop")
    )["reason"]
    assert head_sha(repo) in reason and "the user wrote 3 messages since" in reason
    outside = tmp_path / "shed"
    outside.mkdir()
    payload = {"cwd": str(outside), "session_id": "s1", "transcript_path": transcript}
    assert hook_output(monkeypatch, capsys, payload, "stop").strip() == ""
    payload["session_id"] = "s2"
    assert "the user wrote 3 messages since" in json.loads(hook_output(monkeypatch, capsys, payload, "stop"))["reason"]


def test_the_weekly_lint_writes_and_parks_as_the_vault_does(
    place: Place, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    copy = shutil.copytree(place.folder, tmp_path / "before-lint")
    expected = notes.lint_findings(reference(copy, monkeypatch, tmp_path), write="Log/lint.md", park=True).render()
    assert main(["lint", "--write", "Log/lint.md", "--park-stale"]) == 0
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (expected + "\n", "wrote Log/lint.md\n")
    assert "parked" in captured.out and "Projects/workshop-stale.md" in captured.out
    assert place.read("Log/lint.md") == (copy / "Log" / "lint.md").read_text(encoding="utf-8")
    assert parse(place.read("Projects/workshop-stale.md"))[0]["status"] == "backlog"


def without_stamps(snapshot: dict) -> dict:
    kept = {key: value for key, value in snapshot.items() if key not in ("etag", "synced_at")}
    kept["notes"] = [{key: value for key, value in row.items() if key != "mtime"} for row in snapshot["notes"]]
    return kept


def test_the_snapshot_holds_what_the_vault_holds(place: Place, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    run_out(capsys, "task", "claim", "Projects/orchard-fresh.md", "--machine", "devbox", "--task", "t1")
    place.mirror("Projects/orchard-fresh.md")
    found = json.loads(run_out(capsys, "snapshot", "--json"))
    unchanged = json.loads(run_out(capsys, "snapshot", "--json", "--since", found["etag"]))
    assert unchanged == {"unchanged": True, "etag": found["etag"]}
    assert without_stamps(found) == without_stamps(build_snapshot(reference(place.folder, monkeypatch, tmp_path)))
    by_key = {row["key"]: row for row in found["notes"]}
    assert by_key["Projects/orchard-fresh.md"]["claim"]["machine"] == "devbox"
    assert found["logs"]["Log/orchard-log.md"][0]["links"] == ["orchard"]


def test_read_prints_the_note_as_stored(place: Place, capsys):
    key = "Projects/orchard-fresh.md"
    stored = (place.folder / key).read_text(encoding="utf-8")
    assert run_out(capsys, "read", key) == stored
    assert run_out(capsys, "read", key, "--body") == parse(stored)[1]
    assert main(["read", "Projects/nowhere.md"]) == 1
    missing = capsys.readouterr()
    assert missing.out == "" and missing.err.strip() == MISSING_NOTE[place.name]


def test_the_terminal_renders_what_the_vault_holds(
    place: Place, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    place.write("Areas/frame.md", system_note(repo))
    vault = reference(place.folder, monkeypatch, tmp_path)
    in_a_terminal(monkeypatch)
    term = terminal(False)
    found = views.view(vault, "context", **where(str(repo)))
    expected = [
        human.render_search(views.view(vault, "search", query="kärnan"), term),
        human.render_backlog(views.view(vault, "backlog"), term, paths=True),
        human.render_lint(notes.lint_findings(vault), term, vault.schema.stale_after_days),
        human.render_context(found, hooks.note_commits(str(repo), found["system"]), term),
    ]
    printed = [
        run_out(capsys, "search", "kärnan"),
        run_out(capsys, "backlog", "--paths"),
        run_out(capsys, "lint"),
        run_out(capsys, "context", "--path", str(repo)),
    ]
    assert printed == [text + "\n" for text in expected]
    assert printed[0].startswith("4 notes · ")
    assert printed[2].startswith("15 findings in 9 kinds")
    assert "1 commit since the note" in printed[3]
