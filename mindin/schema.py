from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

from mindin.backends import SCHEMA_KEY, VaultBackend
from mindin.bundle import bundle_problems, bundle_text
from mindin.presets import DEFAULT_PRESET, PRESETS, base_preset, preset_data

CLAIM_KEY = "kebbie"
CLAIM_STALE_DAYS = 14
DEFAULT_LOG_ENTRY_FORMAT = "- [{date}] {line} | branch: {branch} | commits: {commits} | {area}"
FOLDER_KINDS = ("system", "reference", "task", "log", "archive")
PRESET_LINE = re.compile(r"^preset:[ \t]*['\"]?([\w-]+)", re.MULTILINE)


class NoHome(Exception):
    pass


@dataclass(frozen=True)
class Section:
    key: str
    headings: dict[str, str]
    required: bool = False

    def heading(self, language: str) -> str:
        return self.headings.get(language) or self.headings.get("en") or next(iter(self.headings.values()), self.key)


@dataclass(frozen=True)
class Template:
    kind: str
    sections: tuple[Section, ...]
    questions: str | None = None

    @property
    def keys(self) -> list[str]:
        return [section.key for section in self.sections]

    @property
    def required_keys(self) -> list[str]:
        return [section.key for section in self.sections if section.required]

    def section(self, key: str) -> Section | None:
        return next((section for section in self.sections if section.key == key), None)


@dataclass(frozen=True)
class Folder:
    name: str
    kind: str
    weight: float
    searched: bool
    description: str
    kinds: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()


@dataclass(frozen=True)
class Schema:
    data: dict[str, Any]
    problems: tuple[str, ...] = field(default=(), compare=False)
    notices: tuple[str, ...] = field(default=(), compare=False)

    @property
    def preset(self) -> str:
        return str(self.data.get("preset") or DEFAULT_PRESET)

    @property
    def made_from(self) -> str | None:
        named = self.data.get("made_from")
        return str(named) if named else None

    @cached_property
    def folders(self) -> dict[str, Folder]:
        folders: dict[str, Folder] = {}
        for name, spec in (self.data.get("folders") or {}).items():
            if spec is None:
                continue
            folders[name] = Folder(
                name=name,
                kind=str(spec.get("kind", "reference")),
                weight=float(spec.get("weight", 1.0)),
                searched=bool(spec.get("searched", True)),
                description=str(spec.get("description", "")),
                kinds=tuple(str(kind) for kind in spec.get("kinds") or ()),
                statuses=tuple(str(status) for status in spec.get("statuses") or ()),
            )
        return folders

    def folders_of_kind(self, kind: str) -> list[str]:
        return [name for name, folder in self.folders.items() if folder.kind == kind]

    def folder_of(self, key: str) -> str:
        directory = key.rsplit("/", 1)[0] if "/" in key else ""
        declared = [name for name in self.folders if directory == name or directory.startswith(f"{name}/")]
        return max(declared, key=len) if declared else directory.split("/")[0]

    def under(self, key: str, folders: list[str]) -> bool:
        return any(key.startswith(f"{folder}/") for folder in folders)

    def folder_weight(self, folder: str) -> float:
        found = self.folders.get(folder)
        return found.weight if found else 1.0

    def no_home(self, home: str, tools: str, kind: str) -> NoHome:
        return NoHome(
            f"This vault's layout ({self.preset}) has no {home}, so {tools} have nowhere to go. "
            f"To use them, give a folder kind: {kind} in {SCHEMA_KEY}."
        )

    @property
    def archive_folder(self) -> str | None:
        found = self.folders_of_kind("archive")
        return found[0] if found else None

    @property
    def log_folder(self) -> str | None:
        configured = (self.data.get("log") or {}).get("folder")
        if configured:
            return str(configured).strip("/")
        found = self.folders_of_kind("log")
        return found[0] if found else None

    @property
    def log_file_format(self) -> str:
        return str((self.data.get("log") or {}).get("file_format", "{repo}-log.md"))

    def _log_home(self) -> str:
        if self.log_folder is None:
            raise self.no_home("log folder", "log_append and the monthly changelog pages", "log")
        return self.log_folder

    def log_key(self, repo: str) -> str:
        return f"{self._log_home()}/{self.log_file_format.format(repo=repo)}"

    @property
    def period_file_format(self) -> str:
        return str((self.data.get("log") or {}).get("period_format", "{repo}-{period}.md"))

    def period_key(self, repo: str, period: str) -> str:
        return f"{self._log_home()}/{self.period_file_format.format(repo=repo, period=period)}"

    @property
    def log_entry_format(self) -> str:
        return str((self.data.get("log") or {}).get("entry_format", DEFAULT_LOG_ENTRY_FORMAT))

    @property
    def task_folders(self) -> list[str]:
        return self.folders_of_kind("task")

    @property
    def task_folder(self) -> str:
        if not self.task_folders:
            raise self.no_home("task folder", "backlog_add and open tasks", "task")
        return self.task_folders[0]

    @property
    def backlog_folder(self) -> str:
        configured = str((self.data.get("backlog") or {}).get("folder") or "").strip("/")
        return configured if configured in self.task_folders else self.task_folder

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

    @cached_property
    def templates(self) -> dict[str, Template]:
        declared = self.data.get("templates") or {}
        kinds = self.kind_values
        return {
            str(kind): _template(str(kind), spec)
            for kind, spec in declared.items()
            if isinstance(spec, dict) and str(kind) in kinds
        }

    def template(self, kind: str | None) -> Template | None:
        return self.templates.get(str(kind)) if kind is not None else None

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


