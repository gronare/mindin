from __future__ import annotations

from typing import Any

import pytest
from cimd_support import MetadataHost

from mindin.auth.cimd import (
    MAX_DOCUMENT_BYTES,
    ClientMetadataError,
    ClientMetadataFetcher,
    allowed_redirect,
    public_address,
    same_redirect,
)

MCP_CIMD = "https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration"
MCP_SECURITY = "https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations"
CLAUDE_AUTH = "https://claude.com/docs/connectors/building/authentication"
URL = "https://app.example.com/oauth/client.json"


def document(**overrides: Any) -> dict[str, Any]:
    return {
        "client_id": URL,
        "client_name": "Example MCP Client",
        "redirect_uris": ["http://127.0.0.1:3000/callback", "https://app.example.com/callback"],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        **overrides,
    }


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def fetcher(host: MetadataHost, clock: Clock | None = None) -> ClientMetadataFetcher:
    return ClientMetadataFetcher(host.transport(), host.resolve, clock or Clock())


@pytest.mark.anyio
async def test_a_valid_document_becomes_a_public_client():
    host = MetadataHost()
    host.serve(URL, document())
    client = await fetcher(host).client(URL)
    assert client.client_id == URL
    assert client.client_name == "Example MCP Client"
    assert client.token_endpoint_auth_method == "none"
    assert client.client_secret is None
    assert "refresh_token" in client.grant_types
    assert client.scope == "vault:read vault:write"


@pytest.mark.anyio
async def test_grants_this_server_lacks_are_left_out_of_the_client():
    host = MetadataHost()
    host.serve(
        URL,
        document(grant_types=["authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:jwt-bearer"]),
    )
    client = await fetcher(host).client(URL)
    assert client.grant_types == ["authorization_code", "refresh_token"]


@pytest.mark.anyio
async def test_a_mismatched_client_id_is_refused():
    host = MetadataHost()
    host.serve(URL, document(client_id="https://app.example.com/oauth/other.json"))
    with pytest.raises(ClientMetadataError, match="client_id"):
        await fetcher(host).client(URL)
    assert MCP_CIMD


@pytest.mark.parametrize("host_name", ["intranet.example.test", "metadata.example.test"])
@pytest.mark.anyio
async def test_a_url_that_resolves_to_a_private_address_is_never_fetched(host_name: str):
    host = MetadataHost()
    url = f"https://{host_name}/client.json"
    host.serve(url, document(client_id=url))
    with pytest.raises(ClientMetadataError, match="will not fetch"):
        await fetcher(host).client(url)
    assert host.fetched == [], MCP_SECURITY


@pytest.mark.anyio
async def test_an_oversize_document_is_refused():
    host = MetadataHost()
    host.serve(URL, document(client_name="x" * (MAX_DOCUMENT_BYTES + 1)))
    with pytest.raises(ClientMetadataError, match="larger than"):
        await fetcher(host).client(URL)


@pytest.mark.parametrize(
    "url",
    [
        "http://app.example.com/client.json",
        "https://app.example.com",
        "https://app.example.com/",
        "https://user:pw@app.example.com/client.json",
        "https://app.example.com/client.json#frag",
        "https://app.example.com/a/../client.json",
    ],
)
@pytest.mark.anyio
async def test_a_client_id_url_must_be_https_with_a_plain_path(url: str):
    with pytest.raises(ClientMetadataError):
        await fetcher(MetadataHost()).client(url)


@pytest.mark.parametrize(
    "overrides",
    [
        {"redirect_uris": []},
        {"redirect_uris": ["http://evil.example.com/cb"]},
        {"redirect_uris": ["javascript:alert(1)"]},
        {"token_endpoint_auth_method": "client_secret_post"},
        {"token_endpoint_auth_method": "private_key_jwt"},
        {"client_secret": "x"},
        {"grant_types": ["client_credentials"]},
        {"grant_types": ["refresh_token"]},
    ],
)
@pytest.mark.anyio
async def test_a_document_outside_what_this_server_offers_is_refused(overrides: dict[str, Any]):
    host = MetadataHost()
    host.serve(URL, document(**overrides))
    with pytest.raises(ClientMetadataError):
        await fetcher(host).client(URL)


@pytest.mark.anyio
async def test_a_redirect_answer_is_not_followed():
    host = MetadataHost()
    host.serve(URL, b"", status=302, headers={"location": "http://169.254.169.254/latest"})
    with pytest.raises(ClientMetadataError, match="302"):
        await fetcher(host).client(URL)


@pytest.mark.anyio
async def test_the_document_is_cached_for_its_max_age():
    host = MetadataHost()
    host.serve(URL, document(), headers={"cache-control": "max-age=120"})
    clock = Clock()
    fetching = fetcher(host, clock)
    await fetching.client(URL)
    await fetching.client(URL)
    clock.now += 121
    await fetching.client(URL)
    assert host.fetched == [URL, URL]


@pytest.mark.anyio
async def test_no_store_is_respected():
    host = MetadataHost()
    host.serve(URL, document(), headers={"cache-control": "no-store"})
    fetching = fetcher(host)
    await fetching.client(URL)
    await fetching.client(URL)
    assert host.fetched == [URL, URL]


@pytest.mark.anyio
async def test_fetches_are_throttled():
    host = MetadataHost()
    host.serve(URL, document(), headers={"cache-control": "no-store"})
    fetching = fetcher(host)
    for _ in range(30):
        await fetching.client(URL)
    with pytest.raises(ClientMetadataError, match="too many"):
        await fetching.client(URL)


def test_loopback_redirects_match_on_any_port_and_nothing_else():
    assert same_redirect("http://localhost/callback", "http://localhost:3118/callback"), CLAUDE_AUTH
    assert same_redirect("http://127.0.0.1/callback", "http://127.0.0.1:53123/callback")
    assert not same_redirect("http://localhost/callback", "http://127.0.0.1:3118/callback")
    assert not same_redirect("http://localhost/callback", "http://localhost:3118/other")
    assert not same_redirect("https://app.example.com/cb", "https://app.example.com:8443/cb")
    assert not same_redirect("http://localhost.evil.com/cb", "http://localhost.evil.com:1/cb")


def test_redirects_are_https_loopback_or_an_app_scheme():
    assert allowed_redirect("https://claude.ai/api/mcp/auth_callback")
    assert allowed_redirect("http://127.0.0.1:9000/cb")
    assert allowed_redirect("cursor://anysphere.cursor-retrieval/oauth/user-mcp/callback")
    assert not allowed_redirect("http://example.com/cb")
    assert not allowed_redirect("javascript:alert(1)")
    assert not allowed_redirect("data:text/html,x")


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "0.0.0.0",
        "224.0.0.1",
    ],
)
def test_private_and_special_addresses_are_not_public(address: str):
    assert not public_address(address)


def test_a_global_address_is_public():
    assert public_address("93.184.216.34")
    assert public_address("2606:2800:220:1:248:1893:25c8:1946")
