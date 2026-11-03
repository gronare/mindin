from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from notes_vault_mcp.backends import FETCH_WORKERS, MUST_NOT_EXIST, NotFound, VersionConflict, delete_checked
from notes_vault_mcp.backends.local import LocalBackend
from notes_vault_mcp.backends.s3 import S3Backend

BUCKET = "vault-test"
ENDPOINT = "https://s3.amazonaws.com"


def test_local_list_skips_obsidian_and_keeps_schema(vault_dir: Path):
    keys = {entry.key for entry in LocalBackend(vault_dir).list()}
    assert ".vault/schema.yml" in keys
    assert "Areas/orchard.md" in keys
    assert not any(key.startswith(".obsidian/") for key in keys)


def test_local_get_returns_text_and_version(vault_dir: Path):
    text, version = LocalBackend(vault_dir).get("Areas/orchard.md")
    assert text.startswith("---")
    assert ":" in version


def test_local_get_missing_raises_not_found(vault_dir: Path):
    with pytest.raises(NotFound):
        LocalBackend(vault_dir).get("Areas/nope.md")


def test_local_put_returns_a_new_version(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    _, before = backend.get("Areas/orchard.md")
    after = backend.put("Areas/orchard.md", "changed")
    assert after != before
    assert backend.get("Areas/orchard.md")[0] == "changed"


def test_local_put_with_a_stale_version_conflicts(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    _, version = backend.get("Areas/orchard.md")
    backend.put("Areas/orchard.md", "someone else wrote")
    with pytest.raises(VersionConflict):
        backend.put("Areas/orchard.md", "mine", expected_version=version)


def test_local_move_and_delete(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    backend.move("Areas/orchard.md", "Archive/orchard.md")
    assert backend.get("Archive/orchard.md")[0].startswith("---")
    backend.delete("Archive/orchard.md")
    with pytest.raises(NotFound):
        backend.get("Archive/orchard.md")


@pytest.fixture
def s3_backend():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        backend = S3Backend(ENDPOINT, "key", "secret", BUCKET, prefix="vault")
        backend.put("Areas/orchard.md", "---\ntitle: orchard\n---\n\nbody\n")
        backend.put("Notes/skip.txt", "not a note")
        yield backend


def test_the_s3_client_pools_a_connection_per_fetch_worker(s3_backend: S3Backend):
    assert s3_backend.client.meta.config.max_pool_connections == FETCH_WORKERS


def test_s3_list_only_returns_vault_keys(s3_backend: S3Backend):
    assert [entry.key for entry in s3_backend.list()] == ["Areas/orchard.md"]


def test_s3_list_strips_the_prefix_from_keys(s3_backend: S3Backend):
    raw = s3_backend.client.list_objects_v2(Bucket=BUCKET)["Contents"]
    assert {obj["Key"] for obj in raw} == {"vault/Areas/orchard.md", "vault/Notes/skip.txt"}


def test_s3_get_returns_the_etag_as_version(s3_backend: S3Backend):
    text, version = s3_backend.get("Areas/orchard.md")
    assert "orchard" in text
    assert '"' not in version and len(version) == 32


def test_s3_get_missing_raises_not_found(s3_backend: S3Backend):
    with pytest.raises(NotFound):
        s3_backend.get("Areas/nope.md")


def test_s3_put_with_a_stale_etag_conflicts(s3_backend: S3Backend):
    _, version = s3_backend.get("Areas/orchard.md")
    s3_backend.put("Areas/orchard.md", "someone else wrote")
    with pytest.raises(VersionConflict):
        s3_backend.put("Areas/orchard.md", "mine", expected_version=version)


def test_s3_put_with_the_current_etag_succeeds(s3_backend: S3Backend):
    _, version = s3_backend.get("Areas/orchard.md")
    s3_backend.put("Areas/orchard.md", "mine", expected_version=version)
    assert s3_backend.get("Areas/orchard.md")[0] == "mine"


def test_s3_move_and_delete(s3_backend: S3Backend):
    s3_backend.move("Areas/orchard.md", "Archive/orchard.md")
    assert s3_backend.get("Archive/orchard.md")[0].startswith("---")
    with pytest.raises(NotFound):
        s3_backend.get("Areas/orchard.md")
    s3_backend.delete("Archive/orchard.md")
    assert s3_backend.list() == []


def test_s3_list_all_returns_every_object_and_directory_markers(s3_backend: S3Backend):
    s3_backend.mkdir("Attachments/")
    s3_backend.put_bytes("Attachments/pic.png", b"\x89PNG", "image/png")
    keys = [entry.key for entry in s3_backend.list_all()]
    assert keys == ["Areas/orchard.md", "Attachments/", "Attachments/pic.png", "Notes/skip.txt"]
    assert [entry.key for entry in s3_backend.list_all("Attachments/")] == ["Attachments/", "Attachments/pic.png"]
    assert next(entry for entry in s3_backend.list_all() if entry.key == "Attachments/").is_dir


def test_s3_bytes_round_trip_with_a_content_type(s3_backend: S3Backend):
    version = s3_backend.put_bytes("Attachments/pic.png", bytes(range(256)))
    data, read_version = s3_backend.get_bytes("Attachments/pic.png")
    assert data == bytes(range(256))
    assert read_version == version
    head = s3_backend.client.head_object(Bucket=BUCKET, Key="vault/Attachments/pic.png")
    assert head["ContentType"] == "image/png"


def test_s3_get_bytes_missing_raises_not_found(s3_backend: S3Backend):
    with pytest.raises(NotFound):
        s3_backend.get_bytes("Attachments/nope.png")


def test_local_list_all_includes_directories_and_non_notes(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    backend.put_bytes("Attachments/pic.png", b"png")
    keys = [entry.key for entry in backend.list_all()]
    assert "Attachments/" in keys and "Attachments/pic.png" in keys
    assert ".obsidian/" in keys
    assert [entry.key for entry in backend.list_all("Attachments/")] == ["Attachments/", "Attachments/pic.png"]


def test_local_delete_removes_an_empty_directory_and_refuses_a_full_one(vault_dir: Path):
    from notes_vault_mcp.backends import VaultError

    backend = LocalBackend(vault_dir)
    backend.put_bytes("Tmp/a/b.txt", b"x")
    with pytest.raises(VaultError):
        backend.delete("Tmp/")
    assert (vault_dir / "Tmp" / "a" / "b.txt").is_file()
    backend.delete("Tmp/a/b.txt")
    backend.delete("Tmp/a/")
    backend.delete("Tmp/")
    assert not (vault_dir / "Tmp").exists()
    with pytest.raises(VaultError):
        backend.delete("")


def test_make_subject_backend_nests_the_subject_under_the_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from notes_vault_mcp.backends import make_subject_backend

    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    for name in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    backend, identifier = make_subject_backend("1042", "users")
    assert isinstance(backend, LocalBackend)
    assert backend.root == (tmp_path / "users" / "1042").resolve()
    other, other_identifier = make_subject_backend("1187", "users")
    assert identifier != other_identifier


def test_make_subject_backend_extends_the_s3_prefix(monkeypatch: pytest.MonkeyPatch):
    from notes_vault_mcp.backends import make_subject_backend

    monkeypatch.delenv("VAULT_PATH", raising=False)
    monkeypatch.setenv("S3_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("S3_ACCESS_KEY", "key")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    monkeypatch.setenv("S3_PREFIX", "vault")
    with mock_aws():
        backend, _ = make_subject_backend("1042", "users")
    assert isinstance(backend, S3Backend)
    assert backend.prefix == "vault/users/1042/"


def test_local_list_all_walks_only_what_the_prefix_names(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    backend.put("Areas.base", "views: []\n")
    under = {entry.key for entry in backend.list_all("Areas/")}
    named = {entry.key for entry in backend.list_all("Areas")}
    assert "Areas/" in under and "Areas/orchard.md" in under and "Areas.base" not in under
    assert {"Areas/", "Areas/orchard.md", "Areas.base"} <= named
    assert not any(key.startswith("Projects") for key in under | named)
    assert backend.list_all("Nope/") == []
    assert {entry.key for entry in backend.list_all("")} >= under | named


def test_s3_conditional_put_sends_if_match_and_never_heads_first(s3_backend: S3Backend, monkeypatch):
    _, version = s3_backend.get("Areas/orchard.md")
    monkeypatch.setattr(s3_backend, "current_version", lambda key: pytest.fail("a conditional put needs no HEAD"))
    s3_backend.put("Areas/orchard.md", "mine", expected_version=version)
    with pytest.raises(VersionConflict):
        s3_backend.put("Areas/orchard.md", "again", expected_version=version)
    assert s3_backend.get("Areas/orchard.md")[0] == "mine"


def test_s3_create_only_put_refuses_an_existing_key(s3_backend: S3Backend):
    with pytest.raises(VersionConflict):
        s3_backend.put("Areas/orchard.md", "clobber", expected_version=MUST_NOT_EXIST)
    s3_backend.put("Areas/new.md", "fresh", expected_version=MUST_NOT_EXIST)
    assert s3_backend.get("Areas/new.md")[0] == "fresh"


def test_s3_without_conditional_puts_falls_back_to_checking_the_etag_and_says_so_once(
    s3_backend: S3Backend, monkeypatch, caplog
):
    original = s3_backend.client.put_object

    def refusing(**kwargs):
        if "IfMatch" in kwargs or "IfNoneMatch" in kwargs:
            raise ClientError({"Error": {"Code": "NotImplemented", "Message": "no"}}, "PutObject")
        return original(**kwargs)

    monkeypatch.setattr(s3_backend.client, "put_object", refusing)
    _, version = s3_backend.get("Areas/orchard.md")
    with caplog.at_level("WARNING"):
        version = s3_backend.put("Areas/orchard.md", "mine", expected_version=version)
        s3_backend.put("Areas/orchard.md", "again", expected_version=version)
        with pytest.raises(VersionConflict):
            s3_backend.put("Areas/orchard.md", "stale", expected_version=version)
        with pytest.raises(VersionConflict):
            s3_backend.put("Areas/orchard.md", "clobber", expected_version=MUST_NOT_EXIST)
    assert s3_backend.get("Areas/orchard.md")[0] == "again"
    assert [record.message for record in caplog.records].count(
        "notes-vault-mcp: this S3 server refuses conditional PUTs; checking the etag before each write"
    ) == 1


def test_local_create_only_put_refuses_an_existing_key(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    with pytest.raises(VersionConflict):
        backend.put("Areas/orchard.md", "clobber", expected_version=MUST_NOT_EXIST)
    backend.put("Areas/brand-new.md", "fresh", expected_version=MUST_NOT_EXIST)


def test_s3_put_over_a_version_of_a_key_deleted_meanwhile_conflicts(s3_backend: S3Backend):
    _, version = s3_backend.get("Areas/orchard.md")
    s3_backend.delete("Areas/orchard.md")
    with pytest.raises(VersionConflict):
        s3_backend.put("Areas/orchard.md", "mine", expected_version=version)
    with pytest.raises(NotFound):
        s3_backend.get("Areas/orchard.md")


def test_s3_put_over_a_version_answered_with_no_such_key_conflicts(s3_backend: S3Backend, monkeypatch):
    def missing(**kwargs):
        raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "gone"}}, "PutObject")

    monkeypatch.setattr(s3_backend.client, "put_object", missing)
    with pytest.raises(VersionConflict):
        s3_backend.put("Areas/orchard.md", "mine", expected_version="an-old-etag")


def test_s3_checked_delete_sends_if_match_and_refuses_a_changed_key(s3_backend: S3Backend, monkeypatch):
    _, version = s3_backend.get("Areas/orchard.md")
    sent: list[dict] = []
    original = s3_backend.client.delete_object

    def conditional(**kwargs):
        sent.append(kwargs)
        if kwargs.get("IfMatch") != f'"{s3_backend.current_version("Areas/orchard.md")}"':
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "no"}}, "DeleteObject")
        return original(**kwargs)

    monkeypatch.setattr(s3_backend.client, "delete_object", conditional)
    with pytest.raises(VersionConflict):
        s3_backend.delete_checked("Areas/orchard.md", "an-old-etag")
    assert s3_backend.get("Areas/orchard.md")[1] == version
    s3_backend.delete_checked("Areas/orchard.md", version)
    assert [call["IfMatch"] for call in sent] == ['"an-old-etag"', f'"{version}"']
    with pytest.raises(NotFound):
        s3_backend.get("Areas/orchard.md")


def test_s3_without_conditional_deletes_checks_the_etag_first(s3_backend: S3Backend, monkeypatch):
    original = s3_backend.client.delete_object

    def refusing(**kwargs):
        if "IfMatch" in kwargs:
            raise ClientError({"Error": {"Code": "NotImplemented", "Message": "no"}}, "DeleteObject")
        return original(**kwargs)

    monkeypatch.setattr(s3_backend.client, "delete_object", refusing)
    _, version = s3_backend.get("Areas/orchard.md")
    with pytest.raises(VersionConflict):
        s3_backend.delete_checked("Areas/orchard.md", "an-old-etag")
    s3_backend.delete_checked("Areas/orchard.md", version)
    with pytest.raises(NotFound):
        s3_backend.get("Areas/orchard.md")


def test_local_checked_delete_refuses_a_changed_file(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    _, old = backend.get("Areas/orchard.md")
    backend.put("Areas/orchard.md", "changed meanwhile")
    _, new = backend.get("Areas/orchard.md")
    with pytest.raises(VersionConflict):
        backend.delete_checked("Areas/orchard.md", old)
    backend.delete_checked("Areas/orchard.md", new)
    with pytest.raises(NotFound):
        backend.get("Areas/orchard.md")


def test_a_backend_without_checked_deletes_still_refuses_a_changed_file(vault_dir: Path):
    class Plain:
        def __init__(self, inner: LocalBackend) -> None:
            self.inner = inner

        def get_bytes(self, key: str) -> tuple[bytes, str]:
            return self.inner.get_bytes(key)

        def delete(self, key: str) -> None:
            self.inner.delete(key)

    plain = Plain(LocalBackend(vault_dir))
    _, version = plain.inner.get("Areas/orchard.md")
    with pytest.raises(VersionConflict):
        delete_checked(plain, "Areas/orchard.md", "an-old-version")
    delete_checked(plain, "Areas/orchard.md", version)
    with pytest.raises(NotFound):
        plain.inner.get("Areas/orchard.md")