def _template(kind: str, spec: dict[str, Any]) -> Template:
    sections = []
    for item in spec.get("sections") or []:
        heading = item.get("heading")
        headings = {str(code): str(text) for code, text in heading.items()} if isinstance(heading, dict) else {}
        sections.append(Section(str(item["key"]), headings or {"en": str(heading)}, bool(item.get("required", False))))
    questions = spec.get("questions")
    return Template(kind, tuple(sections), str(questions) if questions else None)


LEGACY_REQUIRED = ("problem", "solution", "decisions")
LEGACY_SECTIONS = {
    "en": {
        "problem": "Problem",
        "solution": "Solution",
        "decisions": "Decisions",
        "tests": "Test decisions",
        "scope": "Out of scope",
    },
    "sv": {
        "problem": "Problem",
        "solution": "Lösning",
        "decisions": "Beslut",
        "tests": "Testbeslut",
        "scope": "Utanför ramen",
    },
}


SHIPPED_TASK_SECTIONS = (
    LEGACY_SECTIONS,
    {"en": LEGACY_SECTIONS["en"]},
    {"en": {**LEGACY_SECTIONS["en"], "problem": "Outcome", "solution": "Plan", "tests": "Done when"}},
)


def legacy_task_template(by_language: dict[str, Any]) -> dict[str, Any]:
    keys: list[str] = []
    for headings in by_language.values():
        keys += [str(key) for key in headings or {} if str(key) not in keys]
    sections = [
        {
            "key": key,
            "heading": {
                str(code): str(headings[key]) for code, headings in by_language.items() if (headings or {}).get(key)
            },
            "required": key in LEGACY_REQUIRED,
        }
        for key in keys
    ]
    return {"sections": sections, "questions": None}


