<!-- mcp-name: io.github.gronare/mindin -->

# mindin

Memory for Claude and coding agents, kept as markdown notes you own, with a schema that holds and a tie to your git history.

## Install

```sh
curl -fsSL https://mindin.dev/install.sh | sh
```

It installs the `mindin` command with [uv](https://docs.astral.sh/uv/), asks where your vault lives (a folder, an S3 bucket or a vault server), and offers to add the vault to Claude Code, Codex and Cursor; it changes nothing in them without a yes. mindin was called notes-vault-mcp: the `notes-vault-mcp` package and command still work, and install and run mindin.

### A vault server

A vault server with Docker Compose, for claude.ai, Claude Desktop, mobile and every machine you work on. You need a domain pointing at the machine, ports 80 and 443 open, and an SMTP account for sign-in links.

```sh
mkdir notes-vault && cd notes-vault
curl -fsSL -O https://github.com/gronare/mindin/releases/latest/download/compose.yml -O https://github.com/gronare/mindin/releases/latest/download/Caddyfile
curl -fsSL -o .env https://github.com/gronare/mindin/releases/latest/download/example.env
docker compose run --rm --no-deps vault master-key generate   # paste into .env, with VAULT_HOST and SMTP
docker compose up -d
docker compose exec vault mindin accounts invite you@example.com
```

Then open `https://VAULT_HOST/account` and sign in. [Install with Docker](https://github.com/gronare/mindin/blob/main/docs/install/docker.md) walks through each step.

Without the script, on one machine: `uv tool install mindin`, then `mindin setup`.

mindin asks before it shares anonymous usage data, and sends nothing until you say yes: then, once a day, a random install id, mindin's version, the OS and CPU type and how mindin runs (plugin, CLI or server) go to signals.mindin.dev, never notes, names, paths or your vault's address. `mindin setup --signals yes|no` changes the answer. [The install signal](https://github.com/gronare/mindin/blob/main/docs/reference/install-signal.md) has the details.

## Use

Add the server's address, `https://VAULT_HOST/mcp`, as a custom connector in claude.ai. Claude searches, reads and writes your notes. The same search from a shell on a [signed-in machine](https://github.com/gronare/mindin/blob/main/docs/how-to/sign-in-a-machine.md):

```console
$ mindin search "signing in"
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
- **A wiki for your team.** Everyone at your company's email domain joins an organisation and its wiki; their Claude searches it beside their own vault, and a guard refuses secrets.
- **A vault that moves.** `mindin move` copies a vault between a folder, a bucket and a server, and checks every file.

## Documentation

[docs/](https://github.com/gronare/mindin/blob/main/docs/README.md) has the install guides, the how-tos for each client, the reference for every tool, command and setting, and how it works.

## License

MIT. See [LICENSE](LICENSE).
