from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from mindin.config import ConfigError
from mindin.hosted.accounts.store import Account, HostedStore, MembershipError, VaultNameError
from mindin.hosted.config import auth_config, auth_dir
from mindin.hosted.orgs.mail import departure_mail
from mindin.hosted.orgs.organisations import (
    ADMIN,
    EXPORT_AND_DELETE,
    MEMBER,
    OPERATOR_METHOD,
    ORG_LAYOUTS,
    REMOVALS,
    Admission,
    Departure,
    Organisation,
    OrganisationError,
    Organisations,
    OrgDomain,
)
from mindin.hosted.orgs.rules import SHARING
from mindin.hosted.pages import mail
from mindin.hosted.signin.email import Mailer
from mindin.presets import WIKI_PRESET


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


def told_on_departure(organisations: Organisations, departure: Departure) -> str:
    if departure.delete_after is None:
        return f"mindin: removed {departure.account.email}; their account and own vaults stay"
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
            organisations.told(departure)
            told = "an email tells them how to download their vaults"
        except OSError as exc:
            told = f"the email could not be sent ({exc}); tell them to download their vaults from /account"
    email = departure.account.email
    return f"mindin: removed {email}; their account and own vaults go on {when} unless they sign in before; {told}"


def command_org(args: argparse.Namespace) -> int:
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
    if action in ("admin", "member") and change == "add":
        role = ADMIN if action == "admin" else MEMBER
        admission = organisations.add_member(organisation, args.email, role, OPERATOR_METHOD)
        print(admitted(organisation, admission, "an admin" if role == ADMIN else "a member"))
        return 0
    if action == "admin":
        account = organisations.remove_admin(organisation, args.email)
        print(f"mindin: {account.email} is a member, no longer an admin of {organisation.name}")
        return 0
    if action == "member":
        print(told_on_departure(organisations, organisations.remove_member(organisation, args.email)))
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


def admitted(organisation: Organisation, admission: Admission, role: str) -> str:
    email = admission.account.email
    if admission.pending:
        return f"mindin: {email} already has an account, so they join {organisation.name} once they accept on /account"
    return f"mindin: {email} is {role} of {organisation.name}"


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


def add_commands(sub: Any) -> None:
    org = sub.add_parser("org", help="organisations of the HTTP server: domains, admins, members and their vaults")
    org_sub = org.add_subparsers(dest="org_command", required=True)
    org_sub.add_parser("list", help="list the organisations").set_defaults(func=command_org)
    creating = org_sub.add_parser("create", help="make an organisation with its first admin")
    creating.add_argument("name")
    creating.add_argument("--admin", required=True, help="the first admin's email; gets an account if it has none")
    creating.set_defaults(func=command_org)
    showing = org_sub.add_parser("show", help="print the domains, members, admins and vaults")
    showing.add_argument("organisation", help="the organisation's name or id")
    showing.set_defaults(func=command_org)
    domain = org_sub.add_parser(
        "domain", help="the email domains whose verified addresses join on sign-in, once the domain is verified"
    )
    domain.add_argument("change", choices=("add", "check", "remove"))
    domain.add_argument("organisation", help="the organisation's name or id")
    domain.add_argument("domain")
    domain.add_argument("--verified", action="store_true", help="mark it verified without the DNS record")
    domain.add_argument("--allow-public", action="store_true", help="accept a public email provider's domain")
    domain.set_defaults(func=command_org)
    for name, noun, summary in (
        ("admin", "email", "the admins; the last one cannot be removed"),
        ("member", "email", "the members; removing one ends their access to the organisation's vaults at once"),
    ):
        group = org_sub.add_parser(name, help=summary)
        group.add_argument("change", choices=("add", "remove"))
        group.add_argument("organisation", help="the organisation's name or id")
        group.add_argument(noun)
        group.set_defaults(func=command_org)
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
    making.set_defaults(func=command_org)
    for name, summary in (("lock", "let only the vault's editors write"), ("unlock", "let every member write")):
        locking = vault_sub.add_parser(name, help=summary)
        locking.add_argument("organisation", help="the organisation's name or id")
        locking.add_argument("vault", help="the vault's slug")
        locking.set_defaults(func=command_org)
    for name, summary in (("editor-add", "name an editor of a locked vault"), ("editor-remove", "drop an editor")):
        editing = vault_sub.add_parser(name, help=summary)
        editing.add_argument("organisation", help="the organisation's name or id")
        editing.add_argument("vault", help="the vault's slug")
        editing.add_argument("email")
        editing.set_defaults(func=command_org)
    removal = org_sub.add_parser("removal", help="what happens to a removed member's own vaults")
    removal.add_argument("organisation", help="the organisation's name or id")
    removal.add_argument("setting", choices=REMOVALS)
    removal.add_argument("--days", type=int, help="days before an exported account is deleted")
    removal.set_defaults(func=command_org)
    sharing = org_sub.add_parser(
        "sharing", help="whether members may share their own shared vaults with people outside the organisation"
    )
    sharing.add_argument("organisation", help="the organisation's name or id")
    sharing.add_argument("setting", choices=SHARING)
    sharing.set_defaults(func=command_org)
