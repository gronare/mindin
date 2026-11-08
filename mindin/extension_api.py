"""The names an extension builds on: import them from here, not from the modules they live in."""

from __future__ import annotations

from mindin.backends import MUST_NOT_EXIST, LimitReached, VaultBackend, VersionConflict, make_subject_backend
from mindin.changes import Change, ChangeBus, Kind, Listener
from mindin.config import ConfigError, env, local_root
from mindin.embedding import semantic_requested
from mindin.extensions import (
    Access,
    Extension,
    HealthCheck,
    OrganisationSection,
    Periodic,
    Section,
    Server,
    SignInCheck,
    Subcommands,
    VaultSection,
)
from mindin.hosted.accounts.keys import SealedVaults, VaultFacts
from mindin.hosted.accounts.limits import StorageLimits, with_defaults
from mindin.hosted.accounts.members import INVITATION_TTL
from mindin.hosted.accounts.store import Account, HostedStore, account_of, digest, scope_label
from mindin.hosted.accounts.vaults import Vault, rewrite_slugs
from mindin.hosted.config import MACHINE_KEY_PURPOSE, AuthConfig, auth_dir
from mindin.hosted.database import Connection, Database, has_table
from mindin.hosted.orgs.organisations import Organisation
from mindin.hosted.pages.account import SelfDeletion
from mindin.hosted.pages.html import esc
from mindin.hosted.pages.mail import Mail, branded, images
from mindin.hosted.pages.templates import stamp
from mindin.hosted.pages.web import Browser, limited, page
from mindin.hosted.schema import STEPS
from mindin.hosted.signin.email import ADDRESS as EMAIL_ADDRESS
from mindin.hosted.signin.email import Mailer, SmtpFactory
from mindin.hosted.signin.identities import Identities
from mindin.hosted.signin.registration import Registrant
from mindin.vault import SubjectVaults

STORE_VERSION = len(STEPS)


def honours_conditions(backend: VaultBackend) -> bool:
    """Whether the storage itself refused a conditional write, rather than the server checking the version first."""
    return bool(getattr(backend, "conditional_puts", True))


__all__ = [
    "EMAIL_ADDRESS",
    "INVITATION_TTL",
    "MACHINE_KEY_PURPOSE",
    "MUST_NOT_EXIST",
    "STORE_VERSION",
    "Access",
    "Account",
    "AuthConfig",
    "Browser",
    "Change",
    "ChangeBus",
    "ConfigError",
    "Connection",
    "Database",
    "Extension",
    "HealthCheck",
    "HostedStore",
    "Identities",
    "Kind",
    "LimitReached",
    "Listener",
    "Mail",
    "Mailer",
    "Organisation",
    "OrganisationSection",
    "Periodic",
    "Registrant",
    "SealedVaults",
    "Section",
    "SelfDeletion",
    "Server",
    "SignInCheck",
    "SmtpFactory",
    "StorageLimits",
    "Subcommands",
    "SubjectVaults",
    "Vault",
    "VaultBackend",
    "VaultFacts",
    "VaultSection",
    "VersionConflict",
    "account_of",
    "auth_dir",
    "branded",
    "digest",
    "env",
    "esc",
    "has_table",
    "honours_conditions",
    "images",
    "limited",
    "local_root",
    "make_subject_backend",
    "page",
    "rewrite_slugs",
    "scope_label",
    "stamp",
    "semantic_requested",
    "with_defaults",
]
