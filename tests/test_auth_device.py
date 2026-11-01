from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2
import pytest
from hosted_support import BASE, IDP, Hosted, call_tool, csrf_of, hosted, text_of

from notes_vault_mcp.auth.device import DEVICE_GRANT, USER_CODE_ALPHABET
from notes_vault_mcp.vault import SubjectVaults

RFC_8628 = "https://www.rfc-editor.org/rfc/rfc8628"


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


async def start(http: httpx2.AsyncClient, **form: str) -> dict[str, Any]:
    response = await http.post("/device_authorization", data={"name": "devbox", **form})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def poll(site: Hosted, http: httpx2.AsyncClient, device: dict[str, Any], wait: bool = True) -> httpx2.Response:
    if wait:
        site.clock.advance(device["interval"])
    return await http.post("/token", data={"grant_type": DEVICE_GRANT, "device_code": device["device_code"]})


async def decide(http: httpx2.AsyncClient, user_code: str, action: str = "approve", access: str = "write"):
    page = await http.get("/device", params={"user_code": user_code})
    assert page.status_code == 200, page.text
    form = {"csrf": csrf_of(page.text), "user_code": user_code, "action": action, "access": access}
    return page, await http.post("/device", data=form)


@pytest.mark.anyio
async def test_device_authorization_returns_codes_and_where_to_go(site: Hosted):
    async with site.browser() as http:
        device = await start(http)
    assert len(device["device_code"]) >= 40
    code = device["user_code"]
    assert len(code) == 9 and code[4] == "-", RFC_8628
    assert all(character in USER_CODE_ALPHABET for character in code.replace("-", ""))
    assert device["verification_uri"] == f"{BASE}/device"
    assert device["verification_uri_complete"] == f"{BASE}/device?user_code={code}"
    assert device["interval"] == 5
    assert device["expires_in"] == 600


@pytest.mark.anyio
async def test_pending_then_approved_hands_out_a_key_once(site: Hosted):
    member = await site.member("ann@example.com")
    async with site.browser() as machine, member:
        device = await start(machine)
        pending = await poll(site, machine, device)
        assert pending.status_code == 400
        assert pending.json()["error"] == "authorization_pending", RFC_8628
        page, approved = await decide(member, device["user_code"])
        assert "devbox" in page.text and device["user_code"] in page.text
        assert approved.status_code == 200 and "devbox" in approved.text
        granted = await poll(site, machine, device)
        assert granted.status_code == 200, granted.text
        body = granted.json()
        again = await poll(site, machine, device)
        written = await call_tool(machine, body["access_token"], "list_files", {})
    assert body["access_token"].startswith("nvm_")
    assert body["token_type"] == "Bearer"
    assert body["scope"] == "vault:read vault:write"
    assert body["key_name"] == "devbox"
    assert body["resource"] == f"{BASE}/mcp"
    assert "expires_in" not in body
    assert again.json()["error"] == "invalid_grant", "a device code is spent by the poll that took the key"
    assert "Welcome.md" in text_of(written)
    account = site.store.account("ann@example.com")
    assert [row["name"] for row in site.store.keys(account.id)] == ["devbox"]


@pytest.mark.anyio
async def test_approving_read_only_gives_a_read_key(site: Hosted):
    member = await site.member("ann@example.com")
    async with site.browser() as machine, member:
        device = await start(machine)
        await decide(member, device["user_code"], access="read")
        granted = (await poll(site, machine, device)).json()
        written = await call_tool(
            machine, granted["access_token"], "write_file", {"path": "Areas/x.md", "content": "x"}
        )
    assert granted["scope"] == "vault:read"
    assert written.get("isError")


@pytest.mark.anyio
async def test_a_read_request_offers_no_write(site: Hosted):
    member = await site.member("ann@example.com")
    async with site.browser() as machine, member:
        device = await start(machine, scope="vault:read")
        page, _ = await decide(member, device["user_code"], access="write")
        granted = (await poll(site, machine, device)).json()
    assert 'value="write"' not in page.text
    assert granted["scope"] == "vault:read"


