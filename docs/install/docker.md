# Install with Docker

This runs a vault server with Docker Compose: the server and a Caddy container in front of it that fetches the HTTPS certificate. At the end you have an account, a vault, and an address to give claude.ai.

You need:

- Docker with Compose v2, on an amd64 or arm64 machine. The images are built for both.
- A domain name, such as `vault.example.com`, pointing at this machine, with ports 80 and 443 open to the internet. Caddy needs both to get the certificate. To try the server on one machine, use a name under `localhost`, such as `vault.localhost`: Caddy then signs its own certificate, which the browser warns about.
- An SMTP account for sign-in links. GitHub or your own identity provider work instead; see [Sign-in methods](../how-to/sign-in-methods.md).

## With the script

On the server:

```sh
curl -fsSL https://github.com/gronare/mindin/releases/latest/download/server.sh | sh
```

It makes a `mindin` folder, or uses the folder you are in when it holds a `compose.yml`, and downloads `compose.yml`, `Caddyfile` and `example.env` from the same release. Then it asks for:

- The domain, which becomes `VAULT_HOST`. A single word is refused, `localhost` too; `vault.localhost` is the name for a trial on this machine.
- The SMTP host, port, sender, username and password. For a port other than 587 or 465 it also asks for the security, `starttls`, `ssl` or `none`, with `starttls` as the default; `none` suits a relay without TLS, such as port 25 on a private network, and takes no login. Leave the host empty to skip email: it then asks for a GitHub OAuth app's client ID and secret, and people sign in with GitHub and add a passkey afterwards.
- Your email address, which it invites as the first account.

It writes the answers to `.env`, readable only by you, makes the master key with the image's `master-key generate` and puts it in `.env`, starts the server, waits until `/ready` answers and invites you. It ends with the address to open, `https://vault.example.com/account`; go on at [Sign in](#sign-in).

The master key is the `VAULT_HOSTED_MASTER_KEY` line in `.env`. Keep a copy somewhere other than this machine: without it no vault can be read. See [Encryption](../explanation/encryption.md).

Running it again keeps `.env` and its master key as they are, fetches the release's `compose.yml` and restarts the server, so it also upgrades. It takes any answer from the environment and asks only for the rest, so it also runs without a terminal:

```sh
curl -fsSL https://github.com/gronare/mindin/releases/latest/download/server.sh | \
  VAULT_HOST=vault.example.com FIRST_EMAIL=you@example.com \
  VAULT_SMTP_HOST=smtp.example.com VAULT_SMTP_FROM='Vault <vault@example.com>' \
  VAULT_SMTP_USERNAME=vault VAULT_SMTP_PASSWORD=secret sh
```

`VAULT_SMTP_PORT`, `VAULT_SMTP_SECURITY`, `VAULT_GITHUB_CLIENT_ID` and `VAULT_GITHUB_CLIENT_SECRET` are read the same way, and `MINDIN_RELEASE` set to a tag, such as `v0.15.2`, fetches that release's files instead of the latest.

## By hand

The same steps, one at a time.

### 1. Fetch the files

```sh
mkdir mindin && cd mindin
curl -fsSL -O https://github.com/gronare/mindin/releases/latest/download/compose.yml -O https://github.com/gronare/mindin/releases/latest/download/Caddyfile
curl -fsSL -o .env https://github.com/gronare/mindin/releases/latest/download/example.env
```

`compose.yml` runs the server image and Caddy. `Caddyfile` sends every request for `VAULT_HOST` to the server on port 8765. `.env` holds the settings, with the ones you must edit at the top.

Compose names the volumes after the folder, such as `mindin_vault-data`. A server set up in another folder, such as `notes-vault`, keeps its folder: renamed, Compose would start on new, empty volumes. Run the script or the upgrade inside it.

### 2. Make the master key

```sh
docker compose run --rm --no-deps vault master-key generate
```

It prints one line of base64. Paste it after `VAULT_HOSTED_MASTER_KEY=` in `.env`. This key seals every vault: keep a copy somewhere other than this machine. See [Encryption](../explanation/encryption.md).

### 3. Edit `.env`

Set the values under `Required`:

- `VAULT_HOST`: your domain, without `https://`. The server's address becomes `https://VAULT_HOST`.
- `VAULT_HOSTED_MASTER_KEY`: the key from step 2.
- The `VAULT_SMTP_*` lines: your mail server, the sender address and the login. To sign in another way, comment these out and fill in one of the groups further down.

The vaults are kept in the container's `/data` volume. To keep them in an S3 bucket instead, see [Storage](storage.md). Every other line is optional and described in [Configuration](../reference/configuration.md).

### 4. Start it

```sh
docker compose up -d
```

The server refuses to start without the master key, without a sign-in method, or with a master key that does not open the vault keys it already holds. `docker compose logs vault` shows why.

### 5. Invite yourself

```sh
docker compose exec vault mindin accounts invite you@example.com
```

New accounts are by invitation; see [Manage accounts](../how-to/manage-accounts.md).

## Sign in

Open `https://vault.example.com/account`, enter the invited address and follow the link in the email. Your account starts with one vault, Personal, with the slug `notes`. Add a passkey on the Account page (`/account/settings`) to sign in without email next time.

Open the vault from the Vaults page: its page shows its MCP address, `https://vault.example.com/mcp?vault=notes`. Next: [connect claude.ai](../how-to/connect-claude-ai.md), or [sign a machine in](../how-to/sign-in-a-machine.md) for Claude Code and the CLI.

## What lives where

The `vault-data` volume is mounted at `/data`:

| Path | Holds |
| --- | --- |
| `/data/auth/hosted.sqlite` | Accounts, ways in, hashed keys and tokens, and every vault's sealed key. |
| `/data/vaults` | The sealed vaults, when `VAULT_PATH=/data/vaults`. |
| `/data/cache` | One search index per vault, and the model for search by meaning. |

Back up `/data/auth` together with the master key; see [Back up and rotate keys](../how-to/back-up-and-rotate.md).

## Upgrade

Each release's `compose.yml` names that release's image. Run the script again in the server's folder, or fetch it and restart:

```sh
curl -fsSL -O https://github.com/gronare/mindin/releases/latest/download/compose.yml
docker compose up -d
```

This overwrites `compose.yml`, so keep your own changes in a `compose.override.yml` beside it, which Compose reads on top. [Search by meaning](semantic-search.md) is set up that way.

## Another reverse proxy

Caddy is there so HTTPS works without more setup. To use a proxy you already run, see [Behind a reverse proxy](../how-to/reverse-proxy.md), and set `VAULT_FORWARDED_ALLOW_IPS` in `.env` to that proxy's address: `compose.yml` trusts every peer, which is only safe while nothing but the proxy can reach the server's port.
