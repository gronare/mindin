from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol

from notes_vault_mcp.backends import SCHEMA_KEY, Entry, NotFound, VaultBackend, VaultError
from notes_vault_mcp.frontmatter import FrontmatterError, parse
from notes_vault_mcp.presets import SHARED_PRESET

CURRENT_KINDS = ("system", "howto", "decision", "reference", "trap")
FOLDER_KINDS: dict[str, tuple[str, ...]] = {
    "Areas": ("system",),
    "Resources": ("howto", "decision", "reference", "trap"),
    "Archive": CURRENT_KINDS,
}
REFUSED_TOOLS = frozenset({"backlog_add", "log_append", "claim", "release", "set_status", "lint_and_park", "init"})
HOLDS = (
    "A shared vault holds only how things are now: system notes in Areas/, howto, decision, reference and trap "
    "notes in Resources/, and retired ones in Archive/."
)
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


SERVER = Actor("notes-vault-mcp", None, "server")
_acting: ContextVar[Actor | None] = ContextVar("notes_vault_mcp_actor", default=None)


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


@dataclass(frozen=True)
class SharedRules:
    name: str

    def refuse_tool(self, tool: str) -> SharedRefusal:
        return SharedRefusal(f"{tool} is refused in the shared vault {self.name}. {HOLDS} {ELSEWHERE}")

    @property
    def context_line(self) -> str:
        return f"## shared vault\n{self.name} is a shared vault, so it has no tasks, backlog or log. {HOLDS}"

    def check_note(self, key: str, frontmatter: dict[str, Any]) -> None:
        folder = key.split("/", 1)[0] if "/" in key else ""
        kinds = FOLDER_KINDS.get(folder)
        if kinds is None:
            raise SharedRefusal(
                f"{key} is refused in the shared vault {self.name}: notes go in Areas/, Resources/ or Archive/. "
                f"{HOLDS} {ELSEWHERE}"
            )
        kind = frontmatter.get("kind")
        if kind is not None and str(kind) not in kinds:
            raise SharedRefusal(
                f"{key} is refused in the shared vault {self.name}: {folder}/ takes {', '.join(kinds)} notes, "
                f"not {kind}. {HOLDS} {ELSEWHERE}"
            )

    def check_write(self, key: str, data: bytes) -> None:
        if key == SCHEMA_KEY or key.startswith(".vault/"):
            raise SharedRefusal(f"{key} is refused: the layout of the shared vault {self.name} is fixed.")
        if not recorded(key) or not key.endswith(".md"):
            return
        try:
            frontmatter = parse(data.decode("utf-8"))[0]
        except (UnicodeDecodeError, FrontmatterError):
            frontmatter = {}
        self.check_note(key, frontmatter)


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
    preset = SHARED_PRESET

    def __init__(self, inner: Any, rules: SharedRules, recorder: Recorder) -> None:
        self.inner = inner
        self.rules = rules
        self.recorder = recorder

    def list(self) -> list[Entry]:
        return self.inner.list()

    def list_all(self, prefix: str = "") -> list[Entry]:
        return self.inner.list_all(prefix)

    def get(self, key: str) -> tuple[str, str]:
        return self.inner.get(key)

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        return self.inner.get_bytes(key)

    def mkdir(self, key: str) -> None:
        self.inner.mkdir(key)

    def previous(self, key: str) -> bytes | None:
        try:
            return self.inner.get_bytes(key)[0]
        except NotFound:
            return None

    def put(self, key: str, text: str, expected_version: str | None = None) -> str:
        return self.put_checked(key, text.encode("utf-8"), expected_version)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        return self.put_checked(key, data)

    def put_checked(self, key: str, data: bytes, expected_version: str | None = None, content_type: str = "") -> str:
        self.rules.check_write(key, data)
        before = self.previous(key) if recorded(key) else None
        version = self.inner.put_checked(key, data, expected_version)
        if recorded(key) and before != data:
            self.recorder.record(current_actor(), "created" if before is None else "updated", key, before, data)
        return version

    def delete(self, key: str) -> None:
        before = self.previous(key) if recorded(key) else None
        self.inner.delete(key)
        if before is not None:
            self.recorder.record(current_actor(), "deleted", key, before, None)

    def move(self, src: str, dst: str) -> None:
        data, _ = self.inner.get_bytes(src)
        self.rules.check_write(dst, data)
        self.inner.move(src, dst)
        if recorded(src) or recorded(dst):
            self.recorder.record(current_actor(), "moved", dst, data, data, from_path=src)


def shared_backend(backend: VaultBackend) -> SharedBackend | None:
    return backend if isinstance(backend, SharedBackend) else None
