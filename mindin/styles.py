from __future__ import annotations

import re
from dataclasses import dataclass

from mindin.backends import MUST_NOT_EXIST, NotFound, VaultBackend, VersionConflict

STYLES_FOLDER = ".vault/styles"
OVERRIDE_SUFFIX = ".override"
KIND_RE = re.compile(r"^\w[\w-]*$")
VERSION_RE = re.compile(r"<!--[ \t]*style-version:[ \t]*(\d+)[ \t]*-->[ \t]*\n?")
FRONTMATTER_RE = re.compile(r"---[ \t]*\r?\n.*?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
LEADING = "\ufeff \t\r\n"
STYLE_KEY_RE = re.compile(rf"^{re.escape(STYLES_FOLDER)}/(\w[\w-]*)({re.escape(OVERRIDE_SUFFIX)})?\.md$")
WRITE_RETRIES = 3


class StyleRefused(ValueError):
    pass


@dataclass(frozen=True)
class Style:
    kind: str
    text: str | None = None
    source: str | None = None
    has_default: bool = False

    @property
    def note(self) -> str | None:
        if self.source != "override":
            return None
        override = override_key(self.kind)
        if self.has_default:
            return f"The user's override {override} applies in place of the default {default_key(self.kind)}."
        return f"The user's override {override} applies; there is no default."

    def fields(self) -> dict[str, str | None]:
        return {"style": self.text, "style_source": self.source, "style_note": self.note}


def require_kind(kind: str) -> str:
    if not KIND_RE.match(kind or ""):
        raise StyleRefused(f"style kind {kind!r} is not a name of letters, digits, - and _")
    return kind


def default_key(kind: str) -> str:
    return f"{STYLES_FOLDER}/{require_kind(kind)}.md"


def override_key(kind: str) -> str:
    return f"{STYLES_FOLDER}/{require_kind(kind)}{OVERRIDE_SUFFIX}.md"


def style_key(key: str) -> tuple[str, bool] | None:
    found = STYLE_KEY_RE.match(key)
    return None if found is None else (found.group(1), found.group(2) is not None)


def _past_leading(text: str, at: int) -> int:
    return len(text) - len(text[at:].lstrip(LEADING))


def _stamp(text: str) -> re.Match[str] | None:
    at = _past_leading(text, 0)
    found = VERSION_RE.match(text, at)
    front = None if found else FRONTMATTER_RE.match(text, at)
    return found if front is None else VERSION_RE.match(text, _past_leading(text, front.end()))


def stamp_of(text: str | None) -> int | None:
    found = _stamp(text or "")
    return int(found.group(1)) if found else None


def version_of(text: str | None) -> int:
    return stamp_of(text) or 0


def stamped(text: str, version: int) -> str:
    line = f"<!-- style-version: {version} -->\n"
    found = _stamp(text)
    return line + text if found is None else text[: found.start()] + line + text[found.end() :]


def _stored(backend: VaultBackend, key: str) -> tuple[str | None, str]:
    try:
        return backend.get(key)
    except NotFound:
        return None, MUST_NOT_EXIST


def read_style(backend: VaultBackend, kind: str) -> Style:
    default, _ = _stored(backend, default_key(kind))
    override, _ = _stored(backend, override_key(kind))
    if override is not None:
        return Style(kind, override, "override", default is not None)
    if default is not None:
        return Style(kind, default, "default", True)
    return Style(kind)


def style_for(backend: VaultBackend, kind: str | None) -> Style | None:
    if kind is None or not KIND_RE.match(kind):
        return None
    return read_style(backend, kind)


def guidance(style: Style | None) -> str:
    if style is None or style.text is None:
        return ""
    return guidance_of(style.kind, style.text, style.source, style.note)


def guidance_of(kind: str, text: str, source: str | None, note: str | None) -> str:
    lines = [f"Style for {kind} notes, from the {source}: guidance for how the note reads, never a check."]
    lines += [note] if note else []
    return "\n".join(lines) + "\n\n" + text.strip() + "\n"


def _unchanged(kind: str, text: str, stored: str | None) -> str | None:
    key = default_key(kind)
    if stored is None:
        return None
    held = stamp_of(stored)
    if held is None:
        raise StyleRefused(
            f"{key} has no style-version stamp, so it may be the user's own and was left as it is; to replace it, "
            f"delete it first, or put the user's wording in {override_key(kind)}"
        )
    offered = version_of(text)
    if offered < held:
        raise StyleRefused(f"{key} holds style-version {held}, newer than the {offered} offered; it was left as it is")
    return f"{key} is already at style-version {held}; it was left as it is" if offered == held else None


def write_default(backend: VaultBackend, kind: str, text: str, expected_etag: str | None = None) -> str:
    key = default_key(kind)
    for _ in range(WRITE_RETRIES + 1):
        stored, version = _stored(backend, key)
        unchanged = _unchanged(kind, text, stored)
        if unchanged is not None:
            return f"Unchanged: {key}\netag: {version}\n{unchanged}"
        try:
            written = backend.put(key, text, expected_version=expected_etag or version)
        except VersionConflict:
            if expected_etag is not None:
                raise
            continue
        override, _ = _stored(backend, override_key(kind))
        applies = [] if override is None else [f"The user's override {override_key(kind)} applies in place of it."]
        return "\n".join([f"Written: {key}\netag: {written}", *applies])
    raise VersionConflict(f"{key} kept changing while it was being written; try again")


def write_refusal(key: str) -> str:
    return (
        f"{key} is the user's own override: mindin never writes it. Edit it as a file, in Obsidian, on disk or "
        "over WebDAV; write the default without .override"
    )
