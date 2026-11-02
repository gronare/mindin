from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from notes_vault_mcp.backends import SCHEMA_KEY, NotFound
from notes_vault_mcp.frontmatter import (
    CLAIM_KEY,
    FRONTMATTER_RE,
    FrontmatterError,
    claim_fields,
    dump,
    parse,
    validate,
)
from notes_vault_mcp.index import Note, expand_home, stem_of
from notes_vault_mcp.schema import CLAIM_STALE_DAYS, broken_schema_lines
from notes_vault_mcp.search import age_days, humanize_age
from notes_vault_mcp.vault import Vault

CONTEXT_TRUNCATE = 12000
TRUNCATED_MARKER = "… (truncated, read_file for the rest)"
LOG_TAIL_LINES = 20
OPEN_STATUSES = ("active", "draft")
BACKLOG_STATUS = "backlog"
TRIAGE_HINT = (
    "Settle each before new work: close(path) when it is done, append_file(path, a dated line saying what "
    'is still open) when it is still moving, set_status(path, "backlog", priority) to park it.'
)
PRIORITY_ORDER = ("urgent", "high", "medium", "low")
SLUG_MAX = 60


SCHEMA_REFUSAL = (
    f"{SCHEMA_KEY} is not written through the tools it checks: edit it as a file, on disk or over WebDAV, "
    "or switch layouts with init; context and lint say what is wrong with it"
)


class ValidationError(Exception):
    pass


class InvalidKey(ValidationError, NotFound):
    pass


NOTE_SUFFIX = ".md"
READABLE_SUFFIXES = (".md", ".base")


def note_key(path: str, readable: bool = False) -> str:
    if readable and path == SCHEMA_KEY:
        return path
    if not path or "\x00" in path or "\\" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise InvalidKey(f"not a vault path: {path!r}")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise InvalidKey(f"not a vault path: {path!r} (empty, '.' and '..' segments are refused)")
    if path == SCHEMA_KEY:
        raise InvalidKey(SCHEMA_REFUSAL)
    if any(segment.startswith(".") for segment in segments):
        raise InvalidKey(f"not a vault path: {path!r} (dot-folders and dot-files are outside the note set)")
    if not path.endswith(READABLE_SUFFIXES if readable else NOTE_SUFFIX):
        suffixes = " or ".join(READABLE_SUFFIXES) if readable else NOTE_SUFFIX
        raise InvalidKey(f"not a note: {path!r} (a note path ends in {suffixes})")
    return path


def today() -> str:
    return date.today().isoformat()


def stamp() -> date:
    return date.today()


def _read(vault: Vault, path: str) -> tuple[str, str]:
    return vault.backend.get(path)


def _reindex(vault: Vault, path: str, text: str, version: str) -> None:
    vault.index.upsert(path, text, version, time.time(), len(text.encode("utf-8")))


def read(vault: Vault, path: str) -> str:
    text, version = _read(vault, note_key(path, readable=True))
    frontmatter, body = parse(text)
    superseded_by = frontmatter.get("superseded_by")
    if superseded_by:
        body = f"> [!warning] Superseded by {superseded_by}\n\n{body.lstrip()}"
        text = dump(frontmatter, body)
    return f"etag: {version}\n{text}"


def _with_dates(frontmatter: dict) -> dict:
    stamped = dict(frontmatter)
    stamped["updated"] = stamp()
    if not stamped.get("date"):
        ordered: dict = {}
        for key, value in stamped.items():
            ordered[key] = value
            if key == "title":
                ordered["date"] = stamp()
        ordered.setdefault("date", stamp())
        stamped = ordered
    return stamped


def _require_valid(vault: Vault, frontmatter: dict, key: str, verdict: str) -> None:
    if vault.rules is not None:
        vault.rules.check_note(key, frontmatter)
    problems = validate(frontmatter, key, vault.schema)
    if problems:
        raise ValidationError("\n".join([f"{key} {verdict}:", *[f"- {p}" for p in problems]]))


def _stored_claim(vault: Vault, key: str) -> dict[str, str] | None:
    try:
        text, _ = _read(vault, key)
        return claim_fields(parse(text)[0].get(CLAIM_KEY))
    except (NotFound, FrontmatterError):
        return None


def _with_claim(frontmatter: dict, claim: dict[str, str] | None) -> dict:
    kept = {key: value for key, value in frontmatter.items() if key != CLAIM_KEY}
    if claim is not None:
        kept[CLAIM_KEY] = claim
    return kept


