from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml
from hosted_support import Hosted, call_tool, csrf_of, hosted, text_of

from mindin import notes, provision
from mindin.auth.hosted_keys import SealedVaults, VaultKeys
from mindin.auth.hosted_orgs import Organisations
from mindin.backends import SCHEMA_KEY
from mindin.backends.local import LocalBackend
from mindin.presets import OFFERED, PERSONAL_LAYOUTS, PRESETS
from mindin.schema import load_schema
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
