from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from hosted_support import INITIALIZE, Hosted, call_tool, csrf_of, hosted, open_session, post_mcp, sse_payload, text_of

from mindin import provision
from mindin.backends import SCHEMA_KEY
from mindin.backends.local import LocalBackend
from mindin.cli import main
from mindin.extensions import Extension
from mindin.hosted.accounts import store as hosted_store
from mindin.hosted.accounts.access import SupportAccess
from mindin.hosted.accounts.deletion import delete_departed
from mindin.hosted.accounts.keys import SealedVaults, StillShared, VaultKeys, delete_account
from mindin.hosted.accounts.store import HostedStore, MembershipError
from mindin.hosted.orgs import organisations as hosted_orgs
from mindin.hosted.orgs.organisations import OrganisationError, Organisations, OrgDomain, clean_domain
from mindin.hosted.signin.identities import Identities, Proof
from mindin.shared import Actor, SharedBackend, SharedRefusal, SharedRules, acting_as
from mindin.vault import SubjectVaults

FRONT = "date: 2026-11-01\nupdated: 2026-11-01\nstatus: active\n"
DEPLOY = f'---\ntitle: Deploys\n{FRONT}tags: [ops]\nkind: howto\narea: "[[ops]]"\n---\n\nShip from main.\n'
POLICY = f'---\ntitle: Leave\n{FRONT}tags: [people]\nkind: reference\narea: "[[people]]"\n---\n\nTwenty-five days.\n'


PUBLISHED: dict[str, list[str]] = {}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch):
    PUBLISHED.clear()
    monkeypatch.setattr(hosted_orgs, "txt_records", lambda name: PUBLISHED.get(name, []))
    yield PUBLISHED


