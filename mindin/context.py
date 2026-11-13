from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field

from mindin.backends import NotFound
from mindin.backlog import priority_rank
from mindin.frontmatter import FRONTMATTER_RE
from mindin.index import Note, expand_home
from mindin.notes import BACKLOG_STATUS, OPEN_STATUSES, fetch, note_key
from mindin.schema import NoHome, broken_schema_lines
from mindin.search import age_days, humanize_age
from mindin.vault import Vault

CONTEXT_TRUNCATE = 12000
TRUNCATED_MARKER = "… (truncated, read_file for the rest)"
LOG_TAIL_LINES = 20
TRIAGE_HINT = (
    "Settle each before new work: close(path) when it is done, append_file(path, a dated line saying what "
    'is still open) when it is still moving, set_status(path, "backlog", priority) to park it.'
)
ORGANISATION_PAGE_RULE = (
    "An organisation page is a lead, weighed below the code and the user's latest decision: when one contradicts a "
    "new decision, point that out and propose an updated page, and write it only on the user's explicit yes."
)


def path_overlap(note: Note, target: str | None) -> int:
    if not target:
        return 0
    wanted = expand_home(target).rstrip("/")
    best = 0
    for raw in note.paths:
        candidate = expand_home(raw).rstrip("/")
        if not candidate:
            continue
        if wanted.startswith(candidate) or candidate.startswith(wanted):
            best = max(best, len(os.path.commonprefix([wanted, candidate])))
    return best


def mentions_repo(note: Note, repo: str | None) -> bool:
    if not repo:
        return False
    needle = repo.lower()
    haystack = [note.title.lower(), note.stem.lower(), *[tag.lower() for tag in note.tags]]
    return any(needle in item for item in haystack)


def _relevant(note: Note, path: str | None, repo: str | None) -> bool:
    return path_overlap(note, path) > 0 or mentions_repo(note, repo)


@dataclass(frozen=True)
class RepoPage:
    vault: str
    key: str
    title: str
    line: str


@dataclass
class ContextBundle:
    system: list[tuple[Note, str]] = field(default_factory=list)
    system_rows: list[Note] = field(default_factory=list)
    tasks: list[Note] = field(default_factory=list)
    triage: list[Note] = field(default_factory=list)
    backlog: list[Note] = field(default_factory=list)
    backlog_total: int = 0
    references: list[Note] = field(default_factory=list)
    log_tail: list[str] = field(default_factory=list)
    repo: str | None = None
    stale_after_days: int = 30
    schema_problems: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    shared: bool = False
    organisation_pages: list[RepoPage] = field(default_factory=list)

    def render(self) -> str:
        now = time.time()
        blocks = list(self.notices)
        if self.schema_problems:
            blocks.append("## schema\n" + "\n".join(self.schema_problems))
        if self.system:
            blocks.append("## system notes\n" + "\n\n".join(f"### {note.key}\n{text}" for note, text in self.system))
        if self.system_rows:
            rows = [
                f"{note.key} | {note.title} | {humanize_age(age_days(note, now))} | {note.status}"
                for note in self.system_rows
            ]
            blocks.append("## other system notes\n" + "\n".join(rows))
        if self.organisation_pages:
            rows = [f"{page.vault} | {page.key} | {page.title} | {page.line}" for page in self.organisation_pages]
            blocks.append(
                "## organisation pages\n" + "\n".join(rows) + "\nEach row starts with its vault: "
                f"read one with read_file(path, vault=<vault>). {ORGANISATION_PAGE_RULE}"
            )
        if self.tasks:
            rows = [_task_row(note, now) for note in self.tasks]
            blocks.append("## open tasks\n" + "\n".join(rows))
        if self.triage:
            rows = [_task_row(note, now) for note in self.triage]
            blocks.append(
                f"## triage — {len(self.triage)} open notes older than {self.stale_after_days} days\n"
                + "\n".join(rows)
                + "\n"
                + TRIAGE_HINT
            )
        if self.backlog:
            rows = [f"{note.key} | {note.title} | {note.priority or '-'}" for note in self.backlog]
            blocks.append(f"## backlog ({self.backlog_total})\n" + "\n".join(rows))
        if self.references:
            rows = [f"{note.key} | {note.title} | {humanize_age(age_days(note, now))}" for note in self.references]
            blocks.append("## reference notes\n" + "\n".join(rows))
        if self.log_tail:
            blocks.append(f"## log — {self.repo}\n" + "\n".join(self.log_tail))
        if not blocks:
            return "No vault context for this path or repo."
        return "\n\n".join(blocks)

    def as_dict(self) -> dict:
        now = time.time()
        return {
            "text": self.render(),
            "system": [
                {"key": note.key, "stem": note.stem, "updated": note.updated, "paths": note.paths, "tags": note.tags}
                for note, _ in self.system
            ],
            "triage": [{"key": note.key, "age": humanize_age(age_days(note, now))} for note in self.triage],
            "shared": self.shared,
            "organisation_pages": [asdict(page) for page in self.organisation_pages],
        }

    def view(self) -> dict:
        now = time.time()
        return {
            "repo": self.repo,
            "shared": self.shared,
            "notices": list(self.notices),
            "schema_problems": list(self.schema_problems),
            "system": [note_fields(note, now) for note, _ in self.system],
            "tasks": [note_fields(note, now) for note in self.tasks],
            "triage": [note_fields(note, now) for note in self.triage],
            "backlog": [note_fields(note, now) for note in self.backlog],
            "backlog_total": self.backlog_total,
            "references": [note_fields(note, now) for note in self.references],
            "log_tail": list(self.log_tail),
            "organisation_pages": [asdict(page) for page in self.organisation_pages],
        }


