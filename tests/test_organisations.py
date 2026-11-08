from __future__ import annotations

import base64
from pathlib import Path

import httpx2
import pytest
from hosted_support import INITIALIZE, Hosted, call_tool, csrf_of, hosted, post_mcp, text_of

from mindin.auth import hosted_store
from mindin.auth.hosted_access import SupportAccess
from mindin.auth.hosted_keys import SealedVaults, VaultKeys, delete_account
from mindin.auth.hosted_org_pages import delete_departed
from mindin.auth.hosted_orgs import OrganisationError, Organisations, clean_domain
from mindin.auth.hosted_store import HostedStore
from mindin.auth.identities import Identities, Proof
from mindin.cli import main
from mindin.extensions import Extension
from mindin.vault import SubjectVaults

FRONT = "date: 2026-11-01\nupdated: 2026-11-01\nstatus: active\n"
DEPLOY = f'---\ntitle: Deploys\n{FRONT}tags: [ops]\nkind: howto\narea: "[[ops]]"\n---\n\nShip from main.\n'
POLICY = f'---\ntitle: Leave\n{FRONT}tags: [people]\nkind: reference\narea: "[[people]]"\n---\n\nTwenty-five days.\n'


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


def basic(secret: str, user: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()}


def orgs(site: Hosted) -> Organisations:
    return Organisations(site.store, site.clock)


async def signed_in(site: Hosted, email: str) -> tuple[httpx2.AsyncClient, httpx2.Response]:
    http = site.browser()
    answer = await site.sign_in(http, email)
    return http, answer


async def post_org(site: Hosted, http: httpx2.AsyncClient, path: str, **data: str) -> httpx2.Response:
    organisation = orgs(site).require("Acme")
    page = await http.get(f"/account/organisation/{organisation.id}")
    assert page.status_code == 200, page.text
    return await http.post(f"/account/organisation/{organisation.id}{path}", data={"csrf": csrf_of(page.text), **data})


async def company(site: Hosted) -> tuple[httpx2.AsyncClient, httpx2.AsyncClient]:
    orgs(site).create("Acme", "ada@acme.test")
    ada, answer = await signed_in(site, "ada@acme.test")
    assert answer.status_code == 303, answer.text
    assert (await post_org(site, ada, "/domains", domain="acme.test")).status_code == 200
    assert (await post_org(site, ada, "/admins", email="bea@acme.test")).status_code == 200
    made = await post_org(site, ada, "/vaults", name="Wiki", slug="wiki")
    assert made.status_code == 200 and "Wiki is at /mcp?vault=wiki for every member" in made.text, made.text
    bea, answer = await signed_in(site, "bea@acme.test")
    assert answer.status_code == 303, answer.text
    return ada, bea


def key(site: Hosted, email: str, slug: str = "wiki", name: str = "laptop") -> str:
    account = site.store.account(email)
    vault = site.store.vault_at(account.id, slug)
    assert vault is not None, f"{email} does not reach {slug}"
    return site.store.create_key(account.id, name, ["vault:read", "vault:write"], vault_id=vault.id)


async def write(http: httpx2.AsyncClient, secret: str, path: str, content: str, slug: str = "wiki") -> dict:
    return await call_tool(http, secret, "write_file", {"path": path, "content": content}, path=f"/mcp?vault={slug}")


async def read_note(http: httpx2.AsyncClient, secret: str, path: str, slug: str = "wiki") -> dict:
    return await call_tool(http, secret, "read_file", {"path": path}, path=f"/mcp?vault={slug}")


def history(site: Hosted, slug: str = "wiki") -> list:
    vault = next(item.vault for item in orgs(site).vaults(orgs(site).require("Acme")) if item.vault.slug == slug)
    return SealedVaults(site.store, VaultKeys(site.store, site.keyring)).history(vault.id).changes()


