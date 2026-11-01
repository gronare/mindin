from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from notes_vault_mcp.backends import NotFound
from notes_vault_mcp.frontmatter import dump, parse
from notes_vault_mcp.git import git, is_checkout, repo_name
from notes_vault_mcp.index import Note, stem_of
from notes_vault_mcp.notes import _reindex, changelog_notes, log_lines, note_key, stamp
from notes_vault_mcp.vault import Vault

GENERATED_START = "<!-- changelog:generated -->"
GENERATED_END = "<!-- /changelog:generated -->"
PREVIOUS_MONTH_GRACE_DAYS = 7
PERIOD_RE = re.compile(r"^\d{4}(-(0[1-9]|1[0-2]))?$")


class BadPeriod(ValueError):
    pass


def period_bounds(period: str) -> tuple[str, str]:
    if not PERIOD_RE.match(period):
        raise BadPeriod(f"period {period!r} is not YYYY or YYYY-MM")
    if len(period) == 4:
        year = int(period)
        return f"{year}-01-01", f"{year + 1}-01-01"
    year, month = (int(part) for part in period.split("-"))
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year}-{month:02d}-01", f"{end_year}-{end_month:02d}-01"


def periods_due(on: date) -> list[str]:
    current = on.strftime("%Y-%m")
    if on.day > PREVIOUS_MONTH_GRACE_DAYS:
        return [current]
    previous = date(on.year - 1, 12, 1) if on.month == 1 else date(on.year, on.month - 1, 1)
    return [previous.strftime("%Y-%m"), current]


def git_commits(repo_path: Path, period: str) -> dict[str, list[str]]:
    since, until = period_bounds(period)
    window = [f"--since={since}T00:00:00", f"--until={until}T00:00:00"]
    output = git(["log", *window, "--format=%h|%ad|%s", "--date=short"], repo_path)
    by_day: dict[str, list[str]] = defaultdict(list)
    for line in output.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            by_day[parts[1]].append(f"{parts[0]} {parts[2]}")
    return dict(sorted(by_day.items()))


def _belongs(note: Note, repo: str) -> bool:
    if note.kind == "log":
        return False
    return repo in note.tags or stem_of(note.area.strip("[]").split("|")[0]) == repo


CommitsByDay = dict[str, list[str]]


def commits_of(repo_path: Path | None, period: str, commits: CommitsByDay | None) -> CommitsByDay | None:
    if commits is not None or repo_path is None:
        return commits
    return git_commits(repo_path, period)


def sections(vault: Vault, repo: str, period: str, by_day: CommitsByDay | None) -> list[str]:
    blocks = []
    lines = log_lines(vault, repo, period)
    blocks.append("## log\n" + ("\n".join(lines) if lines else "- (no log lines)"))
    if by_day is not None:
        if by_day:
            days = [f"### {day}\n" + "\n".join(f"- {entry}" for entry in entries) for day, entries in by_day.items()]
            blocks.append("## commits\n" + "\n".join(days))
        else:
            blocks.append("## commits\n- (no commits)")
    notes = sorted(
        (note for note in changelog_notes(vault, period) if _belongs(note, repo)),
        key=lambda note: (note.date, note.key),
    )
    if notes:
        rows = [f"- [[{note.stem}]] — {note.title} ({note.date}, {note.folder})" for note in notes]
        blocks.append("## notes\n" + "\n".join(rows))
    else:
        blocks.append("## notes\n- (no notes)")
    return blocks


def render(
    vault: Vault, repo: str, period: str, repo_path: Path | None = None, commits: CommitsByDay | None = None
) -> str:
    by_day = commits_of(repo_path, period, commits)
    return "\n\n".join([f"# {repo} — {period}", *sections(vault, repo, period, by_day)])


def generated_block(vault: Vault, repo: str, period: str, by_day: CommitsByDay | None) -> str:
    return "\n".join([GENERATED_START, "\n\n".join(sections(vault, repo, period, by_day)), GENERATED_END])


def splice(body: str, block: str) -> str:
    start = body.find(GENERATED_START)
    end = body.find(GENERATED_END)
    if start != -1 and end > start:
        return body[:start] + block + body[end + len(GENERATED_END) :]
    return f"{body.rstrip()}\n\n{block}\n" if body.strip() else f"{block}\n"


def _log_area(vault: Vault, repo: str) -> str:
    note = vault.index.note(vault.schema.log_key(repo))
    return note.area if note and note.area else f"[[{repo}]]"


def _new_page(vault: Vault, repo: str, period: str) -> dict:
    return {
        "title": f"{repo} — {period}",
        "date": date.fromisoformat(period_bounds(period)[0]),
        "updated": stamp(),
        "tags": ["log", repo],
        "status": "active",
        "kind": "log",
        "area": _log_area(vault, repo),
    }


def period_over(period: str, on: date | None = None) -> bool:
    return (on or date.today()).isoformat() >= period_bounds(period)[1]


def write_page(
    vault: Vault, repo: str, period: str, repo_path: Path | None = None, commits: CommitsByDay | None = None
) -> bool:
    period_bounds(period)
    key = note_key(vault.schema.period_key(repo, period))
    try:
        text, _ = vault.backend.get(key)
        frontmatter, body = parse(text)
        existed = True
    except NotFound:
        frontmatter, body, existed = _new_page(vault, repo, period), "", False
    new_body = splice(body, generated_block(vault, repo, period, commits_of(repo_path, period, commits)))
    if existed and new_body == body:
        return False
    frontmatter["updated"] = stamp()
    frontmatter["status"] = "complete" if period_over(period) else "active"
    out = dump(frontmatter, new_body)
    _reindex(vault, key, out, vault.backend.put(key, out))
    return True


def live_repos(vault: Vault) -> list[tuple[str, str]]:
    """Forgets repos whose root is gone, or that were remembered under a worktree directory's name."""
    live = []
    for repo, root in vault.index.known_repos():
        if Path(root).exists() and repo_name(root) == repo:
            live.append((repo, root))
        else:
            vault.index.forget_repo(repo)
    return live


def write_all(vault: Vault, periods: list[str] | None = None, repos: list[tuple[str, str]] | None = None) -> list[str]:
    if vault.schema.log_folder is None:
        return []
    written = []
    for repo, root in repos if repos is not None else live_repos(vault):
        path = Path(root)
        if not is_checkout(path):
            continue
        for period in periods or periods_due(date.today()):
            if write_page(vault, repo, period, path):
                written.append(vault.schema.period_key(repo, period))
    return written
