from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from hosted_support import hosted_config, key_for_subject

from notes_vault_mcp.auth.hosted import build_hosted_app
from notes_vault_mcp.auth.hosted_keys import Keyring, MasterKey
from notes_vault_mcp.vault import SubjectVaults

PROPFIND_ALL = '<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:allprop/></D:propfind>'


def basic(secret: str, user: str = "user@example.com") -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode("ascii")}


@asynccontextmanager
async def dav_client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            yield client


@pytest.fixture
def keyring() -> Keyring:
    return Keyring(MasterKey.generate())


@pytest.fixture
def app(vaults: SubjectVaults, tmp_path: Path, keyring: Keyring) -> Any:
    return build_hosted_app(vaults, hosted_config(tmp_path), keyring=keyring)


@pytest.fixture
def secret(app: Any, vaults: SubjectVaults, tmp_path: Path) -> str:
    key = key_for_subject(tmp_path / "auth", "user1042@example.com", "1042")
    vaults.for_subject("1042")
    return key


def hrefs(response: httpx.Response) -> list[str]:
    return [part.split("</D:href>")[0] for part in response.text.split("<D:href>")[1:]]


@pytest.mark.anyio
async def test_dav_without_credentials_asks_for_basic_auth(app: Any):
    async with dav_client(app) as client:
        response = await client.request("PROPFIND", "/dav/", headers={"Depth": "1"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


@pytest.mark.anyio
async def test_dav_refuses_a_wrong_token(app: Any, secret: str):
    async with dav_client(app) as client:
        response = await client.request("PROPFIND", "/dav/", headers={"Depth": "1", **basic("not-" + secret)})
    assert response.status_code == 401


@pytest.mark.anyio
async def test_options_advertises_webdav(app: Any, secret: str):
    async with dav_client(app) as client:
        response = await client.options("/dav/", headers=basic(secret))
    assert response.status_code == 200
    assert response.headers["dav"] == "1"
    assert "PROPFIND" in response.headers["allow"] and "MKCOL" in response.headers["allow"]


@pytest.mark.anyio
async def test_propfind_lists_the_provisioned_vault(app: Any, secret: str):
    async with dav_client(app) as client:
        response = await client.request(
            "PROPFIND", "/dav/", content=PROPFIND_ALL, headers={"Depth": "1", **basic(secret)}
        )
    assert response.status_code == 207
    assert response.headers["content-type"].startswith("application/xml")
    listed = hrefs(response)
    assert listed[0] == "/dav/"
    assert "/dav/Welcome.md" in listed
    assert "/dav/Areas.base" in listed
    assert "/dav/.vault/" in listed
    assert "/dav/.vault/schema.yml" not in listed


@pytest.mark.anyio
async def test_propfind_depth_zero_describes_one_file(app: Any, secret: str):
    async with dav_client(app) as client:
        response = await client.request("PROPFIND", "/dav/Welcome.md", headers={"Depth": "0", **basic(secret)})
    assert response.status_code == 207
    assert hrefs(response) == ["/dav/Welcome.md"]
    assert "<D:getcontentlength>" in response.text
    assert "<D:getetag>" in response.text
    assert "<D:collection/>" not in response.text


@pytest.mark.anyio
async def test_propfind_on_a_missing_path_is_404(app: Any, secret: str):
    async with dav_client(app) as client:
        response = await client.request("PROPFIND", "/dav/Nowhere/", headers={"Depth": "1", **basic(secret)})
    assert response.status_code == 404


@pytest.mark.anyio
async def test_put_get_and_overwrite(app: Any, secret: str):
    async with dav_client(app) as client:
        created = await client.put("/dav/Areas/dav.md", content=b"# from obsidian\n", headers=basic(secret))
        assert created.status_code == 201
        assert created.headers["etag"].startswith('"')
        fetched = await client.get("/dav/Areas/dav.md", headers=basic(secret))
        assert fetched.status_code == 200
        assert fetched.content == b"# from obsidian\n"
        assert fetched.headers["content-type"].startswith("text/markdown")
        assert fetched.headers["etag"] == created.headers["etag"]
        again = await client.put("/dav/Areas/dav.md", content=b"# edited\n", headers=basic(secret))
        assert again.status_code == 204
        head = await client.head("/dav/Areas/dav.md", headers=basic(secret))
        assert head.status_code == 200 and head.content == b""


@pytest.mark.anyio
async def test_the_vault_stores_no_file_under_its_name(app: Any, secret: str, storage: Path):
    async with dav_client(app) as client:
        await client.put("/dav/Areas/dav.md", content=b"# from obsidian\n", headers=basic(secret))
    names = [path.relative_to(storage).as_posix() for path in storage.rglob("*")]
    assert names and not any("Areas" in name or "dav" in name for name in names)
    assert not any(b"obsidian" in path.read_bytes() for path in storage.rglob("*") if path.is_file())


@pytest.mark.anyio
async def test_binary_attachments_round_trip(app: Any, secret: str):
    payload = bytes(range(256))
    async with dav_client(app) as client:
        await client.request("MKCOL", "/dav/Attachments/", headers=basic(secret))
        put = await client.put("/dav/Attachments/pic.png", content=payload, headers=basic(secret))
        assert put.status_code == 201
        got = await client.get("/dav/Attachments/pic.png", headers=basic(secret))
    assert got.content == payload
    assert got.headers["content-type"] == "image/png"


@pytest.mark.anyio
async def test_mkcol_creates_a_collection_that_propfind_shows(app: Any, secret: str):
    async with dav_client(app) as client:
        created = await client.request("MKCOL", "/dav/Log/", headers=basic(secret))
        assert created.status_code == 201
        again = await client.request("MKCOL", "/dav/Log/", headers=basic(secret))
        assert again.status_code == 405
        listing = await client.request("PROPFIND", "/dav/", headers={"Depth": "1", **basic(secret)})
        detail = await client.request("PROPFIND", "/dav/Log/", headers={"Depth": "0", **basic(secret)})
    assert "/dav/Log/" in hrefs(listing)
    assert "<D:collection/>" in detail.text


@pytest.mark.anyio
async def test_move_renames_a_file_and_the_old_path_is_gone(app: Any, secret: str):
    async with dav_client(app) as client:
        await client.put("/dav/Projects/a.md", content=b"a", headers=basic(secret))
        moved = await client.request(
            "MOVE", "/dav/Projects/a.md", headers={"Destination": "http://localhost/dav/Projects/b.md", **basic(secret)}
        )
        assert moved.status_code == 201
        assert (await client.get("/dav/Projects/a.md", headers=basic(secret))).status_code == 404
        assert (await client.get("/dav/Projects/b.md", headers=basic(secret))).content == b"a"


@pytest.mark.anyio
async def test_move_refuses_to_overwrite_when_told_not_to(app: Any, secret: str):
    async with dav_client(app) as client:
        await client.put("/dav/Projects/a.md", content=b"a", headers=basic(secret))
        await client.put("/dav/Projects/b.md", content=b"b", headers=basic(secret))
        refused = await client.request(
            "MOVE",
            "/dav/Projects/a.md",
            headers={"Destination": "http://localhost/dav/Projects/b.md", "Overwrite": "F", **basic(secret)},
        )
        assert refused.status_code == 412
        assert (await client.get("/dav/Projects/b.md", headers=basic(secret))).content == b"b"


@pytest.mark.anyio
async def test_move_and_copy_carry_whole_collections(app: Any, secret: str):
    async with dav_client(app) as client:
        await client.put("/dav/Old/one.md", content=b"1", headers=basic(secret))
        await client.put("/dav/Old/deep/two.md", content=b"2", headers=basic(secret))
        copied = await client.request(
            "COPY", "/dav/Old/", headers={"Destination": "http://localhost/dav/Copy/", **basic(secret)}
        )
        assert copied.status_code == 201
        moved = await client.request(
            "MOVE", "/dav/Old/", headers={"Destination": "http://localhost/dav/New/", **basic(secret)}
        )
        assert moved.status_code == 201
        assert (await client.get("/dav/New/deep/two.md", headers=basic(secret))).content == b"2"
        assert (await client.get("/dav/Copy/one.md", headers=basic(secret))).content == b"1"
        assert (
            await client.request("PROPFIND", "/dav/Old/", headers={"Depth": "0", **basic(secret)})
        ).status_code == 404


@pytest.mark.anyio
async def test_delete_removes_a_file_and_a_whole_collection(app: Any, secret: str):
    async with dav_client(app) as client:
        await client.put("/dav/Tmp/one.md", content=b"1", headers=basic(secret))
        await client.put("/dav/Tmp/sub/two.md", content=b"2", headers=basic(secret))
        assert (await client.delete("/dav/Tmp/one.md", headers=basic(secret))).status_code == 204
        assert (await client.get("/dav/Tmp/one.md", headers=basic(secret))).status_code == 404
        assert (await client.delete("/dav/Tmp/", headers=basic(secret))).status_code == 204
        assert (
            await client.request("PROPFIND", "/dav/Tmp/", headers={"Depth": "0", **basic(secret)})
        ).status_code == 404
        assert (await client.delete("/dav/Tmp/", headers=basic(secret))).status_code == 404


@pytest.mark.anyio
async def test_a_path_that_climbs_out_is_refused(app: Any, secret: str):
    async with dav_client(app) as client:
        response = await client.request("PROPFIND", "/dav/%2e%2e/other/", headers={"Depth": "0", **basic(secret)})
    assert response.status_code == 403


@pytest.mark.anyio
async def test_a_second_subjects_token_sees_only_its_own_vault(
    app: Any, secret: str, vaults: SubjectVaults, tmp_path: Path
):
    other = key_for_subject(tmp_path / "auth", "other@example.com", "1187")
    async with dav_client(app) as client:
        await client.put("/dav/Areas/mine.md", content=b"mine", headers=basic(secret))
        assert (await client.get("/dav/Areas/mine.md", headers=basic(other))).status_code == 404
        listing = await client.request("PROPFIND", "/dav/", headers={"Depth": "1", **basic(other)})
    assert "/dav/Welcome.md" in hrefs(listing)
    assert "/dav/Areas/" not in hrefs(listing)


@pytest.mark.anyio
async def test_dav_under_a_public_url_path(tmp_path: Path, secret: str, keyring: Keyring):
    vaults = SubjectVaults(cache=tmp_path / "cache", prefix="users")
    app = build_hosted_app(vaults, hosted_config(tmp_path, public_url="http://localhost/vault"), keyring=keyring)
    async with dav_client(app) as client:
        response = await client.request("PROPFIND", "/vault/dav/", headers={"Depth": "1", **basic(secret)})
    assert response.status_code == 207
    assert hrefs(response)[0] == "/vault/dav/"
    assert "/vault/dav/Welcome.md" in hrefs(response)
