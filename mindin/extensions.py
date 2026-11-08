from __future__ import annotations

import argparse
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from mindin.auth import READ_SCOPE

if TYPE_CHECKING:
    from starlette.routing import BaseRoute

    from mindin.auth import AuthConfig
    from mindin.auth.database import Connection, Database
    from mindin.auth.hosted_deletion import SelfDeletion
    from mindin.auth.hosted_keys import SealedVaults
    from mindin.auth.hosted_limits import StorageLimits
    from mindin.auth.hosted_orgs import Organisation
    from mindin.auth.hosted_store import Account, HostedStore
    from mindin.auth.hosted_vaults import Vault
    from mindin.auth.hosted_web import Browser
    from mindin.auth.identities import Registrant
    from mindin.auth.login_email import SmtpFactory
    from mindin.backends import LimitReached
    from mindin.changes import ChangeBus
    from mindin.vault import SubjectVaults

GROUP = "mindin.extensions"
Access = Literal["full", "read", "none"]
ACCESS_RANK: dict[str, int] = {"none": 0, "read": 1, "full": 2}
Section = Callable[["Account", str], str]
VaultSection = Callable[["Account", "Vault", str], str]
OrganisationSection = Callable[["Organisation", "Account", str], str]
HealthCheck = Callable[[], bool]


@dataclass(frozen=True)
class Periodic:
    """Background work run once when the server starts, then every `seconds`, by one server per period."""

    name: str
    seconds: float
    run: Callable[[], None]


@dataclass(frozen=True)
class SignInCheck:
    """A check on the email-link form: its markup, the page policy's added sources, and the test a link waits on."""

    html: str
    sources: Mapping[str, tuple[str, ...]]
    passed: Callable[[Mapping[str, str], str], Awaitable[bool]]


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
    organisation_sections: list[OrganisationSection] = field(default_factory=list)
    health_checks: list[HealthCheck] = field(default_factory=list)
    periodic: list[Periodic] = field(default_factory=list)


class Extension:
    """A package installed beside the server, named in the mindin.extensions entry point group."""

    def migrate(self, connection: Connection) -> None:
        """Create or migrate this extension's own tables in the account store, every time the store opens."""

    def database(self, directory: Path) -> Database | None:
        """The database the account store keeps its tables in, instead of hosted.sqlite in this directory."""
        return None

    def commands(self, subparsers: Subcommands) -> None:
        """Add subcommands to the mindin CLI."""

    def attach(self, server: Server) -> None:
        """Add routes and sections on the account, vault and organisation pages to a hosted server."""

    def bus(self, server: Server) -> ChangeBus | None:
        """The change bus that carries changes made outside this server to it, and this server's changes out."""
        return None

    def access(self, account: Account) -> Access:
        """How much of its vaults an active account may use right now."""
        return "full"

    def storage_limits(self, account: Account, limits: StorageLimits) -> StorageLimits:
        """The storage limits for the vaults this account owns, given those earlier hooks chose; none to start with."""
        return limits

    def refused_write(self, account: Account, vault: Vault, key: str, reason: LimitReached) -> None:
        """Called each time the storage limits refuse a write to a vault this account owns, after the refusal."""

    def admits(self, registrant: Registrant) -> str:
        """Why a new account for this sign-in is refused, shown to the person, or "" to make it."""
        return ""

    def sign_in_check(self) -> SignInCheck | None:
        """A check the email-link form carries and passes before it sends a link, or None for none."""
        return None

    def registered(self, connection: Connection, account: Account, registrant: Registrant) -> None:
        """Called inside the transaction that makes an account from a sign-in, before it commits."""

    def may_erase(self, account: Account) -> bool:
        """Whether the server may erase this account on its own, as when an organisation removed it; False keeps it."""
        return True


@cache
def installed() -> tuple[Extension, ...]:
    return tuple(point.load()() for point in sorted(entry_points(group=GROUP), key=lambda point: point.name))


def access(extensions: Iterable[Extension], account: Account) -> Access:
    return min((extension.access(account) for extension in extensions), key=ACCESS_RANK.__getitem__, default="full")


def refusal(extensions: Iterable[Extension], registrant: Registrant) -> str:
    return next((found for extension in extensions if (found := extension.admits(registrant))), "")


def sign_in_checks(extensions: Iterable[Extension]) -> tuple[SignInCheck, ...]:
    return tuple(found for extension in extensions if (found := extension.sign_in_check()) is not None)


def may_erase(extensions: Iterable[Extension], account: Account) -> bool:
    return all(extension.may_erase(account) for extension in extensions)


def supplied_database(extensions: Iterable[Extension], directory: Path) -> Database | None:
    return next((found for extension in extensions if (found := extension.database(directory)) is not None), None)


def supplied_bus(extensions: Iterable[Extension], server: Server) -> ChangeBus | None:
    return next((found for extension in extensions if (found := extension.bus(server)) is not None), None)


def narrowed(granted: Access, scopes: list[str]) -> list[str]:
    if granted == "full":
        return scopes
    return [scope for scope in scopes if scope == READ_SCOPE] if granted == "read" else []
