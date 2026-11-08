# Configuration

Every setting is an environment variable. Settings read through the server's settings reader are also read from `CLAUDE_PLUGIN_OPTION_<NAME>`, which is how the Claude Code plugin passes them; the bare name wins when both are set. The [hook switches](#hooks) and the [terminal settings](#terminal-and-claude-code) are read by their bare name only.

**Required** says when a setting must be present:

- **yes**: the server or command refuses to start without it.
- **one of**: at least one setting of the group must be present.
- **with X**: needed once X is set; the server refuses to start with X and without it.

The four settings `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY` and `S3_SECRET_KEY` count as one: the bucket is used only when all four are present.

An empty value counts as not set.

## Storage

[Storage](../install/storage.md) explains the choices.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `VAULT_PATH` | one of | | Directory holding the vault. On a server, the folder holding every vault. Wins over `S3_*`. |
| `S3_ENDPOINT` | one of | | Endpoint URL of the S3-compatible store, e.g. `https://s3.example.com`. |
| `S3_BUCKET` | one of | | Bucket holding the vault. It must exist. |
| `S3_ACCESS_KEY` | one of | | Access key. |
| `S3_SECRET_KEY` | one of | | Secret key. |
| `S3_PREFIX` | no | empty | Key prefix inside the bucket. |
| `S3_REGION` | no | `us-east-1` | Region the requests are signed for. |
| `VAULT_SERVER_URL` | one of | | On a client machine: the vault server the machine [signed in to](../how-to/sign-in-a-machine.md). The stdio server, the hooks and the commands work through it. Wins over `VAULT_PATH` and `S3_*`. |
| `VAULT_CACHE_DIR` | no | `~/.cache/mindin`; `/data/cache` in the image | The search indexes, the model for search by meaning, and the model process's socket and log. |
| `VAULT_SCHEMA` | no | | On a client machine: a local schema file that overrides the vault's `.vault/schema.yml`. |