def write(vault: Vault, path: str, content: str, expected_etag: str | None = None) -> str:
    key = note_key(path)
    frontmatter, body = parse(content)
    stamped = _with_dates(_with_claim(frontmatter, _stored_claim(vault, key)))
    _require_valid(vault, stamped, key, "is not a valid note")
    text = dump(stamped, body)
    version = vault.backend.put(key, text, expected_version=expected_etag)
    _reindex(vault, key, text, version)
    return f"Written: {key}\netag: {version}"


def append(vault: Vault, path: str, content: str) -> str:
    key = note_key(path)
    try:
        existing, _ = _read(vault, key)
    except NotFound as exc:
        raise NotFound(f"{key} (append_file extends a note; write_file creates one)") from exc
    frontmatter, body = parse(existing)
    stamped = _with_dates(frontmatter)
    _require_valid(vault, stamped, key, "would not be a valid note")
    body = f"{body.rstrip()}\n{content}\n" if body.strip() else f"{content}\n"
    text = dump(stamped, body)
    version = vault.backend.put(key, text)
    _reindex(vault, key, text, version)
    return f"Appended to: {key}"


def move(vault: Vault, src: str, dst: str) -> str:
    source, target = note_key(src), note_key(dst)
    text, _ = _read(vault, source)
    frontmatter, _ = parse(text)
    _require_valid(vault, frontmatter, target, "would not be a valid note")
    vault.backend.move(source, target)
    text, version = _read(vault, target)
    vault.index.remove(source)
    _reindex(vault, target, text, version)
    return f"Moved: {source} -> {target}"


def delete(vault: Vault, path: str) -> str:
    key = note_key(path)
    vault.backend.delete(key)
    vault.index.remove(key)
    return f"Deleted: {key}"


def _free_key(vault: Vault, folder: str, basename: str) -> str:
    stem = basename.removesuffix(".md")
    candidate = f"{folder}/{stem}.md"
    suffix = 2
    taken = {entry.key for entry in vault.backend.list()}
    while candidate in taken:
        candidate = f"{folder}/{stem}-{suffix}.md"
        suffix += 1
    return candidate


def set_status(vault: Vault, path: str, status: str, priority: str | None = None, source: str | None = None) -> str:
    path = note_key(path)
    text, _ = _read(vault, path)
    frontmatter, body = parse(text)
    frontmatter["status"] = status
    if status == BACKLOG_STATUS:
        frontmatter.pop(CLAIM_KEY, None)
    if priority:
        frontmatter["priority"] = priority
    if source:
        frontmatter["source"] = source
    stamped = _with_dates(frontmatter)
    _require_valid(vault, stamped, path, "would not be a valid note")
    out = dump(stamped, body)
    _reindex(vault, path, out, vault.backend.put(path, out))
    return f"{path}: status {status}" + (f", priority {priority}" if priority else "")


def put_frontmatter(vault: Vault, path: str, frontmatter: dict, body: str, expected_version: str) -> str:
    stamped = _with_dates(frontmatter)
    _require_valid(vault, stamped, path, "would not be a valid note")
    out = dump(stamped, body)
    version = vault.backend.put(path, out, expected_version=expected_version)
    _reindex(vault, path, out, version)
    return version


def _day(text: str) -> date | None:
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def claim_is_fresh(note: Note, today: date | None = None) -> bool:
    if not note.claim:
        return False
    days = [day for day in (_day(json.loads(note.claim)["since"]), _day(note.updated)) if day is not None]
    return bool(days) and ((today or date.today()) - max(days)).days <= CLAIM_STALE_DAYS


def park_stale(vault: Vault) -> list[str]:
    now = time.time()
    task_folders = set(vault.schema.task_folders)
    parked = []
    for note in vault.index.all_notes():
        in_tasks = vault.schema.folder_of(note.key) in task_folders
        if not note.valid or not in_tasks or note.status not in OPEN_STATUSES:
            continue
        if age_days(note, now) <= vault.schema.stale_after_days or claim_is_fresh(note):
            continue
        set_status(
            vault,
            note.key,
            BACKLOG_STATUS,
            priority=note.priority or "low",
            source=note.source or f"veckolint {today()}",
        )
        parked.append(note.key)
    return parked


