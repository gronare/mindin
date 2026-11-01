from __future__ import annotations

import base64
import hashlib
import hmac
from pathlib import Path
from typing import Any

import pytest
from hosted_support import NOTE_BODY, Hosted, call_tool, hosted, post_mcp, query, text_of

from notes_vault_mcp.auth import auth_config
from notes_vault_mcp.auth.billing import HANDOFF_DOMAIN, HANDOFF_SECONDS, handoff_link, handoff_secret, signature
from notes_vault_mcp.cli import main
from notes_vault_mcp.config import ConfigError
from notes_vault_mcp.vault import SubjectVaults

HUB_VECTOR = {
    "domain": "kebbie-billing-handoff-v1",
    "secret": "aGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGhoaGg",
    "account": "acc_onexxxxxxxxxxxxxxxxxxx",
    "expires": 1761471000,
    "checkout": "9LxkSrg_5eB2niUJwUsk01CQ_DFfYcQeqSMZbqYDotU",
    "portal": "rE1pvKwyedjZGvF3N_OKihW1SdU3rpjV4itAhOl55e0",
}
SECRET = base64.urlsafe_b64encode(b"v" * 32).decode().rstrip("=")
BILLING = "https://billing.example.com"
DAY = 86400


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(
        vaults, tmp_path, plans=True, plan_read_only_days=30, billing_url=BILLING, billing_handoff_secret=SECRET
    ) as running:
        yield running


