from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from notes_vault_mcp.auth import READ_SCOPE

if TYPE_CHECKING:
    from starlette.routing import BaseRoute

    from notes_vault_mcp.auth import AuthConfig
    from notes_vault_mcp.auth.database import Connection, Database
    from notes_vault_mcp.auth.hosted_deletion import SelfDeletion
    from notes_vault_mcp.auth.hosted_keys import SealedVaults
    from notes_vault_mcp.auth.hosted_store import Account, HostedStore
    from notes_vault_mcp.auth.hosted_vaults import Vault
    from notes_vault_mcp.auth.hosted_web import Browser
    from notes_vault_mcp.auth.login_email import SmtpFactory
    from notes_vault_mcp.changes import ChangeBus
    from notes_vault_mcp.vault import SubjectVaults

GROUP = "notes_vault_mcp.extensions"
Access = Literal["full", "read", "none"]
ACCESS_RANK: dict[str, int] = {"none": 0, "read": 1, "full": 2}
Section = Callable[["Account", str], str]
VaultSection = Callable[["Account", "Vault", str], str]
HealthCheck = Callable[[], bool]


class Subcommands(Protocol):
    """The CLI's subcommand group, as argparse's add_subparsers returns it."""

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser: ...


@dataclass
class Server:
    """The hosted server an extension attaches to while it is built."""

    config: AuthConfig
    store: HostedStore
    clock: Callable[[], float] = time.time
    browser: Browser | None = None
    vaults: SubjectVaults | None = None
    sealed: SealedVaults | None = None
    deletion: SelfDeletion | None = None
    smtp_factory: SmtpFactory | None = None
    routes: list[BaseRoute] = field(default_factory=list)
    account_sections: list[Section] = field(default_factory=list)
    vault_sections: list[VaultSection] = field(default_factory=list)
    health_checks: list[HealthCheck] = field(default_factory=list)


class Extension:
    """A package installed beside the server, named in the notes_vault_mcp.extensions entry point group."""

    def migrate(self, connection: Connection) -> None:
        """Create or migrate this extension's own tables in the account store, every time the store opens."""

    def database(self, directory: Path) -> Database | None:
        """The database the account store keeps its tables in, instead of hosted.sqlite in this directory."""
        return None

    def commands(self, subparsers: Subcommands) -> None:
        """Add subcommands to the notes-vault-mcp CLI."""

    def attach(self, server: Server) -> None:
        """Add routes and sections on the account and vault pages to a hosted server."""

    def bus(self, server: Server) -> ChangeBus | None:
        """The change bus that carries changes made outside this server to it, and this server's changes out."""
        return None

    def access(self, account: Account) -> Access:
        """How much of its vaults an active account may use right now."""
        return "full"


@cache
def installed() -> tuple[Extension, ...]:
    return tuple(point.load()() for point in sorted(entry_points(group=GROUP), key=lambda point: point.name))


def access(extensions: Iterable[Extension], account: Account) -> Access:
    return min((extension.access(account) for extension in extensions), key=ACCESS_RANK.__getitem__, default="full")


def supplied_database(extensions: Iterable[Extension], directory: Path) -> Database | None:
    return next((found for extension in extensions if (found := extension.database(directory)) is not None), None)


def supplied_bus(extensions: Iterable[Extension], server: Server) -> ChangeBus | None:
    return next((found for extension in extensions if (found := extension.bus(server)) is not None), None)


def narrowed(granted: Access, scopes: list[str]) -> list[str]:
    if granted == "full":
        return scopes
    return [scope for scope in scopes if scope == READ_SCOPE] if granted == "read" else []
