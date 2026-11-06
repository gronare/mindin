from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from mindin import notes
from mindin.cli import main
from mindin.vault import Vault

DAY = timedelta(days=1)


def run(capsys: pytest.CaptureFixture, *argv: str) -> str:
    assert main(list(argv)) == 0, capsys.readouterr().err
    return capsys.readouterr().out


def days_ago(days: int) -> str:
    return (date.today() - days * DAY).isoformat()


def with_backlog(vault: Vault) -> None:
    notes.backlog_add(vault, "Graft the pear rootstock", "orchard", "Before the sap rises", priority="high")
    notes.backlog_add(vault, "Sharpen the lathe chisels", "workshop", "They tear the grain", priority="low")


SEARCH_KARNAN = (
    "3 of 3 (archive: 0 hidden, superseded: 0 hidden)\n"
    "Areas/orchard.md | orchard | 5d | active | - | Kontrollplanet som styr kärnan, lunden och sajternas pinnar. "
    "Varje sajt pinnar tre repon. Doman och DNS hanteras centralt, och en…\n"
    "Projects/orchard-fresh.md | Orchard: bokningswidget i checkout | 5d | active | [[orchard]] | Bokningswidget ska "
    "renderas i checkout och hämta slots från kärnan. Widget-koden bor i frame-addons och måste följa samma…\n"
    "Projects/orchard-draft.md | Orchard: översyn av releaser | 5d | draft | [[orchard]] | …Kopplar ihop kärnan, "
    "teman och tillägg i samma körning så att en sajt kan nå målet. Se [[saknad-not]].\n"
)

SEARCH_NOTHING = (
    "0 of 0 (archive: 0 hidden, superseded: 0 hidden)\n"
    "No notes matched.\n"
    "The vault is written in sv, also en: search again with the terms translated into en.\n"
)

BACKLOG = (
    "2 backlog items\n"
    "- Projects/orchard-graft-the-pear-rootstock.md | Graft the pear rootstock | high | [[orchard]] | 0d\n"
    "- Projects/workshop-sharpen-the-lathe-chisels.md | Sharpen the lathe chisels | low | [[workshop]] | 0d\n"
)

LINT = """14 findings

## archive_status_mismatch (1)
- Archive/wrong-status.md: status active

## broken_frontmatter (1)
- Resources/broken-yaml.md: line 4: expected ',' or ']', but got ':'

## duplicate_stems (1)
- workshop: Areas/workshop.md, Resources/workshop.md

## missing_area (1)
- Resources/no-frontmatter.md: missing area: notes in Resources/ must link their system note as area: "[[stem]]"

## missing_required (5)
- Resources/no-frontmatter.md: date
- Resources/no-frontmatter.md: status
- Resources/no-frontmatter.md: tags
- Resources/no-frontmatter.md: title
- Resources/no-frontmatter.md: updated

## orphans (1)
- Resources/no-frontmatter.md

## stale_active (1)
- Projects/workshop-stale.md: 6mo old, status active

## superseded_target_missing (1)
- Resources/old-domain-howto.md -> [[gone]]

## unresolved_links (2)
- Projects/orchard-draft.md -> [[saknad-not]]
- Resources/old-domain-howto.md -> [[gone]]
"""


def test_search_in_a_pipe_prints_the_rows_as_before(vault: Vault, capsys):
    assert run(capsys, "search", "kärnan") == SEARCH_KARNAN
    assert run(capsys, "search", "nothingatallhere") == SEARCH_NOTHING


def test_search_without_a_limit_takes_the_schemas_default_limit(vault: Vault, vault_dir: Path, capsys):
    schema = vault_dir / ".vault" / "schema.yml"
    schema.write_text(schema.read_text(encoding="utf-8") + "search:\n  default_limit: 3\n", encoding="utf-8")
    default = run(capsys, "search", "orchard").splitlines()
    explicit = run(capsys, "search", "orchard", "--limit", "4").splitlines()
    assert default[0].startswith("3 of 5 ") and len(default) == 4
    assert explicit[0].startswith("4 of 5 ") and len(explicit) == 5


def test_backlog_in_a_pipe_prints_the_rows_as_before(vault: Vault, capsys):
    assert run(capsys, "backlog") == "The backlog is empty.\n"
    with_backlog(vault)
    assert run(capsys, "backlog") == BACKLOG


