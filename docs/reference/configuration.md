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
| `VAULT_CACHE_DIR` | no | `~/.cache/notes-vault-mcp`; `/data/cache` in the image | The search indexes, the model for search by meaning, and the model process's socket and log. |
| `VAULT_SCHEMA` | no | | On a client machine: a local schema file that overrides the vault's `.vault/schema.yml`. |

When the environment names none of `VAULT_PATH`, the four `S3_*` settings or `VAULT_SERVER_URL`, every command, the stdio server included, takes them from the Claude Code plugin's saved config; see [Command line](command-line.md#where-commands-find-the-vault). With neither, the stdio server and the commands exit with one line saying so, and `status` answers `"configured": false`. An HTTP server starts without storage settings but cannot open any vault.

## Server

These apply to `serve --transport http`. `--host` defaults to `127.0.0.1` and `--port` to `8765`.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `VAULT_PUBLIC_URL` | yes | | The address clients reach, path included when the server is not at the root. Must start with `https://`, or `http://localhost` for a trial. The Docker example sets it to `https://VAULT_HOST`. |
| `VAULT_HOSTED_MASTER_KEY` | yes | | Base64 of 32 bytes that wraps every vault's data key; `master-key generate` makes one. The server also refuses to start when it does not open the vault keys it holds. See [Encryption](../explanation/encryption.md). |
| `VAULT_HOSTED_MASTER_KEY_PREVIOUS` | no | | Older master keys, space or comma separated, still accepted while `master-key rewrap` moves the vault keys off them. |
| `VAULT_REGISTRATION` | no | `invite` | `invite`: only invited addresses get an account. `open`: any verified email does. |
| `VAULT_PASSKEYS` | no | `on` | `on` or `off`. |
| `VAULT_ALLOWED_HOSTS` | no | | Extra `Host` header values the MCP endpoint answers, space or comma separated. See [Behind a reverse proxy](../how-to/reverse-proxy.md). |
| `VAULT_AUTH_DIR` | no | `VAULT_CACHE_DIR/auth`; `/data/auth` in the image | Where `hosted.sqlite` lives. |
| `VAULT_SUBJECT_PREFIX` | no | `users` | Where the vaults live in the folder or bucket: `<prefix>/<vault id>/`, or `<prefix>/<account id>/` for an account's original vault. |

The server loads the extensions installed beside it, from the Python entry point group `notes_vault_mcp.extensions`.

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
| `VAULT_IDENTITY_WRITE_CLAIMS` | no | `vault:write` | Entitlements that grant read and write, space separated. |
| `VAULT_IDENTITY_READ_CLAIMS` | no | `vault:read` | Entitlements that grant read only. A token with neither is refused. |
| `VAULT_IDENTITY_NAME` | no | `your company's sign-in` | The proxy's button label. |

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
| `VAULT_EMBED_BATCH` | no | `16` | Sections the model process runs through the model at once. Fewer lowers its peak memory during a first sync. |
| `VAULT_EMBED_THREADS` | no | half the usable cores, at most `4` | Threads the model runs on. The usable cores count a container's CPU limit (cgroup v2 `cpu.max`); ONNX Runtime on its own takes every physical core of the node. |
| `VAULT_EMBED_MAX_TOKENS` | no | `256` | Tokens of a section the model reads. Prose sections stay under it; a section of links or code is cut there instead of padding its batch to 512. |

## Hooks

Read by their bare name only. [Hooks](hooks.md) describes the checks.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `VAULT_STOP_HOOK` | no | on | `off` silences the stop hook. |
| `VAULT_STOP_HOOK_COMMITS` | no | on | `off` turns off the stop hook's checks for unlogged commits and stale notes, and keeps the message check. |
| `VAULT_STOP_HOOK_PROMPTS` | no | `3` | How many messages you write after the agent's last vault write before the stop hook asks it to save them; `off` turns that check off. |

## Terminal and Claude Code

Read by their bare name only.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `NO_COLOR` | no | | Any value keeps the terminal layout without colour. `TERM=dumb` does the same. |
| `XDG_CONFIG_HOME` | no | `~/.config` | The folder holding `notes-vault-mcp/credentials.json`, the keys from `login`. |
| `CLAUDE_CONFIG_DIR` | no | `~/.claude` | Where the commands look for the Claude Code plugin's saved settings. |

## Docker Compose

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `VAULT_HOST` | yes | | Read by `compose.yml`, not by the server: the domain Caddy fetches a certificate for, and the host of `VAULT_PUBLIC_URL`. |
