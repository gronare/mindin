from __future__ import annotations

import argparse
import re
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from mindin.auth import MACHINE_KEY_PURPOSE
from mindin.auth.hosted_store import Account, HostedStore, MembershipError, VaultNameError, account_of
from mindin.auth.hosted_vaults import SHARED, Vault, clash, reaches, vault_of
from mindin.config import env
from mindin.presets import SHARED_PRESET, WIKI_PRESET
from mindin.shared import Actor
from mindin.vault import Reachable

ORG_ID_PREFIX = "org_"
ADMIN = "admin"
MEMBER = "member"
KEEP = "keep"
EXPORT_AND_DELETE = "export-and-delete"
REMOVALS = (KEEP, EXPORT_AND_DELETE)
ALLOWED = "allowed"
DENIED = "denied"
SHARING = (ALLOWED, DENIED)
DEFAULT_REMOVAL_DAYS = 30
DOMAIN_RE = re.compile(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
Removal = Literal["keep", "export-and-delete"]
SPANNING_PURPOSES = (None, MACHINE_KEY_PURPOSE)
ORG_LAYOUTS = (WIKI_PRESET, SHARED_PRESET)
ORGANISATION_METHOD = "organisation"
VERIFY_LABEL = "_mindin-verify"
VERIFY_PREFIX = "mindin-verify="
DNS_TIMEOUT = 5.0
PUBLIC_PROVIDERS = frozenset(
    {"gmail", "googlemail", "outlook", "hotmail", "live", "msn", "yahoo", "ymail", "aol", "gmx", "yandex", "icloud"}
    | {"proton", "protonmail", "zoho", "mail", "tutanota"}
)
PUBLIC_DOMAINS = frozenset(
    {"me.com", "mac.com", "pm.me", "proton.me", "web.de", "t-online.de", "freenet.de", "qq.com", "163.com", "126.com"}
    | {"fastmail.com", "tuta.io", "hey.com", "comcast.net", "telia.com", "bredband.net", "laposte.net", "orange.fr"}
)
SECOND_LEVELS = frozenset({"co", "com", "net", "org", "ne", "or"})

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS organisations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    removal TEXT NOT NULL DEFAULT 'keep' CHECK (removal IN ('keep', 'export-and-delete')),
    removal_days INTEGER NOT NULL DEFAULT {DEFAULT_REMOVAL_DAYS},
    external_sharing TEXT NOT NULL DEFAULT 'denied' CHECK (external_sharing IN ('allowed', 'denied')),
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS organisation_domains (
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    domain TEXT NOT NULL COLLATE NOCASE,
    token TEXT NOT NULL,
    verified_at INTEGER,
    added_at INTEGER NOT NULL,
    checked_at INTEGER,
    checked_records INTEGER,
    PRIMARY KEY (organisation_id, domain)
);
CREATE UNIQUE INDEX IF NOT EXISTS organisation_domains_verified ON organisation_domains(domain)
    WHERE verified_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS organisation_members (
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    source TEXT NOT NULL,
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (organisation_id, account_id)
);
CREATE INDEX IF NOT EXISTS organisation_members_account ON organisation_members(account_id);
CREATE TABLE IF NOT EXISTS organisation_vaults (
    vault_id TEXT PRIMARY KEY REFERENCES vaults(id),
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    layout TEXT NOT NULL DEFAULT 'wiki',
    locked INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS organisation_editors (
    vault_id TEXT NOT NULL REFERENCES vaults(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    added_at INTEGER NOT NULL,
    PRIMARY KEY (vault_id, account_id)
);
CREATE TABLE IF NOT EXISTS organisation_departures (
    organisation_id TEXT NOT NULL REFERENCES organisations(id),
    email TEXT NOT NULL COLLATE NOCASE,
    former_account TEXT NOT NULL,
    removed_at INTEGER NOT NULL,
    delete_after INTEGER,
    PRIMARY KEY (organisation_id, email)
);
"""


class OrganisationError(ValueError):
    pass


@dataclass(frozen=True)
class OrgDomain:
    domain: str
    token: str
    verified_at: int | None
    checked_at: int | None = None
    checked_records: int = 0

    @property
    def verified(self) -> bool:
        return self.verified_at is not None

    @property
    def record_name(self) -> str:
        return f"{VERIFY_LABEL}.{self.domain}"

    @property
    def record_value(self) -> str:
        return VERIFY_PREFIX + self.token


@dataclass(frozen=True)
class Organisation:
    id: str
    name: str
    removal: Removal
    removal_days: int
    created_at: int
    external_sharing: str = DENIED


@dataclass(frozen=True)
class OrgMember:
    account: Account
    role: str
    source: str
    joined_at: int

    @property
    def admin(self) -> bool:
        return self.role == ADMIN


@dataclass(frozen=True)
class OrgVault:
    vault: Vault
    organisation: Organisation
    locked: bool
    editors: tuple[Account, ...]


@dataclass(frozen=True)
class Departure:
    account: Account
    organisation: Organisation
    delete_after: int | None


def now() -> int:
    return int(time.time())


def organisation_of(row: Any) -> Organisation | None:
    if row is None:
        return None
    return Organisation(
        row["id"], row["name"], row["removal"], row["removal_days"], row["created_at"], row["external_sharing"]
    )


def domain_of(email: str) -> str:
    return email.strip().lower().rpartition("@")[2].rstrip(".")


def clean_domain(raw: str) -> str:
    domain = raw.strip().lower().lstrip("@").rstrip(".")
    if not DOMAIN_RE.fullmatch(domain):
        raise OrganisationError(f"{raw.strip() or 'That'} is not an email domain, such as example.com.")
    return domain


def public_domains(raw: str | None = None) -> frozenset[str]:
    listed = env("VAULT_PUBLIC_EMAIL_DOMAINS") if raw is None else raw
    cleaned = (part.strip().lower().lstrip("@").rstrip(".") for part in (listed or "").split(","))
    return PUBLIC_DOMAINS | frozenset(domain for domain in cleaned if domain)


def public_provider(domain: str, listed: frozenset[str] = PUBLIC_DOMAINS) -> bool:
    labels = domain.split(".")
    if any(".".join(labels[start:]) in listed for start in range(len(labels) - 1)):
        return True
    return any(label in PUBLIC_PROVIDERS and suffix_only(labels[at + 1 :]) for at, label in enumerate(labels))


def suffix_only(rest: list[str]) -> bool:
    return len(rest) == 1 or (len(rest) == 2 and rest[0] in SECOND_LEVELS)


def txt_records(name: str) -> list[str]:
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT
    try:
        answer = resolver.resolve(name, "TXT")
    except dns.exception.DNSException:
        return []
    return [b"".join(record.strings).decode("utf-8", "replace") for record in answer]


def matching(connection: Any, emails: tuple[str, ...]) -> list[tuple[str, str]]:
    found = []
    for email in emails:
        row = connection.execute(
            "SELECT organisation_id FROM organisation_domains WHERE domain = ? AND verified_at IS NOT NULL",
            (domain_of(email),),
        ).fetchone()
        if row is not None:
            found.append((row["organisation_id"], email.strip().lower()))
    return found


def on_a_domain(connection: Any, emails: tuple[str, ...]) -> bool:
    return any(not departed(connection, org_id, email) for org_id, email in matching(connection, emails))


def departed(connection: Any, organisation_id: str, email: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM organisation_departures WHERE organisation_id = ? AND email = ?", (organisation_id, email)
        ).fetchone()
        is not None
    )


def is_member(connection: Any, organisation_id: str, account_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM organisation_members WHERE organisation_id = ? AND account_id = ?",
            (organisation_id, account_id),
        ).fetchone()
        is not None
    )


def org_vaults_of(connection: Any, organisation_id: str) -> list[Vault]:
    rows = connection.execute(
        "SELECT vaults.* FROM organisation_vaults JOIN vaults ON vaults.id = organisation_vaults.vault_id"
        " WHERE organisation_vaults.organisation_id = ? ORDER BY vaults.created_at, vaults.rowid",
        (organisation_id,),
    ).fetchall()
    return [vault for vault in map(vault_of, rows) if vault is not None]


def drop_other_claims(connection: Any, organisation_id: str, domain: str) -> None:
    connection.execute(
        "DELETE FROM organisation_domains WHERE domain = ? AND organisation_id != ? AND verified_at IS NULL",
        (domain, organisation_id),
    )


def admit_to_vault(connection: Any, vault: Vault, account_id: str, email: str, organisation_id: str) -> None:
    if vault.account_id == account_id:
        return
    if clash(connection, [account_id], vault.slug, vault.id) is not None:
        connection.execute(
            "INSERT INTO vault_invitations(vault_id, email, invited_by, created_at) VALUES(?, ?, ?, ?)"
            " ON CONFLICT (vault_id, email) DO NOTHING",
            (vault.id, email, organisation_id, now()),
        )
        return
    connection.execute(
        "INSERT INTO vault_members(vault_id, account_id, joined_at) VALUES(?, ?, ?)"
        " ON CONFLICT (vault_id, account_id) DO NOTHING",
        (vault.id, account_id, now()),
    )


def join(connection: Any, organisation_id: str, account_id: str, role: str = MEMBER, source: str = "domain") -> None:
    row = connection.execute("SELECT email FROM accounts WHERE id = ?", (account_id,)).fetchone()
    connection.execute(
        "INSERT INTO organisation_members(organisation_id, account_id, role, source, joined_at) VALUES(?, ?, ?, ?, ?)"
        " ON CONFLICT (organisation_id, account_id) DO NOTHING",
        (organisation_id, account_id, role, source, now()),
    )
    for vault in org_vaults_of(connection, organisation_id):
        admit_to_vault(connection, vault, account_id, row["email"].lower(), organisation_id)


def join_by_domain(connection: Any, account_id: str, emails: tuple[str, ...]) -> list[str]:
    joined = []
    for organisation_id, email in matching(connection, emails):
        if organisation_id in joined or departed(connection, organisation_id, email):
            continue
        if not is_member(connection, organisation_id, account_id):
            join(connection, organisation_id, account_id)
            joined.append(organisation_id)
    return joined


def domain_address(connection: Any, emails: tuple[str, ...]) -> str:
    return next((email for organisation_id, email in matching(connection, emails)), "")


def inside(connection: Any, organisation_id: str, email: str) -> bool:
    if departed(connection, organisation_id, email):
        return False
    member = connection.execute(
        "SELECT 1 FROM organisation_members JOIN accounts ON accounts.id = organisation_members.account_id"
        " WHERE organisation_members.organisation_id = ? AND accounts.email = ?",
        (organisation_id, email),
    ).fetchone()
    return member is not None or any(found == organisation_id for found, _ in matching(connection, (email,)))


def sharing_refusal(connection: Any, vault: Vault, email: str) -> str:
    owning = connection.execute(
        "SELECT organisations.* FROM organisation_vaults JOIN organisations"
        " ON organisations.id = organisation_vaults.organisation_id WHERE organisation_vaults.vault_id = ?",
        (vault.id,),
    ).fetchone()
    if owning is not None:
        if inside(connection, owning["id"], email):
            return ""
        return (
            f"{email} is outside {owning['name']}, and an organisation's vaults are only for its members. "
            f"An admin of {owning['name']} can add them to the organisation instead."
        )
    rows = connection.execute(
        "SELECT organisations.* FROM organisation_members JOIN organisations"
        " ON organisations.id = organisation_members.organisation_id"
        " WHERE organisation_members.account_id = ? AND organisations.external_sharing = ?"
        " ORDER BY organisations.created_at",
        (vault.account_id, DENIED),
    ).fetchall()
    for row in rows:
        if not inside(connection, row["id"], email):
            return (
                f"{row['name']} does not let its members share vaults with people outside it, and {email} is "
                f"outside. An admin of {row['name']} can allow it on the organisation's page."
            )
    return ""


def locked_out(connection: Any, vault_id: str, actor: Actor) -> str:
    row = connection.execute(
        "SELECT vaults.name, organisation_vaults.locked FROM organisation_vaults"
        " JOIN vaults ON vaults.id = organisation_vaults.vault_id WHERE organisation_vaults.vault_id = ?",
        (vault_id,),
    ).fetchone()
    if row is None or not row["locked"] or actor.account_id is None:
        return ""
    editors = connection.execute(
        "SELECT accounts.id, accounts.email FROM organisation_editors"
        " JOIN accounts ON accounts.id = organisation_editors.account_id WHERE organisation_editors.vault_id = ?"
        " ORDER BY organisation_editors.added_at, accounts.email",
        (vault_id,),
    ).fetchall()
    if any(editor["id"] == actor.account_id for editor in editors):
        return ""
    names = ", ".join(editor["email"] for editor in editors)
    who = f"its editors ({names})" if names else "its editors, and it has none yet"
    return (
        f"{row['name']} is locked: only {who} change it. Nothing was written. "
        "Write the change up as a suggestion for an editor instead."
    )


class Organisations:
    def __init__(
        self,
        store: HostedStore,
        clock: Callable[[], float] = time.time,
        resolve: Callable[[str], list[str]] | None = None,
        public: frozenset[str] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.resolve = resolve
        self.public = public_domains() if public is None else public

    def moment(self) -> int:
        return int(self.clock())

    def all(self) -> list[Organisation]:
        with self.store.connect() as connection:
            rows = connection.execute("SELECT * FROM organisations ORDER BY created_at, name").fetchall()
        return [found for found in map(organisation_of, rows) if found is not None]

    def get(self, ref: str) -> Organisation | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM organisations WHERE id = ? OR LOWER(name) = ? ORDER BY created_at LIMIT 1",
                (ref, ref.strip().lower()),
            ).fetchone()
        return organisation_of(row)

    def require(self, ref: str) -> Organisation:
        found = self.get(ref)
        if found is None:
            raise OrganisationError(f"There is no organisation {ref!r}.")
        return found

    def of_account(self, account_id: str) -> list[tuple[Organisation, str]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT organisations.*, organisation_members.role AS member_role FROM organisation_members"
                " JOIN organisations ON organisations.id = organisation_members.organisation_id"
                " WHERE organisation_members.account_id = ? ORDER BY organisations.created_at",
                (account_id,),
            ).fetchall()
        return [(found, row["member_role"]) for row in rows if (found := organisation_of(row)) is not None]

    def role(self, organisation: Organisation, account_id: str) -> str | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT role FROM organisation_members WHERE organisation_id = ? AND account_id = ?",
                (organisation.id, account_id),
            ).fetchone()
        return row["role"] if row else None

    def is_admin(self, organisation: Organisation, account_id: str) -> bool:
        return self.role(organisation, account_id) == ADMIN

    def create(self, name: str, admin_email: str) -> Organisation:
        cleaned = " ".join(name.split())[:64]
        if not cleaned:
            raise OrganisationError("Give the organisation a name.")
        if self.get(cleaned) is not None:
            raise OrganisationError(f"There already is an organisation called {cleaned}.")
        admin, _ = self.store.invite(admin_email, ORGANISATION_METHOD, cleaned)
        organisation_id = ORG_ID_PREFIX + secrets.token_urlsafe(12)
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO organisations(id, name, created_at) VALUES(?, ?, ?)",
                (organisation_id, cleaned, self.moment()),
            )
            join(connection, organisation_id, admin.id, ADMIN, "created")
        return self.require(organisation_id)

    def domains(self, organisation: Organisation) -> list[OrgDomain]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT domain, token, verified_at, checked_at, checked_records FROM organisation_domains"
                " WHERE organisation_id = ? ORDER BY domain",
                (organisation.id,),
            ).fetchall()
        return [
            OrgDomain(row["domain"], row["token"], row["verified_at"], row["checked_at"], row["checked_records"] or 0)
            for row in rows
        ]

    def domain(self, organisation: Organisation, raw: str) -> OrgDomain:
        domain = raw.strip().lower().lstrip("@").rstrip(".")
        found = next((item for item in self.domains(organisation) if item.domain == domain), None)
        if found is None:
            raise OrganisationError(f"{organisation.name} has no domain {raw.strip() or 'by that name'}.")
        return found

    def add_domain(
        self, organisation: Organisation, raw: str, verified: bool = False, allow_public: bool = False
    ) -> OrgDomain:
        domain = clean_domain(raw)
        if not allow_public and public_provider(domain, self.public):
            raise OrganisationError(
                f"{domain} is a public email provider: anyone can have an address there, so it cannot let people"
                " into an organisation."
            )
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT organisation_id, verified_at FROM organisation_domains WHERE domain = ?", (domain,)
            ).fetchall()
            claimed = any(row["organisation_id"] == organisation.id for row in rows)
            if claimed and not verified:
                raise OrganisationError(f"{domain} already belongs to this organisation.")
            if any(row["verified_at"] is not None and row["organisation_id"] != organisation.id for row in rows):
                raise OrganisationError(f"{domain} already belongs to another organisation.")
            moment = self.moment()
            if claimed:
                connection.execute(
                    "UPDATE organisation_domains SET verified_at = COALESCE(verified_at, ?)"
                    " WHERE organisation_id = ? AND domain = ?",
                    (moment, organisation.id, domain),
                )
            else:
                connection.execute(
                    "INSERT INTO organisation_domains(organisation_id, domain, token, verified_at, added_at)"
                    " VALUES(?, ?, ?, ?, ?)",
                    (organisation.id, domain, secrets.token_urlsafe(24), moment if verified else None, moment),
                )
            if verified:
                drop_other_claims(connection, organisation.id, domain)
        return self.domain(organisation, domain)

    def check_domain(self, organisation: Organisation, raw: str) -> OrgDomain:
        claim = self.domain(organisation, raw)
        if claim.verified:
            return claim
        found = (self.resolve or txt_records)(claim.record_name)
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE organisation_domains SET checked_at = ?, checked_records = ? WHERE organisation_id = ?"
                " AND domain = ?",
                (self.moment(), len(found), organisation.id, claim.domain),
            )
        if claim.record_value not in (record.strip() for record in found):
            raise OrganisationError(
                f"No TXT record {claim.record_value} at {claim.record_name} yet. DNS changes can take a while to"
                " show; check again later."
            )
        try:
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE organisation_domains SET verified_at = ? WHERE organisation_id = ? AND domain = ?",
                    (self.moment(), organisation.id, claim.domain),
                )
                drop_other_claims(connection, organisation.id, claim.domain)
        except self.store.database.IntegrityError as exc:
            raise OrganisationError(f"{claim.domain} already belongs to another organisation.") from exc
        return self.domain(organisation, claim.domain)

    def remove_domain(self, organisation: Organisation, raw: str) -> bool:
        with self.store.connect() as connection:
            return (
                connection.execute(
                    "DELETE FROM organisation_domains WHERE organisation_id = ? AND domain = ?",
                    (organisation.id, raw.strip().lower().lstrip("@").rstrip(".")),
                ).rowcount
                > 0
            )

    def members(self, organisation: Organisation) -> list[OrgMember]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT accounts.*, organisation_members.role AS member_role, organisation_members.source AS source,"
                " organisation_members.joined_at AS joined FROM organisation_members"
                " JOIN accounts ON accounts.id = organisation_members.account_id"
                " WHERE organisation_members.organisation_id = ?"
                " ORDER BY organisation_members.role, accounts.email",
                (organisation.id,),
            ).fetchall()
        return [
            OrgMember(account, row["member_role"], row["source"], row["joined"])
            for row in rows
            if (account := account_of(row)) is not None
        ]

    def admins(self, organisation: Organisation) -> list[Account]:
        return [member.account for member in self.members(organisation) if member.admin]

    def member_account(self, organisation: Organisation, email: str) -> Account:
        account = self.store.account(email.strip().lower())
        with self.store.connect() as connection:
            if account is None or not is_member(connection, organisation.id, account.id):
                raise OrganisationError(f"{email.strip()} is not a member of {organisation.name}.")
        return account

    def add_member(self, organisation: Organisation, email: str, role: str = MEMBER) -> Account:
        address = " ".join(email.split()).lower()
        if "@" not in address or len(address) > 254:
            raise OrganisationError("Enter an email address.")
        account, _ = self.store.invite(address, ORGANISATION_METHOD, organisation.name)
        with self.store.connect() as connection:
            connection.execute(
                "DELETE FROM organisation_departures WHERE organisation_id = ? AND email = ?",
                (organisation.id, address),
            )
            join(connection, organisation.id, account.id, role, "admin")
            if role == ADMIN:
                connection.execute(
                    "UPDATE organisation_members SET role = 'admin' WHERE organisation_id = ? AND account_id = ?",
                    (organisation.id, account.id),
                )
        return account

    def add_admin(self, organisation: Organisation, email: str) -> Account:
        return self.add_member(organisation, email, ADMIN)

    def remove_admin(self, organisation: Organisation, email: str) -> Account:
        account = self.member_account(organisation, email)
        with self.store.connect() as connection:
            self.keep_an_admin(connection, organisation, account.id)
            connection.execute(
                "UPDATE organisation_members SET role = 'member' WHERE organisation_id = ? AND account_id = ?",
                (organisation.id, account.id),
            )
        return account

    def keep_an_admin(self, connection: Any, organisation: Organisation, account_id: str) -> None:
        others = connection.execute(
            "SELECT COUNT(*) AS admins FROM organisation_members WHERE organisation_id = ? AND role = 'admin'"
            " AND account_id != ?",
            (organisation.id, account_id),
        ).fetchone()["admins"]
        role = connection.execute(
            "SELECT role FROM organisation_members WHERE organisation_id = ? AND account_id = ?",
            (organisation.id, account_id),
        ).fetchone()
        if role is not None and role["role"] == ADMIN and others == 0:
            raise OrganisationError(f"That is the last admin of {organisation.name}. Make someone else admin first.")

    def set_removal(self, organisation: Organisation, removal: str, days: int | None = None) -> Organisation:
        if removal not in REMOVALS:
            raise OrganisationError(f"A removed member's own vaults are either {' or '.join(REMOVALS)}.")
        kept = organisation.removal_days if days is None else days
        if kept < 1 or kept > 365:
            raise OrganisationError("Keep a removed member's vaults between 1 and 365 days.")
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE organisations SET removal = ?, removal_days = ? WHERE id = ?", (removal, kept, organisation.id)
            )
        return self.require(organisation.id)

    def set_external_sharing(self, organisation: Organisation, setting: str) -> Organisation:
        if setting not in SHARING:
            raise OrganisationError(f"Sharing outside the organisation is either {' or '.join(SHARING)}.")
        with self.store.connect() as connection:
            connection.execute("UPDATE organisations SET external_sharing = ? WHERE id = ?", (setting, organisation.id))
        return self.require(organisation.id)

    def remove_member(self, organisation: Organisation, email: str) -> Departure:
        account = self.member_account(organisation, email)
        admins = [admin for admin in self.admins(organisation) if admin.id != account.id]
        with self.store.connect() as connection:
            self.keep_an_admin(connection, organisation, account.id)
            vaults = org_vaults_of(connection, organisation.id)
        vaults = [self.hand_to_an_admin(vault, admins) if vault.account_id == account.id else vault for vault in vaults]
        for vault in vaults:
            self.store.remove_member(vault, account.id)
        moment = self.moment()
        delete_after = moment + organisation.removal_days * 86400 if organisation.removal == EXPORT_AND_DELETE else None
        with self.store.connect() as connection:
            marks = ", ".join("?" for _ in vaults) or "''"
            connection.execute(
                f"DELETE FROM organisation_editors WHERE account_id = ? AND vault_id IN ({marks})",
                (account.id, *(vault.id for vault in vaults)),
            )
            connection.execute(
                f"DELETE FROM vault_invitations WHERE email = ? AND vault_id IN ({marks})",
                (account.email.lower(), *(vault.id for vault in vaults)),
            )
            connection.execute(
                "DELETE FROM organisation_members WHERE organisation_id = ? AND account_id = ?",
                (organisation.id, account.id),
            )
            connection.execute(
                "INSERT INTO organisation_departures(organisation_id, email, former_account, removed_at, delete_after)"
                " VALUES(?, ?, ?, ?, ?) ON CONFLICT (organisation_id, email) DO UPDATE SET"
                " former_account = excluded.former_account, removed_at = excluded.removed_at,"
                " delete_after = excluded.delete_after",
                (organisation.id, account.email.lower(), account.id, moment, delete_after),
            )
        return Departure(account, organisation, delete_after)

    def hand_to_an_admin(self, vault: Vault, admins: list[Account]) -> Vault:
        for admin in admins:
            try:
                return self.store.hand_over(vault, admin.id)
            except MembershipError:
                continue
        raise OrganisationError(
            f"No other admin can take over {vault.name}; one already reaches another vault at {vault.slug}."
        )

    def due(self) -> list[tuple[str, str]]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT organisation_id, former_account FROM organisation_departures"
                " WHERE delete_after IS NOT NULL AND delete_after <= ?",
                (self.moment(),),
            ).fetchall()
        return [(row["organisation_id"], row["former_account"]) for row in rows]

    def settle(self, organisation_id: str, account_id: str) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE organisation_departures SET delete_after = NULL"
                " WHERE organisation_id = ? AND former_account = ?",
                (organisation_id, account_id),
            )

    def create_vault(
        self, organisation: Organisation, admin: Account, name: str, slug: str | None = None, layout: str = WIKI_PRESET
    ) -> Vault:
        if not self.is_admin(organisation, admin.id):
            raise OrganisationError(f"Only an admin of {organisation.name} makes its vaults.")
        if layout not in ORG_LAYOUTS:
            raise OrganisationError(f"An organisation's vault is laid out as {' or '.join(ORG_LAYOUTS)}.")
        vault = self.store.create_vault(admin.id, name, slug, SHARED)
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO organisation_vaults(vault_id, organisation_id, layout, created_at) VALUES(?, ?, ?, ?)",
                (vault.id, organisation.id, layout, self.moment()),
            )
            for member in connection.execute(
                "SELECT accounts.id, accounts.email FROM organisation_members"
                " JOIN accounts ON accounts.id = organisation_members.account_id"
                " WHERE organisation_members.organisation_id = ?",
                (organisation.id,),
            ).fetchall():
                admit_to_vault(connection, vault, member["id"], member["email"].lower(), organisation.id)
        return vault

    def vaults(self, organisation: Organisation) -> list[OrgVault]:
        with self.store.connect() as connection:
            vaults = org_vaults_of(connection, organisation.id)
        return [found for vault in vaults if (found := self.org_vault(vault)) is not None]

    def org_vault(self, vault: Vault) -> OrgVault | None:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT organisation_id, locked FROM organisation_vaults WHERE vault_id = ?", (vault.id,)
            ).fetchone()
            if row is None:
                return None
            organisation = organisation_of(
                connection.execute("SELECT * FROM organisations WHERE id = ?", (row["organisation_id"],)).fetchone()
            )
            editors = connection.execute(
                "SELECT accounts.* FROM organisation_editors"
                " JOIN accounts ON accounts.id = organisation_editors.account_id"
                " WHERE organisation_editors.vault_id = ? ORDER BY organisation_editors.added_at, accounts.email",
                (vault.id,),
            ).fetchall()
        assert organisation is not None
        named = tuple(account for editor in editors if (account := account_of(editor)) is not None)
        return OrgVault(vault, organisation, bool(row["locked"]), named)

    def vault_named(self, organisation: Organisation, slug: str) -> OrgVault:
        found = next((item for item in self.vaults(organisation) if slug in (item.vault.slug, item.vault.id)), None)
        if found is None:
            raise OrganisationError(f"{organisation.name} has no vault {slug!r}.")
        return found

    def lock(self, item: OrgVault, locked: bool) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE organisation_vaults SET locked = ? WHERE vault_id = ?", (1 if locked else 0, item.vault.id)
            )

    def add_editor(self, item: OrgVault, email: str) -> Account:
        account = self.member_account(item.organisation, email)
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO organisation_editors(vault_id, account_id, added_at) VALUES(?, ?, ?)"
                " ON CONFLICT (vault_id, account_id) DO NOTHING",
                (item.vault.id, account.id, self.moment()),
            )
        return account

    def remove_editor(self, item: OrgVault, email: str) -> bool:
        account = self.store.account(email.strip().lower())
        if account is None:
            return False
        with self.store.connect() as connection:
            return (
                connection.execute(
                    "DELETE FROM organisation_editors WHERE vault_id = ? AND account_id = ?",
                    (item.vault.id, account.id),
                ).rowcount
                > 0
            )


class OrganisationReach:
    def __init__(self, store: HostedStore) -> None:
        self.store = store

    def around(self, claims: dict[str, Any], subject: str) -> list[Reachable]:
        account_id = claims.get("account")
        if not account_id or claims.get("key_purpose") not in SPANNING_PURPOSES:
            return []
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT vaults.*, organisations.name AS organisation_name FROM organisation_members"
                " JOIN organisations ON organisations.id = organisation_members.organisation_id"
                " JOIN organisation_vaults ON organisation_vaults.organisation_id = organisations.id"
                " JOIN vaults ON vaults.id = organisation_vaults.vault_id WHERE organisation_members.account_id = ?"
                " ORDER BY organisations.created_at, vaults.created_at, vaults.rowid",
                (account_id,),
            ).fetchall()
            shared = {
                vault.id: (vault, row["organisation_name"])
                for row in rows
                if (vault := vault_of(row)) is not None and reaches(connection, vault, account_id)
            }
            if not shared:
                return []
            session = vault_of(connection.execute("SELECT * FROM vaults WHERE id = ?", (subject,)).fetchone())
            own = vault_of(
                connection.execute(
                    "SELECT * FROM vaults WHERE account_id = ? ORDER BY created_at, rowid LIMIT 1", (account_id,)
                ).fetchone()
            )
        found: dict[str, Reachable] = {}
        for vault in (session, own, *(vault for vault, _ in shared.values())):
            if vault is not None and vault.id not in found:
                organisation = shared[vault.id][1] if vault.id in shared else ""
                found[vault.id] = Reachable(vault.id, vault.slug, vault.name, organisation)
        return list(found.values()) if len(found) > 1 else []


def describe(organisations: Organisations, organisation: Organisation) -> list[str]:
    lines = [f"{organisation.id}  {organisation.name}  removed members: {organisation.removal}"]
    if organisation.removal == EXPORT_AND_DELETE:
        lines[0] += f" after {organisation.removal_days} days"
    lines[0] += f"  sharing outside: {organisation.external_sharing}"
    lines += [f"domain  @{item.domain}  {domain_state(item)}" for item in organisations.domains(organisation)]
    lines += [f"{member.role}  {member.account.email}" for member in organisations.members(organisation)]
    for item in organisations.vaults(organisation):
        state = "locked, editors: " + (", ".join(e.email for e in item.editors) or "none") if item.locked else "open"
        lines.append(f"vault  {item.vault.slug}  {item.vault.name}  {state}")
    return lines


def domain_state(item: OrgDomain) -> str:
    return "verified" if item.verified else f"unverified: TXT {item.record_name} {item.record_value}"


def told_on_departure(departure: Departure) -> str:
    if departure.delete_after is None:
        return f"mindin: removed {departure.account.email}; their account and own vaults stay"
    from mindin.auth import auth_config, mail
    from mindin.auth.hosted_org_pages import departure_mail
    from mindin.auth.login_email import Mailer
    from mindin.config import ConfigError

    when = time.strftime("%Y-%m-%d", time.gmtime(departure.delete_after))
    told = "no email was sent; tell them to download their vaults from /account"
    try:
        config = auth_config()
    except ConfigError:
        config = None
    if config is not None and config.smtp:
        subject, text, html = departure_mail(config.public_url, departure)
        try:
            Mailer(config.smtp).send(departure.account.email, subject, text, html, mail.images())
            told = "an email tells them how to download their vaults"
        except OSError as exc:
            told = f"the email could not be sent ({exc}); tell them to download their vaults from /account"
    return f"mindin: removed {departure.account.email}; their account and own vaults go on {when}; {told}"


def command_org(args: argparse.Namespace) -> int:
    from mindin.auth import auth_dir

    store = HostedStore(auth_dir())
    organisations = Organisations(store)
    action = args.org_command
    try:
        if action == "list":
            found = organisations.all()
            if not found:
                print("mindin: no organisations yet; `mindin org create <name> --admin <email>` makes one")
            for organisation in found:
                members, domains = organisations.members(organisation), organisations.domains(organisation)
                print(f"{organisation.id}  {organisation.name}  {len(members)} members  {len(domains)} domains")
            return 0
        if action == "create":
            organisation = organisations.create(args.name, args.admin)
            print(f"mindin: made {organisation.name} ({organisation.id}) with the admin {args.admin.strip().lower()}")
            return 0
        organisation = organisations.require(args.organisation)
        if action == "show":
            print("\n".join(describe(organisations, organisation)))
            return 0
        if action == "removal":
            changed = organisations.set_removal(organisation, args.setting, args.days)
            print(f"mindin: removed members of {changed.name}: {changed.removal}, {changed.removal_days} days")
            return 0
        if action == "sharing":
            changed = organisations.set_external_sharing(organisation, args.setting)
            print(f"mindin: members of {changed.name} sharing their own vaults outside it: {changed.external_sharing}")
            return 0
        return command_org_change(organisations, organisation, args)
    except (OrganisationError, MembershipError, VaultNameError) as exc:
        print(f"mindin: {exc}", file=sys.stderr)
        return 2


def command_org_change(organisations: Organisations, organisation: Organisation, args: argparse.Namespace) -> int:
    action, change = args.org_command, args.change
    if action == "domain":
        if change == "add":
            item = organisations.add_domain(organisation, args.domain, args.verified, args.allow_public)
            print(domain_told(organisation, item))
        elif change == "check":
            print(domain_told(organisation, organisations.check_domain(organisation, args.domain)))
        elif organisations.remove_domain(organisation, args.domain):
            print(f"mindin: {args.domain} no longer joins {organisation.name}; its members stay")
        else:
            print(f"mindin: {organisation.name} has no domain {args.domain}", file=sys.stderr)
            return 1
        return 0
    if action == "admin":
        account = (organisations.add_admin if change == "add" else organisations.remove_admin)(organisation, args.email)
        role = "an admin" if change == "add" else "a member, no longer an admin"
        print(f"mindin: {account.email} is {role} of {organisation.name}")
        return 0
    if action == "member":
        if change == "add":
            account = organisations.add_member(organisation, args.email)
            print(f"mindin: {account.email} is a member of {organisation.name}")
            return 0
        print(told_on_departure(organisations.remove_member(organisation, args.email)))
        return 0
    if change == "create":
        admins = organisations.admins(organisation)
        by = store_account(organisations, args.by) if args.by else (admins[0] if admins else None)
        if by is None:
            raise OrganisationError(f"{organisation.name} has no admin to own the vault.")
        vault = organisations.create_vault(organisation, by, args.name, args.slug, args.layout)
        print(f"mindin: {organisation.name} has the vault {vault.name} at /mcp?vault={vault.slug}")
        return 0
    item = organisations.vault_named(organisation, args.vault)
    if change in ("lock", "unlock"):
        organisations.lock(item, change == "lock")
        state = "locked: only its editors write" if change == "lock" else "open: every member writes"
        print(f"mindin: {item.vault.name} is {state}")
        return 0
    if change == "editor-add":
        editor = organisations.add_editor(item, args.email)
        print(f"mindin: {editor.email} edits {item.vault.name}")
        return 0
    if not organisations.remove_editor(item, args.email):
        print(f"mindin: {args.email} does not edit {item.vault.name}", file=sys.stderr)
        return 1
    print(f"mindin: {args.email.strip().lower()} no longer edits {item.vault.name}")
    return 0


def domain_told(organisation: Organisation, item: OrgDomain) -> str:
    if item.verified:
        return f"mindin: addresses at {item.domain} join {organisation.name} when they sign in"
    return (
        f"mindin: {item.domain} joins {organisation.name} once verified: add a TXT record at {item.record_name}"
        f" with the value {item.record_value}, then run `mindin org domain check {organisation.id} {item.domain}`"
    )


def store_account(organisations: Organisations, ref: str) -> Account:
    account = organisations.store.account(ref)
    if account is None:
        raise OrganisationError(f"There is no account {ref!r}.")
    return account


def add_commands(sub: Any, run: Callable[[argparse.Namespace], int]) -> None:
    org = sub.add_parser("org", help="organisations of the HTTP server: domains, admins, members and their vaults")
    org_sub = org.add_subparsers(dest="org_command", required=True)
    org_sub.add_parser("list", help="list the organisations").set_defaults(func=run)
    creating = org_sub.add_parser("create", help="make an organisation with its first admin")
    creating.add_argument("name")
    creating.add_argument("--admin", required=True, help="the first admin's email; gets an account if it has none")
    creating.set_defaults(func=run)
    showing = org_sub.add_parser("show", help="print the domains, members, admins and vaults")
    showing.add_argument("organisation", help="the organisation's name or id")
    showing.set_defaults(func=run)
    domain = org_sub.add_parser(
        "domain", help="the email domains whose verified addresses join on sign-in, once the domain is verified"
    )
    domain.add_argument("change", choices=("add", "check", "remove"))
    domain.add_argument("organisation", help="the organisation's name or id")
    domain.add_argument("domain")
    domain.add_argument("--verified", action="store_true", help="mark it verified without the DNS record")
    domain.add_argument("--allow-public", action="store_true", help="accept a public email provider's domain")
    domain.set_defaults(func=run)
    for name, noun, summary in (
        ("admin", "email", "the admins; the last one cannot be removed"),
        ("member", "email", "the members; removing one ends their access to the organisation's vaults at once"),
    ):
        group = org_sub.add_parser(name, help=summary)
        group.add_argument("change", choices=("add", "remove"))
        group.add_argument("organisation", help="the organisation's name or id")
        group.add_argument(noun)
        group.set_defaults(func=run)
    vault = org_sub.add_parser("vault", help="the organisation's vaults, their lock and their editors")
    vault_sub = vault.add_subparsers(dest="change", required=True)
    making = vault_sub.add_parser("create", help="make a vault every member reads and writes")
    making.add_argument("organisation", help="the organisation's name or id")
    making.add_argument("name")
    making.add_argument("--slug", help="the vault's slug, as in /mcp?vault=<slug>: one lowercase word")
    making.add_argument("--by", help="the admin who owns it; defaults to the first admin")
    making.add_argument(
        "--layout", choices=ORG_LAYOUTS, default=WIKI_PRESET, help="the vault's layout; wiki by default"
    )
    making.set_defaults(func=run)
    for name, summary in (("lock", "let only the vault's editors write"), ("unlock", "let every member write")):
        locking = vault_sub.add_parser(name, help=summary)
        locking.add_argument("organisation", help="the organisation's name or id")
        locking.add_argument("vault", help="the vault's slug")
        locking.set_defaults(func=run)
    for name, summary in (("editor-add", "name an editor of a locked vault"), ("editor-remove", "drop an editor")):
        editing = vault_sub.add_parser(name, help=summary)
        editing.add_argument("organisation", help="the organisation's name or id")
        editing.add_argument("vault", help="the vault's slug")
        editing.add_argument("email")
        editing.set_defaults(func=run)
    removal = org_sub.add_parser("removal", help="what happens to a removed member's own vaults")
    removal.add_argument("organisation", help="the organisation's name or id")
    removal.add_argument("setting", choices=REMOVALS)
    removal.add_argument("--days", type=int, help="days before an exported account is deleted")
    removal.set_defaults(func=run)
    sharing = org_sub.add_parser(
        "sharing", help="whether members may share their own shared vaults with people outside the organisation"
    )
    sharing.add_argument("organisation", help="the organisation's name or id")
    sharing.add_argument("setting", choices=SHARING)
    sharing.set_defaults(func=run)
