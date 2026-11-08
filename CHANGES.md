# Changes

What each mindin release brings, one line per change, as you would tell someone who uses it. scripts/release.sh moves the Unreleased entries under the new version, and the GitHub release shows that version's entries.

## Unreleased

### What's new
- An organisation lets everyone at its verified email domain in on their first sign-in.
- An organisation's vault is a wiki with Areas, Teams, Guides, Decisions, Projects and Incidents.
- An admin can lock an organisation's vault so only its editors write.
- An organisation decides whether members may share their own vaults with people outside it.
- `search` covers a member's own vault and the organisation's vaults, each row labelled with its vault.
- `read_file`, `list_files`, `write_file` and `append_file` take `vault` to reach another vault.
- Session start lists up to five organisation pages about the repo.
- The `rules` tool gives every member's Claude the rules a vault's schema carries.
- A vault can refuse writes that hold a secret, a card number or a personnummer.
- Only the owner or an organisation admin changes a shared vault's schema, and without one it takes no writes.
- A trusted proxy's identity token can name the proxy's own host, listed in `VAULT_IDENTITY_AUDIENCE`.
- `mindin accounts import` leaves `.git/` and Mac leftovers behind, and `--dry-run` lists what it would do.
- Extensions can add background work, storage limits and an email-link check.
- Extensions can decide on every new account.

### Fixed

## 0.15.2

### What's new

### Fixed
- Deleting a note on an S3 server that ignores the etag on deletes is refused when the note changed meanwhile.

## 0.15.1

### What's new

### Fixed
- Removing two ways in at once on a hosted server always leaves the account one way in.
- A write with a local vault's etag is refused whenever the note changed, also when a same-size edit lands within the same instant.
- `search` and `mindin search` without a limit return as many notes as the schema's `search.default_limit`.
- `mindin read` in a terminal shows the title once when the note opens with the same `# heading`.

## 0.15.0

### What's new
- `mindin setup` asks whether to share anonymous usage data, and nothing is sent until you say yes.

### Fixed

## 0.14.3

### What's new
- Sign-in and invitation emails come as branded HTML with one button, in light and dark, and a plain-text part.

### Fixed

## 0.14.2

### What's new

### Fixed
- `mindin` without a vault starts the server and answers each tool call with how to run `mindin setup`.

## 0.14.1

### What's new
- The install script answers at `curl -fsSL https://mindin.dev/install.sh | sh`, and mindin.dev is the package's homepage.
- The Claude Code plugin runs `mindin` itself instead of the `notes-vault-mcp` package, like a fresh install.

### Fixed

## 0.14.0

### What's new
- Renamed from notes-vault-mcp to mindin: the package, the command and the image are `mindin`. The `notes-vault-mcp` package and command keep working and run mindin, the image is published under both names, and `~/.config/notes-vault-mcp` and `~/.cache/notes-vault-mcp` move to `mindin` on the first run, so no machine is signed out or indexed again. Extensions register in the `mindin.extensions` entry point group and import from `mindin.extension_api`.
- `mindin setup` asks where the vault lives, a folder, an S3 bucket or a vault server, and saves it for every command and client on the machine; `--folder`, `--s3` and `--server` do it from a script. The environment still wins, and the Claude Code plugin's settings are read when mindin has none. `status` names which one decided in `source`.
- `curl -fsSL https://github.com/gronare/mindin/releases/latest/download/install.sh | sh` installs mindin with uv, runs `setup` and offers to add the vault to Claude Code, Codex and Cursor.
- `mindin about --json` tells a tool the name, version, contract and the commands this install has.

### Fixed

## 0.13.0

### What's new
- The documentation lives in `docs/`, sorted into install guides, how-tos, reference and explanation, with a page listing every environment variable.
- `deploy/docker/` holds the Docker Compose setup, and each GitHub release carries its `compose.yml`, `Caddyfile` and `example.env`, so a server installs with two `curl` lines. Its `.env` keeps the vaults in the container's volume unless you set a bucket.
- `VAULT_HTTP_STATELESS=on` serves MCP without sessions, so a server restart drops no session a client holds.
- `notes-vault-mcp embed-worker --listen host:port` runs the model for search by meaning in a process of its own that the server reaches through `VAULT_EMBED_URL`, with `VAULT_EMBED_TOKEN` as its secret, so the server restarts fast and stays small.
- `GET /ready` answers `200` when the server is ready for requests and `503` with the names of the failing checks, for a load balancer or a readiness probe.
- `VAULT_INDEX_SNAPSHOTS=on` keeps a sealed copy of each vault's search index in storage, so a restarted or moved server restores a vault's index instead of indexing and embedding every note again. A busy vault's copy is written at most once per `VAULT_INDEX_SNAPSHOT_MINUTES` (default 10) and only when the index changed, plus once when it goes idle and once when the server stops.
- `VAULT_FORWARDED_ALLOW_IPS` names the reverse proxies, as addresses or CIDR ranges, whose `X-Forwarded-For` the server trusts, so rate limits count each real client; the limits are kept in the account store and survive a restart.
- Extensions import what they build on from `notes_vault_mcp.extension_api`; [Extensions](docs/reference/extensions.md) describes each hook.
- A vault server has one address, `/mcp`; another vault than the one picked when connecting is `/mcp?vault=<slug>`, and a slug is one lowercase word, unique among the vaults an account reaches. A new account's first vault is `notes`, and existing slugs that break the rule are rewritten on the first start.

