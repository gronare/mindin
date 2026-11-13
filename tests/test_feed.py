from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from hosted_support import call_tool, hosted, text_of
from mcp.client import Client
from moto import mock_aws

from mindin import feed, index
from mindin.backends import SCHEMA_KEY
from mindin.backends.s3 import S3Backend
from mindin.changes import LocalBus
from mindin.client import LocalClient
from mindin.hosted.accounts.keys import SealedVaults
from mindin.hosted.feed import ChangeFeed
from mindin.schema import load_schema
from mindin.server import build_server
from mindin.vault import SubjectVaults, Vault

FRONT = "date: 2026-09-01\nupdated: 2026-09-01\ntags: [orchard]\nstatus: active\n"


def note(title: str, body: str = "Picked in October.") -> str:
    return f"---\ntitle: {title}\n{FRONT}---\n\n{body}\n"


def changes_of(vault: Vault, since: str | None, include: str = "frontmatter", limit: int = 100) -> dict:
    return asyncio.run(feed.changes(lambda: nullcontext(vault), since, limit=limit, include=include))


def keys(found: dict) -> list[tuple[str, str]]:
    return [(change["key"], change["action"]) for change in found["changes"]]


def cursor_now(vault: Vault) -> str:
    found = changes_of(vault, None)
    assert found["reset"] and found["changes"] == []
    return found["cursor"]


def test_without_a_cursor_the_answer_is_reset_with_the_cursor_to_follow_from(vault: Vault):
    first = changes_of(vault, None)
    assert first["reset"] is True and first["changes"] == []
    assert changes_of(vault, first["cursor"]) == {
        "cursor": first["cursor"],
        "reset": False,
        "more": False,
        "changes": [],
    }


def test_each_changed_note_comes_once_in_its_latest_state(vault: Vault, vault_dir: Path):
    since = cursor_now(vault)
    (vault_dir / "Areas" / "quince.md").write_text(note("Quince"), encoding="utf-8")
    vault.sync(force=True)
    (vault_dir / "Areas" / "quince.md").write_text(note("Quince, ripe"), encoding="utf-8")
    found = changes_of(vault, since)
    assert keys(found) == [("Areas/quince.md", "write")]
    assert found["changes"][0]["note"] == {"frontmatter": {"title": "Quince, ripe", **frontmatter_tail()}}
    assert changes_of(vault, found["cursor"])["changes"] == []


def frontmatter_tail() -> dict:
    return {"date": "2026-09-01", "updated": "2026-09-01", "tags": ["orchard"], "status": "active"}


def test_the_body_comes_only_on_request(vault: Vault, vault_dir: Path):
    since = cursor_now(vault)
    (vault_dir / "Areas" / "quince.md").write_text(note("Quince", "Frost first."), encoding="utf-8")
    assert changes_of(vault, since, include="body")["changes"][0]["note"]["body"] == "\nFrost first.\n"
    with pytest.raises(ValueError, match="include is one of"):
        changes_of(vault, since, include="everything")


def test_a_deleted_note_comes_as_a_delete_and_a_moved_one_as_a_delete_and_a_write(vault: Vault, vault_dir: Path):
    (vault_dir / "Areas" / "quince.md").write_text(note("Quince"), encoding="utf-8")
    (vault_dir / "Areas" / "medlar.md").write_text(note("Medlar"), encoding="utf-8")
    since = cursor_now(vault)
    (vault_dir / "Areas" / "quince.md").unlink()
    (vault_dir / "Areas" / "medlar.md").rename(vault_dir / "Resources" / "medlar.md")
    found = changes_of(vault, since)
    assert sorted(keys(found)) == [
        ("Areas/medlar.md", "delete"),
        ("Areas/quince.md", "delete"),
        ("Resources/medlar.md", "write"),
    ]
    assert all(change["note"] is None for change in found["changes"] if change["action"] == "delete")


def test_a_note_written_again_after_its_delete_comes_as_a_write(vault: Vault, vault_dir: Path):
    (vault_dir / "Areas" / "quince.md").write_text(note("Quince"), encoding="utf-8")
    since = cursor_now(vault)
    (vault_dir / "Areas" / "quince.md").unlink()
    vault.sync(force=True)
    (vault_dir / "Areas" / "quince.md").write_text(note("Quince again"), encoding="utf-8")
    assert keys(changes_of(vault, since)) == [("Areas/quince.md", "write")]


