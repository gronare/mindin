from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from template_support import filled

from mindin import notes, tasks
from mindin.backends import VersionConflict
from mindin.cli import main
from mindin.frontmatter import parse, validate
from mindin.index import Index
from mindin.schema import CLAIM_KEY, Schema, default_schema_data, instructions
from mindin.vault import Vault

NOTE = "Projects/orchard-fresh.md"


@pytest.fixture
def cli_vault(
    vault_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forget_storage: Callable[[], None]
) -> Path:
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cli-cache"))
    monkeypatch.delenv("VAULT_SCHEMA", raising=False)
    forget_storage()
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    return vault_dir


def run(capsys: pytest.CaptureFixture, *argv: str) -> dict:
    assert main(list(argv)) == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


def claim_of(vault_dir: Path, key: str = NOTE) -> dict | None:
    frontmatter, _ = parse((vault_dir / key).read_text(encoding="utf-8"))
    return frontmatter.get(CLAIM_KEY)


def test_claim_writes_the_claim_key_and_release_clears_only_that(cli_vault: Path, capsys):
    before, body = parse((cli_vault / NOTE).read_text(encoding="utf-8"))
    answer = run(capsys, "task", "claim", NOTE, "--machine", "mac", "--task", "export", "--repo", "orchard")
    assert answer["claimed"] is True and answer["replaced"] is None
    claim = claim_of(cli_vault)
    assert claim == answer["claim"]
    assert (claim["machine"], claim["task"], claim["repo"]) == ("mac", "export", "orchard")
    assert len(claim["since"]) == 20 and claim["since"].endswith("Z") and "T" in claim["since"]
    after, after_body = parse((cli_vault / NOTE).read_text(encoding="utf-8"))
    assert {key: value for key, value in after.items() if key not in (CLAIM_KEY, "updated")} == {
        key: value for key, value in before.items() if key != "updated"
    }
    assert after_body.strip() == body.strip()
    assert run(capsys, "task", "release", NOTE) == {"path": NOTE, "released": True, "claim": None}
    assert claim_of(cli_vault) is None
    assert run(capsys, "task", "release", NOTE)["released"] is True, "releasing an unclaimed note is a no-op"


def test_the_same_machine_claims_again_and_keeps_its_since(cli_vault: Path, capsys):
    first = run(capsys, "task", "claim", NOTE, "--machine", "mac", "--task", "export")["claim"]
    again = run(capsys, "task", "claim", NOTE, "--machine", "mac", "--task", "export", "--repo", "api")
    assert again["claimed"] is True and again["claim"]["since"] == first["since"] and again["claim"]["repo"] == "api"


def test_another_machines_claim_refuses_unless_forced(cli_vault: Path, capsys):
    held = run(capsys, "task", "claim", NOTE, "--machine", "devbox", "--task", "search")["claim"]
    refused = run(capsys, "task", "claim", NOTE, "--machine", "mac", "--task", "export")
    assert refused == {"path": NOTE, "claimed": False, "claim": held}
    assert claim_of(cli_vault) == held
    taken = run(capsys, "task", "claim", NOTE, "--machine", "mac", "--task", "export", "--force")
    assert taken["claimed"] is True and taken["replaced"] == held and claim_of(cli_vault)["machine"] == "mac"


def test_release_with_a_machine_leaves_another_machines_claim(cli_vault: Path, capsys):
    held = run(capsys, "task", "claim", NOTE, "--machine", "devbox", "--task", "search")["claim"]
    assert run(capsys, "task", "release", NOTE, "--machine", "mac") == {"path": NOTE, "released": False, "claim": held}
    assert claim_of(cli_vault) == held
    assert run(capsys, "task", "release", NOTE, "--machine", "devbox")["released"] is True
    assert claim_of(cli_vault) is None


def test_claims_are_for_task_notes_only(cli_vault: Path, capsys):
    assert main(["task", "claim", "Areas/orchard.md", "--machine", "mac", "--task", "x"]) == 1
    assert "is not in a task folder" in capsys.readouterr().err


def test_the_task_help_lists_claim_and_release_for_tools_that_detect_them(capsys):
    with pytest.raises(SystemExit):
        main(["task", "--help"])
    out = capsys.readouterr().out
    assert "claim" in out and "release" in out


