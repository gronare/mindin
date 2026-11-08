from __future__ import annotations

import argparse
import logging
import secrets
import smtplib
import sys
from dataclasses import dataclass
from urllib.parse import quote

from mindin.config import ConfigError
from mindin.hosted.accounts.store import Account, HostedStore, MembershipError, digest
from mindin.hosted.accounts.vaults import Vault
from mindin.hosted.config import AuthConfig, auth_dir
from mindin.hosted.pages import mail
from mindin.hosted.pages.templates import stamp
from mindin.hosted.signin.email import Mailer, SmtpFactory
from mindin.hosted.signin.identities import Identities

logger = logging.getLogger(__name__)

INVITATION_TTL = 7 * 86400


@dataclass(frozen=True)
class Invited:
    email: str
    sent: bool


class Invitations:
    def __init__(self, config: AuthConfig, store: HostedStore, smtp_factory: SmtpFactory | None = None) -> None:
        self.config = config
        self.store = store
        self.mailer = Mailer(config.smtp, smtp_factory) if config.smtp else None

    @property
    def public_url(self) -> str:
        return self.config.public_url.rstrip("/")

    def invite(self, vault: Vault, owner: Account, email: str) -> Invited:
        token = secrets.token_urlsafe(32) if self.mailer else ""
        address = self.store.invite_member(vault, email, owner.id, digest(token) if token else None)
        if self.mailer is None:
            return Invited(address, False)
        next_url = f"{self.config.path_prefix}/account/vaults/{vault.slug}"
        Identities(self.store).begin("email", token, "", {"email": address, "next": next_url}, INVITATION_TTL)
        link = f"{self.public_url}/signin/email/confirm?token={quote(token)}"
        subject, text, html = mail.invitation(self.public_url, link, owner.email, vault.name)
        try:
            self.mailer.send(address, subject, text, html, mail.images())
        except (OSError, smtplib.SMTPException) as exc:
            logger.warning("vault invitation: sending failed: %s", exc)
            self.store.withdraw(vault, address)
            raise MembershipError("The invitation could not be sent. Try again later.") from exc
        return Invited(address, True)

    def unsent_hint(self, email: str) -> str:
        return (
            f"This server sends no email, so tell {email} to sign in at {self.public_url}/signin with that address; "
            "the vault is theirs to open from then on."
        )


def shared_vault(store: HostedStore, ref: str, slug: str) -> tuple[Account, Vault] | str:
    account = store.account(ref)
    if account is None:
        return f"no account {ref!r}"
    vault = store.own_vault(account.id, slug)
    if vault is None:
        return f"{account.email} owns no vault {slug!r}"
    if not vault.shared:
        return f"{vault.slug} is a personal vault; only a shared vault has members"
    return account, vault


def command_members(args: argparse.Namespace) -> int:
    from mindin.hosted.config import auth_config

    store = HostedStore(auth_dir())
    found = shared_vault(store, args.account, args.vault)
    if isinstance(found, str):
        print(f"mindin: {found}", file=sys.stderr)
        return 1
    owner, vault = found
    action = args.accounts_command
    if action == "members":
        for member in store.members(vault):
            changed = f"{stamp(member.last_change_at)} UTC" if member.last_change_at else "no changes"
            print(f"{member.email}  {member.role}  since {stamp(member.since)} UTC  {changed}")
        return 0
    if action == "add-member":
        try:
            config = auth_config()
        except ConfigError:
            config = None
        try:
            if config is None:
                address = store.invite_member(vault, args.email, owner.id)
                print(f"mindin: invited {address} to {vault.slug}; no email was sent")
                return 0
            invited = Invitations(config, store).invite(vault, owner, args.email)
        except MembershipError as exc:
            print(f"mindin: {exc}", file=sys.stderr)
            return 2
        sent = "an email with a sign-in link is on its way" if invited.sent else "no email was sent"
        print(f"mindin: invited {invited.email} to {vault.slug}; {sent}")
        return 0
    member = store.account(args.email)
    if store.withdraw(vault, args.email):
        print(f"mindin: withdrew the invitation of {args.email.lower()} to {vault.slug}")
        return 0
    try:
        removed = member is not None and store.remove_member(vault, member.id)
    except MembershipError as exc:
        print(f"mindin: {exc}", file=sys.stderr)
        return 2
    if not removed:
        print(f"mindin: {args.email} is not a member of {vault.slug}", file=sys.stderr)
        return 1
    print(f"mindin: removed {member.email} from {vault.slug}; their keys and connections to it are revoked")
    return 0
