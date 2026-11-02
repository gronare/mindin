from __future__ import annotations

import argparse
import sqlite3
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from hosted_support import NOTE_BODY, Hosted, call_tool, hosted, post_mcp, text_of
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from notes_vault_mcp import cli, extensions
from notes_vault_mcp.auth.hosted_store import Account, HostedStore
from notes_vault_mcp.extensions import Access, Extension, Server
from notes_vault_mcp.vault import SubjectVaults


class Ledger(Extension):
    def __init__(self) -> None:
        self.granted: dict[str, Access] = {}
        self.ran: list[str] = []

    def migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE IF NOT EXISTS ledger_notes (account_id TEXT PRIMARY KEY, note TEXT)")

    def commands(self, subparsers: argparse._SubParsersAction) -> None:
        tally = subparsers.add_parser("tally", help="count the accounts")
        tally.set_defaults(func=lambda args: self.ran.append("tally") or 0)

    def attach(self, server: Server) -> None:
        async def count(_: Request) -> Response:
            return PlainTextResponse(str(len(server.store.accounts())))

        server.routes.append(Route(server.config.path_prefix + "/ledger/count", endpoint=count, methods=["GET"]))
        server.account_sections.append(lambda account, csrf: f"<h2>Ledger</h2><p>{account.email}</p>")
        server.vault_sections.append(lambda account, vault, csrf: f"<h2>Ledger</h2><p>{vault.slug}</p>")

    def access(self, account: Account) -> Access:
        return self.granted.get(account.email, "full")


LEDGER = Ledger()


def make_ledger() -> Ledger:
    return LEDGER


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    ledger = Ledger()
    async with hosted(vaults, tmp_path, extensions=[ledger]) as running:
        running.ledger = ledger
        yield running


@pytest.mark.anyio
async def test_an_extension_adds_a_route_and_sections_on_the_account_and_vault_pages(site: Hosted):
    http = await site.member("ann@example.com")
    slug = site.store.vaults(site.store.account("ann@example.com").id)[0].slug
    async with http:
        counted = await http.get("/ledger/count")
        page = await http.get("/account/settings")
        vault_page = await http.get(f"/account/vaults/{slug}")
    assert (counted.status_code, counted.text) == (200, "1")
    assert "<h2>Ledger</h2><p>ann@example.com</p>" in page.text
    assert f"<h2>Ledger</h2><p>{slug}</p>" in vault_page.text


@pytest.mark.anyio
async def test_an_extensions_access_narrows_every_request(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com")
    async with http:
        written = await call_tool(http, key, "write_file", {"path": "Areas/a.md", "content": NOTE_BODY})
        site.ledger.granted["ann@example.com"] = "read"
        read = await call_tool(http, key, "read_file", {"path": "Areas/a.md"})
        refused_write = await call_tool(http, key, "write_file", {"path": "Areas/b.md", "content": NOTE_BODY})
        site.ledger.granted["ann@example.com"] = "none"
        refused = await post_mcp(http, key, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        page = await http.get("/account/settings")
    assert not written.get("isError"), text_of(written)
    assert "Hemligt" in text_of(read)
    assert refused_write.get("isError") and "may only read" in text_of(refused_write)
    assert (refused.status_code, refused.json()["error"]) == (403, "account_restricted")
    assert page.status_code == 200


@pytest.mark.anyio
async def test_without_extensions_access_is_full(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as site:
        http = await site.member("ann@example.com")
        key = site.key_for("ann@example.com")
        async with http:
            written = await call_tool(http, key, "write_file", {"path": "Areas/a.md", "content": NOTE_BODY})
            missing = await http.get("/ledger/count")
    assert not written.get("isError"), text_of(written)
    assert missing.status_code == 404


def test_the_store_runs_each_extensions_migration(tmp_path: Path):
    store = HostedStore(tmp_path, [Ledger()])
    HostedStore(tmp_path, [Ledger()])
    with store.connect() as connection:
        found = connection.execute("SELECT name FROM sqlite_master WHERE name = 'ledger_notes'").fetchone()
    assert found is not None


def test_the_strictest_extension_decides():
    account = Account("acc_x", "ann@example.com", "sub", "", "active", "cli", 0, None)
    reader, closed = Ledger(), Ledger()
    reader.granted["ann@example.com"] = "read"
    closed.granted["ann@example.com"] = "none"
    assert extensions.access([], account) == "full"
    assert extensions.access([Ledger(), reader], account) == "read"
    assert extensions.access([reader, closed], account) == "none"
    assert extensions.narrowed("read", ["vault:read", "vault:write"]) == ["vault:read"]
    assert extensions.narrowed("none", ["vault:read"]) == []


def test_installed_extensions_come_from_the_entry_point_group(monkeypatch: pytest.MonkeyPatch):
    point = EntryPoint(name="ledger", value=f"{__name__}:make_ledger", group=extensions.GROUP)
    monkeypatch.setattr(extensions, "entry_points", lambda group: [point] if group == extensions.GROUP else [])
    extensions.installed.cache_clear()
    try:
        assert extensions.installed() == (LEDGER,)
    finally:
        extensions.installed.cache_clear()


def test_an_extension_adds_a_cli_command(monkeypatch: pytest.MonkeyPatch):
    ledger = Ledger()
    monkeypatch.setattr(cli, "installed", lambda: (ledger,))
    assert cli.main(["tally"]) == 0
    assert ledger.ran == ["tally"]
