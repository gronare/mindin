from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from mindin.hosted.accounts.store import Account, HostedStore
from mindin.hosted.accounts.vaults import Vault
from mindin.hosted.config import AuthConfig
from mindin.hosted.database import Connection, Row
from mindin.hosted.pages import mail
from mindin.hosted.signin.email import Mailer, SmtpFactory

logger = logging.getLogger(__name__)

GRANT_CHOICES = {"hour": ("one hour", 3600), "day": ("one day", 86400), "week": ("seven days", 7 * 86400)}
LOG_ROWS = 50


class NoGrant(PermissionError):
    pass


class RequestRefused(ValueError):
    pass


@dataclass(frozen=True)
class SupportRequest:
    id: int
    account: Account
    vault: Vault | None
    operator: str
    reason: str
    requested_at: int

    @property
    def vault_label(self) -> str:
        return self.vault.name if self.vault else "any of your vaults"


def one_line(text: str) -> str:
    return " ".join(text.split())


class SupportAccess:
    def __init__(self, store: HostedStore, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    def grant(self, vault: Vault, seconds: int) -> int:
        moment = int(self.clock())
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO vault_support_grants(vault_id, account_id, granted_at, expires_at) VALUES(?, ?, ?, ?)"
                " ON CONFLICT (vault_id) DO UPDATE SET account_id = excluded.account_id,"
                " granted_at = excluded.granted_at, expires_at = excluded.expires_at",
                (vault.id, vault.account_id, moment, moment + seconds),
            )
        return moment + seconds

    def revoke(self, account_id: str, vault_id: str) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "DELETE FROM vault_support_grants WHERE vault_id = ? AND account_id = ?", (vault_id, account_id)
            )

    def grant_until(self, vault_id: str) -> int | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM vault_support_grants WHERE vault_id = ? AND expires_at > ?",
                (vault_id, int(self.clock())),
            ).fetchone()
        return row["expires_at"] if row else None

    def ask(self, account: Account, vault: Vault | None, operator: str, reason: str) -> SupportRequest:
        operator, reason = one_line(operator), one_line(reason)
        if not operator or not reason:
            raise RequestRefused("an operator and a reason are required")
        if vault is not None and vault.account_id != account.id:
            raise RequestRefused(f"{account.email} does not own the vault {vault.slug}")
        with self.store.connect() as connection:
            if self.pending_row(connection, account.id) is not None:
                raise RequestRefused(f"{account.email} already has a support request waiting; withdraw it first")
            moment = int(self.clock())
            row = connection.execute(
                "INSERT INTO vault_support_requests(account_id, vault_id, operator, reason, requested_at)"
                " VALUES(?, ?, ?, ?, ?) RETURNING id",
                (account.id, vault.id if vault else None, operator, reason, moment),
            ).fetchone()
        return SupportRequest(row["id"], account, vault, operator, reason, moment)

    def pending_row(self, connection: Connection, account_id: str) -> Row | None:
        return connection.execute(
            "SELECT * FROM vault_support_requests WHERE account_id = ? AND answer IS NULL", (account_id,)
        ).fetchone()

    def request_of(self, row: Row) -> SupportRequest | None:
        account = self.store.account_by_id(row["account_id"])
        if account is None:
            return None
        vault = self.store.vault(row["vault_id"]) if row["vault_id"] else None
        return SupportRequest(row["id"], account, vault, row["operator"], row["reason"], row["requested_at"])

    def pending(self, account_id: str) -> SupportRequest | None:
        with self.store.connect() as connection:
            row = self.pending_row(connection, account_id)
        return self.request_of(row) if row else None

    def requests(self) -> list[SupportRequest]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM vault_support_requests WHERE answer IS NULL ORDER BY requested_at, id"
            ).fetchall()
        return [found for row in rows if (found := self.request_of(row)) is not None]

    def answer(self, account_id: str, answer: str) -> SupportRequest | None:
        found = self.pending(account_id)
        if found is None:
            return None
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE vault_support_requests SET answer = ?, answered_at = ? WHERE id = ? AND answer IS NULL",
                (answer, int(self.clock()), found.id),
            )
        return found

    def withdraw(self, account_id: str) -> bool:
        return self.answer(account_id, "withdrawn") is not None

    def decline(self, account_id: str) -> bool:
        return self.answer(account_id, "declined") is not None

    def approve(self, account: Account, vault: Vault, seconds: int) -> int | None:
        found = self.pending(account.id)
        if found is None or vault.account_id != account.id or (found.vault and found.vault.id != vault.id):
            return None
        self.answer(account.id, "approved")
        return self.grant(vault, seconds)

    def admit(self, vault: Vault, operator: str, reason: str, action: str) -> None:
        operator, reason = one_line(operator), one_line(reason)
        if not operator or not reason:
            raise NoGrant("an operator and a reason are required")
        with self.store.connect() as connection:
            live = connection.execute(
                "SELECT 1 FROM vault_support_grants WHERE vault_id = ? AND expires_at > ?",
                (vault.id, int(self.clock())),
            ).fetchone()
            if live is None:
                raise NoGrant(f"the vault {vault.slug} has no support grant, or the grant has expired")
            connection.execute(
                "INSERT INTO vault_access(account_id, vault_id, at, operator, reason, action) VALUES(?, ?, ?, ?, ?, ?)",
                (vault.account_id, vault.id, int(self.clock()), operator, reason, action),
            )

    def log(self, account_id: str, limit: int = LOG_ROWS) -> list[Row]:
        with self.store.connect() as connection:
            return connection.execute(
                "SELECT vault_access.at, vault_access.operator, vault_access.reason, vault_access.action,"
                " vaults.name AS vault_name FROM vault_access LEFT JOIN vaults ON vaults.id = vault_access.vault_id"
                " WHERE vault_access.account_id = ? ORDER BY vault_access.at DESC, vault_access.id DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()


def chosen_vault(store: HostedStore, account: Account, ref: str | None) -> Vault | None:
    if ref:
        return store.own_vault(account.id, ref)
    vaults = store.vaults(account.id)
    return vaults[0] if len(vaults) == 1 else None


def request_mail(public_url: str, request: SupportRequest) -> mail.Mail:
    which = f"your vault {request.vault.name}" if request.vault else "one of your vaults"
    return mail.branded(
        "Support asks to open your vault",
        "Support asks to open your vault",
        f"{request.operator} from support on {mail.host_of(public_url)} asks to open {which}: {request.reason}",
        "Open your account page",
        f"{public_url.rstrip('/')}/account/settings",
        "Approve it there for a set time, or decline it. Every look support takes is logged on that page.",
        "Nothing is opened unless you approve.",
    )


def ask_for_support(
    store: HostedStore,
    config: AuthConfig | None,
    account: Account,
    vault: Vault | None,
    operator: str,
    reason: str,
    smtp_factory: SmtpFactory | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[SupportRequest, bool]:
    """Record a support request and email the account; False when no email went out and it waits on the page."""
    request = SupportAccess(store, clock).ask(account, vault, operator, reason)
    if config is None or config.smtp is None:
        return request, False
    subject, text, html = request_mail(config.public_url, request)
    try:
        Mailer(config.smtp, smtp_factory).send(account.email, subject, text, html, mail.images())
    except OSError as exc:
        logger.warning("mindin: the support request email to %s failed: %s", account.email, exc)
        return request, False
    return request, True
