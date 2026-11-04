from __future__ import annotations

from pathlib import Path

from mindin.cli import main
from mindin.frontmatter import parse
from mindin.notes import lint, lint_note_text
from mindin.vault import Vault


def groups(vault: Vault) -> dict[str, list[str]]:
    return lint(vault).as_dict()


def test_broken_frontmatter_is_reported_with_the_line(vault: Vault):
    assert any("Resources/broken-yaml.md: line" in entry for entry in groups(vault)["broken_frontmatter"])


def test_missing_required_fields_are_reported_per_field(vault: Vault):
    found = groups(vault)["missing_required"]
    assert "Resources/no-frontmatter.md: title" in found
    assert "Resources/no-frontmatter.md: status" in found


def test_missing_area_is_its_own_finding(vault: Vault):
    assert any("Resources/no-frontmatter.md" in entry for entry in groups(vault)["missing_area"])


def test_unknown_tags_are_silent_while_the_vocabulary_is_open(vault: Vault):
    assert "unknown_tags" not in groups(vault)


def test_unknown_tags_are_reported_when_strict(vault: Vault):
    vault.schema.data["tags"] = {"strict": True, "vocabulary": ["orchard"]}
    assert any("cluster" in entry for entry in groups(vault)["unknown_tags"])


def test_unresolved_links_name_the_source_and_the_target(vault: Vault):
    assert "Projects/orchard-draft.md -> [[saknad-not]]" in groups(vault)["unresolved_links"]


def test_orphans_ignore_the_log_and_archive_folders(vault: Vault):
    orphans = groups(vault)["orphans"]
    assert "Resources/no-frontmatter.md" in orphans
    assert not any(entry.startswith(("Log/", "Archive/")) for entry in orphans)


def test_stale_active_reports_an_old_open_task(vault: Vault):
    assert any("Projects/workshop-stale.md" in entry for entry in groups(vault)["stale_active"])


def test_a_fresh_open_task_is_not_stale(vault: Vault):
    assert not any("orchard-fresh" in entry for entry in groups(vault)["stale_active"])


def test_archive_status_mismatch_catches_an_archived_active_note(vault: Vault):
    assert groups(vault)["archive_status_mismatch"] == ["Archive/wrong-status.md: status active"]


def test_duplicate_stems_name_both_keys(vault: Vault):
    assert "workshop: Areas/workshop.md, Resources/workshop.md" in groups(vault)["duplicate_stems"]


def test_superseded_target_missing_is_reported(vault: Vault):
    assert groups(vault)["superseded_target_missing"] == ["Resources/old-domain-howto.md -> [[gone]]"]


def test_render_counts_the_findings(vault: Vault):
    text = lint(vault).render()
    assert text.splitlines()[0].endswith("findings")
    assert "## stale_active" in text


def test_the_lint_note_carries_a_complete_frontmatter(vault: Vault):
    text = lint_note_text(lint(vault))
    assert text.startswith("---\ntitle: Lint ")
    assert "kind: log" in text
    assert 'area: "[[vault]]"' in text


def test_the_repo_log_is_not_a_duplicate_stem(vault: Vault):
    assert not any(entry.startswith("orchard:") for entry in groups(vault)["duplicate_stems"])


def test_a_note_with_an_area_link_is_connected_not_orphaned(vault: Vault):
    from mindin import notes

    notes.write(
        vault,
        "Resources/lonely-trap.md",
        "---\ntitle: Ensam fälla\ndate: 2026-07-01\nupdated: 2026-07-01\ntags: [orchard]\nstatus: active\n"
        'kind: trap\narea: "[[orchard]]"\n---\n\nIngen länkar hit, men area gör den till en del av grafen.\n',
    )
    assert "Resources/lonely-trap.md" not in groups(vault)["orphans"]


def test_lint_park_stale_moves_old_open_notes_and_reports_them(vault: Vault, vault_dir: Path, capsys):
    assert main(["lint", "--park-stale"]) == 0
    out = capsys.readouterr().out
    assert "## parked" in out and "Projects/workshop-stale.md" in out
    assert "stale_active" not in out
    frontmatter, _ = parse((vault_dir / "Projects/workshop-stale.md").read_text(encoding="utf-8"))
    assert frontmatter["status"] == "backlog"