def test_read_in_a_pipe_prints_the_file_as_stored(vault: Vault, vault_dir: Path, capsys):
    stored = (vault_dir / "Areas/orchard.md").read_text(encoding="utf-8")
    assert run(capsys, "read", "Areas/orchard.md") == stored
    assert run(capsys, "read", "Areas/orchard.md", "--body") == stored.split("---\n", 2)[2]


def test_lint_in_a_pipe_prints_the_report_as_before(vault: Vault, capsys):
    assert run(capsys, "lint") == LINT


def in_a_terminal(monkeypatch: pytest.MonkeyPatch, width: int = 100, color: bool = False) -> None:
    monkeypatch.setattr("mindin.terminal.stdout_is_terminal", lambda: True)
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setenv("TERM", "xterm-256color")
    if color:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", "1")


def test_search_in_a_terminal_shows_title_path_and_snippet(vault: Vault, monkeypatch, capsys):
    in_a_terminal(monkeypatch, width=120)
    lines = run(capsys, "search", "kärnan").splitlines()
    assert lines[0] == "3 notes · archive and superseded hidden"
    assert lines[2].startswith("orchard ") and lines[2].endswith("system  5 days")
    assert lines[3] == "Areas/orchard.md"
    assert lines[6].startswith("Orchard: bokningswidget i checkout") and lines[6].endswith("task    5 days")
    assert lines[7] == "Projects/orchard-fresh.md · orchard"
    assert lines[-1].startswith("  …Kopplar ihop kärnan") and lines[-1].endswith("Se saknad-not.")
    assert "[[" not in "\n".join(lines)


def test_search_in_a_narrow_terminal_cuts_rows_but_never_a_path(vault: Vault, monkeypatch, capsys):
    in_a_terminal(monkeypatch, width=50)
    lines = run(capsys, "search", "kärnan").splitlines()
    paths = {"Areas/orchard.md", "Projects/orchard-fresh.md · orchard", "Projects/orchard-draft.md · orchard"}
    assert paths <= set(lines)
    assert all(len(line) <= 50 for line in lines if line not in paths)
    assert any(line.endswith("…") for line in lines if line.startswith("  "))


def test_a_note_found_by_meaning_is_marked(monkeypatch):
    from mindin.human import render_search
    from mindin.terminal import terminal

    in_a_terminal(monkeypatch)
    row = {"key": "Resources/frost-trap.md", "title": "Frost cracks the grafts", "kind": "trap", "area": "[[orchard]]"}
    found = {
        "shown": 1,
        "total": 1,
        "by_meaning": 1,
        "hidden_archive": 0,
        "hidden_superseded": 2,
        "term_count": 0,
        "best_terms": 0,
        "languages": ["en"],
        "rows": [
            row
            | {
                "age_days": 18,
                "snippet": "Wrap them [[before-winter|before winter]].",
                "section": "",
                "by_meaning": True,
            }
        ],
    }
    lines = render_search(found, terminal()).splitlines()
    assert lines[0] == "1 note · 1 found by meaning · 2 hidden in the archive and superseded"
    assert lines[2] == "Frost cracks the grafts  ≈ meaning  trap  18 days"
    assert lines[4] == "  Wrap them before winter."


def test_colour_marks_meaning_and_no_color_and_plain_drop_it(vault: Vault, monkeypatch, capsys):
    in_a_terminal(monkeypatch, color=True)
    coloured = run(capsys, "search", "kärnan")
    assert "\x1b[1morchard" in coloured and "\x1b[34msystem" in coloured
    assert run(capsys, "search", "kärnan", "--plain") == SEARCH_KARNAN
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in run(capsys, "search", "kärnan")


def test_backlog_in_a_terminal_has_one_row_per_item(vault: Vault, monkeypatch, capsys):
    with_backlog(vault)
    in_a_terminal(monkeypatch)
    assert run(capsys, "backlog") == (
        "● high  Graft the pear rootstock   orchard   today\n"
        "● low   Sharpen the lathe chisels  workshop  today\n"
        "\n"
        "2 items · --paths shows where each one lives · --area orchard filters\n"
    )
    with_paths = run(capsys, "backlog", "--paths").splitlines()
    assert with_paths[0].endswith("today  Projects/orchard-graft-the-pear-rootstock.md")
    assert with_paths[-1] == "2 items · mindin read <path> opens one · --area orchard filters"
    assert run(capsys, "backlog", "--plain") == BACKLOG