def test_a_claim_retries_on_a_note_that_changed_under_it_and_keeps_that_change(vault: Vault, monkeypatch):
    original = vault.backend.get
    raced = []

    def get_then_race(key: str) -> tuple[str, str]:
        answer = original(key)
        if not raced:
            raced.append(key)
            path = Path(vault.backend.root) / key
            path.write_text(path.read_text(encoding="utf-8") + "\nWritten meanwhile.\n", encoding="utf-8")
        return answer

    monkeypatch.setattr(vault.backend, "get", get_then_race)
    answer = tasks.claim_note(vault, NOTE, "mac", "export")
    assert answer["claimed"] is True and raced == [NOTE]
    frontmatter, body = parse(original(NOTE)[0])
    assert frontmatter[CLAIM_KEY]["machine"] == "mac" and "Written meanwhile." in body


def test_a_claim_that_keeps_conflicting_gives_up_with_the_conflict(vault: Vault, monkeypatch):
    def always_conflicting(*_args, **_kwargs):
        raise VersionConflict("changed")

    tasks.claim_note(vault, NOTE, "mac", "export")
    monkeypatch.setattr(vault.backend, "put", always_conflicting)
    with pytest.raises(VersionConflict, match="kept changing"):
        tasks.claim_note(vault, NOTE, "mac", "export")
    with pytest.raises(VersionConflict, match="kept changing"):
        tasks.release_note(vault, NOTE)


def test_write_file_keeps_the_stored_claim_whatever_the_content_says(vault: Vault):
    tasks.claim_note(vault, NOTE, "devbox", "search")
    stored = parse(vault.backend.get(NOTE)[0])[0][CLAIM_KEY]
    text, _ = vault.backend.get(NOTE)
    frontmatter, body = parse(text)
    del frontmatter[CLAIM_KEY]
    notes.write(vault, NOTE, notes.dump(frontmatter, body + "\nMore.\n"))
    assert parse(vault.backend.get(NOTE)[0])[0][CLAIM_KEY] == stored, "an agent rewriting the note keeps the claim"
    frontmatter[CLAIM_KEY] = {"machine": "hand", "task": "edit", "since": "2026-01-01T00:00:00Z"}
    notes.write(vault, NOTE, notes.dump(frontmatter, body))
    assert parse(vault.backend.get(NOTE)[0])[0][CLAIM_KEY] == stored, "a hand edit of the key does not take"
    new = "Projects/orchard-new.md"
    notes.write(vault, new, notes.dump(frontmatter, filled(vault.schema, "task", body.strip())))
    assert CLAIM_KEY not in parse(vault.backend.get(new)[0])[0], "a new note starts unclaimed"


def test_close_and_a_move_to_the_backlog_clear_the_claim(vault: Vault):
    tasks.claim_note(vault, NOTE, "mac", "export")
    notes.set_status(vault, NOTE, "active")
    assert CLAIM_KEY in parse(vault.backend.get(NOTE)[0])[0], "staying active keeps it"
    notes.set_status(vault, NOTE, "backlog", priority="low")
    assert CLAIM_KEY not in parse(vault.backend.get(NOTE)[0])[0]
    tasks.claim_note(vault, NOTE, "mac", "export")
    archived = notes.close(vault, NOTE)
    assert CLAIM_KEY not in parse(vault.backend.get(archived)[0])[0]


def test_the_schema_accepts_a_well_formed_claim_and_names_a_broken_one():
    schema = Schema(default_schema_data())
    assert CLAIM_KEY in schema.optional_fields
    base = {"title": "x", "date": "2026-10-01", "updated": "2026-10-01", "tags": ["x"], "status": "active"}
    good = {"machine": "mac", "task": "export", "repo": "", "since": "2026-10-23T10:00:00Z"}
    assert validate({**base, CLAIM_KEY: good}, "Areas/x.md", schema) == []
    for broken in ("mac", {"machine": "mac"}, {"machine": "mac", "task": "export"}, ["mac"]):
        problems = validate({**base, CLAIM_KEY: broken}, "Areas/x.md", schema)
        assert problems == [f"{CLAIM_KEY} must be a mapping with machine, task and since (and optionally repo)"], broken


def test_the_instructions_name_the_claim_only_when_the_schema_lists_it():
    data = default_schema_data()
    assert f"{CLAIM_KEY}, on a task note, says which machine and task work on it" in instructions(Schema(data))
    data["frontmatter"]["optional"] = [field for field in data["frontmatter"]["optional"] if field != CLAIM_KEY]
    assert f"{CLAIM_KEY}, on a task note" not in instructions(Schema(data))


