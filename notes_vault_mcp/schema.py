from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from notes_vault_mcp.backends import SCHEMA_KEY, VaultBackend

CLAIM_KEY = "kebbie"
CLAIM_STALE_DAYS = 14
DEFAULT_LOG_ENTRY_FORMAT = "- [{date}] {line} | branch: {branch} | commits: {commits} | {area}"


@dataclass(frozen=True)
class Folder:
    name: str
    kind: str
    weight: float
    searched: bool
    description: str


@dataclass(frozen=True)
class Schema:
    data: dict[str, Any]

    @property
    def folders(self) -> dict[str, Folder]:
        folders: dict[str, Folder] = {}
        for name, spec in (self.data.get("folders") or {}).items():
            spec = spec or {}
            folders[name] = Folder(
                name=name,
                kind=str(spec.get("kind", "reference")),
                weight=float(spec.get("weight", 1.0)),
                searched=bool(spec.get("searched", True)),
                description=str(spec.get("description", "")),
            )
        return folders

    def folders_of_kind(self, kind: str) -> list[str]:
        return [name for name, folder in self.folders.items() if folder.kind == kind]

    def folder_weight(self, folder: str) -> float:
        found = self.folders.get(folder)
        return found.weight if found else 1.0

    @property
    def archive_folder(self) -> str:
        found = self.folders_of_kind("archive")
        return found[0] if found else "Archive"

    @property
    def log_folder(self) -> str:
        configured = (self.data.get("log") or {}).get("folder")
        if configured:
            return str(configured)
        found = self.folders_of_kind("log")
        return found[0] if found else "Log"

    @property
    def log_file_format(self) -> str:
        return str((self.data.get("log") or {}).get("file_format", "{repo}-log.md"))

    def log_key(self, repo: str) -> str:
        return f"{self.log_folder}/{self.log_file_format.format(repo=repo)}"

    @property
    def period_file_format(self) -> str:
        return str((self.data.get("log") or {}).get("period_format", "{repo}-{period}.md"))

    def period_key(self, repo: str, period: str) -> str:
        return f"{self.log_folder}/{self.period_file_format.format(repo=repo, period=period)}"

    @property
    def log_entry_format(self) -> str:
        return str((self.data.get("log") or {}).get("entry_format", DEFAULT_LOG_ENTRY_FORMAT))

    @property
    def task_folders(self) -> list[str]:
        return self.folders_of_kind("task")

    @property
    def system_folders(self) -> list[str]:
        return self.folders_of_kind("system")

    @property
    def reference_folders(self) -> list[str]:
        return self.folders_of_kind("reference")

    @property
    def unsearched_folders(self) -> list[str]:
        return [name for name, folder in self.folders.items() if not folder.searched]

    def _frontmatter(self) -> dict[str, Any]:
        return self.data.get("frontmatter") or {}

    @property
    def required_fields(self) -> list[str]:
        return list(self._frontmatter().get("required") or [])

    @property
    def optional_fields(self) -> list[str]:
        return list(self._frontmatter().get("optional") or [])

    @property
    def area_required_in(self) -> list[str]:
        return list(self._frontmatter().get("area_required_in") or [])

    @property
    def status_values(self) -> list[str]:
        return list(self._frontmatter().get("status_values") or [])

    @property
    def kind_rules(self) -> dict[str, str]:
        declared = self.data.get("kinds") or {}
        return {str(name): str(rule) for name, rule in declared.items() if rule is not None}

    @property
    def kind_values(self) -> list[str]:
        declared = self.data.get("kinds") or {}
        dropped = {str(name) for name, rule in declared.items() if rule is None}
        listed = [str(k) for k in (self._frontmatter().get("kind_values") or []) if str(k) not in dropped]
        return listed + [name for name in self.kind_rules if name not in listed]

    @property
    def priority_values(self) -> list[str]:
        return list(self._frontmatter().get("priority_values") or [])

    @property
    def tag_vocabulary(self) -> list[str]:
        return list((self.data.get("tags") or {}).get("vocabulary") or [])

    @property
    def tags_strict(self) -> bool:
        return bool((self.data.get("tags") or {}).get("strict", False))

    @property
    def synonyms(self) -> list[list[str]]:
        return [[str(word) for word in group] for group in (self.data.get("synonyms") or [])]

    def synonyms_of(self, term: str) -> list[str]:
        lowered = term.lower()
        found: list[str] = []
        for group in self.synonyms:
            if lowered in [word.lower() for word in group]:
                found.extend(word for word in group if word.lower() != lowered)
        return found

    @property
    def language(self) -> str:
        return str(self.data.get("language") or "en")

    @property
    def other_languages(self) -> list[str]:
        return [str(code) for code in (self.data.get("other_languages") or [])]

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([self.language, *self.other_languages]))

    @property
    def task_sections(self) -> dict[str, str]:
        by_language = (self.data.get("task") or {}).get("sections") or {}
        chosen = by_language.get(self.language) or by_language.get("en") or {}
        return {str(key): str(heading) for key, heading in chosen.items() if heading}

    @property
    def stale_after_days(self) -> int:
        return int(self.data.get("stale_after_days", 30))

    def _search(self) -> dict[str, Any]:
        return self.data.get("search") or {}

    @property
    def default_limit(self) -> int:
        return int(self._search().get("default_limit", 15))

    @property
    def recency_half_life_days(self) -> float:
        return float(self._search().get("recency_half_life_days", 90))

    @property
    def bm25_weights(self) -> tuple[float, float, float, float, float, float]:
        search = self._search()
        return (
            float(search.get("title_weight", 10.0)),
            float(search.get("summary_weight", 5.0)),
            float(search.get("tags_weight", 3.0)),
            float(search.get("body_weight", 1.0)),
            float(search.get("stem_weight", 20.0)),
            float(search.get("stemmed_weight", 0.5)),
        )


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def default_schema_data() -> dict[str, Any]:
    text = resources.files("notes_vault_mcp.templates").joinpath("schema.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def default_schema_text() -> str:
    return resources.files("notes_vault_mcp.templates").joinpath("schema.yml").read_text(encoding="utf-8")


def _override_from_backend(backend: VaultBackend) -> dict[str, Any]:
    try:
        text, _ = backend.get(SCHEMA_KEY)
    except Exception:
        return {}
    return yaml.safe_load(text) or {}


def load_schema(backend: VaultBackend | None = None, override_path: str | None = None) -> Schema:
    data = default_schema_data()
    if override_path:
        text = Path(override_path).expanduser().read_text(encoding="utf-8")
        return Schema(deep_merge(data, yaml.safe_load(text) or {}))
    if backend is not None:
        return Schema(deep_merge(data, _override_from_backend(backend)))
    return Schema(data)


def _folder_lines(schema: Schema) -> list[str]:
    lines = []
    for name, folder in schema.folders.items():
        suffix = " (not searched by default)" if not folder.searched else ""
        lines.append(f"- {name}/ — {folder.description}{suffix}")
    return lines


def _kind_lines(schema: Schema) -> str:
    rules = schema.kind_rules
    if not rules:
        return " " + ", ".join(schema.kind_values)
    return "".join(f"\n  - {kind}: {rules[kind]}" if kind in rules else f"\n  - {kind}" for kind in schema.kind_values)


def _language_line(schema: Schema) -> str:
    others = ", ".join(schema.languages[1:])
    if not others:
        return f"Search in the vault's language, {schema.language}; inflected forms match through stemming."
    return (
        f"Search in the vault's language, {schema.language}, first; inflected forms match through stemming. "
        f"When nothing matches, translate the terms into the vault's other languages ({others}) and search again."
    )


def instructions(schema: Schema) -> str:
    required = ", ".join(schema.required_fields)
    optional = ", ".join(schema.optional_fields)
    statuses = ", ".join(schema.status_values)
    kinds = _kind_lines(schema)
    priorities = ", ".join(schema.priority_values)
    priority_line = f"\n- priority, on a backlog note, is one of: {priorities}" if priorities else ""
    area_folders = ", ".join(schema.area_required_in)
    claim_line = (
        f"\n- {CLAIM_KEY}, on a task note, says which machine and task work on it: "
        "the claiming tool writes and clears it, write_file keeps it as stored, so never edit it; "
        "a note held by another machine is not yours to start"
        if CLAIM_KEY in schema.optional_fields
        else ""
    )
    tags = ", ".join(schema.tag_vocabulary)
    vocabulary = f"Tags come from this vocabulary: {tags}." if tags else "Tags are free-form."
    strictness = " Tags outside it are rejected." if schema.tags_strict else " It is a guide, not a gate."
    folders = "\n".join(_folder_lines(schema))
    language = _language_line(schema)
    return f"""\
The vault is the single source of truth for plans, decisions, progress and reference material.
This server indexes it, so search is cheap: run it before reading anything, and before writing code.

Folders and what each is for:
{folders}

Frontmatter contract for every note:
- required: {required}
- optional: {optional}
- status is one of: {statuses}
- kind is one of:{kinds}{priority_line}
- area is a wikilink to the system note, shaped [[stem]], and is required in: {area_folders}{claim_line}
{vocabulary}{strictness}

Workflow:
1. Start a session with `context` (path, repo) — it returns the system notes for the code you are
   about to touch, the open tasks, the reference notes and the tail of the repo log. Cheaper and
   more complete than searching blind.
   Its `triage` section lists open notes older than the stale window: settle each before new work,
   `close` when done, `append_file` a dated line when still moving, `set_status(path, "backlog",
   priority)` to park it.
2. Before the first edit to a repo, an open note for the work must exist. Write it with `write_file`.
3. Search with `search` before claiming anything about the current state. A note is a lead, the code
   is the truth: when the code disproves a note, correct the note in the same pass.
4. At the end of a session, `log_append` one line per repo with the commits it produced.
5. When work is finished, `close` the note — it moves to the archive and sets the status. A note
   left open is what makes the vault drift.
6. The moment something is deferred, in so many words or in passing, `backlog_add` it in the same
   turn: a task note with status backlog, its area, one line and a priority. `backlog` lists them by
   priority; picking one up is setting its status to active.

`search` excludes the archive and superseded notes unless you ask for them, and says how many it hid.
A wikilink [[stem]] is a search on the stem: that note comes first, even from the archive.
{language}
"""
