from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml
from hosted_support import INITIALIZE, Hosted, call_tool, csrf_of, hosted, open_session, post_mcp, sse_payload, text_of

from mindin import notes, provision
from mindin.auth.hosted_keys import SealedVaults, VaultKeys
from mindin.auth.hosted_orgs import Organisations
from mindin.backends import PREVIOUS_SCHEMA_KEY, SCHEMA_KEY
from mindin.backends.local import LocalBackend
from mindin.bundle import SessionRules, bundle_of
from mindin.presets import OFFERED, PERSONAL_LAYOUTS, PRESETS, preset_data
from mindin.schema import Schema, load_schema, schema_problems, shared_instructions
from mindin.vault import SubjectVaults, Vault

FOLDERS = ["Areas", "Teams", "Guides", "Decisions", "Projects", "Incidents", "Archive"]
FRONT = "date: 2026-11-01\nupdated: 2026-11-01\nstatus: active\nteam: payments\nauthor: Ada Lovelace\n"
GUIDE = (
    f'---\ntitle: Adding a payment method\n{FRONT}tags: [payments]\nkind: howto\narea: "[[payments]]"\n'
    "repos: [shop, cart-adapter]\n---\n\nAdd the provider to the adapter, then the settings page.\n"
)


def page(kind: str, status: str = "active", team: str = "payments") -> str:
    return (
        f"---\ntitle: A page\ndate: 2026-11-01\nupdated: 2026-11-01\nstatus: {status}\nteam: {team}\n"
        f'author: Ada Lovelace\ntags: [payments]\nkind: {kind}\narea: "[[payments]]"\n---\n\nText.\n'
    )


def wiki(tmp_path: Path) -> Vault:
    backend = LocalBackend(tmp_path / "vault")
    provision.initialize(backend, preset="wiki")
    vault = Vault(backend, load_schema(backend), index_path=tmp_path / "cache" / "index.sqlite")
    vault.sync(force=True)
    return vault


def test_the_wiki_writes_its_folders_kinds_and_views(tmp_path: Path):
    vault = wiki(tmp_path)
    schema = vault.schema
    assert schema.preset == "wiki" and schema.problems == ()
    assert list(schema.folders) == FOLDERS
    assert {name: folder.kinds for name, folder in schema.folders.items() if name != "Archive"} == {
        "Areas": ("system",),
        "Teams": ("team",),
        "Guides": ("howto",),
        "Decisions": ("decision",),
        "Projects": ("project",),
        "Incidents": ("incident",),
    }
    assert {"team", "author"} <= set(schema.required_fields) and "repos" in schema.optional_fields
    assert schema.unsearched_folders == ["Archive"]
    for name in PRESETS["wiki"].files:
        assert yaml.safe_load(vault.backend.get(name)[0])["views"]
    assert "wiki" not in OFFERED and "wiki" not in PERSONAL_LAYOUTS


def test_the_wiki_checks_each_pages_folder_team_and_status(tmp_path: Path):
    vault = wiki(tmp_path)
    assert notes.write(vault, "Guides/adding-a-payment-method.md", GUIDE).startswith("Written:")
    assert notes.write(vault, "Incidents/cart-down.md", page("incident", "complete")).startswith("Written:")
    refusals = {
        "Guides/no-team.md": (GUIDE.replace("team: payments\n", ""), "missing required field: team"),
        "Guides/a-decision.md": (page("decision"), "Guides/ takes howto notes, not decision"),
        "Projects/open.md": (page("project"), "Projects/ takes notes with status complete, superseded, not active"),
        "Incidents/ongoing.md": (page("incident"), "Incidents/ takes notes with status complete, superseded"),
    }
    for key, (text, problem) in refusals.items():
        with pytest.raises(notes.ValidationError, match=problem):
            notes.write(vault, key, text)


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


def key(site: Hosted, email: str) -> str:
    account = site.store.account(email)
    vault = site.store.vault_at(account.id, "wiki")
    return site.store.create_key(account.id, "laptop", ["vault:read", "vault:write"], vault_id=vault.id)


def organisations_vault(site: Hosted, slug: str) -> str | None:
    vault = site.store.vault_at(site.store.account("ada@acme.test").id, slug)
    backend, _ = SealedVaults(site.store, VaultKeys(site.store, site.keyring)).for_vault(vault.id)
    return provision.current_preset(backend)


async def write(site: Hosted, http, secret: str, path: str, content: str) -> dict:
    return await call_tool(http, secret, "write_file", {"path": path, "content": content}, path="/mcp?vault=wiki")


