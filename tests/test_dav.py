from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx2 as httpx
import pytest
from hosted_support import first_vault_id, hosted_config, key_for_subject

from mindin.auth.hosted import build_hosted_app
from mindin.auth.hosted_keys import Keyring, MasterKey
from mindin.vault import SubjectVaults

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
    vaults.for_subject(first_vault_id(tmp_path / "auth", "user1042@example.com"))
    return key


@pytest.fixture
def vault_id(secret: str, tmp_path: Path) -> str:
    return first_vault_id(tmp_path / "auth", "user1042@example.com")


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
        response = await client.options("/dav/vault/", headers=basic(secret))
        root = await client.options("/dav/", headers=basic(secret))
    assert response.status_code == 200
    assert response.headers["dav"] == "1"
    assert "PROPFIND" in response.headers["allow"] and "MKCOL" in response.headers["allow"]
    assert root.status_code == 200 and "MKCOL" not in root.headers["allow"]


@pytest.mark.anyio
async def test_propfind_lists_the_provisioned_vault(app: Any, secret: str):
    async with dav_client(app) as client:
        response = await client.request(
            "PROPFIND", "/dav/vault/", content=PROPFIND_ALL, headers={"Depth": "1", **basic(secret)}
        )
    assert response.status_code == 207
    assert response.headers["content-type"].startswith("application/xml")
    listed = hrefs(response)
    assert listed[0] == "/dav/vault/"
    assert "/dav/vault/Welcome.md" in listed
    assert "/dav/vault/Areas.base" in listed
    assert "/dav/vault/.vault/" in listed
    assert "/dav/vault/.vault/schema.yml" not in listed


@pytest.mark.anyio
async def test_propfind_depth_zero_describes_one_file(app: Any, secret: str):
    async with dav_client(app) as client:
        response = await client.request("PROPFIND", "/dav/vault/Welcome.md", headers={"Depth": "0", **basic(secret)})
    assert response.status_code == 207
    assert hrefs(response) == ["/dav/vault/Welcome.md"]
    assert "<D:getcontentlength>" in response.text
    assert "<D:getetag>" in response.text
    assert "<D:collection/>" not in response.text


@pytest.mark.anyio
async def test_propfind_on_a_missing_path_is_404(app: Any, secret: str):
    async with dav_client(app) as client:
        response = await client.request("PROPFIND", "/dav/vault/Nowhere/", headers={"Depth": "1", **basic(secret)})
    assert response.status_code == 404


@pytest.mark.anyio
async def test_put_get_and_overwrite(app: Any, secret: str):
    async with dav_client(app) as client:
        created = await client.put("/dav/vault/Areas/dav.md", content=b"# from obsidian\n", headers=basic(secret))
        assert created.status_code == 201
        assert created.headers["etag"].startswith('"')
        fetched = await client.get("/dav/vault/Areas/dav.md", headers=basic(secret))
        assert fetched.status_code == 200
        assert fetched.content == b"# from obsidian\n"
        assert fetched.headers["content-type"].startswith("text/markdown")
        assert fetched.headers["etag"] == created.headers["etag"]
        again = await client.put("/dav/vault/Areas/dav.md", content=b"# edited\n", headers=basic(secret))
        assert again.status_code == 204
        head = await client.head("/dav/vault/Areas/dav.md", headers=basic(secret))
        assert head.status_code == 200 and head.content == b""


@pytest.mark.anyio
async def test_the_vault_stores_no_file_under_its_name(app: Any, secret: str, storage: Path):
    async with dav_client(app) as client:
        await client.put("/dav/vault/Areas/dav.md", content=b"# from obsidian\n", headers=basic(secret))
    names = [path.relative_to(storage).as_posix() for path in storage.rglob("*")]
    assert names and not any("Areas" in name or "dav" in name for name in names)
    assert not any(b"obsidian" in path.read_bytes() for path in storage.rglob("*") if path.is_file())