### Fixed
- Two writes to one note at the same moment both land: `append_file`, `set_status`, `close`, `log_append`, `write_file` and the monthly log pages write over the version they read and read again when the note changed meanwhile, two `backlog_add` calls with one title get two notes, and a new vault's first files never overwrite a file already there.
- WebDAV `PUT` honours `If-Match` and `If-None-Match: *` and answers `412` when the file changed.
- A note's age counts calendar days in local time, so a note from five days back reads "5 days" also just after midnight.
- A vault server's root address opens the account page instead of answering not found.
- The access log leaves out query strings, so sign-in and invitation links never land in it.
- An unknown vault address answers like a known one the caller cannot reach.
- WebDAV opens vaults and talks to storage off the event loop, so a slow vault never stalls other requests or `/up`.
- An edit from Obsidian over WebDAV shows in search at once instead of after up to 20 seconds.
- `close` keeps a change that lands on the note while it is being archived.

## 0.12.0

### What's new
- In a terminal, `search`, `backlog`, `read` and `lint` print for a person: titles first, paths whole, ages in words, colour for status, priority and kind, and rows cut at the terminal's width. In a pipe or with `--plain` they print as before; `NO_COLOR` drops the colour.
- `context` shows what a session in this repo starts with: the system note, the commits newer than it, open tasks, the backlog, traps and the last log line. `--path` reads it for another folder.
- `backlog --paths` shows each note's path, and a backlog of more than 20 ideas is grouped by priority; `--all` shows every row.
- `read --raw` prints the note exactly as stored, also in a terminal.
- `lint` in a terminal counts the findings per kind with a plain explanation, and `lint --kind <kind>` lists one kind.

### Fixed
- The session-start hook's repo log shows only log lines, never the log note's frontmatter.

## 0.11.0

### What's new
- The stop hook asks the agent to save the decisions and ideas in your messages once you have written three since its last vault write, also outside a git repo; `VAULT_STOP_HOOK_PROMPTS` sets the number or turns the check `off`.
- `VAULT_STOP_HOOK_COMMITS=off` turns off the stop hook's unlogged-commit and stale-note checks and keeps the message check, for sessions whose log another tool writes.
- A vault server loads the extensions installed beside it, from the Python entry point group `notes_vault_mcp.extensions`.
- An account on a vault server owns several vaults, each with its own name, address (`/v/<slug>/mcp`), keys, connections, layout and encryption key; a key, connector or signed-in computer reaches only the vault it was made for. An existing account's vault becomes its first vault, in place, on the first start.
- `/account` lists your vaults and makes new ones; each vault has its own page to connect, see and revoke what reaches it, export, import and delete it. Who you are, ways in, support access and account deletion move to Account. A vault's layout is picked when it is made and switched on its own page.
- `login --vault <slug>` signs a machine in to one vault, and `vault use <slug> [--dir <folder>]` picks the vault for this machine or for a folder and everything below it; the hooks and the stdio server follow the folder they run in.
- A vault can be shared: its owner invites people by email, each signs in with their own account and makes their own keys and connections, and the owner removes members, hands the vault over or deletes it. A shared vault keeps only current notes (system, howto, decision, reference, trap) and refuses tasks, backlog and logs with a message the agent can act on.
- Every write, move and delete in a shared vault, through MCP or WebDAV, is kept with who made it, through what and when; its page shows each change as a diff and restores the version before it as a new change. `accounts add-vault --shared`, `members`, `add-member` and `remove-member` do the same from the shell, and `whoami` and `status --probe` show each vault's kind and role.
- Support access is granted per vault, and `support` and `accounts import` take `--vault` when the account has several. `accounts vaults`, `add-vault` and `rename-vault` manage an account's vaults from the shell.

### Fixed

## 0.10.3

### What's new

### Fixed
- claude.ai connects with its published client identity, whose metadata lists a grant the server does not use.
- A client that signs in is asked for read and write access, not only read, so a connector can write notes.

## 0.10.2

### What's new

### Fixed
- Obsidian's Remotely Save syncs the vault itself over WebDAV, with its default base folder, instead of uploading a copy into a folder named after the Obsidian vault.

