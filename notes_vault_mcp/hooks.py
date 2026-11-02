from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from notes_vault_mcp import changelog
from notes_vault_mcp.config import cache_dir
from notes_vault_mcp.git import current_branch, git, in_main_checkout, main_checkout, repo_name, repo_root
from notes_vault_mcp.notes import context, vault_paths_in
from notes_vault_mcp.vault import Vault, open_vault

SHA_PREFIX_LENGTH = 7
CHANGELOG_META = "changelog_ran_on"
VAULT_HINT = (
    "## how to read the vault\n"
    "The notes above live in the vault, not on disk. Read a note with the vault MCP tool `read_file(path)`, "
    "find others with `search`, file an idea with `backlog_add`, and never look for the vault's files in the "
    "filesystem: a wikilink [[stem]] is `search` on the stem, not a path."
)

__all__ = ["git", "repo_name", "repo_root"]


def read_hook_input(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


COMMITS_SHOWN = 10


def _commits_since(root: Path, since: str, paths: list[str]) -> str:
    if not since or not paths:
        return ""
    lines = git(["log", "--oneline", f"--since={since}", "--", *paths], root).splitlines()
    if len(lines) <= COMMITS_SHOWN:
        return "\n".join(lines)
    shown = "\n".join(lines[:COMMITS_SHOWN])
    return f"{len(lines)} commits, newest {COMMITS_SHOWN}:\n{shown}"


def commit_blocks(cwd: str, system: list[dict]) -> list[str]:
    root = repo_root(cwd)
    main = main_checkout(cwd)
    if root is None or main is None:
        return []
    blocks = []
    for note in system:
        # Note paths name the main checkout; git log takes them relative to the worktree it runs in.
        paths = [os.path.relpath(path, main) for path in vault_paths_in(note["paths"], main)]
        commits = _commits_since(root, note["updated"], paths)
        if commits:
            blocks.append(f"## commits since the note — {note['key']} (updated {note['updated']})\n{commits}")
    return blocks


def render_session_start(bundle: dict, cwd: str) -> str:
    return "\n\n".join([bundle["text"], *commit_blocks(cwd, bundle["system"]), VAULT_HINT])


def session_start_text(vault: Vault, cwd: str) -> str:
    repo = repo_name(cwd)
    bundle = context(vault, path=str(in_main_checkout(cwd)), repo=repo)
    main = main_checkout(cwd)
    if repo_root(cwd) is not None and main is not None:
        vault.index.remember_repo(repo, str(main))
    return render_session_start(bundle.as_dict(), cwd)


def session_start(raw: str) -> str:
    data = read_hook_input(raw)
    cwd = str(data.get("cwd") or os.getcwd())
    vault = open_vault()
    try:
        vault.index.sync()
        return session_start_text(vault, cwd)
    finally:
        vault.close()


RECENT = "24 hours ago"
# Reflog actions that create a commit; fast-forwards, resets and checkouts only move HEAD.
OWN_COMMIT_ACTION = re.compile(
    r"^(commit|cherry-pick|revert|am)\b"
    r"|^(rebase|pull)\b.*? \((pick|continue|reword|edit|squash|fixup)\): "
    r"|^(merge|pull)\b.*: Merge made by"
)


def _own_commit_shas(root: Path) -> set[str] | None:
    output = git(["reflog", "show", "--format=%H|%gs", "HEAD"], root)
    if not output:
        return None
    shas = set()
    for line in output.splitlines():
        sha, _, action = line.partition("|")
        if OWN_COMMIT_ACTION.match(action):
            shas.add(sha)
    return shas


def _recent_commits(root: Path) -> list[tuple[str, str]]:
    output = git(["log", f"--since={RECENT}", "--format=%H|%s"], root)
    own = _own_commit_shas(root)
    commits = []
    for line in output.splitlines():
        sha, _, subject = line.partition("|")
        sha = sha.strip()
        if sha and (own is None or sha in own):
            commits.append((sha[:SHA_PREFIX_LENGTH], subject.strip()))
    return commits


def _log_text(vault: Vault, repo: str) -> str:
    try:
        text, _ = vault.backend.get(vault.schema.log_key(repo))
    except Exception:
        return ""
    return text


def unlogged_in(logged: str, root: Path) -> list[tuple[str, str]]:
    return [(sha, subject) for sha, subject in _recent_commits(root) if sha not in logged]


def unlogged_commits(vault: Vault, root: Path, repo: str) -> list[tuple[str, str]]:
    return unlogged_in(_log_text(vault, repo), root)


def stale_labels(triage: list[dict]) -> list[tuple[str, str]]:
    return [(note["key"], f"{note['key']} ({note['age']} old)") for note in triage]


def stale_open_notes(vault: Vault, repo: str, path: str) -> list[tuple[str, str]]:
    return stale_labels(context(vault, path=path, repo=repo).as_dict()["triage"])


VAULT_WRITES = frozenset(
    {"write_file", "append_file", "backlog_add", "log_append", "close", "set_status", "move_file", "delete_file"}
)
WRAPPED_PROMPT = re.compile(
    r"\s*(<(bash-|command-|local-command-|task-notification|system-reminder)|\[Request interrupted by user)"
)
ASKED_LIMIT = 200
PROMPT_THRESHOLD = 3


def _vault_tool_uses(node: object) -> Iterator[dict]:
    if isinstance(node, dict):
        if node.get("type") == "tool_use" and "vault" in str(node.get("name", "")).lower():
            yield node
        for value in node.values():
            yield from _vault_tool_uses(value)
    elif isinstance(node, list):
        for value in node:
            yield from _vault_tool_uses(value)


def _collect_vault_paths(node: object, touched: set[str]) -> None:
    for tool_use in _vault_tool_uses(node):
        arguments = tool_use.get("input") or {}
        for key in ("path", "source", "dest"):
            value = arguments.get(key) if isinstance(arguments, dict) else None
            if isinstance(value, str):
                touched.add(value)


def is_vault_write(name: str) -> bool:
    server, _, tool = name.removeprefix("mcp__").rpartition("__")
    return name.startswith("mcp__") and "vault" in server.lower() and tool in VAULT_WRITES


def _prompt_texts(content: object) -> list[str] | None:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return None
    blocks = [block for block in content if isinstance(block, dict)]
    if any(block.get("type") == "tool_result" for block in blocks):
        return None
    return [str(block.get("text", "")) for block in blocks if block.get("type") == "text"]


def is_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or any(entry.get(flag) for flag in ("isMeta", "isCompactSummary", "isSidechain")):
        return False
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") != "human":
        return False
    texts = _prompt_texts((entry.get("message") or {}).get("content"))
    if not texts or not any(text.strip() for text in texts):
        return False
    return origin is not None or not all(WRAPPED_PROMPT.match(text) for text in texts)


@dataclass
class Transcript:
    touched: set[str] = field(default_factory=set)
    prompts_since_write: list[str] = field(default_factory=list)


def _read_entry(entry: dict, number: int, found: Transcript) -> None:
    for tool_use in _vault_tool_uses(entry):
        if is_vault_write(str(tool_use.get("name", ""))):
            found.prompts_since_write.clear()
    _collect_vault_paths(entry, found.touched)
    if is_prompt(entry):
        found.prompts_since_write.append(str(entry.get("uuid") or f"line-{number}"))


def read_transcript(transcript_path: str | None) -> Transcript:
    found = Transcript()
    if not transcript_path:
        return found
    path = Path(transcript_path)
    if not path.is_file():
        return found
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle):
            if '"user"' not in line and ("tool_use" not in line or "vault" not in line.lower()):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                _read_entry(entry, number, found)
    return found


