"""The names an extension builds on: import them from here, not from the modules they live in."""

from __future__ import annotations

from mindin.auth import MACHINE_KEY_PURPOSE, AuthConfig, auth_dir
from mindin.auth.database import Connection, Database, has_table
from mindin.auth.hosted import limited
from mindin.auth.hosted_deletion import SelfDeletion
from mindin.auth.hosted_keys import SealedVaults, VaultFacts
from mindin.auth.hosted_limits import StorageLimits, with_defaults
from mindin.auth.hosted_members import INVITATION_TTL
from mindin.auth.hosted_orgs import Organisation
from mindin.auth.hosted_store import Account, HostedStore, account_of, digest, scope_label
from mindin.auth.hosted_vaults import MIGRATIONS, Vault, rewrite_slugs
from mindin.auth.hosted_web import Browser, esc, page
from mindin.auth.identities import Identities
from mindin.auth.login_email import ADDRESS as EMAIL_ADDRESS
from mindin.auth.login_email import Mailer, SmtpFactory
from mindin.auth.mail import Mail, branded, images
from mindin.auth.pages import stamp
from mindin.auth.registration import Registrant
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
from mindin.vault import SubjectVaults

STORE_VERSION = len(MIGRATIONS)


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