@pytest.mark.anyio
async def test_an_organisation_vault_is_a_wiki_unless_the_admin_picks_shared(site: Hosted):
    Organisations(site.store, site.clock).create("Acme", "ada@acme.test")
    organisation = Organisations(site.store, site.clock).require("Acme")
    async with site.browser() as ada:
        await site.sign_in(ada, "ada@acme.test")
        settings = await ada.get(f"/account/organisation/{organisation.id}")
        made = await ada.post(
            f"/account/organisation/{organisation.id}/vaults",
            data={"csrf": csrf_of(settings.text), "name": "Wiki", "slug": "wiki"},
        )
        secret = key(site, "ada@acme.test")
        schema = await call_tool(ada, secret, "read_file", {"path": SCHEMA_KEY}, path="/mcp?vault=wiki")
        guide = await write(site, ada, secret, "Guides/adding-a-payment-method.md", GUIDE)
        resource = await write(site, ada, secret, "Resources/deploys.md", GUIDE)
        basic = base64.b64encode(f"ada:{secret}".encode()).decode()
        dav = await ada.put(
            "/dav/vault/Incidents/cart-down.md",
            content=page("howto", "complete").encode(),
            headers={"Authorization": f"Basic {basic}"},
        )
        settings = await ada.get(f"/account/organisation/{organisation.id}")
        await ada.post(
            f"/account/organisation/{organisation.id}/vaults",
            data={"csrf": csrf_of(settings.text), "name": "Policies", "slug": "policies", "layout": "shared"},
        )
        policies = organisations_vault(site, "policies")
    assert policies == "shared"
    assert 'Layout<select name="layout"><option value="wiki"' in settings.text
    assert made.status_code == 200, made.text
    assert "preset: wiki" in text_of(schema)
    assert not guide.get("isError"), text_of(guide)
    assert resource.get("isError")
    assert "notes go in Areas/, Teams/, Guides/, Decisions/, Projects/, Incidents/ or Archive/" in text_of(resource)
    assert dav.status_code == 403 and "Incidents/ takes incident notes, not howto" in dav.text


@pytest.mark.anyio
async def test_an_organisation_vault_picked_as_shared_is_served_and_reprovisioned_as_shared(site: Hosted):
    organisation = Organisations(site.store, site.clock).create("Acme", "ada@acme.test")
    async with site.browser() as ada:
        await site.sign_in(ada, "ada@acme.test")
        settings = await ada.get(f"/account/organisation/{organisation.id}")
        made = await ada.post(
            f"/account/organisation/{organisation.id}/vaults",
            data={"csrf": csrf_of(settings.text), "name": "Policies", "slug": "policies", "layout": "shared"},
        )
    assert made.status_code == 200, made.text
    vault = site.store.vault_at(site.store.account("ada@acme.test").id, "policies")
    backend, _ = SealedVaults(site.store, VaultKeys(site.store, site.keyring))(vault.id)
    assert backend.layout == "shared"
    cli = Organisations(site.store, site.clock).create_vault(
        organisation, site.store.account("ada@acme.test"), "Handbook", "handbook", "shared"
    )
    assert SealedVaults(site.store, VaultKeys(site.store, site.keyring))(cli.id)[0].layout == "shared"


@pytest.mark.anyio
async def test_an_organisation_vault_made_without_a_layout_opens_as_a_wiki(site: Hosted):
    organisations = Organisations(site.store, site.clock)
    organisation = organisations.create("Acme", "ada@acme.test")
    organisations.create_vault(organisation, site.store.account("ada@acme.test"), "Wiki", "wiki")
    async with site.browser() as ada:
        await site.sign_in(ada, "ada@acme.test")
        guide = await write(site, ada, key(site, "ada@acme.test"), "Guides/adding-a-payment-method.md", GUIDE)
    assert not guide.get("isError"), text_of(guide)


def test_the_wiki_bundle_says_when_to_write_and_what_session_start_shows():
    schema = Schema(preset_data("wiki"))
    bundle = bundle_of(schema)
    assert bundle.session == SessionRules(("Areas", "Guides", "Decisions"), 5)
    assert bundle.session.shows("Guides/adding-a-payment-method.md") and not bundle.session.shows("Teams/payments.md")
    assert "explicit yes" in bundle.instructions and "Search this vault first" in bundle.instructions
    told = shared_instructions(schema, "Wiki")
    assert "3. Write here only as this vault's own rules, below, say." in told
    assert told.endswith(
        "At session start, `context` shows at most 5 pages from Areas/, Guides/, Decisions/ whose `repos` name the "
        "repo being worked in.\n"
    )
    assert "own rules" not in shared_instructions(Schema(preset_data("shared")), "Club")


