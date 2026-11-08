from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from mindin.auth.hosted_keys import SealedVaults, delete_vault
from mindin.auth.hosted_snapshots import IndexSnapshots
from mindin.backends import SCHEMA_KEY
from mindin.changes import Announcing, Change, LocalBus
from mindin.vault import SubjectVaults, Vault

FRONT = "date: 2026-09-01\nupdated: 2026-09-01\ntags: [workshop]\nstatus: active\n"
LATHE = f"---\ntitle: Lathe\n{FRONT}---\n\nThe lathe wants oil before turning.\n"


class Ticks:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FailingBus(LocalBus):
    def publish(self, change: Change) -> None:
        raise ConnectionError("the bus is down")


class StuckBus(LocalBus):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def publish(self, change: Change) -> None:
        self.release.wait(10)


class CountingSnapshots:
    def __init__(self) -> None:
        self.saved: list[str] = []

    def save(self, subject: str, index_path: Path) -> bool:
        self.saved.append(subject)
        return True

    def restore(self, subject: str, index_path: Path) -> bool:
        return False


def index_file(path: Path, *notes: tuple[str, str]) -> Path:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS notes (key TEXT, version TEXT)")
        db.execute("DELETE FROM notes")
        db.executemany("INSERT INTO notes VALUES (?, ?)", notes)
    return path


def settle(vaults: SubjectVaults) -> None:
    assert vaults._snapshotting is not None
    vaults._snapshotting.submit(lambda: None).result(5)


def sealed_vaults(sealed: SealedVaults, cache: Path, **kwargs) -> SubjectVaults:
    vaults = SubjectVaults(cache=cache, prefix="users", **kwargs)
    vaults.use_backends(sealed, identify=sealed.identifier)
    return vaults


@pytest.mark.parametrize("bus", [FailingBus, StuckBus])
def test_a_write_succeeds_at_once_whatever_the_bus_does(
    bus: type[LocalBus], sealed: SealedVaults, vault_id: str, tmp_path: Path
):
    vaults = sealed_vaults(sealed, tmp_path / "cache")
    chosen = bus()
    vaults.use_bus(chosen)
    backend = vaults.for_subject(vault_id).backend
    assert isinstance(backend, Announcing)
    started = time.monotonic()
    for number in range(5):
        backend.put(f"Areas/lathe-{number}.md", LATHE)
    assert time.monotonic() - started < 2
    assert backend.get("Areas/lathe-4.md")[0] == LATHE
    if isinstance(chosen, StuckBus):
        chosen.release.set()
    vaults.close()


def test_a_full_change_queue_drops_changes_instead_of_waiting(sealed: SealedVaults, vault_id: str, tmp_path: Path):
    from mindin.changes import Publisher

    bus = StuckBus()
    publisher = Publisher(bus, capacity=1)
    started = time.monotonic()
    for _ in range(20):
        publisher.publish(Change(vault_id, "write", "here"))
    assert time.monotonic() - started < 1
    bus.release.set()
    publisher.close()


def test_an_announcer_that_raises_never_fails_the_write(storage: Path, vaults: SubjectVaults):
    inner = vaults.for_subject("ann").backend

    def broken() -> None:
        raise RuntimeError("no announcing today")

    backend = Announcing(inner, broken)
    backend.put("Areas/lathe.md", LATHE)
    assert backend.get("Areas/lathe.md")[0] == LATHE


def test_a_slow_open_of_one_vault_leaves_the_others_serving(storage: Path, tmp_path: Path):
    vaults = SubjectVaults(cache=tmp_path / "cache")
    plain = vaults.backend_factory
    entered, release = threading.Event(), threading.Event()

    def slow_for_frame(subject: str):
        if subject == "frame":
            entered.set()
            release.wait(10)
        return plain(subject)

    vaults.backend_factory = slow_for_frame
    vaults.for_subject("orchard")
    opening = threading.Thread(target=vaults.for_subject, args=("frame",))
    opening.start()
    assert entered.wait(5)
    started = time.monotonic()
    vaults.for_subject("orchard")
    assert time.monotonic() - started < 1
    release.set()
    opening.join(5)
    assert vaults.open_subjects == ["frame", "orchard"]
    vaults.close()


