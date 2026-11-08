from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, TypeVar

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Implementation

from mindin import changelog, credentials, hooks, notes, tasks
from mindin.backends import SCHEMA_KEY
from mindin.config import ConfigError, cache_dir, vault_id
from mindin.git import current_branch, in_main_checkout, main_checkout, remote_url, repo_name, repo_root
from mindin.schema import NoHome, Schema, default_schema_data, schema_from_text

T = TypeVar("T")
HTTP_TIMEOUT = httpx2.Timeout(20.0, read=60.0)
NO_FILES = "No files under "


class RemoteError(Exception):
    pass


class NotSignedIn(ConfigError):
    def __init__(self, url: str, vault: str | None = None) -> None:
        if vault:
            super().__init__(
                f"mindin: not signed in to the vault {vault} on {url}; run `mindin login {url} --vault {vault}`"
            )
        else:
            super().__init__(f"mindin: not signed in to {url}; run `mindin login {url}`")


def signed_in_key(url: str, cwd: str | None = None) -> str:
    vault, entry = credentials.chosen(url, cwd)
    if entry is None:
        raise NotSignedIn(url, vault)
    return entry["key"]


def client_info() -> Implementation:
    from mindin import __version__

    return Implementation(name="mindin", version=__version__)


class Remote:
    def __init__(self, url: str, client: Client) -> None:
        self.url = url
        self.client = client
        self._schema: Schema | None = None

    async def call(self, tool: str, **arguments: Any) -> str:
        result = await self.client.call_tool(
            tool, {name: value for name, value in arguments.items() if value is not None}
        )
        text = "".join(getattr(block, "text", "") for block in result.content)
        if result.is_error:
            raise RemoteError(text.strip() or f"{tool} failed on {self.url}")
        return text

    async def read(self, path: str) -> str:
        text = await self.call("read_file", path=path)
        first, _, rest = text.partition("\n")
        return rest if first.startswith("etag:") else text

    async def schema(self) -> Schema:
        if self._schema is None:
            try:
                self._schema = schema_from_text(await self.read(SCHEMA_KEY))
            except RemoteError:
                self._schema = Schema(default_schema_data())
        return self._schema

    async def context(self, cwd: str) -> dict:
        found = await self.call(
            "context",
            path=str(in_main_checkout(cwd)),
            repo=repo_name(cwd),
            remote=remote_url(cwd) or None,
            format="json",
        )
        return json.loads(found)

    async def keys(self) -> list[str]:
        listing = await self.call("list_files")
        return [] if listing.startswith(NO_FILES) else [line for line in listing.splitlines() if line]


def unwrap(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def key_refused(url: str, status: int) -> RemoteError:
    return RemoteError(f"{url} refused the stored key ({status}); sign in again with `mindin login {url}`")


def refused(exc: BaseException, url: str) -> Exception:
    exc = unwrap(exc)
    if isinstance(exc, RemoteError | ConfigError):
        return exc
    if isinstance(exc, httpx2.HTTPError):
        return RemoteError(f"cannot reach {url}: {exc}")
    return exc if isinstance(exc, Exception) else RemoteError(str(exc))


@asynccontextmanager
async def connect(url: str, key: str | None = None) -> AsyncIterator[Remote]:
    token = key or signed_in_key(url)
    refusals: list[int] = []

    async def note_refusal(response: httpx2.Response) -> None:
        if response.status_code in (401, 403):
            refusals.append(response.status_code)

    try:
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT,
                event_hooks={"response": [note_refusal]},
            ) as http,
            Client(streamable_http_client(f"{url}/mcp", http_client=http), client_info=client_info()) as client,
        ):
            yield Remote(url, client)
    except BaseException:
        if refusals:
            raise key_refused(url, refusals[0]) from None
        raise


def run(url: str, action: Callable[[Remote], Awaitable[T]]) -> T:
    async def go() -> T:
        async with connect(url) as remote:
            return await action(remote)

    try:
        return anyio.run(go)
    except BaseException as exc:
        error = refused(exc, url)
        if error is exc:
            raise
        raise error from None


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


def read_cwd(raw: str) -> tuple[dict, str]:
    data = hooks.read_hook_input(raw)
    return data, str(data.get("cwd") or os.getcwd())


def session_start(url: str, raw: str) -> str:
    _, cwd = read_cwd(raw)

    async def action(remote: Remote) -> str:
        return hooks.render_session_start(await remote.context(cwd), cwd)

    with credentials.working_in(cwd):
        text = run(url, action)
        LocalState(url).remember_repo(cwd)
    return text


async def write_pages(remote: Remote, repos: list[tuple[str, str]], periods: list[str]) -> list[str]:
    written = []
    for repo, root in repos:
        path = Path(root)
        if not (path / ".git").exists():
            continue
        for period in periods:
            answer = await remote.call(
                "changelog", repo=repo, period=period, commits=changelog.git_commits(path, period), write=True
            )
            if answer.startswith("wrote "):
                written.append(answer.removeprefix("wrote "))
    return written


async def refresh_month_pages(remote: Remote, state: LocalState, today: date | None = None) -> list[str]:
    on = today or date.today()
    if state.data.get(hooks.CHANGELOG_META) == on.isoformat():
        return []
    state.data[hooks.CHANGELOG_META] = on.isoformat()
    state.save()
    if (await remote.schema()).log_folder is None:
        return []
    return await write_pages(remote, state.live_repos(), changelog.periods_due(on))


