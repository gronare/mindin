from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable
from datetime import UTC, datetime

from mindin.hosted.orgs.organisations import Departure, Organisations, OrgInvitation
from mindin.hosted.pages import mail
from mindin.hosted.signin.email import Mailer

logger = logging.getLogger(__name__)

RESEND_AFTER = 600


def mail_date(moment: int) -> str:
    day = datetime.fromtimestamp(moment, UTC)
    return f"{day.day} {day:%B %Y}"


def invitation_mail(public_url: str, item: OrgInvitation) -> mail.Mail:
    name = item.organisation.name
    inviter = item.invited_by.email if item.invited_by is not None else name
    return mail.branded(
        f"{name} invites you to its vaults",
        f"Join {name}",
        f"{inviter} added {item.account.email} to {name} on {mail.host_of(public_url)}. Accept on your account page"
        f" to read and write {name}'s vaults. Your own vaults and your account stay yours.",
        "Open your account page",
        f"{public_url.rstrip('/')}/account/settings",
        "The invitation waits there until you accept or decline it.",
        "Not expecting this? Decline it there, or ignore this email: nothing changes until you accept.",
    )


def departure_mail(public_url: str, departure: Departure) -> mail.Mail:
    when = mail_date(departure.delete_after or 0)
    link = f"{public_url.rstrip('/')}/account"
    return mail.branded(
        f"{departure.organisation.name} removed you from its vaults",
        "Download your vaults",
        f"{departure.organisation.name} removed {departure.account.email} from its vaults on "
        f"{mail.host_of(public_url)}. Your own vaults and your account are deleted on {when}. Sign in before"
        f" {when} to keep your account, or to download each of your vaults as a zip.",
        "Download your vaults",
        link,
        "Each vault's page has a Download zip button.",
    )


class OrgMail:
    def __init__(
        self, organisations: Organisations, mailer: Mailer | None, public_url: str, clock: Callable[[], float]
    ) -> None:
        self.organisations = organisations
        self.mailer = mailer
        self.public_url = public_url.rstrip("/")
        self.clock = clock

    @property
    def sends(self) -> bool:
        return self.mailer is not None

    def resend_from(self, item: OrgInvitation) -> int:
        return item.mailed_at + RESEND_AFTER if item.mailed_at else 0

    def waiting(self, item: OrgInvitation) -> bool:
        return self.resend_from(item) > self.clock()

    def invite(self, item: OrgInvitation) -> bool:
        if not self.deliver(item.account.email, invitation_mail(self.public_url, item), "organisation invitation"):
            return False
        self.organisations.mailed(item)
        return True

    def depart(self, departure: Departure) -> bool:
        if not self.deliver(
            departure.account.email, departure_mail(self.public_url, departure), "organisation departure"
        ):
            return False
        self.organisations.told(departure)
        return True

    def deliver(self, to: str, message: mail.Mail, what: str) -> bool:
        if self.mailer is None:
            return False
        subject, text, html = message
        try:
            self.mailer.send(to, subject, text, html, mail.images())
        except (OSError, smtplib.SMTPException) as exc:
            logger.warning("%s: sending failed: %s", what, exc)
            return False
        return True
