from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from mindin.auth.hosted_keys import Keyring, MasterKey, SealedVaults, VaultKeys, delete_vault
from mindin.auth.hosted_store import HostedStore
from mindin.backends import SCHEMA_KEY
from mindin.changes import NULL_BUS, Announcing, Change, LocalBus
from mindin.search import search
from mindin.vault import SubjectVaults, Vault

FRONT = "date: 2026-09-01\nupdated: 2026-09-01\ntags: [orchard]\nstatus: active\n"
QUINCE = f"---\ntitle: Quince\n{FRONT}---\n\nThe quince wants frost before picking.\n"


@pytest.fixture
def sealed(storage: Path, tmp_path: Path) -> SealedVaults:
    store = HostedStore(tmp_path / "auth", extensions=())
    return SealedVaults(store, VaultKeys(store, Keyring(MasterKey.generate())))


@pytest.fixture
def vault_id(sealed: SealedVaults) -> str:
    account, _ = sealed.store.invite("ann@example.com")
    vault = sealed.store.first_vault(account.id)
    assert vault is not None
    return vault.id


def replica(sealed: SealedVaults, cache: Path, bus: LocalBus | None) -> SubjectVaults:
    vaults = SubjectVaults(cache=cache, prefix="users")
    vaults.use_backends(sealed, identify=sealed.identifier)
    if bus is not None:
        vaults.use_bus(bus)
    return vaults


@pytest.fixture
def pair(sealed: SealedVaults, tmp_path: Path) -> Iterator[tuple[SubjectVaults, SubjectVaults]]:
    bus = LocalBus()
    first, second = replica(sealed, tmp_path / "cache-a", bus), replica(sealed, tmp_path / "cache-b", bus)
    yield first, second
    first.close()
    second.close()


def found(vault: Vault, query: str) -> list[str]:
    vault.sync()
    return [row.note.key for row in search(vault.index, vault.schema, query).rows]


def test_a_write_on_one_server_is_found_by_a_search_on_the_other_at_once(
    pair: tuple[SubjectVaults, SubjectVaults], vault_id: str
):
    first, second = pair
    assert found(second.for_subject(vault_id), "quince") == []
    first.for_subject(vault_id).backend.put("Areas/quince.md", QUINCE)
    first.drain()
    assert found(second.for_subject(vault_id), "quince") == ["Areas/quince.md"]


def test_a_deleted_vault_leaves_no_index_on_the_other_server(
    pair: tuple[SubjectVaults, SubjectVaults], sealed: SealedVaults, vault_id: str, tmp_path: Path
):
    first, second = pair
    found(second.for_subject(vault_id), "quince")
    index_file = second.for_subject(vault_id).index_path
    assert index_file is not None and index_file.exists()
    vault = sealed.store.vault(vault_id)
    assert vault is not None
    first.discard(vault_id)
    delete_vault(sealed, vault, first.cache)
    first.publish(vault_id, "gone")
    first.drain()
    assert vault_id not in second.open_subjects
    assert list((tmp_path / "cache-b").glob(f"{sealed.identifier(vault_id)}.sqlite*")) == []


def test_a_change_to_every_vault_makes_each_open_vault_read_the_storage_again(
    pair: tuple[SubjectVaults, SubjectVaults], sealed: SealedVaults, vault_id: str
):
    _, second = pair
    found(second.for_subject(vault_id), "quince")
    sealed.for_vault(vault_id)[0].put("Areas/quince.md", QUINCE)
    second.apply(Change("", "all", "elsewhere"))
    assert found(second.for_subject(vault_id), "quince") == ["Areas/quince.md"]


def test_a_server_ignores_the_changes_it_published_itself(sealed: SealedVaults, vault_id: str, tmp_path: Path):
    heard: list[Change] = []
    bus = LocalBus()
    bus.listen(heard.append)
    vaults = replica(sealed, tmp_path / "cache", bus)
    found(vaults.for_subject(vault_id), "quince")
    sealed.for_vault(vault_id)[0].put("Areas/quince.md", QUINCE)
    vaults.publish(vault_id, "write")
    vaults.drain()
    assert heard == [Change(vault_id, "write", vaults.origin)]
    assert found(vaults.for_subject(vault_id), "quince") == []
    vaults.close()


def test_without_a_bus_a_server_keeps_its_vaults_as_before(sealed: SealedVaults, vault_id: str, tmp_path: Path):
    first, second = replica(sealed, tmp_path / "cache-a", None), replica(sealed, tmp_path / "cache-b", None)
    assert first.bus is NULL_BUS
    assert not isinstance(first.for_subject(vault_id).backend, Announcing)
    found(second.for_subject(vault_id), "quince")
    first.for_subject(vault_id).backend.put("Areas/quince.md", QUINCE)
    assert found(second.for_subject(vault_id), "quince") == []
    first.close()
    second.close()


def test_a_new_layout_on_one_server_is_the_layout_on_the_other(
    pair: tuple[SubjectVaults, SubjectVaults], sealed: SealedVaults, vault_id: str
):
    first, second = pair
    found(second.for_subject(vault_id), "quince")
    storage = sealed.for_vault(vault_id)[0]
    schema, _ = storage.get(SCHEMA_KEY)
    storage.put(SCHEMA_KEY, schema + "\nlog:\n  file_format: '{repo}-journal.md'\n")
    first.publish(vault_id, "schema")
    first.drain()
    assert second.for_subject(vault_id).schema.log_key("orchard").endswith("orchard-journal.md")
