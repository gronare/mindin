# Run mindin for a team

An organisation lets a team share vaults on one server. Everyone at the company's email domain joins by signing in, keeps a personal vault of their own, and reads and writes the organisation's vaults, such as a wiki. This guide sets one up on a running server; [Install with Docker](../install/docker.md) gets you there.

## 1. Connect the company sign-in

Register mindin as a confidential client in your identity provider, with the callback `VAULT_PUBLIC_URL/oidc/callback`, and set:

```sh
VAULT_OIDC_ISSUER=https://id.example.com
VAULT_OIDC_CLIENT_ID=mindin
VAULT_OIDC_CLIENT_SECRET=...
VAULT_OIDC_NAME="Example sign-in"
```

The provider must send a verified email: an address it does not mark `email_verified` joins nothing. Email links, GitHub and a trusted proxy work too, since each gives a verified address; see [Set up sign-in methods](sign-in-methods.md).

## 2. Make the organisation

On the server's machine (in Docker, prefix each command with `docker compose exec vault`):

```sh
mindin org create "Example" --admin ada@example.com
mindin org domain add Example example.com
mindin org admin add Example bea@example.com
```

`--admin` names the first admin, who gets an account when they have none. A domain matches exactly: `example.com` does not let in `dev.example.com`, so add each subdomain on its own. A domain belongs to one organisation.

Whoever signs in with a verified address at a listed domain joins on that sign-in, with no invitation, also under `VAULT_REGISTRATION=invite`; an account that existed before joins on its next sign-in. An address outside the domains gets in only when an admin adds it.

From here on admins can do the rest on the Account page: under Organisation, Manage opens the organisation's page, with its domains, members and admins, vaults, and what happens when someone is removed.

## 3. Make the wiki

```sh
mindin org vault create Example "Wiki" --slug wiki
```

Or use New vault under Vaults on the organisation's page. Every member reaches it at once, at `VAULT_PUBLIC_URL/mcp?vault=wiki`, and reads and writes it. An organisation's vault is a [shared vault](../explanation/shared-vaults.md): it holds how things are now, and every change is kept with who made it and can be restored from the vault's page.

### Lock a vault

To let only some people change a vault, such as one with policies, lock it and name its editors:

```sh
mindin org vault lock Example policies
mindin org vault editor-add Example policies bea@example.com
```

Everyone else still reads it. A write from anyone but an editor is refused, through Claude and Obsidian alike, with a message naming the editors, so Claude can draft the change for one of them instead. `org vault unlock` opens it again.

## 4. Members connect Claude and Obsidian

Each member signs in at `VAULT_PUBLIC_URL/signin` with the company sign-in. Their Vaults page lists their personal vault and the organisation's vaults. On each vault's page they make their own keys and connections:

- claude.ai, Claude Desktop and mobile: add a custom connector with the vault's address, such as `VAULT_PUBLIC_URL/mcp?vault=wiki`. See [Connect claude.ai](connect-claude-ai.md).
- Claude Code and other terminals: `mindin login VAULT_PUBLIC_URL --vault wiki`. See [Sign a machine in](sign-in-a-machine.md).
- Obsidian: make a key on the vault's page and use it as the WebDAV password. See [Open a server vault in Obsidian](obsidian.md).

## Remove someone

```sh
mindin org removal Example export-and-delete --days 30
mindin org member remove Example cy@example.com
```

Removing a member stops their keys, tokens and connections to every organisation vault on their next request. What they wrote stays, under their name in each vault's history. A vault they owned passes to another admin. Their personal vaults follow the organisation's setting:

- `keep` (the default): the account stays, as an account of its own.
- `export-and-delete`: they get an email with a link to download their vaults, and the account and its vaults are deleted after the given days.

A removed person does not rejoin through the domain; an admin adds them back with `mindin org member add`. The last admin cannot be removed or lose the role.

`mindin org show Example` prints the domains, admins, members and vaults. [Command line](../reference/command-line.md#organisations) lists every `org` command.
