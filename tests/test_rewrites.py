from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest

from mindin import notes, provision
from mindin.backends import VersionConflict
from mindin.backends.local import LocalBackend
from mindin.frontmatter import parse
from mindin.vault import Vault


class Meddled:
    """A backend whose next read of `key` lets another writer in just after the read."""

    def __init__(self, inner: Any, key: str, meddle: Callable[[], None], times: int = 1) -> None:
        self.inner = inner
        self.key = key
        self.meddle = meddle
        self.times = times

    def get(self, key: str) -> tuple[str, str]:
        found = self.inner.get(key)
        if key == self.key and self.times:
            self.times -= 1
            self.meddle()
        return found

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@pytest.fixture
def other(vault: Vault, vault_dir: Path, tmp_path: Path) -> Vault:
    second = Vault(backend=LocalBackend(vault_dir), schema=vault.schema, index_path=tmp_path / "other.sqlite")
    yield second
    second.close()


def meddle(vault: Vault, key: str, action: Callable[[], object], times: int = 1) -> None:
    vault.backend = Meddled(vault.backend, key, action, times)


def body(vault: Vault, key: str) -> str:
    return parse(vault.backend.get(key)[0])[1]


def test_two_appends_at_once_through_two_servers_keep_both_lines(vault: Vault, other: Vault):
    key = "Areas/orchard.md"
    meddle(vault, key, lambda: notes.append(other, key, "Line from the other server."))
    notes.append(vault, key, "Line from this server.")
    text = body(other, key)
    assert "Line from the other server." in text and "Line from this server." in text


def test_a_status_change_keeps_an_append_that_landed_while_it_ran(vault: Vault, other: Vault):
    key = "Projects/orchard-fresh.md"
    meddle(vault, key, lambda: notes.append(other, key, "Written meanwhile."))
    notes.set_status(vault, key, "draft")
    frontmatter, text = parse(other.backend.get(key)[0])
    assert frontmatter["status"] == "draft" and "Written meanwhile." in text


def test_two_log_lines_at_once_both_land(vault: Vault, other: Vault):
    key = notes.note_key(vault.schema.log_key("orchard"))
    meddle(vault, key, lambda: notes.log_append(other, "orchard", "the other server's session"))
    notes.log_append(vault, "orchard", "this server's session")
    text = body(other, key)
    assert "the other server's session" in text and "this server's session" in text


def test_a_write_keeps_a_claim_set_while_it_ran(vault: Vault, other: Vault):
    key = "Projects/orchard-fresh.md"
    text, _ = vault.backend.get(key)
    claimed = text.replace(
        "status: active", "status: active\nkebbie:\n  machine: m1\n  task: t1\n  since: '2026-09-01'"
    )
    meddle(vault, key, lambda: other.backend.put(key, claimed))
    notes.write(vault, key, text)
    assert parse(other.backend.get(key)[0])[0]["kebbie"]["machine"] == "m1"


def test_closing_a_note_keeps_what_was_appended_while_it_closed(vault: Vault, other: Vault):
    key = "Projects/orchard-fresh.md"
    meddle(vault, key, lambda: notes.append(other, key, "Last words."))
    archived = notes.close(vault, key)
    assert archived != key and "Last words." in body(other, archived)
    assert key not in {entry.key for entry in other.backend.list()}


class MeddledDelete:
    """A backend that lets another writer in just before the first delete of `key`."""

    def __init__(self, inner: Any, key: str, meddle: Callable[[], None]) -> None:
        self.inner = inner
        self.key = key
        self.meddle = meddle

    def _before(self, key: str) -> None:
        if key == self.key and self.meddle is not None:
            meddle, self.meddle = self.meddle, None
            meddle()

    def delete(self, key: str) -> None:
        self._before(key)
        self.inner.delete(key)

    def delete_checked(self, key: str, expected_version: str) -> None:
        self._before(key)
        self.inner.delete_checked(key, expected_version)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def test_closing_a_note_keeps_an_append_that_landed_just_before_the_delete(vault: Vault, other: Vault):
    key = "Projects/orchard-fresh.md"
    vault.backend = MeddledDelete(vault.backend, key, lambda: notes.append(other, key, "Last words."))
    archived = notes.close(vault, key)
    assert archived != key and "Last words." in body(other, archived)
    assert key not in {entry.key for entry in other.backend.list()}
    assert [entry.key for entry in other.backend.list() if "orchard-fresh" in entry.key] == [archived]