@pytest.mark.anyio
async def test_denying_answers_access_denied(site: Hosted):
    member = await site.member("ann@example.com")
    async with site.browser() as machine, member:
        device = await start(machine)
        await decide(member, device["user_code"], action="deny")
        denied = await poll(site, machine, device)
    assert denied.json()["error"] == "access_denied", RFC_8628


@pytest.mark.anyio
async def test_an_expired_code_answers_expired_token(site: Hosted):
    member = await site.member("ann@example.com")
    async with site.browser() as machine, member:
        device = await start(machine)
        site.clock.advance(device["expires_in"] + 1)
        page = await member.get("/device", params={"user_code": device["user_code"]})
        expired = await poll(site, machine, device, wait=False)
    assert "devbox" not in page.text, "an expired code is not offered for approval"
    assert expired.json()["error"] == "expired_token", RFC_8628


@pytest.mark.anyio
async def test_polling_too_fast_slows_the_client_down(site: Hosted):
    async with site.browser() as machine:
        device = await start(machine)
        first = await poll(site, machine, device, wait=False)
        fast = await poll(site, machine, device, wait=False)
        site.clock.advance(6)
        still_fast = await poll(site, machine, device, wait=False)
        site.clock.advance(15)
        patient = await poll(site, machine, device, wait=False)
    assert first.json()["error"] == "authorization_pending"
    assert fast.json()["error"] == "slow_down", RFC_8628
    assert still_fast.json()["error"] == "slow_down", "the interval grew by five seconds"
    assert patient.json()["error"] == "authorization_pending"


@pytest.mark.anyio
async def test_the_device_page_signs_in_first_and_returns_with_the_code(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as machine, site.browser() as person:
        device = await start(machine)
        first = await person.get("/device", params={"user_code": device["user_code"].lower()})
        assert first.headers["location"].startswith(f"{IDP}/authorize?")
        state, code = site.idp.approve(first.headers["location"], "sub-ann", "ann@example.com")
        landed = await person.get("/oidc/callback", params={"state": state, "code": code})
    assert landed.status_code == 303
    assert landed.headers["location"] == f"/device?user_code={device['user_code']}"


@pytest.mark.anyio
async def test_a_wrong_code_is_refused_and_guessing_is_limited(site: Hosted):
    member = await site.member("ann@example.com")
    async with member:
        page = await member.get("/device")
        csrf = csrf_of(page.text)
        answers = [
            (
                await member.post("/device", data={"csrf": csrf, "user_code": "BBBB-BBBB", "action": "lookup"})
            ).status_code
            for _ in range(6)
        ]
    assert answers[:5] == [400] * 5
    assert answers[5] == 429


@pytest.mark.anyio
async def test_approval_needs_the_session_csrf(site: Hosted):
    member = await site.member("ann@example.com")
    async with site.browser() as machine, member:
        device = await start(machine)
        forged = await member.post(
            "/device", data={"csrf": "forged", "user_code": device["user_code"], "action": "approve"}
        )
        pending = await poll(site, machine, device)
    assert forged.status_code == 403
    assert pending.json()["error"] == "authorization_pending"


@pytest.mark.anyio
async def test_an_unknown_client_cannot_start_a_device_flow(site: Hosted):
    async with site.browser() as machine:
        refused = await machine.post("/device_authorization", data={"client_id": "made-up"})
        bad_scope = await machine.post("/device_authorization", data={"scope": "admin"})
    assert refused.status_code == 401
    assert bad_scope.json()["error"] == "invalid_scope"


@pytest.mark.anyio
async def test_a_suspended_account_gets_no_key_after_approving(site: Hosted):
    member = await site.member("ann@example.com")
    async with site.browser() as machine, member:
        device = await start(machine)
        await decide(member, device["user_code"])
        site.store.set_status("ann@example.com", "suspended")
        refused = await poll(site, machine, device)
    assert refused.json()["error"] == "access_denied"


def test_the_user_code_alphabet_has_no_vowels_or_lookalikes():
    assert not set(USER_CODE_ALPHABET) & set("AEIOUY01O")
