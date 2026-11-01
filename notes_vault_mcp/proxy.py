from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from notes_vault_mcp import __version__, credentials
from notes_vault_mcp.remote import Remote, RemoteError, connect, refused
from notes_vault_mcp.server import MACHINE_TOOLS

T = TypeVar("T")


def sign_in_text(url: str) -> str:
    return (
        f"The vault lives on {url}, and this machine is not signed in to it. Tell the user to run "
        f"`notes-vault-mcp login {url}` in a terminal (or /vault:login), then reconnect this server with /mcp."
    )


class Upstream:
    # One task owns the connection, because an anyio task group must close in the task that opened it.
    def __init__(self, url: str) -> None:
        self.url = url
        self.remote: Remote | None = None
        self.failure: Exception = RemoteError(f"not connected to {url}")
        self.ready = anyio.Event()
        self.reconnect = anyio.Event()

    def settle(self, remote: Remote | None, failure: Exception | None = None) -> None:
        self.remote = remote
        if failure is not None:
            self.failure = failure
        self.reconnect = anyio.Event()
        self.ready.set()

    async def supervise(self) -> None:
        while True:
            try:
                async with connect(self.url) as remote:
                    self.settle(remote)
                    await self.reconnect.wait()
            except Exception as exc:
                self.settle(None, refused(exc, self.url))
                await self.reconnect.wait()

    def ask_again(self) -> None:
        if not self.reconnect.is_set():
            self.ready = anyio.Event()
            self.reconnect.set()

    async def use(self, request: Callable[[Remote], Awaitable[T]]) -> T:
        await self.ready.wait()
        remote = self.remote
        if remote is not None:
            try:
                return await request(remote)
            except Exception:
                if self.remote is not remote:
                    return await self.use(request)
        self.ask_again()
        await self.ready.wait()
        if self.remote is None:
            raise self.failure
        return await request(self.remote)


def build_proxy(upstream: Upstream, instructions: str | None) -> Server[Any]:
    async def list_tools(_: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        cursor = params.cursor if params is not None else None
        listed = await upstream.use(lambda remote: remote.client.list_tools(cursor=cursor, cache_mode="refresh"))
        tools = [tool for tool in listed.tools if tool.name not in MACHINE_TOOLS]
        return listed.model_copy(update={"tools": tools})

    async def call_tool(_: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name in MACHINE_TOOLS:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Unknown tool: {params.name}")], is_error=True
            )
        return await upstream.use(lambda remote: remote.client.call_tool(params.name, params.arguments or {}))

    return Server(
        "vault", version=__version__, instructions=instructions, on_list_tools=list_tools, on_call_tool=call_tool
    )


def build_signed_out(url: str) -> Server[Any]:
    async def list_tools(_: Any, __: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[])

    return Server("vault", version=__version__, instructions=sign_in_text(url), on_list_tools=list_tools)


async def serve(server: Server[Any]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def run_proxy(url: str) -> None:
    if credentials.stored(url) is None:
        await serve(build_signed_out(url))
        return
    upstream = Upstream(url)
    async with anyio.create_task_group() as group:
        group.start_soon(upstream.supervise)
        await upstream.ready.wait()
        if upstream.remote is None:
            group.cancel_scope.cancel()
            raise upstream.failure
        await serve(build_proxy(upstream, upstream.remote.client.instructions))
        group.cancel_scope.cancel()


def proxy_stdio(url: str) -> int:
    try:
        anyio.run(run_proxy, url)
    except Exception as exc:
        print(f"notes-vault-mcp: {refused(exc, url)}", file=sys.stderr)
        return 1
    return 0
