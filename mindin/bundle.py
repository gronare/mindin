from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mindin.schema import Schema

DEFAULT_SESSION_LIMIT = 5
BUNDLE_KEYS = {"instructions", "session"}


@dataclass(frozen=True)
class SessionRules:
    folders: tuple[str, ...]
    limit: int

    def shows(self, key: str) -> bool:
        return any(key.startswith(f"{folder}/") for folder in self.folders)


@dataclass(frozen=True)
class Bundle:
    instructions: str
    session: SessionRules | None

    @property
    def empty(self) -> bool:
        return not self.instructions and self.session is None


def _section(data: dict[str, Any]) -> dict[str, Any]:
    found = data.get("bundle")
    return found if isinstance(found, dict) else {}


def bundle_of(schema: Schema) -> Bundle:
    section = _section(schema.data)
    session = section.get("session")
    rules = None
    if isinstance(session, dict) and session.get("folders"):
        rules = SessionRules(
            tuple(str(folder).strip("/") for folder in session["folders"]),
            int(session.get("limit", DEFAULT_SESSION_LIMIT)),
        )
    return Bundle(str(section.get("instructions") or "").strip(), rules)


def bundle_problems(data: dict[str, Any]) -> list[str]:
    if "bundle" not in data or data["bundle"] is None:
        return []
    section = data["bundle"]
    if not isinstance(section, dict):
        return ["bundle must be a mapping with instructions: and session:"]
    problems: list[str] = []
    unknown = sorted(set(section) - BUNDLE_KEYS)
    if unknown:
        problems.append(f"bundle has unknown keys {', '.join(unknown)}; it takes {', '.join(sorted(BUNDLE_KEYS))}")
    if "instructions" in section and not isinstance(section["instructions"], str):
        problems.append("bundle.instructions must be text")
    session = section.get("session")
    if session is not None:
        problems += _session_problems(session, set((data.get("folders") or {}).keys()))
    return problems


def _session_problems(session: Any, folders: set[str]) -> list[str]:
    if not isinstance(session, dict):
        return ["bundle.session must be a mapping like { folders: [Areas], limit: 5 }"]
    problems: list[str] = []
    named = session.get("folders")
    if not isinstance(named, list) or not all(isinstance(folder, str) for folder in named):
        problems.append("bundle.session.folders must be a list of folders, like [Areas, Guides]")
    else:
        problems += [
            f"bundle.session.folders names '{folder}', which is not one of the folders"
            for folder in named
            if folder.strip("/") not in folders
        ]
    limit = session.get("limit", DEFAULT_SESSION_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        problems.append("bundle.session.limit must be a whole number, 1 or more")
    return problems


def bundle_text(schema: Schema) -> str:
    bundle = bundle_of(schema)
    if bundle.empty:
        return ""
    lines = ["", "This vault's own rules:"]
    if bundle.instructions:
        lines.append(bundle.instructions)
    if bundle.session is not None:
        folders = ", ".join(f"{folder}/" for folder in bundle.session.folders)
        lines.append(
            f"At session start, `context` shows at most {bundle.session.limit} pages from {folders} "
            "whose `repos` name the repo being worked in."
        )
    return "\n".join(lines) + "\n"
