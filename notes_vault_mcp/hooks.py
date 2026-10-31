from __future__ import annotations

import contextlib
import json
import os
import re
import time
from datetime import date
from pathlib import Path

from notes_vault_mcp import changelog
from notes_vault_mcp.git import current_branch, git, in_main_checkout, main_checkout, repo_name, repo_root
from notes_vault_mcp.notes import context, vault_paths_in
from notes_vault_mcp.search import age_days, humanize_age
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


def session_start_text(vault: Vault, cwd: str) -> str:
    repo = repo_name(cwd)
    bundle = context(vault, path=str(in_main_checkout(cwd)), repo=repo)
    blocks = [bundle.render()]
    root = repo_root(cwd)
    main = main_checkout(cwd)
    if root is not None and main is not None:
        vault.index.remember_repo(repo, str(main))
        for note, _ in bundle.system:
            # Note paths name the main checkout; git log takes them relative to the worktree it runs in.
            paths = [os.path.relpath(path, main) for path in vault_paths_in(note, main)]
            commits = _commits_since(root, note.updated, paths)
            if commits:
                blocks.append(f"## commits since the note — {note.key} (updated {note.updated})\n{commits}")
    blocks.append(VAULT_HINT)
    return "\n\n".join(blocks)


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


def unlogged_commits(vault: Vault, root: Path, repo: str) -> list[tuple[str, str]]:
    logged = _log_text(vault, repo)
    return [(sha, subject) for sha, subject in _recent_commits(root) if sha not in logged]


def stale_open_notes(vault: Vault, repo: str, path: str) -> list[tuple[str, str]]:
    now = time.time()
    bundle = context(vault, path=path, repo=repo)
    return [(note.key, f"{note.key} ({humanize_age(age_days(note, now))} old)") for note in bundle.triage]


def _collect_vault_paths(node: object, touched: set[str]) -> None:
    if isinstance(node, dict):
        if node.get("type") == "tool_use" and "vault" in str(node.get("name", "")).lower():
            arguments = node.get("input") or {}
            for key in ("path", "source", "dest"):
                value = arguments.get(key) if isinstance(arguments, dict) else None
                if isinstance(value, str):
                    touched.add(value)
        for value in node.values():
            _collect_vault_paths(value, touched)
    elif isinstance(node, list):
        for value in node:
            _collect_vault_paths(value, touched)


def touched_notes(transcript_path: str | None) -> set[str]:
    touched: set[str] = set()
    if not transcript_path:
        return touched
    path = Path(transcript_path)
    if not path.is_file():
        return touched
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "tool_use" not in line or "vault" not in line.lower():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            _collect_vault_paths(entry, touched)
    return touched


def _reason(commits: list[tuple[str, str]], stale: list[str], repo: str, branch: str | None = None) -> str:
    parts = []
    if commits:
        listed = ", ".join(f"{sha} {subject}" for sha, subject in commits)
        shas = [sha for sha, _ in commits]
        on_branch = f", branch='{branch}'" if branch else ""
        parts.append(f"log_append(repo='{repo}'{on_branch}, line='<what landed>', commits={shas}) — {listed}")
    if stale:
        parts.append("close these notes, or say in them what is still open: " + ", ".join(stale))
    return "The vault is behind this session. " + "; ".join(parts)


def refresh_month_pages(vault: Vault, today: date | None = None) -> list[str]:
    stamp = (today or date.today()).isoformat()
    if vault.index.meta(CHANGELOG_META) == stamp:
        return []
    vault.index.set_meta(CHANGELOG_META, stamp)
    vault.index.db.commit()
    return changelog.write_all(vault, periods=changelog.periods_due(today or date.today()))


def stop(raw: str) -> str | None:
    data = read_hook_input(raw)
    if data.get("stop_hook_active"):
        return None
    if (os.environ.get("VAULT_STOP_HOOK") or "").lower() == "off":
        return None
    cwd = str(data.get("cwd") or os.getcwd())
    root = repo_root(cwd)
    if root is None:
        return None
    repo = repo_name(cwd)
    vault = open_vault()
    try:
        vault.index.sync()
        with contextlib.suppress(Exception):
            refresh_month_pages(vault)
        commits = unlogged_commits(vault, root, repo)
        stale = stale_open_notes(vault, repo, str(in_main_checkout(cwd)))
    finally:
        vault.close()
    touched = touched_notes(data.get("transcript_path"))
    blocking = [label for key, label in stale if key in touched]
    if not commits and not blocking:
        return None
    return json.dumps({"decision": "block", "reason": _reason(commits, blocking, repo, current_branch(root))})
