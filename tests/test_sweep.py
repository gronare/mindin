from __future__ import annotations

from pathlib import Path

import pytest

from mindin import tasks
from mindin.backends import NotFound
from mindin.cli import main
from mindin.frontmatter import parse
from mindin.sweep import close_done
from mindin.vault import Vault, open_vault

LOG_HEAD = (
    "---\ntitle: Log — orchard\ndate: 2026-01-02\nupdated: 2026-09-01\ntags: [log]\nstatus: active\nkind: log\n"
    'area: "[[orchard]]"\n---\n\n'
)


def task(
    vault_dir: Path,
    stem: str,
    body: str,
    *,
    source: str = "",
    status: str = "active",
    day: str = "2026-09-01",
    updated: str | None = None,
    path: str | None = "~/projects/orchard",
) -> str:
    key = f"Projects/{stem}.md"
    head = (
        f"---\ntitle: {stem}\ndate: {day}\nupdated: {updated or day}\ntags: [orchard]\nstatus: {status}\nkind: task\n"
    )
    head += 'area: "[[orchard]]"\n' + (f"source: {source}\n" if source else "") + (f"path: {path}\n" if path else "")
    head += "---\n\n"
    (vault_dir / key).write_text(head + body, encoding="utf-8")
    return key


def log(vault: Vault, vault_dir: Path, *lines: str) -> None:
    (vault_dir / "Log" / "orchard-log.md").write_text(LOG_HEAD + "\n".join(lines) + "\n", encoding="utf-8")
    vault.index.sync(force=True)


def closed_body(vault: Vault, stem: str) -> tuple[dict, str]:
    return parse(vault.backend.get(f"Archive/{stem}.md")[0])


def swept(vault: Vault) -> tuple[list[str], dict[str, str]]:
    closed, uncertain = close_done(vault)
    return [verdict.key for verdict in closed], {verdict.key: verdict.reason for verdict in uncertain}