def publish(item: OrgDomain) -> None:
    PUBLISHED.setdefault(item.record_name, []).append(item.record_value)


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
    publish(orgs(site).domain(orgs(site).require("Acme"), "acme.test"))
    assert (await post_org(site, ada, "/domains/check", domain="acme.test")).status_code == 200
    assert (await post_org(site, ada, "/admins", email="bea@acme.test")).status_code == 200
    made = await post_org(site, ada, "/vaults", name="Wiki", slug="wiki", layout="shared")
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
        policies = await post_org(site, ada, "/vaults", name="Policies", slug="policies", layout="shared")
        assert policies.status_code == 200
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
async def test_a_locked_vault_refuses_a_member_who_names_it_from_their_own_vaults_connection(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        await site.sign_in(cy, "cy@acme.test")
        await post_org(site, ada, "/vaults", name="Policies", slug="policies", layout="shared")
        await post_org(site, ada, "/vaults/lock", vault="policies", locked="on")
        await post_org(site, ada, "/vaults/editors", vault="policies", email="bea@acme.test")
        arguments = {"path": "Resources/leave.md", "content": POLICY, "vault": "policies"}
        refused = await call_tool(cy, cli_key(site, "cy@acme.test", spans=True), "write_file", arguments)
        allowed = await call_tool(bea, cli_key(site, "bea@acme.test", spans=True), "write_file", arguments)
    assert refused.get("isError") and "Policies is locked: only its editors (bea@acme.test)" in text_of(refused)
    assert not allowed.get("isError"), text_of(allowed)


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
async def test_a_removed_member_does_not_rejoin_through_another_address_at_the_domain(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        await site.sign_in(cy, "cy@acme.test")
        assert "is removed" in (await post_org(site, ada, "/members/remove", email="cy@acme.test")).text
    account = site.store.account("cy@acme.test")
    identities = Identities(site.store)
    again = identities.sign_in(Proof("github", "4242", ("cy@acme.test", "cyrus@acme.test"), "cy"), "invite")
    renamed = identities.sign_in(Proof("github", "4242", ("cyrus@acme.test",), "cy"), "invite")
    assert again.account.id == account.id and renamed.account.id == account.id
    assert orgs(site).of_account(account.id) == []
    fresh = identities.sign_in(Proof("github", "5151", ("cy@acme.test", "cy2@acme.test"), "cy"), "invite")
    assert fresh.account.id == account.id and orgs(site).of_account(account.id) == []


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
    assert "Someone an admin added by email keeps their account whatever\nthe setting." in saved.text
    assert "their own vaults are deleted on" in removed.text and "An email tells them" in removed.text
    message = site.smtp.messages[-1]
    assert message["To"] == "cy@acme.test" and message["Subject"] == "Acme removed you from its vaults"
    body = message.get_body(("plain",)).get_content()
    due = datetime.fromtimestamp(site.clock() + 14 * 86400, UTC)
    assert "http://localhost/account" in body
    assert f"Sign in before {due.day} {due:%B %Y} to keep your account" in " ".join(body.split())
    sealed = SealedVaults(site.store, VaultKeys(site.store, site.keyring))

    def erase(account) -> None:
        delete_account(sealed, account, vaults.cache)

    assert delete_departed(orgs(site), erase) == 0
    site.clock.advance(15 * 86400)
    assert delete_departed(orgs(site), erase) == 1
    assert site.store.account("cy@acme.test") is None
    assert history(site)[0].author_email == "cy@acme.test"
    assert delete_departed(orgs(site), erase) == 0
    async with site.browser() as back:
        await site.sign_in(back, "cy@acme.test")
    assert orgs(site).due() == []
    assert orgs(site).removed(orgs(site).require("Acme"))[0].kept_at is None


@pytest.mark.anyio
async def test_signing_in_before_the_date_keeps_a_removed_members_account_and_vaults(
    site: Hosted, vaults: SubjectVaults
):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        await site.sign_in(cy, "cy@acme.test")
        await write(cy, key(site, "cy@acme.test"), "Areas/mine.md", DEPLOY)
        await post_org(site, ada, "/removal", removal="export-and-delete", days="14")
        await post_org(site, ada, "/members/remove", email="cy@acme.test")
        async with site.browser() as back:
            returned = await site.sign_in(back, "cy@acme.test")
        page = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
    sealed = SealedVaults(site.store, VaultKeys(site.store, site.keyring))
    site.clock.advance(15 * 86400)
    assert delete_departed(orgs(site), lambda account: delete_account(sealed, account, vaults.cache)) == 0
    account = site.store.account("cy@acme.test")
    today = datetime.fromtimestamp(time.time(), UTC)
    assert returned.status_code == 303 and account is not None and account.active
    assert site.store.vaults(account.id) and not orgs(site).of_account(account.id)
    assert f"<td>kept: signed in again on {today.day} {today:%b}</td>" in page.text


@pytest.mark.anyio
async def test_an_outsiders_account_joins_only_on_their_yes_and_its_removal_never_deletes_it(
    site: Hosted, vaults: SubjectVaults
):
    ada, bea = await company(site)
    site.store.invite("eve@other.test")
    eve_account = site.store.account("eve@other.test")
    async with ada, bea, site.browser() as eve:
        assert (await post_org(site, ada, "/removal", removal="export-and-delete", days="1")).status_code == 200
        added = await post_org(site, ada, "/members", email="eve@other.test")
        waiting = orgs(site).of_account(eve_account.id)
        admin_page = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
        await site.sign_in(eve, "eve@other.test")
        settings = await eve.get("/account/settings")
        accepted = await eve.post(
            f"/account/organisation/{orgs(site).require('Acme').id}/accept", data={"csrf": csrf_of(settings.text)}
        )
        joined = orgs(site).of_account(eve_account.id)
        removed = await post_org(site, ada, "/members/remove", email="eve@other.test")
    assert "they join once they accept the invitation on their account page" in added.text
    assert waiting == [] and "<h2>Invitations · 1</h2>" in admin_page.text
    assert "Organisation invitations" in settings.text and "<td>Acme</td><td>Member</td>" in settings.text
    assert accepted.status_code == 200 and [found.name for found, _ in joined] == ["Acme"]
    assert "their own vaults stay with their account" in removed.text
    sealed = SealedVaults(site.store, VaultKeys(site.store, site.keyring))
    site.clock.advance(2 * 86400)
    assert delete_departed(orgs(site), lambda account: delete_account(sealed, account, vaults.cache)) == 0
    assert site.store.account("eve@other.test") is not None


def test_a_declined_or_withdrawn_invitation_lets_nobody_in(tmp_path: Path):
    store = HostedStore(tmp_path)
    organisations = Organisations(store)
    organisation = organisations.create("Acme", "ada@acme.test")
    store.invite("eve@other.test")
    eve = store.account("eve@other.test")
    assert organisations.add_admin(organisation, "eve@other.test").pending
    assert organisations.decline(organisation, eve.id)
    with pytest.raises(OrganisationError, match="no invitation"):
        organisations.accept(organisation, eve)
    organisations.add_member(organisation, "eve@other.test")
    assert organisations.withdraw(organisation, "eve@other.test")
    assert organisations.pending(organisation) == [] and organisations.of_account(eve.id) == []


def test_a_departed_account_that_still_shares_a_vault_is_kept_and_not_retried(tmp_path: Path):
    store = HostedStore(tmp_path)
    moment = [1_800_000_000.0]
    organisations = Organisations(store, lambda: moment[0])
    organisation = organisations.create("Acme", "ada@acme.test")
    organisations.add_domain(organisation, "acme.test", verified=True)
    organisation = organisations.set_removal(organisation, "export-and-delete", 1)
    Identities(store).sign_in(Proof("github", "cy", ("cy@acme.test",), "cy"), "open")
    organisations.remove_member(organisation, "cy@acme.test")
    tried: list[str] = []

    def erase(account) -> None:
        tried.append(account.email)
        raise StillShared("cy@acme.test still owns a vault others use")

    moment[0] += 2 * 86400
    assert delete_departed(organisations, erase) == 0
    assert delete_departed(organisations, erase) == 0
    assert tried == ["cy@acme.test"] and store.account("cy@acme.test") is not None


class Keeper(Extension):
    def may_erase(self, account) -> bool:
        return account.email != "cy@acme.test"


def test_an_extension_keeps_a_departed_account_the_organisation_would_delete(tmp_path: Path):
    store = HostedStore(tmp_path, extensions=[Keeper()])
    moment = [1_800_000_000.0]
    organisations = Organisations(store, lambda: moment[0])
    organisation = organisations.create("Acme", "ada@acme.test")
    organisations.add_domain(organisation, "acme.test", verified=True)
    organisation = organisations.set_removal(organisation, "export-and-delete", 1)
    for email in ("cy@acme.test", "dan@acme.test"):
        Identities(store).sign_in(Proof("github", email, (email,), email), "open")
        assert organisations.remove_member(organisation, email).delete_after is not None
    erased: list[str] = []
    moment[0] += 2 * 86400
    assert delete_departed(organisations, lambda account: erased.append(account.email)) == 1
    assert erased == ["dan@acme.test"] and organisations.due() == []


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
    orgs(site).add_domain(organisation, "acme.test", verified=True)
    assert orgs(site).of_account(account.id) == []
    async with dan, site.browser() as again:
        await site.sign_in(again, "dan@acme.test")
    assert [found.name for found, _ in orgs(site).of_account(account.id)] == ["Acme"]


@pytest.mark.anyio
async def test_an_unverified_company_address_does_not_join(site: Hosted):
    organisation = orgs(site).create("Acme", "ada@acme.test")
    orgs(site).add_domain(organisation, "acme.test", verified=True)
    async with site.browser() as http:
        answer = await site.sign_in(http, "mal@acme.test", verified=False)
    assert answer.status_code == 403 and site.store.account("mal@acme.test") is None


@pytest.mark.anyio
async def test_an_admin_cannot_claim_a_public_email_provider(site: Hosted):
    orgs(site).create("Acme", "ada@acme.test")
    ada, _ = await signed_in(site, "ada@acme.test")
    async with ada:
        answers = [
            await post_org(site, ada, "/domains", domain=domain)
            for domain in ("gmail.com", "GoogleMail.com", "yahoo.co.uk", "gmx.de", "proton.me", "web.de")
        ]
    assert all(answer.status_code == 400 and "public email provider" in answer.text for answer in answers)
    assert orgs(site).domains(orgs(site).require("Acme")) == []


def test_the_operator_lists_more_public_providers_and_only_the_cli_forces_one(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("VAULT_PUBLIC_EMAIL_DOMAINS", "freemail.test, @Mailbox.Test.")
    store = HostedStore(tmp_path)
    organisations = Organisations(store)
    organisation = organisations.create("Acme", "ada@acme.test")
    for domain in ("freemail.test", "eu.mailbox.test", "gmail.com"):
        with pytest.raises(OrganisationError, match="public email provider"):
            organisations.add_domain(organisation, domain, verified=True)
    assert organisations.add_domain(organisation, "gmail.com", verified=True, allow_public=True).verified
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "auth"))
    assert main(["org", "create", "Mail", "--admin", "op@mail.test"]) == 0
    assert main(["org", "domain", "add", "Mail", "hotmail.com", "--verified"]) == 2
    assert main(["org", "domain", "add", "Mail", "hotmail.com", "--verified", "--allow-public"]) == 0
    assert "addresses at hotmail.com join Mail" in capsys.readouterr().out


@pytest.mark.anyio
async def test_an_unverified_domain_lets_nobody_in_until_its_txt_record_matches(site: Hosted):
    orgs(site).create("Acme", "ada@acme.test")
    site.store.invite("dan@acme.test")
    ada, _ = await signed_in(site, "ada@acme.test")
    async with ada, site.browser() as cy, site.browser() as dan:
        added = await post_org(site, ada, "/domains", domain="acme.test")
        claim = orgs(site).domain(orgs(site).require("Acme"), "acme.test")
        missing = await post_org(site, ada, "/domains/check", domain="acme.test")
        remembered = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
        early = await site.sign_in(cy, "cy@acme.test")
        made_early = site.store.account("cy@acme.test")
        await site.sign_in(dan, "dan@acme.test")
        PUBLISHED[claim.record_name] = ["mindin-verify=someone-elses-token"]
        wrong = await post_org(site, ada, "/domains/check", domain="acme.test")
        PUBLISHED[claim.record_name].append(claim.record_value)
        other = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
        checked = await post_org(site, ada, "/domains/check", domain="acme.test")
        joined = await site.sign_in(cy, "cy@acme.test")
    assert added.status_code == 200 and "Not checked yet." in added.text
    assert '<code id="record-host-0">_mindin-verify.acme.test</code>' in added.text
    assert f'<code id="record-value-0">{claim.record_value}</code>' in added.text
    assert 'data-copy="record-value-0" hidden>Copy</button>' in added.text
    assert missing.status_code == 400 and "Checked today " in remembered.text
    assert ": no TXT record yet. DNS changes can take up to an hour." in remembered.text
    assert "a TXT record is there, but not this value" in other.text
    assert not claim.verified and claim.record_value.startswith("mindin-verify=")
    assert early.status_code == 403 and made_early is None
    assert orgs(site).of_account(site.store.account("dan@acme.test").id) == []
    assert wrong.status_code == 400 and "No TXT record" in wrong.text
    assert checked.status_code == 200 and "acme.test is verified" in checked.text
    assert joined.status_code == 303
    assert [found.name for found, _ in orgs(site).of_account(site.store.account("cy@acme.test").id)] == ["Acme"]


def test_the_operator_cli_adds_a_domain_unverified_or_verified(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "auth"))
    assert main(["org", "create", "Acme", "--admin", "ada@acme.test"]) == 0
    assert main(["org", "create", "Beta", "--admin", "bo@beta.test"]) == 0
    assert main(["org", "domain", "add", "Acme", "acme.test"]) == 0
    told = capsys.readouterr().out
    assert main(["org", "domain", "add", "Beta", "beta.test", "--verified"]) == 0
    assert main(["org", "domain", "check", "Acme", "acme.test"]) == 2
    capsys.readouterr()
    assert main(["org", "show", "Acme"]) == 0
    acme = capsys.readouterr().out
    assert main(["org", "show", "Beta"]) == 0
    beta = capsys.readouterr().out
    assert "add a TXT record at _mindin-verify.acme.test with the value mindin-verify=" in told
    assert "domain  @acme.test  unverified: TXT _mindin-verify.acme.test mindin-verify=" in acme
    assert "domain  @beta.test  verified" in beta
    identities = Identities(HostedStore(tmp_path / "auth"))
    assert identities.sign_in(Proof("oidc", "s1", ("cy@acme.test",)), "invite").refusal == "uninvited"
    assert identities.sign_in(Proof("oidc", "s2", ("bea@beta.test",)), "invite").account is not None


