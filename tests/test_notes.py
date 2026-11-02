from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from notes_vault_mcp import notes
from notes_vault_mcp.backends import NotFound, VersionConflict
from notes_vault_mcp.frontmatter import FrontmatterError, parse
from notes_vault_mcp.vault import Vault

ORCHARD = str(Path("~/projects/orchard").expanduser())

VALID = """---
title: Ny plan
date: 2026-08-01
updated: 2026-08-01
tags: [orchard]
status: active
kind: task
area: "[[orchard]]"
---

Kroppen.
"""


def body_of(vault: Vault, key: str) -> tuple[dict, str]:
    text, _ = vault.backend.get(key)
    return parse(text)


def test_read_prefixes_the_etag(vault: Vault):
    text = notes.read(vault, "Areas/orchard.md")
    assert text.startswith("etag: ")


def test_read_warns_about_a_superseded_note(vault: Vault):
    assert "> [!warning] Superseded by [[gone]]" in notes.read(vault, "Resources/old-domain-howto.md")


def test_write_stamps_updated_with_today(vault: Vault):
    notes.write(vault, "Projects/ny-plan.md", VALID)
    frontmatter, _ = body_of(vault, "Projects/ny-plan.md")
    assert frontmatter["updated"] == date.today()
    assert frontmatter["date"] == date(2026, 8, 1)


def test_write_fills_a_missing_date(vault: Vault):
    content = VALID.replace("date: 2026-08-01\n", "")
    notes.write(vault, "Projects/ny-plan.md", content)
    frontmatter, _ = body_of(vault, "Projects/ny-plan.md")
    assert frontmatter["date"] == date.today()
    assert list(frontmatter)[:2] == ["title", "date"]


def test_write_indexes_the_note_at_once(vault: Vault):
    notes.write(vault, "Projects/ny-plan.md", VALID)
    assert vault.index.note("Projects/ny-plan.md").title == "Ny plan"


def test_write_refuses_a_note_without_area(vault: Vault):
    content = VALID.replace('area: "[[orchard]]"\n', "")
    with pytest.raises(notes.ValidationError) as caught:
        notes.write(vault, "Projects/ny-plan.md", content)
    assert "missing area" in str(caught.value)


def test_write_refuses_an_unknown_status(vault: Vault):
    content = VALID.replace("status: active", "status: halvklar")
    with pytest.raises(notes.ValidationError) as caught:
        notes.write(vault, "Projects/ny-plan.md", content)
    assert "halvklar" in str(caught.value)


def test_write_refuses_broken_yaml(vault: Vault):
    with pytest.raises(FrontmatterError):
        notes.write(vault, "Projects/ny-plan.md", "---\ntags: [unclosed\nstatus: active\n---\n\nkropp\n")


def test_write_refuses_a_stale_etag(vault: Vault):
    notes.write(vault, "Projects/ny-plan.md", VALID)
    _, stale = vault.backend.get("Projects/ny-plan.md")
    notes.write(vault, "Projects/ny-plan.md", VALID.replace("Kroppen.", "Nagon annan skrev."))
    with pytest.raises(VersionConflict):
        notes.write(vault, "Projects/ny-plan.md", VALID, expected_etag=stale)


def test_write_accepts_the_current_etag(vault: Vault):
    result = notes.write(vault, "Projects/ny-plan.md", VALID)
    etag = result.splitlines()[-1].removeprefix("etag: ")
    notes.write(vault, "Projects/ny-plan.md", VALID, expected_etag=etag)


def test_append_adds_a_line_and_bumps_updated(vault: Vault):
    notes.append(vault, "Areas/orchard.md", "En ny rad.")
    frontmatter, body = body_of(vault, "Areas/orchard.md")
    assert body.rstrip().endswith("En ny rad.")
    assert frontmatter["updated"] == date.today()


def test_append_refuses_a_missing_note(vault: Vault, vault_dir):
    with pytest.raises(NotFound, match="write_file creates one"):
        notes.append(vault, "Projects/helt-ny.md", "Första raden.")
    assert not (vault_dir / "Projects" / "helt-ny.md").exists()


def test_move_reindexes_under_the_new_key(vault: Vault):
    notes.move(vault, "Projects/orchard-fresh.md", "Projects/omdopt.md")
    assert vault.index.note("Projects/orchard-fresh.md") is None
    assert vault.index.note("Projects/omdopt.md").title == "Orchard: bokningswidget i checkout"


def test_delete_removes_the_note_and_the_index_row(vault: Vault):
    notes.delete(vault, "Projects/orchard-fresh.md")
    assert vault.index.note("Projects/orchard-fresh.md") is None
    with pytest.raises(NotFound):
        vault.backend.get("Projects/orchard-fresh.md")