@pytest.mark.anyio
async def test_binary_attachments_round_trip(app: Any, secret: str):
    payload = bytes(range(256))
    async with dav_client(app) as client:
        await client.request("MKCOL", "/dav/vault/Attachments/", headers=basic(secret))
        put = await client.put("/dav/vault/Attachments/pic.png", content=payload, headers=basic(secret))
        assert put.status_code == 201
        got = await client.get("/dav/vault/Attachments/pic.png", headers=basic(secret))
    assert got.content == payload
    assert got.headers["content-type"] == "image/png"


@pytest.mark.anyio
async def test_mkcol_creates_a_collection_that_propfind_shows(app: Any, secret: str):
    async with dav_client(app) as client:
        created = await client.request("MKCOL", "/dav/vault/Log/", headers=basic(secret))
        assert created.status_code == 201
        again = await client.request("MKCOL", "/dav/vault/Log/", headers=basic(secret))
        assert again.status_code == 405
        listing = await client.request("PROPFIND", "/dav/vault/", headers={"Depth": "1", **basic(secret)})
        detail = await client.request("PROPFIND", "/dav/vault/Log/", headers={"Depth": "0", **basic(secret)})
    assert "/dav/vault/Log/" in hrefs(listing)
    assert "<D:collection/>" in detail.text


@pytest.mark.anyio
async def test_move_renames_a_file_and_the_old_path_is_gone(app: Any, secret: str):
    async with dav_client(app) as client:
        await client.put("/dav/vault/Projects/a.md", content=b"a", headers=basic(secret))
        moved = await client.request(
            "MOVE",
            "/dav/vault/Projects/a.md",
            headers={"Destination": "http://localhost/dav/vault/Projects/b.md", **basic(secret)},
        )
        assert moved.status_code == 201
        assert (await client.get("/dav/vault/Projects/a.md", headers=basic(secret))).status_code == 404
        assert (await client.get("/dav/vault/Projects/b.md", headers=basic(secret))).content == b"a"


@pytest.mark.anyio
async def test_move_refuses_to_overwrite_when_told_not_to(app: Any, secret: str):
    async with dav_client(app) as client:
        await client.put("/dav/vault/Projects/a.md", content=b"a", headers=basic(secret))
        await client.put("/dav/vault/Projects/b.md", content=b"b", headers=basic(secret))
        refused = await client.request(
            "MOVE",
            "/dav/vault/Projects/a.md",
            headers={"Destination": "http://localhost/dav/vault/Projects/b.md", "Overwrite": "F", **basic(secret)},
        )
        assert refused.status_code == 412
        assert (await client.get("/dav/vault/Projects/b.md", headers=basic(secret))).content == b"b"


@pytest.mark.anyio
async def test_move_and_copy_carry_whole_collections(app: Any, secret: str):
    async with dav_client(app) as client:
        await client.put("/dav/vault/Old/one.md", content=b"1", headers=basic(secret))
        await client.put("/dav/vault/Old/deep/two.md", content=b"2", headers=basic(secret))
        copied = await client.request(
            "COPY", "/dav/vault/Old/", headers={"Destination": "http://localhost/dav/vault/Copy/", **basic(secret)}
        )
        assert copied.status_code == 201
        moved = await client.request(
            "MOVE", "/dav/vault/Old/", headers={"Destination": "http://localhost/dav/vault/New/", **basic(secret)}
        )
        assert moved.status_code == 201
        assert (await client.get("/dav/vault/New/deep/two.md", headers=basic(secret))).content == b"2"
        assert (await client.get("/dav/vault/Copy/one.md", headers=basic(secret))).content == b"1"
        assert (
            await client.request("PROPFIND", "/dav/vault/Old/", headers={"Depth": "0", **basic(secret)})
        ).status_code == 404


