from __future__ import annotations

import argparse
import sys
from typing import Any

from mindin.backends import NotFound
from mindin.config import ConfigError
from mindin.hosted.accounts.access import NoGrant, RequestRefused, SupportAccess, ask_for_support, chosen_vault
from mindin.hosted.accounts.keys import sealed_vaults_from_env
from mindin.hosted.accounts.store import HostedStore
from mindin.hosted.config import AuthConfig, auth_config, auth_dir
from mindin.hosted.pages.templates import stamp


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


def sending_config() -> AuthConfig | None:
    try:
        return auth_config()
    except ConfigError:
        return None


def command_request(args: argparse.Namespace) -> int:
    store = HostedStore(auth_dir())
    account = store.account(args.account)
    if account is None:
        print(f"mindin: no account {args.account!r}", file=sys.stderr)
        return 1
    vault = store.own_vault(account.id, args.vault) if args.vault else None
    if args.vault and vault is None:
        print(f"mindin: {account.email} owns no vault {args.vault!r}", file=sys.stderr)
        return 1
    try:
        request, mailed = ask_for_support(store, sending_config(), account, vault, args.operator, args.reason)
    except RequestRefused as exc:
        print(f"mindin: refused for {account.email}: {exc}", file=sys.stderr)
        return 1
    told = "an email asks them to answer it" if mailed else "no email was sent; it waits on their account page"
    print(f"mindin: asked {account.email} to open {request.vault.slug if request.vault else 'any vault'}; {told}")
    return 0


def command_requests(args: argparse.Namespace) -> int:
    found = SupportAccess(HostedStore(auth_dir())).requests()
    if not found:
        print("mindin: no support requests are waiting")
    for request in found:
        vault = request.vault.slug if request.vault else "any"
        print(f"{stamp(request.requested_at)}  {request.account.email}  {vault}  {request.operator}  {request.reason}")
    return 0


def command_withdraw(args: argparse.Namespace) -> int:
    store = HostedStore(auth_dir())
    account = store.account(args.account)
    if account is None or not SupportAccess(store).withdraw(account.id):
        print(f"mindin: no support request is waiting for {args.account!r}", file=sys.stderr)
        return 1
    print(f"mindin: withdrew the support request to {account.email}")
    return 0


def add_operator(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operator", required=True, help="who on the service's staff does this")
    parser.add_argument("--reason", required=True, help="why; the account sees it in its access log")


def add_commands(sub: Any) -> None:
    support = sub.add_parser("support", help="ask to open an account's vault, and open it once the account approves")
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
    asking = support_sub.add_parser("request", help="ask the account to open a vault to support; it is emailed")
    asking.add_argument("account", help="the account's email or id")
    asking.add_argument("--vault", help="the vault's slug; without it the account picks one of its vaults")
    asking.add_argument("--operator", required=True, help="who on the service's staff asks")
    asking.add_argument("--reason", required=True, help="why; the account reads it with the request")
    asking.set_defaults(func=command_request)
    listing = support_sub.add_parser("requests", help="list the requests that wait for an answer")
    listing.set_defaults(func=command_requests)
    withdrawing = support_sub.add_parser("withdraw", help="take back the request an account has not answered")
    withdrawing.add_argument("account", help="the account's email or id")
    withdrawing.set_defaults(func=command_withdraw)