def test_close_moves_into_the_archive_and_completes(vault: Vault):
    destination = notes.close(vault, "Projects/orchard-fresh.md")
    assert destination == "Archive/orchard-fresh.md"
    frontmatter, _ = body_of(vault, destination)
    assert frontmatter["status"] == "complete"
    assert vault.index.note("Projects/orchard-fresh.md") is None


def test_close_with_merged_into_sets_superseded_by(vault: Vault):
    destination = notes.close(vault, "Projects/orchard-fresh.md", merged_into="Projects/orchard-draft.md")
    frontmatter, _ = body_of(vault, destination)
    assert frontmatter["status"] == "superseded"
    assert frontmatter["superseded_by"] == "[[orchard-draft]]"


def test_close_accepts_an_explicit_status(vault: Vault):
    destination = notes.close(vault, "Projects/orchard-fresh.md", status="draft")
    assert body_of(vault, destination)[0]["status"] == "draft"


def test_close_suffixes_a_name_the_archive_already_holds(vault: Vault):
    notes.close(vault, "Projects/orchard-fresh.md")
    notes.write(vault, "Projects/orchard-fresh.md", VALID)
    assert notes.close(vault, "Projects/orchard-fresh.md") == "Archive/orchard-fresh-2.md"


def test_log_append_adds_a_line_to_the_repo_log(vault: Vault):
    notes.log_append(vault, "orchard", "Bokningen klar", commits=("abc1234", "def5678"))
    _, body = body_of(vault, "Log/orchard-log.md")
    last = body.strip().splitlines()[-1]
    assert last == f"- [{date.today().isoformat()}] Bokningen klar | branch: - | commits: abc1234 def5678 | [[orchard]]"


def test_log_append_records_the_branch(vault: Vault):
    notes.log_append(vault, "orchard", "Bokningen klar", commits=("abc1234",), branch="fix-booking")
    _, body = body_of(vault, "Log/orchard-log.md")
    assert "| branch: fix-booking | commits: abc1234 |" in body.strip().splitlines()[-1]


def test_log_append_creates_a_missing_log(vault: Vault):
    notes.log_append(vault, "frame", "Första raden")
    frontmatter, body = body_of(vault, "Log/frame-log.md")
    assert frontmatter["title"] == "Log — frame"
    assert frontmatter["kind"] == "log"
    assert frontmatter["tags"] == ["log", "frame"]
    assert frontmatter["area"] == "[[frame]]"
    assert "| commits: - |" in body


def test_log_append_takes_an_area_override(vault: Vault):
    notes.log_append(vault, "frame", "rad", area="[[orchard]]")
    assert body_of(vault, "Log/frame-log.md")[0]["area"] == "[[orchard]]"


def test_log_append_wraps_a_bare_area_stem_in_a_wikilink(vault: Vault):
    notes.log_append(vault, "frame", "rad", area="orchard")
    frontmatter, body = body_of(vault, "Log/frame-log.md")
    assert frontmatter["area"] == "[[orchard]]"
    assert body.rstrip().endswith("| [[orchard]]")


def test_context_returns_the_system_note_in_full(vault: Vault):
    bundle = notes.context(vault, path=ORCHARD, repo="orchard")
    assert [note.key for note, _ in bundle.system] == ["Areas/orchard.md"]
    assert "Kontrollplanet som styr kärnan" in bundle.system[0][1]


def test_context_lists_only_open_tasks_for_the_repo(vault: Vault):
    bundle = notes.context(vault, path=ORCHARD, repo="orchard")
    assert {note.key for note in bundle.tasks} == {
        "Projects/orchard-fresh.md",
        "Projects/orchard-draft.md",
    }


def test_context_lists_a_stale_task_under_triage_with_the_three_ways_to_settle_it(vault: Vault):
    text = notes.context(vault, path=str(Path("~/workshop").expanduser()), repo="workshop").render()
    assert "## triage — 1 open notes older than 30 days" in text
    assert "Projects/workshop-stale.md" in text.split("## triage")[1]
    assert "Projects/workshop-stale.md" not in text.split("## triage")[0]
    assert "close(path)" in text and 'set_status(path, "backlog", priority)' in text


