from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import yaml

from mindin.backends import MUST_NOT_EXIST, PREVIOUS_SCHEMA_KEY, SCHEMA_KEY, NotFound, VaultBackend, VersionConflict
from mindin.presets import (
    CUSTOM_PRESET,
    DEFAULT_PRESET,
    DEFAULT_SCHEMA_TEMPLATE,
    PRESETS,
    STARTING_POINTS,
    Preset,
    base_preset,
    preset_data,
    preset_named,
    template,
)
from mindin.schema import Schema, deep_merge

BASES = PRESETS[DEFAULT_PRESET].files
PRESET_KEY_LINE = re.compile(r"^preset:.*$", re.MULTILINE)
VERSION_LINE = re.compile(r"^version:.*$", re.MULTILINE)
WELCOME_KEY = "Welcome.md"


@dataclass
class Provisioned:
    preset: str
    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    backup: str | None = None

    def lines(self) -> list[str]:
        lines = [f"wrote {key}" for key in self.written]
        lines += [f"kept {key} (force overwrites)" for key in self.kept]
        lines += [f"removed {key}: the previous layout's view, unchanged since it was written" for key in self.removed]
        if self.backup:
            lines.append(f"saved the schema it replaced as {self.backup}")
        return lines


def _fill(text: str, values: dict[str, str]) -> str:
    for name, value in values.items():
        text = text.replace("{{" + name + "}}", value)
    return text


def _loaded(schema_text: str | None) -> dict:
    try:
        loaded = yaml.safe_load(schema_text or "")
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _files(preset: Preset, schema_text: str) -> list[tuple[str, str]]:
    stale = str(_loaded(schema_text).get("stale_after_days", 30))
    return [(name, _fill(template(preset.file_template(name)), {"STALE_DAYS": stale})) for name in preset.files]


def _folder_list(schema_text: str, preset: Preset) -> str:
    schema = Schema(deep_merge(preset_data(preset.name), _loaded(schema_text)))
    root = schema.data.get("root_description")
    lines = [f"- the root: {root}"] if root else []
    lines += [f"- `{name}/`: {folder.description}" for name, folder in schema.folders.items()]
    return "\n".join(lines) or "- (none yet)"


def made_from(schema_text: str | None) -> str | None:
    named = _loaded(schema_text).get("made_from")
    return str(named) if named in STARTING_POINTS else None


def custom_schema(source: str | None) -> str:
    if source is None:
        return template(PRESETS[CUSTOM_PRESET].schema_template)
    if source not in STARTING_POINTS:
        raise ValueError(f"no layout named {source!r} to start from; the layouts are: {', '.join(STARTING_POINTS)}")
    copied = template(preset_named(source).schema_template)
    lines = f"preset: {CUSTOM_PRESET}\nmade_from: {source}"
    if PRESET_KEY_LINE.search(copied):
        return PRESET_KEY_LINE.sub(lines, copied, count=1)
    return VERSION_LINE.sub(lambda found: f"{found.group(0)}\n{lines}", copied, count=1)


def _views_of(schema_text: str | None) -> Preset:
    named = preset_of(schema_text)
    source = made_from(schema_text) if named == CUSTOM_PRESET else None
    return preset_named(source or named)


def stored(backend: VaultBackend, key: str) -> str | None:
    try:
        return backend.get(key)[0]
    except NotFound:
        return None


def named_preset(schema_text: str | None) -> str | None:
    if schema_text is None:
        return None
    named = _loaded(schema_text).get("preset")
    return str(named) if named is not None else DEFAULT_PRESET


def preset_of(schema_text: str | None) -> str | None:
    named = named_preset(schema_text)
    return None if named is None else base_preset(named)


def current_preset(backend: VaultBackend) -> str | None:
    return preset_of(stored(backend, SCHEMA_KEY))


def _untouched_bases(backend: VaultBackend, old: Preset, old_schema: str, keep: set[str]) -> list[str]:
    return [
        key
        for key, text in _files(old, old_schema)
        if key.endswith(".base") and key not in keep and stored(backend, key) == text
    ]


def initialize(
    backend: VaultBackend,
    force: bool = False,
    schema_template: str = DEFAULT_SCHEMA_TEMPLATE,
    welcome: dict[str, str] | None = None,
    preset: str | None = None,
    start_from: str | None = None,
) -> Provisioned:
    if start_from is not None and preset != CUSTOM_PRESET:
        raise ValueError(f"only the {CUSTOM_PRESET} layout starts from another layout")
    old_schema = stored(backend, SCHEMA_KEY)
    if preset is None and named_preset(old_schema) not in (None, *PRESETS):
        return Provisioned(str(named_preset(old_schema)), kept=[SCHEMA_KEY])
    named = preset or preset_of(old_schema)
    chosen = preset_named(named)
    if chosen.name == CUSTOM_PRESET:
        source = start_from if preset else made_from(old_schema)
        schema_text = custom_schema(source)
        views = preset_named(source) if source else chosen
    else:
        schema_text = template(chosen.schema_template if named else schema_template)
        views = chosen
    plan = [(SCHEMA_KEY, schema_text), *_files(views, schema_text)]
    if welcome is not None:
        values = {"DATE": date.today().isoformat(), "FOLDERS": _folder_list(schema_text, chosen), **welcome}
        plan.append((WELCOME_KEY, _fill(template("welcome.md"), values)))
    existing = {entry.key for entry in backend.list()}
    result = Provisioned(chosen.name)
    if force and old_schema is not None and old_schema != schema_text:
        backend.put(PREVIOUS_SCHEMA_KEY, old_schema)
        result.backup = PREVIOUS_SCHEMA_KEY
        old = _views_of(old_schema)
        if old.name != views.name:
            result.removed = _untouched_bases(backend, old, old_schema, {key for key, _ in plan})
    for key, text in plan:
        if not force and (key in existing or (key == SCHEMA_KEY and old_schema is not None)):
            result.kept.append(key)
            continue
        try:
            backend.put(key, text, expected_version=None if force else MUST_NOT_EXIST)
        except VersionConflict:
            result.kept.append(key)
            continue
        result.written.append(key)
    for key in result.removed:
        backend.delete(key)
    return result


def is_empty(backend: VaultBackend) -> bool:
    return not backend.list()