@pytest.mark.anyio
async def test_flow_1_a_colleague_joins_by_signing_in_and_an_outsider_does_not(site: Hosted):
    assert site.config.registration == "invite"
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy, site.browser() as eve:
        joined = await site.sign_in(cy, "cy@acme.test")
        listed = await cy.get("/account")
        settings = await cy.get("/account/settings")
        admin_page = await cy.get(f"/account/organisation/{orgs(site).require('Acme').id}")
        refused = await site.sign_in(eve, "eve@elsewhere.test")
        lookalike = await site.sign_in(eve, "eve@dev.acme.test", sub="sub-eve-dev")
    assert joined.status_code == 303, joined.text
    cy_account = site.store.account("cy@acme.test")
    assert cy_account is not None and cy_account.active
    slugs = [vault.slug for vault in site.store.reachable_vaults(cy_account.id)]
    assert slugs == ["notes", "wiki"]
    assert "Wiki" in listed.text and '<span class="chip shared">Acme</span>' in listed.text
    assert "<td>Acme</td><td>Member</td>" in settings.text and admin_page.status_code == 403
    assert refused.status_code == 403 and site.store.account("eve@elsewhere.test") is None
    assert lookalike.status_code == 403 and site.store.account("eve@dev.acme.test") is None
    members = {member.account.email: member.role for member in orgs(site).members(orgs(site).require("Acme"))}
    assert members == {"ada@acme.test": "admin", "bea@acme.test": "admin", "cy@acme.test": "member"}