def test_two_requests_for_one_closed_vault_open_it_once(storage: Path, tmp_path: Path):
    vaults = SubjectVaults(cache=tmp_path / "cache")
    plain = vaults.backend_factory
    opened: list[str] = []
    gate = threading.Barrier(2)

    def counting(subject: str):
        opened.append(subject)
        time.sleep(0.1)
        return plain(subject)

    vaults.backend_factory = counting

    def ask() -> None:
        gate.wait(5)
        vaults.for_subject("orchard")

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert opened == ["orchard"]
    vaults.close()


class SlowIndex:
    def __init__(self) -> None:
        self.entered, self.release = threading.Event(), threading.Event()

    def close(self) -> None:
        self.entered.set()
        self.release.wait(10)


def test_closing_an_idle_vault_holds_up_no_other_vault(storage: Path, tmp_path: Path):
    clock = Ticks()
    vaults = SubjectVaults(cache=tmp_path / "cache", clock=clock)
    idle = vaults.for_subject("frame")
    slow = SlowIndex()
    idle._index = slow  # type: ignore[assignment]
    vaults.for_subject("orchard")
    clock.now += vaults.idle_seconds + 1
    with vaults.using_subject("orchard"):
        pass
    clock.now += 0.5
    evicting = threading.Thread(target=vaults.for_subject, args=("workshop",))
    evicting.start()
    assert slow.entered.wait(5)
    started = time.monotonic()
    vaults.for_subject("orchard")
    assert time.monotonic() - started < 1
    slow.release.set()
    evicting.join(5)
    vaults.close()


def test_snapshots_of_a_busy_vault_wait_for_the_interval_and_skip_an_unchanged_index(tmp_path: Path):
    clock = Ticks()
    vaults = SubjectVaults(cache=tmp_path / "cache", clock=clock)
    snapshots = CountingSnapshots()
    vaults.use_snapshots(snapshots, every_seconds=600)
    path = index_file(tmp_path / "frame.sqlite", ("Areas/lathe.md", "v1"))
    for _ in range(5):
        vaults.snapshot_later("frame", path)
        settle(vaults)
    assert snapshots.saved == ["frame"]
    clock.now += 601
    vaults.snapshot_later("frame", path)
    settle(vaults)
    assert snapshots.saved == ["frame"]
    index_file(path, ("Areas/lathe.md", "v2"))
    vaults.snapshot_later("frame", path)
    settle(vaults)
    assert snapshots.saved == ["frame", "frame"]
    vaults.close()


def test_a_change_inside_the_interval_is_saved_when_the_interval_ends(tmp_path: Path):
    vaults = SubjectVaults(cache=tmp_path / "cache")
    snapshots = CountingSnapshots()
    vaults.use_snapshots(snapshots, every_seconds=0.2)
    path = index_file(tmp_path / "frame.sqlite", ("Areas/lathe.md", "v1"))
    vaults.snapshot_later("frame", path)
    settle(vaults)
    index_file(path, ("Areas/lathe.md", "v2"))
    vaults.snapshot_later("frame", path)
    settle(vaults)
    assert snapshots.saved == ["frame"]
    deadline = time.monotonic() + 5
    while len(snapshots.saved) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert snapshots.saved == ["frame", "frame"]
    vaults.close()


def test_going_idle_and_shutting_down_save_a_changed_index_inside_the_interval(storage: Path, tmp_path: Path):
    clock = Ticks()
    vaults = SubjectVaults(cache=tmp_path / "cache", clock=clock)
    snapshots = CountingSnapshots()
    vaults.use_snapshots(snapshots, every_seconds=600)
    for subject in ("frame", "orchard"):
        with vaults.using_subject(subject) as vault:
            vault.backend.put("Areas/lathe.md", LATHE)
            vault.sync(force=True)
            vault.snapshot = vault.settled
            assert vault.settled is not None
            vault.settled()
    settle(vaults)
    assert sorted(snapshots.saved) == ["frame", "orchard"]
    with vaults.using_subject("frame") as vault:
        vault.backend.put("Areas/lathe.md", LATHE.replace("oil", "grease"))
        vault.sync(force=True)
    with vaults.using_subject("orchard") as vault:
        vault.backend.put("Areas/lathe.md", LATHE.replace("oil", "wax"))
        vault.sync(force=True)
    clock.now += 60
    vaults.for_subject("orchard")
    clock.now += vaults.idle_seconds + 1 - 60
    vaults.for_subject("orchard")
    settle(vaults)
    assert sorted(snapshots.saved) == ["frame", "frame", "orchard"]
    vaults.close()
    assert sorted(snapshots.saved) == ["frame", "frame", "orchard", "orchard"]


