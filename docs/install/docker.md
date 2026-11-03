# Install with Docker

This runs a vault server with Docker Compose: the server and a Caddy container in front of it that fetches the HTTPS certificate. At the end you have an account, a vault, and an address to give claude.ai.

You need:

- Docker with Compose v2.
- A domain name, such as `vault.example.com`, pointing at this machine, with ports 80 and 443 open to the internet. Caddy needs both to get the certificate.
- An SMTP account for sign-in links. GitHub or your own identity provider work instead; see [Sign-in methods](../how-to/sign-in-methods.md).

## 1. Fetch the files

```sh
mkdir notes-vault && cd notes-vault
curl -fsSL -O https://github.com/gronare/notes-vault-mcp/releases/latest/download/compose.yml -O https://github.com/gronare/notes-vault-mcp/releases/latest/download/Caddyfile
curl -fsSL -o .env https://github.com/gronare/notes-vault-mcp/releases/latest/download/example.env
```

`compose.yml` runs the server image and Caddy. `Caddyfile` sends every request for `VAULT_HOST` to the server on port 8765. `.env` holds the settings, with the ones you must edit at the top.

## 2. Make the master key

```sh
docker compose run --rm --no-deps vault master-key generate
```

It prints one line of base64. Paste it after `VAULT_HOSTED_MASTER_KEY=` in `.env`. This key seals every vault: keep a copy somewhere other than this machine. See [Encryption](../explanation/encryption.md).

## 3. Edit `.env`

Set the values under `Required`:

- `VAULT_HOST`: your domain, without `https://`. The server's address becomes `https://VAULT_HOST`.
- `VAULT_HOSTED_MASTER_KEY`: the key from step 2.
- The `VAULT_SMTP_*` lines: your mail server, the sender address and the login. To sign in another way, comment these out and fill in one of the groups further down.

The vaults are kept in the container's `/data` volume. To keep them in an S3 bucket instead, see [Storage](storage.md). Every other line is optional and described in [Configuration](../reference/configuration.md).

## 4. Start it

```sh
docker compose up -d
```

The server refuses to start without the master key, without a sign-in method, or with a master key that does not open the vault keys it already holds. `docker compose logs vault` shows why.

## 5. Invite yourself

```sh
docker compose exec vault notes-vault-mcp accounts invite you@example.com
```

New accounts are by invitation; see [Manage accounts](../how-to/manage-accounts.md).

## 6. Sign in

Open `https://vault.example.com/account`, enter the invited address and follow the link in the email. Your account starts with one vault. Add a passkey on the account page to sign in without email next time.

The vault's page shows its MCP address, `https://vault.example.com/v/<slug>/mcp`. Next: [connect claude.ai](../how-to/connect-claude-ai.md), or [sign a machine in](../how-to/sign-in-a-machine.md) for Claude Code and the CLI.

## What lives where

The `vault-data` volume is mounted at `/data`:

| Path | Holds |
| --- | --- |
| `/data/auth/hosted.sqlite` | Accounts, ways in, hashed keys and tokens, and every vault's sealed key. |
| `/data/vaults` | The sealed vaults, when `VAULT_PATH=/data/vaults`. |
| `/data/cache` | One search index per vault, and the model for search by meaning. |

Back up `/data/auth` together with the master key; see [Back up and rotate keys](../how-to/back-up-and-rotate.md).

## Upgrade

Each release's `compose.yml` names that release's image. Fetch it again and restart:

```sh
curl -fsSL -O https://github.com/gronare/notes-vault-mcp/releases/latest/download/compose.yml
docker compose up -d
```

This overwrites `compose.yml`, so keep your own changes in a `compose.override.yml` beside it, which Compose reads on top. [Search by meaning](semantic-search.md) is set up that way.

## Another reverse proxy

Caddy is there so HTTPS works without more setup. To use a proxy you already run, see [Behind a reverse proxy](../how-to/reverse-proxy.md), and set `VAULT_FORWARDED_ALLOW_IPS` in `.env` to that proxy's address: `compose.yml` trusts every peer, which is only safe while nothing but the proxy can reach the server's port.
