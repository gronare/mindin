from __future__ import annotations

import inspect
import sqlite3
import threading
import typing
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from hosted_support import NOTE_BODY, Hosted, call_tool, hosted, post_mcp, text_of
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from mindin import cli, extension_api, extensions
from mindin.extensions import Access, Extension, Periodic, Server, Subcommands
from mindin.hosted.accounts.store import Account, HostedStore
from mindin.hosted.orgs.organisations import Organisations
from mindin.hosted.periodic import Leases, PeriodicRunner
from mindin.vault import SubjectVaults


class Ledger(Extension):
    def __init__(self) -> None:
        self.granted: dict[str, Access] = {}
        self.ran: list[str] = []

    def migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE IF NOT EXISTS ledger_notes (account_id TEXT PRIMARY KEY, note TEXT)")

    def commands(self, subparsers: Subcommands) -> None:
        tally = subparsers.add_parser("tally", help="count the accounts")
        tally.set_defaults(func=lambda args: self.ran.append("tally") or 0)

    def attach(self, server: Server) -> None:
        async def count(_: Request) -> Response:
            return PlainTextResponse(str(len(server.store.accounts())))

        server.routes.append(Route(server.config.path_prefix + "/ledger/count", endpoint=count, methods=["GET"]))
        server.account_sections.append(lambda account, csrf: f"<h2>Ledger</h2><p>{account.email}</p>")
        server.vault_sections.append(lambda account, vault, csrf: f"<h2>Ledger</h2><p>{vault.slug}</p>")
        server.organisation_sections.append(
            lambda organisation, viewer, csrf: f"<h2>Ledger</h2><p>{organisation.name} for {viewer.email}</p>"
        )

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
async def test_an_extension_adds_a_section_to_the_organisation_page_for_its_admins(site: Hosted):
    organisation = Organisations(site.store, site.clock).create("Acme", "ada@acme.test")
    ada, bob = await site.member("ada@acme.test"), await site.member("bob@example.com")
    async with ada, bob:
        page = await ada.get(f"/account/organisation/{organisation.id}")
        outside = await bob.get(f"/account/organisation/{organisation.id}")
    assert page.status_code == 200 and "<h2>Ledger</h2><p>Acme for ada@acme.test</p>" in page.text
    assert outside.status_code == 403 and "Ledger" not in outside.text


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


EXTENSION_NEEDS = {
    "EMAIL_ADDRESS",
    "INVITATION_TTL",
    "MACHINE_KEY_PURPOSE",
    "MUST_NOT_EXIST",
    "SHARED",
    "STORE_VERSION",
    "Access",
    "Account",
    "Actor",
    "AuthConfig",
    "Browser",
    "Change",
    "ConfigError",
    "Extension",
    "GithubConfig",
    "History",
    "HostedStore",
    "Identities",
    "Keyring",
    "Kind",
    "Leases",
    "LimitReached",
    "Listener",
    "Mail",
    "Mailer",
    "MasterKey",
    "OidcConfig",
    "Organisation",
    "OrganisationError",
    "OrganisationSection",
    "Organisations",
    "Periodic",
    "Proof",
    "RateCounter",
    "Registrant",
    "RequestRefused",
    "S3Backend",
    "SealedVaults",
    "Server",
    "SignInCheck",
    "SmtpConfig",
    "SmtpFactory",
    "StorageLimits",
    "Subcommands",
    "SubjectVaults",
    "SupportAccess",
    "SupportRequest",
    "Vault",
    "VaultFacts",
    "VaultKeys",
    "VersionConflict",
    "account_of",
    "account_tables",
    "ask_for_support",
    "auth_dir",
    "branded",
    "build_hosted_app",
    "cli_main",
    "delete_departed",
    "digest",
    "env",
    "esc",
    "has_table",
    "honours_conditions",
    "images",
    "installed",
    "limited",
    "local_root",
    "make_subject_backend",
    "page",
    "passes",
    "rewrite_slugs",
    "scope_label",
    "semantic_requested",
    "stamp",
    "with_defaults",
}


def test_every_name_of_the_extension_api_imports():
    missing = [name for name in extension_api.__all__ if not hasattr(extension_api, name)]
    assert missing == []


def test_the_extension_api_covers_what_an_extension_builds_on():
    assert EXTENSION_NEEDS - set(extension_api.__all__) == set()


def test_the_server_an_extension_attaches_to_names_the_type_of_every_field():
    hints = typing.get_type_hints(Server, {**vars(extensions), **vars(extension_api), "BaseRoute": Route})
    assert [field for field, hint in hints.items() if hint is typing.Any] == []
    assert hints["sealed"] == extension_api.SealedVaults | None


def test_the_commands_hook_takes_a_public_type():
    annotation = inspect.signature(Extension.commands).parameters["subparsers"].annotation
    assert annotation == "Subcommands"


def test_storage_that_checks_versions_itself_honours_conditions():
    class Checking:
        conditional_puts = False

    assert extension_api.honours_conditions(Checking()) is False
    assert extension_api.honours_conditions(object()) is True


class Ticker(Extension):
    def __init__(self) -> None:
        self.ticks = 0
        self.ticked = threading.Event()

    def attach(self, server: Server) -> None:
        server.periodic.append(Periodic("tick", 0.01, self.tick))

    def tick(self) -> None:
        self.ticks += 1
        if self.ticks >= 3:
            self.ticked.set()
        if self.ticks == 1:
            raise RuntimeError("a failing run does not stop the next one")


@pytest.mark.anyio
async def test_periodic_work_runs_while_the_server_runs_and_stops_with_it(vaults: SubjectVaults, tmp_path: Path):
    ticker = Ticker()
    async with hosted(vaults, tmp_path, extensions=[ticker]):
        assert ticker.ticked.wait(5)
    stopped = ticker.ticks
    threading.Event().wait(0.05)
    assert ticker.ticks == stopped


def test_two_replicas_run_a_periodic_job_once_per_period(tmp_path: Path):
    store = HostedStore(tmp_path)
    moment = [1_800_000_000.0]
    runs: list[str] = []
    job = Periodic("organisation-departures", 3600.0, lambda: runs.append("ran"))
    replicas = [PeriodicRunner([job], Leases(store, lambda: moment[0], holder).claim) for holder in ("a", "b")]
    assert [runner.tick(job) for runner in replicas] == [True, False]
    moment[0] += 1800
    assert [runner.tick(job) for runner in replicas] == [False, False]
    moment[0] += 1800
    assert [runner.tick(job) for runner in reversed(replicas)] == [True, False]
    assert runs == ["ran", "ran"]


def test_a_branded_mail_has_the_sign_in_look_and_a_plain_part():
    mail = extension_api.branded("Ends soon", "It ends", "In three days.", "Open", "https://x.test/a", "Note <b>.")
    assert mail.subject == "Ends soon"
    assert "Open: https://x.test/a" in mail.text and "Note <b>." in mail.text
    assert "Note &lt;b&gt;." in mail.html and 'href="https://x.test/a"' in mail.html and "cid:mark@mindin" in mail.html
