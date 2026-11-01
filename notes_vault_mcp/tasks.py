from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from notes_vault_mcp import notes
from notes_vault_mcp.backends import MUST_NOT_EXIST, VersionConflict
from notes_vault_mcp.frontmatter import CLAIM_KEY, claim_fields, dump, parse
from notes_vault_mcp.git import current_branch, in_main_checkout, main_checkout, repo_name
from notes_vault_mcp.index import stem_of
from notes_vault_mcp.schema import Schema
from notes_vault_mcp.vault import Vault

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
DEFAULT_NEED = ("problem", "solution", "decisions")
CLAIM_ATTEMPTS = 3


@dataclass(frozen=True)
class Place:
    repo: str
    checkout: Path | None
    area: str | None
    area_tags: tuple[str, ...] = ()


def located(cwd: str, system: list[dict]) -> Place:
    first = system[0] if system else None
    return Place(
        repo=repo_name(cwd),
        checkout=main_checkout(cwd),
        area=first["stem"] if first else None,
        area_tags=tuple(first["tags"]) if first else (),
    )


def system_notes(vault: Vault, cwd: str) -> list[dict]:
    return notes.context(vault, path=str(in_main_checkout(cwd)), repo=repo_name(cwd)).as_dict()["system"]


def place(vault: Vault, cwd: str) -> Place:
    where = located(cwd, system_notes(vault, cwd))
    if where.checkout is not None:
        vault.index.remember_repo(where.repo, str(where.checkout))
    return where


def home_relative(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)


def task_stem(repo: str, name: str) -> str:
    family = notes.slugify(repo)
    slug = notes.slugify(name)
    if not slug:
        return family
    return slug if slug == family or slug.startswith(f"{family}-") else notes.slugify(f"{repo} {name}")


def free_key(taken_keys: list[str], folder: str, stem: str) -> str:
    taken = {stem_of(key).lower() for key in taken_keys}
    candidate, suffix = stem, 2
    while candidate.lower() in taken:
        candidate = f"{stem}-{suffix}"
        suffix += 1
    return f"{folder}/{candidate}.md"


def task_tags(schema: Schema, repo: str, area_tags: tuple[str, ...]) -> list[str]:
    vocabulary = schema.tag_vocabulary
    if not vocabulary or repo in vocabulary:
        return [repo]
    known = [tag for tag in area_tags if tag in vocabulary]
    return known[:1] or [repo]


def section_skeleton(schema: Schema) -> str:
    return "\n\n".join(f"## {heading}" for heading in schema.task_sections.values()) + "\n"


def require_name(name: str) -> None:
    if not name.strip():
        raise ValueError("task open needs --name, or --note to claim a note")


def new_task(schema: Schema, name: str, where: Place, branch: str | None, taken_keys: list[str]) -> tuple[str, str]:
    key = free_key(taken_keys, schema.task_folder, task_stem(where.repo, name))
    frontmatter: dict = {
        "title": name.strip(),
        "date": notes.stamp(),
        "updated": notes.stamp(),
        "tags": task_tags(schema, where.repo, where.area_tags),
        "status": "active",
        "kind": "task",
        "area": f"[[{where.area or where.repo}]]",
        "summary": "",
    }
    if where.checkout is not None:
        frontmatter["path"] = home_relative(where.checkout)
    if branch:
        frontmatter["source"] = f"branch {branch}"
    return key, dump(frontmatter, section_skeleton(schema))


def opened(key: str, where: Place) -> dict:
    return {"path": key, "created": True, "repo": where.repo, "area": where.area}


def open_task(vault: Vault, name: str, cwd: str, branch: str | None = None) -> dict:
    require_name(name)
    where = place(vault, cwd)
    taken = [entry.key for entry in vault.backend.list()]
    key, text = new_task(vault.schema, name, where, branch or current_branch(cwd), taken)
    notes.write(vault, key, text, expected_etag=MUST_NOT_EXIST)
    return opened(key, where)


def task_key(schema: Schema, path: str) -> str:
    key = notes.note_key(path)
    if schema.folder_of(key) not in schema.task_folders:
        raise ValueError(f"{key} is not in a task folder ({', '.join(schema.task_folders)})")
    return key


def picked_up(key: str, text: str, cwd: str) -> dict:
    frontmatter, _ = parse(text)
    area = notes._area_stem(str(frontmatter.get("area") or "")) or None
    return {"path": key, "created": False, "repo": repo_name(cwd), "area": area}


def claim_task(vault: Vault, path: str, cwd: str) -> dict:
    key = task_key(vault.schema, path)
    text, _ = vault.backend.get(key)
    notes.set_status(vault, key, "active")
    return picked_up(key, text, cwd)


def claim_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def claim_note(vault: Vault, path: str, machine: str, task: str, repo: str = "", force: bool = False) -> dict:
    key = task_key(vault.schema, path)
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
    key = task_key(vault.schema, path)
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
    known_sections(vault.schema, need)
    text, _ = vault.backend.get(notes.note_key(path))
    return missing_in(vault.schema, text, need)


def known_sections(schema: Schema, need: list[str]) -> None:
    unknown = [key for key in need if key not in schema.task_sections]
    if unknown:
        raise ValueError(f"unknown section {', '.join(unknown)}; the schema names {', '.join(schema.task_sections)}")


def missing_in(schema: Schema, text: str, need: list[str]) -> list[str]:
    sections = schema.task_sections
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
        area=where.area,
        branch=branch or current_branch(cwd),
    )
    return {"path": vault.schema.log_key(where.repo)}


def log_paths(schema: Schema, repos: list[str]) -> dict:
    if len(repos) == 1:
        return {"path": schema.log_key(repos[0])}
    return {repo: schema.log_key(repo) for repo in repos}
