from __future__ import annotations

import logging
import secrets
import smtplib
from dataclasses import dataclass
from urllib.parse import quote

from mindin.hosted.accounts.store import Account, HostedStore, MembershipError, digest
from mindin.hosted.accounts.vaults import Vault
from mindin.hosted.config import AuthConfig
from mindin.hosted.pages import mail
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