@pytest.mark.anyio
async def test_delete_removes_a_file_and_a_whole_collection(app: Any, secret: str):
    async with dav_client(app) as client:
        await client.put("/dav/vault/Tmp/one.md", content=b"1", headers=basic(secret))
        await client.put("/dav/vault/Tmp/sub/two.md", content=b"2", headers=basic(secret))
        assert (await client.delete("/dav/vault/Tmp/one.md", headers=basic(secret))).status_code == 204
        assert (await client.get("/dav/vault/Tmp/one.md", headers=basic(secret))).status_code == 404
        assert (await client.delete("/dav/vault/Tmp/", headers=basic(secret))).status_code == 204
        assert (
            await client.request("PROPFIND", "/dav/vault/Tmp/", headers={"Depth": "0", **basic(secret)})
        ).status_code == 404
        assert (await client.delete("/dav/vault/Tmp/", headers=basic(secret))).status_code == 404


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
        await client.put("/dav/vault/Areas/mine.md", content=b"mine", headers=basic(secret))
        assert (await client.get("/dav/vault/Areas/mine.md", headers=basic(other))).status_code == 404
        listing = await client.request("PROPFIND", "/dav/vault/", headers={"Depth": "1", **basic(other)})
    assert "/dav/vault/Welcome.md" in hrefs(listing)
    assert "/dav/vault/Areas/" not in hrefs(listing)


@pytest.mark.anyio
async def test_dav_under_a_public_url_path(tmp_path: Path, secret: str, keyring: Keyring):
    vaults = SubjectVaults(cache=tmp_path / "cache", prefix="users")
    app = build_hosted_app(vaults, hosted_config(tmp_path, public_url="http://localhost/vault"), keyring=keyring)
    async with dav_client(app) as client:
        response = await client.request("PROPFIND", "/vault/dav/vault/", headers={"Depth": "1", **basic(secret)})
    assert response.status_code == 207
    assert hrefs(response)[0] == "/vault/dav/vault/"
    assert "/vault/dav/vault/Welcome.md" in hrefs(response)


@pytest.mark.anyio
async def test_a_remotely_save_walk_lists_the_storage_once(
    vault_id: str, app: Any, secret: str, vaults: SubjectVaults, monkeypatch: pytest.MonkeyPatch
):
    async with dav_client(app) as client:
        for path in ("Areas/a.md", "Projects/b.md", "Projects/Deep/c.md"):
            await client.put(f"/dav/vault/{path}", content=b"x", headers=basic(secret))
        inner = vaults.for_subject(vault_id).backend.inner
        listings: list[str] = []
        real = inner.list_all
        monkeypatch.setattr(inner, "list_all", lambda prefix="": listings.append(prefix) or real(prefix))
        for folder in ("", "Areas/", "Projects/", "Projects/Deep/"):
            walked = await client.request("PROPFIND", f"/dav/vault/{folder}", headers={"Depth": "1", **basic(secret)})
            assert walked.status_code == 207
    assert len(listings) <= 1


def keys(vault_id: str, vaults: SubjectVaults) -> set[str]:
    return {entry.key for entry in vaults.for_subject(vault_id).backend.list_all()}


@pytest.mark.anyio
async def test_the_root_lists_one_vault_collection(app: Any, secret: str):
    async with dav_client(app) as client:
        listing = await client.request("PROPFIND", "/dav/", headers={"Depth": "1", **basic(secret)})
        alone = await client.request("PROPFIND", "/dav/", headers={"Depth": "0", **basic(secret)})
        put = await client.put("/dav/stray.md", content=b"x", headers=basic(secret))
        mkcol = await client.request("MKCOL", "/dav/", headers=basic(secret))
    assert listing.status_code == 207
    assert hrefs(listing) == ["/dav/", "/dav/vault/"]
    assert listing.text.count("<D:collection/>") == 2
    assert hrefs(alone) == ["/dav/"]
    assert put.status_code == 405 and mkcol.status_code == 405


