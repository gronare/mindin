from __future__ import annotations

from pathlib import Path

import numpy
import pytest
from fake_models import concept_vector
from hosted_support import Hosted, hosted

from mindin.auth.hosted_keys import SealedVaults, VaultKeys, delete_vault
from mindin.auth.hosted_snapshots import IndexSnapshots
from mindin.vault import NoSubject, SubjectVaults

NOTE = "---\ntitle: Kopian\ndate: 2026-10-01\nupdated: 2026-10-01\ntags: []\nstatus: active\n---\nsäkerhetskopia\n"
CHANGED = NOTE.replace("säkerhetskopia", "säkerhetskopia varje natt")


class CountingEmbedder:
    model_id = "fake-1"
    min_similarity = 0.3

    def __init__(self) -> None:
        self.embedded: list[str] = []

    def passages(self, texts):
        self.embedded.extend(texts)
        return numpy.vstack([concept_vector(text) for text in texts])

    def query(self, text):
        return concept_vector(text)


class Ticks:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def replica(site: Hosted, cache: Path, embedder: CountingEmbedder | None = None, clock=None) -> SubjectVaults:
    vaults = SubjectVaults(cache=cache, prefix="users", clock=clock or Ticks())
    vaults.embedder = embedder
    sealed = SealedVaults(site.store, VaultKeys(site.store, site.keyring), vaults.prefix)
    vaults.use_backends(sealed, identify=sealed.identifier)
    vaults.use_snapshots(IndexSnapshots(sealed))
    return vaults


def snapshot_objects(site: Hosted, vault_id: str) -> list[str]:
    sealed = SealedVaults(site.store, VaultKeys(site.store, site.keyring))
    return [entry.key for entry in sealed.snapshot_storage(vault_id).list_all() if not entry.is_dir]


@pytest.fixture
async def site(storage: Path, tmp_path: Path):
    first = SubjectVaults(cache=tmp_path / "first")
    async with hosted(first, tmp_path, index_snapshots=True) as running:
        await running.member("ann@example.com", sub="sub-ann")
        yield running
    first.close()


def embedded_on_first_replica(site: Hosted, tmp_path: Path) -> str:
    vault_id = site.vault_of("ann@example.com")
    first = replica(site, tmp_path / "one", CountingEmbedder())
    with first.using_subject(vault_id) as vault:
        vault.backend.put("Projects/kopian.md", NOTE)
        vault.sync(force=True)
    first.close()
    assert snapshot_objects(site, vault_id)
    return vault_id


@pytest.mark.anyio
async def test_an_empty_cache_restores_the_snapshot_and_embeds_no_unchanged_note(site: Hosted, tmp_path: Path):
    vault_id = embedded_on_first_replica(site, tmp_path)
    embedder = CountingEmbedder()
    second = replica(site, tmp_path / "two", embedder)
    with second.using_subject(vault_id) as vault:
        assert vault.index.vectors is not None and vault.index.vectors.count() > 0
        vault.sync(force=True)
        assert embedder.embedded == []
        assert vault.index.vectors.nearest("backup")[0].key == "Projects/kopian.md"
    second.close()


@pytest.mark.anyio
async def test_a_note_changed_after_the_snapshot_is_embedded_again(site: Hosted, tmp_path: Path):
    vault_id = embedded_on_first_replica(site, tmp_path)
    embedder = CountingEmbedder()
    second = replica(site, tmp_path / "two", embedder)
    with second.using_subject(vault_id) as vault:
        vault.backend.put("Projects/kopian.md", CHANGED)
        vault.sync()
        assert embedder.embedded and all("varje natt" in text for text in embedder.embedded)
    second.close()


@pytest.mark.anyio
async def test_an_idle_vault_leaves_a_snapshot_of_its_words_index(site: Hosted, tmp_path: Path):
    vault_id = site.vault_of("ann@example.com")
    clock = Ticks()
    first = replica(site, tmp_path / "one", clock=clock)
    with first.using_subject(vault_id) as vault:
        vault.backend.put("Projects/kopian.md", NOTE)
        vault.sync(force=True)
    assert not snapshot_objects(site, vault_id)
    clock.now += first.idle_seconds + 1
    with pytest.raises(NoSubject):
        first.for_subject("someone-else")
    first.close()
    second = replica(site, tmp_path / "two")
    with second.using_subject(vault_id) as vault:
        assert vault.index.note("Projects/kopian.md") is not None
    second.close()


@pytest.mark.anyio
async def test_deleting_a_vault_deletes_its_snapshot(site: Hosted, tmp_path: Path):
    vault_id = embedded_on_first_replica(site, tmp_path)
    sealed = SealedVaults(site.store, VaultKeys(site.store, site.keyring))
    hosted_vault = site.store.vault(vault_id)
    assert hosted_vault is not None
    delete_vault(sealed, hosted_vault, tmp_path / "one")
    assert snapshot_objects(site, vault_id) == []
    assert IndexSnapshots(sealed).save(vault_id, tmp_path / "one" / "gone.sqlite") is False


@pytest.mark.anyio
async def test_without_the_setting_no_snapshot_is_written_or_read(storage: Path, tmp_path: Path):
    vaults = SubjectVaults(cache=tmp_path / "cache")
    vaults.embedder = CountingEmbedder()
    async with hosted(vaults, tmp_path) as site:
        await site.member("ann@example.com", sub="sub-ann")
        vault_id = site.vault_of("ann@example.com")
        with vaults.using_subject(vault_id) as vault:
            vault.backend.put("Projects/kopian.md", NOTE)
            vault.sync(force=True)
            assert vault.settled is None
        assert vaults.snapshots is None
        vaults.close()
        assert snapshot_objects(site, vault_id) == []
