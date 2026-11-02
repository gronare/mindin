# notes-vault-mcp documentation

Start with [Install with Docker](install/docker.md): it takes you from an empty machine to a vault server you can connect claude.ai to.

## Tutorial

- [Install with Docker](install/docker.md): a vault server with Docker Compose and Caddy, your account, your first vault.

## How-to guides

Installing:

- [Storage](install/storage.md): a local folder or an S3 bucket, and what the bucket's key needs.
- [Search by meaning](install/semantic-search.md): the `-semantic` image, the model from Hugging Face, memory.
- [Install on Kubernetes](install/kubernetes.md): the manifests in `deploy/k8s/`.
- [Run a server without Docker](install/without-docker.md): the Python package behind your own TLS.
- [Behind a reverse proxy](how-to/reverse-proxy.md): another proxy in place of Caddy, and the `Host` header.

Connecting:

- [Connect claude.ai, Claude Desktop and mobile](how-to/connect-claude-ai.md)
- [Sign a machine in to a server](how-to/sign-in-a-machine.md): `login`, and several vaults on one machine.
- [Use it with Claude Code](how-to/claude-code.md): the plugin, or the server and hooks by hand.
- [Use it with Codex, Cursor and other clients](how-to/other-clients.md): stdio clients, URL clients, keys, kebbie.
- [Open a server vault in Obsidian](how-to/obsidian.md)

Running a server:

- [Set up sign-in methods](how-to/sign-in-methods.md): email links, GitHub, your identity provider, a trusted proxy, passkeys.
- [Manage accounts and vaults](how-to/manage-accounts.md): invitations, suspension, more vaults, sharing.
- [Back up and rotate keys](how-to/back-up-and-rotate.md)
- [Open a vault for support, or import one](how-to/support-access-and-import.md)

Keeping a vault tidy:

- [Run lint every week](how-to/weekly-lint.md)

## Reference

- [Configuration](reference/configuration.md): every environment variable.
- [MCP tools](reference/tools.md)
- [Command line](reference/command-line.md)
- [Agent commands](reference/agent-commands.md): `task`, `log`, `snapshot` and `status`, JSON out.
- [Hooks](reference/hooks.md): `session-start` and `stop`.
- [The schema file](reference/schema.md): `.vault/schema.yml`.
- [Device flow](reference/device-flow.md): signing a tool in without a browser.

## Explanation

- [How it works](explanation/how-it-works.md): the index, session start, writes.
- [Notes and layouts](explanation/notes-and-layouts.md): folders, kinds, presets.
- [Lifecycle, backlog and the repo log](explanation/lifecycle.md)
- [Search](explanation/search.md): ranking by words and by meaning.
- [Accounts and vaults on a server](explanation/accounts-and-vaults.md)
- [Shared vaults](explanation/shared-vaults.md)
- [Encryption on a server](explanation/encryption.md)
