from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from mindin.backends import PREVIOUS_SCHEMA_KEY, SCHEMA_KEY, Entry, NotFound, VaultBackend, VaultError, delete_checked
from mindin.frontmatter import FrontmatterError, parse
from mindin.presets import SHARED_LAYOUTS, SHARED_PRESET
from mindin.schema import Schema, schema_from_text

CURRENT_KINDS = ("system", "howto", "decision", "reference", "trap")
FOLDER_KINDS: dict[str, tuple[str, ...]] = {
    "Areas": ("system",),
    "Resources": ("howto", "decision", "reference", "trap"),
    "Archive": CURRENT_KINDS,
}
REFUSED_TOOLS = frozenset({"backlog_add", "log_append", "claim", "release", "set_status", "lint_and_park", "init"})
HOLDS = "A shared vault holds only how things are now, in the folders its layout names."
ELSEWHERE = "Keep tasks, the backlog and logs in your personal vault, through its own connection."
WHAT_BELONGS = (
    "How things are now: systems, how-tos, decisions and references. Plans, logs, backlogs and personal reasoning "
    "stay in each member's own vault, and the server refuses them here. Every change is kept with who made it."
)


@dataclass(frozen=True)
class Actor:
    email: str
    account_id: str | None
    client: str


SERVER = Actor("mindin", None, "server")
_acting: ContextVar[Actor | None] = ContextVar("mindin_actor", default=None)


@contextmanager
def acting_as(actor: Actor | None) -> Iterator[None]:
    token = _acting.set(actor)
    try:
        yield
    finally:
        _acting.reset(token)


def current_actor() -> Actor:
    explicit = _acting.get()
    if explicit is not None:
        return explicit
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    claims = (token.claims or {}) if token is not None else {}
    if token is not None and claims.get("email"):
        return Actor(str(claims["email"]), claims.get("account"), str(claims.get("client") or token.client_id))
    return SERVER


class SharedRefusal(VaultError):
    pass


def recorded(key: str) -> bool:
    return not key.endswith("/") and not any(segment.startswith(".") for segment in key.split("/"))


def in_layout(key: str) -> bool:
    return key.split("/", 1)[0] == ".vault"


@dataclass(frozen=True)
class SharedRules:
    name: str

    def refuse_tool(self, tool: str) -> SharedRefusal:
        return SharedRefusal(f"{tool} is refused in the shared vault {self.name}. {HOLDS} {ELSEWHERE}")

    @property
    def context_line(self) -> str:
        return f"## shared vault\n{self.name} is a shared vault, so it has no tasks, backlog or log. {HOLDS}"

    def check_note(self, key: str, frontmatter: dict[str, Any], schema: Schema) -> None:
        folder = key.split("/", 1)[0] if "/" in key else ""
        allowed = folder_kinds(schema)
        kinds = allowed.get(folder)
        if kinds is None:
            names = [f"{name}/" for name in allowed]
            where = ", ".join(names[:-1]) + f" or {names[-1]}" if len(names) > 1 else "".join(names)
            raise SharedRefusal(
                f"{key} is refused in the shared vault {self.name}: notes go in {where}. {HOLDS} {ELSEWHERE}"
            )
        kind = frontmatter.get("kind")
        if kind is not None and str(kind) not in kinds:
            raise SharedRefusal(
                f"{key} is refused in the shared vault {self.name}: {folder}/ takes {', '.join(kinds)} notes, "
                f"not {kind}. {HOLDS} {ELSEWHERE}"
            )

    def check_write(self, key: str, data: bytes, schema: Schema) -> None:
        if key == SCHEMA_KEY or key.startswith(".vault/"):
            raise SharedRefusal(
                f"{key} is refused: the layout of the shared vault {self.name} is fixed for its members; its "
                "owner or an organisation admin changes it."
            )
        if not recorded(key) or not key.endswith(".md"):
            return
        try:
            frontmatter = parse(data.decode("utf-8"))[0]
        except (UnicodeDecodeError, FrontmatterError):
            frontmatter = {}
        self.check_note(key, frontmatter, schema)


def folder_kinds(schema: Schema) -> dict[str, tuple[str, ...]]:
    declared = {name: folder.kinds for name, folder in schema.folders.items() if folder.kinds}
    return declared or FOLDER_KINDS


class Recorder(Protocol):
    def record(
        self,
        actor: Actor,
        action: str,
        path: str,
        before: bytes | None,
        after: bytes | None,
        from_path: str | None = None,
    ) -> None: ...