def note_fields(note: Note, now: float) -> dict:
    return {
        "key": note.key,
        "stem": note.stem,
        "title": note.title,
        "kind": note.kind,
        "status": note.status,
        "priority": note.priority,
        "area": note.area,
        "summary": note.summary,
        "tags": note.tags,
        "paths": note.paths,
        "updated": note.updated,
        "age_days": int(age_days(note, now)),
    }


def _claim_text(note: Note) -> str:
    claim = json.loads(note.claim) if note.claim else None
    if not claim:
        return ""
    return f" | worked on at {claim['machine']} · {claim['task']} since {claim['since'][:10]}"


def _task_row(note: Note, now: float) -> str:
    return f"{note.key} | {note.title} | {humanize_age(age_days(note, now))} | {note.status}{_claim_text(note)}"


def _truncate(text: str) -> str:
    if len(text) <= CONTEXT_TRUNCATE:
        return text
    return text[:CONTEXT_TRUNCATE] + "\n" + TRUNCATED_MARKER


def _system_rank(note: Note, path: str | None, repo: str | None) -> tuple:
    target = expand_home(path).rstrip("/") if path else ""
    exact = any(expand_home(p).rstrip("/") == target for p in note.paths) if target else False
    return (not exact, note.stem != repo, note.status != "active", -path_overlap(note, path), note.key)


def unread(key: str, exc: Exception) -> str:
    return f"{key} could not be read, so it is left out: {' '.join(str(exc).split()) or type(exc).__name__}"


def _system_notes(
    vault: Vault, notes: list[Note], path: str | None, repo: str | None, notices: list[str]
) -> tuple[list[tuple[Note, str]], list[Note]]:
    folders = set(vault.schema.system_folders)
    system = [n for n in notes if n.kind == "system" or vault.schema.folder_of(n.key) in folders]
    candidates = [n for n in system if _relevant(n, path, repo)]
    candidates.sort(key=lambda note: _system_rank(note, path, repo))
    hub = next((n for n in candidates if n.stem == repo), None)
    floor = path_overlap(hub, path) if hub else -1
    full = ([hub] if hub else []) + [n for n in candidates if n is not hub and path_overlap(n, path) > floor]
    full = full[:3]
    loaded = []
    for note in full:
        try:
            text, _ = fetch(vault, note.key)
        except NotFound:
            continue
        except Exception as exc:
            # Named in the notices and left out: one unreadable note must not cost the session its context.
            notices.append(unread(note.key, exc))
            continue
        loaded.append((note, _truncate(text)))
    rows = [n for n in candidates if n not in full][:15]
    return loaded, rows


def _log_tail(vault: Vault, repo: str | None, notices: list[str]) -> list[str]:
    if not repo:
        return []
    try:
        key = note_key(vault.schema.log_key(repo))
    except (NotFound, NoHome):
        return []
    try:
        text, _ = fetch(vault, key)
    except NotFound:
        return []
    except Exception as exc:
        notices.append(unread(key, exc))
        return []
    lines = [line for line in FRONTMATTER_RE.sub("", text, count=1).splitlines() if line.strip()]
    return lines[-LOG_TAIL_LINES:]


def context(
    vault: Vault,
    path: str | None = None,
    repo: str | None = None,
    query: str | None = None,
    limit: int = 10,
) -> ContextBundle:
    schema = vault.schema
    notes = [note for note in vault.index.all_notes() if note.valid]
    task_folders = set(schema.task_folders)
    reference_folders = set(schema.reference_folders)
    needle = (query or "").lower()
    folder = {note.key: schema.folder_of(note.key) for note in notes}

    tasks = [
        note
        for note in notes
        if folder[note.key] in task_folders and note.status in OPEN_STATUSES and _relevant(note, path, repo)
    ]
    references = [
        note
        for note in notes
        if folder[note.key] in reference_folders
        and (_relevant(note, path, repo) or (needle and needle in f"{note.title} {' '.join(note.tags)}".lower()))
    ]
    backlog_rows = [
        note
        for note in notes
        if folder[note.key] in task_folders and note.status == BACKLOG_STATUS and _relevant(note, path, repo)
    ]
    tasks.sort(key=lambda note: note.updated, reverse=True)
    references.sort(key=lambda note: note.updated, reverse=True)
    backlog_rows.sort(key=lambda note: note.updated, reverse=True)
    backlog_rows.sort(key=lambda note: priority_rank(note.priority))
    now = time.time()
    stale_after = vault.schema.stale_after_days
    triage = [note for note in tasks if age_days(note, now) > stale_after]
    fresh = [note for note in tasks if age_days(note, now) <= stale_after]
    notices: list[str] = []
    system, system_rows = _system_notes(vault, notes, path, repo, notices)
    log_tail = _log_tail(vault, repo, notices)
    return ContextBundle(
        system=system,
        system_rows=system_rows,
        tasks=fresh[:limit],
        triage=triage[:limit],
        backlog=backlog_rows[:limit],
        backlog_total=len(backlog_rows),
        references=references[:limit],
        log_tail=log_tail,
        repo=repo,
        stale_after_days=vault.schema.stale_after_days,
        schema_problems=broken_schema_lines(vault.schema),
        notices=notices,
    )
