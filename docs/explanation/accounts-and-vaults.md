# Accounts and vaults on a server

A vault server serves a household, a team, or you on several machines. People sign in to accounts, and each account owns one or more vaults, each with its own address, keys, connections, index and encryption key.

The vaults live in the configured folder or bucket, one under `VAULT_SUBJECT_PREFIX/<vault id>/` each. Search by meaning works as on one machine, with one model shared by every account.

## Vaults

A new account starts with one vault, named Personal, with the slug `notes`. Each person opens the Vaults page, `VAULT_PUBLIC_URL/account`, which lists their vaults and makes new ones. A vault has a name and a slug: one word of lowercase letters and digits, starting with a letter, unique among the vaults one account reaches. The server's address is `VAULT_PUBLIC_URL/mcp`, where a client reaches the vault picked when it connects; `VAULT_PUBLIC_URL/mcp?vault=<slug>` reaches that one vault. A vault is personal or [shared](shared-vaults.md), chosen when it is made.

Each personal vault's page has:

- **Connect.** The vault's MCP address, the `login` line for a computer, and the WebDAV address.
- **Connections and keys.** Every connector, signed-in computer and key that reaches this vault, with when it was last used; disconnect, sign out or revoke each one, and make a key.
- **Layout.** The vault's [layout](notes-and-layouts.md#switching-layouts), and switching to another.
- **Your data.** The vault downloads as a zip at any time; a zip imports into it, replacing files with the same path.
- **Delete this vault.** Typing its name, after a sign-in within the last ten minutes, destroys the vault's key, files, index, keys and connections. The account's other vaults stay as they are.

## Connections

Every connection reaches one vault, the one it was made for, and asks for read or read and write.

| Client | How it connects |
| --- | --- |
| claude.ai, Claude Desktop, mobile | A custom connector with the vault's address, signed in through the browser. See [Connect claude.ai](../how-to/connect-claude-ai.md). |
| Claude Code, Codex, kebbie, the hooks and the CLI | [`login`](../how-to/sign-in-a-machine.md) once per machine and vault, then `VAULT_SERVER_URL`. |
| Any other OAuth-capable MCP client | The vault's address as a remote server, signed in through the browser. |
| A client that only takes a header | A key made on the vault's page, sent as `Authorization: Bearer nvm_...`. See [Other clients](../how-to/other-clients.md#clients-that-only-take-a-header). |
| Obsidian's Remotely Save | WebDAV at `VAULT_PUBLIC_URL/dav/` with a key from the vault's page. See [Obsidian](../how-to/obsidian.md). |

## The Account page

The Account page, `VAULT_PUBLIC_URL/account/settings`, shows who the person is, their [ways in](../how-to/sign-in-methods.md#several-ways-into-one-account), and:

- **Support access.** The person can let the server's staff open one vault for a limited time; see [Support access](../how-to/support-access-and-import.md).
- **Deletion.** The account deletes itself after a sign-in within the last ten minutes and its email typed out: every vault's key goes first, then their files, indexes, keys, connections and the ways in. It is refused while the account owns a shared vault that has other members.

`hosted.sqlite` under `VAULT_AUTH_DIR` holds the accounts, the ways in, the hashed keys and tokens, the registered clients and every vault's sealed key.