@pytest.mark.anyio
async def test_a_remotely_save_session_syncs_the_vault_itself(
    vault_id: str, app: Any, secret: str, vaults: SubjectVaults
):
    auth = basic(secret)
    async with dav_client(app) as client:
        exists = await client.request("PROPFIND", "/dav/obsidian/", headers={"Depth": "0", **auth})
        assert exists.status_code == 207
        assert hrefs(exists) == ["/dav/obsidian/"]
        assert (await client.request("MKCOL", "/dav/obsidian/", headers=auth)).status_code == 201
        assert (await client.options("/dav/obsidian/", headers=auth)).status_code == 200

        probe = "/dav/obsidian/rs-test-folder-Xy7/"
        assert (await client.request("MKCOL", probe, headers=auth)).status_code == 201
        assert (await client.request("PROPFIND", probe, headers={"Depth": "0", **auth})).status_code == 207
        assert (await client.put(probe + "rs-test-file-Ab3", content=bytes(100), headers=auth)).status_code == 201
        assert (await client.put(probe + "rs-test-file-Ab3", content=bytes(200), headers=auth)).status_code == 204
        assert (await client.get(probe + "rs-test-file-Ab3", headers=auth)).content == bytes(200)
        assert (await client.delete(probe + "rs-test-file-Ab3", headers=auth)).status_code == 204
        assert (await client.delete(probe, headers=auth)).status_code == 204

        assert (await client.put("/dav/obsidian/Projects/plan.md", content=b"plan", headers=auth)).status_code == 201
        walked = []
        for folder in ("/dav/obsidian", "/dav/obsidian/Projects"):
            listing = await client.request("PROPFIND", folder, headers={"Depth": "1", **auth})
            assert listing.status_code == 207
            walked += [unquote(href) for href in hrefs(listing)]
        assert walked and all(href.startswith("/dav/obsidian/") for href in walked)
        assert "/dav/obsidian/Welcome.md" in walked and "/dav/obsidian/Projects/plan.md" in walked

        assert (await client.get("/dav/obsidian/Projects/plan.md", headers=auth)).content == b"plan"
        moved = await client.request(
            "MOVE",
            "/dav/obsidian/Projects/plan.md",
            headers={"Destination": "http://localhost/dav/obsidian/Projects/done.md", **auth},
        )
        assert moved.status_code == 201
        assert vaults.for_subject(vault_id).backend.get_bytes("Projects/done.md")[0] == b"plan"
        assert (await client.delete("/dav/obsidian/Projects/done.md", headers=auth)).status_code == 204
        assert (await client.delete("/dav/obsidian/", headers=auth)).status_code == 403
    stored = keys(vault_id, vaults)
    assert "Welcome.md" in stored and "Projects/done.md" not in stored
    assert not any(key.startswith(("obsidian", "rs-test")) for key in stored)


@pytest.mark.anyio
async def test_any_vault_name_reaches_the_same_vault(app: Any, secret: str, vault_id: str, vaults: SubjectVaults):
    auth = basic(secret)
    async with dav_client(app) as client:
        put = await client.put("/dav/My%20Vault/Areas/n%C3%A5got.md", content=b"n", headers=auth)
        assert put.status_code == 201
        listing = await client.request("PROPFIND", "/dav/My%20Vault/Areas/", headers={"Depth": "1", **auth})
        assert hrefs(listing) == ["/dav/My%20Vault/Areas/", "/dav/My%20Vault/Areas/n%C3%A5got.md"]
        assert (await client.get("/dav/vault/Areas/n%C3%A5got.md", headers=auth)).content == b"n"
        moved = await client.request(
            "MOVE",
            "/dav/My%20Vault/Areas/n%C3%A5got.md",
            headers={"Destination": "http://localhost/dav/other/Areas/moved.md", **auth},
        )
        assert moved.status_code == 201
    stored = keys(vault_id, vaults)
    assert "Areas/moved.md" in stored and "Areas/något.md" not in stored
    assert not any(key.startswith(("My Vault", "other")) for key in stored)


def test_if_match_star_writes_only_over_the_version_that_exists():
    from datetime import UTC, datetime

    from starlette.requests import Request

    from mindin.backends import Entry
    from mindin.dav import PreconditionFailed, expected_version

    request = Request({"type": "http", "headers": [(b"if-match", b"*")]})
    entry = Entry(key="Areas/orchard.md", version="v-orchard", mtime=datetime.now(tz=UTC), size=3)
    assert expected_version(request, entry) == "v-orchard"
    with pytest.raises(PreconditionFailed):
        expected_version(request, None)