def close(vault: Vault, path: str, merged_into: str | None = None, status: str | None = None) -> str:
    path = note_key(path)
    text, _ = _read(vault, path)
    frontmatter, body = parse(text)
    if merged_into:
        frontmatter["status"] = "superseded"
        frontmatter["superseded_by"] = f"[[{stem_of(merged_into)}]]"
    else:
        frontmatter["status"] = status or "complete"
    frontmatter.pop(CLAIM_KEY, None)
    archive = vault.schema.archive_folder
    archived = archive is None or vault.schema.under(path, [archive])
    destination = path if archived else _free_key(vault, archive, path.rsplit("/", 1)[-1])
    stamped = _with_dates(frontmatter)
    _require_valid(vault, stamped, destination, "cannot be closed into a valid note")
    out = dump(stamped, body)
    version = vault.backend.put(destination, out)
    if not archived:
        vault.backend.delete(path)
        vault.index.remove(path)
    _reindex(vault, destination, out, version)
    return destination


def _area_link(area: str | None, repo: str) -> str:
    stem = (area or repo).strip()
    return stem if stem.startswith("[[") else f"[[{stem}]]"


def _new_log(repo: str, area: str | None) -> tuple[dict, str]:
    frontmatter = {
        "title": f"Log — {repo}",
        "date": stamp(),
        "updated": stamp(),
        "tags": ["log", repo],
        "status": "active",
        "kind": "log",
        "area": _area_link(area, repo),
    }
    return frontmatter, ""


def log_append(
    vault: Vault,
    repo: str,
    line: str,
    commits: tuple[str, ...] = (),
    area: str | None = None,
    branch: str | None = None,
) -> str:
    key = note_key(vault.schema.log_key(repo))
    try:
        existing, _ = _read(vault, key)
        frontmatter, body = parse(existing)
    except NotFound:
        frontmatter, body = _new_log(repo, area)
    if area:
        frontmatter["area"] = _area_link(area, repo)
    frontmatter["updated"] = stamp()
    entry = vault.schema.log_entry_format.format(
        date=today(),
        line=line,
        commits=" ".join(commits) if commits else "-",
        branch=branch or "-",
        area=frontmatter.get("area") or "-",
    )
    body = f"{body.rstrip()}\n{entry}\n" if body.strip() else f"{entry}\n"
    text = dump(frontmatter, body)
    version = vault.backend.put(key, text)
    _reindex(vault, key, text, version)
    return f"Logged to {key}:\n{entry}"


def priority_rank(value: str) -> int:
    return PRIORITY_ORDER.index(value) if value in PRIORITY_ORDER else len(PRIORITY_ORDER)


