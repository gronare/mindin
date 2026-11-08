from __future__ import annotations

import argparse
import sys
from typing import Any

from mindin.backends import NotFound
from mindin.hosted.accounts.access import NoGrant, SupportAccess, chosen_vault
from mindin.hosted.accounts.keys import sealed_vaults_from_env
from mindin.hosted.accounts.store import HostedStore
from mindin.hosted.config import auth_dir


def command_support(args: argparse.Namespace) -> int:
    store = HostedStore(auth_dir())
    account = store.account(args.account)
    if account is None:
        print(f"mindin: no account {args.account!r}", file=sys.stderr)
        return 1
    vault = chosen_vault(store, account, args.vault)
    if vault is None:
        print(f"mindin: name one of {account.email}'s vaults with --vault <slug>", file=sys.stderr)
        return 1
    action = "list files" if args.support_command == "files" else f"read {args.path}"
    sealed = sealed_vaults_from_env(store)
    try:
        SupportAccess(store).admit(vault, args.operator, args.reason, action)
    except NoGrant as exc:
        print(f"mindin: refused for {account.email}: {exc}", file=sys.stderr)
        return 1
    backend, _ = sealed.for_vault(vault.id)
    if args.support_command == "files":
        for entry in backend.list_all():
            print(entry.key)
        return 0
    try:
        data, _ = backend.get_bytes(args.path)
    except NotFound:
        print(f"mindin: no such file: {args.path}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(data)
    sys.stdout.flush()
    return 0


def add_operator(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operator", required=True, help="who on the service's staff does this")
    parser.add_argument("--reason", required=True, help="why; the account sees it in its access log")


def add_commands(sub: Any) -> None:
    support = sub.add_parser("support", help="open an account's vault under the support grant it gave on /account")
    support_sub = support.add_subparsers(dest="support_command", required=True)
    listing = support_sub.add_parser("files", help="list the vault's paths")
    listing.add_argument("account", help="the account's email or id")
    listing.add_argument("--vault", help="the vault's slug; needed when the account has several")
    add_operator(listing)
    listing.set_defaults(func=command_support)
    reading = support_sub.add_parser("read", help="print one file")
    reading.add_argument("account", help="the account's email or id")
    reading.add_argument("path")
    reading.add_argument("--vault", help="the vault's slug; needed when the account has several")
    add_operator(reading)
    reading.set_defaults(func=command_support)
