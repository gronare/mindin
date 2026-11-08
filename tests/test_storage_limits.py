from __future__ import annotations

import base64
import io
import zipfile
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from hosted_support import NOTE_BODY, FixedLimits, Hosted, call_tool, csrf_of, hosted, text_of

from mindin.auth.hosted_limits import StorageLimits, content_type_of
from mindin.auth.hosted_store import Account
from mindin.extensions import Extension
from mindin.vault import SubjectVaults

HOSTED = StorageLimits(account_bytes=40_000, file_bytes=10_000, content_types=("text/*", "image/*", "application/pdf"))


def note(size: int) -> str:
    return NOTE_BODY + "x" * (size - len(NOTE_BODY.encode()))


def basic(secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"ann@example.com:{secret}".encode()).decode()}


async def write(http: httpx2.AsyncClient, key: str, path: str, size: int) -> dict:
    return await call_tool(http, key, "write_file", {"path": path, "content": note(size)})


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path, extensions=[FixedLimits(HOSTED)]) as running:
        yield running


@pytest.mark.anyio
async def test_a_file_over_the_file_limit_is_refused_naming_the_limit(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com")
    async with http:
        refused = await write(http, key, "Areas/big.md", 12_000)
        kept = await write(http, key, "Areas/small.md", 8_000)
    assert refused.get("isError") and "it is 12 kB, and a file is at most 10 kB" in text_of(refused)
    assert not kept.get("isError"), text_of(kept)


@pytest.mark.anyio
async def test_a_full_account_refuses_growth_but_still_reads_and_shrinks(site: Hosted):
    http = await site.member("ann@example.com")
    account = site.store.account("ann@example.com")
    site.store.create_vault(account.id, "Workshop")
    notes, workshop = site.key_for("ann@example.com"), site.key_for("ann@example.com", vault="workshop", name="w")
    async with http:
        first = await write(http, notes, "Areas/a.md", 9_000)
        second = await write(http, workshop, "Areas/b.md", 9_000)
        refused = await write(http, workshop, "Areas/c.md", 5_000)
        shrunk = await write(http, notes, "Areas/a.md", 500)
        read = await call_tool(http, workshop, "read_file", {"path": "Areas/b.md"})
    assert not first.get("isError") and not second.get("isError")
    assert refused.get("isError") and "storage of 40 kB is full" in text_of(refused)
    assert "reading and export still work" in text_of(refused)
    assert not shrunk.get("isError"), text_of(shrunk)
    assert "xxxx" in text_of(read)


@pytest.mark.anyio
async def test_a_shared_vault_counts_toward_its_owner(site: Hosted):
    http = await site.member("ann@example.com")
    account = site.store.account("ann@example.com")
    site.store.create_vault(account.id, "Team", kind="shared")
    personal, team = site.key_for("ann@example.com"), site.key_for("ann@example.com", vault="team", name="t")
    async with http:
        shared = await write(http, team, "Areas/plan.md", 9_000)
        await write(http, team, "Areas/more.md", 9_000)
        filled = await write(http, personal, "Areas/a.md", 9_000)
        refused = await write(http, personal, "Areas/b.md", 9_000)
    assert not shared.get("isError") and not filled.get("isError"), text_of(shared) + text_of(filled)
    assert refused.get("isError") and "is full" in text_of(refused)


@pytest.mark.anyio
async def test_webdav_refuses_a_type_and_a_full_account_with_their_own_statuses(site: Hosted):
    http = await site.member("ann@example.com")
    key = site.key_for("ann@example.com")
    async with http:
        video = await http.put("/dav/vault/clip.mp4", content=b"\x00" * 100, headers=basic(key))
        renamed = await http.put("/dav/vault/Areas/a.md", content=b"text", headers=basic(key))
        moved = await http.request(
            "MOVE", "/dav/vault/Areas/a.md", headers={**basic(key), "Destination": "/dav/vault/clip.mov"}
        )
        images = [
            await http.put(f"/dav/vault/Bilagor/{name}.png", content=b"\x89PNG" + b"\x00" * 9_000, headers=basic(key))
            for name in "pqr"
        ]
        more = await http.put("/dav/vault/Bilagor/s.png", content=b"\x89PNG" + b"\x00" * 9_000, headers=basic(key))
        read = await http.get("/dav/vault/Bilagor/p.png", headers=basic(key))
    assert video.status_code == 415 and "video/mp4" in video.text and "text/*" in video.text
    assert renamed.status_code == 201 and moved.status_code == 415
    assert [image.status_code for image in images] == [201, 201, 201]
    assert more.status_code == 507 and "is full" in more.text
    assert read.status_code == 200


@pytest.mark.anyio
async def test_a_zip_upload_with_a_refused_file_imports_nothing_and_says_why(site: Hosted):
    http = await site.member("ann@example.com", sub="sub-ann")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Areas/fine.md", b"fine")
        archive.writestr("film.mkv", b"\x00" * 10)
    async with http:
        csrf = csrf_of((await http.get("/account/vaults/notes")).text)
        stopped = await http.post(
            "/account/vaults/notes/import",
            data={"csrf": csrf},
            files={"archive": ("vault.zip", buffer.getvalue(), "application/zip")},
        )
        listed = await http.get("/dav/vault/Areas/fine.md", headers=basic(site.key_for("ann@example.com")))
    assert stopped.status_code == 415
    assert "Nothing was imported" in stopped.text and "film.mkv is refused" in stopped.text
    assert listed.status_code == 404


class Roomier(Extension):
    def storage_limits(self, account: Account, limits: StorageLimits) -> StorageLimits:
        return replace(limits, file_bytes=None) if account.email == "ann@example.com" else limits


@pytest.mark.anyio
async def test_a_later_extension_changes_the_limits_an_earlier_one_chose(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path, extensions=[FixedLimits(HOSTED), Roomier()]) as site:
        ann, bob = await site.member("ann@example.com"), await site.member("bob@example.com")
        async with ann, bob:
            roomy = await write(ann, site.key_for("ann@example.com"), "Areas/big.md", 12_000)
            tight = await write(bob, site.key_for("bob@example.com"), "Areas/big.md", 12_000)
    assert not roomy.get("isError"), text_of(roomy)
    assert tight.get("isError") and "a file is at most" in text_of(tight)


@pytest.mark.anyio
async def test_without_an_extension_a_large_file_of_any_type_is_stored(
    vaults: SubjectVaults, tmp_path: Path
):
    async with hosted(vaults, tmp_path) as site:
        http = await site.member("ann@example.com")
        async with http:
            key = site.key_for("ann@example.com")
            stored = await http.put("/dav/vault/clip.mp4", content=b"\x00" * 200_000, headers=basic(key))
    assert stored.status_code == 201


def test_content_types_come_from_the_name_alone():
    assert content_type_of("Areas/a.md") == "text/markdown"
    assert content_type_of("Board.canvas") == "application/json"
    assert content_type_of("Bilagor/scan.PDF") == "application/pdf"
    assert content_type_of("clip.mp4") == "video/mp4"
    assert content_type_of("folder.d/README") == "application/octet-stream"
