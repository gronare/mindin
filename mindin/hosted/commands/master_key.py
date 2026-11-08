from __future__ import annotations

import argparse
import sys
from typing import Any

from mindin.backends import VaultError
from mindin.hosted.accounts.keys import Keyring, MasterKey, VaultKeys
from mindin.hosted.accounts.store import HostedStore
from mindin.hosted.config import auth_dir


def command_master_key(args: argparse.Namespace) -> int:
    if args.master_key_command == "generate":
        print(MasterKey.generate().encoded())
        return 0
    store = HostedStore(auth_dir())
    keys = VaultKeys(store, Keyring.from_env())
    if args.master_key_command == "status":
        current = keys.keyring.current.key_id
        print(f"mindin: current master key {current}")
        for key_id, count in sorted(keys.counts().items()):
            state = "current" if key_id == current else "previous" if key_id in keys.keyring.key_ids else "MISSING"
            print(f"{key_id}  {count} vault keys  {state}")
        return 0
    try:
        rewrapped, kept = keys.rewrap()
    except VaultError as exc:
        print(f"mindin: {exc}", file=sys.stderr)
        return 1
    current = keys.keyring.current.key_id
    print(f"mindin: rewrapped {rewrapped} vault keys under {current}, {kept} already were")
    return 0


def add_commands(sub: Any) -> None:
    master = sub.add_parser("master-key", help="the master key that wraps each hosted vault's data key")
    master_sub = master.add_subparsers(dest="master_key_command", required=True)
    master_sub.add_parser("generate", help="print a fresh base64 master key").set_defaults(func=command_master_key)
    master_sub.add_parser("status", help="count vault keys per master key").set_defaults(func=command_master_key)
    master_sub.add_parser(
        "rewrap", help="wrap every vault key under VAULT_HOSTED_MASTER_KEY, unwrapping with the previous keys"
    ).set_defaults(func=command_master_key)
