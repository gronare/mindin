from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from mindin import note_templates, sweep
from mindin.backends import SCHEMA_KEY, NotFound
from mindin.frontmatter import FrontmatterError, dump, parse, validate
from mindin.index import Note
from mindin.notes import OPEN_STATUSES, fetch, note_key, park_stale, reindex, stamp, today
from mindin.search import age_days, humanize_age
from mindin.vault import Vault


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


def _stored(vault: Vault, note: Note) -> str | None:
    try:
        return fetch(vault, note.key)[0] if note.valid else None
    except NotFound:
        return None


def _lint_frontmatter(vault: Vault, note: Note, text: str | None, findings: Findings) -> None:
    if not note.valid:
        findings.add("broken_frontmatter", f"{note.key}: {note.error}")
        return
    if text is None:
        return
    try:
        frontmatter, body = parse(text)
    except FrontmatterError as exc:
        findings.add("broken_frontmatter", f"{note.key}: {exc}")
        return
    archive = [vault.schema.archive_folder] if vault.schema.archive_folder else []
    kind = note_templates.template_kind(vault.schema, note.key, frontmatter)
    checked = {**frontmatter, "kind": kind}
    off = [] if vault.schema.under(note.key, archive) else note_templates.problems(vault.schema, checked, body)
    if off:
        findings.add("off_template", f"{note.key}: does not follow the {kind} template: {'; '.join(off)}")
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
    texts = vault.index.note_texts()
    for note in notes:
        _lint_frontmatter(vault, note, texts.get(note.key) or _stored(vault, note), findings)
    _lint_links(vault, [note for note in notes if note.valid], findings)
    _lint_lifecycle(vault, [note for note in notes if note.valid], findings)
    return findings


def lint_findings(vault: Vault, write: str | None = None, park: bool = False, close_done: bool = False) -> Findings:
    if write:
        note_key(write)
    closed, uncertain = sweep.close_done(vault) if close_done else ([], [])
    parked = park_stale(vault) if park else []
    findings = lint(vault)
    for verdict in closed:
        findings.add("closed_done", f"{verdict.key}: {verdict.evidence}")
    for verdict in uncertain:
        findings.add("done_uncertain", f"{verdict.key}: {verdict.reason}; {verdict.evidence}")
    for key in parked:
        findings.add("parked", f"{key}: status backlog, untouched for more than {vault.schema.stale_after_days} days")
    if write:
        text = lint_note_text(findings)
        reindex(vault, write, text, vault.backend.put(write, text))
    return findings


def lint_and_park(vault: Vault, write: str | None = None, park: bool = False, close_done: bool = False) -> str:
    return lint_findings(vault, write=write, park=park, close_done=close_done).render()


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
