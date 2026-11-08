"""The names an extension builds on: import them from here, not from the modules they live in."""

from __future__ import annotations

from mindin.backends import MUST_NOT_EXIST, LimitReached, VaultBackend, VersionConflict, make_subject_backend
from mindin.backends.s3 import S3Backend
from mindin.changes import Change, ChangeBus, Kind, Listener
from mindin.cli import main as cli_main
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
    installed,
)
from mindin.hosted.accounts.access import RequestRefused, SupportAccess, SupportRequest, ask_for_support
from mindin.hosted.accounts.deletion import delete_departed
from mindin.hosted.accounts.history import History
from mindin.hosted.accounts.keys import Keyring, MasterKey, SealedVaults, VaultFacts, VaultKeys
from mindin.hosted.accounts.limits import StorageLimits, with_defaults
from mindin.hosted.accounts.members import INVITATION_TTL
from mindin.hosted.accounts.store import Account, HostedStore, account_of, digest, scope_label
from mindin.hosted.accounts.vaults import SHARED, Vault, rewrite_slugs
from mindin.hosted.app import build_hosted_app, passes
from mindin.hosted.config import MACHINE_KEY_PURPOSE, AuthConfig, GithubConfig, OidcConfig, SmtpConfig, auth_dir
from mindin.hosted.database import Connection, Database, account_tables, has_table
from mindin.hosted.orgs.organisations import Organisation, OrganisationError, Organisations
from mindin.hosted.pages.account import SelfDeletion
from mindin.hosted.pages.html import esc
from mindin.hosted.pages.mail import Mail, branded, images
from mindin.hosted.pages.templates import stamp
from mindin.hosted.pages.web import Browser, RateCounter, limited, page
from mindin.hosted.periodic import Leases
from mindin.hosted.schema import STEPS
from mindin.hosted.signin.email import ADDRESS as EMAIL_ADDRESS
from mindin.hosted.signin.email import Mailer, SmtpFactory
from mindin.hosted.signin.identities import Identities, Proof
from mindin.hosted.signin.registration import Registrant
from mindin.shared import Actor
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
    "SHARED",
    "STORE_VERSION",
    "Access",
    "Account",
    "Actor",
    "AuthConfig",
    "Browser",
    "Change",
    "ChangeBus",
    "ConfigError",
    "Connection",
    "Database",
    "Extension",
    "GithubConfig",
    "HealthCheck",
    "History",
    "HostedStore",
    "Identities",
    "Keyring",
    "Kind",
    "Leases",
    "LimitReached",
    "Listener",
    "Mail",
    "Mailer",
    "MasterKey",
    "OidcConfig",
    "Organisation",
    "OrganisationError",
    "OrganisationSection",
    "Organisations",
    "Periodic",
    "Proof",
    "RateCounter",
    "Registrant",
    "RequestRefused",
    "S3Backend",
    "SealedVaults",
    "Section",
    "SelfDeletion",
    "Server",
    "SignInCheck",
    "SmtpConfig",
    "SmtpFactory",
    "StorageLimits",
    "Subcommands",
    "SubjectVaults",
    "SupportAccess",
    "SupportRequest",
    "Vault",
    "VaultBackend",
    "VaultFacts",
    "VaultKeys",
    "VaultSection",
    "VersionConflict",
    "account_of",
    "account_tables",
    "ask_for_support",
    "auth_dir",
    "branded",
    "build_hosted_app",
    "cli_main",
    "delete_departed",
    "digest",
    "env",
    "esc",
    "has_table",
    "honours_conditions",
    "images",
    "installed",
    "limited",
    "local_root",
    "make_subject_backend",
    "page",
    "passes",
    "rewrite_slugs",
    "scope_label",
    "semantic_requested",
    "stamp",
    "with_defaults",
]
