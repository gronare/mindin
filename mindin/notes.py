from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import date

from mindin import note_templates, styles
from mindin.backends import MUST_NOT_EXIST, SCHEMA_KEY, NotFound, VersionConflict, delete_checked
from mindin.bundle import bundle_of
from mindin.frontmatter import CLAIM_KEY, FrontmatterError, claim_fields, dump, parse, validate
from mindin.guard import refusal
from mindin.index import Note, stem_of
from mindin.schema import CLAIM_STALE_DAYS
from mindin.search import age_days
from mindin.vault import Vault

OPEN_STATUSES = ("active", "draft")
BACKLOG_STATUS = "backlog"
REWRITE_RETRIES = 3

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


def fetch(vault: Vault, path: str) -> tuple[str, str]:
    return vault.backend.get(path)


def reindex(vault: Vault, path: str, text: str, version: str) -> None:
    vault.index.upsert(path, text, version, time.time(), len(text.encode("utf-8")))


def rewrite(vault: Vault, key: str, change: Callable[[str | None], str | None]) -> tuple[str, str] | None:
    """Writes change(stored text, or None when absent) only over the version it read, reading again on a conflict."""
    for _ in range(REWRITE_RETRIES + 1):
        try:
            current, version = fetch(vault, key)
        except NotFound:
            current, version = None, MUST_NOT_EXIST
        text = change(current)
        if text is None:
            return None
        try:
            written = vault.backend.put(key, text, expected_version=version)
        except VersionConflict:
            continue
        reindex(vault, key, text, written)
        return text, written
    raise VersionConflict(f"{key} kept changing while it was being written; try again")


def read(vault: Vault, path: str) -> str:
    if styles.style_key(path) is not None:
        text, version = fetch(vault, path)
        return f"etag: {version}\n{text}"
    text, version = fetch(vault, note_key(path, readable=True))
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


def require_valid(vault: Vault, frontmatter: dict, key: str, verdict: str) -> None:
    if vault.rules is not None:
        vault.rules.check_note(key, frontmatter, vault.schema)
    problems = validate(frontmatter, key, vault.schema)
    if problems:
        raise ValidationError("\n".join([f"{key} {verdict}:", *[f"- {p}" for p in problems]]))


def _off_template_before(vault: Vault, frontmatter: dict, stored: str) -> bool:
    try:
        before, body = parse(stored)
    except FrontmatterError:
        return True
    if not note_templates.binds(before) or before.get("kind") != frontmatter.get("kind"):
        return False
    return note_templates.DRAFT_MARKER not in body and bool(note_templates.problems(vault.schema, before, body))


def _template_warning(vault: Vault, key: str, frontmatter: dict, body: str, stored: str | None = None) -> str:
    kept_kind = None if stored is None or frontmatter.get("kind") else _kind_in(stored)
    checked = {**frontmatter, "kind": kept_kind} if kept_kind else frontmatter
    if stored is not None and _off_template_before(vault, checked, stored):
        return ""
    return note_templates.off_template_warning(vault.schema, key, checked, body)


def require_clean(vault: Vault, key: str, text: str) -> str:
    refused = refusal(key, text, bundle_of(vault.schema).guard) if vault.rules is None else ""
    if refused:
        raise ValidationError(refused)
    return text


def _stored_text(vault: Vault, key: str) -> str | None:
    try:
        return fetch(vault, key)[0]
    except NotFound:
        return None


def _kind_in(text: str) -> str | None:
    try:
        kind = parse(text)[0].get("kind")
    except FrontmatterError:
        return None
    return str(kind) if kind else None


def _claim_in(text: str | None) -> dict[str, str] | None:
    if text is None:
        return None
    try:
        return claim_fields(parse(text)[0].get(CLAIM_KEY))
    except FrontmatterError:
        return None


def _with_claim(frontmatter: dict, claim: dict[str, str] | None) -> dict:
    kept = {key: value for key, value in frontmatter.items() if key != CLAIM_KEY}
    if claim is not None:
        kept[CLAIM_KEY] = claim
    return kept


def write_style(vault: Vault, path: str, content: str, expected_etag: str | None = None) -> str:
    kind, override = styles.style_key(path) or ("", False)
    if override:
        raise styles.StyleRefused(styles.write_refusal(path))
    return styles.write_default(vault.backend, kind, content, expected_etag=expected_etag)


def write(vault: Vault, path: str, content: str, expected_etag: str | None = None) -> str:
    if styles.style_key(path) is not None:
        return write_style(vault, path, content, expected_etag)
    key = note_key(path)
    frontmatter, body = parse(content)
    warnings: list[str] = []

    def written(existing: str | None) -> str:
        stamped = _with_dates(_with_claim(frontmatter, _claim_in(existing)))
        require_valid(vault, stamped, key, "is not a valid note")
        kept_kind = existing is not None and _kind_in(existing) is not None
        kindless = "" if kept_kind else note_templates.kindless_warning(vault.schema, key, stamped, body)
        warnings[:] = [_template_warning(vault, key, stamped, body, existing), kindless]
        return require_clean(vault, key, dump(stamped, body))

    if expected_etag is None:
        stored = rewrite(vault, key, written)
        assert stored is not None
        version = stored[1]
    else:
        text = written(_stored_text(vault, key))
        version = vault.backend.put(key, text, expected_version=expected_etag)
        reindex(vault, key, text, version)
    return "\n".join([f"Written: {key}\netag: {version}", *filter(None, warnings)])


