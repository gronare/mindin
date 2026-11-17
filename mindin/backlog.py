from __future__ import annotations

import re
import time
import unicodedata

from mindin import note_templates, styles
from mindin.backends import MUST_NOT_EXIST, VaultBackend, VaultError, VersionConflict
from mindin.frontmatter import dump
from mindin.index import Note, stem_of
from mindin.notes import (
    BACKLOG_STATUS,
    REWRITE_RETRIES,
    ValidationError,
    free_key,
    note_key,
    reindex,
    require_clean,
    require_valid,
    stamp,
)
from mindin.search import age_days, humanize_age
from mindin.vault import Vault

PRIORITY_ORDER = ("urgent", "high", "medium", "low")
SLUG_MAX = 60
STYLE_KIND = "backlog"


def styled(style: styles.Style | None) -> str:
    found = styles.guidance(style)
    return f"\n\n{found}" if found else ""


def filed_style(backend: VaultBackend) -> str:
    try:
        return styled(styles.style_for(backend, STYLE_KIND))
    except VaultError as error:
        return f"\n\nThe {STYLE_KIND} style could not be read ({error}); the note is filed without it."


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


def area_stem(area: str) -> str:
    return stem_of(area.strip().strip("[]").split("|")[0].strip())


def _draft_body(vault: Vault, line: str, source: str | None) -> str:
    template = vault.schema.template("task")
    if template is None:
        return f"{line}\n"
    first = template.sections[0].key
    home = "source" if template.section("source") else first
    origin = "\n".join(part for part in (line, source) if part)
    texts = {first: note_templates.DRAFT_MARKER}
    texts[home] = f"{texts[home]}\n\n{origin}" if home == first else origin
    return note_templates.written_body(template, vault.schema.language, texts)


def _sections_body(vault: Vault, sections: dict[str, str]) -> str:
    template = vault.schema.template("task")
    if template is None:
        raise ValueError("this vault's task notes have no template: file the idea with line instead of sections")
    unknown = [key for key in sections if key not in template.keys]
    if unknown:
        raise ValueError(f"unknown section {', '.join(unknown)}; the task template has {', '.join(template.keys)}")
    language = vault.schema.language
    return note_templates.written_body(template, language, {key: str(text) for key, text in sections.items()})


def sections_warning(vault: Vault, sections: dict[str, str] | None) -> str:
    template = vault.schema.template("task")
    if not sections or template is None:
        return ""
    language = vault.schema.language
    empty = [
        f"{section.key} (## {section.heading(language)})"
        for section in template.sections
        if section.required and not str(sections.get(section.key) or "").strip()
    ]
    return f"\nWarning: filed, though the task template asks for text in {', '.join(empty)}." if empty else ""


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def backlog_add(
    vault: Vault,
    title: str,
    area: str,
    line: str | None = None,
    priority: str | None = None,
    source: str | None = None,
    sections: dict[str, str] | None = None,
) -> str:
    wanted_stem = area_stem(area)
    if not wanted_stem:
        raise ValueError("area is required: the stem of the system note, or [[stem]]")
    if not (line or "").strip() and not sections:
        raise ValueError(
            "backlog_add needs line, one line saying what and why, or sections, the task template filled in"
        )
    body = _sections_body(vault, sections) if sections else _draft_body(vault, str(line).strip(), source)
    family = wanted_stem.split("-")[0]
    slug = _title_slug(title)
    stem = slug if slug == family or slug.startswith(f"{family}-") else f"{family}-{slug}"
    frontmatter: dict = {
        "title": title,
        "date": stamp(),
        "updated": stamp(),
        "tags": [family],
        "status": BACKLOG_STATUS,
        "kind": "task",
        "area": f"[[{wanted_stem}]]",
        "summary": (line or "").strip() or _first_line(sections.get("goal", "") if sections else "") or title,
    }
    if priority:
        frontmatter["priority"] = priority
    if source:
        frontmatter["source"] = source
    text = dump(frontmatter, body)
    lost: frozenset[str] = frozenset()
    for _ in range(REWRITE_RETRIES + 1):
        key = note_key(free_key(vault, vault.schema.backlog_folder, stem, lost))
        try:
            require_valid(vault, frontmatter, key, "is not a valid note")
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        require_clean(vault, key, text)
        try:
            version = vault.backend.put(key, text, expected_version=MUST_NOT_EXIST)
        except VersionConflict:
            lost |= {key}
            continue
        reindex(vault, key, text, version)
        return key
    raise VersionConflict(f"every free name for {stem} was taken while the note was being added; try again")


def _in_area(note: Note, area: str | None) -> bool:
    if not area:
        return True
    wanted = area_stem(area)
    have = area_stem(note.area)
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
