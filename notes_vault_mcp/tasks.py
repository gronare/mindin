from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from notes_vault_mcp import notes
from notes_vault_mcp.backends import MUST_NOT_EXIST, VersionConflict
from notes_vault_mcp.frontmatter import CLAIM_KEY, claim_fields, dump, folder_of, parse
from notes_vault_mcp.git import current_branch, in_main_checkout, main_checkout, repo_name
from notes_vault_mcp.index import Note, stem_of
from notes_vault_mcp.vault import Vault

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
DEFAULT_NEED = ("problem", "solution", "decisions")
CLAIM_ATTEMPTS = 3


@dataclass(frozen=True)
class Place:
    repo: str
    checkout: Path | None
    area: Note | None


def place(vault: Vault, cwd: str) -> Place:
    repo = repo_name(cwd)
    checkout = main_checkout(cwd)
    if checkout is not None:
        vault.index.remember_repo(repo, str(checkout))
    bundle = notes.context(vault, path=str(in_main_checkout(cwd)), repo=repo)
    return Place(repo=repo, checkout=checkout, area=bundle.system[0][0] if bundle.system else None)


def home_relative(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)


def task_stem(repo: str, name: str) -> str:
    family = notes.slugify(repo)
    slug = notes.slugify(name)
    if not slug:
        return family
    return slug if slug == family or slug.startswith(f"{family}-") else notes.slugify(f"{repo} {name}")


def free_key(vault: Vault, folder: str, stem: str) -> str:
    taken = {stem_of(entry.key).lower() for entry in vault.backend.list()}
    candidate, suffix = stem, 2
    while candidate.lower() in taken:
        candidate = f"{stem}-{suffix}"
        suffix += 1
    return f"{folder}/{candidate}.md"


def task_tags(vault: Vault, repo: str, area: Note | None) -> list[str]:
    vocabulary = vault.schema.tag_vocabulary
    if not vocabulary or repo in vocabulary:
        return [repo]
    known = [tag for tag in (area.tags if area else []) if tag in vocabulary]
    return known[:1] or [repo]


def section_skeleton(vault: Vault) -> str:
    return "\n\n".join(f"## {heading}" for heading in vault.schema.task_sections.values()) + "\n"


def open_task(vault: Vault, name: str, cwd: str, branch: str | None = None) -> dict:
    if not name.strip():
        raise ValueError("task open needs --name, or --note to claim a note")
    where = place(vault, cwd)
    area_stem = where.area.stem if where.area else None
    key = free_key(vault, vault.schema.task_folders[0], task_stem(where.repo, name))
    frontmatter: dict = {
        "title": name.strip(),
        "date": notes.stamp(),
        "updated": notes.stamp(),
        "tags": task_tags(vault, where.repo, where.area),
        "status": "active",
        "kind": "task",
        "area": f"[[{area_stem or where.repo}]]",
        "summary": "",
    }
    if where.checkout is not None:
        frontmatter["path"] = home_relative(where.checkout)
    on_branch = branch or current_branch(cwd)
    if on_branch:
        frontmatter["source"] = f"branch {on_branch}"
    notes.write(vault, key, dump(frontmatter, section_skeleton(vault)), expected_etag=MUST_NOT_EXIST)
    return {"path": key, "created": True, "repo": where.repo, "area": area_stem}


def claim_task(vault: Vault, path: str, cwd: str) -> dict:
    path = notes.note_key(path)
    if folder_of(path) not in vault.schema.task_folders:
        raise ValueError(f"{path} is not in a task folder ({', '.join(vault.schema.task_folders)})")
    text, _ = vault.backend.get(path)
    frontmatter, _ = parse(text)
    notes.set_status(vault, path, "active")
    area = notes._area_stem(str(frontmatter.get("area") or "")) or None
    return {"path": path, "created": False, "repo": repo_name(cwd), "area": area}


def claim_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _task_note(vault: Vault, path: str) -> str:
    key = notes.note_key(path)
    if folder_of(key) not in vault.schema.task_folders:
        raise ValueError(f"{key} is not in a task folder ({', '.join(vault.schema.task_folders)})")
    return key


