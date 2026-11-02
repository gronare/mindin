from __future__ import annotations

from notes_vault_mcp import provision
from notes_vault_mcp.auth.hosted_web import esc
from notes_vault_mcp.backends import SCHEMA_KEY
from notes_vault_mcp.presets import OFFERED, PRESETS
from notes_vault_mcp.schema import broken_schema_lines, schema_from_text

BUILT_IN = "default"
SWITCHING = (
    "Switching writes a new schema and new Bases views. No note is moved, renamed or deleted: notes in folders "
    "the new layout does not name stay where they are and stay searchable. Each note is checked against the new "
    "rules the next time it is written, and lint lists the ones that no longer fit. The schema it replaces is "
    f"saved as {provision.PREVIOUS_SCHEMA_KEY}; views from the old layout that you never changed are removed."
)
EDITING = (
    f"The layout lives in {SCHEMA_KEY}, a file in your vault. To make it your own, edit it over WebDAV with any "
    "WebDAV client (Obsidian hides dot-folders, so it does not show it). If an edit does not hold, the vault keeps "
    "running on the layout the file names, and this page, context and lint say what is wrong."
)


def describe(named: str) -> str:
    if named in PRESETS:
        return f"This vault uses {PRESETS[named].title}."
    return f"This vault's schema names {named!r}, a layout this version does not ship; it is read as written."


def broken_box(text: str | None) -> str:
    lines = broken_schema_lines(schema_from_text(text)) if text is not None else []
    if not lines:
        return ""
    said = [f"<p>{esc(line)}</p>" for line in lines if not line.startswith("- ")]
    items = "".join(f"<li>{esc(line.removeprefix('- '))}</li>" for line in lines if line.startswith("- "))
    return f'<div class="notice">{"".join(said)}{f"<ul>{items}</ul>" if items else ""}</div>'


def layout_title(schema_text: str | None) -> str:
    named = provision.named_preset(schema_text)
    if named is None:
        return "no layout yet"
    if named in PRESETS:
        return f"{PRESETS[named].title.lower() if named == 'default' else PRESETS[named].title} layout"
    return f"{named} layout"


def layout_options(selected: str) -> str:
    return "".join(
        f'<label class="option"><input type="radio" name="preset" value="{esc(name)}" required'
        f"{' checked' if name == selected else ''}><b>{esc(PRESETS[name].title)}</b>"
        f"<span>{esc(PRESETS[name].summary)}</span></label>"
        for name in OFFERED
    )
