from __future__ import annotations

import argparse
import sys
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import IO

from mindin.auth import auth_dir
from mindin.auth.hosted_limits import size_text
from mindin.auth.hosted_store import HostedStore
from mindin.backends import LimitReached, VaultBackend, VaultError
from mindin.transfer import count_text, skipped_as, skipped_lines

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 100_000
SPOOL_BYTES = 16 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

Source = Iterator[tuple[str, int, Callable[[], bytes] | None]]


class BadArchive(VaultError):
    pass


class Refused(LimitReached):
    def __init__(self, refusals: list[LimitReached]) -> None:
        super().__init__("; ".join(str(refusal) for refusal in refusals), refusals[0].status)
        self.refusals = refusals


@dataclass
class Imported:
    written: int = 0
    unchanged: int = 0
    folders: int = 0
    skipped: int = 0

    def summary(self) -> str:
        left = f", {self.skipped} skipped" if self.skipped else ""
        return f"{self.written} files written, {self.unchanged} unchanged, {self.folders} folders made{left}"


@dataclass
class ImportPlan:
    files: list[tuple[str, int, Callable[[], bytes]]] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    refusals: list[LimitReached] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(size for _, size, _ in self.files)

    def lines(self) -> list[str]:
        lines = [f"{count_text(len(self.files))} files, {size_text(self.size)}"]
        lines += [f"skipped: {line}" for line in skipped_lines(self.skipped)]
        lines += [f"refused: {refusal}" for refusal in self.refusals]
        return lines


def valid_path(name: str) -> bool:
    body = name[:-1] if name.endswith("/") else name
    segments = body.split("/")
    return (
        bool(body)
        and all(segment not in ("", ".", "..") for segment in segments)
        and not any(character in name for character in ("\\", "\0"))
    )


def prepare_import(target: VaultBackend, source: Source) -> ImportPlan:
    plan = ImportPlan()
    for name, size, read in source:
        if not valid_path(name):
            raise BadArchive(f"refusing the path {name!r}")
        if skipped_as(name) is not None:
            if read is not None:
                plan.skipped.append(name)
        elif read is None:
            plan.folders.append(name if name.endswith("/") else name + "/")
        else:
            plan.files.append((name, size, read))
    check = getattr(target, "preflight", None)
    if check is not None:
        plan.refusals = check({name: size for name, size, _ in plan.files})
    return plan


def refuse_import(target: VaultBackend, plan: ImportPlan) -> Refused:
    report = getattr(target, "report_refusal", None)
    for found in plan.refusals if report is not None else ():
        report(found)
    return Refused(plan.refusals)


def import_files(target: VaultBackend, source: Source | ImportPlan) -> Imported:
    plan = source if isinstance(source, ImportPlan) else prepare_import(target, source)
    if plan.refusals:
        raise refuse_import(target, plan)
    existing = {entry.key: entry for entry in target.list_all()}
    counts = Imported(skipped=len(plan.skipped))
    for name, _, read in plan.files:
        data = read()
        known = existing.get(name)
        if known is not None and known.size == len(data) and target.get_bytes(name)[0] == data:
            counts.unchanged += 1
            continue
        target.put_bytes(name, data)
        counts.written += 1
    taken = set(existing) | {name for name, _, _ in plan.files}
    occupied = taken | {key[: index + 1] for key in taken for index, char in enumerate(key) if char == "/"}
    for folder in plan.folders:
        if folder in occupied:
            continue
        target.mkdir(folder)
        occupied.add(folder)
        counts.folders += 1
    return counts


def backend_source(source: VaultBackend) -> Source:
    for entry in source.list_all():
        if entry.is_dir:
            yield entry.key, 0, None
        else:
            yield entry.key, entry.size, lambda key=entry.key: source.get_bytes(key)[0]


