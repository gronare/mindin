from __future__ import annotations

import base64
import os
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from mindin.backends import MUST_NOT_EXIST, NotFound, VaultError, VersionConflict
from mindin.backends.local import LocalBackend
from mindin.backends.s3 import S3Backend
from mindin.backends.sealed import LISTING_TTL, MAX_PATH_BYTES, SealedBackend, Unreadable
from mindin.vault import SubjectVaults, open_vault

BUCKET = "sealed-test"
NOTE = "---\ntitle: Kvartalsplan\n---\n\nTopphemlig kvartalsplan.\n"


def raw_files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


class Counting:
    def __init__(self, inner: LocalBackend) -> None:
        self.inner = inner
        self.fetches = 0
        self.listings = 0

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        self.fetches += 1
        return self.inner.get_bytes(key)

    def list_all(self, prefix: str = "") -> list:
        self.listings += 1
        return self.inner.list_all(prefix)


@pytest.fixture
def sealed(tmp_path: Path) -> SealedBackend:
    return SealedBackend(LocalBackend(tmp_path / "raw"), os.urandom(32))


def test_contents_and_names_round_trip_and_leave_nothing_readable(sealed: SealedBackend, tmp_path: Path):
    version = sealed.put("Projects/kvartalsplan.md", NOTE)
    sealed.put_bytes("Bilagor/kvitto.png", bytes(range(256)))
    text, read_version = sealed.get("Projects/kvartalsplan.md")
    assert (text, read_version) == (NOTE, version)
    assert sealed.get_bytes("Bilagor/kvitto.png")[0] == bytes(range(256))
    raw = raw_files(tmp_path / "raw")
    for secret in ("Projects", "kvartalsplan", "Bilagor", "kvitto", ".md", ".png"):
        assert not any(secret in name for name in raw), secret
    assert not any(b"Topphemlig" in blob or b"Kvartalsplan" in blob for blob in raw.values())


def test_swapping_two_blobs_fails_to_decrypt(sealed: SealedBackend, tmp_path: Path):
    sealed.put("a.md", "first")
    sealed.put("b.md", "second")
    a, b = (tmp_path / "raw" / sealed.object_key(key) for key in ("a.md", "b.md"))
    first, second = a.read_bytes(), b.read_bytes()
    a.write_bytes(second)
    b.write_bytes(first)
    with pytest.raises(Unreadable):
        sealed.get("a.md")
    with pytest.raises(Unreadable):
        sealed.get("b.md")


def test_another_vaults_key_reads_neither_names_nor_contents(sealed: SealedBackend, tmp_path: Path):
    sealed.put("a.md", "mine")
    stranger = SealedBackend(LocalBackend(tmp_path / "raw"), os.urandom(32))
    assert stranger.list_all() == []
    blob = (tmp_path / "raw" / sealed.object_key("a.md")).read_bytes()
    stranger.inner.put_bytes(stranger.object_key("a.md"), blob)
    with pytest.raises(Unreadable):
        stranger.get("a.md")


def test_listing_recovers_paths_sizes_and_etags_without_fetching(tmp_path: Path):
    counting = Counting(LocalBackend(tmp_path / "raw"))
    sealed = SealedBackend(counting, os.urandom(32))
    version = sealed.put("Areas/orchard.md", NOTE)
    sealed.put(".vault/schema.yml", "languages: [sv]\n")
    sealed.put_bytes(".obsidian/app.json", b"{}")
    sealed.mkdir("Empty/")
    counting.fetches = 0
    entries = {entry.key: entry for entry in sealed.list_all()}
    assert set(entries) == {"Areas/orchard.md", ".vault/schema.yml", ".obsidian/app.json", "Empty/"}
    assert entries["Areas/orchard.md"].version == version
    assert entries["Areas/orchard.md"].size == len(NOTE.encode())
    assert entries["Empty/"].is_dir and entries["Empty/"].size == 0
    assert [entry.key for entry in sealed.list()] == [".vault/schema.yml", "Areas/orchard.md"]
    assert [entry.key for entry in sealed.list_all("Areas")] == ["Areas/orchard.md"]
    assert counting.fetches == 0


def test_conditional_writes_still_conflict(sealed: SealedBackend):
    version = sealed.put("a.md", "one", expected_version=MUST_NOT_EXIST)
    with pytest.raises(VersionConflict):
        sealed.put("a.md", "two", expected_version=MUST_NOT_EXIST)
    sealed.put("a.md", "someone else")
    with pytest.raises(VersionConflict):
        sealed.put("a.md", "mine", expected_version=version)