def stop(url: str, raw: str) -> str | None:
    target = hooks.stop_target(raw)
    if target is None:
        return None
    state = LocalState(url)
    asked = hooks.AskedPrompts(state.data.setdefault("prompts_asked", {}), state.save)
    root = target.root
    if root is None:
        return hooks.verdict(target, [], [], asked)

    async def action(remote: Remote) -> str | None:
        with contextlib.suppress(Exception):
            await refresh_month_pages(remote, state)
        try:
            logged = await remote.read((await remote.schema()).log_key(target.repo))
        except (RemoteError, NoHome):
            logged = ""
        commits = hooks.unlogged_in(logged, root)
        stale = hooks.stale_labels((await remote.context(target.cwd))["triage"])
        return hooks.verdict(target, commits, stale, asked)

    return run(url, action)


async def task_open(remote: Remote, args: Any) -> dict:
    schema = await remote.schema()
    if args.note:
        key = tasks.task_key(schema, args.note)
        text = await remote.read(key)
        await remote.call("set_status", path=key, status="active")
        return tasks.picked_up(key, text, args.cwd)
    tasks.require_name(args.name or "")
    where = tasks.located(args.cwd, (await remote.context(args.cwd))["system"])
    branch = args.branch or current_branch(args.cwd)
    key, text = tasks.new_task(schema, args.name, where, branch, await remote.keys())
    await remote.call("write_file", path=key, content=text, expected_etag="*")
    return tasks.opened(key, where)


async def task_check(remote: Remote, args: Any) -> dict:
    schema = await remote.schema()
    need = [key.strip() for key in args.need.split(",") if key.strip()]
    tasks.known_sections(schema, need)
    return {"missing": tasks.missing_in(schema, await remote.read(notes.note_key(args.path)), need)}


async def task_close(remote: Remote, args: Any) -> dict:
    return {"path": (await remote.call("close", path=args.path)).removeprefix("Closed: ").strip()}


async def task_backlog(remote: Remote, args: Any) -> dict:
    await remote.call("set_status", path=args.path, status=notes.BACKLOG_STATUS, priority=args.priority)
    return {"path": args.path}


async def task_claim(remote: Remote, args: Any) -> dict:
    found = await remote.call(
        "claim", path=args.path, machine=args.machine, task=args.task, repo=args.repo or "", force=args.force
    )
    return json.loads(found)


async def task_release(remote: Remote, args: Any) -> dict:
    return json.loads(await remote.call("release", path=args.path, machine=args.machine, task=args.task))


async def log_append(remote: Remote, args: Any) -> dict:
    if not args.line.strip():
        raise ValueError("a log line needs text")
    where = tasks.located(args.cwd, (await remote.context(args.cwd))["system"])
    await remote.call(
        "log_append",
        repo=where.repo,
        line=args.line.strip(),
        commits=list(args.commit or ()),
        area=where.area,
        branch=args.branch or current_branch(args.cwd),
    )
    return {"path": (await remote.schema()).log_key(where.repo)}


async def log_path(remote: Remote, args: Any) -> dict:
    return tasks.log_paths(await remote.schema(), args.repo)


async def snapshot(remote: Remote, args: Any) -> dict:
    return json.loads(await remote.call("snapshot", since=args.since))


JSON_ACTIONS: dict[str, Callable[[Remote, Any], Awaitable[dict]]] = {
    "task_open": task_open,
    "task_check": task_check,
    "task_close": task_close,
    "task_backlog": task_backlog,
    "task_claim": task_claim,
    "task_release": task_release,
    "log_append": log_append,
    "log_path": log_path,
    "snapshot": snapshot,
}


def json_action(url: str, name: str, args: Any) -> dict:
    action = JSON_ACTIONS[name]
    with credentials.working_in(getattr(args, "cwd", None)):
        if name in ("task_open", "log_append"):
            LocalState(url).remember_repo(args.cwd)
        return run(url, lambda remote: action(remote, args))


def changelog_command(url: str, args: Any) -> list[str]:
    if args.all:
        period = args.period or (args.repo if args.repo and changelog.PERIOD_RE.match(args.repo) else None)
        periods = [period] if period else changelog.periods_due(date.today())
        repos = LocalState(url).live_repos()
        return [f"wrote {key}" for key in run(url, lambda remote: write_pages(remote, repos, periods))]
    repo_path = Path(args.repo_path).expanduser() if args.repo_path else None
    commits = changelog.git_commits(repo_path, args.period) if repo_path is not None else None
    found = run(
        url,
        lambda remote: remote.call(
            "changelog", repo=args.repo, period=args.period, commits=commits, write=bool(args.write)
        ),
    )
    return [found]


def tool_text(url: str, tool: str, **arguments: Any) -> str:
    return run(url, lambda remote: remote.call(tool, **arguments))


def whoami(url: str) -> dict:
    return json.loads(tool_text(url, "whoami"))


def view(url: str, command: str, **arguments: Any) -> dict | None:
    try:
        found = tool_text(url, "view", command=command, **arguments)
    except RemoteError as exc:
        if "Unknown tool" in str(exc):
            return None
        raise
    return json.loads(found)


def lint(url: str, write: str | None = None, park: bool = False) -> tuple[str, int]:
    async def action(remote: Remote) -> tuple[str, int]:
        if write or park:
            text = await remote.call("lint_and_park", write=write, park=park)
        else:
            text = await remote.call("lint")
        return text, (await remote.schema()).stale_after_days

    return run(url, action)