def test_set_status_parks_and_revives_a_note(vault: Vault, vault_dir: Path):
    assert notes.set_status(vault, "Projects/workshop-stale.md", "backlog", priority="low", source="triage") == (
        "Projects/workshop-stale.md: status backlog, priority low"
    )
    frontmatter, _ = parse((vault_dir / "Projects/workshop-stale.md").read_text(encoding="utf-8"))
    assert frontmatter["status"] == "backlog" and frontmatter["priority"] == "low" and frontmatter["source"] == "triage"
    assert str(frontmatter["updated"]) == notes.today()
    assert notes.context(vault, repo="workshop").render().count("Projects/workshop-stale.md") == 1
    notes.set_status(vault, "Projects/workshop-stale.md", "active")
    assert vault.index.note("Projects/workshop-stale.md").status == "active"


def test_set_status_refuses_a_status_outside_the_schema(vault: Vault):
    with pytest.raises(notes.ValidationError, match="status"):
        notes.set_status(vault, "Projects/workshop-stale.md", "someday")


def test_park_stale_moves_only_the_old_open_notes_to_the_backlog(vault: Vault):
    parked = notes.park_stale(vault)
    assert parked == ["Projects/workshop-stale.md"]
    note = vault.index.note("Projects/workshop-stale.md")
    assert note.status == "backlog" and note.priority == "low" and note.source.startswith("veckolint ")
    assert vault.index.note("Projects/orchard-fresh.md").status == "active"
    assert notes.park_stale(vault) == []


def test_context_includes_the_reference_notes(vault: Vault):
    bundle = notes.context(vault, path=ORCHARD, repo="orchard")
    assert "Resources/booking-trap.md" in {note.key for note in bundle.references}


def test_context_ends_with_the_log_tail(vault: Vault):
    text = notes.context(vault, path=ORCHARD, repo="orchard").render()
    assert "## log — orchard" in text
    assert "Release-grinden mäter A/B" in text


def test_the_log_tail_holds_log_lines_only(vault: Vault):
    tail = notes.context(vault, path=ORCHARD, repo="orchard").log_tail
    assert tail and all(line.startswith("- [") for line in tail), tail


def test_context_says_so_when_nothing_matches(vault: Vault):
    assert notes.context(vault, path="/opt/annat", repo="annat").render() == "No vault context for this path or repo."


def test_the_log_never_shares_a_stem_with_the_hub_note(vault: Vault):
    notes.log_append(vault, "orchard", "rad")
    stems = [note.stem for note in vault.index.all_notes()]
    assert stems.count("orchard") == 1
    assert "orchard-log" in stems


def test_the_log_filename_follows_the_schema_setting(vault: Vault):
    vault.schema.data["log"] = {**vault.schema.data["log"], "file_format": "logg-{repo}.md"}
    notes.log_append(vault, "frame", "rad")
    assert vault.backend.get("Log/logg-frame.md")[0].startswith("---")
    assert notes.log_lines(vault, "frame", date.today().isoformat()[:7])


def test_context_puts_the_hub_note_before_a_draft_with_the_same_path(vault: Vault):
    notes.write(
        vault,
        "Areas/avtal-utkast.md",
        "---\ntitle: Avtal utkast\ndate: 2026-07-01\nupdated: 2026-07-01\ntags: [orchard]\nstatus: draft\n"
        "kind: system\npath: ~/projects/orchard\n---\n\nEtt utkast.\n",
    )
    bundle = notes.context(vault, path=ORCHARD, repo="orchard")
    assert [note.key for note, _ in bundle.system][0] == "Areas/orchard.md"


def test_context_reads_a_deeper_system_note_in_full_and_lists_root_level_ones_as_rows(vault: Vault):
    notes.write(
        vault,
        "Areas/orchard-bokning.md",
        "---\ntitle: Bokning\ndate: 2026-07-01\nupdated: 2026-07-01\ntags: [orchard]\nstatus: active\n"
        "kind: system\npath: ~/projects/orchard/app/models/bookings\n---\n\nBokningsdomänen.\n",
    )
    notes.write(
        vault,
        "Areas/orchard-personuppgifter.md",
        "---\ntitle: Personuppgifter\ndate: 2026-07-01\nupdated: 2026-07-01\ntags: [orchard]\nstatus: active\n"
        "kind: system\npath: ~/projects/orchard\n---\n\nInventering.\n",
    )
    at_root = notes.context(vault, path=ORCHARD, repo="orchard")
    assert [note.key for note, _ in at_root.system] == ["Areas/orchard.md"]
    assert {note.key for note in at_root.system_rows} >= {
        "Areas/orchard-bokning.md",
        "Areas/orchard-personuppgifter.md",
    }
    deep = notes.context(vault, path=f"{ORCHARD}/app/models/bookings", repo="orchard")
    assert [note.key for note, _ in deep.system] == ["Areas/orchard.md", "Areas/orchard-bokning.md"]
    assert "## other system notes" in at_root.render()