def test_move_reseals_under_the_new_path_and_delete_removes(sealed: SealedBackend):
    sealed.put("Inbox/idea.md", "idea")
    sealed.move("Inbox/idea.md", "Areas/idea.md")
    assert sealed.get("Areas/idea.md")[0] == "idea"
    assert [entry.key for entry in sealed.list_all()] == ["Areas/idea.md"]
    sealed.delete("Areas/idea.md")
    assert sealed.list_all() == []


def test_a_long_path_fits_a_local_filename_and_an_overlong_one_is_refused(sealed: SealedBackend):
    deep = "/".join(["mapp-med-ett-långt-namn"] * 18) + "/note.md"
    assert len(deep.encode()) <= MAX_PATH_BYTES
    sealed.put(deep, "deep")
    assert sealed.get(deep)[0] == "deep"
    assert all(len(segment) <= 255 for segment in sealed.object_key(deep).split("/"))
    with pytest.raises(VaultError):
        sealed.put("x" * (MAX_PATH_BYTES + 1) + ".md", "too long")


def test_on_s3_keys_stay_under_the_limit_and_objects_carry_no_content_type():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        inner = S3Backend("https://s3.amazonaws.com", "key", "secret", BUCKET, prefix="users/acc_0123456789abcdef")
        sealed = SealedBackend(inner, os.urandom(32))
        longest = "p" * (MAX_PATH_BYTES - 3) + ".md"
        sealed.put(longest, NOTE)
        sealed.put_bytes("Bilagor/bild.png", b"\x89PNG", "image/png")
        listed = client.list_objects_v2(Bucket=BUCKET)["Contents"]
        assert all(len(obj["Key"].encode()) <= 1024 for obj in listed)
        for obj in listed:
            head = client.head_object(Bucket=BUCKET, Key=obj["Key"])
            assert head["ContentType"] == "application/octet-stream"
            body = client.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()
            assert b"Topphemlig" not in body and b"PNG" not in body
        assert {entry.key for entry in sealed.list_all()} == {longest, "Bilagor/bild.png"}
        assert sealed.get(longest)[0] == NOTE


def test_storage_is_chosen_before_any_subject_vault_opens(storage: Path, tmp_path: Path):
    vaults = SubjectVaults(cache=tmp_path / "cache")
    vaults.for_subject("ann")
    with pytest.raises(VaultError):
        vaults.use_backends(vaults.plain_backend)
    vaults.close()


def test_a_missing_note_is_named_by_its_path_and_never_by_its_sealed_key(sealed: SealedBackend):
    with pytest.raises(NotFound, match=r"^Projects/nowhere\.md$"):
        sealed.get("Projects/nowhere.md")
    with pytest.raises(NotFound, match=r"^Projects/nowhere\.md$"):
        sealed.move("Projects/nowhere.md", "Projects/b.md")


def test_a_local_vault_still_writes_plaintext(vault_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    vault = open_vault()
    vault.backend.put("Areas/plain.md", NOTE)
    assert not getattr(vault.backend, "sealed", False)
    assert (vault_dir / "Areas" / "plain.md").read_text(encoding="utf-8") == NOTE
    vault.close()


class Ticking:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_folder_walk_lists_the_storage_once(tmp_path: Path):
    counting = Counting(LocalBackend(tmp_path / "raw"))
    sealed = SealedBackend(counting, os.urandom(32), clock=Ticking())
    for path in ("Areas/a.md", "Projects/b.md", "Projects/Deep/c.md", "Inbox/d.md"):
        sealed.put(path, "x")
    counting.listings = 0
    walked = [sealed.list_all(folder) for folder in ("", "Areas/", "Projects/", "Projects/Deep/", "Inbox/")]
    assert [len(found) for found in walked] == [4, 1, 2, 1, 1]
    assert [entry.key for entry in sealed.list()] == ["Areas/a.md", "Inbox/d.md", "Projects/Deep/c.md", "Projects/b.md"]
    assert counting.listings == 1


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda sealed: sealed.put("new.md", "n"), {"a.md", "b.md", "new.md"}),
        (lambda sealed: sealed.put_bytes("new.png", b"n"), {"a.md", "b.md", "new.png"}),
        (lambda sealed: sealed.mkdir("Folder/"), {"a.md", "b.md", "Folder/"}),
        (lambda sealed: sealed.delete("a.md"), {"b.md"}),
        (lambda sealed: sealed.move("a.md", "c.md"), {"b.md", "c.md"}),
    ],
    ids=["put", "put_bytes", "mkdir", "delete", "move"],
)
def test_every_change_through_the_vault_drops_the_cached_listing(tmp_path: Path, change, expected: set[str]):
    sealed = SealedBackend(LocalBackend(tmp_path / "raw"), os.urandom(32), clock=Ticking())
    sealed.put("a.md", "a")
    sealed.put("b.md", "b")
    assert {entry.key for entry in sealed.list_all()} == {"a.md", "b.md"}
    change(sealed)
    assert {entry.key for entry in sealed.list_all()} == expected