def test_a_cursor_older_than_the_kept_deletes_answers_reset(vault: Vault, vault_dir: Path):
    (vault_dir / "Areas" / "quince.md").write_text(note("Quince"), encoding="utf-8")
    since = cursor_now(vault)
    (vault_dir / "Areas" / "quince.md").unlink()
    vault.sync(force=True)
    vault.index.prune_feed(keep=0)
    found = changes_of(vault, since)
    assert found["reset"] is True and found["changes"] == []
    assert changes_of(vault, found["cursor"])["reset"] is False


def test_a_cursor_from_before_a_rebuild_or_from_elsewhere_answers_reset(vault: Vault):
    since = cursor_now(vault)
    vault.index.rebuild()
    assert changes_of(vault, since)["reset"] is True
    assert changes_of(vault, "not-a-cursor")["reset"] is True
    assert changes_of(vault, "17")["reset"] is True


def test_a_limit_pages_through_with_more(vault: Vault, vault_dir: Path):
    since = cursor_now(vault)
    for name in ("apple", "pear", "plum"):
        (vault_dir / "Areas" / f"{name}.md").write_text(note(name), encoding="utf-8")
    seen: list[str] = []
    found = changes_of(vault, since, limit=2)
    while True:
        seen += [change["key"] for change in found["changes"]]
        if not found["more"]:
            break
        found = changes_of(vault, found["cursor"], limit=2)
    assert sorted(seen) == ["Areas/apple.md", "Areas/pear.md", "Areas/plum.md"]


def test_a_new_schema_comes_as_a_schema_change(vault: Vault, vault_dir: Path):
    since = cursor_now(vault)
    schema = vault_dir / ".vault" / "schema.yml"
    schema.write_text(schema.read_text(encoding="utf-8") + "\n# kept\n", encoding="utf-8")
    assert keys(changes_of(vault, since)) == [(SCHEMA_KEY, "schema")]


def later(seconds: float, action) -> threading.Thread:
    thread = threading.Timer(seconds, action)
    thread.start()
    return thread


@pytest.mark.anyio
async def test_a_wait_on_a_folder_wakes_on_a_file_change(vault: Vault, vault_dir: Path):
    since = await asyncio.to_thread(cursor_now, vault)
    later(0.5, lambda: (vault_dir / "Areas" / "quince.md").write_text(note("Quince"), encoding="utf-8"))
    started = time.monotonic()
    found = await feed.changes(lambda: nullcontext(vault), since, wait=30)
    assert keys(found) == [("Areas/quince.md", "write")]
    assert time.monotonic() - started < 10
    assert feed.folder_events(vault.backend.root)._watch is None


@pytest.mark.anyio
async def test_a_wait_on_an_idle_folder_ends_with_no_changes_and_ignores_hidden_files(vault: Vault, vault_dir: Path):
    since = await asyncio.to_thread(cursor_now, vault)
    reads: list[float] = []
    real = vault.index.feed_page

    def counted(*args):
        reads.append(time.monotonic())
        return real(*args)

    vault.index.feed_page = counted
    (vault_dir / ".obsidian").mkdir(exist_ok=True)
    later(0.3, lambda: (vault_dir / ".obsidian" / "workspace.json").write_text("{}", encoding="utf-8"))
    started = time.monotonic()
    found = await feed.changes(lambda: nullcontext(vault), since, wait=1.5)
    assert found == {"cursor": since, "reset": False, "more": False, "changes": []}
    assert 1.4 < time.monotonic() - started < 5
    assert len(reads) == 2


@pytest.fixture
def bucket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[S3Backend]:
    monkeypatch.setattr(index, "SYNC_THROTTLE_SECONDS", 1.0)
    monkeypatch.setattr(feed, "LISTING_SECONDS", 1.0)
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="orchard")
        yield S3Backend("https://s3.amazonaws.com", "key", "secret", "orchard", prefix="vault")


class Listings:
    def __init__(self, backend: S3Backend) -> None:
        self.backend = backend
        self.count = 0
        self.real = backend.list
        backend.list = self.list

    def list(self):
        self.count += 1
        return self.real()