def test_no_snapshot_path_mints_a_data_key(sealed: SealedVaults, tmp_path: Path):
    snapshots = IndexSnapshots(sealed)
    path = index_file(tmp_path / "frame.sqlite", ("Areas/lathe.md", "v1"))
    assert snapshots.restore("vlt_neverkeyed", tmp_path / "restored.sqlite") is False
    assert snapshots.save("vlt_neverkeyed", path) is False
    assert not sealed.keys.has_key("vlt_neverkeyed")


def test_a_sweep_removes_the_local_index_of_a_vault_that_is_gone(tmp_path: Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for name in ("aaaaaaaaaaaa.sqlite", "bbbbbbbbbbbb.sqlite", "bbbbbbbbbbbb.sqlite-wal", "workshop.sqlite"):
        (cache / name).write_bytes(b"orchard")
    vaults = SubjectVaults(cache=cache)
    vaults.live_identifiers = lambda: {"aaaaaaaaaaaa"}
    assert vaults.sweep() == ["bbbbbbbbbbbb"]
    assert sorted(path.name for path in cache.iterdir()) == ["aaaaaaaaaaaa.sqlite", "workshop.sqlite"]


def test_the_sweep_spares_an_open_vault_and_drops_a_deleted_one(sealed: SealedVaults, vault_id: str, tmp_path: Path):
    vaults = sealed_vaults(sealed, tmp_path / "cache")
    with vaults.using_subject(vault_id) as vault:
        vault.sync(force=True)
        index_path = vault.index_path
    assert index_path is not None and index_path.exists()
    assert sealed.identifier(vault_id) in sealed.live_identifiers()
    vaults.live_identifiers = sealed.live_identifiers
    assert vaults.sweep() == []
    hosted_vault = sealed.store.vault(vault_id)
    assert hosted_vault is not None
    other = sealed_vaults(sealed, tmp_path / "other")
    other.for_subject(vault_id).sync(force=True)
    delete_vault(sealed, hosted_vault, tmp_path / "cache")
    vaults.discard(vault_id)
    other.discard(vault_id)
    other.live_identifiers = sealed.live_identifiers
    assert other.sweep() == [sealed.identifier(vault_id)]
    assert list((tmp_path / "other").glob("*.sqlite*")) == []


def test_a_layout_change_heard_on_the_bus_reloads_on_the_next_request_not_on_the_listener(
    sealed: SealedVaults, vault_id: str, tmp_path: Path
):
    vaults = sealed_vaults(sealed, tmp_path / "cache")
    vault = vaults.for_subject(vault_id)
    reloads: list[str] = []
    original = Vault.reload_schema

    def recording(self: Vault) -> None:
        reloads.append(threading.current_thread().name)
        original(self)

    vault.reload_schema = recording.__get__(vault)  # type: ignore[method-assign]
    storage = sealed.for_vault(vault_id)[0]
    schema, _ = storage.get(SCHEMA_KEY)
    storage.put(SCHEMA_KEY, schema + "\nlog:\n  file_format: '{repo}-journal.md'\n")
    listener = threading.Thread(target=vaults.apply, args=(Change(vault_id, "schema", "elsewhere"),), name="listener")
    listener.start()
    listener.join(5)
    assert reloads == []
    assert vaults.for_subject(vault_id).schema.log_key("workshop").endswith("workshop-journal.md")
    assert reloads == [threading.current_thread().name]
    vaults.close()


def test_marking_a_vault_stale_after_its_own_write_keeps_the_listing_it_already_holds(
    sealed: SealedVaults, vault_id: str, tmp_path: Path
):
    vaults = sealed_vaults(sealed, tmp_path / "cache")
    backend = vaults.for_subject(vault_id).backend
    listings: list[str] = []
    inner_list = backend.inner.list_all

    def counted(prefix: str = ""):
        listings.append(prefix)
        return inner_list(prefix)

    backend.inner.list_all = counted
    backend.forget_listing()
    backend.list_all()
    for number in range(3):
        backend.put_bytes(f"Areas/lathe-{number}.md", LATHE.encode())
        vaults.wrote(vault_id)
        backend.list_all()
    assert len(listings) == 1
    vaults.close()
