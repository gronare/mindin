from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from mindin import provision
from mindin.backends import LimitReached, VaultBackend
from mindin.backends.local import LocalBackend
from mindin.config import ConfigError, cache_dir, env
from mindin.hosted.accounts.access import NoGrant, SupportAccess, chosen_vault
from mindin.hosted.accounts.import_export import BadArchive, backend_source, import_files, prepare_import, refuse_import
from mindin.hosted.accounts.keys import StillShared, delete_account, sealed_vaults_from_env
from mindin.hosted.accounts.limits import Quotas
from mindin.hosted.accounts.members import Invitations, shared_vault
from mindin.hosted.accounts.store import AccountStatus, HostedStore, MembershipError, VaultNameError
from mindin.hosted.accounts.vaults import PERSONAL, SHARED
from mindin.hosted.commands.support import add_operator
from mindin.hosted.config import auth_config, auth_dir
from mindin.hosted.pages.templates import stamp
from mindin.presets import PERSONAL_LAYOUTS


def command_accounts(args: argparse.Namespace) -> int:
    store = HostedStore(auth_dir())
    action = args.accounts_command
    if action == "list":
        accounts = store.accounts()
        if not accounts:
            print("mindin: no accounts yet; `mindin accounts invite <email>` adds one")
        for account in accounts:
            keys = len(store.keys(account.id))
            vaults = len(store.vaults(account.id))
            signed_in = f"{stamp(account.last_login_at)} UTC" if account.last_login_at else "never signed in"
            print(f"{account.id}  {account.email}  {account.status}  {vaults} vaults  {keys} keys  {signed_in}")
        return 0
    if action == "invite":
        try:
            account, created = store.invite(args.email)
        except ValueError as exc:
            print(f"mindin: {exc}", file=sys.stderr)
            return 2
        verb = "invited" if created else "already has an account,"
        print(f"mindin: {account.email} {verb} status {account.status}")
        return 0
    if action in ("vaults", "add-vault", "rename-vault"):
        return command_vaults(store, args)
    status: AccountStatus = "suspended" if action == "suspend" else "active"
    account = store.set_status(args.account, status)
    if account is None:
        print(f"mindin: no account {args.account!r}", file=sys.stderr)
        return 1
    print(f"mindin: {account.email} is {account.status}")
    return 0


def command_vaults(store: HostedStore, args: argparse.Namespace) -> int:
    account = store.account(args.account)
    if account is None:
        print(f"mindin: no account {args.account!r}", file=sys.stderr)
        return 1
    action = args.accounts_command
    try:
        if action == "add-vault":
            if args.shared and args.preset:
                print("mindin: a shared vault has its own fixed layout; leave out --preset", file=sys.stderr)
                return 2
            vault = store.create_vault(account.id, args.name, args.slug, SHARED if args.shared else PERSONAL)
            if args.preset:
                backend, _ = sealed_vaults_from_env(store).for_vault(vault.id)
                provision.initialize(backend, preset=args.preset)
            print(f"mindin: {account.email} has the {vault.kind} vault {vault.name} at /mcp?vault={vault.slug}")
            return 0
        if action == "rename-vault":
            vault = store.own_vault(account.id, args.vault)
            if vault is None:
                print(f"mindin: {account.email} has no vault {args.vault!r}", file=sys.stderr)
                return 1
            renamed = store.rename_vault(vault.id, args.name, args.slug)
            print(f"mindin: {renamed.name} is at /mcp?vault={renamed.slug}")
            return 0
    except VaultNameError as exc:
        print(f"mindin: {exc}", file=sys.stderr)
        return 2
    sealed = sealed_vaults_from_env(store)
    for vault in store.reachable_vaults(account.id):
        facts = sealed.facts(vault.id)
        keys = len(store.keys(account.id, vault.id))
        kind = f"shared, {store.role(vault, account.id)}" if vault.shared else vault.kind
        print(f"{vault.slug}  {vault.name}  {kind}  {vault.id}  {facts.notes} notes  {facts.files} files  {keys} keys")
    return 0


def command_members(args: argparse.Namespace) -> int:
    store = HostedStore(auth_dir())
    found = shared_vault(store, args.account, args.vault)
    if isinstance(found, str):
        print(f"mindin: {found}", file=sys.stderr)
        return 1
    owner, vault = found
    action = args.accounts_command
    if action == "members":
        for member in store.members(vault):
            changed = f"{stamp(member.last_change_at)} UTC" if member.last_change_at else "no changes"
            print(f"{member.email}  {member.role}  since {stamp(member.since)} UTC  {changed}")
        return 0
    if action == "add-member":
        try:
            config = auth_config()
        except ConfigError:
            config = None
        try:
            if config is None:
                address = store.invite_member(vault, args.email, owner.id)
                print(f"mindin: invited {address} to {vault.slug}; no email was sent")
                return 0
            invited = Invitations(config, store).invite(vault, owner, args.email)
        except MembershipError as exc:
            print(f"mindin: {exc}", file=sys.stderr)
            return 2
        sent = "an email with a sign-in link is on its way" if invited.sent else "no email was sent"
        print(f"mindin: invited {invited.email} to {vault.slug}; {sent}")
        return 0
    member = store.account(args.email)
    if store.withdraw(vault, args.email):
        print(f"mindin: withdrew the invitation of {args.email.lower()} to {vault.slug}")
        return 0
    try:
        removed = member is not None and store.remove_member(vault, member.id)
    except MembershipError as exc:
        print(f"mindin: {exc}", file=sys.stderr)
        return 2
    if not removed:
        print(f"mindin: {args.email} is not a member of {vault.slug}", file=sys.stderr)
        return 1
    print(f"mindin: removed {member.email} from {vault.slug}; their keys and connections to it are revoked")
    return 0


