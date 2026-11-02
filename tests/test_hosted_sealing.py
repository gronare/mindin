from __future__ import annotations

import base64
import io
import os
import shutil
import sqlite3
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import boto3
import httpx2
import pytest
from hosted_support import NOTE_BODY, Hosted, call_tool, csrf_of, hosted, hosted_config, key_for_subject, text_of
from moto import mock_aws

from notes_vault_mcp.auth.hosted import build_hosted_app
from notes_vault_mcp.auth.hosted_access import SupportAccess
from notes_vault_mcp.auth.hosted_keys import Keyring, MasterKey, SealedVaults, VaultKeys
from notes_vault_mcp.auth.hosted_store import HostedStore
from notes_vault_mcp.auth.hosted_transfer import export_zip
from notes_vault_mcp.backends import VaultError
from notes_vault_mcp.backends.local import LocalBackend
from notes_vault_mcp.backends.sealed import MAGIC, SealedBackend, Unreadable
from notes_vault_mcp.cli import main
from notes_vault_mcp.config import ConfigError
from notes_vault_mcp.vault import SingleVault, SubjectVaults, Vault

BUCKET = "hosted-vaults"
ENDPOINT = "https://s3.amazonaws.com"
DAV_NOTE = b"---\ntitle: Planen\n---\n\nTopphemlig plan for hosten.\n"
PLAINTEXT_NAMES = ("Areas", "secret", "Projects", "plan-x", "Welcome", "schema", ".vault", ".base", "Bilagor", ".md")
PLAINTEXT_BODIES = (b"Hemligt", b"Topphemlig", b"title:", b"stale_after_days", b"Welcome", b"languages")


def basic(secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"ann@example.com:{secret}".encode()).decode()}


def files_of(backend) -> dict[str, bytes]:
    return {entry.key: backend.get_bytes(entry.key)[0] for entry in backend.list_all() if not entry.is_dir}


def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}


def folder_files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def site_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[Hosted], None]:
    def point_cli_at(site: Hosted) -> None:
        monkeypatch.setenv("VAULT_AUTH_DIR", str(site.config.auth_dir))
        monkeypatch.setenv("VAULT_HOSTED_MASTER_KEY", site.keyring.current.encoded())
        monkeypatch.delenv("VAULT_HOSTED_MASTER_KEY_PREVIOUS", raising=False)

    return point_cli_at


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path, site_env: Callable[[Hosted], None]):
    async with hosted(vaults, tmp_path) as running:
        site_env(running)
        yield running


@pytest.fixture(params=["local", "s3"])
def backing(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[], dict[str, bytes]]]:
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    for name in ("VAULT_SCHEMA", "S3_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    if request.param == "local":
        root = tmp_path / "store"
        monkeypatch.setenv("VAULT_PATH", str(root))
        for name in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET"):
            monkeypatch.delenv(name, raising=False)
        yield lambda: folder_files(root)
        return
    monkeypatch.delenv("VAULT_PATH", raising=False)
    for name, value in (
        ("S3_ENDPOINT", ENDPOINT),
        ("S3_ACCESS_KEY", "k"),
        ("S3_SECRET_KEY", "s"),
        ("S3_BUCKET", BUCKET),
    ):
        monkeypatch.setenv(name, value)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)

        def objects() -> dict[str, bytes]:
            listed = client.list_objects_v2(Bucket=BUCKET).get("Contents", [])
            return {obj["Key"]: client.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read() for obj in listed}

        yield objects


@pytest.mark.anyio
async def test_writes_through_mcp_and_webdav_leave_no_plaintext_in_storage(
    backing: Callable[[], dict[str, bytes]], tmp_path: Path
):
    vaults = SubjectVaults(cache=tmp_path / "cache", prefix="users", dav_url="http://localhost/dav/")
    async with hosted(vaults, tmp_path) as site:
        http = await site.member("ann@example.com", sub="sub-ann")
        key = site.key_for("ann@example.com")
        async with http:
            written = await call_tool(http, key, "write_file", {"path": "Areas/secret.md", "content": NOTE_BODY})
            assert not written.get("isError"), text_of(written)
            assert (await http.request("MKCOL", "/dav/vault/Bilagor/", headers=basic(key))).status_code == 201
            put = await http.put("/dav/vault/Projects/plan-x.md", content=DAV_NOTE, headers=basic(key))
            assert put.status_code == 201
            found = await call_tool(http, key, "search", {"query": "Hemligt"})
            got = await http.get("/dav/vault/Areas/secret.md", headers=basic(key))
    vaults.close()
    assert "Areas/secret.md" in text_of(found)
    assert b"Hemligt" in got.content
    raw = backing()
    assert len(raw) >= 9
    for name, blob in raw.items():
        assert not any(secret in name for secret in (*PLAINTEXT_NAMES, "sub-ann", "ann@example")), name
        assert blob.startswith(MAGIC), name
        assert not any(secret in blob for secret in PLAINTEXT_BODIES), name