def test_verified_on_a_domain_already_claimed_marks_that_claim_verified(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "auth"))
    for name, admin in (("Acme", "ada@acme.test"), ("Beta", "bo@beta.test"), ("Gamma", "gi@gamma.test")):
        assert main(["org", "create", name, "--admin", admin]) == 0
    assert main(["org", "domain", "add", "Acme", "acme.test"]) == 0
    assert main(["org", "domain", "add", "Gamma", "acme.test"]) == 0
    assert main(["org", "domain", "add", "Acme", "acme.test"]) == 2
    capsys.readouterr()
    assert main(["org", "domain", "add", "Acme", "acme.test", "--verified"]) == 0
    marked = capsys.readouterr().out
    assert main(["org", "domain", "add", "Beta", "acme.test", "--verified"]) == 2
    refused = capsys.readouterr().err
    assert main(["org", "show", "Acme"]) == 0
    acme = capsys.readouterr().out
    assert main(["org", "show", "Gamma"]) == 0
    gamma = capsys.readouterr().out
    assert "addresses at acme.test join Acme when they sign in" in marked
    assert "acme.test already belongs to another organisation." in refused
    assert "domain  @acme.test  verified" in acme and "acme.test" not in gamma


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


@pytest.mark.anyio
async def test_a_member_neither_moves_nor_deletes_the_vaults_layout_over_webdav(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        await site.sign_in(cy, "cy@acme.test")
        member = basic(key(site, "cy@acme.test"), "cy")
        await cy.put("/dav/vault/Resources/deploys.md", content=DEPLOY.encode(), headers=member)
        moved = await cy.request(
            "MOVE", f"/dav/vault/{SCHEMA_KEY}", headers={**member, "Destination": "/dav/vault/Areas/schema.md"}
        )
        moved_in = await cy.request(
            "MOVE", "/dav/vault/Resources/deploys.md", headers={**member, "Destination": f"/dav/vault/{SCHEMA_KEY}"}
        )
        deleted = await cy.delete(f"/dav/vault/{SCHEMA_KEY}", headers=member)
        folder = await cy.delete("/dav/vault/.vault/", headers=member)
        schema = await cy.get(f"/dav/vault/{SCHEMA_KEY}", headers=member)
    assert [moved.status_code, moved_in.status_code, deleted.status_code, folder.status_code] == [403] * 4
    assert schema.status_code == 200 and b"preset: shared" in schema.content


def test_only_a_layout_editor_deletes_moves_or_makes_folders_under_the_vaults_layout(tmp_path: Path):
    inner = LocalBackend(tmp_path / "vault")
    provision.initialize(inner, preset="shared")
    inner.put("Areas/orchard.md", DEPLOY.replace("kind: howto", "kind: system"))
    admin = Actor("ada@acme.test", "acc_ada", "laptop")
    member = Actor("cy@acme.test", "acc_cy", "laptop")
    backend = SharedBackend(inner, SharedRules("Wiki"), NoHistory(), edits_layout=lambda actor: actor == admin)
    _, version = inner.get(SCHEMA_KEY)
    with acting_as(member):
        for attempt in (
            lambda: backend.delete(SCHEMA_KEY),
            lambda: backend.delete_checked(SCHEMA_KEY, version),
            lambda: backend.move(SCHEMA_KEY, "Areas/schema.md"),
            lambda: backend.move("Areas/orchard.md", ".vault/orchard.md"),
            lambda: backend.mkdir(".vault/more/"),
        ):
            with pytest.raises(SharedRefusal, match="layout of the shared vault Wiki is fixed"):
                attempt()
    assert inner.get(SCHEMA_KEY)[1] == version
    with acting_as(admin):
        backend.delete(SCHEMA_KEY)
    with acting_as(member), pytest.raises(SharedRefusal, match="has no .vault/schema.yml, so it takes no writes"):
        backend.put("Areas/pruning.md", DEPLOY.replace("kind: howto", "kind: system"))
    with acting_as(admin), pytest.raises(SharedRefusal, match="has no .vault/schema.yml"):
        backend.put("Areas/pruning.md", DEPLOY.replace("kind: howto", "kind: system"))
    with acting_as(admin):
        backend.put(SCHEMA_KEY, "preset: shared\n")
    with acting_as(member):
        backend.put("Areas/pruning.md", DEPLOY.replace("kind: howto", "kind: system"))
    assert inner.get("Areas/pruning.md")[0].startswith("---")


class NoHistory:
    def record(self, *args, **kwargs) -> None:
        pass


def test_a_domain_matches_exactly_and_ignores_case(tmp_path: Path):
    store = HostedStore(tmp_path)
    organisations = Organisations(store)
    organisation = organisations.create("Acme", "ada@acme.test")
    assert organisations.add_domain(organisation, " @Acme.TEST. ", verified=True).domain == "acme.test"
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


def test_a_verified_domain_belongs_to_one_organisation_and_an_unverified_claim_blocks_nobody(tmp_path: Path):
    organisations = Organisations(HostedStore(tmp_path))
    first = organisations.create("Acme", "ada@acme.test")
    second = organisations.create("Other", "oz@other.test")
    squatted = organisations.add_domain(second, "acme.test")
    claim = organisations.add_domain(first, "acme.test")
    with pytest.raises(OrganisationError, match="this organisation"):
        organisations.add_domain(first, "acme.test")
    publish(claim)
    assert organisations.check_domain(first, "acme.test").verified
    assert organisations.domains(second) == [], "the losing unverified claim is dropped"
    with pytest.raises(OrganisationError, match="another organisation"):
        organisations.add_domain(second, "acme.test")
    assert squatted.token != claim.token


def test_the_company_address_becomes_the_account_address(tmp_path: Path):
    store = HostedStore(tmp_path)
    organisations = Organisations(store)
    organisations.add_domain(organisations.create("Acme", "ada@acme.test"), "acme.test", verified=True)
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


def test_a_removal_that_fails_midway_changes_nothing_and_a_retry_completes_it(tmp_path: Path):
    store = HostedStore(tmp_path)
    SupportAccess(store)
    organisations = Organisations(store)
    organisation = organisations.create("Acme", "ada@acme.test")
    organisations.add_admin(organisation, "bea@acme.test")
    vault = organisations.create_vault(organisation, store.account("ada@acme.test"), "Wiki", "wiki")
    ada = store.account("ada@acme.test")

    def failing() -> int:
        raise OSError("the clock is gone")

    organisations.moment = failing
    with pytest.raises(OSError):
        organisations.remove_member(organisation, "ada@acme.test")
    assert store.vault(vault.id).account_id == ada.id
    assert [member.account.email for member in organisations.members(organisation)] == [
        "ada@acme.test",
        "bea@acme.test",
    ]
    del organisations.moment
    organisations.remove_member(organisation, "ada@acme.test")
    assert store.vault(vault.id).account_id == store.account("bea@acme.test").id
    assert not store.reaches(store.vault(vault.id), ada.id) and organisations.of_account(ada.id) == []


def test_only_an_admin_makes_org_vaults_and_editors_are_members(tmp_path: Path):
    store = HostedStore(tmp_path)
    organisations = Organisations(store)
    organisation = organisations.create("Acme", "ada@acme.test")
    cy = organisations.add_member(organisation, "cy@acme.test").account
    with pytest.raises(OrganisationError, match="Only an admin"):
        organisations.create_vault(organisation, cy, "Wiki")
    item = organisations.org_vault(organisations.create_vault(organisation, store.account("ada@acme.test"), "Wiki"))
    with pytest.raises(OrganisationError, match="not a member"):
        organisations.add_editor(item, "eve@else.test")


def test_the_cli_sets_up_an_organisation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "auth"))
    assert main(["org", "create", "Acme", "--admin", "ada@acme.test"]) == 0
    assert main(["org", "domain", "add", "Acme", "acme.test", "--verified"]) == 0
    assert main(["org", "admin", "add", "Acme", "bea@acme.test"]) == 0
    assert main(["org", "vault", "create", "Acme", "Wiki", "--slug", "wiki"]) == 0
    assert main(["org", "vault", "lock", "Acme", "wiki"]) == 0
    assert main(["org", "vault", "editor-add", "Acme", "wiki", "bea@acme.test"]) == 0
    assert main(["org", "removal", "Acme", "export-and-delete", "--days", "10"]) == 0
    assert main(["org", "sharing", "Acme", "allowed"]) == 0
    assert main(["org", "admin", "remove", "Acme", "zed@acme.test"]) == 2
    capsys.readouterr()
    assert main(["org", "show", "Acme"]) == 0
    shown = capsys.readouterr().out
    assert "removed members: export-and-delete after 10 days  sharing outside: allowed" in shown
    assert (
        "domain  @acme.test  verified" in shown and "admin  ada@acme.test" in shown and "admin  bea@acme.test" in shown
    )
    assert "vault  wiki  Wiki  locked, editors: bea@acme.test" in shown


