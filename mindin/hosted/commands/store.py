from __future__ import annotations

import argparse
import sys
from typing import Any

from mindin.config import ConfigError
from mindin.hosted.accounts.store import HostedStore
from mindin.hosted.config import auth_dir
from mindin.hosted.schema import STEPS


def command_migrate(args: argparse.Namespace) -> int:
    try:
        store = HostedStore(auth_dir())
    except ConfigError:
        raise
    except Exception as exc:
        print(f"mindin: the account store was not migrated: {exc}", file=sys.stderr)
        return 1
    found = "" if store.migrated_from is None else f" (was {store.migrated_from})"
    print(f"store at version {len(STEPS)}{found}")
    return 0


def add_commands(sub: Any) -> None:
    migrate = sub.add_parser("migrate", help="bring the hosted account store up to this release and exit")
    migrate.set_defaults(func=command_migrate)