def test_a_long_backlog_is_grouped_and_stops_at_twenty(vault: Vault, monkeypatch, capsys):
    for number in range(25):
        notes.backlog_add(
            vault, f"Prune row {number:02}", "orchard", "Winter work", priority=("high", "low")[number % 2]
        )
    in_a_terminal(monkeypatch)
    lines = run(capsys, "backlog").splitlines()
    assert lines[0] == "● high · 13"
    assert lines[1].startswith("  Prune row ")
    assert "● low · 12" in lines
    assert sum(line.startswith("  Prune row") for line in lines) == 20
    assert "and 5 more · --all shows them" in lines
    assert sum(line.startswith("  Prune row") for line in run(capsys, "backlog", "--all").splitlines()) == 25


def test_read_in_a_terminal_turns_the_frontmatter_into_two_lines(vault: Vault, vault_dir: Path, monkeypatch, capsys):
    in_a_terminal(monkeypatch, width=60)
    lines = run(capsys, "read", "Areas/orchard.md").splitlines()
    assert lines[:4] == [
        "orchard",
        "system · active · updated 5 days ago · #orchard #core #grove",
        "─" * 60,
        "",
    ]
    assert lines[4] == "Kontrollplanet som styr kärnan, lunden och sajternas pinnar."
    assert lines[5] == "Varje sajt pinnar tre repon."
    assert all(len(line) <= 60 for line in lines)
    assert "title: orchard" not in lines
    stored = (vault_dir / "Areas/orchard.md").read_text(encoding="utf-8")
    assert run(capsys, "read", "Areas/orchard.md", "--raw") == stored
    assert run(capsys, "read", "Areas/orchard.md", "--plain") == stored


def test_lint_in_a_terminal_counts_each_kind_in_plain_english(vault: Vault, monkeypatch, capsys):
    in_a_terminal(monkeypatch)
    lines = run(capsys, "lint").splitlines()
    assert lines[0] == "14 findings in 9 kinds"
    assert lines[2] == "  5  missing fields       a field every note needs is absent"
    assert "  1  stale open tasks     open and untouched for more than 30 days" in lines
    assert lines[-1] == "lint --kind missing-fields lists one kind · --park-stale moves stale tasks to the backlog"
    assert run(capsys, "lint", "--kind", "unresolved-links").splitlines() == [
        "unresolved links (2) · a [[link]] with no note behind it",
        "",
        "Projects/orchard-draft.md  → saknad-not",
        "Resources/old-domain-howto.md  → gone",
    ]


def test_lint_kind_in_a_pipe_prints_that_section_only(vault: Vault, capsys):
    assert run(capsys, "lint", "--kind", "orphans") == "1 findings\n\n## orphans (1)\n- Resources/no-frontmatter.md\n"
    assert main(["lint", "--kind", "weeds"]) == 2
    assert "no finding kind named 'weeds'" in capsys.readouterr().err


def test_every_finding_kind_the_linter_writes_has_a_name_and_an_explanation():
    import inspect
    import re

    from mindin.human import LINT_KINDS, lint_kind_named

    written = set(re.findall(r'findings\.add\(\s*"(\w+)"', inspect.getsource(notes)))
    assert written and written <= set(LINT_KINDS)
    assert all(name and explanation for name, explanation in LINT_KINDS.values())
    assert all(lint_kind_named(kind) == kind for kind in LINT_KINDS)


def test_the_findings_read_back_from_their_report(vault: Vault):
    findings = notes.lint(vault)
    assert notes.Findings.parse(findings.render()).as_dict() == findings.as_dict()