def test_a_write_from_elsewhere_shows_once_the_cached_listing_expires(tmp_path: Path):
    key, clock = os.urandom(32), Ticking()
    sealed = SealedBackend(LocalBackend(tmp_path / "raw"), key, clock=clock)
    sealed.put("a.md", "a")
    assert [entry.key for entry in sealed.list_all()] == ["a.md"]
    SealedBackend(LocalBackend(tmp_path / "raw"), key).put("b.md", "b")
    assert [entry.key for entry in sealed.list_all()] == ["a.md"]
    clock.now += LISTING_TTL
    assert [entry.key for entry in sealed.list_all()] == ["a.md", "b.md"]


def test_a_listing_that_raced_a_write_is_not_kept(tmp_path: Path):
    inner = LocalBackend(tmp_path / "raw")
    sealed = SealedBackend(inner, os.urandom(32), clock=Ticking())
    sealed.put("a.md", "a")
    real = inner.list_all

    def listing_then_a_write(prefix: str = "") -> list:
        found = real(prefix)
        inner.list_all = real
        sealed.put("late.md", "late")
        return found

    inner.list_all = listing_then_a_write
    assert [entry.key for entry in sealed.list_all()] == ["a.md"]
    assert [entry.key for entry in sealed.list_all()] == ["a.md", "late.md"]


def test_a_burst_of_writes_after_a_listing_lists_the_storage_once(tmp_path: Path):
    key = os.urandom(32)
    counting = Counting(LocalBackend(tmp_path / "raw"))
    sealed = SealedBackend(counting, key, clock=Ticking())
    sealed.put("Areas/orchard.md", "o")
    sealed.put("Areas/frame.md", "f")
    counting.listings = 0
    assert [entry.key for entry in sealed.list_all()] == ["Areas/frame.md", "Areas/orchard.md"]
    version = ""
    for round in range(5):
        version = sealed.put_checked(f"Inbox/workshop-{round}.md", b"w" * (round + 1))
        sealed.list_all("Inbox/")
    sealed.delete("Areas/frame.md")
    sealed.mkdir("Projects/")
    listed = {entry.key: entry for entry in sealed.list_all()}
    assert counting.listings == 1
    assert set(listed) == {"Areas/orchard.md", "Projects/"} | {f"Inbox/workshop-{round}.md" for round in range(5)}
    assert listed["Inbox/workshop-4.md"].version == version and listed["Inbox/workshop-4.md"].size == 5
    fresh = {entry.key: entry.version for entry in SealedBackend(counting.inner, key).list_all()}
    assert {key: entry.version for key, entry in listed.items()} == fresh


def test_a_failed_write_drops_the_cached_listing(tmp_path: Path):
    counting = Counting(LocalBackend(tmp_path / "raw"))
    sealed = SealedBackend(counting, os.urandom(32), clock=Ticking())
    sealed.put("Areas/orchard.md", "o")
    sealed.list_all()
    counting.listings = 0
    with pytest.raises(VersionConflict):
        sealed.put("Areas/orchard.md", "stale", expected_version="not-the-version")
    sealed.list_all()
    assert counting.listings == 1


def test_a_checked_delete_refuses_a_changed_file_and_removes_an_unchanged_one(sealed: SealedBackend):
    old = sealed.put("Areas/orchard.md", "first")
    new = sealed.put("Areas/orchard.md", "second")
    with pytest.raises(VersionConflict):
        sealed.delete_checked("Areas/orchard.md", old)
    assert sealed.get("Areas/orchard.md")[0] == "second"
    sealed.delete_checked("Areas/orchard.md", new)
    assert sealed.list_all() == []


def test_the_key_contexts_keep_the_former_name_so_existing_vaults_stay_readable():
    from mindin.auth.hosted_history import history_key
    from mindin.auth.hosted_keys import MasterKey, _wrap_context

    key = bytes(range(32))
    sealed = SealedBackend(None, key)
    assert MasterKey(key).key_id == "95eebe68f89ddf3a"
    assert _wrap_context("v1", "k1") == b"notes-vault-mcp wrap v1|v1|k1"
    assert history_key(key).hex() == "936e129df25ad95fe950df8e0b0c0378875808a44c0fca2e098ea8e8290ee97c"
    assert sealed.object_key("Areas/orchard.md") == "67Q9me9hupb49upXE9eVw9wauxET-17aoc7XjmoZkks"
    blob = base64.b64decode("bnZtMeoALf19pS/mWkbbd+cSPxr+iaxQpVG+ZufEUnrRW2Rgv+4/aX2M")
    assert sealed.unseal("Areas/orchard.md", blob) == b"# Orchard\n"
