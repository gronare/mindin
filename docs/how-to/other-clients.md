# Use it with Codex, Cursor and other clients

## Clients that start a stdio server

Any client that starts a stdio MCP server works. Codex:

```sh
codex mcp add vault --env VAULT_PATH=$HOME/vault -- uvx notes-vault-mcp
```

Cursor (`.cursor/mcp.json`), Claude Desktop's local config and most other clients take the same shape:

```json
{
  "mcpServers": {
    "vault": {
      "command": "uvx",
      "args": ["notes-vault-mcp"],
      "env": { "VAULT_PATH": "/home/me/vault" }
    }
  }
}
```

For a bucket, pass the `S3_*` settings in place of `VAULT_PATH` ([Storage](../install/storage.md)). For a vault server, [sign the machine in](sign-in-a-machine.md) and pass `VAULT_SERVER_URL`:

```sh
codex mcp add vault --env VAULT_SERVER_URL=https://vault.example.com -- uvx notes-vault-mcp
```

The server's instructions carry the vault schema's rules, so a client without the CLAUDE.md snippet that [`init`](claude-code.md#without-the-plugin) prints still picks kinds and statuses the same way.

The container image runs the same stdio server:

```sh
docker run --rm -i -e S3_ENDPOINT -e S3_ACCESS_KEY -e S3_SECRET_KEY -e S3_BUCKET ghcr.io/gronare/notes-vault-mcp:latest
```

## Clients that connect to a URL

An MCP client that speaks OAuth takes the server's address, `VAULT_PUBLIC_URL/mcp`, or one vault's, `VAULT_PUBLIC_URL/mcp?vault=<slug>`, as a remote server and signs in through the browser.

`VAULT_PUBLIC_URL/mcp` works for every client too: a key reaches the vault it was made for, and a client signed in through the browser asks which vault it gets.

## Clients that only take a header

1. On the vault's page, make a key, named after where it will live, with read or read and write.
2. Send `Authorization: Bearer nvm_...` to `VAULT_PUBLIC_URL/mcp` or to the vault's address.

The key reaches only its vault; another vault's address refuses it. Keys are shown once, show when they were last used, and are revoked on the vault's page.

## kebbie

[kebbie](https://kebbie.dev) uses the vault for each task's note, through the [task and log commands](../reference/agent-commands.md). For a vault server, [sign the machine in](sign-in-a-machine.md) once; the commands read `VAULT_SERVER_URL` from the environment or from the Claude Code plugin's settings.