def test_two_backlog_ideas_with_one_title_get_two_notes(vault: Vault, other: Vault):
    folder = vault.schema.backlog_folder
    taken: list[str] = []
    vault.backend = Meddled(vault.backend, "", lambda: None)
    real_list = vault.backend.inner.list

    def listing_then_meddle() -> list:
        listed = real_list()
        if not taken:
            taken.append(notes.backlog_add(other, "Graft pears", "orchard", "from the other server"))
        return listed

    vault.backend.list = listing_then_meddle
    mine = notes.backlog_add(vault, "Graft pears", "orchard", "from this server")
    assert taken[0] != mine and mine.startswith(folder)
    assert "from the other server" in body(other, taken[0]) and "from this server" in body(other, mine)


def test_a_note_that_never_stops_changing_gives_up_with_a_conflict(vault: Vault, other: Vault):
    key = "Areas/orchard.md"
    meddle(vault, key, lambda: notes.append(other, key, "again"), times=notes.REWRITE_RETRIES + 1)
    with pytest.raises(VersionConflict):
        notes.append(vault, key, "never lands")


def test_provisioning_keeps_a_file_another_server_wrote_first(tmp_path: Path):
    backend = LocalBackend(tmp_path / "fresh")
    theirs = "---\ntitle: Welcome\n---\n\nWritten by the server that won.\n"
    real_list = backend.list

    def listing_then_lose_the_race() -> list:
        listed = real_list()
        LocalBackend(tmp_path / "fresh").put(provision.WELCOME_KEY, theirs)
        return listed

    backend.list = listing_then_lose_the_race
    result = provision.initialize(backend, welcome={"DAV_URL": "http://localhost/dav/"})
    assert provision.WELCOME_KEY in result.kept
    assert backend.get(provision.WELCOME_KEY)[0] == theirs


@asynccontextmanager
async def dav(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    from hosted_support import first_vault_id, hosted_config, key_for_subject

    from mindin.auth.hosted import build_hosted_app
    from mindin.auth.hosted_keys import Keyring, MasterKey
    from mindin.vault import SubjectVaults

    vaults = SubjectVaults(cache=tmp_path / "cache", prefix="users")
    app = build_hosted_app(vaults, hosted_config(tmp_path), keyring=Keyring(MasterKey.generate()))
    secret = key_for_subject(tmp_path / "auth", "ann@example.com", "sub-ann")
    vaults.for_subject(first_vault_id(tmp_path / "auth", "ann@example.com"))
    login = base64.b64encode(f"ann@example.com:{secret}".encode()).decode("ascii")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://localhost", headers={"Authorization": f"Basic {login}"}
        ) as client:
            yield client
    vaults.close()


@pytest.mark.anyio
async def test_webdav_refuses_a_put_over_a_version_it_did_not_read(storage: Path, tmp_path: Path):
    async with dav(tmp_path) as client:
        first = await client.put("/dav/vault/Areas/pears.md", content=b"one")
        read = first.headers["etag"]
        second = await client.put("/dav/vault/Areas/pears.md", content=b"two", headers={"If-Match": read})
        stale = await client.put("/dav/vault/Areas/pears.md", content=b"three", headers={"If-Match": read})
        kept = await client.get("/dav/vault/Areas/pears.md")
    assert second.status_code == 204 and stale.status_code == 412
    assert kept.content == b"two"


@pytest.mark.anyio
async def test_webdav_creates_only_when_asked_for_a_new_file(storage: Path, tmp_path: Path):
    async with dav(tmp_path) as client:
        created = await client.put("/dav/vault/Areas/plums.md", content=b"one", headers={"If-None-Match": "*"})
        again = await client.put("/dav/vault/Areas/plums.md", content=b"two", headers={"If-None-Match": "*"})
        missing = await client.put("/dav/vault/Areas/figs.md", content=b"one", headers={"If-Match": "*"})
        kept = await client.get("/dav/vault/Areas/plums.md")
    assert (created.status_code, again.status_code, missing.status_code) == (201, 412, 412)
    assert kept.content == b"one"
