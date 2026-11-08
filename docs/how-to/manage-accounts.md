# Manage accounts and vaults

Run these on the server's machine, with the server's environment: in Docker, prefix each with `docker compose exec vault`; on Kubernetes, with `kubectl exec deploy/notes-vault-mcp --`. [Accounts and vaults](../explanation/accounts-and-vaults.md) explains what they act on.

## Let people in

New accounts are by invitation. `accounts invite` lets an address in the first time it signs in with a verified email:

```sh
mindin accounts invite ann@example.com
```

`VAULT_REGISTRATION=open` lets anyone with a verified email create an account. An [extension](../reference/extensions.md#hooks) can refuse a new account or put a check on the email-link form.

## Suspend, activate, delete

```sh
mindin accounts list
mindin accounts suspend ann@example.com     # every key and token stops at once
mindin accounts activate ann@example.com
mindin accounts delete ann@example.com --yes
```

`accounts delete` destroys every vault's key first, then their files, indexes, keys, connections and the account's ways in. It is refused while the account owns a [shared vault](../explanation/shared-vaults.md) that has other members; hand each one over or delete it first. People can also delete their own account on the Account page.

## Give an account more vaults

```sh
mindin accounts vaults ann@example.com                      # slug, name, notes, files, keys
mindin accounts add-vault ann@example.com "Workshop" --slug workshop --preset para
mindin accounts rename-vault ann@example.com workshop --name "Shed" --slug shed
```

A slug is one word of lowercase letters and digits, starting with a letter; without `--slug`, it is made from the name. `--preset` picks the vault's [layout](../explanation/notes-and-layouts.md#layouts). People make their own vaults on the Vaults page too.

## Share a vault

```sh
mindin accounts add-vault ann@example.com "Orchard club" --slug orchard --shared
mindin accounts add-member ann@example.com orchard bo@example.com
mindin accounts members ann@example.com orchard              # owner, members, invitations
mindin accounts remove-member ann@example.com orchard bo@example.com
```

With SMTP set up, `add-member` sends the address one email with a sign-in link. `remove-member` withdraws an invitation, or removes a member and revokes their keys to the vault. The owner does the same on the vault's page. See [Shared vaults](../explanation/shared-vaults.md).
