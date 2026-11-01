from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from notes_vault_mcp.index import WIKILINK_RE, Note, expand_home
from notes_vault_mcp.notes import OPEN_STATUSES
from notes_vault_mcp.schema import Schema
from notes_vault_mcp.vault import Vault

SNAPSHOT_FORMAT = "1"
ROLES = ("system", "task")
LOG_LINES = 50
OPENING_MAX = 600
LOG_LINE_RE = re.compile(r"^\s*[-*] \[(\d{4}-\d{2}-\d{2})\]\s*(.*?)\s*$")


def written_paths(paths: list[str]) -> list[str]:
    # The index stores each "~" path followed by its expansion on this machine; a client wants the path as written.
    written: list[str] = []
    for path in paths:
        if written and written[-1].startswith("~") and path == expand_home(written[-1]):
            continue
        written.append(path)
    return written


def role_of(schema: Schema, note: Note) -> str | None:
    folder = schema.folders.get(schema.folder_of(note.key))
    if folder is not None and folder.kind in ("archive", "log"):
        return None
    if folder is not None and folder.kind in ROLES:
        return folder.kind
    return note.kind if note.kind in ROLES else None


def links_in(text: str) -> list[str]:
    stems = [raw.split("|", 1)[0].split("#", 1)[0].strip().rsplit("/", 1)[-1] for raw in WIKILINK_RE.findall(text)]
    return list(dict.fromkeys(stem for stem in stems if stem))


def log_lines(body: str) -> list[dict]:
    parsed = []
    for line in body.splitlines():
        match = LOG_LINE_RE.match(line)
        if match:
            parsed.append({"date": match.group(1), "text": match.group(2), "links": links_in(match.group(2))})
    return parsed[-LOG_LINES:]


def opening(body: str) -> str:
    paragraph: list[str] = []
    for line in body.splitlines():
        text = line.strip()
        if not text:
            if paragraph:
                break
            continue
        if text.startswith("#") and not paragraph:
            continue
        if text.startswith("#"):
            break
        paragraph.append(text)
    joined = " ".join(paragraph)
    return joined if len(joined) <= OPENING_MAX else joined[: OPENING_MAX - 1].rstrip() + "…"


def log_key_template(schema: Schema) -> str | None:
    return f"{schema.log_folder}/{schema.log_file_format}" if schema.log_folder else None


def format_pattern(template: str, fields: dict[str, str]) -> re.Pattern[str]:
    pattern = re.escape(template)
    for name, part in fields.items():
        pattern = pattern.replace(re.escape("{" + name + "}"), part)
    return re.compile(pattern)


def log_notes(schema: Schema, notes: list[Note]) -> list[Note]:
    if schema.log_folder is None:
        return []
    prefix = f"{schema.log_folder}/"
    log_file = format_pattern(schema.log_file_format, {"repo": ".+"})
    period_page = format_pattern(schema.period_file_format, {"repo": ".+", "period": r"\d{4}(?:-\d{2})?"})
    found = []
    for note in notes:
        name = note.key.removeprefix(prefix)
        if note.key.startswith(prefix) and log_file.fullmatch(name) and not period_page.fullmatch(name):
            found.append(note)
    return found


def note_row(note: Note, role: str) -> dict:
    return {
        "key": note.key,
        "stem": note.stem,
        "role": role,
        "kind": note.kind,
        "status": note.status,
        "title": note.title,
        "summary": note.summary,
        "priority": note.priority,
        "area": note.area,
        "paths": written_paths(note.paths),
        "date": note.date,
        "updated": note.updated,
        "mtime": note.mtime,
        "claim": json.loads(note.claim) if note.claim else None,
    }


def etag_of(schema: Schema, versions: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256(SNAPSHOT_FORMAT.encode())
    digest.update(json.dumps(schema.data, sort_keys=True, default=str).encode())
    for key, version in sorted(versions):
        digest.update(f"\0{key}\0{version}".encode())
    return digest.hexdigest()[:20]


def synced_at(vault: Vault) -> str | None:
    last = vault.index.meta("last_sync")
    return datetime.fromtimestamp(float(last), UTC).isoformat(timespec="seconds") if last else None


def build_snapshot(vault: Vault, since: str | None = None) -> dict:
    schema = vault.schema
    every = vault.index.all_notes()
    board = [(note, role) for note in every if note.valid and (role := role_of(schema, note)) is not None]
    logs = [note.key for note in log_notes(schema, every)]
    versions = {note.key: note.version for note in every}
    etag = etag_of(schema, [(key, versions[key]) for key in [*(note.key for note, _ in board), *logs]])
    if since is not None and since == etag:
        return {"unchanged": True, "etag": etag}
    opened = [note.key for note, role in board if role == "task" and note.status in OPEN_STATUSES]
    bodies = vault.index.bodies([*opened, *logs])
    openings = {key: text for key in opened if (text := opening(bodies.get(key, "")))}
    return {
        "etag": etag,
        "synced_at": synced_at(vault),
        "layout": schema.preset,
        "log_key_template": log_key_template(schema),
        "notes": [note_row(note, role) for note, role in board],
        "openings": openings,
        "logs": {key: log_lines(bodies.get(key, "")) for key in logs},
    }
