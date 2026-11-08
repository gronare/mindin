from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from mindin.auth.hosted_keys import SealedVaults, StillShared, delete_account, delete_vault
from mindin.auth.hosted_store import Account
from mindin.auth.hosted_vaults import Vault
from mindin.extensions import may_erase

if TYPE_CHECKING:
    from mindin.auth.hosted_orgs import Organisations
    from mindin.vault import SubjectVaults

logger = logging.getLogger(__name__)


def erase_vault(vaults: SubjectVaults, sealed: SealedVaults, vault: Vault) -> int:
    vaults.discard(vault.id)
    deleted = delete_vault(sealed, vault, vaults.cache)
    vaults.publish(vault.id, "gone")
    return deleted


def erase_account(vaults: SubjectVaults, sealed: SealedVaults, account: Account) -> None:
    owned = sealed.store.vaults(account.id)
    for vault in owned:
        vaults.discard(vault.id)
    delete_account(sealed, account, vaults.cache)
    for vault in owned:
        vaults.publish(vault.id, "gone")


def delete_departed(organisations: Organisations, erase: Callable[[Account], None]) -> int:
    deleted = 0
    for organisation_id, account_id in organisations.due():
        account = organisations.store.account_by_id(account_id)
        if account is not None and not may_erase(organisations.store.extensions, account):
            logger.info("organisation departure: an extension keeps %s", account.id)
        elif account is not None and not organisations.of_account(account.id):
            try:
                erase(account)
                deleted += 1
                organisations.settle(organisation_id, account_id, erased=True)
                continue
            except StillShared as exc:
                logger.warning("organisation departure: %s; the account stays", exc)
        organisations.settle(organisation_id, account_id)
    return deleted
