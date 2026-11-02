from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml

DEFAULT_PRESET = "default"
CUSTOM_PRESET = "custom"
SHARED_PRESET = "shared"


@dataclass(frozen=True)
class Preset:
    name: str
    title: str
    summary: str
    directory: str
    files: tuple[str, ...]
    offered: bool = True

    @property
    def schema_template(self) -> str:
        return f"{self.directory}schema.yml"

    def file_template(self, name: str) -> str:
        return f"{self.directory}{name}"


PRESETS: dict[str, Preset] = {
    preset.name: preset
    for preset in (
        Preset(
            "default",
            "Built-in",
            "Areas, Resources, Projects, Log, Archive: one living note per system, open work, a log per repo.",
            "",
            ("Areas.base", "Open tasks.base", "Resources.base", "Backlog.base", "Log.base"),
            offered=False,
        ),
        Preset(
            "para",
            "PARA",
            "Projects, Areas, Resources and Archive, sorted by how actionable each note is.",
            "presets/para/",
            ("Projects.base", "Backlog.base", "Areas.base", "Resources.base", "Log.base"),
        ),
        Preset(
            "simple",
            "Simple",
            "Most notes in the root, typed by a categories property, with open work in Projects.",
            "presets/simple/",
            (
                "Open work.base",
                "Backlog.base",
                "Categories.base",
                "Recent.base",
                "Templates/Bases/Category.base",
                "Templates/Bases/Daily.base",
                "Templates/Category Template.md",
                "Templates/Daily Note Template.md",
                "Templates/Project Template.md",
            ),
        ),
        Preset(
            "journal",
            "Journal",
            "A note for every day, with open work, a log per repo and an archive beside them.",
            "presets/journal/",
            (
                "Days.base",
                "Open work.base",
                "Backlog.base",
                "Log.base",
                "Templates/Bases/Day.base",
                "Templates/Daily Note Template.md",
            ),
        ),
        Preset(
            "custom",
            "Custom",
            "An empty schema you fill in yourself.",
            "presets/custom/",
            ("All notes.base",),
        ),
        Preset(
            SHARED_PRESET,
            "Shared",
            "Areas for system notes, Resources for how-tos, decisions, references and traps, and Archive.",
            "presets/shared/",
            ("Areas.base", "Resources.base"),
            offered=False,
        ),
    )
}
OFFERED = tuple(name for name, preset in PRESETS.items() if preset.offered)
PERSONAL_LAYOUTS = tuple(name for name in PRESETS if name != SHARED_PRESET)


def template(name: str) -> str:
    return resources.files("notes_vault_mcp.templates").joinpath(name).read_text(encoding="utf-8")


def preset_named(name: str | None) -> Preset:
    chosen = PRESETS.get(name or DEFAULT_PRESET)
    if chosen is None:
        raise ValueError(f"no preset named {name!r}; the presets are: {', '.join(PRESETS)}")
    return chosen


def base_preset(name: str | None) -> str:
    if name is None:
        return DEFAULT_PRESET
    return name if name in PRESETS else CUSTOM_PRESET


def preset_data(name: str | None) -> dict[str, Any]:
    return yaml.safe_load(template(preset_named(name).schema_template)) or {}