ORCHARD_HOOK = """## system notes
### Areas/orchard.md
---
title: orchard
date: 2026-01-10
updated: {d5}
tags: [orchard, core, grove]
status: active
kind: system
summary: Kontrollplanet för frame-kärnan och lunden av sajter.
path: ~/projects/orchard
---

Kontrollplanet som styr kärnan, lunden och sajternas pinnar. Varje sajt pinnar tre repon.
Doman och DNS hanteras centralt, och en release flyttar lunden i fas.
Bokning, kundvy och betalning hänger ihop via samma modell. lundkod styr vilken release som gäller.
Se [[booking-trap]] för fällan i avbokningen.


## open tasks
Projects/orchard-draft.md | Orchard: översyn av releaser | 5d | draft
Projects/orchard-fresh.md | Orchard: bokningswidget i checkout | 5d | active

## reference notes
Resources/booking-trap.md | Bokning: fällan i avbokningen | 1mo

## log — orchard
- [2026-08-01] Bokningswidget bakom flagga | commits: a1b2c3d | [[orchard]]
- [2026-08-15] Release-grinden mäter A/B | commits: e4f5a6b | [[orchard]]

## how to read the vault
The notes above live in the vault, not on disk. Read a note with the vault MCP tool `read_file(path)`, find others \
with `search`, file an idea with `backlog_add`, and never look for the vault's files in the filesystem: a wikilink \
[[stem]] is `search` on the stem, not a path.
"""


def orchard_repo(tmp_path: Path) -> Path:
    from test_cli_hooks import make_repo

    return make_repo(tmp_path / "repos", "orchard")


def test_the_session_start_log_tail_holds_log_lines_only(vault: Vault, monkeypatch, tmp_path, capsys):
    from test_cli_hooks import feed

    repo = orchard_repo(tmp_path)
    feed(monkeypatch, {"cwd": str(repo)})
    assert run(capsys, "hook", "session-start") == ORCHARD_HOOK.format(d5=days_ago(5))
    assert notes.context(vault, repo="orchard").log_tail == [
        "- [2026-08-01] Bokningswidget bakom flagga | commits: a1b2c3d | [[orchard]]",
        "- [2026-08-15] Release-grinden mäter A/B | commits: e4f5a6b | [[orchard]]",
    ]


def test_context_in_a_pipe_prints_what_the_hook_prints(vault: Vault, monkeypatch, tmp_path, capsys):
    repo = orchard_repo(tmp_path)
    assert run(capsys, "context", "--path", str(repo)) == ORCHARD_HOOK.format(d5=days_ago(5))


def test_context_in_a_terminal_sums_up_the_session_start(vault: Vault, monkeypatch, tmp_path, capsys):
    repo = orchard_repo(tmp_path)
    in_a_terminal(monkeypatch)
    monkeypatch.chdir(repo)
    assert run(capsys, "context") == (
        "orchard  Kontrollplanet för frame-kärnan och lunden av sajter.\n"
        "Areas/orchard.md · updated 5 days ago\n"
        "\n"
        "Open        Orchard: översyn av releaser\n"
        "            Orchard: bokningswidget i checkout\n"
        "Traps       Bokning: fällan i avbokningen\n"
        "Last log    2026-08-15  Release-grinden mäter A/B\n"
    )
    assert run(capsys, "context", "--plain") == ORCHARD_HOOK.format(d5=days_ago(5))


def test_context_in_a_terminal_lists_the_commits_since_the_note(
    vault: Vault, vault_dir: Path, monkeypatch, tmp_path, capsys
):
    import subprocess

    from test_cli_hooks import head_sha, make_repo

    repo = make_repo(tmp_path / "repos", "frame")
    (repo / "rows.txt").write_text("north\n", encoding="utf-8")
    identity = ["-c", "user.email=t@example.com", "-c", "user.name=T"]
    subprocess.run(["git", "add", "rows.txt"], cwd=repo, check=True)
    subprocess.run(["git", *identity, "commit", "-qm", "Prune the north row"], cwd=repo, check=True)
    sha = head_sha(repo)
    (vault_dir / "Areas" / "frame.md").write_text(
        "---\ntitle: frame\ndate: 2020-01-01\nupdated: 2020-01-01\n"
        f"tags: [frame]\nstatus: active\nkind: system\npath: {repo}\n---\n\nKärnan.\n",
        encoding="utf-8",
    )
    vault.index.sync(force=True)
    in_a_terminal(monkeypatch, color=True)
    out = run(capsys, "context", "--path", str(repo))
    assert "\x1b[38;5;208m2 commits since the note\x1b[0m" in out
    monkeypatch.setenv("NO_COLOR", "1")
    lines = run(capsys, "context", "--path", str(repo)).splitlines()
    at = lines.index("2 commits since the note")
    assert lines[at + 1].startswith("  ") and "First commit" in lines[at + 1]
    assert lines[at + 2].startswith(f"  {sha}  Prune the north row") and lines[at + 2].endswith("today")