@pytest.mark.anyio
async def test_a_hosted_vault_is_always_sealed_and_cannot_be_switched_to_plain(site: Hosted, vaults: SubjectVaults):
    await site.member("ann@example.com", sub="sub-ann")
    assert isinstance(vaults.for_subject(site.vault_of("ann@example.com")).backend, SealedBackend)
    with pytest.raises(VaultError):
        vaults.use_backends(vaults.plain_backend)


def test_hosted_refuses_to_start_without_a_master_key(
    vaults: SubjectVaults, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("VAULT_HOSTED_MASTER_KEY", raising=False)
    with pytest.raises(ConfigError, match="VAULT_HOSTED_MASTER_KEY"):
        build_hosted_app(vaults, hosted_config(tmp_path))
    monkeypatch.setenv("VAULT_HOSTED_MASTER_KEY", "not base64!")
    with pytest.raises(ConfigError, match="not base64"):
        build_hosted_app(vaults, hosted_config(tmp_path))


def test_hosted_refuses_a_single_unsealed_vault(vault: Vault, tmp_path: Path):
    with pytest.raises(ConfigError, match="sealed"):
        build_hosted_app(SingleVault(vault), hosted_config(tmp_path), keyring=Keyring(MasterKey.generate()))


@pytest.mark.anyio
async def test_the_account_downloads_its_whole_vault_as_a_zip(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    key = site.key_for("ann@example.com")
    async with http:
        await http.put("/dav/vault/Bilagor/bild.png", content=bytes(range(256)), headers=basic(key))
        response = await http.get("/account/vaults/ann/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    exported = unzip(response.content)
    assert exported == files_of(vaults.for_subject(site.vault_of("ann@example.com")).backend)
    assert exported["Bilagor/bild.png"] == bytes(range(256))
    assert {".vault/schema.yml", "Welcome.md"} <= exported.keys()


@pytest.mark.anyio
async def test_export_needs_a_session(site: Hosted):
    async with site.browser() as http:
        response = await http.get("/account/vaults/ann/export")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/signin")


def zipped(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


async def upload(http: httpx2.AsyncClient, archive: bytes) -> httpx2.Response:
    csrf = csrf_of((await http.get("/account/vaults/ann")).text)
    return await http.post(
        "/account/vaults/ann/import", data={"csrf": csrf}, files={"archive": ("vault.zip", archive, "application/zip")}
    )


@pytest.mark.anyio
async def test_a_zip_upload_imports_files_and_a_second_upload_changes_nothing(
    site: Hosted, vaults: SubjectVaults, storage: Path
):
    http = await site.member("ann@example.com", sub="sub-ann")
    files = {"Areas/imported.md": NOTE_BODY.encode(), ".obsidian/app.json": b"{}", "Bilagor/a.bin": b"\x00\xff"}
    async with http:
        first = await upload(http, zipped(files))
        raw = folder_files(storage)
        second = await upload(http, zipped(files))
    assert first.status_code == 200, first.text
    assert "3 files written" in first.text
    assert "0 files written, 3 unchanged" in second.text
    assert folder_files(storage) == raw
    held = files_of(vaults.for_subject(site.vault_of("ann@example.com")).backend)
    assert all(held[name] == data for name, data in files.items())


@pytest.mark.anyio
async def test_a_zip_with_a_climbing_path_is_refused_whole(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    async with http:
        before = files_of(vaults.for_subject(site.vault_of("ann@example.com")).backend)
        refused = await upload(http, zipped({"Areas/fine.md": b"fine", "../evil.md": b"evil"}))
        garbage = await upload(http, b"not a zip")
    assert refused.status_code == 400
    assert garbage.status_code == 400
    assert files_of(vaults.for_subject(site.vault_of("ann@example.com")).backend) == before


@pytest.mark.anyio
async def test_the_upload_needs_the_pages_csrf_token(site: Hosted):
    http = await site.member("ann@example.com", sub="sub-ann")
    async with http:
        response = await http.post(
            "/account/vaults/ann/import",
            data={"csrf": "wrong"},
            files={"archive": ("v.zip", zipped({}), "application/zip")},
        )
    assert response.status_code == 403


async def post_account(http: httpx2.AsyncClient, path: str, **fields: str) -> httpx2.Response:
    csrf = csrf_of((await http.get("/account/settings")).text)
    return await http.post(path, data={"csrf": csrf, **fields})


@pytest.mark.anyio
async def test_support_opens_a_vault_only_under_a_live_grant_and_every_access_is_logged(
    site: Hosted, capsys: pytest.CaptureFixture[str]
):
    http = await site.member("ann@example.com", sub="sub-ann")
    key = site.key_for("ann@example.com")
    support = ["--operator", "carl", "--reason", "ticket 4711"]
    async with http:
        written = await call_tool(http, key, "write_file", {"path": "Areas/secret.md", "content": NOTE_BODY})
        assert not written.get("isError")
        assert main(["support", "files", "ann@example.com", *support]) == 1
        assert "refused" in capsys.readouterr().err
        assert "Nobody from support has opened your vaults" in (await http.get("/account/settings")).text
        assert (await post_account(http, "/account/support", vault="ann", duration="day")).status_code == 303
        assert main(["support", "files", "ann@example.com", *support]) == 0
        assert "Areas/secret.md" in capsys.readouterr().out.splitlines()
        assert main(["support", "read", "ann@example.com", "Areas/secret.md", *support]) == 0
        assert "Hemligt" in capsys.readouterr().out
        page = (await http.get("/account/settings")).text
        assert (await post_account(http, "/account/support/end", vault="ann")).status_code == 303
        assert main(["support", "read", "ann@example.com", "Areas/secret.md", *support]) == 1
    assert "Personal: open to support until" in page
    assert page.count("ticket 4711") == 2
    assert "read Areas/secret.md" in page and "list files" in page and "carl" in page
    account = site.store.account("ann@example.com")
    assert len(SupportAccess(site.store).log(account.id)) == 2


@pytest.mark.anyio
async def test_a_grant_expires_and_a_reason_is_required(site: Hosted):
    await site.member("ann@example.com", sub="sub-ann")
    account = site.store.account("ann@example.com")
    vault = site.store.vault(site.vault_of("ann@example.com"))
    access = SupportAccess(site.store, clock=lambda: site.clock.now)
    access.grant(vault, 3600)
    with pytest.raises(PermissionError):
        access.admit(vault, "carl", "  ", "list files")
    access.admit(vault, "carl", "ticket", "list files")
    site.clock.advance(3601)
    with pytest.raises(PermissionError):
        access.admit(vault, "carl", "ticket", "list files")
    assert len(access.log(account.id)) == 1


@pytest.mark.anyio
async def test_a_deleted_accounts_notes_are_unreadable_even_with_the_master_key(
    site: Hosted, vaults: SubjectVaults, storage: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    http = await site.member("ann@example.com", sub="sub-ann")
    key = site.key_for("ann@example.com")
    async with http:
        await call_tool(http, key, "write_file", {"path": "Areas/secret.md", "content": NOTE_BODY})
    account = site.store.account("ann@example.com")
    vault_id = site.vault_of("ann@example.com")
    vaults.close()
    snapshot = tmp_path / "snapshot"
    shutil.copytree(storage, snapshot)
    assert main(["accounts", "delete", "ann@example.com"]) == 2
    assert main(["accounts", "delete", "ann@example.com", "--yes"]) == 0
    assert "deleted ann@example.com" in capsys.readouterr().out
    assert not (storage / "users" / vault_id).exists()
    assert not list((tmp_path / "cache").glob("*.sqlite*"))
    assert site.store.account(account.id) is None
    with sqlite3.connect(site.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM vault_data_keys").fetchone()[0] == 0
    shutil.copytree(snapshot, storage, dirs_exist_ok=True)
    keys = VaultKeys(site.store, site.keyring)
    assert not keys.has_key(vault_id)
    revived = SealedBackend(LocalBackend(storage / "users" / vault_id), os.urandom(32))
    assert revived.list_all() == []
    blobs = folder_files(storage / "users" / vault_id).values()
    assert blobs
    for blob in blobs:
        with pytest.raises(Unreadable):
            revived.unseal("Areas/secret.md", blob)


@pytest.mark.anyio
async def test_rotating_the_master_key_keeps_every_vault_readable(
    site: Hosted, vaults: SubjectVaults, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    for name in ("ann", "bo"):
        http = await site.member(f"{name}@example.com", sub=f"sub-{name}")
        key = site.key_for(f"{name}@example.com")
        async with http:
            await call_tool(http, key, "write_file", {"path": f"Areas/{name}.md", "content": NOTE_BODY})
    vaults.close()
    old, new = site.keyring.current, MasterKey.generate()
    monkeypatch.setenv("VAULT_HOSTED_MASTER_KEY", new.encoded())
    monkeypatch.setenv("VAULT_HOSTED_MASTER_KEY_PREVIOUS", old.encoded())
    assert main(["master-key", "rewrap"]) == 0
    assert "rewrapped 2 vault keys" in capsys.readouterr().out
    monkeypatch.delenv("VAULT_HOSTED_MASTER_KEY_PREVIOUS")
    assert main(["master-key", "status"]) == 0
    assert f"{new.key_id}  2 vault keys  current" in capsys.readouterr().out
    store = HostedStore(site.config.auth_dir)
    rotated = SealedVaults(store, VaultKeys(store, Keyring(new)))
    for name in ("ann", "bo"):
        backend, _ = rotated.for_vault(site.vault_of(f"{name}@example.com"))
        assert "Hemligt" in backend.get(f"Areas/{name}.md")[0]
    with pytest.raises(ConfigError, match=new.key_id):
        build_hosted_app(SubjectVaults(cache=tmp_path / "other"), site.config, keyring=Keyring(old))


@pytest.fixture
def account_env(storage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[HostedStore, str]:
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setenv("VAULT_HOSTED_MASTER_KEY", MasterKey.generate().encoded())
    key_for_subject(tmp_path / "auth", "ann@example.com", "a1b2c3")
    store = HostedStore(tmp_path / "auth")
    return store, store.account("ann@example.com").id


def sealed_target(store: HostedStore, account_id: str) -> SealedBackend:
    vault = store.first_vault(account_id)
    return SealedVaults(store, VaultKeys(store, Keyring.from_env())).for_vault(vault.id)[0]


def grant_first_vault(store: HostedStore, account_id: str) -> None:
    SupportAccess(store).grant(store.first_vault(account_id), 3600)


IMPORT = ["accounts", "import", "ann@example.com", "--operator", "carl", "--reason", "moving my vault in"]


def test_importing_a_folder_seals_every_file_and_the_export_matches_byte_for_byte(
    account_env: tuple[HostedStore, str], vault_dir: Path, storage: Path, capsys: pytest.CaptureFixture[str]
):
    store, account_id = account_env
    grant_first_vault(store, account_id)
    source = folder_files(vault_dir)
    assert main([*IMPORT, "--from-path", str(vault_dir)]) == 0
    assert capsys.readouterr().out.strip().endswith(f"{len(source)} files written, 0 unchanged, 0 folders made")
    exported = io.BytesIO()
    export_zip(sealed_target(store, account_id), exported)
    assert unzip(exported.getvalue()) == source
    raw = folder_files(storage)
    assert not any(b"title:" in blob for blob in raw.values())
    assert main([*IMPORT, "--from-path", str(vault_dir)]) == 0
    assert f"0 files written, {len(source)} unchanged" in capsys.readouterr().out
    assert folder_files(storage) == raw


def test_importing_from_a_bucket_preserves_paths_and_bytes(
    account_env: tuple[HostedStore, str], vault_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    store, account_id = account_env
    grant_first_vault(store, account_id)
    source = folder_files(vault_dir)
    monkeypatch.setenv("IMPORT_S3_ACCESS_KEY", "k")
    monkeypatch.setenv("IMPORT_S3_SECRET_KEY", "s")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="old-vault")
        for name, data in source.items():
            client.put_object(Bucket="old-vault", Key=f"carl/{name}", Body=data)
        bucket = ["--from-s3-bucket", "old-vault", "--from-s3-prefix", "carl", "--from-s3-endpoint", ENDPOINT]
        assert main([*IMPORT, *bucket]) == 0
    assert files_of(sealed_target(store, account_id)) == source


def test_import_is_refused_without_the_accounts_grant(
    account_env: tuple[HostedStore, str], vault_dir: Path, storage: Path, capsys: pytest.CaptureFixture[str]
):
    store, account_id = account_env
    assert main([*IMPORT, "--from-path", str(vault_dir)]) == 1
    assert "refused" in capsys.readouterr().err
    assert not folder_files(storage)
    assert SupportAccess(store).log(account_id) == []


@pytest.mark.anyio
async def test_the_data_key_is_shredded_before_any_object_is_deleted(
    site: Hosted, vaults: SubjectVaults, monkeypatch: pytest.MonkeyPatch
):
    from notes_vault_mcp.auth.hosted_keys import delete_account

    await site.member("ann@example.com", sub="sub-ann")
    vaults.for_subject(site.vault_of("ann@example.com"))
    vaults.close()
    account = site.store.account("ann@example.com")
    vault_id = site.vault_of("ann@example.com")
    keys = VaultKeys(site.store, site.keyring)
    assert keys.has_key(vault_id)

    def broken(self, key: str) -> None:
        raise OSError("storage is down")

    monkeypatch.setattr(LocalBackend, "delete", broken)
    with pytest.raises(OSError):
        delete_account(SealedVaults(site.store, keys), account, site.config.auth_dir)
    assert not keys.has_key(vault_id)