## 0.10.1

### What's new

### Fixed
- search, backlog, lint and every other command find a vault server named in the Claude Code plugin's settings, not only the task and log commands.

## 0.10.0

### What's new
- The HTTP server always serves accounts, one vault each, with an /account page. This breaks older setups: the --auth flag and the bearer, builtin, oidc and forwarded modes are removed, and so are the owner and tokens commands.
- Sign in with a passkey, an email link over SMTP, GitHub, your OpenID Connect provider or a trusted proxy's signed identity header. Each method is on when its settings are present, and an account can use several.
- The provider settings are called VAULT_OIDC_ISSUER, VAULT_OIDC_CLIENT_ID, VAULT_OIDC_CLIENT_SECRET and VAULT_OIDC_SCOPES. This breaks older setups: the VAULT_HOSTED_OIDC_* names are not read.
- Registration is by invitation, or open to any verified email with VAULT_REGISTRATION=open.
- Every account's vault is encrypted in storage, file contents and names, under its own key wrapped by a master key. This breaks older setups: the server needs VAULT_HOSTED_MASTER_KEY to start, made with notes-vault-mcp master-key generate.
- Encrypted vaults are stored under new paths, so an existing plaintext vault is not read. Bring it in with notes-vault-mcp accounts import, or as a zip on /account.
- master-key status and master-key rewrap rotate the master key, with the old one in VAULT_HOSTED_MASTER_KEY_PREVIOUS.
- An account exports its vault as a zip, imports one, and deletes itself on /account. notes-vault-mcp accounts delete does the same from the shell.
- An account can allow support access for an hour, a day or a week. The support files and support read commands and accounts import refuse without it, and every access shows in the account's log.
- Layout presets: init --preset para, simple, journal or custom, each with its folders, kinds, tags and Bases views. An account picks its layout on /account and can switch later.
- A schema can nest folders and is checked when loaded. One that does not hold leaves the vault on its preset, and context, lint and the account page list each problem.
- notes-vault-mcp login signs a machine in to a vault server through the device flow, and whoami and logout go with it. With VAULT_SERVER_URL set, the stdio server, the hooks and every command work through the server.
- The Claude Code plugin offers a vault server as a third choice beside a folder and a bucket.
- New tools on a server: whoami for every connection, and claim, release and changelog for a signed-in machine. context answers JSON with format json.
- lint --write and --park-stale run through a vault server when VAULT_SERVER_URL is set, so a scheduled lint can keep a hosted vault tidy.
- snapshot --json answers a board's system and task notes, the openings of open tasks and the repo log tails in one call under an etag, and --since answers unchanged while nothing moved. log path takes --repo more than once.
- read prints one note, from the local vault or through the vault server, and --body leaves out its frontmatter.

### Fixed
- A repo log that log_append creates is titled Log — <repo>.
- A changelog period takes whole days at both ends, so it keeps commits from early on its first day and leaves out those from early on the next period's first day.

## 0.9.2

### What's new
- The claim field on a task note is called kebbie. This breaks older claims: a note that carries the previous key is not read as claimed.

### Fixed

## 0.9.1

### What's new
- Search by meaning on a server. A query is answered beside a running embed instead of waiting behind it, and the model stays warm while the HTTP server runs, so searches use vectors during a first sync. A server starts embedding notes without vectors as soon as it starts.
- Lower memory while embedding. VAULT_EMBED_BATCH (default 16), VAULT_EMBED_THREADS (half the usable cores, at most 4, counting a container's CPU limit) and VAULT_EMBED_MAX_TOKENS (default 256) bound the embedding process. The S3 connection pool matches the fetch workers.
- Hosted mode. --auth hosted serves one vault per account, with sign-in through an OIDC provider, invitations, an /account page and API keys. A machine can get its key through the OAuth device flow at /device. OAuth clients can identify themselves with a client ID metadata document.

### Fixed
- WebDAV refuses writes from credentials that may only read.
- Notes written through the claude.ai connector count as touched by the session-start hook.

## 0.9.0

### What's new
- Search by meaning. With VAULT_SEMANTIC=on and the [semantic] extra installed, search also finds notes that match the question without sharing its words. Results say how many matched by meaning only. The default model is mpnet; set VAULT_SEMANTIC_MODEL to pick MiniLM instead.
- One embedding process is shared by every session on a machine, so several agents searching at once load the model once.
- Two images: ghcr.io/gronare/notes-vault-mcp:v0.9.0 without the model, and :v0.9.0-semantic with it.
- The README is rewritten around what the server does and how to start it.

## 0.8.0

### What's new
- A task note can carry a claim field naming the machine and task working on it. The server keeps the field as stored when a note is written, so an agent can claim a note without another machine overwriting the claim.
- Every release checks that the tag matches the package version before anything is published.
