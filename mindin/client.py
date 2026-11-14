from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, nullcontext
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, TypeVar

import anyio
from anyio.from_thread import BlockingPortal, start_blocking_portal

from mindin import changelog, credentials, feed, notes, tasks, views
from mindin.config import ConfigError, cache_dir, vault_id
from mindin.frontmatter import dump, parse
from mindin.git import current_branch, in_main_checkout, main_checkout, repo_name, repo_root
from mindin.remote import Remote, RemoteError, connect, refused, signed_in_key
from mindin.schema import NoHome, Schema
from mindin.search import render, search
from mindin.snapshot import build_snapshot
from mindin.vault import Vault, Vectoring, open_vault

T = TypeVar("T")
CHANGELOG_META = "changelog_ran_on"
ASKED_LIMIT = 200
REMOTE_BACKLOG_LIMIT = 10000
FIRST_PAUSE = 1.0
LONGEST_PAUSE = 60.0
Emit = Callable[[dict], None]


class ReaderGone(Exception):
    pass


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


class VaultClient(Protocol):
    def __enter__(self) -> VaultClient: ...
    def __exit__(
        self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...
    def context(self, cwd: str) -> dict: ...
    def context_view(self, cwd: str) -> dict | None: ...
    def remember_repo(self, cwd: str) -> None: ...
    def log_text(self, repo: str) -> str | None: ...
    def refresh_month_pages(self, today: date | None = None) -> list[str]: ...
    def asked_prompts(self) -> AskedPrompts: ...
    def changelog(self, repo: str, period: str, repo_path: Path | None, write: bool) -> str: ...
    def write_changelogs(self, period: str | None) -> list[str]: ...
    def lint(self, write: str | None, park: bool) -> tuple[str, notes.Findings]: ...
    def stale_after_days(self) -> int: ...
    def schema(self) -> Schema: ...
    def backlog_view(self, area: str | None, priority: str | None) -> dict | None: ...
    def backlog_text(self, area: str | None, priority: str | None) -> str: ...
    def search_view(self, query: str, limit: int | None) -> dict | None: ...
    def search_text(self, query: str, limit: int | None) -> str: ...
    def read(self, path: str) -> str: ...
    def task_open(self, args: Any) -> dict: ...
    def task_check(self, args: Any) -> dict: ...
    def task_close(self, args: Any) -> dict: ...
    def task_backlog(self, args: Any) -> dict: ...
    def task_claim(self, args: Any) -> dict: ...
    def task_release(self, args: Any) -> dict: ...
    def log_append(self, args: Any) -> dict: ...
    def log_path(self, args: Any) -> dict: ...
    def snapshot(self, args: Any) -> dict: ...
    def changes(self, args: Any) -> dict: ...
    def watch(self, args: Any, emit: Emit) -> None: ...


def open_client(vectors: Vectoring = "off") -> LocalClient | RemoteClient:
    url = credentials.configured_server()
    return LocalClient(vectors) if url is None else RemoteClient(url)


def where(cwd: str) -> dict[str, str]:
    return {"path": str(in_main_checkout(cwd)), "repo": repo_name(cwd)}


class LocalClient:
    def __init__(self, vectors: Vectoring = "off") -> None:
        self.vectors = vectors
        self._vault: Vault | None = None
        self._synced = False

    def __enter__(self) -> LocalClient:
        return self

    def __exit__(self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        if self._vault is not None:
            self._vault.close()

    @property
    def vault(self) -> Vault:
        if self._vault is None:
            self._vault = open_vault(vectors=self.vectors)
        return self._vault

    def synced(self) -> Vault:
        vault = self.vault
        if not self._synced:
            vault.index.sync()
            self._synced = True
        return vault

    def context(self, cwd: str) -> dict:
        return notes.context(self.synced(), **where(cwd)).as_dict()

    def context_view(self, cwd: str) -> dict | None:
        return views.context_bundle(self.synced(), **where(cwd)).view()

    def remember_repo(self, cwd: str) -> None:
        main = main_checkout(cwd)
        if repo_root(cwd) is not None and main is not None:
            self.vault.index.remember_repo(repo_name(cwd), str(main))

    def log_text(self, repo: str) -> str | None:
        vault = self.synced()
        try:
            text, _ = vault.backend.get(vault.schema.log_key(repo))
        except NoHome:
            return None
        except Exception:
            return ""
        return text

    def refresh_month_pages(self, today: date | None = None) -> list[str]:
        vault = self.synced()
        stamp = (today or date.today()).isoformat()
        if vault.index.meta(CHANGELOG_META) == stamp:
            return []
        vault.index.set_meta(CHANGELOG_META, stamp)
        vault.index.db.commit()
        return changelog.write_all(vault, periods=changelog.periods_due(today or date.today()))

    def asked_prompts(self) -> AskedPrompts:
        path = cache_dir() / "stop-prompts.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        return AskedPrompts(data, lambda: path.write_text(json.dumps(data, indent=2), encoding="utf-8"))

    def changelog(self, repo: str, period: str, repo_path: Path | None, write: bool) -> str:
        vault = self.synced()
        if write:
            changed = changelog.write_page(vault, repo, period, repo_path)
            return ("wrote " if changed else "unchanged ") + vault.schema.period_key(repo, period)
        return changelog.render(vault, repo, period, repo_path)

    def write_changelogs(self, period: str | None) -> list[str]:
        return changelog.write_all(self.synced(), periods=[period] if period else None)

    def lint(self, write: str | None, park: bool) -> tuple[str, notes.Findings]:
        self.vault.index.sync(force=True)
        self._synced = True
        findings = notes.lint_findings(self.vault, write=write, park=park)
        return findings.render(), findings

    def stale_after_days(self) -> int:
        return self.vault.schema.stale_after_days

    def schema(self) -> Schema:
        return self.vault.schema

    def backlog_view(self, area: str | None, priority: str | None) -> dict | None:
        return views.backlog_view(self.synced(), area=area, priority=priority)

    def backlog_text(self, area: str | None, priority: str | None) -> str:
        vault = self.synced()
        return notes.render_backlog(notes.backlog(vault, area=area, priority=priority), vault)

    def search_view(self, query: str, limit: int | None) -> dict | None:
        vault = self.synced()
        return views.search_view(search(vault.index, vault.schema, query, limit=limit))

    def search_text(self, query: str, limit: int | None) -> str:
        vault = self.synced()
        return render(search(vault.index, vault.schema, query, limit=limit))

    def read(self, path: str) -> str:
        return notes.read(self.vault, path).partition("\n")[2]

    def read_stored(self, path: str) -> dict:
        text, version = self.vault.backend.get(notes.note_key(path, readable=True))
        return {"text": text, "etag": version}

    def write_note(self, path: str, content: str, etag: str | None) -> dict:
        return written(notes.write(self.synced(), path, content, expected_etag=etag))

    def task_open(self, args: Any) -> dict:
        if args.note:
            return tasks.claim_task(self.synced(), args.note, args.cwd)
        return tasks.open_task(self.synced(), args.name or "", args.cwd, branch=args.branch)

    def task_check(self, args: Any) -> dict:
        return {"missing": tasks.missing_sections(self.synced(), args.path, tasks.needed(args.need))}

    def task_close(self, args: Any) -> dict:
        return {"path": notes.close(self.synced(), args.path)}

    def task_backlog(self, args: Any) -> dict:
        notes.set_status(self.synced(), args.path, notes.BACKLOG_STATUS, priority=args.priority)
        return {"path": args.path}

    def task_claim(self, args: Any) -> dict:
        return tasks.claim_note(
            self.synced(), args.path, args.machine, args.task, repo=args.repo or "", force=args.force
        )

    def task_release(self, args: Any) -> dict:
        if args.path is None:
            return tasks.release_claims_of(self.synced(), args.machine or "", args.task or "")
        return tasks.release_note(self.synced(), args.path, machine=args.machine)

    def log_append(self, args: Any) -> dict:
        return tasks.append_log(
            self.synced(), args.cwd, args.line, commits=tuple(args.commit or ()), branch=args.branch
        )

    def log_path(self, args: Any) -> dict:
        return tasks.log_paths(self.synced().schema, args.repo)

    def snapshot(self, args: Any) -> dict:
        return build_snapshot(self.synced(), since=args.since)

    def opened(self) -> nullcontext[Vault]:
        return nullcontext(self.vault)

    def changes(self, args: Any) -> dict:
        found = feed.changes(self.opened, args.since, args.limit, args.wait, included(args), longest=args.wait or 0)
        return asyncio.run(found)

    def watch(self, args: Any, emit: Emit) -> None:
        async def follow(at: dict[str, str | None]) -> None:
            async for found in feed.follow(self.opened, at["cursor"], included(args), args.limit):
                emit(found)
                at["cursor"] = found["cursor"]

        asyncio.run(again_and_again(follow, args.since, "the vault"))


class LocalState:
    def __init__(self, url: str) -> None:
        slug = credentials.vault_for(url)
        self.path = cache_dir() / f"remote-{vault_id(f'{url}#{slug}' if slug else url)}.json"
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.data: dict[str, Any] = data if isinstance(data, dict) else {}

    @property
    def repos(self) -> dict[str, str]:
        return self.data.setdefault("repos", {})

    def remember_repo(self, cwd: str) -> None:
        main = main_checkout(cwd)
        if repo_root(cwd) is not None and main is not None:
            self.repos[repo_name(cwd)] = str(main)
            self.save()

    def live_repos(self) -> list[tuple[str, str]]:
        live = {repo: root for repo, root in self.repos.items() if Path(root).exists() and repo_name(root) == repo}
        if live != self.repos:
            self.data["repos"] = live
            self.save()
        return sorted(live.items())

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


class RemoteClient:
    def __init__(self, url: str, key: str | None = None) -> None:
        self.url = url
        self.key = key
        self.state = LocalState(url)
        self._stack = ExitStack()
        self._portal: BlockingPortal | None = None
        self._remote: Remote | None = None

    def __enter__(self) -> RemoteClient:
        return self

    def __exit__(self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        try:
            self._stack.__exit__(kind, exc, tb)
        except BaseException as raised:
            error = refused(raised, self.url)
            if error is raised:
                raise
            raise error from None
        if exc is not None:
            error = refused(exc, self.url)
            if error is not exc:
                raise error from None

    def _on(self, action: Callable[[Remote], Awaitable[T]]) -> T:
        if self._portal is None or self._remote is None:
            key = self.key or signed_in_key(self.url)
            portal = self._stack.enter_context(start_blocking_portal())
            self._remote = self._stack.enter_context(portal.wrap_async_context_manager(connect(self.url, key)))
            self._portal = portal
        remote = self._remote
        return self._portal.call(lambda: action(remote))

    def tool_text(self, tool: str, **arguments: Any) -> str:
        return self._on(lambda remote: remote.call(tool, **arguments))

    def schema(self) -> Schema:
        return self._on(lambda remote: remote.schema())

    def view(self, command: str, **arguments: Any) -> dict | None:
        try:
            found = self.tool_text("view", command=command, **arguments)
        except RemoteError as exc:
            if "Unknown tool" in str(exc):
                return None
            raise
        return json.loads(found)

    def context(self, cwd: str) -> dict:
        return self._on(lambda remote: remote.context(cwd))

    def context_view(self, cwd: str) -> dict | None:
        return self.view("context", **where(cwd))

    def remember_repo(self, cwd: str) -> None:
        self.state.remember_repo(cwd)

    def log_text(self, repo: str) -> str | None:
        try:
            return self.read(self.schema().log_key(repo))
        except NoHome:
            return None
        except RemoteError:
            return ""

    def write_pages(self, repos: list[tuple[str, str]], periods: list[str]) -> list[str]:
        written = []
        for repo, root in repos:
            path = Path(root)
            if not (path / ".git").exists():
                continue
            for period in periods:
                commits = changelog.git_commits(path, period)
                answer = self.tool_text("changelog", repo=repo, period=period, commits=commits, write=True)
                if answer.startswith("wrote "):
                    written.append(answer.removeprefix("wrote "))
        return written

    def refresh_month_pages(self, today: date | None = None) -> list[str]:
        on = today or date.today()
        if self.state.data.get(CHANGELOG_META) == on.isoformat():
            return []
        self.state.data[CHANGELOG_META] = on.isoformat()
        self.state.save()
        if self.schema().log_folder is None:
            return []
        return self.write_pages(self.state.live_repos(), changelog.periods_due(on))

    def asked_prompts(self) -> AskedPrompts:
        return AskedPrompts(self.state.data.setdefault("prompts_asked", {}), self.state.save)

    def changelog(self, repo: str, period: str, repo_path: Path | None, write: bool) -> str:
        commits = changelog.git_commits(repo_path, period) if repo_path is not None else None
        return self.tool_text("changelog", repo=repo, period=period, commits=commits, write=write)

    def write_changelogs(self, period: str | None) -> list[str]:
        periods = [period] if period else changelog.periods_due(date.today())
        return self.write_pages(self.state.live_repos(), periods)

    def lint(self, write: str | None, park: bool) -> tuple[str, notes.Findings]:
        text = self.tool_text("lint_and_park", write=write, park=park) if write or park else self.tool_text("lint")
        return text, notes.Findings.parse(text)

    def stale_after_days(self) -> int:
        return self.schema().stale_after_days

    def backlog_view(self, area: str | None, priority: str | None) -> dict | None:
        return self.view("backlog", area=area, priority=priority)

    def backlog_text(self, area: str | None, priority: str | None) -> str:
        return self.tool_text("backlog", area=area, priority=priority, limit=REMOTE_BACKLOG_LIMIT)

    def search_view(self, query: str, limit: int | None) -> dict | None:
        return self.view("search", query=query, limit=limit)

    def search_text(self, query: str, limit: int | None) -> str:
        return self.tool_text("search", query=query, limit=limit)

    def read(self, path: str) -> str:
        return self._on(lambda remote: remote.read(path))

    def read_stored(self, path: str) -> dict:
        first, _, text = self.tool_text("read_file", path=path).partition("\n")
        return {"text": without_superseded_warning(text), "etag": first.removeprefix("etag:").strip()}

    def write_note(self, path: str, content: str, etag: str | None) -> dict:
        return written(self.tool_text("write_file", path=path, content=content, expected_etag=etag))

    def task_open(self, args: Any) -> dict:
        self.remember_repo(args.cwd)
        schema = self.schema()
        if args.note:
            key = tasks.task_key(schema, args.note)
            text = self.read(key)
            self.tool_text("set_status", path=key, status="active")
            return tasks.picked_up(key, text, args.cwd)
        tasks.require_name(args.name or "")
        located = tasks.located(args.cwd, self.context(args.cwd)["system"])
        branch = args.branch or current_branch(args.cwd)
        keys = self._on(lambda remote: remote.keys())
        key, text = tasks.new_task(schema, args.name, located, branch, keys)
        self.tool_text("write_file", path=key, content=text, expected_etag="*")
        return tasks.opened(key, located)

    def task_check(self, args: Any) -> dict:
        schema = self.schema()
        need = tasks.known_sections(schema, tasks.needed(args.need))
        return {"missing": tasks.missing_in(schema, self.read(notes.note_key(args.path)), need)}

    def task_close(self, args: Any) -> dict:
        return {"path": self.tool_text("close", path=args.path).removeprefix("Closed: ").strip()}

    def task_backlog(self, args: Any) -> dict:
        self.tool_text("set_status", path=args.path, status=notes.BACKLOG_STATUS, priority=args.priority)
        return {"path": args.path}

    def task_claim(self, args: Any) -> dict:
        found = self.tool_text(
            "claim", path=args.path, machine=args.machine, task=args.task, repo=args.repo or "", force=args.force
        )
        return json.loads(found)

    def task_release(self, args: Any) -> dict:
        return json.loads(self.tool_text("release", path=args.path, machine=args.machine, task=args.task))

    def log_append(self, args: Any) -> dict:
        self.remember_repo(args.cwd)
        if not args.line.strip():
            raise ValueError("a log line needs text")
        located = tasks.located(args.cwd, self.context(args.cwd)["system"])
        self.tool_text(
            "log_append",
            repo=located.repo,
            line=args.line.strip(),
            commits=list(args.commit or ()),
            area=located.area,
            branch=args.branch or current_branch(args.cwd),
        )
        return {"path": self.schema().log_key(located.repo)}

    def log_path(self, args: Any) -> dict:
        return tasks.log_paths(self.schema(), args.repo)

    def snapshot(self, args: Any) -> dict:
        return json.loads(self.tool_text("snapshot", since=args.since))

    def changes(self, args: Any) -> dict:
        deadline = time.monotonic() + (args.wait or 0)
        since = args.since
        while True:
            remaining = max(deadline - time.monotonic(), 0.0)
            wait = min(remaining, feed.MAX_WAIT_SECONDS)
            arguments = {"since": since, "limit": args.limit, "wait": wait or None, "include": included(args)}
            found = json.loads(self.tool_text("changes", **arguments))
            if feed.settled(found) or remaining <= wait:
                return found
            since = found["cursor"]

    def watch(self, args: Any, emit: Emit) -> None:
        key = self.key or signed_in_key(self.url)

        async def follow(at: dict[str, str | None]) -> None:
            first = True
            async with connect(self.url, key) as remote:
                while True:
                    arguments = {"since": at["cursor"], "limit": args.limit, "include": included(args)}
                    found = json.loads(await remote.call("changes", wait=feed.MAX_WAIT_SECONDS, **arguments))
                    if first or feed.settled(found):
                        emit(found)
                        first = False
                    at["cursor"] = found["cursor"]

        anyio.run(again_and_again, follow, args.since, self.url)


def written(answer: str) -> dict:
    head, _, tail = answer.partition("\n")
    etag, _, warning = tail.partition("\n")
    found = {"path": head.removeprefix("Written:").strip(), "etag": etag.removeprefix("etag:").strip()}
    return {**found, "warning": warning} if warning else found


def without_superseded_warning(text: str) -> str:
    frontmatter, body = parse(text)
    warning = f"> [!warning] Superseded by {frontmatter.get('superseded_by')}\n\n"
    if frontmatter.get("superseded_by") and body.startswith(warning):
        return dump(frontmatter, body.removeprefix(warning))
    return text


def included(args: Any) -> str:
    return "body" if getattr(args, "body", False) else "frontmatter"


def lasting(error: BaseException) -> bool:
    return isinstance(error, ConfigError) or any(
        sign in str(error) for sign in ("refused the stored key", "Unknown tool", "include is one of")
    )


async def again_and_again(
    follow: Callable[[dict[str, str | None]], Awaitable[None]], since: str | None, where: str
) -> None:
    at, pause = {"cursor": since}, FIRST_PAUSE
    while True:
        started = time.monotonic()
        try:
            await follow(at)
        except Exception as exc:
            error = refused(exc, where)
            if isinstance(error, ReaderGone) or lasting(error):
                raise error from None
            if time.monotonic() - started > LONGEST_PAUSE:
                pause = FIRST_PAUSE
            print(
                f"mindin: watch lost {where} ({str(error).splitlines()[0][:200]}); again in {pause:.0f}s",
                file=sys.stderr,
            )
            await anyio.sleep(pause)
            pause = min(pause * 2, LONGEST_PAUSE)
