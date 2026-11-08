from __future__ import annotations

from pathlib import Path

import pytest
from hosted_support import Hosted, hosted, query

from mindin.vault import SubjectVaults

PUBLIC_URL = "http://localhost/vault"


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path, public_url=PUBLIC_URL, issuer=PUBLIC_URL) as running:
        yield running


@pytest.mark.anyio
async def test_the_metadata_sits_at_the_root_well_known_paths(site: Hosted):
    async with site.browser() as http:
        resource = await http.get("/.well-known/oauth-protected-resource/vault/mcp")
        server = await http.get("/.well-known/oauth-authorization-server/vault")
    assert resource.json()["resource"] == f"{PUBLIC_URL}/mcp"
    assert resource.json()["authorization_servers"] == [PUBLIC_URL]
    assert server.json()["authorization_endpoint"] == f"{PUBLIC_URL}/authorize"


@pytest.mark.anyio
async def test_the_mcp_endpoint_under_the_prefix_asks_for_a_token(site: Hosted):
    async with site.browser() as http:
        response = await http.post("/vault/mcp", json={})
        outside = await http.post("/mcp", json={})
    assert response.status_code == 401
    assert "/.well-known/oauth-protected-resource/vault/mcp" in response.headers["www-authenticate"]
    assert outside.status_code == 404


@pytest.mark.anyio
async def test_signing_in_stays_under_the_prefix(site: Hosted):
    site.invite("ann@example.com")
    async with site.browser() as http:
        first = await http.get("/vault/account")
        page = await http.get(first.headers["location"])
        email = await http.post("/vault/signin/email", data={"email": "ann@example.com", "next": "/account"})
        token = query(site.smtp.last_link("ann@example.com"))["token"]
        landed = await http.post("/vault/signin/email/confirm", data={"token": token})
        account = await http.get(landed.headers["location"])
    assert first.headers["location"] == "/vault/signin?next=%2Fvault%2Faccount"
    assert 'action="/vault/signin/email"' in page.text
    assert email.status_code == 200
    assert site.smtp.last_link("ann@example.com").startswith(f"{PUBLIC_URL}/signin/email/confirm?token=")
    assert landed.headers["location"] == "/vault/account", "a next outside the prefix falls back to the account"
    assert account.status_code == 200
    assert "Path=/vault" in landed.headers["set-cookie"]