def claim_note(vault: Vault, path: str, machine: str, task: str, repo: str = "", force: bool = False) -> dict:
    key = _task_note(vault, path)
    machine, task = machine.strip(), task.strip()
    if not machine or not task:
        raise ValueError("task claim needs --machine and --task")
    for _ in range(CLAIM_ATTEMPTS):
        text, version = vault.backend.get(key)
        frontmatter, body = parse(text)
        held = claim_fields(frontmatter.get(CLAIM_KEY))
        elsewhere = held is not None and held["machine"] != machine
        if elsewhere and not force:
            return {"path": key, "claimed": False, "claim": held}
        since = held["since"] if held is not None and not elsewhere and held["task"] == task else claim_stamp()
        claim = {"machine": machine, "task": task, "repo": repo.strip(), "since": since}
        frontmatter[CLAIM_KEY] = claim
        try:
            notes.put_frontmatter(vault, key, frontmatter, body, version)
        except VersionConflict:
            continue
        return {"path": key, "claimed": True, "claim": claim, "replaced": held if elsewhere else None}
    raise VersionConflict(f"{key} kept changing while it was being claimed; try again")


def release_note(vault: Vault, path: str, machine: str | None = None) -> dict:
    key = _task_note(vault, path)
    for _ in range(CLAIM_ATTEMPTS):
        text, version = vault.backend.get(key)
        frontmatter, body = parse(text)
        if CLAIM_KEY not in frontmatter:
            return {"path": key, "released": True, "claim": None}
        held = claim_fields(frontmatter.get(CLAIM_KEY))
        if held is not None and machine and held["machine"] != machine.strip():
            return {"path": key, "released": False, "claim": held}
        del frontmatter[CLAIM_KEY]
        try:
            notes.put_frontmatter(vault, key, frontmatter, body, version)
        except VersionConflict:
            continue
        return {"path": key, "released": True, "claim": None}
    raise VersionConflict(f"{key} kept changing while it was being released; try again")


def release_claims_of(vault: Vault, machine: str, task: str) -> dict:
    machine, task = machine.strip(), task.strip()
    if not machine or not task:
        raise ValueError("task release without a path needs --machine and --task")
    vault.index.sync(force=True)
    held = [
        note.key
        for note in vault.index.all_notes()
        if note.claim and (claim := json.loads(note.claim))["machine"] == machine and claim["task"] == task
    ]
    for key in held:
        release_note(vault, key, machine=machine)
    return {"paths": held, "released": True}


def filled_headings(body: str) -> dict[str, bool]:
    headings: list[tuple[int, str, int]] = []
    lines = body.splitlines()
    fenced = False
    for number, line in enumerate(lines):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else HEADING_RE.match(line)
        if match:
            headings.append((len(match.group(1)), match.group(2).strip().casefold(), number))
    filled: dict[str, bool] = {}
    for position, (level, title, start) in enumerate(headings):
        end = next((line for lvl, _, line in headings[position + 1 :] if lvl <= level), len(lines))
        has_text = any(line.strip() for line in lines[start + 1 : end])
        filled[title] = filled.get(title, False) or has_text
    return filled


def missing_sections(vault: Vault, path: str, need: list[str]) -> list[str]:
    sections = vault.schema.task_sections
    unknown = [key for key in need if key not in sections]
    if unknown:
        raise ValueError(f"unknown section {', '.join(unknown)}; the schema names {', '.join(sections)}")
    text, _ = vault.backend.get(notes.note_key(path))
    _, body = parse(text)
    filled = filled_headings(body)
    return [key for key in need if not filled.get(sections[key].strip().casefold(), False)]


def append_log(vault: Vault, cwd: str, line: str, commits: tuple[str, ...] = (), branch: str | None = None) -> dict:
    if not line.strip():
        raise ValueError("a log line needs text")
    where = place(vault, cwd)
    notes.log_append(
        vault,
        where.repo,
        line.strip(),
        commits=commits,
        area=where.area.stem if where.area else None,
        branch=branch or current_branch(cwd),
    )
    return {"path": vault.schema.log_key(where.repo)}