def test_the_index_carries_the_claim_and_context_shows_it(vault: Vault):
    tasks.claim_note(vault, NOTE, "devbox", "search", repo="orchard")
    stored = json.loads(vault.index.note(NOTE).claim)
    assert (stored["machine"], stored["task"], stored["repo"]) == ("devbox", "search", "orchard")
    rendered = notes.context(vault, repo="orchard").render()
    assert f"worked on at devbox · search since {stored['since'][:10]}" in rendered
    tasks.release_note(vault, NOTE)
    assert vault.index.note(NOTE).claim == ""


def test_an_index_from_before_the_claim_column_gains_it_and_reads_again(tmp_path: Path, vault: Vault):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE notes (key TEXT PRIMARY KEY, version TEXT, mtime REAL, folder TEXT, stem TEXT, title TEXT, "
        "summary TEXT, status TEXT, kind TEXT, area TEXT, tags TEXT, paths TEXT, date TEXT, updated TEXT, "
        "superseded_by TEXT, valid INT, error TEXT, size INT, priority TEXT, source TEXT);"
        "INSERT INTO notes (key, version) VALUES ('Projects/x.md', 'v1');"
    )
    db.commit()
    db.close()
    index = Index(path, vault.backend)
    assert "claim" in index._columns("notes")
    assert index.all_notes() == [], "the old rows are dropped so the next sync reads every note again"
    index.close()


def test_release_by_machine_and_task_follows_a_moved_note_and_a_deleted_one_counts_as_released(cli_vault: Path, capsys):
    run(capsys, "task", "claim", NOTE, "--machine", "mac", "--task", "export")
    run(capsys, "task", "claim", "Projects/orchard-draft.md", "--machine", "devbox", "--task", "export")
    moved = "Projects/orchard-renamed.md"
    (cli_vault / NOTE).rename(cli_vault / moved)
    assert main(["task", "release", NOTE, "--machine", "mac"]) == 1
    assert "no such note" in capsys.readouterr().err
    assert run(capsys, "task", "release", "--machine", "mac", "--task", "export") == {
        "paths": [moved],
        "released": True,
    }
    assert claim_of(cli_vault, moved) is None
    assert claim_of(cli_vault, "Projects/orchard-draft.md")["machine"] == "devbox", "another machine's claim stays"
    (cli_vault / moved).unlink()
    assert run(capsys, "task", "release", "--machine", "mac", "--task", "export") == {"paths": [], "released": True}
    assert main(["task", "release", "--machine", "mac"]) == 1
    assert "needs --machine and --task" in capsys.readouterr().err


def test_park_stale_leaves_a_freshly_claimed_note_and_parks_a_stale_claim(vault: Vault):
    stale = "Projects/workshop-stale.md"
    tasks.claim_note(vault, stale, "mac", "export")
    note = vault.index.note(stale)
    assert notes.claim_is_fresh(note)
    old = date.today() - timedelta(days=40)
    assert not notes.claim_is_fresh(
        replace(
            note, updated=old.isoformat(), claim=json.dumps({**json.loads(note.claim), "since": f"{old}T08:00:00Z"})
        )
    )
    text, _ = vault.backend.get(stale)
    path = Path(vault.backend.root) / stale
    aged = text.replace(f"updated: {date.today().isoformat()}", f"updated: {old.isoformat()}")
    path.write_text(aged.replace(json.loads(note.claim)["since"], f"{old}T08:00:00Z"), encoding="utf-8")
    vault.index.sync(force=True)
    assert notes.park_stale(vault) == [stale], "a claim quiet for more than 14 days no longer holds the note"
    tasks.claim_note(vault, NOTE, "mac", "fresh")
    fresh_text = (Path(vault.backend.root) / NOTE).read_text(encoding="utf-8")
    (Path(vault.backend.root) / NOTE).write_text(
        fresh_text.replace(f"updated: {date.today().isoformat()}", f"updated: {old.isoformat()}"), encoding="utf-8"
    )
    vault.index.sync(force=True)
    assert NOTE not in notes.park_stale(vault), "a claim made within 14 days holds the note however old its updated"


def test_a_new_task_note_is_created_only_where_none_exists(vault: Vault, monkeypatch):
    seen = []
    original = vault.backend.put

    def spy(key, text, expected_version=None):
        seen.append(expected_version)
        return original(key, text, expected_version=expected_version)

    monkeypatch.setattr(vault.backend, "put", spy)
    monkeypatch.setattr(tasks, "place", lambda _vault, _cwd: tasks.Place(repo="orchard", checkout=None, area=None))
    tasks.open_task(vault, "Prune", "/nowhere")
    assert seen == ["*"]