def test_a_forwarded_identity_joins_by_domain_and_shares_the_account_with_the_company_sign_in(tmp_path: Path):
    store = HostedStore(tmp_path)
    organisations = Organisations(store)
    organisations.add_domain(organisations.create("Acme", "ada@acme.test"), "acme.test", verified=True)
    identities = Identities(store)
    through_proxy = identities.sign_in(Proof("forwarded", "proxy-cy", ("cy@acme.test",), "cy@acme.test"), "invite")
    on_the_pages = identities.sign_in(Proof("oidc", "idp-cy", ("cy@acme.test",), "cy@acme.test"), "invite")
    assert through_proxy.account is not None and on_the_pages.account is not None
    assert through_proxy.account.id == on_the_pages.account.id
    assert [found.name for found, _ in organisations.of_account(through_proxy.account.id)] == ["Acme"]
    assert sorted(way.method for way in identities.ways_in(through_proxy.account.id)) == ["forwarded", "oidc"]


def note(title: str, body: str, day: str = "2026-11-01", kind: str = "howto", area: str = "payments") -> str:
    return (
        f"---\ntitle: {title}\ndate: {day}\nupdated: {day}\nstatus: active\ntags: [payments]\nkind: {kind}\n"
        f'area: "[[{area}]]"\n---\n\n{body}\n'
    )


