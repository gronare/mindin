from __future__ import annotations

import argparse
import sys
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from notes_vault_mcp.auth import auth_dir
from notes_vault_mcp.auth.hosted_store import HostedStore
from notes_vault_mcp.backends import VaultBackend, VaultError

if TYPE_CHECKING:
    pass

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
SPOOL_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

Source = Iterator[tuple[str, Callable[[], bytes] | None]]


class BadArchive(VaultError):
    pass


@dataclass
class Imported:
    written: int = 0
    unchanged: int = 0
    folders: int = 0

    def summary(self) -> str:
        return f"{self.written} files written, {self.unchanged} unchanged, {self.folders} folders made"


def valid_path(name: str) -> bool:
    body = name[:-1] if name.endswith("/") else name
    segments = body.split("/")
    return (
        bool(body)
        and all(segment not in ("", ".", "..") for segment in segments)
        and not any(character in name for character in ("\\", "\0"))
    )


def import_files(target: VaultBackend, source: Source) -> Imported:
    existing = {entry.key: entry for entry in target.list_all()}
    files: list[tuple[str, Callable[[], bytes]]] = []
    folders: list[str] = []
    for name, read in source:
        if not valid_path(name):
            raise BadArchive(f"refusing the path {name!r}")
        if read is None:
            folders.append(name if name.endswith("/") else name + "/")
        else:
            files.append((name, read))
    counts = Imported()
    for name, read in files:
        data = read()
        known = existing.get(name)
        if known is not None and known.size == len(data) and target.get_bytes(name)[0] == data:
            counts.unchanged += 1
            continue
        target.put_bytes(name, data)
        counts.written += 1
    taken = set(existing) | {name for name, _ in files}
    occupied = taken | {key[: index + 1] for key in taken for index, char in enumerate(key) if char == "/"}
    for folder in folders:
        if folder in occupied:
            continue
        target.mkdir(folder)
        occupied.add(folder)
        counts.folders += 1
    return counts


def backend_source(source: VaultBackend) -> Source:
    for entry in source.list_all():
        if entry.is_dir:
            yield entry.key, None
        else:
            yield entry.key, lambda key=entry.key: source.get_bytes(key)[0]


def zip_source(archive: zipfile.ZipFile) -> Source:
    members = archive.infolist()
    if len(members) > MAX_ENTRIES:
        raise BadArchive(f"the archive holds more than {MAX_ENTRIES} entries")
    if sum(member.file_size for member in members) > MAX_EXPANDED_BYTES:
        raise BadArchive("the archive expands beyond what one import takes")
    for member in members:
        if member.is_dir():
            yield member.filename, None
        else:
            yield member.filename, lambda member=member: archive.read(member)


def export_zip(backend: VaultBackend, out: IO[bytes]) -> int:
    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry in backend.list_all():
            moment = max(entry.mtime.timetuple()[:6], ZIP_EPOCH)
            info = zipfile.ZipInfo(entry.key, date_time=moment)
            if entry.is_dir:
                archive.writestr(info, b"")
                continue
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, backend.get_bytes(entry.key)[0])
            count += 1
    return count


def import_source(args: argparse.Namespace) -> VaultBackend:
    from notes_vault_mcp.config import ConfigError, env

    if args.from_path:
        from pathlib import Path

        from notes_vault_mcp.backends.local import LocalBackend

        root = Path(args.from_path).expanduser()
        if not root.is_dir():
            raise ConfigError(f"notes-vault-mcp: {root} is not a folder")
        return LocalBackend(root)
    from notes_vault_mcp.backends.s3 import S3Backend

    access_key = env("IMPORT_S3_ACCESS_KEY") or env("S3_ACCESS_KEY")
    secret_key = env("IMPORT_S3_SECRET_KEY") or env("S3_SECRET_KEY")
    endpoint = args.from_s3_endpoint or env("S3_ENDPOINT") or ""
    if not (access_key and secret_key):
        raise ConfigError("notes-vault-mcp: importing from S3 needs IMPORT_S3_ACCESS_KEY and IMPORT_S3_SECRET_KEY")
    return S3Backend(
        endpoint,
        access_key,
        secret_key,
        args.from_s3_bucket,
        prefix=args.from_s3_prefix or "",
        region=args.from_s3_region or env("S3_REGION") or "us-east-1",
    )


def command_import(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth.hosted_access import NoGrant, SupportAccess, chosen_vault
    from notes_vault_mcp.auth.hosted_keys import sealed_vaults_from_env

    store = HostedStore(auth_dir())
    account = store.account(args.account)
    if account is None:
        print(f"notes-vault-mcp: no account {args.account!r}", file=sys.stderr)
        return 1
    vault = chosen_vault(store, account, args.vault)
    if vault is None:
        print(f"notes-vault-mcp: name one of {account.email}'s vaults with --vault <slug>", file=sys.stderr)
        return 1
    if vault.shared:
        print(
            f"notes-vault-mcp: {vault.slug} is a shared vault; it takes notes through its connections, so each change"
            " is kept in its history",
            file=sys.stderr,
        )
        return 1
    source = import_source(args)
    sealed = sealed_vaults_from_env(store)
    try:
        SupportAccess(store).admit(vault, args.operator, args.reason, "import")
    except NoGrant as exc:
        print(f"notes-vault-mcp: refused for {account.email}: {exc}", file=sys.stderr)
        return 1
    target, _ = sealed.for_vault(vault.id)
    try:
        counts = import_files(target, backend_source(source))
    except BadArchive as exc:
        print(f"notes-vault-mcp: {exc}", file=sys.stderr)
        return 1
    print(f"notes-vault-mcp: imported into {account.email}'s {vault.slug}: {counts.summary()}")
    return 0
