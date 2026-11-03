from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from forwarded_support import ISSUER, TYP, Proxy
from hosted_support import BASE, INITIALIZE, Hosted, csrf_of, hosted, query
from login_support import Authenticator

from notes_vault_mcp.auth import ForwardedConfig
from notes_vault_mcp.auth.hosted_store import HostedStore, now
from notes_vault_mcp.auth.identities import Identities, Proof
from notes_vault_mcp.vault import SubjectVaults

WEBAUTHN = "https://www.w3.org/TR/webauthn-3/"
GITHUB_OAUTH = "https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps"
GITHUB_EMAILS = "https://docs.github.com/en/rest/users/emails"


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


async def email_sign_in(site: Hosted, http: httpx2.AsyncClient, email: str, next_url: str = "/account"):
    sent = await http.post("/signin/email", data={"email": email, "next": next_url})
    assert sent.status_code == 200, sent.text
    link = site.smtp.last_link(email)
    token = query(link)["token"]
    confirm = await http.get("/signin/email/confirm", params={"token": token})
    assert confirm.status_code == 200, confirm.text
    return await http.post("/signin/email/confirm", data={"token": token})


async def github_sign_in(
    site: Hosted, http: httpx2.AsyncClient, user_id: int, login: str, emails: list[tuple[str, bool, bool]]
) -> httpx2.Response:
    started = await http.get("/signin/github", params={"next": "/account"})
    assert started.status_code == 302, started.text
    state, code = site.github.approve(started.headers["location"], user_id, login, emails)
    return await http.get("/signin/github/callback", params={"state": state, "code": code})


async def add_passkey(http: httpx2.AsyncClient, authenticator: Authenticator, name: str = "laptop") -> httpx2.Response:
    page = await http.get("/account/settings")
    csrf = csrf_of(page.text)
    begun = await http.post("/account/passkeys/options", json={"csrf": csrf})
    assert begun.status_code == 200, begun.text
    credential = authenticator.create(begun.json()["publicKey"])
    return await http.post(
        "/account/passkeys",
        json={"csrf": csrf, "ceremony": begun.json()["ceremony"], "credential": credential, "name": name},
    )


async def passkey_sign_in(
    http: httpx2.AsyncClient, authenticator: Authenticator, origin: str | None = None
) -> httpx2.Response:
    begun = await http.post("/signin/passkey/options", json={"next": "/account"})
    assert begun.status_code == 200, begun.text
    options = begun.json()["publicKey"]
    assert options.get("allowCredentials", []) == [], "discoverable credentials: the browser picks the passkey"
    assert options["userVerification"] == "required"
    credential = authenticator.get(options, origin)
    return await http.post("/signin/passkey", json={"ceremony": begun.json()["ceremony"], "credential": credential})


def ways(site: Hosted, email: str) -> list[tuple[str, str]]:
    account = site.store.account(email)
    assert account is not None
    return [(way.method, way.label) for way in site.identities.ways_in(account.id)]


# --- the sign-in page ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_sign_in_page_lists_exactly_the_configured_methods(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path, github=None, oidc=None, passkeys=False) as site, site.browser() as http:
        page = await http.get("/signin")
        github = await http.get("/signin/github")
    assert "Email me a link" in page.text
    assert "GitHub" not in page.text and "Pocket ID" not in page.text and "passkey" not in page.text
    assert github.status_code == 404


@pytest.mark.anyio
async def test_every_method_appears_when_configured(site: Hosted):
    async with site.browser() as http:
        page = await http.get("/signin")
        script = await http.get("/signin/passkey.js")
    assert all(label in page.text for label in ("Sign in with a passkey", "Email me a link", "GitHub", "Pocket ID"))
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert script.headers["content-type"].startswith("text/javascript")
    assert "navigator.credentials.get" in script.text


# --- email links --------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_email_link_signs_an_invited_address_in_over_starttls(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as http:
        landed = await email_sign_in(site, http, "Ann@Example.com")
        page = await http.get("/account/settings")
    assert landed.status_code == 303 and landed.headers["location"] == "/account"
    assert page.status_code == 200 and "ann@example.com" in page.text
    assert site.smtp.connections == [("smtp.example.test", 587)]
    assert site.smtp.logins == [("mailer", "smtp-password", True)], "the password only goes over TLS"
    message = site.smtp.messages[0]
    assert message["From"] == "vault@example.test" and message["To"] == "ann@example.com"
    assert site.store.account("ann@example.com").status == "active"
    assert ways(site, "ann@example.com") == [("email", "ann@example.com")]