def test_a_bundle_that_does_not_hold_names_each_problem():
    data = preset_data("wiki")
    data["bundle"] = {"instructions": ["no"], "session": {"folders": ["Nowhere"], "limit": 0}, "extra": 1}
    assert schema_problems(data) == [
        "bundle has unknown keys extra; it takes instructions, session",
        "bundle.instructions must be text",
        "bundle.session.folders names 'Nowhere', which is not one of the folders",
        "bundle.session.limit must be a whole number, 1 or more",
    ]


async def org_with_wiki(site: Hosted, http) -> None:
    organisation = Organisations(site.store, site.clock).create("Acme", "ada@acme.test")
    Organisations(site.store, site.clock).add_member(organisation, "cy@acme.test")
    await site.sign_in(http, "ada@acme.test")
    settings = await http.get(f"/account/organisation/{organisation.id}")
    made = await http.post(
        f"/account/organisation/{organisation.id}/vaults",
        data={"csrf": csrf_of(settings.text), "name": "Wiki", "slug": "wiki"},
    )
    assert made.status_code == 200, made.text


def own_key(site: Hosted, email: str) -> str:
    account = site.store.account(email)
    vault = site.store.first_vault(account.id)
    return site.store.create_key(account.id, "cli", ["vault:read", "vault:write"], purpose="device", vault_id=vault.id)


@pytest.mark.anyio
async def test_every_member_gets_the_wikis_rules_from_the_wiki_and_from_their_own_session(site: Hosted):
    async with site.browser() as ada, site.browser() as cy:
        await org_with_wiki(site, ada)
        await site.sign_in(cy, "cy@acme.test")
        secret = own_key(site, "cy@acme.test")
        wiki_rules, own_rules = await in_one_session(cy, secret, [("rules", {"vault": "wiki"}), ("rules", {})])
        opened = await post_mcp(cy, key(site, "cy@acme.test"), INITIALIZE, path="/mcp?vault=wiki")
    told = sse_payload(opened.text)["result"]["instructions"]
    assert "This vault's own rules:" in text_of(wiki_rules) and "write it on an explicit yes" in text_of(wiki_rules)
    assert "This vault's own rules:" in told and "Guides/" in told
    assert "own rules" not in text_of(own_rules) and "Projects/ — Open work" not in text_of(wiki_rules)


@pytest.mark.anyio
async def test_an_organisation_admin_edits_the_wikis_rules_and_a_member_cannot(site: Hosted):
    edited = provision.template("presets/wiki/schema.yml").replace(
        "A page here is finished work", "Ask the team lead first. A page here is finished work"
    )
    async with site.browser() as ada, site.browser() as cy:
        await org_with_wiki(site, ada)
        await site.sign_in(cy, "cy@acme.test")
        admin_key, member_key = key(site, "ada@acme.test"), key(site, "cy@acme.test")
        member = await cy.put(f"/dav/vault/{SCHEMA_KEY}", content=edited.encode(), headers=basic(member_key))
        personal = await ada.put(
            f"/dav/vault/{SCHEMA_KEY}", content=b"version: 1\npreset: para\n", headers=basic(admin_key)
        )
        admin = await ada.put(f"/dav/vault/{SCHEMA_KEY}", content=edited.encode(), headers=basic(admin_key))
        rules = await call_tool(cy, member_key, "rules", {}, path="/mcp?vault=wiki")
        previous = await cy.get(f"/dav/vault/{PREVIOUS_SCHEMA_KEY}", headers=basic(member_key))
    assert member.status_code == 403 and "its owner or an organisation admin changes it" in member.text
    assert personal.status_code == 403 and "preset must stay one of the shared layouts" in personal.text
    assert admin.status_code in (200, 201, 204), admin.text
    assert "Ask the team lead first." in text_of(rules)
    assert previous.status_code == 200 and "Ask the team lead" not in previous.text


def basic(secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"member:{secret}".encode()).decode()}


async def in_one_session(http, token: str, calls: list[tuple[str, dict]]) -> list[dict]:
    session = await open_session(http, token, "/mcp")
    answers = []
    for number, (name, arguments) in enumerate(calls, 2):
        body = {
            "jsonrpc": "2.0",
            "id": number,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = await post_mcp(http, token, body, session, "/mcp")
        assert response.status_code == 200, response.text
        answers.append(sse_payload(response.text)["result"])
    return answers