def touched_notes(transcript_path: str | None) -> set[str]:
    return read_transcript(transcript_path).touched


class AskedPrompts:
    def __init__(self, data: dict, save: Callable[[], None]) -> None:
        self.data = data
        self.save = save

    def last(self, session: str) -> str | None:
        return self.data.get(session)

    def remember(self, session: str, prompt: str) -> None:
        self.data.pop(session, None)
        self.data[session] = prompt
        for stale in list(self.data)[:-ASKED_LIMIT]:
            del self.data[stale]
        self.save()


def local_asked_prompts() -> AskedPrompts:
    path = cache_dir() / "stop-prompts.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return AskedPrompts(data, lambda: path.write_text(json.dumps(data, indent=2), encoding="utf-8"))


def unsaved_prompts(found: Transcript, last_asked: str | None) -> list[str]:
    prompts = found.prompts_since_write
    if last_asked in prompts:
        return prompts[prompts.index(last_asked) + 1 :]
    return prompts


def _reason(
    commits: list[tuple[str, str]], stale: list[str], repo: str, branch: str | None = None, prompts: int = 0
) -> str:
    parts = []
    if commits:
        listed = ", ".join(f"{sha} {subject}" for sha, subject in commits)
        shas = [sha for sha, _ in commits]
        on_branch = f", branch='{branch}'" if branch else ""
        parts.append(f"log_append(repo='{repo}'{on_branch}, line='<what landed>', commits={shas}) — {listed}")
    if stale:
        parts.append("close these notes, or say in them what is still open: " + ", ".join(stale))
    if prompts:
        messages = "1 message" if prompts == 1 else f"{prompts} messages"
        parts.append(
            f"the user wrote {messages} since your last vault write: save any decision, deferral or idea from "
            "them in the vault (a task note, a Decisions line, backlog_add) before stopping, or stop again if "
            "there is nothing to save"
        )
    return "The vault is behind this session. " + "; ".join(parts)