def settled_templates(merged: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(merged.get("templates") or {}, dict):
        return merged
    named = override.get("templates") or {}
    templates = {**(merged.get("templates") or {}), **named}
    task = override.get("task") if isinstance(override.get("task"), dict) else {}
    legacy = task.get("sections")
    readable = isinstance(legacy, dict) and all(isinstance(headings, dict) for headings in legacy.values())
    if readable and legacy and legacy not in SHIPPED_TASK_SECTIONS and "task" not in named:
        templates = {**templates, "task": legacy_task_template(deep_merge(LEGACY_SECTIONS, legacy))}
    return {**merged, "templates": templates}


def default_schema_data() -> dict[str, Any]:
    return preset_data(DEFAULT_PRESET)


def _where(path: list[str]) -> str:
    return ".".join(path)


def _strings(value: Any, path: list[str], problems: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        problems.append(f"{_where(path)} must be a list of words, like [a, b]")
        return []
    return value


def _mapping(value: Any, path: list[str], problems: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{_where(path)} must be a mapping of name: value")
        return {}
    return value


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _folder_problems(name: Any, spec: Any, problems: list[str]) -> None:
    where = f"folders.{name}"
    segments = str(name).split("/")
    if not isinstance(name, str) or any(segment in ("", ".", "..") or segment.startswith(".") for segment in segments):
        problems.append(f"{where}: a folder is a vault path like Notes or Work/Logs, without dot segments")
    if spec is None:
        return
    if not isinstance(spec, dict):
        problems.append(f"{where} must be a mapping like {{ kind: reference, description: ... }}")
        return
    kind = spec.get("kind", "reference")
    if kind not in FOLDER_KINDS:
        problems.append(f"{where}.kind '{kind}' is not one of: {', '.join(FOLDER_KINDS)}")
    if "weight" in spec and (not _number(spec["weight"]) or spec["weight"] < 0):
        problems.append(f"{where}.weight must be a number, 0 or more")
    if "searched" in spec and not isinstance(spec["searched"], bool):
        problems.append(f"{where}.searched must be true or false")
    _strings(spec.get("kinds"), [where, "kinds"], problems)
    _strings(spec.get("statuses"), [where, "statuses"], problems)


def _format_problem(path: str, value: Any, fields: dict[str, str], needs: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return f"{path} must be text"
    missing = [name for name in needs if "{" + name + "}" not in value]
    if missing:
        return f"{path} must contain {', '.join('{' + name + '}' for name in missing)}"
    try:
        value.format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        return f"{path} names an unknown field {exc}; it may use {', '.join('{' + name + '}' for name in fields)}"
    return None


def schema_problems(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["the schema must be a mapping of keys like folders:, frontmatter:, tags:"]
    problems: list[str] = []
    if "preset" in data and not isinstance(data["preset"], str):
        problems.append(f"preset must be the name of a preset: {', '.join(PRESETS)}")
    if "made_from" in data and not isinstance(data["made_from"], str):
        problems.append("made_from must be the name of the layout the schema was copied from")
    if "root_description" in data and not isinstance(data["root_description"], str):
        problems.append("root_description must be text")
    for name, spec in _mapping(data.get("folders"), ["folders"], problems).items():
        _folder_problems(name, spec, problems)
    frontmatter = _mapping(data.get("frontmatter"), ["frontmatter"], problems)
    for key in ("required", "optional", "area_required_in", "status_values", "kind_values", "priority_values"):
        _strings(frontmatter.get(key), ["frontmatter", key], problems)
    for name, rule in _mapping(data.get("kinds"), ["kinds"], problems).items():
        if rule is not None and not isinstance(rule, str):
            problems.append(f"kinds.{name} must be a one-line rule, or null to drop the kind")
    tags = _mapping(data.get("tags"), ["tags"], problems)
    _strings(tags.get("vocabulary"), ["tags", "vocabulary"], problems)
    if "strict" in tags and not isinstance(tags["strict"], bool):
        problems.append("tags.strict must be true or false")
    synonyms = data.get("synonyms")
    if synonyms is not None and not (isinstance(synonyms, list) and all(isinstance(group, list) for group in synonyms)):
        problems.append("synonyms must be a list of word groups, like - [car, automobile]")
    _strings(data.get("other_languages"), ["other_languages"], problems)
    if "language" in data and not isinstance(data["language"], str):
        problems.append("language must be a language code, like en")
    stale = data.get("stale_after_days", 30)
    if not isinstance(stale, int) or isinstance(stale, bool) or stale < 1:
        problems.append("stale_after_days must be a whole number of days, 1 or more")
    for key, value in _mapping(data.get("search"), ["search"], problems).items():
        if not _number(value):
            problems.append(f"search.{key} must be a number")
    log = _mapping(data.get("log"), ["log"], problems)
    if log.get("folder") is not None and not isinstance(log.get("folder"), str):
        problems.append("log.folder must be a folder path, or left out")
    checks = (
        ("file_format", {"repo": "r"}, ("repo",)),
        ("period_format", {"repo": "r", "period": "2026-01"}, ("repo", "period")),
        ("entry_format", {"date": "d", "line": "l", "commits": "c", "branch": "b", "area": "a"}, ()),
    )
    for key, fields, needs in checks:
        if key in log:
            problem = _format_problem(f"log.{key}", log[key], fields, needs)
            if problem:
                problems.append(problem)
    _mapping(data.get("backlog"), ["backlog"], problems)
    task = _mapping(data.get("task"), ["task"], problems)
    for code, headings in _mapping(task.get("sections"), ["task", "sections"], problems).items():
        if not (isinstance(headings, dict) and all(isinstance(h, str) for h in headings.values())):
            problems.append(f"task.sections.{code} must map section keys to headings, like {{ goal: Goal }}")
        elif not all(_heading_holds(h) for h in headings.values() if h):
            problems.append(f"task.sections.{code} has a heading on more than one line or ending in #")
    for kind, spec in _mapping(data.get("templates"), ["templates"], problems).items():
        _template_problems(f"templates.{kind}", spec, problems)
    problems += bundle_problems(data)
    if problems:
        return problems
    return _contract_problems(Schema(data))


TEMPLATE_SHAPE = "{ sections: [{ key: goal, heading: { en: Goal }, required: true }], questions: null }"
SECTION_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def _heading_holds(text: str) -> bool:
    return len(text.splitlines()) == 1 and not text.rstrip().endswith("#")


def _template_problems(where: str, spec: Any, problems: list[str]) -> None:
    if spec is None:
        return
    if not isinstance(spec, dict):
        problems.append(f"{where} must be a mapping like {TEMPLATE_SHAPE}, or null to drop it")
        return
    unknown = [str(key) for key in spec if key not in ("sections", "questions")]
    if unknown:
        problems.append(f"{where} has {', '.join(unknown)}; a template takes sections and questions")
    sections = spec.get("sections")
    if not isinstance(sections, list) or not sections:
        problems.append(f"{where}.sections must be a list of sections, like {TEMPLATE_SHAPE}")
        return
    keys: list[str] = []
    for position, section in enumerate(sections):
        at = f"{where}.sections[{position}]"
        if not isinstance(section, dict):
            problems.append(f"{at} must be a mapping with key, heading and required")
            continue
        key = section.get("key")
        if not isinstance(key, str) or not SECTION_KEY.match(key):
            problems.append(f"{at}.key must be a lowercase word, like goal or out_of_scope")
        elif key in keys:
            problems.append(f"{at}.key {key} is already a section of {where}")
        else:
            keys.append(key)
        heading = section.get("heading")
        by_language = heading.values() if isinstance(heading, dict) else [heading]
        if not by_language or not all(isinstance(text, str) and text.strip() for text in by_language):
            problems.append(f"{at}.heading must be a heading per language, like {{ en: Goal, sv: Mål }}")
        elif not all(_heading_holds(text) for text in by_language):
            problems.append(f"{at}.heading must be one line that does not end in #, or no note could carry it")
        if "required" in section and not isinstance(section["required"], bool):
            problems.append(f"{at}.required must be true or false")
        extra = [str(name) for name in section if name not in ("key", "heading", "required")]
        if extra:
            problems.append(f"{at} has {', '.join(extra)}; a section takes key, heading and required")
    questions = spec.get("questions")
    if questions is not None and questions not in keys:
        problems.append(f"{where}.questions must name one of its sections ({', '.join(keys)}), or be null")


def _contract_problems(schema: Schema) -> list[str]:
    problems: list[str] = []
    statuses = schema.status_values
    needed = ["complete", "superseded"] + (["active", "backlog"] if schema.task_folders else [])
    missing = [status for status in needed if status not in statuses]
    if missing:
        problems.append(
            f"frontmatter.status_values lacks {', '.join(missing)}, which close"
            + (", backlog_add and set_status write" if schema.task_folders else " writes")
        )
    kinds = [("task", "the task folder")] if schema.task_folders else []
    kinds += [("log", "the log folder")] if schema.log_folder else []
    for kind, where in kinds:
        if kind not in schema.kind_values:
            problems.append(f"frontmatter.kind_values lacks {kind}, which the tools write in {where}")
    for folder in schema.folders.values():
        unknown = [kind for kind in folder.kinds if kind not in schema.kind_values]
        if unknown:
            problems.append(f"folders.{folder.name}.kinds names {', '.join(unknown)}, which kind_values lacks")
        unknown = [status for status in folder.statuses if status not in schema.status_values]
        if unknown:
            problems.append(f"folders.{folder.name}.statuses names {', '.join(unknown)}, which status_values lacks")
    for folder in schema.area_required_in:
        if folder not in schema.folders and folder != schema.log_folder:
            problems.append(f"frontmatter.area_required_in names '{folder}', which is not one of the folders")
    configured = (schema.data.get("backlog") or {}).get("folder")
    if configured and str(configured).strip("/") not in schema.task_folders:
        problems.append(f"backlog.folder '{configured}' is not a folder of kind task")
    return problems


def parse_schema_text(text: str) -> tuple[dict[str, Any], list[str]]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark is not None else ""
        problem = getattr(exc, "problem", None) or "this is not valid YAML"
        return {}, [f"{where}{problem}"]
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        return {}, ["the schema must be a mapping of keys like folders:, frontmatter:, tags:"]
    return data, []


def schema_from_text(text: str) -> Schema:
    override, problems = parse_schema_text(text)
    line = PRESET_LINE.search(text)
    named = override.get("preset") or (line.group(1) if line else None)
    base = preset_data(base_preset(named))
    notices = () if named is None or named in PRESETS else (unknown_preset(str(named)),)
    if problems:
        return Schema(base, tuple(problems), notices)
    merged = settled_templates(deep_merge(base, override), override)
    found = schema_problems(merged)
    return Schema(base, tuple(found), notices) if found else Schema(merged, (), notices)


def unknown_preset(name: str) -> str:
    return (
        f"preset '{name}' is not one this version ships ({', '.join(PRESETS)}), so the file is read as "
        "written, over the empty custom schema"
    )


def _schema_text(backend: VaultBackend) -> str | None:
    try:
        text, _ = backend.get(SCHEMA_KEY)
    except Exception:
        return None
    return text


def load_schema(backend: VaultBackend | None = None, override_path: str | None = None) -> Schema:
    if override_path:
        return schema_from_text(Path(override_path).expanduser().read_text(encoding="utf-8"))
    text = _schema_text(backend) if backend is not None else None
    return schema_from_text(text) if text is not None else Schema(default_schema_data())


def broken_schema_lines(schema: Schema) -> list[str]:
    lines = [f"{SCHEMA_KEY}: {notice}." for notice in schema.notices]
    if schema.problems:
        lines.append(f"{SCHEMA_KEY} does not hold, so the vault runs on the {schema.preset} layout until it is fixed:")
        lines += [f"- {problem}" for problem in schema.problems]
    return lines


@dataclass(frozen=True)
class Capability:
    tool: str
    on: bool
    reason: str
    folders: tuple[str, ...] = ()


def _named(word: str, folders: list[str]) -> str:
    return f"{word} folder{'s' if len(folders) > 1 else ''} {', '.join(folders)}"


def capabilities(schema: Schema) -> list[Capability]:
    tasks, log, archive, systems = schema.task_folders, schema.log_folder, schema.archive_folder, schema.system_folders
    task_reason = _named("task", tasks) if tasks else "no task folder"
    log_reason = _named("log", [log]) if log else "no log folder"
    close_reason = f"moves notes to {archive}" if archive else "keeps notes in place, no archive folder"
    if systems:
        loads = f"system notes from {', '.join(systems)}"
    else:
        loads = "notes of kind system" if "system" in schema.kind_values else ""
    return [
        Capability("backlog_add, backlog and task open", bool(tasks), task_reason, tuple(tasks)),
        Capability("log_append and the stop hook's commit check", bool(log), log_reason, (log,) if log else ()),
        Capability("close", True, close_reason, (archive,) if archive else ()),
        Capability(
            "session start", bool(loads), f"loads {loads}" if loads else "loads no system notes", tuple(systems)
        ),
    ]


def _folder_lines(schema: Schema) -> list[str]:
    root = schema.data.get("root_description")
    lines = [f"- the root — {root}"] if root else []
    for name, folder in schema.folders.items():
        suffix = " (not searched by default)" if not folder.searched else ""
        lines.append(f"- {name}/ — {folder.description}{suffix}")
    return lines


def _kind_lines(schema: Schema) -> str:
    rules = schema.kind_rules
    if not rules:
        return " " + ", ".join(schema.kind_values)
    return "".join(f"\n  - {kind}: {rules[kind]}" if kind in rules else f"\n  - {kind}" for kind in schema.kind_values)


def template_line(template: Template, language: str) -> str:
    headings = [f"## {section.heading(language)}{'*' if section.required else ''}" for section in template.sections]
    line = f"- {template.kind}: {', '.join(headings)}"
    questions = template.section(template.questions) if template.questions else None
    if questions is not None:
        line += (
            f". Under ## {questions.heading(language)}, `- [ ]` marks a question that blocks the start "
            "and `- ` one the work settles"
        )
    return line


def _template_block(schema: Schema) -> str:
    if not schema.templates:
        return ""
    lines = [template_line(template, schema.language) for template in schema.templates.values()]
    return (
        "\nNote templates, by kind (* is required). write_file refuses a new note, or a rewrite of one that follows "
        "its template, that lacks one of its kind's headings or leaves a required one empty; a backlog or draft note "
        "is a draft, held to nothing, and a note of only the headings is one just opened. An older note is brought to "
        "its template when it is next rewritten; lint lists those that are not. `template` gives the headings and a "
        "skeleton:\n" + "\n".join(lines)
    )


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
    area_line = (
        f"\n- area is a wikilink to the system note, shaped [[stem]], and is required in: {area_folders}"
        if area_folders
        else "\n- area, when set, is a wikilink to the system note, shaped [[stem]]"
    )
    log_step = (
        "4. At the end of a session, `log_append` one line per repo with the commits it produced."
        if schema.log_folder
        else "4. This layout keeps no repo log: `log_append` is off until the schema names a log folder."
    )
    close_step = (
        "5. When work is finished, `close` the note — it moves to the archive and sets the status. A note\n"
        "   left open is what makes the vault drift."
        if schema.archive_folder
        else "5. When work is finished, `close` the note — it sets the status; this layout has no archive, so\n"
        "   the note stays where it is. A note left open is what makes the vault drift."
    )
    backlog_step = (
        "6. The moment something is deferred, in so many words or in passing, `backlog_add` it in the same\n"
        "   turn: a task note with status backlog, its area, one line and a priority. `backlog` lists them by\n"
        "   priority; picking one up is setting its status to active"
        + (" and writing it out to the task template." if schema.template("task") else ".")
        if schema.task_folders
        else "6. This layout keeps no task folder: `backlog_add` and the backlog are off until the schema names one."
    )
    return f"""\
The vault is the single source of truth for plans, decisions, progress and reference material.
This server indexes it, so search is cheap: run it before reading anything, and before writing code.

Folders and what each is for:
{folders}

Frontmatter contract for every note:
- required: {required}
- optional: {optional}
- status is one of: {statuses}
- kind is one of:{kinds}{priority_line}{area_line}{claim_line}
{vocabulary}{strictness}{_template_block(schema)}

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
{log_step}
{close_step}
{backlog_step}

`search` excludes the archive and superseded notes unless you ask for them, and says how many it hid.
A wikilink [[stem]] is a search on the stem: that note comes first, even from the archive.
{language}
{bundle_text(schema)}"""


def shared_instructions(schema: Schema, name: str) -> str:
    area_folders = ", ".join(schema.area_required_in)
    area_line = (
        f"\n- area is a wikilink to the system note, shaped [[stem]], and is required in: {area_folders}"
        if area_folders
        else ""
    )
    tags = ", ".join(schema.tag_vocabulary)
    vocabulary = f"Tags come from this vocabulary: {tags}." if tags else "Tags are free-form."
    write_step = (
        "3. Write here only as this vault's own rules, below, say."
        if bundle_text(schema)
        else "3. Write a howto, decision, reference or trap in Resources/ when something is learned that others need."
    )
    return f"""\
This is {name}, a shared vault. Its owner and the members they invite read and write it, every change
applies at once, and each one is kept with who made it, so it can be shown and restored.

It holds only how things are now. Tasks, plans, the backlog, session logs and personal reasoning do not
belong here: they go in the user's personal vault, through its own connection. This server refuses them,
and refuses backlog_add, log_append, set_status, claim and release.

Folders and what each is for:
{chr(10).join(_folder_lines(schema))}

Frontmatter contract for every note:
- required: {", ".join(schema.required_fields)}
- optional: {", ".join(schema.optional_fields)}
- status is one of: {", ".join(schema.status_values)}
- kind is one of:{_kind_lines(schema)}{area_line}
{vocabulary}{_template_block(schema)}

Workflow:
1. Search with `search` before writing, and read the note you are about to change: other people keep
   it too. `context` (path, repo) returns the system notes for the code you are about to touch.
2. Correct a system note in place when the code or the facts change; it describes the present, never
   the history, which the server keeps.
{write_step}
4. When a note no longer holds, `close` it with merged_into the note that replaces it; it moves to the
   archive.

`search` excludes the archive and superseded notes unless you ask for them, and says how many it hid.
A wikilink [[stem]] is a search on the stem: that note comes first, even from the archive.
{_language_line(schema)}
{bundle_text(schema)}"""