def test_a_note_whose_branch_landed_is_closed_with_the_log_line(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-prune", "## Goal\nCut the low branches.\n", source="branch prune-grove")
    log(vault, vault_dir, "- [2026-09-03] prune-grove: Cut the low branches | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault) == (["Projects/orchard-prune.md"], {})
    frontmatter, body = closed_body(vault, "orchard-prune")
    assert frontmatter["status"] == "complete"
    assert "prune-grove: Cut the low branches | commits: 9f1e2d3" in body.rstrip().splitlines()[-1]


def test_a_note_a_log_line_marks_done_is_closed(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-prune", "Cut the low branches.\n", status="backlog")
    log(vault, vault_dir, "- [2026-09-03] done [[orchard-prune]] Pruned the grove | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault)[0] == ["Projects/orchard-prune.md"]


def test_a_done_line_whose_link_differs_in_case_closes_the_note(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-prune", "Cut.\n")
    log(vault, vault_dir, "- [2026-09-03] Done [[Orchard-Prune]] | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault)[0] == ["Projects/orchard-prune.md"]


def test_a_note_a_session_end_line_names_stays_open_for_the_next_session(vault: Vault, vault_dir: Path):
    key = task(vault_dir, "orchard-prune", "## Goal\nPrune the whole grove over a few sessions.\n")
    log(vault, vault_dir, "- [2026-09-03] North rows, see [[orchard-prune]] | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault) == ([], {key: "the log line names the note without marking it done"})
    assert parse(vault.backend.get(key)[0])[0]["status"] == "active"


@pytest.mark.parametrize(
    "entry",
    [
        "inte klar [[orchard-prune]]",
        "nästan klar [[orchard-prune]]",
        "not done [[orchard-prune]]",
        "Pruned, done [[orchard-prune]]",
    ],
)
def test_a_line_not_starting_with_the_done_marker_leaves_the_note_open(vault: Vault, vault_dir: Path, entry: str):
    key = task(vault_dir, "orchard-prune", "Cut.\n")
    log(vault, vault_dir, f"- [2026-09-03] {entry} | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault) == ([], {key: "the log line names the note without marking it done"})


@pytest.mark.parametrize("later", ["inte klar [[orchard-prune]]", "[[orchard-prune]] unpushed after a revert"])
def test_a_later_line_naming_the_note_keeps_a_done_note_open(vault: Vault, vault_dir: Path, later: str):
    key = task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove")
    log(
        vault,
        vault_dir,
        "- [2026-09-03] done [[orchard-prune]] | commits: 9f1e2d3 | [[orchard]]",
        "- [2026-09-03] prune-grove: Cut | commits: 9f1e2d3 | [[orchard]]",
        f"- [2026-09-10] {later} | commits: - | [[orchard]]",
    )
    assert swept(vault) == ([], {key: "a later log line names the note without marking it done"})


def test_a_session_line_on_the_notes_branch_is_listed_not_closed(vault: Vault, vault_dir: Path):
    key = task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove")
    log(vault, vault_dir, "- [2026-09-01] Cut the low branches | branch: prune-grove | commits: 9f1e2d3 | [[orchard]]")
    reason = "the log line names the branch its commits were made on, not a push of it"
    assert swept(vault) == ([], {key: reason})
    assert parse(vault.backend.get(key)[0])[0]["status"] == "active"


def test_a_push_line_with_its_branch_field_closes_the_note(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove")
    log(vault, vault_dir, "- [2026-09-03] prune-grove: Cut | branch: prune-grove | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault)[0] == ["Projects/orchard-prune.md"]


def test_a_done_line_written_on_the_notes_branch_closes_the_note(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove")
    log(
        vault,
        vault_dir,
        "- [2026-09-03] prune-grove: Cut | branch: prune-grove | commits: 9f1e2d3 | [[orchard]]",
        "- [2026-09-03] done [[orchard-prune]] | branch: prune-grove | commits: - | [[orchard]]",
    )
    assert swept(vault)[0] == ["Projects/orchard-prune.md"]


def test_a_line_starting_with_the_branch_written_on_another_branch_is_not_a_push(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove")
    log(vault, vault_dir, "- [2026-09-03] prune-grove: still cutting | branch: main | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault) == ([], {})


def test_work_on_the_branch_after_its_push_keeps_the_note_open(vault: Vault, vault_dir: Path):
    key = task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove")
    log(
        vault,
        vault_dir,
        "- [2026-09-03] prune-grove: Cut | branch: prune-grove | commits: 9f1e2d3 | [[orchard]]",
        "- [2026-09-03] Second pass | branch: prune-grove | commits: 4e5f6a7 | [[orchard]]",
    )
    assert swept(vault) == ([], {key: "a later log line shows work on the branch after the push"})


def test_a_note_whose_commit_alone_is_in_the_log_is_listed_not_closed(vault: Vault, vault_dir: Path):
    key = task(vault_dir, "orchard-prune", "Landed in 9f1e2d3.\n", day="2026-08-20", updated="2026-09-03")
    log(vault, vault_dir, "- [2026-09-03] Cut the low branches | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault) == ([], {key: "only a commit the note mentions is in the log, not the note itself"})


def test_a_note_without_evidence_is_left_open_and_unlisted(vault: Vault, vault_dir: Path):
    key = task(vault_dir, "orchard-prune", "Cut the low branches.\n", source="branch prune-grove")
    log(vault, vault_dir, "- [2026-09-03] prune-grove-extra: Something else | commits: 9f1e2d3 | [[orchard-prune-x]]")
    assert swept(vault) == ([], {})
    assert parse(vault.backend.get(key)[0])[0]["status"] == "active"


@pytest.mark.parametrize(
    ("body", "updated", "line", "reason"),
    [
        ("Cut.\n", "2026-09-05", "- [2026-09-03] prune-grove: Cut | commits: 9f1e2d3 | [[orchard]]", "changed on"),
        ("Cut.\n", None, "- [2026-09-03] prune-grove: Cut; unpushed | commits: 9f1e2d3 | [[orchard]]", "not landed"),
        ("Cut.\n", None, "- [2026-09-03] prune-grove: Cut, lokalt opushat | commits: - | [[orchard]]", "not landed"),
        ("- [ ] Which saw?\n", None, "- [2026-09-03] prune-grove: Cut | commits: 9f1e2d3 | [[orchard]]", "unchecked"),
    ],
)
def test_an_unsure_note_is_listed_and_left_open(vault: Vault, vault_dir: Path, body, updated, line, reason):
    key = task(vault_dir, "orchard-prune", body, source="branch prune-grove", updated=updated)
    log(vault, vault_dir, line)
    closed, uncertain = swept(vault)
    assert closed == [] and reason in uncertain[key]
    assert parse(vault.backend.get(key)[0])[0]["status"] == "active"


def test_a_note_whose_stem_is_only_the_lines_area_is_not_named(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-prune", "Cut.\n")
    log(vault, vault_dir, "- [2026-09-03] Something else | commits: 9f1e2d3 | [[orchard-prune]]")
    assert swept(vault) == ([], {})


def test_a_note_changed_the_day_a_log_line_marks_it_done_is_closed(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-graft", "Grafted.\n", day="2026-08-20", updated="2026-09-03")
    log(vault, vault_dir, "- [2026-09-03] done [[orchard-graft]]: Grafted | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault) == (["Projects/orchard-graft.md"], {})


def test_a_note_another_machine_holds_is_not_touched(vault: Vault, vault_dir: Path):
    key = task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove")
    vault.index.sync(force=True)
    tasks.claim_note(vault, key, "laptop", "prune-grove", "orchard")
    before = vault.backend.get(key)
    log(vault, vault_dir, "- [2099-01-01] prune-grove: Cut | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault) == ([], {})
    assert vault.backend.get(key) == before


def test_a_note_claimed_after_the_index_read_is_listed_not_closed(vault: Vault, vault_dir: Path):
    key = task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove")
    log(vault, vault_dir, "- [2026-09-03] prune-grove: Cut | commits: 9f1e2d3 | [[orchard]]")
    claim = "kebbie:\n  machine: laptop\n  task: prune-grove\n  repo: orchard\n  since: 2026-09-04\n"
    held = (vault_dir / key).read_text(encoding="utf-8").replace("kind: task\n", "kind: task\n" + claim)
    (vault_dir / key).write_text(held, encoding="utf-8")
    closed, uncertain = swept(vault)
    assert closed == [] and "held by kebbie" in uncertain[key]
    assert (vault_dir / key).read_text(encoding="utf-8") == held


def test_a_note_close_refuses_is_listed_and_the_sweep_goes_on(vault: Vault, vault_dir: Path):
    schema = vault_dir / ".vault" / "schema.yml"
    schema.write_text(schema.read_text(encoding="utf-8") + "bundle:\n  guard:\n    secrets: true\n", encoding="utf-8")
    vault = open_vault()
    secret = "export DEPLOY_TOKEN=Zx81kQp0vLm2Rt7sYw4N\n"
    leaky = task(vault_dir, "orchard-graft", secret, source="branch graft-rows")
    task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove")
    log(
        vault,
        vault_dir,
        "- [2026-09-03] graft-rows: Grafted | commits: 1a2b3c4 | [[orchard]]",
        "- [2026-09-03] prune-grove: Cut | commits: 9f1e2d3 | [[orchard]]",
    )
    closed, uncertain = swept(vault)
    assert closed == ["Projects/orchard-prune.md"] and "close refused it" in uncertain[leaky]
    assert (vault_dir / leaky).read_text(encoding="utf-8").endswith(secret)
    vault.close()


def test_a_period_page_is_not_evidence(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-prune", "Cut.\n")
    log(vault, vault_dir, "- [2026-08-01] Something | commits: - | [[orchard]]")
    page = LOG_HEAD + "- [2026-09-03] Notes: [[orchard-prune]]\n"
    (vault_dir / "Log" / "orchard-2026-09.md").write_text(page, encoding="utf-8")
    vault.index.sync(force=True)
    assert swept(vault) == ([], {})


def test_lint_close_done_reports_closed_and_unsure_notes(vault: Vault, vault_dir: Path, capsys):
    task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove")
    task(vault_dir, "orchard-graft", "- [ ] Which rootstock?\n", source="branch graft-rows")
    log(
        vault,
        vault_dir,
        "- [2026-09-03] prune-grove: Cut | commits: 9f1e2d3 | [[orchard]]",
        "- [2026-09-03] graft-rows: Grafted | commits: 1a2b3c4 | [[orchard]]",
    )
    assert main(["lint", "--close-done", "--write", "Log/lint.md"]) == 0
    out = capsys.readouterr().out
    assert "## closed_done (1)\n- Projects/orchard-prune.md: Log/orchard-log.md: - [2026-09-03] prune-grove" in out
    assert "## done_uncertain (1)\n- Projects/orchard-graft.md: the note still has unchecked items" in out
    assert "## done_uncertain" in (vault_dir / "Log" / "lint.md").read_text(encoding="utf-8")
    with pytest.raises(NotFound):
        vault.backend.get("Projects/orchard-prune.md")


def test_a_push_line_in_another_repos_log_leaves_the_note_alone(vault: Vault, vault_dir: Path):
    key = task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove")
    (vault_dir / "Log" / "cider-log.md").write_text(
        LOG_HEAD + "- [2026-09-03] prune-grove: Pressed | branch: prune-grove | commits: 9f1e2d3 | [[orchard]]\n",
        encoding="utf-8",
    )
    vault.index.sync(force=True)
    assert swept(vault) == ([], {})
    assert parse(vault.backend.get(key)[0])[0]["status"] == "active"


def test_a_note_naming_no_repo_is_listed_not_closed_by_its_branch(vault: Vault, vault_dir: Path):
    key = task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove", path=None)
    log(vault, vault_dir, "- [2026-09-03] prune-grove: Cut | branch: prune-grove | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault) == ([], {key: "the note names no repo, so the line with its branch may be another repo's"})


def test_a_line_starting_with_a_one_word_branch_is_listed_not_closed(vault: Vault, vault_dir: Path):
    key = task(vault_dir, "orchard-prune", "Cut.\n", source="branch fix")
    log(vault, vault_dir, "- [2026-09-03] fix: typo in the grove map | commits: 9f1e2d3 | [[orchard]]")
    reason = "the branch fix is one word, which a log line may start with for another reason"
    assert swept(vault) == ([], {key: reason})


def test_a_one_word_branchs_push_line_with_its_branch_field_closes_the_note(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-prune", "Cut.\n", source="branch fix")
    log(vault, vault_dir, "- [2026-09-03] fix: Cut | branch: fix | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault)[0] == ["Projects/orchard-prune.md"]


def test_a_note_whose_path_is_a_folder_in_its_repo_closes_on_the_repos_push_line(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove", path="~/projects/orchard/rows/north")
    log(vault, vault_dir, "- [2026-09-03] prune-grove: Cut | branch: prune-grove | commits: 9f1e2d3 | [[orchard]]")
    assert swept(vault)[0] == ["Projects/orchard-prune.md"]


def test_a_done_line_on_the_branch_closes_a_note_naming_no_repo(vault: Vault, vault_dir: Path):
    task(vault_dir, "orchard-prune", "Cut.\n", source="branch prune-grove", path=None)
    log(vault, vault_dir, "- [2026-09-03] done [[orchard-prune]] | branch: prune-grove | commits: - | [[orchard]]")
    assert swept(vault)[0] == ["Projects/orchard-prune.md"]
