from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import Implementation

from mindin import credentials
from mindin.backends import SCHEMA_KEY
from mindin.config import ConfigError
from mindin.git import in_main_checkout, remote_url, repo_name
from mindin.schema import Schema, default_schema_data, schema_from_text

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


def tool_text(url: str, tool: str, **arguments: Any) -> str:
    return run(url, lambda remote: remote.call(tool, **arguments))


def whoami(url: str) -> dict:
    return json.loads(tool_text(url, "whoami"))
