from __future__ import annotations

import re

from mindin.index import Note
from mindin.notes import RepoPage, path_overlap
from mindin.vault import Vault

PAGES_SHOWN = 5
LINE_LENGTH = 160
REMOTE_RE = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?")
LINK_RE = re.compile(r"\[\[([^\]|#]+)")


def plain_remote(raw: str) -> str:
    text = REMOTE_RE.sub("", raw.strip().lower()).replace(":", "/", 1)
    return text.removesuffix("/").removesuffix(".git")


def names_repo(note: Note, repo: str, remote: str) -> bool:
    wanted = repo.lower()
    for raw in note.repos:
        entry = plain_remote(raw)
        if entry == wanted or entry.rsplit("/", 1)[-1] == wanted:
            return True
        if remote and (remote == entry or remote.endswith("/" + entry)):
            return True
    return False


def area_stem(note: Note) -> str:
    found = LINK_RE.search(note.area)
    return (found.group(1) if found else note.area).strip().rsplit("/", 1)[-1].lower()


def relevance(note: Note, path: str | None, repo: str, remote: str, areas: set[str]) -> int:
    if repo and names_repo(note, repo, remote):
        return 3
    if path_overlap(note, path) > 0 or (repo and note.stem.lower() == repo.lower()):
        return 2
    return 1 if area_stem(note) in areas else 0


def first_line(note: Note, body: str) -> str:
    if note.summary:
        return note.summary[:LINE_LENGTH]
    lines = (line.strip() for line in body.splitlines())
    return next((line[:LINE_LENGTH] for line in lines if line and not line.startswith("#")), "")


def pages_for(
    vault: Vault, slug: str, path: str | None, repo: str | None, remote: str | None, areas: set[str]
) -> list[tuple[int, str, RepoPage]]:
    schema = vault.schema
    unsearched = set(schema.unsearched_folders)
    wanted = {area.lower() for area in areas} | ({repo.lower()} if repo else set())
    found = []
    for note in vault.index.all_notes():
        if not note.valid or note.superseded_by or note.status == "superseded":
            continue
        if schema.folder_of(note.key) in unsearched:
            continue
        score = relevance(note, path, repo or "", plain_remote(remote or ""), wanted)
        if score:
            found.append((score, note))
    bodies = vault.index.bodies([note.key for _, note in found])
    return [
        (score, note.updated, RepoPage(slug, note.key, note.title, first_line(note, bodies.get(note.key, ""))))
        for score, note in found
    ]


def strongest(found: list[tuple[int, str, RepoPage]], limit: int = PAGES_SHOWN) -> list[RepoPage]:
    ranked = sorted(found, key=lambda entry: entry[1], reverse=True)
    ranked.sort(key=lambda entry: -entry[0])
    return [page for _, _, page in ranked[:limit]]
