from __future__ import annotations

import re
from typing import Any

import yaml

from notes_vault_mcp.schema import CLAIM_KEY, Schema

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
AREA_RE = re.compile(r"^\[\[[^\]|]+(\|[^\]]+)?\]\]$")
MAX_FRONTMATTER_CHARS = 64 * 1024
MAX_ALIASES = 8
CLAIM_FIELDS = ("machine", "task", "repo", "since")


class FrontmatterError(Exception):
    def __init__(self, message: str, line: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.line = line

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


class _BoundedLoader(yaml.SafeLoader):
    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.aliases = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            self.aliases += 1
            if self.aliases > MAX_ALIASES:
                raise FrontmatterError(f"more than {MAX_ALIASES} YAML aliases", self.peek_event().start_mark.line + 2)
        return super().compose_node(parent, index)


def parse(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    if len(match.group(1)) > MAX_FRONTMATTER_CHARS:
        raise FrontmatterError(f"frontmatter is longer than {MAX_FRONTMATTER_CHARS} characters")
    loader = _BoundedLoader(match.group(1) + "\n")
    try:
        loaded = loader.get_single_data()
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 2) if mark is not None else 1
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise FrontmatterError(problem, line) from exc
    finally:
        loader.dispose()
    if loaded is None:
        return {}, text[match.end() :]
    if not isinstance(loaded, dict):
        raise FrontmatterError("frontmatter is not a mapping", 2)
    return loaded, text[match.end() :]


class _Dumper(yaml.SafeDumper):
    def choose_scalar_style(self) -> str:
        style = super().choose_scalar_style()
        return '"' if style == "'" else style


def _represent_str(dumper: _Dumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value.rstrip("\n") else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


def _represent_none(dumper: _Dumper, _value: None) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


def _represent_list(dumper: _Dumper, value: list) -> yaml.SequenceNode:
    flat = all(not isinstance(item, (dict, list, tuple)) for item in value)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", value, flow_style=flat)


def _represent_dict(dumper: _Dumper, value: dict) -> yaml.MappingNode:
    return dumper.represent_mapping("tag:yaml.org,2002:map", value, flow_style=False)


_Dumper.add_representer(str, _represent_str)
_Dumper.add_representer(type(None), _represent_none)
_Dumper.add_representer(list, _represent_list)
_Dumper.add_representer(tuple, _represent_list)
_Dumper.add_representer(dict, _represent_dict)


def dump(frontmatter: dict[str, Any], body: str) -> str:
    block = (
        yaml.dump(frontmatter, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1 << 30)
        if frontmatter
        else ""
    )
    return f"---\n{block}---\n\n" + body.lstrip("\n")


def folder_of(key: str) -> str:
    return key.split("/")[0] if "/" in key else ""


def tags_of(frontmatter: dict[str, Any]) -> list[str]:
    raw = frontmatter.get("tags")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [str(raw).strip()] if str(raw).strip() else []


def _missing(frontmatter: dict[str, Any], field: str) -> bool:
    value = frontmatter.get(field)
    return value is None or value == "" or value == []


def claim_fields(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    fields = {name: "" if value.get(name) is None else str(value.get(name)).strip() for name in CLAIM_FIELDS}
    if not all(fields[name] for name in ("machine", "task", "since")):
        return None
    return fields


def validate(frontmatter: dict[str, Any], key: str, schema: Schema) -> list[str]:
    problems: list[str] = []
    for field in schema.required_fields:
        if _missing(frontmatter, field):
            problems.append(f"missing required field: {field}")

    tags = frontmatter.get("tags")
    if tags is not None and not isinstance(tags, (list, str)):
        problems.append(f"tags must be a list, not {type(tags).__name__}")

    status = frontmatter.get("status")
    if status is not None and str(status) not in schema.status_values:
        problems.append(f"status '{status}' is not one of: {', '.join(schema.status_values)}")

    kind = frontmatter.get("kind")
    if kind is not None and str(kind) not in schema.kind_values:
        problems.append(f"kind '{kind}' is not one of: {', '.join(schema.kind_values)}")

    priority = frontmatter.get("priority")
    if priority is not None and schema.priority_values and str(priority) not in schema.priority_values:
        problems.append(f"priority '{priority}' is not one of: {', '.join(schema.priority_values)}")

    claim = frontmatter.get(CLAIM_KEY)
    if claim is not None and claim_fields(claim) is None:
        problems.append(f"{CLAIM_KEY} must be a mapping with machine, task and since (and optionally repo)")

    folder = next((name for name in schema.area_required_in if key.startswith(f"{name}/")), None)
    if folder is not None:
        area = frontmatter.get("area")
        if area is None or str(area).strip() == "":
            problems.append(f'missing area: notes in {folder}/ must link their system note as area: "[[stem]]"')
        elif not AREA_RE.match(str(area).strip()):
            problems.append(f"area '{area}' must be a wikilink shaped [[stem]]")

    if schema.tags_strict:
        vocabulary = set(schema.tag_vocabulary)
        for tag in tags_of(frontmatter):
            if tag not in vocabulary:
                problems.append(f"tag '{tag}' is not in the vocabulary")

    return problems
