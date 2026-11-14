from __future__ import annotations

from typing import Any

from mindin.hosted.commands import accounts, master_key, orgs, store, support


def add_commands(sub: Any) -> None:
    for commands in (accounts, orgs, master_key, support, store):
        commands.add_commands(sub)