def slugify(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    if len(slug) <= SLUG_MAX:
        return slug
    head = slug[: SLUG_MAX + 1]
    return head.rsplit("-", 1)[0] if "-" in head else head[:SLUG_MAX]


def _title_slug(title: str) -> str:
    return slugify(title) or re.sub(r"[\W_]+", "-", title.casefold()).strip("-")[:SLUG_MAX] or "untitled"


def _area_stem(area: str) -> str:
    return stem_of(area.strip().strip("[]").split("|")[0].strip())


def backlog_add(
    vault: Vault,
    title: str,
    area: str,
    line: str,
    priority: str | None = None,
    source: str | None = None,
) -> str:
    area_stem = _area_stem(area)
    if not area_stem:
        raise ValueError("area is required: the stem of the system note, or [[stem]]")
    family = area_stem.split("-")[0]
    slug = _title_slug(title)
    stem = slug if slug == family or slug.startswith(f"{family}-") else f"{family}-{slug}"
    key = note_key(_free_key(vault, vault.schema.backlog_folder, stem))
    frontmatter: dict = {
        "title": title,
        "date": stamp(),
        "updated": stamp(),
        "tags": [family],
        "status": BACKLOG_STATUS,
        "kind": "task",
        "area": f"[[{area_stem}]]",
        "summary": line,
    }
    if priority:
        frontmatter["priority"] = priority
    if source:
        frontmatter["source"] = source
    problems = validate(frontmatter, key, vault.schema)
    if problems:
        raise ValueError("; ".join(problems))
    text = dump(frontmatter, f"{line}\n")
    _reindex(vault, key, text, vault.backend.put(key, text))
    return key


def _in_area(note: Note, area: str | None) -> bool:
    if not area:
        return True
    wanted = _area_stem(area)
    have = _area_stem(note.area)
    return have == wanted or have.startswith(f"{wanted}-")


def backlog(vault: Vault, area: str | None = None, priority: str | None = None) -> list[Note]:
    task_folders = set(vault.schema.task_folders)
    rows = [
        note
        for note in vault.index.all_notes()
        if note.valid
        and vault.schema.folder_of(note.key) in task_folders
        and note.status == BACKLOG_STATUS
        and _in_area(note, area)
        and (not priority or note.priority == priority)
    ]
    rows.sort(key=lambda note: note.updated, reverse=True)
    rows.sort(key=lambda note: priority_rank(note.priority))
    return rows


def render_backlog(rows: list[Note], vault: Vault | None = None) -> str:
    if not rows and vault is not None and not vault.schema.task_folders:
        return str(vault.schema.no_home("task folder", "backlog_add and the backlog", "task"))
    if not rows:
        return "The backlog is empty."
    now = time.time()
    lines = [
        f"- {note.key} | {note.title} | {note.priority or '-'} | {note.area or '-'} | "
        f"{humanize_age(age_days(note, now))}"
        for note in rows
    ]
    return f"{len(rows)} backlog items\n" + "\n".join(lines)


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


def _system_notes(
    vault: Vault, notes: list[Note], path: str | None, repo: str | None
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
            text, _ = _read(vault, note.key)
        except Exception:
            continue
        loaded.append((note, _truncate(text)))
    rows = [n for n in candidates if n not in full][:15]
    return loaded, rows


def _log_tail(vault: Vault, repo: str | None) -> list[str]:
    if not repo:
        return []
    try:
        text, _ = _read(vault, note_key(vault.schema.log_key(repo)))
    except Exception:
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
    system, system_rows = _system_notes(vault, notes, path, repo)
    return ContextBundle(
        system=system,
        system_rows=system_rows,
        tasks=fresh[:limit],
        triage=triage[:limit],
        backlog=backlog_rows[:limit],
        backlog_total=len(backlog_rows),
        references=references[:limit],
        log_tail=_log_tail(vault, repo),
        repo=repo,
        stale_after_days=vault.schema.stale_after_days,
        schema_problems=broken_schema_lines(vault.schema),
    )


@dataclass
class Findings:
    groups: dict[str, list[str]] = field(default_factory=dict)

    def add(self, kind: str, entry: str) -> None:
        self.groups.setdefault(kind, []).append(entry)

    def as_dict(self) -> dict[str, list[str]]:
        return {kind: sorted(entries) for kind, entries in sorted(self.groups.items()) if entries}

    def render(self) -> str:
        grouped = self.as_dict()
        if not grouped:
            return "No findings."
        total = sum(len(entries) for entries in grouped.values())
        blocks = [f"{total} findings"]
        for kind, entries in grouped.items():
            blocks.append(f"\n## {kind} ({len(entries)})\n" + "\n".join(f"- {entry}" for entry in entries))
        return "\n".join(blocks)

    def only(self, kind: str) -> Findings:
        return Findings({kind: list(self.groups.get(kind, []))})

    @classmethod
    def parse(cls, text: str) -> Findings:
        findings, kind = cls(), None
        for line in text.splitlines():
            heading = FINDINGS_HEADING_RE.match(line)
            if heading:
                kind = heading.group(1)
            elif kind is not None and line.startswith("- "):
                findings.add(kind, line[2:])
        return findings


FINDINGS_HEADING_RE = re.compile(r"^## (\w+) \(\d+\)$")


def _lint_frontmatter(vault: Vault, note: Note, findings: Findings) -> None:
    if not note.valid:
        findings.add("broken_frontmatter", f"{note.key}: {note.error}")
        return
    try:
        text, _ = _read(vault, note.key)
        frontmatter, _ = parse(text)
    except Exception as exc:
        findings.add("broken_frontmatter", f"{note.key}: {exc}")
        return
    for problem in validate(frontmatter, note.key, vault.schema):
        if problem.startswith("missing required field: "):
            findings.add("missing_required", f"{note.key}: {problem.removeprefix('missing required field: ')}")
        elif problem.startswith("missing area") or problem.startswith("area "):
            findings.add("missing_area", f"{note.key}: {problem}")
        elif problem.startswith("tag "):
            findings.add("unknown_tags", f"{note.key}: {problem}")
        else:
            findings.add("invalid_field", f"{note.key}: {problem}")


def _lint_links(vault: Vault, notes: list[Note], findings: Findings) -> None:
    stems = vault.index.stems()
    bases = {entry.key.rsplit("/", 1)[-1].lower() for entry in vault.backend.list() if entry.key.endswith(".base")}
    rows = vault.index.db.execute("SELECT src, target FROM links ORDER BY src, target").fetchall()
    for row in rows:
        if row["target"] not in stems and row["target"] not in bases:
            findings.add("unresolved_links", f"{row['src']} -> [[{row['target']}]]")
    linked = vault.index.inbound_targets()
    unsearched = vault.schema.unsearched_folders
    ignored = [folder for folder in (vault.schema.log_folder, vault.schema.archive_folder, *unsearched) if folder]
    for note in notes:
        if vault.schema.under(note.key, ignored) or note.area:
            continue
        if note.stem.lower() not in linked:
            findings.add("orphans", note.key)


def _lint_lifecycle(vault: Vault, notes: list[Note], findings: Findings) -> None:
    now = time.time()
    schema = vault.schema
    task_folders = set(schema.task_folders)
    archive = [schema.archive_folder] if schema.archive_folder else []
    stems = vault.index.stems()
    log = [schema.log_folder] if schema.log_folder else []
    by_stem: dict[str, list[str]] = {}
    for note in notes:
        folder = schema.folder_of(note.key)
        if not schema.under(note.key, log):
            by_stem.setdefault(note.stem.lower(), []).append(note.key)
        if folder in task_folders and note.status in OPEN_STATUSES:
            days = age_days(note, now)
            if days > schema.stale_after_days:
                findings.add("stale_active", f"{note.key}: {humanize_age(days)} old, status {note.status}")
        if archive and folder in task_folders and note.status in ("complete", "superseded"):
            findings.add("done_not_closed", f"{note.key}: status {note.status}, still in {folder}/")
        if schema.under(note.key, archive) and note.status not in ("complete", "superseded"):
            findings.add("archive_status_mismatch", f"{note.key}: status {note.status or '-'}")
        if note.superseded_by:
            target = note.superseded_by.strip("[]").split("|")[0].strip().lower()
            if target and target not in stems:
                findings.add("superseded_target_missing", f"{note.key} -> {note.superseded_by}")
    for stem, keys in by_stem.items():
        if len(keys) > 1:
            findings.add("duplicate_stems", f"{stem}: {', '.join(sorted(keys))}")


def lint(vault: Vault) -> Findings:
    findings = Findings()
    for problem in (*vault.schema.notices, *vault.schema.problems):
        findings.add("schema", f"{SCHEMA_KEY}: {problem}")
    notes = vault.index.all_notes()
    for note in notes:
        _lint_frontmatter(vault, note, findings)
    _lint_links(vault, [note for note in notes if note.valid], findings)
    _lint_lifecycle(vault, [note for note in notes if note.valid], findings)
    return findings


def lint_findings(vault: Vault, write: str | None = None, park: bool = False) -> Findings:
    if write:
        note_key(write)
    parked = park_stale(vault) if park else []
    findings = lint(vault)
    for key in parked:
        findings.add("parked", f"{key}: status backlog, untouched for more than {vault.schema.stale_after_days} days")
    if write:
        text = lint_note_text(findings)
        _reindex(vault, write, text, vault.backend.put(write, text))
    return findings


def lint_and_park(vault: Vault, write: str | None = None, park: bool = False) -> str:
    return lint_findings(vault, write=write, park=park).render()


def lint_note_text(findings: Findings) -> str:
    frontmatter = {
        "title": f"Lint {today()}",
        "date": stamp(),
        "updated": stamp(),
        "tags": ["log"],
        "status": "complete",
        "kind": "log",
        "area": "[[vault]]",
    }
    return dump(frontmatter, findings.render())


def changelog_notes(vault: Vault, period: str) -> list[Note]:
    return [note for note in vault.index.all_notes() if note.date.startswith(period)]


def log_lines(vault: Vault, repo: str, period: str) -> list[str]:
    try:
        text, _ = _read(vault, note_key(vault.schema.log_key(repo)))
    except Exception:
        return []
    return [line for line in text.splitlines() if line.lstrip().startswith(f"- [{period}")]


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def vault_paths_in(paths: list[str], root: Path) -> list[str]:
    target = _resolved(root)
    inside = []
    for raw in paths:
        candidate = _resolved(Path(expand_home(raw)))
        if candidate == target or target in candidate.parents:
            inside.append(str(candidate))
    return inside