def zip_source(archive: zipfile.ZipFile) -> Source:
    members = archive.infolist()
    if len(members) > MAX_ENTRIES:
        raise BadArchive(f"the archive holds more than {MAX_ENTRIES} entries")
    if sum(member.file_size for member in members) > MAX_EXPANDED_BYTES:
        raise BadArchive("the archive expands beyond what one import takes")
    for member in members:
        if member.is_dir():
            yield member.filename, 0, None
        else:
            yield member.filename, member.file_size, lambda member=member: archive.read(member)


def import_zip(target: VaultBackend, file: IO[bytes]) -> Imported:
    with zipfile.ZipFile(file) as archive:
        return import_files(target, zip_source(archive))


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
    from mindin.config import ConfigError, env

    if args.from_path:
        from pathlib import Path

        from mindin.backends.local import LocalBackend

        root = Path(args.from_path).expanduser()
        if not root.is_dir():
            raise ConfigError(f"mindin: {root} is not a folder")
        return LocalBackend(root)
    from mindin.backends.s3 import S3Backend

    access_key = env("IMPORT_S3_ACCESS_KEY") or env("S3_ACCESS_KEY")
    secret_key = env("IMPORT_S3_SECRET_KEY") or env("S3_SECRET_KEY")
    endpoint = args.from_s3_endpoint or env("S3_ENDPOINT") or ""
    if not (access_key and secret_key):
        raise ConfigError("mindin: importing from S3 needs IMPORT_S3_ACCESS_KEY and IMPORT_S3_SECRET_KEY")
    return S3Backend(
        endpoint,
        access_key,
        secret_key,
        args.from_s3_bucket,
        prefix=args.from_s3_prefix or "",
        region=args.from_s3_region or env("S3_REGION") or "us-east-1",
    )


def command_import(args: argparse.Namespace) -> int:
    from mindin.auth.hosted_access import NoGrant, SupportAccess, chosen_vault
    from mindin.auth.hosted_keys import sealed_vaults_from_env
    from mindin.auth.hosted_limits import Quotas

    store = HostedStore(auth_dir())
    account = store.account(args.account)
    if account is None:
        print(f"mindin: no account {args.account!r}", file=sys.stderr)
        return 1
    vault = chosen_vault(store, account, args.vault)
    if vault is None:
        print(f"mindin: name one of {account.email}'s vaults with --vault <slug>", file=sys.stderr)
        return 1
    if vault.shared:
        print(
            f"mindin: {vault.slug} is a shared vault; it takes notes through its connections, so each change"
            " is kept in its history",
            file=sys.stderr,
        )
        return 1
    source = import_source(args)
    sealed = sealed_vaults_from_env(store)
    try:
        SupportAccess(store).admit(vault, args.operator, args.reason, "import")
    except NoGrant as exc:
        print(f"mindin: refused for {account.email}: {exc}", file=sys.stderr)
        return 1
    sealed_target, _ = sealed.for_vault(vault.id)
    quotas = Quotas(store, store.extensions, lambda vault_id: sealed.facts(vault_id).size)
    target = quotas.guard(vault, sealed_target)
    try:
        plan = prepare_import(target, backend_source(source))
    except BadArchive as exc:
        print(f"mindin: {exc}", file=sys.stderr)
        return 1
    out = sys.stderr if plan.refusals else sys.stdout
    for line in plan.lines():
        print(f"mindin: {line}", file=out)
    if plan.refusals and not args.dry_run:
        refuse_import(target, plan)
    if plan.refusals:
        print(
            f"mindin: nothing imported into {account.email}'s {vault.slug}; the server refuses the files above",
            file=sys.stderr,
        )
        return 1
    if args.dry_run:
        print(f"mindin: dry run; nothing imported into {account.email}'s {vault.slug}")
        return 0
    try:
        counts = import_files(target, plan)
    except LimitReached as exc:
        print(f"mindin: {exc}", file=sys.stderr)
        return 1
    print(f"mindin: imported into {account.email}'s {vault.slug}: {counts.summary()}")
    return 0