class SharedBackend:
    def __init__(
        self,
        inner: Any,
        rules: SharedRules,
        recorder: Recorder,
        guard: Callable[[Actor], str] | None = None,
        layout: str = SHARED_PRESET,
        edits_layout: Callable[[Actor], bool] | None = None,
    ) -> None:
        self.inner = inner
        self.layout = layout
        self.edits_layout = edits_layout
        self.rules = rules
        self.recorder = recorder
        self.guard = guard

    def allowed(self) -> Actor:
        actor = current_actor()
        refusal = self.guard(actor) if self.guard is not None else ""
        if refusal:
            raise SharedRefusal(refusal)
        return actor

    def list(self) -> list[Entry]:
        return self.inner.list()

    def list_all(self, prefix: str = "") -> list[Entry]:
        return self.inner.list_all(prefix)

    def get(self, key: str) -> tuple[str, str]:
        return self.inner.get(key)

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        return self.inner.get_bytes(key)

    def mkdir(self, key: str) -> None:
        self.guard_layout(self.allowed(), key)
        self.inner.mkdir(key)

    def forget_listing(self) -> None:
        self.inner.forget_listing()

    def previous(self, key: str) -> bytes | None:
        try:
            return self.inner.get_bytes(key)[0]
        except NotFound:
            return None

    def put(self, key: str, text: str, expected_version: str | None = None) -> str:
        return self.put_checked(key, text.encode("utf-8"), expected_version)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        return self.put_checked(key, data)

    def layout_editor(self, actor: Actor) -> bool:
        return self.edits_layout is not None and self.edits_layout(actor)

    def guard_layout(self, actor: Actor, *keys: str) -> None:
        if any(in_layout(key) for key in keys) and not self.layout_editor(actor):
            raise SharedRefusal(
                f"{next(key for key in keys if in_layout(key))} is refused: the layout of the shared vault "
                f"{self.rules.name} is fixed for its members; its owner or an organisation admin changes it."
            )

    def stored_schema(self) -> Schema:
        stored = self.previous(SCHEMA_KEY)
        if stored is None:
            raise SharedRefusal(
                f"The shared vault {self.rules.name} has no {SCHEMA_KEY}, so it takes no writes until its owner or "
                "an organisation admin puts the schema back. Nothing was written."
            )
        return schema_from_text(stored.decode("utf-8", "replace"))

    def put_checked(self, key: str, data: bytes, expected_version: str | None = None, content_type: str = "") -> str:
        actor = self.allowed()
        if key == SCHEMA_KEY and self.layout_editor(actor):
            return self.put_layout(data, expected_version)
        self.rules.check_write(key, data, self.stored_schema())
        before = self.previous(key) if recorded(key) else None
        version = self.inner.put_checked(key, data, expected_version)
        if recorded(key) and before != data:
            self.recorder.record(actor, "created" if before is None else "updated", key, before, data)
        return version

    def put_layout(self, data: bytes, expected_version: str | None) -> str:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SharedRefusal(f"{SCHEMA_KEY} must be UTF-8 text.") from exc
        schema = schema_from_text(text)
        problems = list(schema.problems) + list(schema.notices)
        if schema.preset not in SHARED_LAYOUTS:
            problems.append(f"preset must stay one of the shared layouts: {', '.join(SHARED_LAYOUTS)}")
        if problems:
            listed = "\n".join(f"- {problem}" for problem in problems)
            raise SharedRefusal(f"{SCHEMA_KEY} is refused for the shared vault {self.rules.name}:\n{listed}")
        previous = self.previous(SCHEMA_KEY)
        if previous is not None and previous != data:
            self.inner.put_checked(PREVIOUS_SCHEMA_KEY, previous)
        return self.inner.put_checked(SCHEMA_KEY, data, expected_version)

    def delete(self, key: str) -> None:
        actor = self.allowed()
        self.guard_layout(actor, key)
        before = self.previous(key) if recorded(key) else None
        self.inner.delete(key)
        if before is not None:
            self.recorder.record(actor, "deleted", key, before, None)

    def delete_checked(self, key: str, expected_version: str) -> None:
        actor = self.allowed()
        self.guard_layout(actor, key)
        before = self.previous(key) if recorded(key) else None
        delete_checked(self.inner, key, expected_version)
        if before is not None:
            self.recorder.record(actor, "deleted", key, before, None)

    def move(self, src: str, dst: str) -> None:
        actor = self.allowed()
        self.guard_layout(actor, src, dst)
        data, _ = self.inner.get_bytes(src)
        self.rules.check_write(dst, data, self.stored_schema())
        self.inner.move(src, dst)
        if recorded(src) or recorded(dst):
            self.recorder.record(actor, "moved", dst, data, data, from_path=src)


def shared_backend(backend: VaultBackend) -> SharedBackend | None:
    return backend if isinstance(backend, SharedBackend) else None