def cli_key(site: Hosted, email: str, slug: str | None = None, spans: bool = False) -> str:
    account = site.store.account(email)
    vault = site.store.vault_at(account.id, slug) if slug else site.store.first_vault(account.id)
    scopes = ["vault:read", "vault:write"]
    return site.store.create_key(account.id, "cli", scopes, purpose="device", vault_id=vault.id, spans_account=spans)


async def in_one_session(
    http: httpx2.AsyncClient, token: str, calls: list[tuple[str, dict]], path: str = "/mcp"
) -> list[dict]:
    session = await open_session(http, token, path)
    answers = []
    for number, (name, arguments) in enumerate(calls, 2):
        body = {
            "jsonrpc": "2.0",
            "id": number,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = await post_mcp(http, token, body, session, path)
        assert response.status_code == 200, response.text
        answers.append(sse_payload(response.text)["result"])
    return answers


@pytest.mark.anyio
async def test_flow_3_search_labels_own_and_wiki_rows_and_reads_the_wiki_in_the_same_session(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea:
        guide = note("Adding Klarna", "The payments team added Klarna through the checkout adapter.")
        assert not (await write(ada, key(site, "ada@acme.test"), "Resources/klarna.md", guide)).get("isError")
        own = cli_key(site, "bea@acme.test")
        for number in range(4):
            old = note(
                f"Klarna notes {number}", "Klarna klarna klarna, old thoughts.", day="2024-01-0" + str(number + 1)
            )
            assert not (await write(bea, own, f"Resources/klarna-{number}.md", old, slug="notes")).get("isError")
        found, read, mine, shared = await in_one_session(
            bea,
            own,
            [
                ("search", {"query": "klarna"}),
                ("read_file", {"path": "Resources/klarna.md", "vault": "wiki"}),
                ("write_file", {"path": "Resources/mine.md", "content": note("Mine", "Mine.")}),
                ("write_file", {"path": "Resources/ours.md", "content": note("Ours", "Ours."), "vault": "wiki"}),
            ],
        )
        narrow = await call_tool(bea, key(site, "bea@acme.test", "notes"), "search", {"query": "klarna"})
    lines = text_of(found).splitlines()
    assert lines[0].startswith("5 of 5"), lines
    assert lines[1].startswith("Searched 2 vaults: notes (") and "wiki (Wiki, Acme)" in lines[1], lines[1]
    assert lines[2].startswith("wiki | Resources/klarna.md | Adding Klarna"), lines
    assert all(line.startswith("notes | Resources/klarna-") for line in lines[3:7]), lines
    assert "checkout adapter" in text_of(read), text_of(read)
    assert not mine.get("isError") and not shared.get("isError"), (text_of(mine), text_of(shared))
    assert [change.path for change in history(site)][0] == "Resources/ours.md"
    assert "Searched" not in text_of(narrow) and "wiki |" not in text_of(narrow), "an API key stays on its vault"


@pytest.mark.anyio
async def test_a_member_of_no_organisation_searches_and_reads_one_vault_as_before(site: Hosted):
    await company(site)
    site.invite("dan@elsewhere.test")
    async with site.browser() as dan:
        assert (await site.sign_in(dan, "dan@elsewhere.test")).status_code == 303
        own = cli_key(site, "dan@elsewhere.test")
        assert not (await write(dan, own, "Resources/klarna.md", note("Klarna", "Klarna."), slug="notes")).get(
            "isError"
        )
        found, other = await in_one_session(
            dan,
            own,
            [("search", {"query": "klarna"}), ("read_file", {"path": "Resources/klarna.md", "vault": "wiki"})],
        )
        bundle = await call_tool(dan, own, "context", {"path": "/src/payments", "repo": "klarna"})
    lines = text_of(found).splitlines()
    assert lines[0].startswith("1 of 1") and lines[1].startswith("Resources/klarna.md | Klarna |"), lines
    assert other.get("isError") and "does not reach a vault 'wiki'" in text_of(other)
    assert "## system notes" not in text_of(bundle) and "organisation pages" not in text_of(bundle)


def wiki_page(title: str, repos: str = "", area: str = "payments", kind: str = "howto") -> str:
    listed = f"repos: [{repos}]\n" if repos else ""
    return (
        f'---\ntitle: {title}\n{FRONT}tags: [team]\nkind: {kind}\narea: "[[{area}]]"\n{listed}'
        f"summary: How {title.lower()} works.\n---\n\nBody of {title}.\n"
    )


@pytest.mark.anyio
async def test_flow_2_session_start_lists_the_wikis_pages_for_the_repo_capped(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea:
        wiki = key(site, "ada@acme.test")
        pages = {
            "Resources/disputes.md": wiki_page("Disputes", "github.com/acme/payments"),
            "Resources/klarna.md": wiki_page("Klarna", "acme/payments"),
            "Resources/ledger.md": wiki_page("Ledger", "payments"),
            "Areas/payments.md": wiki_page("Payments", kind="system"),
            "Resources/fees.md": wiki_page("Fees"),
            "Resources/payouts.md": wiki_page("Payouts"),
            "Resources/search.md": wiki_page("Search index", "acme/search-api", area="search"),
        }
        for path, text in pages.items():
            assert not (await write(ada, wiki, path, text)).get("isError"), path
        own = cli_key(site, "bea@acme.test")
        hub = note("Payments", "Our payments service.", kind="system")
        assert not (await write(bea, own, "Areas/payments.md", hub, slug="notes")).get("isError")
        arguments = {"path": "/src/payments", "repo": "payments", "remote": "git@github.com:acme/payments.git"}
        bundle = await call_tool(bea, own, "context", arguments | {"format": "json"})
    text = json.loads(text_of(bundle))["text"]
    section = text.split("## organisation pages\n", 1)[1].split("\n\n", 1)[0].splitlines()
    rows = [line for line in section if line.startswith("wiki | ")]
    assert len(rows) == 5, section
    assert [row.split(" | ")[1] for row in rows[:4]] == [
        "Resources/disputes.md",
        "Resources/klarna.md",
        "Resources/ledger.md",
        "Areas/payments.md",
    ], rows
    assert rows[4].split(" | ")[1] in ("Resources/fees.md", "Resources/payouts.md"), rows
    assert "wiki | Resources/klarna.md | Klarna | How klarna works." in rows
    assert "search.md" not in text and "read_file(path, vault=<vault>)" in section[-1]
    assert text.index("## system notes") < text.index("## organisation pages")


async def invite_to(http: httpx2.AsyncClient, slug: str, email: str) -> httpx2.Response:
    page = await http.get(f"/account/vaults/{slug}")
    return await http.post(f"/account/vaults/{slug}/members/invite", data={"csrf": csrf_of(page.text), "email": email})


@pytest.mark.anyio
async def test_sharing_a_members_own_vault_outside_follows_the_organisations_policy(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea:
        form = await bea.get("/account/vaults/new")
        made = await bea.post("/account/vaults", data={"csrf": csrf_of(form.text), "name": "Side", "kind": "shared"})
        assert made.status_code == 303, made.text
        shown = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
        outside = await invite_to(bea, "side", "eve@elsewhere.test")
        colleague = await invite_to(bea, "side", "cy@acme.test")
        allowed = await post_org(site, ada, "/external-sharing", external_sharing="allowed")
        later = await invite_to(bea, "side", "eve@elsewhere.test")
        closed = await post_org(site, ada, "/external-sharing", external_sharing="denied")
    assert "Sharing outside Acme" in shown.text
    assert 'value="denied" checked><b>Members only</b>' in shown.text, "the default keeps sharing inside"
    assert outside.status_code == 400
    assert "Acme does not let its members share vaults with people outside it, and eve@elsewhere.test" in outside.text
    assert colleague.status_code == 200 and "cy@acme.test" in colleague.text
    assert "may share the vaults they own with anyone" in allowed.text
    assert later.status_code == 200 and "eve@elsewhere.test" in later.text
    assert "share the vaults they own only with each other" in closed.text
    pending = {
        member.email
        for member in site.store.members(site.store.vault_at(site.store.account("bea@acme.test").id, "side"))
    }
    assert {"cy@acme.test", "eve@elsewhere.test"} <= pending, "a stricter policy ends no sharing that exists"


@pytest.mark.anyio
async def test_an_organisation_vault_is_never_shared_outside_whatever_the_policy(site: Hosted):
    await company(site)
    organisation = orgs(site).set_external_sharing(orgs(site).require("Acme"), "allowed")
    wiki = orgs(site).vault_named(organisation, "wiki").vault
    ada = site.store.account("ada@acme.test")
    with pytest.raises(MembershipError, match="eve@elsewhere.test is outside Acme, and an organisation's vaults"):
        site.store.invite_member(wiki, "eve@elsewhere.test", ada.id)


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
        assert main(["org", "admin", "add", "Beta", "gil@else.test"]) == 0
    assert plans.made == [
        ("ada@acme.test", "operator", True, "Acme"),
        ("cy@else.test", "organisation", True, "Acme"),
        ("bea@else.test", "organisation", True, "Acme"),
        ("own@solo.test", "operator", True, "own@solo.test"),
        ("dan@solo.test", "oidc", True, "dan@solo.test"),
        ("eve@solo.test", "operator", True, "eve@solo.test"),
        ("bo@beta.test", "operator", True, "Beta"),
        ("fay@else.test", "operator", True, "Beta"),
        ("gil@else.test", "operator", True, "Beta"),
    ]
    assert running.store.account("cy@else.test").name == "planned"


@pytest.mark.anyio
async def test_a_verified_domain_says_when_and_how_many_joined_through_it(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        page = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
        await site.sign_in(cy, "cy@acme.test")
        after = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
    day = datetime.fromtimestamp(site.clock(), UTC)
    assert f'<span class="chip">Verified {day.day} {day:%b %Y}</span>' in page.text
    assert "nobody has joined through it yet · the record is not read again" in page.text
    assert "1 person joined through it · the record is not read again" in after.text


@pytest.mark.anyio
async def test_the_lead_counts_the_members_and_the_vaults(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        page = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
        await site.sign_in(cy, "cy@acme.test")
        after = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
    assert '<p class="lead">Organisation · you are an admin · 2 members · 1 vault</p>' in page.text
    assert '<p class="lead">Organisation · you are an admin · 3 members · 1 vault</p>' in after.text


@pytest.mark.anyio
async def test_each_member_row_says_whether_they_have_signed_in(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        await site.sign_in(cy, "cy@acme.test")
        assert (await post_org(site, ada, "/members", email="dan@acme.test")).status_code == 200
        page = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
    assert "<th>Signed in</th>" in page.text
    assert '<td>ada@acme.test</td><td>Admin</td><td><span class="chip">today ' in page.text
    assert '<td>cy@acme.test</td><td>Member</td><td><span class="chip">today ' in page.text
    assert "<td>joined through @acme.test</td>" in page.text
    assert (
        '<td>dan@acme.test</td><td>Member</td><td><span class="chip off">not yet</span></td>'
        "<td>added by an admin</td>" in page.text
    )


@pytest.mark.anyio
async def test_an_invitation_is_mailed_and_sent_again_at_most_every_ten_minutes(site: Hosted):
    ada, bea = await company(site)
    site.store.invite("eve@other.test")
    async with ada, bea:
        added = await post_org(site, ada, "/members", email="eve@other.test")
        page = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
        early = await post_org(site, ada, "/invitations/resend", email="eve@other.test")
        site.clock.advance(601)
        again = await post_org(site, ada, "/invitations/resend", email="eve@other.test")
        withdrawn = await post_org(site, ada, "/invitations/remove", email="eve@other.test")
    mailed = [message for message in site.smtp.messages if message["To"] == "eve@other.test"]
    assert "An email tells them so." in added.text and len(mailed) == 2
    assert mailed[0]["Subject"] == "Acme invites you to its vaults"
    body = mailed[0].get_body(("plain",)).get_content()
    assert "ada@acme.test added eve@other.test to Acme" in body and "http://localhost/account/settings" in body
    assert "<h2>Invitations · 1</h2>" in page.text and "<td>eve@other.test</td><td>ada@acme.test</td>" in page.text
    assert "waiting for them to accept on their account page" in page.text
    assert 'aria-label="Send again eve@other.test">Send again</button>' in page.text
    assert "<td>eve@other.test</td>" not in page.text.split("<h2>Invitations")[0]
    assert early.status_code == 400 and "send it again from" in early.text
    assert again.status_code == 200 and "The invitation email went to eve@other.test again." in again.text
    assert withdrawn.status_code == 200 and "<h2>Invitations" not in withdrawn.text


@pytest.mark.anyio
async def test_without_smtp_an_invitation_waits_on_the_account_page_and_is_not_sent_again(
    vaults: SubjectVaults, tmp_path: Path
):
    async with hosted(vaults, tmp_path, smtp=None) as plain:
        orgs(plain).create("Acme", "ada@acme.test")
        plain.store.invite("eve@other.test")
        ada, _ = await signed_in(plain, "ada@acme.test")
        async with ada:
            added = await post_org(plain, ada, "/members", email="eve@other.test")
    assert "This server sends no email; tell them it waits there." in added.text
    assert "This server sends no email, so each invitation waits on the person's account page" in added.text
    assert "Send again" not in added.text and "Withdraw eve@other.test" in added.text


@pytest.mark.anyio
async def test_each_org_vault_shows_its_notes_and_layout(site: Hosted):
    ada, bea = await company(site)
    async with ada, bea:
        await write(bea, key(site, "bea@acme.test"), "Resources/deploys.md", DEPLOY)
        page = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
    assert '<p class="facts">1 note · shared layout</p>' in page.text
    assert " MB" not in page.text and "1 GB" not in page.text


@pytest.mark.anyio
async def test_the_removed_list_says_what_happens_to_each_account_and_when(site: Hosted, vaults: SubjectVaults):
    ada, bea = await company(site)
    async with ada, bea, site.browser() as cy:
        await site.sign_in(cy, "cy@acme.test")
        await post_org(site, ada, "/members", email="dan@acme.test")
        await post_org(site, ada, "/removal", removal="export-and-delete", days="30")
        await post_org(site, ada, "/members/remove", email="cy@acme.test")
        removed = await post_org(site, ada, "/members/remove", email="dan@acme.test")
        sealed = SealedVaults(site.store, VaultKeys(site.store, site.keyring))
        site.clock.advance(31 * 86400)
        delete_departed(orgs(site), lambda account: delete_account(sealed, account, vaults.cache))
        later = await ada.get(f"/account/organisation/{orgs(site).require('Acme').id}")
    sent = datetime.fromtimestamp(site.clock() - 31 * 86400, UTC)
    due = datetime.fromtimestamp(site.clock() - 86400, UTC)
    gone = datetime.fromtimestamp(site.clock(), UTC)
    assert '<h2 class="subhead">Removed</h2>' in removed.text
    assert (
        f'<td>cy@acme.test</td><td class="nowrap">{sent:%Y-%m-%d}</td>'
        f"<td>export link sent {sent.day} {sent:%b} · deleted on {due.day} {due:%b} unless they sign in before"
        " then</td>" in removed.text
    )
    assert "<td>dan@acme.test</td>" in removed.text
    assert "<td>kept: added by an admin, not through a domain</td>" in removed.text
    assert f"<td>deleted on {gone.day} {gone:%b}, with their own vaults</td>" in later.text
