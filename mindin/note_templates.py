from __future__ import annotations

import re
from typing import Any

from mindin.schema import Schema, Section, Template, template_line

FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
DRAFT_STATUSES = ("backlog", "draft")
CLOSED_STATUSES = ("complete", "superseded")
DRAFT_MARKER = "_A draft from the backlog: write this note out before the work starts._"
DRAFT_LINE = "the backlog's draft line is still there: write the note out in place of it"


def heading_of(line: str) -> tuple[int, str] | None:
    level = len(line) - len(line.lstrip("#"))
    if not 1 <= level <= 6 or line[level : level + 1] not in (" ", "\t"):
        return None
    return level, line[level:].rstrip(" \t#").strip(" \t")


def filled_headings(body: str) -> dict[str, bool]:
    headings: list[tuple[int, str, int]] = []
    lines = body.splitlines()
    fenced = False
    for number, line in enumerate(lines):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        found = None if fenced else heading_of(line)
        if found:
            headings.append((found[0], found[1].casefold(), number))
    filled: dict[str, bool] = {}
    for position, (level, title, start) in enumerate(headings):
        end = next((line for lvl, _, line in headings[position + 1 :] if lvl <= level), len(lines))
        has_text = any(line.strip() for line in lines[start + 1 : end])
        filled[title] = filled.get(title, False) or has_text
    return filled


def section_state(section: Section, filled: dict[str, bool]) -> bool | None:
    found = [filled[text.strip().casefold()] for text in section.headings.values() if text.strip().casefold() in filled]
    return any(found) if found else None


def skeleton(template: Template | None, language: str) -> str:
    if template is None or not template.sections:
        return ""
    return "\n\n".join(f"## {section.heading(language)}" for section in template.sections) + "\n"


def written_body(template: Template, language: str, texts: dict[str, str]) -> str:
    blocks = []
    for section in template.sections:
        text = (texts.get(section.key) or "").strip()
        blocks.append(f"## {section.heading(language)}\n{text}" if text else f"## {section.heading(language)}")
    return "\n\n".join(blocks) + "\n"


def binds(frontmatter: dict[str, Any]) -> bool:
    return str(frontmatter.get("status") or "") not in (*DRAFT_STATUSES, *CLOSED_STATUSES)


def problems(schema: Schema, frontmatter: dict[str, Any], body: str) -> list[str]:
    template = schema.template(frontmatter.get("kind"))
    if template is None or not binds(frontmatter):
        return []
    filled = filled_headings(body)
    states = {section.key: section_state(section, filled) for section in template.sections}
    language = schema.language
    absent = [f"## {s.heading(language)}" for s in template.sections if states[s.key] is None]
    empty = [f"## {s.heading(language)}" for s in template.sections if s.required and states[s.key] is False]
    if not absent and not any(states.values()):
        return []
    found = [DRAFT_LINE] if DRAFT_MARKER in body else []
    found += [f"missing headings: {', '.join(absent)}"] if absent else []
    found += [f"required sections left empty: {', '.join(empty)}"] if empty else []
    return found


def refusal(schema: Schema, key: str, frontmatter: dict[str, Any], body: str) -> str:
    found = problems(schema, frontmatter, body)
    if not found:
        return ""
    template = schema.template(frontmatter.get("kind"))
    assert template is not None
    return "\n".join(
        [
            f"{key} does not follow the {template.kind} template:",
            *[f"- {problem}" for problem in found],
            f"The {template.kind} template (* is required): {template_line(template, schema.language)[2:]}",
            f"`template` with kind {template.kind} gives a skeleton; a backlog or draft note is held to nothing.",
        ]
    )


def describe(schema: Schema, template: Template) -> dict[str, Any]:
    return {
        "kind": template.kind,
        "language": schema.language,
        "sections": [
            {"key": s.key, "heading": s.heading(schema.language), "required": s.required} for s in template.sections
        ],
        "questions": template.questions,
        "skeleton": skeleton(template, schema.language),
    }


def template_answer(schema: Schema, kind: str | None = None) -> dict[str, Any]:
    if kind is None:
        return {"language": schema.language, "templates": {k: describe(schema, t) for k, t in schema.templates.items()}}
    if kind not in schema.kind_values:
        raise ValueError(f"kind '{kind}' is not one of: {', '.join(schema.kind_values)}")
    return describe(schema, schema.template(kind) or Template(kind, ()))