When the environment names none of `VAULT_PATH`, the four `S3_*` settings or `VAULT_SERVER_URL`, every command, the stdio server included, takes them from the vault [`setup`](command-line.md#setting-up-a-machine) saved, then from the Claude Code plugin's saved config; see [Where commands find the vault](command-line.md#where-commands-find-the-vault). With none of them, the stdio server and the commands exit with one line saying so, and `status` answers `"configured": false`. An HTTP server starts without storage settings but cannot open any vault.

## Server

These apply to `serve --transport http`. `--host` defaults to `127.0.0.1` and `--port` to `8765`.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `VAULT_PUBLIC_URL` | yes | | The address clients reach, path included when the server is not at the root. Must start with `https://`, or `http://localhost` for a trial. The Docker example sets it to `https://VAULT_HOST`. |
| `VAULT_HOSTED_MASTER_KEY` | yes | | Base64 of 32 bytes that wraps every vault's data key; `master-key generate` makes one. The server also refuses to start when it does not open the vault keys it holds. See [Encryption](../explanation/encryption.md). |
| `VAULT_HOSTED_MASTER_KEY_PREVIOUS` | no | | Older master keys, space or comma separated, still accepted while `master-key rewrap` moves the vault keys off them. |
| `VAULT_REGISTRATION` | no | `invite` | `invite`: only invited addresses and addresses at an organisation's verified domain get an account. `open`: any verified email does. |
| `VAULT_PASSKEYS` | no | `on` | `on` or `off`. |
| `VAULT_HTTP_STATELESS` | no | `off` | `on` keeps no MCP session on the server between requests, so a restart drops no session a client holds. Each request then stands alone: the server sends nothing to a client outside the answer to a request. |
| `VAULT_INDEX_SNAPSHOTS` | no | `off` | `on` keeps a sealed copy of each vault's search index in the folder or bucket, under `<prefix>/indexes/<vault id>/`, written when the vault goes idle, when the server shuts down, and at most once per `VAULT_INDEX_SNAPSHOT_MINUTES` while new notes are embedded, and only when the index changed since the last copy. A server whose `VAULT_CACHE_DIR` lacks a vault's index starts from that copy and indexes or embeds only the notes that changed since. Deleting the vault deletes the copy. |
| `VAULT_INDEX_SNAPSHOT_MINUTES` | no | `10` | The least time between two copies of one vault's index while it is in use. |
| `VAULT_ALLOWED_HOSTS` | no | | Extra `Host` header values the MCP endpoint answers, space or comma separated. See [Behind a reverse proxy](../how-to/reverse-proxy.md). |
| `VAULT_FORWARDED_ALLOW_IPS` | no | `127.0.0.1,::1`; `*` in the Docker example | The addresses of your reverse proxies, as IP addresses or CIDR ranges, comma separated. The server takes the client's address from `X-Forwarded-For` only on requests from these, and counts its rate limits per client address. `*` trusts every peer. See [The client's address](../how-to/reverse-proxy.md#the-clients-address). |
| `VAULT_AUTH_DIR` | no | `VAULT_CACHE_DIR/auth`; `/data/auth` in the image | Where `hosted.sqlite` lives: the accounts, vaults, keys, tokens and rate-limit counts. |
| `VAULT_SUBJECT_PREFIX` | no | `users` | Where the vaults live in the folder or bucket: `<prefix>/<vault id>/`. See [What is stored](../install/storage.md#what-is-stored). |

The server loads the extensions installed beside it, from the Python entry point group `mindin.extensions`.

## Sign-in

At least one of `VAULT_SMTP_HOST`, `VAULT_GITHUB_CLIENT_ID`, `VAULT_OIDC_ISSUER` and `VAULT_IDENTITY_PUBLIC_KEY` must be set; passkeys are added after a first sign-in. [Set up sign-in methods](../how-to/sign-in-methods.md) walks through each.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `VAULT_SMTP_HOST` | one of | | Turns on email links. |
| `VAULT_SMTP_FROM` | with `VAULT_SMTP_HOST` | | The sender, e.g. `Vault <vault@example.com>`. |
| `VAULT_SMTP_USERNAME` | no | | The SMTP login. |
| `VAULT_SMTP_PASSWORD` | no | | The SMTP password. |
| `VAULT_SMTP_SECURITY` | no | `starttls` | `starttls`, `ssl` or `none`. `none` is refused with a username or password. |
| `VAULT_SMTP_PORT` | no | 587, 465 or 25 by `VAULT_SMTP_SECURITY` | The SMTP port. |
| `VAULT_GITHUB_CLIENT_ID` | one of | | Turns on GitHub sign-in. |
| `VAULT_GITHUB_CLIENT_SECRET` | with `VAULT_GITHUB_CLIENT_ID` | | The OAuth app's secret. |
| `VAULT_OIDC_ISSUER` | one of | | Turns on your OpenID Connect provider. |
| `VAULT_OIDC_CLIENT_ID` | with `VAULT_OIDC_ISSUER` | | The client's ID. |
| `VAULT_OIDC_CLIENT_SECRET` | with `VAULT_OIDC_ISSUER` | | The client's secret. |
| `VAULT_OIDC_NAME` | no | `your organisation` | The provider's button label. |
| `VAULT_OIDC_SCOPES` | no | `openid email profile` | Scopes asked of the provider, space separated. |
| `VAULT_IDENTITY_PUBLIC_KEY` | one of | | Turns on a trusted proxy: its PEM public key (RS256 or ES256), raw or base64 on one line. |
| `VAULT_IDENTITY_ISSUER` | with `VAULT_IDENTITY_PUBLIC_KEY` | | The `iss` in the proxy's tokens. |
| `VAULT_IDENTITY_HEADER` | no | `X-Forwarded-Identity` | The header carrying the token. |
| `VAULT_IDENTITY_TYP` | no | | When set, the token's `typ` must match. |
| `VAULT_IDENTITY_NAME` | no | `your company's sign-in` | The proxy's button label. |
| `VAULT_IDENTITY_AUDIENCE` | no | | More hosts the token's `aud` may name besides this server's own, space or comma separated: the proxy's host when clients connect through it. |
| `VAULT_IDENTITY_WRITE_CLAIMS` | no | `vault:write` | Entitlements that grant read and write, space separated. |
| `VAULT_IDENTITY_READ_CLAIMS` | no | `vault:read` | Entitlements that grant read only. A token with neither is refused. |

### Organisations

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `VAULT_PUBLIC_EMAIL_DOMAINS` | no | | More public email domains, comma separated, that no organisation can add, besides the built-in ones such as `gmail.com`, `outlook.com`, `icloud.com` and `proton.me`. A listed domain also covers its subdomains. `mindin org domain add --allow-public` still adds one. |

A domain an organisation adds is verified by a TXT record at `_mindin-verify.<domain>`, looked up through the server's own DNS resolver with a five-second timeout, so the server needs to reach a resolver that answers for public domains.

## Import

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `IMPORT_S3_ACCESS_KEY` | no | `S3_ACCESS_KEY` | The access key `accounts import --from-s3-bucket` reads the old bucket with. |
| `IMPORT_S3_SECRET_KEY` | no | `S3_SECRET_KEY` | Its secret key. |

## Search by meaning

[Search by meaning](../install/semantic-search.md) explains these. They need the `semantic` extra.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `VAULT_SEMANTIC` | no | off | `on`, `1`, `true` or `yes` adds search by meaning. |
| `VAULT_SEMANTIC_MODEL` | no | `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | The fastembed model. |
| `VAULT_EMBED_URL` | no | | `tcp://host:port` of an `embed-worker` that this server sends its sections and queries to, instead of starting its own model process. The server then needs `numpy` but not `fastembed`. See [The model in its own process](../install/semantic-search.md#the-model-in-its-own-process). |
| `VAULT_EMBED_TOKEN` | with `embed-worker --listen` on an address other than loopback | | The secret an `embed-worker` asks of every request. Set the same value on the worker and on the server whose `VAULT_EMBED_URL` points at it. |
| `VAULT_EMBED_BATCH` | no | `16` | Sections the model process runs through the model at once. Fewer lowers its peak memory during a first sync. |
| `VAULT_EMBED_THREADS` | no | half the usable cores, at most `4` | Threads the model runs on. The usable cores count a container's CPU limit (cgroup v2 `cpu.max`); ONNX Runtime on its own takes every physical core of the node. |
| `VAULT_EMBED_MAX_TOKENS` | no | `256` | Tokens of a section the model reads. Prose sections stay under it; a section of links or code is cut there instead of padding its batch to 512. |

## Hooks

Read by their bare name only. [Hooks](hooks.md) describes the checks.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `VAULT_STOP_HOOK` | no | on | `off` silences the stop hook, for sessions started by a tool that writes the log itself. |
| `VAULT_STOP_HOOK_COMMITS` | no | on | `off` turns off the stop hook's checks for unlogged commits and stale notes, and keeps the message check, for sessions whose log another tool writes. |
| `VAULT_STOP_HOOK_PROMPTS` | no | `3` | How many messages you write after the agent's last vault write before the stop hook asks it to save them; `off` turns that check off. |

## Terminal and Claude Code

Read by their bare name only.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `NO_COLOR` | no | | Any value keeps the terminal layout without colour. `TERM=dumb` does the same. |
| `XDG_CONFIG_HOME` | no | `~/.config` | The folder holding `mindin/config.toml`, the vault `setup` saved, and `mindin/credentials.json`, the keys from `login` and `setup`. A `notes-vault-mcp` folder there, and one in `~/.cache`, is moved to `mindin` the first time mindin runs, keeping its old name as a link. |
| `XDG_STATE_HOME` | no | `~/.local/state` | The folder holding `mindin/install-id`, the [install signal](#install-signal)'s id. Unset, and with `VAULT_CACHE_DIR` set, the id is in `state` beside that folder instead. |
| `CLAUDE_CONFIG_DIR` | no | `~/.claude` | Where the commands look for the Claude Code plugin's saved settings. |

## Install signal

mindin sends nothing until you say yes; `mindin setup` asks once in a terminal, and an empty answer is no. After a yes, the running server sends a random install id, mindin's version, the OS and CPU type and how mindin runs (plugin, CLI or server) to signals.mindin.dev once a day, never notes, names, paths or your vault's address. `mindin setup --signals yes|no` changes the answer, and `VAULT_SIGNALS` wins over it.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `VAULT_SIGNALS` | no | | `on` or `off`, winning over the answer `setup` saved as `[signals] install` in `config.toml`. Off deletes the install id. Without either, nothing is sent. |

## Docker Compose

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `VAULT_HOST` | yes | | Read by `compose.yml`, not by the server: the domain Caddy fetches a certificate for, and the host of `VAULT_PUBLIC_URL`. A name under `localhost`, such as `vault.localhost`, runs a trial on one machine with Caddy's own certificate. |