def command_delete_account(args: argparse.Namespace) -> int:
    store = HostedStore(auth_dir())
    account = store.account(args.account)
    if account is None:
        print(f"mindin: no account {args.account!r}", file=sys.stderr)
        return 1
    if not args.yes:
        print(
            f"mindin: deleting {account.email} destroys every vault it owns for good; repeat with --yes",
            file=sys.stderr,
        )
        return 2
    try:
        deleted = delete_account(sealed_vaults_from_env(store), account, cache_dir())
    except StillShared as exc:
        print(f"mindin: {exc}", file=sys.stderr)
        return 1
    print(f"mindin: deleted {account.email}: its vaults' data keys, {deleted} objects and their indexes")
    return 0


def import_source(args: argparse.Namespace) -> VaultBackend:
    if args.from_path:
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


def add_commands(sub: Any) -> None:
    accounts = sub.add_parser("accounts", help="the accounts of the HTTP server")
    accounts_sub = accounts.add_subparsers(dest="accounts_command", required=True)
    accounts_sub.add_parser("list", help="list accounts with status and key count").set_defaults(func=command_accounts)
    inviting = accounts_sub.add_parser("invite", help="let this email address sign in and get a vault")
    inviting.add_argument("email")
    inviting.set_defaults(func=command_accounts)
    for name, summary in (
        ("suspend", "refuse the account's keys and tokens at once"),
        ("activate", "lift a suspension"),
    ):
        changing = accounts_sub.add_parser(name, help=summary)
        changing.add_argument("account", help="the account's email or id")
        changing.set_defaults(func=command_accounts)
    listing_vaults = accounts_sub.add_parser("vaults", help="list the account's vaults with notes, files and keys")
    listing_vaults.add_argument("account", help="the account's email or id")
    listing_vaults.set_defaults(func=command_accounts)
    adding = accounts_sub.add_parser("add-vault", help="give the account another vault")
    adding.add_argument("account", help="the account's email or id")
    adding.add_argument("name", help="the vault's name; the address is made from it unless --slug names one")
    adding.add_argument("--slug", help="the vault's slug, as in /mcp?vault=<slug>: one lowercase word")
    adding.add_argument("--preset", choices=PERSONAL_LAYOUTS, help="write this layout now (needs the master key)")
    adding.add_argument(
        "--shared", action="store_true", help="a shared vault: the account and the members it invites, current notes"
    )
    adding.set_defaults(func=command_accounts)
    members = accounts_sub.add_parser("members", help="list a shared vault's owner, members and open invitations")
    members.add_argument("account", help="the owner's email or id")
    members.add_argument("vault", help="the vault's slug")
    members.set_defaults(func=command_members)
    adding_member = accounts_sub.add_parser(
        "add-member", help="invite an email to a shared vault; with SMTP set up, it gets a sign-in link"
    )
    adding_member.add_argument("account", help="the owner's email or id")
    adding_member.add_argument("vault", help="the vault's slug")
    adding_member.add_argument("email")
    adding_member.set_defaults(func=command_members)
    removing_member = accounts_sub.add_parser(
        "remove-member", help="withdraw an invitation, or remove a member and revoke their keys to the vault"
    )
    removing_member.add_argument("account", help="the owner's email or id")
    removing_member.add_argument("vault", help="the vault's slug")
    removing_member.add_argument("email")
    removing_member.set_defaults(func=command_members)
    renaming = accounts_sub.add_parser("rename-vault", help="change a vault's name or address")
    renaming.add_argument("account", help="the account's email or id")
    renaming.add_argument("vault", help="the vault's slug")
    renaming.add_argument("--name")
    renaming.add_argument(
        "--slug", help="a new slug, one lowercase word; clients that use /mcp?vault=<old slug> must change too"
    )
    renaming.set_defaults(func=command_accounts)
    deleting = accounts_sub.add_parser(
        "delete", help="destroy the data keys, objects and indexes of every vault, then the account itself"
    )
    deleting.add_argument("account", help="the account's email or id")
    deleting.add_argument("--yes", action="store_true", help="confirm; the vault cannot be recovered")
    deleting.set_defaults(func=command_delete_account)
    importing = accounts_sub.add_parser(
        "import", help="seal every file of a plaintext vault into the account (needs its support grant)"
    )
    importing.add_argument("account", help="the account's email or id")
    importing.add_argument("--vault", help="the vault's slug; needed when the account has several")
    origin = importing.add_mutually_exclusive_group(required=True)
    origin.add_argument("--from-path", help="a local vault folder")
    origin.add_argument("--from-s3-bucket", help="a bucket; credentials from IMPORT_S3_ACCESS_KEY/SECRET_KEY")
    importing.add_argument("--from-s3-prefix", default="", help="the vault's prefix in that bucket")
    importing.add_argument("--from-s3-endpoint", help="defaults to S3_ENDPOINT")
    importing.add_argument("--from-s3-region", help="defaults to S3_REGION, then us-east-1")
    importing.add_argument("--dry-run", action="store_true", help="list what would be imported, skipped and refused")
    add_operator(importing)
    importing.set_defaults(func=command_import)
