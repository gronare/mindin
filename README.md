<!-- mcp-name: io.github.gronare/notes-vault-mcp -->

# notes-vault-mcp

Memory for Claude and coding agents, kept as markdown notes you own, with a schema that holds and a tie to your git history.

## Install

A vault server with Docker Compose, for claude.ai, Claude Desktop, mobile and every machine you work on. You need a domain pointing at the machine, ports 80 and 443 open, and an SMTP account for sign-in links.

```sh
mkdir notes-vault && cd notes-vault
curl -fsSL -O https://github.com/gronare/notes-vault-mcp/releases/latest/download/compose.yml -O https://github.com/gronare/notes-vault-mcp/releases/latest/download/Caddyfile
curl -fsSL -o .env https://github.com/gronare/notes-vault-mcp/releases/latest/download/example.env
docker compose run --rm --no-deps vault master-key generate   # paste into .env, with VAULT_HOST and SMTP
docker compose up -d
docker compose exec vault notes-vault-mcp accounts invite you@example.com
```

Then open `https://VAULT_HOST/account` and sign in. [Install with Docker](https://github.com/gronare/notes-vault-mcp/blob/main/docs/install/docker.md) walks through each step.

Only Claude Code on one machine, with a local folder:

```sh
claude mcp add vault -s user -e VAULT_PATH=$HOME/vault -- uvx notes-vault-mcp
```

## Use

Add the server's address, `https://VAULT_HOST/mcp`, as a custom connector in claude.ai. Claude searches, reads and writes your notes. The same search from a shell on a [signed-in machine](https://github.com/gronare/notes-vault-mcp/blob/main/docs/how-to/sign-in-a-machine.md):

```console
$ notes-vault-mcp search "signing in"
1 note · archive and superseded hidden

myapp  system  3 days
Areas/myapp.md
  ## Sign-in Users sign in with passkeys; email links are the fallback.
```

## What you get

- **Notes you own.** Plain markdown in a folder or an S3 bucket; open them in Obsidian, keep them in git.
- **A schema with teeth.** Required fields, legal statuses and kinds, a tag vocabulary; a write that breaks the contract is refused. Start from PARA, a simple vault, a journal or your own layout.
- **A lifecycle.** `close` archives finished work, `backlog_add` files what was deferred, `log_append` writes one line per repo per session, `lint` reports drift.
- **Memory tied to code.** A note names the paths it describes; the session-start hook prints the commits newer than the note.
- **Search that reads your languages.** Stemming, folded diacritics, synonyms, and optional search by meaning that runs on your machine.
- **Your own vault server.** Accounts with passkeys, email links, GitHub or your identity provider, several vaults each, shared vaults, sealed in storage.

## Documentation

[docs/](https://github.com/gronare/notes-vault-mcp/blob/main/docs/README.md) has the install guides, the how-tos for each client, the reference for every tool, command and setting, and how it works.

## License

MIT. See [LICENSE](LICENSE).