@pytest.mark.anyio
async def test_opening_the_link_does_not_spend_it_and_posting_spends_it_once(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as http, site.browser() as other:
        await http.post("/signin/email", data={"email": "ann@example.com"})
        token = query(site.smtp.last_link("ann@example.com"))["token"]
        scanned = await other.get("/signin/email/confirm", params={"token": token})
        first = await http.post("/signin/email/confirm", data={"token": token})
        replay = await other.post("/signin/email/confirm", data={"token": token})
    assert scanned.status_code == 200, "a mail scanner's GET must not use the link up"
    assert first.status_code == 303
    assert replay.status_code == 400


@pytest.mark.anyio
async def test_an_email_link_expires_after_fifteen_minutes(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as http:
        await http.post("/signin/email", data={"email": "ann@example.com"})
        token = query(site.smtp.last_link("ann@example.com"))["token"]
        with site.store.connect() as connection:
            connection.execute("UPDATE ceremonies SET expires_at = ? WHERE kind = 'email'", (now() - 1,))
        late = await http.post("/signin/email/confirm", data={"token": token})
    assert late.status_code == 400
    with site.store.connect() as connection:
        stored = [row[0] for row in connection.execute("SELECT key_hash FROM ceremonies").fetchall()]
    assert token not in stored, "only the hash of the token is stored"


@pytest.mark.anyio
async def test_an_uninvited_address_gets_the_same_answer_and_no_mail(site: Hosted):
    async with site.browser() as http:
        answer = await http.post("/signin/email", data={"email": "stranger@example.com"})
    assert answer.status_code == 200
    assert "If stranger@example.com can sign in here" in answer.text
    assert site.smtp.messages == []


@pytest.mark.anyio
async def test_email_links_are_rate_limited_per_address_and_per_client(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as http:
        per_address = [
            (await http.post("/signin/email", data={"email": "ann@example.com"})).status_code for _ in range(4)
        ]
        per_client = [
            (await http.post("/signin/email", data={"email": f"x{n}@example.com"})).status_code for n in range(8)
        ]
    assert per_address == [200, 200, 200, 429]
    assert per_client[:6] == [200] * 6 and per_client[6:] == [429, 429], "ten per client in a quarter hour"
    assert len(site.smtp.messages) == 3


@pytest.mark.anyio
async def test_a_header_smuggled_into_the_address_is_refused(site: Hosted):
    async with site.browser() as http:
        answer = await http.post("/signin/email", data={"email": "ann@example.com\r\nBcc: all@example.com"})
    assert answer.status_code == 400
    assert site.smtp.messages == []


# --- github -------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_github_signs_in_by_a_verified_email_and_keys_the_identity_by_numeric_id(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as http:
        landed = await github_sign_in(site, http, 4242, "ann-codes", [("ann@example.com", True, True)])
        page = await http.get("/account/settings")
    assert landed.status_code == 303, landed.text
    assert page.status_code == 200
    account = site.store.account("ann@example.com")
    assert site.identities.account_for("github", "4242") == account, GITHUB_OAUTH
    async with site.browser() as http:
        renamed = await github_sign_in(site, http, 4242, "ann-renamed", [("new@example.com", True, True)])
    assert renamed.status_code == 303, "a new login name or email is still the same GitHub user"


@pytest.mark.anyio
async def test_an_unverified_github_email_claims_nothing(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as http:
        refused = await github_sign_in(site, http, 4242, "ann", [("ann@example.com", False, True)])
    assert refused.status_code == 403, GITHUB_EMAILS
    assert site.store.account("ann@example.com").status == "invited"


@pytest.mark.anyio
async def test_github_needs_the_browser_that_started_it(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as starter, site.browser() as attacker:
        started = await starter.get("/signin/github")
        state, code = site.github.approve(started.headers["location"], 1, "ann", [("ann@example.com", True, True)])
        refused = await attacker.get("/signin/github/callback", params={"state": state, "code": code})
    assert refused.status_code == 400


# --- registration -------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_invite_only_refuses_a_verified_stranger(site: Hosted):
    async with site.browser() as http:
        refused = await github_sign_in(site, http, 7, "stranger", [("stranger@example.com", True, True)])
    assert refused.status_code == 403
    assert "has no account" in refused.text
    assert site.store.account("stranger@example.com") is None


@pytest.mark.anyio
async def test_open_registration_makes_an_account_for_a_verified_email(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path, registration="open") as site, site.browser() as http:
        landed = await github_sign_in(site, http, 7, "newcomer", [("new@example.com", True, True)])
        unverified = await github_sign_in(site, http, 8, "ghost", [("ghost@example.com", False, True)])
    assert landed.status_code == 303
    account = site.store.account("new@example.com")
    assert account is not None and account.active and account.status_source == "registration"
    assert unverified.status_code == 403
    assert site.store.account("ghost@example.com") is None


# --- one account, several ways in ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_verified_email_joins_a_new_identity_to_the_existing_account(site: Hosted):
    http = await site.member("ann@example.com", sub="oidc-ann")
    async with http:
        await http.post("/logout", data={"csrf": csrf_of((await http.get("/account/settings")).text)})
    async with site.browser() as http:
        landed = await github_sign_in(
            site, http, 99, "ann", [("other@example.com", True, True), ("ann@example.com", True, False)]
        )
        via_email = await email_sign_in(site, http, "ann@example.com")
    assert landed.status_code == 303 and via_email.status_code == 303
    assert len(site.store.accounts()) == 1
    assert [method for method, _ in ways(site, "ann@example.com")] == ["oidc", "github", "email"]


@pytest.mark.anyio
async def test_a_signed_in_account_links_github_with_another_email(site: Hosted):
    http = await site.member("ann@example.com")
    async with http:
        page = await http.get("/account/settings")
        started = await http.post("/signin/github", data={"csrf": csrf_of(page.text)})
        state, code = site.github.approve(started.headers["location"], 5, "ann", [("ann@work.example", True, True)])
        linked = await http.get("/signin/github/callback", params={"state": state, "code": code})
        forged = await http.post("/signin/github", data={"csrf": "wrong"})
    assert linked.status_code == 303 and linked.headers["location"] == "/account/settings"
    assert ("github", "ann") in ways(site, "ann@example.com")
    assert forged.status_code == 403


@pytest.mark.anyio
async def test_linking_an_identity_that_opens_another_account_is_refused(site: Hosted):
    bo = await site.member("bo@example.com")
    async with bo:
        await github_sign_in(site, bo, 5, "bo", [("bo@example.com", True, True)])
    ann = await site.member("ann@example.com")
    async with ann:
        page = await ann.get("/account/settings")
        started = await ann.post("/signin/github", data={"csrf": csrf_of(page.text)})
        state, code = site.github.approve(started.headers["location"], 5, "bo", [("bo@example.com", True, True)])
        refused = await ann.get("/signin/github/callback", params={"state": state, "code": code})
    assert refused.status_code == 409
    assert ("github", "bo") not in ways(site, "ann@example.com")


@pytest.mark.anyio
async def test_the_last_way_in_cannot_be_removed(site: Hosted):
    http = await site.member("ann@example.com", sub="oidc-ann")
    async with http:
        page = await http.get("/account/settings")
        assert "Ways in" in page.text and ">Remove</button>" not in page.text.split("Ways in")[1].split("<h2>")[0]
        last = await http.post(
            "/account/identities/remove", data={"csrf": csrf_of(page.text), "method": "oidc", "subject": "oidc-ann"}
        )
        await github_sign_in(site, http, 5, "ann", [("ann@example.com", True, True)])
        page = await http.get("/account/settings")
        removed = await http.post(
            "/account/identities/remove", data={"csrf": csrf_of(page.text), "method": "oidc", "subject": "oidc-ann"}
        )
        again = await http.post(
            "/account/identities/remove", data={"csrf": csrf_of(page.text), "method": "github", "subject": "5"}
        )
    assert last.status_code == 400 and "last way in" in last.text
    assert removed.status_code == 303
    assert again.status_code == 400
    assert ways(site, "ann@example.com") == [("github", "ann")]


# --- passkeys -----------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_passkey_added_on_the_account_page_signs_in_on_its_own(site: Hosted):
    authenticator = Authenticator("localhost", BASE)
    http = await site.member("ann@example.com")
    async with http:
        added = await add_passkey(http, authenticator)
        page = await http.get("/account/settings")
    assert added.status_code == 200, added.text
    assert added.json()["redirect"] == "/account/settings"
    assert "Passkey" in page.text and "laptop" in page.text
    async with site.browser() as fresh:
        signed_in = await passkey_sign_in(fresh, authenticator)
        account_page = await fresh.get("/account/settings")
        again = await passkey_sign_in(fresh, authenticator)
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["redirect"] == "/account"
    assert account_page.status_code == 200 and "ann@example.com" in account_page.text
    assert again.status_code == 200, "the signature counter moves forward"


@pytest.mark.anyio
async def test_a_passkey_asserted_for_another_origin_is_refused(site: Hosted):
    authenticator = Authenticator("localhost", BASE)
    http = await site.member("ann@example.com")
    async with http:
        await add_passkey(http, authenticator)
    async with site.browser() as fresh:
        refused = await passkey_sign_in(fresh, authenticator, origin="https://evil.example")
        page = await fresh.get("/account/settings")
    assert refused.status_code == 403, WEBAUTHN
    assert page.status_code == 303


@pytest.mark.anyio
async def test_an_unknown_passkey_and_a_replayed_ceremony_are_refused(site: Hosted):
    stranger = Authenticator("localhost", BASE)
    async with site.browser() as http:
        unknown = await passkey_sign_in(http, stranger)
    authenticator = Authenticator("localhost", BASE)
    member = await site.member("ann@example.com")
    async with member:
        await add_passkey(member, authenticator)
    async with site.browser() as http:
        begun = (await http.post("/signin/passkey/options", json={})).json()
        credential = authenticator.get(begun["publicKey"])
        body = {"ceremony": begun["ceremony"], "credential": credential}
        first = await http.post("/signin/passkey", json=body)
        replay = await http.post("/signin/passkey", json=body)
    assert unknown.status_code == 403
    assert first.status_code == 200
    assert replay.status_code == 400


@pytest.mark.anyio
async def test_adding_a_passkey_needs_the_session_and_its_csrf(site: Hosted):
    authenticator = Authenticator("localhost", BASE)
    http = await site.member("ann@example.com")
    async with http, site.browser() as stranger:
        forged = await http.post("/account/passkeys/options", json={"csrf": "wrong"})
        anonymous = await stranger.post("/account/passkeys/options", json={"csrf": "x"})
        as_form = await http.post(
            "/account/passkeys/options", data={"csrf": csrf_of((await http.get("/account/settings")).text)}
        )
        added = await add_passkey(http, authenticator)
        duplicate = await add_passkey(http, authenticator)
    assert forged.status_code == 403 and anonymous.status_code == 403
    assert as_form.status_code == 403, "JSON only, so a cross-site form cannot start it"
    assert added.status_code == 200
    assert duplicate.status_code == 409, "a credential is registered once"


@pytest.mark.anyio
async def test_removing_a_passkey_leaves_the_other_way_in(site: Hosted):
    authenticator = Authenticator("localhost", BASE)
    http = await site.member("ann@example.com")
    async with http:
        await add_passkey(http, authenticator)
        page = await http.get("/account/settings")
        passkey_id = next(
            way.subject
            for way in site.identities.ways_in(site.store.account("ann@example.com").id)
            if way.method == "passkey"
        )
        removed = await http.post(
            "/account/identities/remove", data={"csrf": csrf_of(page.text), "method": "passkey", "subject": passkey_id}
        )
    assert removed.status_code == 303
    async with site.browser() as fresh:
        refused = await passkey_sign_in(fresh, authenticator)
    assert refused.status_code == 403


# --- forwarded identity -------------------------------------------------------------------------------------


def forwarded(proxy: Proxy) -> ForwardedConfig:
    return ForwardedConfig(public_key=proxy.pem, issuer=ISSUER, typ=TYP, name="Acme sign-in")


@pytest.mark.anyio
async def test_a_forwarded_identity_reaches_the_vault_through_a_configured_proxy(
    vaults: SubjectVaults, tmp_path: Path, proxy: Proxy
):
    async with hosted(vaults, tmp_path, forwarded=forwarded(proxy)) as site, site.browser() as http:
        site.invite("user1042@example.com")
        headers = {"X-Forwarded-Identity": proxy.token("1042")}
        mcp = await http.post("/mcp", headers={**headers, **mcp_headers()}, content=json.dumps(INITIALIZE))
        page = await http.get("/signin", params={"next": "/account"}, headers=headers)
        account_page = await http.get("/account/settings")
    assert mcp.status_code == 200, mcp.text
    assert page.status_code == 303 and page.headers["location"] == "/account"
    assert account_page.status_code == 200 and "user1042@example.com" in account_page.text
    assert ways(site, "user1042@example.com") == [("forwarded", "user1042@example.com")]


@pytest.mark.anyio
async def test_a_forwarded_identity_names_one_of_its_vaults_with_the_vault_parameter(
    vaults: SubjectVaults, tmp_path: Path, proxy: Proxy
):
    async with hosted(vaults, tmp_path, forwarded=forwarded(proxy)) as site, site.browser() as http:
        site.invite("user1042@example.com")
        site.store.create_vault(site.store.account("user1042@example.com").id, "Workshop")
        headers = {"X-Forwarded-Identity": proxy.token("1042"), **mcp_headers()}
        named = await http.post("/mcp?vault=workshop", headers=headers, content=json.dumps(INITIALIZE))
        unknown = await http.post("/mcp?vault=nowhere", headers=headers, content=json.dumps(INITIALIZE))
    assert named.status_code == 200, named.text
    assert unknown.status_code == 403 and unknown.json()["error"] == "wrong_vault"


@pytest.mark.anyio
async def test_a_forged_or_unentitled_forwarded_identity_is_refused(
    vaults: SubjectVaults, tmp_path: Path, proxy: Proxy
):
    async with hosted(vaults, tmp_path, forwarded=forwarded(proxy)) as site, site.browser() as http:
        site.invite("user1042@example.com")
        forged = await post_with(http, proxy.foreign_token())
        unentitled = await post_with(http, proxy.token("1042", entitlements=()))
        uninvited = await post_with(http, proxy.token("7"))
    assert forged.status_code == 403
    assert unentitled.status_code == 403
    assert uninvited.status_code == 403 and uninvited.json()["error"] == "no_account"


@pytest.mark.anyio
async def test_the_forwarded_header_is_ignored_unless_configured(site: Hosted, proxy: Proxy):
    site.invite("user1042@example.com")
    async with site.browser() as http:
        mcp = await post_with(http, proxy.token("1042"))
        page = await http.get("/signin", headers={"X-Forwarded-Identity": proxy.token("1042")})
        direct = await http.get("/signin/forwarded", headers={"X-Forwarded-Identity": proxy.token("1042")})
    assert mcp.status_code == 401
    assert page.status_code == 200 and "Sign in" in page.text
    assert direct.status_code == 404
    assert site.store.account("user1042@example.com").status == "invited"


def mcp_headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-06-18",
    }


async def post_with(http: httpx2.AsyncClient, identity: str) -> httpx2.Response:
    headers = {**mcp_headers(), "X-Forwarded-Identity": identity}
    return await http.post("/mcp", headers=headers, content=json.dumps(INITIALIZE))


# --- storage ------------------------------------------------------------------------------------------------


def test_an_existing_store_keeps_its_oidc_subjects_as_identities(tmp_path: Path):
    store = HostedStore(tmp_path)
    account, _ = store.invite("ann@example.com")
    with store.connect() as connection:
        connection.execute("UPDATE accounts SET sub = 'pocket-sub', status = 'active' WHERE id = ?", (account.id,))
    identities = Identities(store)
    assert identities.account_for("oidc", "pocket-sub").id == account.id
    signed_in = identities.sign_in(Proof("oidc", "pocket-sub", ("ann@example.com",)), "invite").account
    assert signed_in is not None and signed_in.sub == "pocket-sub", "the vault folder stays where it was"


def test_a_suspended_account_gains_no_identity(tmp_path: Path):
    store = HostedStore(tmp_path)
    store.invite("ann@example.com")
    store.set_status("ann@example.com", "suspended")
    identities = Identities(store)
    outcome = identities.sign_in(Proof("github", "1", ("ann@example.com",)), "invite")
    assert outcome.account is not None and outcome.account.status == "suspended"
    assert identities.ways_in(outcome.account.id) == []