def refresh_month_pages(vault: Vault, today: date | None = None) -> list[str]:
    stamp = (today or date.today()).isoformat()
    if vault.index.meta(CHANGELOG_META) == stamp:
        return []
    vault.index.set_meta(CHANGELOG_META, stamp)
    vault.index.db.commit()
    return changelog.write_all(vault, periods=changelog.periods_due(today or date.today()))


def _switched_off(name: str) -> bool:
    return (os.environ.get(name) or "").lower() == "off"


def prompt_threshold() -> int:
    raw = (os.environ.get("VAULT_STOP_HOOK_PROMPTS") or "").strip().lower()
    if raw == "off":
        return 0
    try:
        number = int(raw)
    except ValueError:
        return PROMPT_THRESHOLD
    return number if number >= 1 else PROMPT_THRESHOLD


@dataclass(frozen=True)
class StopTarget:
    data: dict
    cwd: str
    root: Path | None
    repo: str
    prompts: int


def stop_target(raw: str) -> StopTarget | None:
    data = read_hook_input(raw)
    if data.get("stop_hook_active") or _switched_off("VAULT_STOP_HOOK"):
        return None
    cwd = str(data.get("cwd") or os.getcwd())
    root = repo_root(cwd)
    prompts = prompt_threshold()
    if root is None and not prompts:
        return None
    return StopTarget(data=data, cwd=cwd, root=root, repo=repo_name(cwd) if root else "", prompts=prompts)


def stop(raw: str) -> str | None:
    target = stop_target(raw)
    if target is None:
        return None
    commits: list[tuple[str, str]] = []
    stale: list[tuple[str, str]] = []
    if target.root is not None:
        vault = open_vault()
        try:
            vault.index.sync()
            with contextlib.suppress(Exception):
                refresh_month_pages(vault)
            commits = unlogged_commits(vault, target.root, target.repo)
            stale = stale_open_notes(vault, target.repo, str(in_main_checkout(target.cwd)))
        finally:
            vault.close()
    return verdict(target, commits, stale, local_asked_prompts())


def verdict(
    target: StopTarget, commits: list[tuple[str, str]], stale: list[tuple[str, str]], asked: AskedPrompts
) -> str | None:
    found = read_transcript(target.data.get("transcript_path"))
    blocking = [label for key, label in stale if key in found.touched]
    session = str(target.data.get("session_id") or "")
    prompts = unsaved_prompts(found, asked.last(session)) if target.prompts and session else []
    if len(prompts) < target.prompts:
        prompts = []
    if not commits and not blocking and not prompts:
        return None
    if prompts:
        asked.remember(session, prompts[-1])
    branch = current_branch(target.root) if target.root is not None else None
    reason = _reason(commits, blocking, target.repo, branch, len(prompts))
    return json.dumps({"decision": "block", "reason": reason})