@pytest.fixture
async def plans_off(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


def admin_headers(site: Hosted, scope: str = "billing") -> dict[str, str]:
    return {"Authorization": f"Bearer {site.store.create_admin_token('billing-' + scope, scope)}"}


async def set_plan(site: Hosted, http: Any, account: str, status: str, until: int | None) -> Any:
    return await http.put(
        f"/v1/admin/accounts/{account}/plan", json={"status": status, "until": until}, headers=admin_headers(site)
    )


# --- the handoff signature ----------------------------------------------------------------------------------


@pytest.mark.parametrize("purpose", ["checkout", "portal"])
def test_the_signature_has_the_hubs_shape(purpose: str):
    secret = handoff_secret(HUB_VECTOR["secret"])
    made = signature(secret, purpose, HUB_VECTOR["account"], HUB_VECTOR["expires"], domain=HUB_VECTOR["domain"])
    assert made == HUB_VECTOR[purpose]


def test_the_vault_signs_under_its_own_domain():
    secret = handoff_secret(SECRET)
    message = HANDOFF_DOMAIN.encode() + b"checkout\0acc_x\0" + b"1761471000"
    expected = base64.urlsafe_b64encode(hmac.new(secret, message, hashlib.sha256).digest()).decode().rstrip("=")
    assert HANDOFF_DOMAIN == "vault-billing-handoff-v1"
    assert signature(secret, "checkout", "acc_x", 1761471000) == expected
    link = handoff_link(BILLING, secret, "portal", "acc_x", 1761471000)
    assert link.startswith(f"{BILLING}/billing_portal?account=acc_x&expires=1761471000&signature=")


@pytest.mark.parametrize("raw", ["not base64!", base64.urlsafe_b64encode(b"short").decode()])
def test_a_handoff_secret_must_be_32_bytes_of_base64url(raw: str):
    with pytest.raises(ConfigError):
        handoff_secret(raw)


def test_billing_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com")
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path))
    monkeypatch.setenv("VAULT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("VAULT_SMTP_FROM", "vault@example.com")
    monkeypatch.setenv("VAULT_BILLING_URL", BILLING + "/")
    monkeypatch.setenv("VAULT_BILLING_HANDOFF_SECRET", SECRET)
    monkeypatch.setenv("VAULT_PLAN_READ_ONLY_DAYS", "14")
    config = auth_config()
    assert config.plans is False
    assert config.billing_url == BILLING
    assert config.plan_read_only_days == 14
    assert SECRET not in repr(config)


# --- handoff links from /account ----------------------------------------------------------------------------


@pytest.mark.parametrize(("purpose", "path"), [("checkout", "checkout"), ("portal", "billing_portal")])
@pytest.mark.anyio
async def test_the_account_page_hands_off_to_billing_with_a_signed_link(site: Hosted, purpose: str, path: str):
    http = await site.member("ann@example.com")
    account = site.store.account("ann@example.com")
    async with http:
        response = await http.get(f"/account/billing/{purpose}")
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    location = response.headers["location"]
    assert location.startswith(f"{BILLING}/{path}?")
    params = query(location)
    assert params["account"] == account.id
    assert int(params["expires"]) == int(site.clock()) + HANDOFF_SECONDS
    assert params["signature"] == signature(handoff_secret(SECRET), purpose, account.id, int(params["expires"]))


@pytest.mark.anyio
async def test_a_handoff_signs_in_first(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as http:
        first = await http.get("/account/billing/checkout")
    assert first.status_code == 303
    assert first.headers["location"] == "/signin?next=%2Faccount%2Fbilling%2Fcheckout"


@pytest.mark.anyio
async def test_without_a_billing_url_there_is_no_handoff(plans_off: Hosted):
    http = await plans_off.member("ann@example.com")
    async with http:
        missing = await http.get("/account/billing/checkout")
        page = await http.get("/account")
    assert missing.status_code == 404
    assert "/account/billing/" not in page.text


# --- the admin api ------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_admin_api_needs_a_billing_token(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as http:
        anonymous = await http.get("/v1/admin/accounts")
        forged = await http.get("/v1/admin/accounts", headers={"Authorization": "Bearer nva_0123456789abforgedforged"})
        key = site.key_for("ann@example.com")
        with_key = await http.get("/v1/admin/accounts", headers={"Authorization": f"Bearer {key}"})
        other_scope = await http.get("/v1/admin/accounts", headers=admin_headers(site, scope="support"))
    assert (anonymous.status_code, anonymous.json()) == (401, {"error": "unauthorized"})
    assert forged.status_code == 401
    assert with_key.status_code == 401
    assert (other_scope.status_code, other_scope.json()) == (403, {"error": "wrong-scope"})


@pytest.mark.anyio
async def test_set_plan_answers_the_account_plan(site: Hosted):
    site.invite("ann@example.com")
    account = site.store.account("ann@example.com")
    until = int(site.clock()) + 30 * DAY
    async with site.browser() as http:
        answer = await set_plan(site, http, account.id, "active", until)
        unknown = await set_plan(site, http, "acc_nobody", "active", until)
        too_far = await set_plan(site, http, account.id, "active", 2**60)
        bad = await http.put(
            f"/v1/admin/accounts/{account.id}/plan", json={"status": "gold"}, headers=admin_headers(site)
        )
    assert answer.status_code == 200
    assert answer.json() == {
        "account": account.id,
        "email": "ann@example.com",
        "plan": {"status": "active", "until": until, "entitled": True},
    }
    assert (unknown.status_code, unknown.json()) == (404, {"error": "not-found"})
    assert (too_far.status_code, too_far.json()) == (400, {"error": "until-out-of-range"})
    assert bad.status_code == 400


@pytest.mark.anyio
async def test_the_list_pages_through_accounts_oldest_first(site: Hosted):
    for name in ("ann", "bo", "cy"):
        site.invite(f"{name}@example.com")
    headers = admin_headers(site)
    async with site.browser() as http:
        first = (await http.get("/v1/admin/accounts", params={"limit": 2}, headers=headers)).json()
        second = await http.get("/v1/admin/accounts", params={"limit": 2, "after": first["next"]}, headers=headers)
        one = await http.get(f"/v1/admin/accounts/{first['accounts'][0]['account']}", headers=headers)
        out_of_range = await http.get("/v1/admin/accounts", params={"limit": 501}, headers=headers)
    assert [row["email"] for row in first["accounts"]] == ["ann@example.com", "bo@example.com"]
    assert first["next"] == first["accounts"][1]["account"]
    assert [row["email"] for row in second.json()["accounts"]] == ["cy@example.com"]
    assert second.json()["next"] is None
    row = one.json()
    assert row["account"].startswith("acc_") and len(row["account"]) == 26
    assert set(row) == {"account", "email", "status", "created_at", "deactivated_at", "plan", "keys", "last_seen"}
    assert row["plan"] == {"status": "active", "until": None, "entitled": True}
    assert out_of_range.status_code == 400


@pytest.mark.anyio
async def test_deactivate_and_activate_drive_the_account_status(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com")
    account = site.store.account("ann@example.com")
    headers = admin_headers(site)
    async with http:
        off = await http.post(f"/v1/admin/accounts/{account.id}/deactivate", headers=headers)
        refused = await post_mcp(http, key, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        listed = (await http.get(f"/v1/admin/accounts/{account.id}", headers=headers)).json()
        on = await http.post(f"/v1/admin/accounts/{account.id}/activate", headers=headers)
        missing = await http.post("/v1/admin/accounts/acc_nobody/activate", headers=headers)
        listing = await call_tool(http, key, "list_files", {})
    assert off.status_code == 204 and on.status_code == 204
    assert refused.status_code == 403
    assert listed["status"] == "suspended" and listed["deactivated_at"] is not None
    assert site.store.account(account.id).status_source == "billing"
    assert missing.status_code == 404
    assert not listing.get("isError")


# --- the plan decides each request --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_current_plan_reads_and_writes(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com")
    account = site.store.account("ann@example.com")
    async with http:
        await set_plan(site, http, account.id, "active", int(site.clock()) + DAY)
        written = await call_tool(http, key, "write_file", {"path": "Areas/a.md", "content": NOTE_BODY})
    assert not written.get("isError"), text_of(written)


@pytest.mark.anyio
async def test_past_until_the_vault_is_read_only_then_refused(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com")
    account = site.store.account("ann@example.com")
    async with http:
        await set_plan(site, http, account.id, "active", int(site.clock()) + DAY)
        await call_tool(http, key, "write_file", {"path": "Areas/a.md", "content": NOTE_BODY})
        site.clock.advance(2 * DAY)
        read = await call_tool(http, key, "read_file", {"path": "Areas/a.md"})
        written = await call_tool(http, key, "write_file", {"path": "Areas/b.md", "content": NOTE_BODY})
        exported = await http.get("/dav/vault/Areas/a.md", headers=basic(key))
        put = await http.put("/dav/vault/Areas/c.md", content=b"x", headers=basic(key))
        page = await http.get("/account")
        site.clock.advance(30 * DAY)
        refused = await post_mcp(http, key, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        dav_refused = await http.get("/dav/vault/Areas/a.md", headers=basic(key))
        still_signs_in = await http.get("/account")
    assert "Hemligt" in text_of(read)
    assert written.get("isError") and "may only read" in text_of(written)
    assert exported.status_code == 200
    assert put.status_code == 403
    assert "readable and exportable" in page.text and "/account/billing/checkout" in page.text
    assert (refused.status_code, refused.json()["error"]) == (403, "plan-inactive")
    assert dav_refused.status_code == 401
    assert still_signs_in.status_code == 200 and "renewed" in still_signs_in.text


@pytest.mark.anyio
async def test_an_ended_subscription_counts_from_when_it_was_set(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com")
    account = site.store.account("ann@example.com")
    async with http:
        await set_plan(site, http, account.id, "inactive", None)
        read_only = await call_tool(http, key, "write_file", {"path": "Areas/b.md", "content": NOTE_BODY})
        site.clock.advance(31 * DAY)
        refused = await post_mcp(http, key, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert "may only read" in text_of(read_only)
    assert refused.status_code == 403


@pytest.mark.anyio
async def test_with_plans_off_the_plan_is_ignored(plans_off: Hosted):
    http = await plans_off.member("ann@example.com")
    key = plans_off.key_for("ann@example.com")
    account = plans_off.store.account("ann@example.com")
    plans_off.store.set_plan(account.id, "inactive", 1, 1)
    async with http:
        written = await call_tool(http, key, "write_file", {"path": "Areas/a.md", "content": NOTE_BODY})
        page = await http.get("/account")
    assert not written.get("isError"), text_of(written)
    assert "<h2>Plan</h2>" not in page.text


def basic(secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"ann@example.com:{secret}".encode()).decode()}


# --- the admin token command --------------------------------------------------------------------------------


def test_the_admin_command_mints_and_revokes_a_billing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path))
    assert main(["admin", "token", "--name", "billing", "--scope", "billing"]) == 0
    token = capsys.readouterr().out.strip()
    from notes_vault_mcp.auth.hosted_store import HostedStore

    store = HostedStore(tmp_path)
    assert token.startswith("nva_")
    assert store.admin_token_scope(token) == "billing"
    assert main(["admin", "revoke", "billing"]) == 0
    assert store.admin_token_scope(token) is None
    assert main(["admin", "revoke", "billing"]) == 1


def test_serve_takes_plans_off_by_default():
    from notes_vault_mcp.cli import build_parser

    assert build_parser().parse_args(["serve"]).plans == "off"
    assert build_parser().parse_args(["serve", "--plans", "on"]).plans == "on"