@pytest.mark.anyio
async def test_a_wait_on_a_bucket_lists_it_no_more_often_than_its_pace(bucket: S3Backend, tmp_path: Path):
    vault = Vault(bucket, load_schema(bucket), index_path=tmp_path / "bucket.sqlite")
    since = await asyncio.to_thread(cursor_now, vault)
    listings = Listings(bucket)
    later(1.6, lambda: bucket.put("Areas/quince.md", note("Quince")))
    found = await feed.changes(lambda: nullcontext(vault), since, wait=4.5)
    assert keys(found) == [("Areas/quince.md", "write")]
    assert 1 <= listings.count <= 3
    idle_from = found["cursor"]
    listings.count = 0
    started = time.monotonic()
    assert (await feed.changes(lambda: nullcontext(vault), idle_from, wait=3.2))["changes"] == []
    assert listings.count <= int(time.monotonic() - started) + 1
    vault.close()


class Enough(BaseException):
    pass


def test_watch_prints_the_cursor_then_one_line_per_batch(vault: Vault, vault_dir: Path):
    lines: list[dict] = []

    def emit(found: dict) -> None:
        lines.append(json.loads(json.dumps(found)))
        if len(lines) == 1:
            later(0.3, lambda: (vault_dir / "Areas" / "quince.md").write_text(note("Quince"), encoding="utf-8"))
        if len(lines) == 2:
            raise Enough

    client = LocalClient()
    client._vault = vault
    args = SimpleNamespace(since=None, body=False, limit=None)
    finished = threading.Event()

    def run() -> None:
        try:
            client.watch(args, emit)
        except Enough:
            finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(20)
    assert finished.is_set()
    assert lines[0]["reset"] is True and lines[0]["changes"] == []
    assert keys(lines[1]) == [("Areas/quince.md", "write")]
    assert lines[1]["cursor"] != lines[0]["cursor"]


@pytest.mark.anyio
async def test_the_stdio_server_answers_changes(vault: Vault, vault_dir: Path):
    async with Client(build_server(vault)) as client:
        first = json.loads((await client.call_tool("changes", {})).content[0].text)
        (vault_dir / "Areas" / "quince.md").write_text(note("Quince"), encoding="utf-8")
        found = json.loads((await client.call_tool("changes", {"since": first["cursor"], "wait": 10})).content[0].text)
    assert keys(found) == [("Areas/quince.md", "write")]


def server(sealed: SealedVaults, cache: Path, bus: LocalBus | None = None) -> SubjectVaults:
    vaults = SubjectVaults(cache=cache, prefix="users")
    vaults.use_backends(sealed, identify=sealed.identifier)
    vaults.use_feed(ChangeFeed(sealed.store, lambda vault_id: sealed.for_vault(vault_id)[0]))
    if bus is not None:
        vaults.use_bus(bus)
    return vaults


@pytest.fixture
def served(sealed: SealedVaults, tmp_path: Path) -> Iterator[SubjectVaults]:
    vaults = server(sealed, tmp_path / "cache")
    yield vaults
    vaults.close()


def hosted_changes(vaults: SubjectVaults, vault_id: str, since: str | None, wait: float = 0) -> dict:
    return asyncio.run(feed.changes(lambda: vaults.using_subject(vault_id), since, wait=wait))


def test_a_hosted_write_is_numbered_and_its_path_kept_sealed(
    served: SubjectVaults, sealed: SealedVaults, vault_id: str
):
    since = hosted_changes(served, vault_id, None)["cursor"]
    served.for_subject(vault_id).backend.put("Areas/quince.md", note("Quince"))
    with sealed.store.connect() as connection:
        rows = connection.execute("SELECT seq, path, action FROM vault_feed WHERE vault_id = ?", (vault_id,)).fetchall()
    assert [(row["seq"], row["action"]) for row in rows] == [(int(since) + 1, "write")]
    assert "quince" not in rows[0]["path"]


def test_a_hosted_cursor_follows_the_latest_state_of_each_note(served: SubjectVaults, vault_id: str):
    backend = served.for_subject(vault_id).backend
    since = hosted_changes(served, vault_id, None)["cursor"]
    backend.put("Areas/quince.md", note("Quince"))
    backend.put("Areas/quince.md", note("Quince, ripe"))
    backend.put("Areas/medlar.md", note("Medlar"))
    backend.move("Areas/medlar.md", "Resources/medlar.md")
    backend.put("Areas/pear.md", note("Pear"))
    backend.delete("Areas/pear.md")
    found = hosted_changes(served, vault_id, since)
    assert keys(found) == [
        ("Areas/quince.md", "write"),
        ("Areas/medlar.md", "delete"),
        ("Resources/medlar.md", "write"),
        ("Areas/pear.md", "delete"),
    ]
    assert found["changes"][0]["note"]["frontmatter"]["title"] == "Quince, ripe"
    assert found["changes"][0]["version"] == backend.get("Areas/quince.md")[1]
    assert hosted_changes(served, vault_id, found["cursor"])["changes"] == []