def append(vault: Vault, path: str, content: str) -> str:
    key = note_key(path)

    def appended(existing: str | None) -> str:
        if existing is None:
            raise NotFound(f"{key} (append_file extends a note; write_file creates one)")
        frontmatter, body = parse(existing)
        stamped = _with_dates(frontmatter)
        require_valid(vault, stamped, key, "would not be a valid note")
        body = f"{body.rstrip()}\n{content}\n" if body.strip() else f"{content}\n"
        return require_clean(vault, key, dump(stamped, body))

    rewrite(vault, key, appended)
    return f"Appended to: {key}"


def move(vault: Vault, src: str, dst: str) -> str:
    source, target = note_key(src), note_key(dst)
    text, _ = fetch(vault, source)
    frontmatter, _ = parse(text)
    require_valid(vault, frontmatter, target, "would not be a valid note")
    vault.backend.move(source, target)
    text, version = fetch(vault, target)
    vault.index.remove(source)
    reindex(vault, target, text, version)
    return f"Moved: {source} -> {target}"


def delete(vault: Vault, path: str) -> str:
    key = note_key(path)
    vault.backend.delete(key)
    vault.index.remove(key)
    return f"Deleted: {key}"


def free_key(vault: Vault, folder: str, basename: str, lost: frozenset[str] = frozenset()) -> str:
    stem = basename.removesuffix(".md")
    candidate = f"{folder}/{stem}.md"
    suffix = 2
    taken = {entry.key for entry in vault.backend.list()} | lost
    while candidate in taken:
        candidate = f"{folder}/{stem}-{suffix}.md"
        suffix += 1
    return candidate


def set_status(vault: Vault, path: str, status: str, priority: str | None = None, source: str | None = None) -> str:
    path = note_key(path)

    def restated(text: str | None) -> str:
        if text is None:
            raise NotFound(path)
        frontmatter, body = parse(text)
        frontmatter["status"] = status
        if status == BACKLOG_STATUS:
            frontmatter.pop(CLAIM_KEY, None)
        if priority:
            frontmatter["priority"] = priority
        if source:
            frontmatter["source"] = source
        stamped = _with_dates(frontmatter)
        require_valid(vault, stamped, path, "would not be a valid note")
        return dump(stamped, body)

    rewrite(vault, path, restated)
    return f"{path}: status {status}" + (f", priority {priority}" if priority else "")


def put_frontmatter(vault: Vault, path: str, frontmatter: dict, body: str, expected_version: str) -> str:
    stamped = _with_dates(frontmatter)
    require_valid(vault, stamped, path, "would not be a valid note")
    out = dump(stamped, body)
    version = vault.backend.put(path, out, expected_version=expected_version)
    reindex(vault, path, out, version)
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
    lost: frozenset[str] = frozenset()
    for _ in range(REWRITE_RETRIES + 1):
        text, read_version = fetch(vault, path)
        frontmatter, body = parse(text)
        if merged_into:
            frontmatter["status"] = "superseded"
            frontmatter["superseded_by"] = f"[[{stem_of(merged_into)}]]"
        else:
            frontmatter["status"] = status or "complete"
        frontmatter.pop(CLAIM_KEY, None)
        archive = vault.schema.archive_folder
        archived = archive is None or vault.schema.under(path, [archive])
        destination = path if archived else free_key(vault, archive, path.rsplit("/", 1)[-1], lost)
        stamped = _with_dates(frontmatter)
        require_valid(vault, stamped, destination, "cannot be closed into a valid note")
        out = dump(stamped, body)
        try:
            version = vault.backend.put(destination, out, expected_version=read_version if archived else MUST_NOT_EXIST)
        except VersionConflict:
            if not archived:
                lost |= {destination}
            continue
        if not archived:
            try:
                delete_checked(vault.backend, path, read_version)
            except VersionConflict:
                vault.backend.delete(destination)
                continue
            vault.index.remove(path)
        reindex(vault, destination, out, version)
        return destination
    raise VersionConflict(f"{path} kept changing while it was being closed; try again")


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
    entries: list[str] = []

    def logged(existing: str | None) -> str:
        frontmatter, body = _new_log(repo, area) if existing is None else parse(existing)
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
        entries.append(entry)
        body = f"{body.rstrip()}\n{entry}\n" if body.strip() else f"{entry}\n"
        return dump(frontmatter, body)

    rewrite(vault, key, logged)
    return f"Logged to {key}:\n{entries[-1]}"