@pytest.mark.anyio
async def test_flow_1_members_write_the_wiki_and_make_their_own_webdav_key(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        await site.sign_in(cy, "cy@acme.test")
        page = await cy.get("/account/vaults/wiki")
        made = await cy.post(
            "/account/vaults/wiki/keys", data={"csrf": csrf_of(page.text), "name": "Obsidian", "access": "write"}
        )
        dav_key = made.text.split('<code id="new-key">')[1].split("<")[0]
        put = await cy.put("/dav/vault/Resources/deploys.md", content=DEPLOY.encode(), headers=basic(dav_key, "cy"))
        read = await call_tool(
            cy, key(site, "bea@acme.test"), "read_file", {"path": "Resources/deploys.md"}, "/mcp?vault=wiki"
        )
    assert made.status_code == 200 and put.status_code == 201
    assert "Ship from main." in text_of(read)
    assert history(site)[0].author_email == "cy@acme.test" and history(site)[0].client == "Obsidian"


@pytest.mark.anyio
async def test_flow_14_a_locked_vault_refuses_a_members_write_and_takes_an_editors(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        await site.sign_in(cy, "cy@acme.test")
        assert (await post_org(site, ada, "/vaults", name="Policies", slug="policies")).status_code == 200
        before = await write(cy, key(site, "cy@acme.test", "policies"), "Resources/leave.md", POLICY, "policies")
        locked = await post_org(site, ada, "/vaults/lock", vault="policies", locked="on")
        editor = await post_org(site, ada, "/vaults/editors", vault="policies", email="bea@acme.test")
        refused = await write(cy, key(site, "cy@acme.test", "policies"), "Resources/leave.md", DEPLOY, "policies")
        dav_key = key(site, "cy@acme.test", "policies", "dav")
        dav = await cy.put("/dav/vault/Resources/leave.md", content=DEPLOY.encode(), headers=basic(dav_key, "cy"))
        allowed = await write(bea, key(site, "bea@acme.test", "policies"), "Resources/leave.md", DEPLOY, "policies")
        read = await call_tool(
            cy,
            key(site, "cy@acme.test", "policies"),
            "read_file",
            {"path": "Resources/leave.md"},
            "/mcp?vault=policies",
        )
        page = await cy.get("/account/vaults/policies")
    assert not before.get("isError"), text_of(before)
    assert "only its editors write" in locked.text and "bea@acme.test edits Policies" in editor.text
    assert refused.get("isError") and "Policies is locked: only its editors (bea@acme.test)" in text_of(refused)
    assert dav.status_code == 403 and "bea@acme.test" in dav.text
    assert not allowed.get("isError"), text_of(allowed)
    assert "Ship from main." in text_of(read)
    assert "Locked: only its editors write (bea@acme.test)" in page.text
    assert [change.author_email for change in history(site, "policies")] == ["bea@acme.test", "cy@acme.test"]


@pytest.mark.anyio
async def test_a_vault_box_lists_editors_only_while_the_vault_is_locked(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea:
        opened = await post_org(site, ada, "/vaults", name="Policies", slug="policies")
        await post_org(site, ada, "/vaults/lock", vault="policies", locked="on")
        locked = await post_org(site, ada, "/vaults/editors", vault="policies", email="bea@acme.test")
        unlocked = await post_org(site, ada, "/vaults/lock", vault="policies", locked="")
        relocked = await post_org(site, ada, "/vaults/lock", vault="policies", locked="on")
    assert "Add an editor" not in opened.text and "No editors." not in opened.text
    assert "Add an editor" in locked.text and '<li class="row">bea@acme.test' in locked.text
    assert "Add an editor" not in unlocked.text and '<li class="row">bea@acme.test' not in unlocked.text
    assert '<li class="row">bea@acme.test' in relocked.text


@pytest.mark.anyio
async def test_flow_13_a_removed_member_keeps_their_account_and_their_wiki_notes(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        await site.sign_in(cy, "cy@acme.test")
        wiki_key = key(site, "cy@acme.test")
        await write(cy, wiki_key, "Resources/deploys.md", DEPLOY)
        removed = await post_org(site, ada, "/members/remove", email="cy@acme.test")
        after = await post_mcp(cy, wiki_key, INITIALIZE, path="/mcp?vault=wiki")
        own = await cy.get("/account/vaults/notes")
        wiki = await cy.get("/account/vaults/wiki")
        again = await site.sign_in(site.browser(), "cy@acme.test")
    assert "cy@acme.test is removed; their own vaults stay" in removed.text
    assert after.status_code == 401
    assert own.status_code == 200 and wiki.status_code == 404
    assert again.status_code == 303
    cy_account = site.store.account("cy@acme.test")
    assert [vault.slug for vault in site.store.reachable_vaults(cy_account.id)] == ["notes"]
    assert orgs(site).of_account(cy_account.id) == [], "a removed member does not rejoin through the domain"
    assert history(site)[0].author_email == "cy@acme.test"


@pytest.mark.anyio
async def test_flow_13_export_and_delete_mails_a_link_and_deletes_the_account_later(
    site: Hosted, vaults: SubjectVaults
):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        await site.sign_in(cy, "cy@acme.test")
        await write(cy, key(site, "cy@acme.test"), "Resources/deploys.md", DEPLOY)
        saved = await post_org(site, ada, "/removal", removal="export-and-delete", days="14")
        removed = await post_org(site, ada, "/members/remove", email="cy@acme.test")
    assert "Saved." in saved.text
    assert "their own vaults are deleted on" in removed.text and "An email tells them" in removed.text
    message = site.smtp.messages[-1]
    assert message["To"] == "cy@acme.test" and message["Subject"] == "Acme removed you from its vaults"
    assert "http://localhost/account" in message.get_body(("plain",)).get_content()
    sealed = SealedVaults(site.store, VaultKeys(site.store, site.keyring))

    def erase(account) -> None:
        delete_account(sealed, account, vaults.cache)

    assert delete_departed(orgs(site), erase) == 0
    site.clock.advance(15 * 86400)
    assert delete_departed(orgs(site), erase) == 1
    assert site.store.account("cy@acme.test") is None
    assert history(site)[0].author_email == "cy@acme.test"
    assert delete_departed(orgs(site), erase) == 0


@pytest.mark.anyio
async def test_the_days_before_deletion_take_only_plain_digits(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea:
        page = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
        answers = [
            await post_org(site, ada, "/removal", removal="export-and-delete", days=days) for days in ("²", "1000", "7")
        ]
    assert 'pattern="[0-9]{1,3}"' in page.text
    assert [answer.status_code for answer in answers] == [400, 400, 200]
    assert orgs(site).require("Acme").removal_days == 7


@pytest.mark.anyio
async def test_an_existing_account_on_the_domain_joins_on_its_next_sign_in(site: Hosted):
    site.store.invite("dan@acme.test")
    dan, _ = await signed_in(site, "dan@acme.test")
    account = site.store.account("dan@acme.test")
    organisation = orgs(site).create("Acme", "ada@acme.test")
    orgs(site).add_domain(organisation, "acme.test")
    assert orgs(site).of_account(account.id) == []
    async with dan, site.browser() as again:
        await site.sign_in(again, "dan@acme.test")
    assert [found.name for found, _ in orgs(site).of_account(account.id)] == ["Acme"]


@pytest.mark.anyio
async def test_an_unverified_company_address_does_not_join(site: Hosted):
    organisation = orgs(site).create("Acme", "ada@acme.test")
    orgs(site).add_domain(organisation, "acme.test")
    async with site.browser() as http:
        answer = await site.sign_in(http, "mal@acme.test", verified=False)
    assert answer.status_code == 403 and site.store.account("mal@acme.test") is None


@pytest.mark.anyio
async def test_a_member_whose_address_clashes_joins_once_they_rename(site: Hosted):
    ada, bea = await company(site)
    site.store.invite("cy@acme.test")
    cy_account = site.store.account("cy@acme.test")
    site.store.create_vault(cy_account.id, "My wiki", "wiki")
    async with ada, bea, site.browser() as cy:
        await site.sign_in(cy, "cy@acme.test")
        waiting = await cy.get("/account")
        site.store.rename_vault(site.store.own_vault(cy_account.id, "wiki").id, slug="mywiki")
        joined = await cy.get("/account")
    assert "You are invited to Wiki" in waiting.text
    assert '<span class="chip shared">Acme</span>' in joined.text
    assert site.store.vault_at(cy_account.id, "wiki").account_id == site.store.account("ada@acme.test").id


@pytest.mark.anyio
async def test_org_vaults_take_no_invitations_and_cannot_be_left(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea:
        page = await bea.get("/account/vaults/wiki")
        left = await bea.post("/account/vaults/wiki/leave", data={"csrf": csrf_of(page.text)})
        owner_page = await ada.get("/account/vaults/wiki")
        invited = await ada.post(
            "/account/vaults/wiki/members/invite", data={"csrf": csrf_of(owner_page.text), "email": "x@else.test"}
        )
    assert left.status_code == 409 and invited.status_code == 409
    assert "Send invitation" not in owner_page.text and "Every member of Acme reaches this vault" in owner_page.text


def test_a_domain_matches_exactly_and_ignores_case(tmp_path: Path):
    store = HostedStore(tmp_path)
    organisations = Organisations(store)
    organisation = organisations.create("Acme", "ada@acme.test")
    assert organisations.add_domain(organisation, " @Acme.TEST. ") == "acme.test"
    identities = Identities(store)
    joined = identities.sign_in(Proof("oidc", "s1", ("Cy@ACME.test",), "cy"), "invite")
    sub = identities.sign_in(Proof("oidc", "s2", ("eve@dev.acme.test",), "eve"), "invite")
    suffix = identities.sign_in(Proof("oidc", "s3", ("eve@notacme.test",), "eve"), "invite")
    assert joined.account is not None and joined.account.email == "cy@acme.test"
    assert sub.account is None and sub.refusal == "uninvited"
    assert suffix.account is None and suffix.refusal == "uninvited"
    for bad in ("acme", "", "a@b@c", "acme..test", "-acme.test"):
        with pytest.raises(OrganisationError):
            clean_domain(bad)


def test_a_domain_belongs_to_one_organisation(tmp_path: Path):
    organisations = Organisations(HostedStore(tmp_path))
    first = organisations.create("Acme", "ada@acme.test")
    second = organisations.create("Other", "oz@other.test")
    organisations.add_domain(first, "acme.test")
    with pytest.raises(OrganisationError, match="another organisation"):
        organisations.add_domain(second, "acme.test")


def test_the_company_address_becomes_the_account_address(tmp_path: Path):
    store = HostedStore(tmp_path)
    organisations = Organisations(store)
    organisations.add_domain(organisations.create("Acme", "ada@acme.test"), "acme.test")
    outcome = Identities(store).sign_in(Proof("github", "77", ("me@home.test", "cy@acme.test"), "cy"), "invite")
    assert outcome.account is not None and outcome.account.email == "cy@acme.test"


def test_the_last_admin_stays(tmp_path: Path):
    organisations = Organisations(HostedStore(tmp_path))
    organisation = organisations.create("Acme", "ada@acme.test")
    with pytest.raises(OrganisationError, match="last admin"):
        organisations.remove_admin(organisation, "ada@acme.test")
    with pytest.raises(OrganisationError, match="last admin"):
        organisations.remove_member(organisation, "ada@acme.test")
    organisations.add_admin(organisation, "bea@acme.test")
    organisations.remove_admin(organisation, "ada@acme.test")
    assert [admin.email for admin in organisations.admins(organisation)] == ["bea@acme.test"]


def test_removing_the_owner_of_an_org_vault_hands_it_to_an_admin(tmp_path: Path):
    store = HostedStore(tmp_path)
    SupportAccess(store)
    organisations = Organisations(store)
    organisation = organisations.create("Acme", "ada@acme.test")
    organisations.add_admin(organisation, "bea@acme.test")
    vault = organisations.create_vault(organisation, store.account("ada@acme.test"), "Wiki", "wiki")
    organisations.remove_member(organisation, "ada@acme.test")
    assert store.vault(vault.id).account_id == store.account("bea@acme.test").id
    assert not store.reaches(store.vault(vault.id), store.account("ada@acme.test").id)


def test_only_an_admin_makes_org_vaults_and_editors_are_members(tmp_path: Path):
    store = HostedStore(tmp_path)
    organisations = Organisations(store)
    organisation = organisations.create("Acme", "ada@acme.test")
    cy = organisations.add_member(organisation, "cy@acme.test")
    with pytest.raises(OrganisationError, match="Only an admin"):
        organisations.create_vault(organisation, cy, "Wiki")
    item = organisations.org_vault(organisations.create_vault(organisation, store.account("ada@acme.test"), "Wiki"))
    with pytest.raises(OrganisationError, match="not a member"):
        organisations.add_editor(item, "eve@else.test")


def test_the_cli_sets_up_an_organisation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "auth"))
    assert main(["org", "create", "Acme", "--admin", "ada@acme.test"]) == 0
    assert main(["org", "domain", "add", "Acme", "acme.test"]) == 0
    assert main(["org", "admin", "add", "Acme", "bea@acme.test"]) == 0
    assert main(["org", "vault", "create", "Acme", "Wiki", "--slug", "wiki"]) == 0
    assert main(["org", "vault", "lock", "Acme", "wiki"]) == 0
    assert main(["org", "vault", "editor-add", "Acme", "wiki", "bea@acme.test"]) == 0
    assert main(["org", "removal", "Acme", "export-and-delete", "--days", "10"]) == 0
    assert main(["org", "admin", "remove", "Acme", "zed@acme.test"]) == 2
    capsys.readouterr()
    assert main(["org", "show", "Acme"]) == 0
    shown = capsys.readouterr().out
    assert "removed members: export-and-delete after 10 days" in shown
    assert "domain  @acme.test" in shown and "admin  ada@acme.test" in shown and "admin  bea@acme.test" in shown
    assert "vault  wiki  Wiki  locked, editors: bea@acme.test" in shown


def test_a_forwarded_identity_joins_by_domain_and_shares_the_account_with_the_company_sign_in(tmp_path: Path):
    store = HostedStore(tmp_path)
    organisations = Organisations(store)
    organisations.add_domain(organisations.create("Acme", "ada@acme.test"), "acme.test")
    identities = Identities(store)
    through_proxy = identities.sign_in(Proof("forwarded", "proxy-cy", ("cy@acme.test",), "cy@acme.test"), "invite")
    on_the_pages = identities.sign_in(Proof("oidc", "idp-cy", ("cy@acme.test",), "cy@acme.test"), "invite")
    assert through_proxy.account is not None and on_the_pages.account is not None
    assert through_proxy.account.id == on_the_pages.account.id
    assert [found.name for found, _ in organisations.of_account(through_proxy.account.id)] == ["Acme"]
    assert sorted(way.method for way in identities.ways_in(through_proxy.account.id)) == ["forwarded", "oidc"]


class Plans(Extension):
    def __init__(self) -> None:
        self.made: list[tuple[str, str, bool, str]] = []

    def registered(self, connection, account, registrant) -> None:
        connection.execute("UPDATE accounts SET name = 'planned' WHERE id = ?", (account.id,))
        self.made.append((account.email, registrant.method, registrant.invited, registrant.label))


@pytest.mark.anyio
async def test_every_way_an_account_is_made_calls_registered_once(vaults: SubjectVaults, tmp_path: Path, monkeypatch):
    plans = Plans()
    async with hosted(vaults, tmp_path, extensions=[plans]) as running:
        store = HostedStore(running.config.auth_dir, extensions=[plans])
        Organisations(store).create("Acme", "ada@acme.test")
        ada, _ = await signed_in(running, "ada@acme.test")
        async with ada, running.browser() as dan:
            assert (await post_org(running, ada, "/members", email="cy@else.test")).status_code == 200
            assert (await post_org(running, ada, "/admins", email="bea@else.test")).status_code == 200
            owner, _ = store.invite("own@solo.test")
            shared = running.store.create_vault(owner.id, "Club", "club", "shared")
            running.store.invite_member(shared, "dan@solo.test", owner.id)
            assert (await running.sign_in(dan, "dan@solo.test")).status_code == 303
        monkeypatch.setattr(hosted_store, "installed", lambda: (plans,))
        monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "cli"))
        assert main(["accounts", "invite", "eve@solo.test"]) == 0
        assert main(["org", "create", "Beta", "--admin", "bo@beta.test"]) == 0
        assert main(["org", "member", "add", "Beta", "fay@else.test"]) == 0
    assert plans.made == [
        ("ada@acme.test", "organisation", True, "Acme"),
        ("cy@else.test", "organisation", True, "Acme"),
        ("bea@else.test", "organisation", True, "Acme"),
        ("own@solo.test", "operator", True, "own@solo.test"),
        ("dan@solo.test", "oidc", True, "dan@solo.test"),
        ("eve@solo.test", "operator", True, "eve@solo.test"),
        ("bo@beta.test", "organisation", True, "Beta"),
        ("fay@else.test", "organisation", True, "Beta"),
    ]
    assert running.store.account("cy@else.test").name == "planned"


@pytest.mark.anyio
async def test_the_lead_counts_the_members_and_the_vaults(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        page = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
        await site.sign_in(cy, "cy@acme.test")
        after = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
    assert '<p class="lead">Organisation · you are an admin · 2 members · 1 vault</p>' in page.text
    assert '<p class="lead">Organisation · you are an admin · 3 members · 1 vault</p>' in after.text