def test_a_hosted_cursor_older_than_the_pruned_rows_answers_reset(
    served: SubjectVaults, sealed: SealedVaults, vault_id: str
):
    backend = served.for_subject(vault_id).backend
    since = hosted_changes(served, vault_id, None)["cursor"]
    backend.put("Areas/quince.md", note("Quince"))
    backend.put("Areas/medlar.md", note("Medlar"))
    pruning = ChangeFeed(sealed.store, lambda found: sealed.for_vault(found)[0], keep_rows=1)
    assert pruning.prune(vault_id) == int(since) + 1
    assert hosted_changes(served, vault_id, since)["reset"] is True
    assert keys(hosted_changes(served, vault_id, str(int(since) + 1))) == [("Areas/medlar.md", "write")]


def test_a_hosted_feed_leaves_out_files_that_are_not_notes(served: SubjectVaults, vault_id: str):
    backend = served.for_subject(vault_id).backend
    since = hosted_changes(served, vault_id, None)["cursor"]
    backend.put_bytes("Areas/photo.png", b"\x89PNG")
    backend.put(".obsidian/workspace.json", "{}")
    assert hosted_changes(served, vault_id, since) == {"cursor": since, "reset": False, "more": False, "changes": []}


@pytest.mark.anyio
async def test_a_hosted_wait_ends_idle_and_leaves_no_waiter_behind(served: SubjectVaults, vault_id: str):
    opened = lambda: served.using_subject(vault_id)  # noqa: E731
    since = (await feed.changes(opened, None))["cursor"]
    started = time.monotonic()
    found = await feed.changes(opened, since, wait=1.0)
    assert found["changes"] == [] and 0.9 < time.monotonic() - started < 3
    assert served.waking.count() == 0


@pytest.mark.anyio
async def test_a_wait_on_one_server_returns_when_another_server_writes(
    sealed: SealedVaults, vault_id: str, tmp_path: Path
):
    bus = LocalBus()
    first, second = server(sealed, tmp_path / "a", bus), server(sealed, tmp_path / "b", bus)
    opened = lambda: second.using_subject(vault_id)  # noqa: E731
    since = (await feed.changes(opened, None))["cursor"]
    later(0.3, lambda: first.for_subject(vault_id).backend.put("Areas/quince.md", note("Quince")))
    started = time.monotonic()
    found = await feed.changes(opened, since, wait=30)
    assert keys(found) == [("Areas/quince.md", "write")]
    assert time.monotonic() - started < 5
    first.close()
    second.close()


@pytest.mark.anyio
async def test_the_hosted_server_answers_changes_to_any_key(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as site:
        ann = await site.member("ann@example.com")
        key = site.key_for("ann@example.com")
        first = json.loads(text_of(await call_tool(ann, key, "changes", {})))
        assert first["reset"] is True
        howto = note("Quince").replace("status: active", 'status: active\nkind: howto\narea: "[[orchard]]"')
        await call_tool(ann, key, "write_file", {"path": "Resources/quince.md", "content": howto})
        found = json.loads(text_of(await call_tool(ann, key, "changes", {"since": first["cursor"], "wait": 5})))
        await ann.aclose()
    assert keys(found) == [("Resources/quince.md", "write")]


@pytest.mark.anyio
async def test_watch_connects_again_with_a_growing_pause_from_its_last_cursor(monkeypatch: pytest.MonkeyPatch):
    from mindin import client
    from mindin.remote import RemoteError

    pauses: list[float] = []
    seen: list[str | None] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)

    async def follow(at: dict) -> None:
        seen.append(at["cursor"])
        if len(seen) == 1:
            at["cursor"] = "7"
        if len(seen) < 4:
            raise RemoteError("cannot reach https://vault.example.test: connection refused")
        raise RemoteError("https://vault.example.test refused the stored key (401); sign in again")

    monkeypatch.setattr(client.anyio, "sleep", pause)
    with pytest.raises(RemoteError, match="refused the stored key"):
        await client.again_and_again(follow, "3", "https://vault.example.test")
    assert seen == ["3", "7", "7", "7"]
    assert pauses == [1.0, 2.0, 4.0]