@pytest.mark.anyio
async def test_if_match_star_on_a_file_deleted_meanwhile_is_412(
    app: Any, secret: str, vault_id: str, vaults: SubjectVaults, monkeypatch: pytest.MonkeyPatch
):
    async with dav_client(app) as client:
        await client.put("/dav/vault/Areas/orchard.md", content=b"first", headers=basic(secret))
        backend = vaults.for_subject(vault_id).backend
        real = backend.list_all
        stale = real("Areas/orchard.md")
        backend.delete("Areas/orchard.md")
        monkeypatch.setattr(
            backend, "list_all", lambda prefix="": stale if prefix == "Areas/orchard.md" else real(prefix)
        )
        response = await client.put(
            "/dav/vault/Areas/orchard.md", content=b"second", headers={"If-Match": "*", **basic(secret)}
        )
    assert response.status_code == 412
    assert "Areas/orchard.md" not in keys(vault_id, vaults)


@pytest.mark.anyio
async def test_a_slow_vault_open_leaves_the_event_loop_free(
    app: Any, secret: str, vaults: SubjectVaults, monkeypatch: pytest.MonkeyPatch
):
    import time

    import anyio

    real = vaults.for_subject

    def slow(subject: str):
        time.sleep(0.6)
        return real(subject)

    monkeypatch.setattr(vaults, "for_subject", slow)
    async with dav_client(app) as client:
        statuses: list[int] = []

        async def walk() -> None:
            statuses.append((await client.request("PROPFIND", "/dav/vault/", headers=basic(secret))).status_code)

        async with anyio.create_task_group() as group:
            group.start_soon(walk)
            started = time.monotonic()
            await anyio.sleep(0.05)
            waited = time.monotonic() - started
    assert statuses == [207]
    assert waited < 0.4


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("method", "path", "headers", "content"),
    [
        ("PUT", "/dav/vault/Areas/workshop.md", {}, b"---\ntitle: Workshop\n---\n\nWorkshop note.\n"),
        ("DELETE", "/dav/vault/Welcome.md", {}, b""),
        ("MKCOL", "/dav/vault/Frames/", {}, b""),
        ("MOVE", "/dav/vault/Welcome.md", {"Destination": "/dav/vault/Moved.md"}, b""),
        ("COPY", "/dav/vault/Welcome.md", {"Destination": "/dav/vault/Copied.md"}, b""),
    ],
    ids=["put", "delete", "mkcol", "move", "copy"],
)
async def test_a_webdav_write_shows_in_this_servers_index_at_once(
    app: Any, secret: str, vault_id: str, vaults: SubjectVaults, method: str, path: str, headers: dict, content: bytes
):
    vault = vaults.for_subject(vault_id)
    vault.sync(force=True)
    before = {note.key for note in vault.index.all_notes()}
    async with dav_client(app) as client:
        response = await client.request(method, path, headers={**headers, **basic(secret)}, content=content)
    assert response.status_code < 300
    vault.sync()
    after = {note.key for note in vault.index.all_notes()}
    expected = {
        "PUT": before | {"Areas/workshop.md"},
        "DELETE": before - {"Welcome.md"},
        "MKCOL": before,
        "MOVE": (before - {"Welcome.md"}) | {"Moved.md"},
        "COPY": before | {"Copied.md"},
    }[method]
    assert after == expected


@pytest.mark.anyio
async def test_a_webdav_read_or_a_refused_write_leaves_the_index_alone(
    app: Any, secret: str, vault_id: str, vaults: SubjectVaults
):
    vault = vaults.for_subject(vault_id)
    vault.sync(force=True)
    async with dav_client(app) as client:
        await client.request("PROPFIND", "/dav/vault/", headers=basic(secret))
        refused = await client.put(
            "/dav/vault/Areas/workshop.md", content=b"x", headers={"If-Match": '"nope"', **basic(secret)}
        )
    assert refused.status_code == 412
    assert vault._stale is False
