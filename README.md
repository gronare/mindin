<!-- mcp-name: io.github.gronare/notes-vault-mcp -->

# notes-vault-mcp

Memory for coding agents, kept as a folder of markdown notes you own, with a schema that holds and a tie to your git history.

The vault is an Obsidian-style folder of `.md` files with YAML frontmatter, on disk, in an S3 bucket or
on a vault server. This server indexes it and gives any MCP client (Claude Code, Codex, Cursor, claude.ai,
Claude Desktop and mobile) a cheap way to search it, read it and write it. Writes are checked against a
schema the vault carries. Notes can point at code, and at the start of a session the agent gets the
commits made since each relevant note was last updated, so a stale note is caught before it is believed.

## Why this one

- **The notes are yours.** Plain markdown in a folder or a bucket, open in Obsidian, in git, or in `cat`. No database to export from.
- **A schema with teeth.** Required fields, legal statuses and kinds, folders that must link an area, a tag vocabulary. A write that breaks the contract is refused, not stored. Start from PARA, a simple root-first vault, a journal or your own layout.
- **A lifecycle, not a pile.** `close` archives finished work, `backlog_add` files what was deferred, `log_append` writes one line per repo per session, `lint` reports drift and can park stale tasks. A weekly `lint` keeps `active` meaning active.
- **Memory tied to code.** A note names the paths it describes; the session-start hook prints the commits newer than the note, and the stop hook refuses to end a session that left its commits unlogged.
- **Search that reads your languages.** Stemming for every language Snowball knows, folded diacritics, synonyms from the schema, and optional search by meaning that runs on your machine with one model process shared by every session.
- **Your own vault server.** Run it over HTTPS for yourself or a group: accounts with passkeys, email links, GitHub or your identity provider, each with as many vaults as it needs, encrypted in storage. claude.ai, Claude Desktop and the mobile app connect to it as a connector, and every machine's CLI, hooks and agents sign in to it.

[kebbie](https://kebbie.dev) uses it for each task's note.

## Quick start

A local folder and Claude Code:

```sh
claude mcp add vault -s user -e VAULT_PATH=$HOME/vault -- uvx notes-vault-mcp
VAULT_PATH=$HOME/vault uvx notes-vault-mcp init
```

`init` writes `.vault/schema.yml` and the Obsidian Bases views into the vault, keeps anything that
exists, and prints a CLAUDE.md snippet with the rules an agent follows on its side: `context` at session
start, a note before the first edit, `log_append` at the end. Paste it into your CLAUDE.md.
`init --preset para` starts from another [layout](#layouts).

The [Claude Code plugin](#claude-code) does the same and adds the two hooks and the skills. For a
vault in S3 or MinIO and for other clients, see [Run it locally](#run-it-locally); for claude.ai and for
a vault several machines share, see [Run a server](#run-a-server).

- [How it works](#how-it-works)
- [Tools](#tools)
- [Run it locally](#run-it-locally)
- [Run a server](#run-a-server)
- [Connect to a server](#connect-to-a-server)
- [Search by meaning](#search-by-meaning)
- [Command line](#command-line)
- [Configuration](#configuration)
- [Deploy](#deploy)

## How it works

### Notes and folders

A note is a markdown file with frontmatter. The built-in layout has five folders, and the folder
decides the kind of note and how much it weighs in search:

| Folder | Kind | Weight | Role |
| --- | --- | --- | --- |
| `Areas/` | system | 3.0 | One living note per system. Current state only. The hubs of the graph. |
| `Resources/` | reference | 2.0 | Traps, how-tos and decisions with their reasons. |
| `Projects/` | task | 1.0 | Open work spanning sessions, and the backlog. Closed with `close`. |
| `Log/` | log | 1.0 | One append-only log per repo, plus period pages. |
| `Archive/` | archive | 0.3 | History. Searched only on request. |

The contract every note must satisfy:

```yaml
frontmatter:
  required: [title, date, updated, tags, status]
  optional: [kind, area, summary, path, superseded_by, priority, source, kebbie]
  area_required_in: [Projects, Resources, Log]
  status_values: [draft, active, backlog, complete, superseded]
  kind_values: [system, task, trap, howto, decision, reference, log]
  priority_values: [urgent, high, medium, low]
```

Each kind carries a one-line rule, and the server puts the rules in its instructions so every client
picks kinds the same way:

```yaml
kinds:
  trap: "a gotcha that bit once and will bite again; nothing to do, nothing was chosen"
  decision: "a choice and its reasons; something was chosen, alternatives were dropped"
  howto: "steps that are known to work; nothing chosen, nothing open"
```

`path` ties a note to code: a comma-separated list of directories (`~` is kept as written and also
indexed expanded). `context` and the session-start hook match the working directory against it.

### Layouts

The folders above are the built-in layout, which `init` writes when it is given no preset. `init
--preset <name>` starts a vault from another layout instead, each with its own folders, frontmatter
contract, kinds, tags and Bases views:

| Preset | Folders |
| --- | --- |
| `para` | Projects, Areas, Resources (with Resources/Logs), Archive |
| `simple` | Most notes in the root, typed by a `categories` link to an overview note; Projects (open work and the backlog), References, Clippings, Daily, Templates, Attachments. No log, no archive |
| `journal` | Daily (one note per day, `Daily/YYYY/YYYY-MM-DD.md`), Notes, Projects, Logs, Templates, Archive |
| `custom` | An empty schema you fill in |

`simple` follows Steph Ango's vault: a category note such as `Books.md`, made from
`Templates/Category Template`, embeds `Templates/Bases/Category.base` and lists every note whose
`categories` links to it, and the daily note template embeds the notes written or linked that day. Open
work is the one exception to notes in the root: it lives in `Projects/`, so the tools can file it, and
`Open work.base` and `Backlog.base` show it. In Obsidian, point the core Daily notes
plugin at the `Daily` folder and `Templates/Daily Note Template`; for `journal`, set its date format to
`YYYY/YYYY-MM-DD`.

A vault whose schema names a preset this version does not ship is read as written, over the empty
custom schema, and `context`, `lint` and the account page say so; `init` keeps that schema until
`--preset <name> --force` switches it.

Each tool finds its home by folder kind: open tasks and the backlog go to the folder of kind `task`,
`log_append` writes to the folder of kind `log`, and `close` moves notes to the folder of kind
`archive`. In a layout without one of these, the tool says which folder kind is missing.

`init --preset <name> --force` switches an existing vault: the schema and the views are rewritten, the
old schema is kept as `.vault/schema.previous.yml`, and no note is moved or deleted. Notes in folders the
new layout does not name stay where they are and stay searchable; each is checked against the new rules
the next time it is written, and `lint` lists the ones that do not fit it. The `init` tool sets up only
a vault without a schema, so an MCP client cannot change the rules that check its own writes. On a
[server](#run-a-server), a vault picks its layout when it is made and switches it on its page later.

### The schema

`.vault/schema.yml` is deep-merged over the preset it names (`preset: para`), or over the built-in
layout when it names none, so it only needs to carry what differs. A vault adds a kind by adding an
entry (`research: "reading notes on a topic; no task, no decision"`), rewrites a rule by restating it,
and drops a kind or a folder by setting it to `null`. Folders nest by path
(`Work/Logs: { kind: log }`). `preset: custom` starts from nothing. `VAULT_SCHEMA` points at a local
file that overrides the one in the vault.

The schema is checked when it is loaded. A schema that does not hold leaves the vault on its preset,
and `context`, `lint` and the account page list each problem, for example
`folders.Inbox.kind 'todo' is not one of: system, reference, task, log, archive`. A running server picks
up an edited schema on its next refresh.

Other settings: `language` and `other_languages` as ISO codes (`language: sv`, `other_languages: [en]`;
changing them rebuilds the index), the tag vocabulary and whether it is strict, synonym groups,
`stale_after_days` (30), the search weights, the task section headings per language, and the repo log's
file and line formats:

```yaml
log:
  folder: Log
  file_format: "{repo}-log.md"
  period_format: "{repo}-{period}.md"
  entry_format: "- [{date}] {line} | branch: {branch} | commits: {commits} | {area}"
```

The default log suffix keeps `Log/myapp-log.md` from colliding with the hub note `Areas/myapp.md`,
which Obsidian could not tell apart as `[[myapp]]`. Every place that builds the log path reads
`file_format`, so changing it moves all of them at once; rename the existing files to match.

### Search

A local SQLite index under `VAULT_CACHE_DIR` holds every note's frontmatter and body. Every tool call
refreshes it first, at most once every 20 seconds, fetching only the notes whose version changed. Search
never downloads the vault.

Ranking is BM25 with folder weights, recency decay and a status factor, so the living system note
outranks a two-year-old archived plan on the same words. The index folds diacritics, stems every word
in the vault's languages (`äppelpressar` finds `äppelpressen`, `pressing` finds `presses`) and expands
the schema's synonyms (`bokning` finds `booking`). A stemmed-only match weighs `stemmed_weight` (0.5),
so the exact form ranks first.

Every term is required; when no note has them all, the result holds the notes matching the most terms
and the header says so. A query that is exactly a note's stem, or `[[stem]]`, puts that note first, even
from the archive. A bare commit sha finds the notes that mention it. Quoted phrases match as phrases.
Archive and superseded notes are hidden unless asked for, and the header says how many. When nothing
matches, the reply names the vault's languages so the client can translate the terms and search again.

With [search by meaning](#search-by-meaning) on, notes that share no word with the query are found too.

### Session start

`context` is the one call at the start of a session. For a working directory and repo name it returns
the system notes whose `path` covers the directory, the open tasks for it, a triage list of open notes
older than `stale_after_days` with the three ways to settle each (close, a dated line, or park in the
backlog), the backlog items for the area, the reference notes and the tail of the repo log.

The Claude Code session-start hook prints that bundle and then, for each system note, the commits
touching the note's paths since the note was last updated. That is the answer to "is this note still
true?" before the agent trusts it.

### Lifecycle, backlog and the repo log

- `close` sets a note's status to `complete` (or `superseded`, with `superseded_by`, when `merged_into`
  is given) and moves it into the archive.
- `set_status` changes the status without rewriting the note: `backlog` with a priority parks it,
  `active` picks it up again.
- `backlog_add` files a task note with `status: backlog`, an area, a one-line summary, an optional
  priority and an optional source (who said it and when, or a sha). `backlog` lists them by priority
  then age. Lint leaves backlog notes alone however old they get.
- `log_append` writes one dated line to the repo log with the commits the session produced, and
  creates the log when it is missing. The stop hook checks these shas.

### Lint

`lint` reads every note and reports `broken_frontmatter`, `missing_required` (per field),
`invalid_field`, `missing_area`, `unknown_tags` (only with a strict vocabulary), `unresolved_links`,
`orphans` (no inbound wikilink; log and archive ignored), `stale_active`, `done_not_closed`,
`archive_status_mismatch`, `duplicate_stems` and `superseded_target_missing`.

```sh
notes-vault-mcp lint
notes-vault-mcp lint --write Log/lint.md --park-stale
```

`--write` keeps the report as a note. `--park-stale` sets open task notes untouched for longer than
`stale_after_days` to `backlog` (priority `low`, source `veckolint <date>`) and lists them under
`parked`, so `active` means touched within the window; `set_status(path, "active")` revives one. A
note claimed through `task claim` within the last 14 days is left alone. Run it weekly. For a vault on a
server, sign the machine that runs the schedule in with [`login`](#sign-a-machine-in) and set
`VAULT_SERVER_URL`; the server parks the notes and writes the report itself.

## Tools

| Tool | Cost | What it does |
| --- | --- | --- |
| `search` | cheap | Full-text and, when on, meaning search over the index. Filters: `folder`, `status`, `tag`, `kind`, `area`, `path_prefix`, `since`, `include_archive`, `include_superseded`, `limit`. |
| `context` | cheap | The session-start bundle for a `path` and `repo`; `format: "json"` answers it as data. |
| `backlog` | cheap | The backlog by priority then age, filtered by `area` and `priority`. |
| `list_files` | cheap | Paths only. |
| `whoami` | cheap | The account a connection signs in as and whether it may write. |
| `read_file` | moderate | One note, prefixed with `etag: <version>`. A superseded note carries a warning callout. |
| `lint` | moderate | Reads every note and reports drift. |
| `write_file` | write | Validates against the schema and refuses the write if it does not hold. Stamps `updated`, fills `date`. `expected_etag` makes the write conditional; `"*"` creates a note that must not exist yet. On S3 the condition travels with the PUT (`If-Match`, `If-None-Match: *`); a server that refuses conditional PUTs gets a HEAD first, logged once. Only `.md` notes outside dot-folders are written. |
| `append_file` | write | Appends and bumps `updated`; the note must exist and still satisfy the schema. |
| `set_status` | write | Changes a note's status, with `priority` and `source` for the backlog. |
| `close` | write | Finishes a note and moves it into the archive. |
| `backlog_add` | write | Files a backlog note. |
| `log_append` | write | One dated line in the repo log, with the commits and the branch. |
| `move_file` | write | Moves or renames a note; it must satisfy the schema at the destination. |
| `delete_file` | write | Deletes for good. Prefer `close`. |
| `init` | write | Sets up a vault without a schema: the schema of the `preset` it is given and the Bases views. On a vault with a schema it only writes the missing views. |
| `obsidian_access` | write | Server only. Mints the WebDAV password for Obsidian's Remotely Save and shows it once. |

A server also serves `claim`, `release`, `changelog` and `lint_and_park` to a machine signed in with
[`login`](#sign-a-machine-in), for the [task and log commands](#task-and-log-commands), `changelog` and
`lint --write --park-stale` that run through it; connectors never see them.

Over HTTP, a connection carries `vault:read` or `vault:read vault:write`. A write tool called on a
read-only connection fails with an error saying so, rather than half-writing.

## Run it locally

Set `VAULT_PATH` for a folder, or `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` and `S3_BUCKET` for a
bucket (MinIO included). The server runs over stdio, started by the client. With no vault configured, it
exits with one line saying so.

### Claude Code

The plugin installs the server, the two hooks and the skills, and asks for the vault settings: a local
folder, an S3 bucket, or a [vault server](#claude-code-with-a-server).

```sh
claude plugin marketplace add https://github.com/gronare/claude-plugins
claude plugin install vault@gronare
```

It passes the settings as `CLAUDE_PLUGIN_OPTION_<NAME>` variables, which the server reads as if they
were the bare names; the bare name wins when both are set.

Without the plugin, add the server and the hooks yourself:

```sh
claude mcp add vault -s user \
  -e S3_ENDPOINT=https://minio.example.com \
  -e S3_ACCESS_KEY=... \
  -e S3_SECRET_KEY=... \
  -e S3_BUCKET=vault \
  -- uvx notes-vault-mcp
```

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "VAULT_PATH=$HOME/vault uvx notes-vault-mcp hook session-start" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "VAULT_PATH=$HOME/vault uvx notes-vault-mcp hook stop" }] }
    ]
  }
}
```

Both hooks read the hook JSON on stdin and exit 0 whatever happens. `session-start` prints the
[context bundle](#session-start) with the commits since each note. `stop` blocks the end of a session
that left work unrecorded: commits made in this checkout in the last 24 hours whose sha is not in the
repo log, and open task notes older than `stale_after_days` that the session read or wrote through the
vault tools. It answers `{"decision": "block", "reason": ...}` with the `log_append` call to make, or
nothing when the vault is up to date. Stale notes the session did not touch are listed by `context`
under `triage` at the next start instead. It also blocks, in any directory, once you have written three
messages since the session's last write through the vault tools, asking the agent to save the decisions,
deferrals and ideas in them; it asks once per message, so an agent with nothing to save stops on its next
try and is asked again after three more. `VAULT_STOP_HOOK=off` silences the hook, which a tool that drives
agent sessions and writes the log itself sets in the sessions it starts. `VAULT_STOP_HOOK_PROMPTS` sets how
many messages it waits for, or turns that check off with `off`; `VAULT_STOP_HOOK_COMMITS=off` turns off the
commit and stale-note checks alone, for sessions whose log another tool writes.

### Codex, Cursor and other MCP clients

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

The server's instructions carry the schema's rules, so a client without the CLAUDE.md snippet still
picks kinds and statuses the same way. The container image does the same job:

```sh
docker run --rm -i -e S3_ENDPOINT -e S3_ACCESS_KEY -e S3_SECRET_KEY -e S3_BUCKET ghcr.io/gronare/notes-vault-mcp:latest
```

## Run a server

`serve --transport http` runs a vault server: people sign in to accounts, and each account owns one
or more vaults, each with its own address, keys, connections, index and encryption key. It serves a household, a team, or you on several machines.
claude.ai, Claude Desktop and the mobile app connect to it as a custom connector, and any machine's CLI,
hooks and agents [sign in to it](#connect-to-a-server).

```sh
export VAULT_PUBLIC_URL=https://vault.example.com
export VAULT_HOSTED_MASTER_KEY=$(notes-vault-mcp master-key generate)
export S3_ENDPOINT=https://s3.example.com S3_BUCKET=vault S3_ACCESS_KEY=... S3_SECRET_KEY=...
export VAULT_SMTP_HOST=smtp.example.com VAULT_SMTP_FROM="Vault <vault@example.com>" \
  VAULT_SMTP_USERNAME=... VAULT_SMTP_PASSWORD=...
notes-vault-mcp serve --transport http --host 0.0.0.0 --port 8765
notes-vault-mcp accounts invite ann@example.com
```

It needs three things, and refuses to start without them:

- `VAULT_PUBLIC_URL`, the https address people reach it on, path included when the server is not at the
  root (`http://localhost` works for a trial). Put TLS in front of it; the examples under
  [Deploy](#deploy) use Caddy, which fetches the certificate itself.
- `VAULT_HOSTED_MASTER_KEY`, the key that seals every vault (see [Encryption](#encryption)).
- At least one way to sign in other than passkeys.

The vaults live in the configured folder or bucket, one under `VAULT_SUBJECT_PREFIX/<vault id>/` each.
Search by meaning works as [on a single machine](#search-by-meaning), with one model shared by every
account.

### Sign-in methods

There are no passwords. Each method is on when its settings are present, and the sign-in page at
`/signin` lists only those.

| Method | Settings | Register this callback |
| --- | --- | --- |
| Email link | `VAULT_SMTP_HOST`, `VAULT_SMTP_FROM`, `VAULT_SMTP_USERNAME`, `VAULT_SMTP_PASSWORD` | |
| GitHub | `VAULT_GITHUB_CLIENT_ID`, `VAULT_GITHUB_CLIENT_SECRET` | `VAULT_PUBLIC_URL/signin/github/callback` |
| Your identity provider (Pocket ID, Okta, Google Workspace, Keycloak) | `VAULT_OIDC_ISSUER`, `VAULT_OIDC_CLIENT_ID`, `VAULT_OIDC_CLIENT_SECRET` | `VAULT_PUBLIC_URL/oidc/callback` |
| A trusted proxy | `VAULT_IDENTITY_PUBLIC_KEY`, `VAULT_IDENTITY_ISSUER` | |
| Passkeys | on unless `VAULT_PASSKEYS=off` | |

- **Email link.** A link works once, for 15 minutes. `VAULT_SMTP_SECURITY` is `starttls` (port 587, the
  default), `ssl` (465) or `none` (25, only without a username and password); `VAULT_SMTP_PORT`
  overrides the port.
- **GitHub.** Create an OAuth app at github.com/settings/developers with the callback above. The account
  is tied to the GitHub user, so a renamed login keeps working; only verified emails count.
- **Your identity provider.** Register one confidential client with the callback above.
  `VAULT_OIDC_NAME` is the button's label; `VAULT_OIDC_SCOPES` defaults to `openid email profile`.
- **A trusted proxy.** When a proxy in front authenticates people and forwards a signed identity token
  (the pattern Cloudflare Access uses), the server verifies it with `VAULT_IDENTITY_PUBLIC_KEY` (PEM,
  RS256 or ES256, raw or base64 on one line) and signs that person in, MCP requests included. The token
  arrives in `X-Forwarded-Identity` (`VAULT_IDENTITY_HEADER`), needs `iss`, `sub`, `exp` and an `aud`
  naming this server's host, and carries an entitlement: `vault:write` or `vault:read` by default
  (`VAULT_IDENTITY_WRITE_CLAIMS`, `VAULT_IDENTITY_READ_CLAIMS`, in its `entitlements` list or `scope`).
  `VAULT_IDENTITY_TYP` pins its `typ`, and `VAULT_IDENTITY_NAME` labels the button. The proxy's email
  counts as verified. The header is ignored unless these are set.
- **Passkeys.** Sign in once another way, then use "Add a passkey" on the account page. After that,
  "Sign in with a passkey" needs nothing else.

An account can have several ways in. A sign-in whose verified email matches an account joins it, so a
person can start with an email link and later use GitHub or a passkey. The account page lists every way
in and removes any but the last.

### Accounts

New accounts are by invitation: `accounts invite ann@example.com` lets that address in the first time it
signs in with a verified email. `VAULT_REGISTRATION=open` lets anyone with a verified email create an
account.

```sh
notes-vault-mcp accounts invite ann@example.com
notes-vault-mcp accounts list
notes-vault-mcp accounts suspend ann@example.com     # every key and token stops at once
notes-vault-mcp accounts activate ann@example.com
notes-vault-mcp accounts delete ann@example.com --yes
notes-vault-mcp accounts vaults ann@example.com                      # slug, name, notes, files, keys
notes-vault-mcp accounts add-vault ann@example.com "Workshop" [--slug workshop] [--preset para]
notes-vault-mcp accounts add-vault ann@example.com "Orchard club" --shared
notes-vault-mcp accounts rename-vault ann@example.com workshop --name "Shed" --slug shed
notes-vault-mcp accounts members ann@example.com orchard-club              # owner, members, invitations
notes-vault-mcp accounts add-member ann@example.com orchard-club bo@example.com
notes-vault-mcp accounts remove-member ann@example.com orchard-club bo@example.com
```

A new account starts with one vault, named Personal, at an address made from its email. Each person
opens `VAULT_PUBLIC_URL/account`, which lists their vaults and makes new ones. A vault has a name and a
slug that is unique on the server; its address is `VAULT_PUBLIC_URL/v/<slug>/mcp`. Each vault's page has:

- **Connect.** The vault's MCP address, the `login` line for a computer, and the WebDAV address.
- **Connections and keys.** Every connector, signed-in computer and key that reaches this vault, with when
  it was last used; disconnect, sign out or revoke each one, and make a key.
- **Layout.** A new vault picks PARA, Simple, Journal or Custom [layout](#layouts) when it is made, and
  switches to another of them here later, without moving any note. A vault made without one, such as an
  account's first vault, asks here. A vault in the built-in layout keeps it; the page names it, and it is
  switched from the CLI only.
- **Your data.** The vault downloads as a zip at any time; a zip imports into it, replacing files with
  the same path.
- **Delete this vault.** Typing its name, after a sign-in within the last ten minutes, destroys the
  vault's key, files, index, keys and connections. The account's other vaults stay as they are.

### Shared vaults

A vault is personal or shared, chosen when it is made. A personal vault is its owner's alone. A shared
vault belongs to its owner and the members the owner invites, and holds only how things are now: system
notes in `Areas/`, howto, decision, reference and trap notes in `Resources/`, and retired notes in
`Archive/`. Its layout is fixed. The server refuses, with a message the agent can act on, a note in any
other folder or of another kind, and `backlog_add`, `log_append`, `set_status`, `claim`, `release` and
changelog writes; tasks, the backlog and logs stay in each member's personal vault. `context` in a shared
vault says so instead of listing tasks, a backlog or a log.

The owner invites an email on the vault's page, or with `accounts add-member`. With SMTP set up the
address gets one email with a sign-in link; signing in through it, or any other way with that verified
address, makes the person a member, and makes them an account when they have none, also under
`VAULT_REGISTRATION=invite`. A pending invitation can be withdrawn. Members sign in with their own
account and make their own keys and connections to the vault; it shows under Shared on their vaults
page. Removing a member, or a member leaving, stops their keys, tokens and signed-in computers for that
vault on their next request. The owner can make a member the owner instead, and deletes the vault for
everyone. Support access to a shared vault is the owner's to grant.

Every change to a shared vault applies at once and is kept: each write, move and delete through MCP or
WebDAV records the path, what happened, the member's email, the key, computer or connector it came
through and the time, and keeps the version before it, sealed with the vault's key. The vault's page
shows the recent changes and the full history; each change opens as a diff, and "Restore previous"
writes the version before it back as a new change. Files in dot-folders, such as `.obsidian/`, are not
kept.

An account that owns a shared vault with other members cannot be deleted until each such vault is
handed over or deleted. A deleted member's past changes stay in the history under their email.

The Account page shows who the person is, their ways in, and:

- **Support access.** The person can let the server's staff open one vault for an hour, a day or a week,
  and end it early. Every access is logged with time, vault, operator and reason, and the log shows on
  the page.
- **Deletion.** The account deletes itself after a sign-in within the last ten minutes and its email typed
  out: every vault's key goes first, then their files, indexes, keys, connections and the ways in. It is
  refused while the account owns a shared vault that has other members.

`accounts delete` does the same from the shell. `hosted.sqlite` under `VAULT_AUTH_DIR` holds the
accounts, the ways in, the hashed keys and tokens, the registered clients and every vault's sealed key.

### Encryption

Every vault is sealed before it reaches storage: file contents with AES-256-GCM, and file and folder
names too, so the bucket holds only opaque object names and ciphertext. Each vault has its own data
key, wrapped by the master key in `VAULT_HOSTED_MASTER_KEY` (base64 of 32 bytes, made with
`notes-vault-mcp master-key generate`). The server refuses to start without it, or with a master key
that does not open the vault keys it holds.

Keep the master key outside the bucket and back it up together with `hosted.sqlite`: a vault is read
with both, and with neither alone. The server decrypts when it answers, so search, connectors and
Obsidian over WebDAV see plain notes; it is not end-to-end encryption. The search index and the vectors
for search by meaning are kept in plaintext under `VAULT_CACHE_DIR`; put that directory on an encrypted
volume.

To rotate the master key, set the new key in `VAULT_HOSTED_MASTER_KEY` and the old one in
`VAULT_HOSTED_MASTER_KEY_PREVIOUS`, run `notes-vault-mcp master-key rewrap`, check
`notes-vault-mcp master-key status`, then remove the old key.

Run one server per bucket: a sealed vault keeps its listing for five seconds, so a second server writing
to the same vault is seen late.

### Support access and import

With the account's permission for one vault, the staff reads that vault and imports into it from the
shell. Each command names who runs it and why, and refuses without the permission. `--vault <slug>` names
the vault when the account has more than one:

```sh
notes-vault-mcp support files ann@example.com --vault workshop --operator sam --reason "lost note"
notes-vault-mcp support read ann@example.com Areas/home.md --operator sam --reason "lost note"
notes-vault-mcp accounts import ann@example.com --from-path ~/old-vault --operator sam --reason "move in"
notes-vault-mcp accounts import ann@example.com --from-s3-bucket old-vault --from-s3-prefix ann \
  --operator sam --reason "move in"
```

`accounts import` seals every file of a plaintext vault into the account. From S3 it takes
`IMPORT_S3_ACCESS_KEY` and `IMPORT_S3_SECRET_KEY` (or the server's own `S3_*` keys), and
`--from-s3-endpoint` and `--from-s3-region` when they differ from the server's.

### The `Host` header

The MCP endpoint answers the hostname of `VAULT_PUBLIC_URL` and localhost, and gives any other `Host`
`421`. A
Service name, a LAN hostname, or the host a proxy rewrites requests to goes in `VAULT_ALLOWED_HOSTS`.

## Connect to a server

Every connection reaches one vault, the one it was made for, and asks for read or read and write. Each
vault's page on `VAULT_PUBLIC_URL/account` shows these with its own addresses.

| Client | How it connects |
| --- | --- |
| claude.ai, Claude Desktop, mobile | A custom connector with the vault's address, `VAULT_PUBLIC_URL/v/<slug>/mcp`, signed in through the browser |
| Claude Code, Codex, kebbie, the hooks and the CLI | [`login`](#sign-a-machine-in) once per machine and vault, then `VAULT_SERVER_URL` |
| Any other OAuth-capable MCP client | The vault's address as a remote server, signed in through the browser |
| A client that only takes a header | A key made on the vault's page, sent as `Authorization: Bearer nvm_...` |
| Obsidian's Remotely Save | WebDAV at `VAULT_PUBLIC_URL/dav/` with a key from the vault's page |

`VAULT_PUBLIC_URL/mcp` works for every client too: a key reaches the vault it was made for, and a client
signed in through the browser asks which vault it gets.

### claude.ai, Claude Desktop and mobile

In claude.ai, open Settings, Connectors, Add custom connector, and paste the vault's address,
`VAULT_PUBLIC_URL/v/<slug>/mcp`; name the connector after the vault. The browser signs in to the server
and asks for read or read and write; the connector reaches that vault and no other. Add one connector
per vault you want in claude.ai. There is nothing to register: the
server takes claude.ai's client metadata document, and dynamic registration for other clients. Connect
once on the web and the same connector appears in Claude Desktop and in the mobile app.

### Sign a machine in

```sh
uvx notes-vault-mcp login https://vault.example.com --vault notes
```

It prints a link and a code. Open the link on any device, sign in, check that the code matches and
approve read and write. Without `--vault` the approval page asks which vault the machine gets. The machine gets its own key, named after its host name (`--name` picks another),
stored in `~/.config/notes-vault-mcp/credentials.json` and readable only by you. Over ssh it works the
same way, and `--no-browser` only prints the link. `notes-vault-mcp whoami` shows the account, the vault
and whether the key may write, and for a shared vault the account's role in it; `notes-vault-mcp logout [--vault <slug>]` forgets the keys on this machine.
Revoke a key for good on its vault's page.

Then set `VAULT_SERVER_URL` to the server. The stdio MCP server, the hooks and every command work through
it as with a folder or a bucket, except `sync`, since the server keeps the index. `VAULT_SERVER_URL` wins over `VAULT_PATH` and
`S3_*` when both are set.

### Several vaults on one machine

A machine can be signed in to several of your vaults on the same server: run `login --vault <slug>` once
for each. The first one becomes the machine's default. A folder, such as a repo, can use another vault,
and so can everything below it:

```sh
notes-vault-mcp login https://vault.example.com --vault work
notes-vault-mcp vault use work --dir ~/projects/client-repo   # this folder and below use work
notes-vault-mcp vault use notes                              # the default everywhere else
notes-vault-mcp vault drop --dir ~/projects/client-repo      # back to the default
notes-vault-mcp vault list                                   # JSON: the vault here, the default, the folders
```

The hooks, the stdio MCP server and every command pick the vault of the folder they run in; the hooks
take it from the session's working directory. The keys and folders are kept in `credentials.json`.

### Claude Code with a server

```sh
claude plugin install vault@gronare --config vault_server_url=https://vault.example.com
```

Then run `/vault:login` (or the `login` command above), and reconnect with `/mcp`, plugin:vault:vault,
Reconnect. The session-start and stop hooks use the same key.

### Codex and other stdio clients

```sh
codex mcp add vault --env VAULT_SERVER_URL=https://vault.example.com -- uvx notes-vault-mcp
```

Any client from [Run it locally](#codex-cursor-and-other-mcp-clients) takes `VAULT_SERVER_URL` in place
of `VAULT_PATH`. Until the machine is signed in, the server's instructions tell the agent to ask for `login`.

### kebbie

kebbie runs the [task and log commands](#task-and-log-commands). Sign the machine in once; the commands
read `VAULT_SERVER_URL` from the environment or from the Claude Code plugin's settings.

### A machine without a browser

A tool that speaks the OAuth device flow can sign itself in the way `login` does.
`POST /device_authorization` with `name=<machine>` (and optionally `scope=vault:read`) answers
`device_code`, `user_code`, `verification_uri`, `verification_uri_complete`, `interval` and
`expires_in`. The person opens `VAULT_PUBLIC_URL/device`, enters the code and approves. Poll
`POST /token` with `grant_type=urn:ietf:params:oauth:grant-type:device_code` and the `device_code` every
`interval` seconds; it answers `authorization_pending` until the person approves, `slow_down` when polled
too often, `access_denied` or `expired_token`, and once `{"access_token": "nvm_...", "token_type":
"Bearer", "scope": "...", "key_name": "...", "resource": ".../mcp", "vault": "<slug>", "vault_name":
"..."}`. `vault=<slug>` on the first request asks for that vault; without it the person picks one when
approving.

### Header clients

Make a key on the vault's page, named after where it will live and read or read and write, and send
`Authorization: Bearer nvm_...` to `VAULT_PUBLIC_URL/mcp` or to the vault's address. The key reaches only
its vault; another vault's address refuses it. Keys are shown once, show when they were last used, and are
revoked on the vault's page.

### Obsidian

Remotely Save takes WebDAV at `VAULT_PUBLIC_URL/dav/`, the account's email as the username, and as the
password either a key from the vault's page or the one the `obsidian_access` tool mints when you ask Claude for
it (asking again replaces it). Leave the base folder as it is: whatever name Remotely Save puts after
`/dav/`, it syncs the vault itself. Set Depth to "only supports depth='1'". A new vault's welcome note
carries these steps. The address is the same for every vault: the key decides which vault opens. Other WebDAV clients open `VAULT_PUBLIC_URL/dav/vault/`. Obsidian hides `.vault/`, so
edit the schema with one of them (Finder's Connect to Server, Cyberduck). A proxy
in front must pass `/dav/*` straight through, with the `Authorization` header and the WebDAV verbs and
headers (`PROPFIND`, `MKCOL`, `MOVE`, `COPY`, `Depth`, `Destination`, `Overwrite`) intact.

## Search by meaning

Off by default. With it on, `search` also finds notes that share no word with the query: `inloggning`
finds the note about the OAuth flow, and an English query reaches a Swedish note. Exact names still win.
The word ranking and the meaning ranking are fused, a note's stem still puts that note first, a sha still
finds exactly the notes that mention it, and the filters and the archive and superseded hiding apply to
both. The header counts the rows found by meaning alone (`4 of 9, 2 by meaning only`), and a row that
matched one section of a long note names it: `§ OAuth-flödet · …`.

Install the `semantic` extra and set `VAULT_SEMANTIC=on`:

```sh
claude mcp add vault -s user \
  -e VAULT_PATH=$HOME/vault \
  -e VAULT_SEMANTIC=on \
  -- uvx --from 'notes-vault-mcp[semantic]' notes-vault-mcp
```

A server run from the container image uses the `-semantic` tag, which carries the extra, with
`VAULT_SEMANTIC=on`: `ghcr.io/gronare/notes-vault-mcp:v0.11.0-semantic`. The plain tag leaves it out, and
every client of one server shares its model and index.

It runs on your machine. The model, `paraphrase-multilingual-mpnet-base-v2` run through fastembed and
ONNX, is downloaded once from Hugging Face into `VAULT_CACHE_DIR/models`. No API key, and no note leaves
the machine. Each note is split at its headings, each section gets a vector, and the vectors live in the
same SQLite index: computed for the notes whose version changed, dropped with the note, and computed
again for every note when the model changes.

The model runs in one background process per machine, shared by every server and command that uses the
same `VAULT_CACHE_DIR`, however many sessions are open. The first one that needs it starts it; it keeps
running when that session ends, and exits by itself once nobody has asked it anything for five minutes.
The next search starts it again. An HTTP server keeps it loaded for as long as the server runs, so its
first search after a quiet spell finds by meaning too. If it crashes, the next search starts a new one. A session that never
searches, with no new note to embed, never starts it. Its log is `VAULT_CACHE_DIR/embed.log`. It needs
Unix sockets; without them the server says so once and searches by words.

On a machine short of memory, `VAULT_SEMANTIC_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
swaps in a smaller model: a quarter of the disk and about half the memory, and it finds fewer notes by
meaning. Every session on the machine should name the same model, since a session asking for another one
restarts the shared process with its own.

What it costs, measured on an M2 Pro with a vault of 500 notes in 3,300 sections:

| | mpnet (default) | MiniLM |
| --- | --- | --- |
| Disk | 1 GB for the model, about 4 KB per section in the index (14 MB here) | 250 MB for the model, about 2 KB per section (7 MB here) |
| Shared process | 1.1 to 1.7 GB just after it starts, about 500 MB once the system has paged out what searches do not touch | About 900 MB just after it starts, about 160 MB later |
| Each server | About 35 MB more than searching by words | About 25 MB more |
| First sync | About four minutes (14 sections a second) | About a minute (60 sections a second) |
| Query | About 23 ms, against 11 ms by words alone; about 50 ms each with six sessions searching at once | About 20 ms; about 33 ms with six at once |

On a server the shared process runs in the server's container, and the first sync is when it uses the
most memory: loading mpnet peaks near 2 GB by itself, and embedding stays at about that peak with the
defaults (`VAULT_EMBED_BATCH`, `VAULT_EMBED_MAX_TOKENS`). Give the container a memory limit of at least
3 GiB with mpnet; below that the kernel kills the process, and the server with it, while it embeds. A
restarted server goes on from the notes that already carry vectors.

A server starts embedding the notes still without vectors as soon as it starts, in the background, and
searches by words meanwhile. A query is embedded beside a long embed, not behind it; one that would wait
more than three seconds for the shared process, because it is still loading the model, is answered by
words. The first query after the process exited waits under a second for it to start again.

With `VAULT_SEMANTIC=on` and the extra missing, the server logs one line saying so and searches by
words. `notes-vault-mcp sync` embeds whatever is pending and says how many notes carry vectors;
`sync --rebuild` computes them all again.

## Command line

```sh
notes-vault-mcp serve --transport stdio          # the default, also with no subcommand
notes-vault-mcp serve --transport http --host 0.0.0.0 --port 8765
notes-vault-mcp init [--preset para] [--force]
notes-vault-mcp sync [--rebuild]                 # refresh the index; --rebuild reads every note again
notes-vault-mcp search "bokning" --limit 5       # the same ranking, from a shell
notes-vault-mcp lint [--write Log/lint.md] [--park-stale]
notes-vault-mcp backlog --area myapp --priority high
notes-vault-mcp changelog myapp 2026-08 --repo-path ~/projects/myapp [--write]
notes-vault-mcp changelog --all                  # this month, and last month during its first week
notes-vault-mcp hook session-start | stop        # the Claude Code hooks, JSON on stdin
notes-vault-mcp login [<url>] [--vault <slug>] [--name laptop] [--no-browser]
notes-vault-mcp whoami [<url>] | logout [<url>] [--vault <slug>]
notes-vault-mcp vault list | use <slug> [--dir <folder>] | drop --dir <folder>
notes-vault-mcp status [--probe]
```

On the server's own machine:

```sh
notes-vault-mcp accounts list | invite <email> | suspend <account> | activate <account>
notes-vault-mcp accounts delete <account> --yes
notes-vault-mcp accounts vaults <account> | add-vault <account> <name> [--slug] [--preset | --shared] | rename-vault <account> <slug> [--name] [--slug]
notes-vault-mcp accounts members | add-member | remove-member <owner> <slug> [<email>]
notes-vault-mcp accounts import <account> [--vault <slug>] --from-path <dir> | --from-s3-bucket <bucket> --operator <name> --reason <text>
notes-vault-mcp support files | read <account> [<path>] [--vault <slug>] --operator <name> --reason <text>
notes-vault-mcp master-key generate | status | rewrap
```

`changelog` prints the log lines, the git commits grouped by day, and the repo's notes dated inside
the period (`YYYY` or `YYYY-MM`), each day whole from midnight to midnight. With `--write` it keeps that
as a period page, `Log/<repo>-<period>.md`, between the markers `<!-- changelog:generated -->` and
`<!-- /changelog:generated -->`; prose above the markers (a summary written at month end) is left alone,
and the page's status follows the calendar. `--all` does it for every repo the session-start hook has
seen on this machine, and the stop hook runs that once a day, so the pages stay current without a cron.
Through a server, the commits are read on this machine and the page is written on the server.

### Task and log commands

For a tool that drives coding agents and records the facts itself at fixed points (start, push, done,
reset) instead of relying on the agent to call the MCP tools. Each prints one JSON object on stdout and
exits 0; a failure exits non-zero with one line on stderr. Every write goes through the same path as
the MCP tools: schema validation, the `updated` stamp and the index.

```sh
notes-vault-mcp task open --name "Fix login" --cwd ~/wt/wt1.myapp [--branch fix-login]
# {"path": "Projects/myapp-fix-login.md", "created": true, "repo": "myapp", "area": "myapp"}
notes-vault-mcp task open --note Projects/myapp-idea.md --cwd ~/wt/wt1.myapp
# {"path": "Projects/myapp-idea.md", "created": false, "repo": "myapp", "area": "myapp"}
notes-vault-mcp task check Projects/myapp-fix-login.md --need problem,solution,decisions
# {"missing": ["solution", "decisions"]}
notes-vault-mcp task close Projects/myapp-fix-login.md        # {"path": "Archive/myapp-fix-login.md"}
notes-vault-mcp task backlog Projects/myapp-fix-login.md --priority medium   # {"path": "Projects/…"}
notes-vault-mcp task claim Projects/myapp-idea.md --machine mac --task fix-login --repo myapp [--force]
# {"path": "Projects/myapp-idea.md", "claimed": true, "claim": {"machine": "mac", "task": "fix-login", "repo": "myapp", "since": "2026-10-23T09:12:00Z"}, "replaced": null}
notes-vault-mcp task release Projects/myapp-idea.md [--machine mac]
# {"path": "Projects/myapp-idea.md", "released": true, "claim": null}
notes-vault-mcp task release --machine mac --task fix-login
# {"paths": ["Projects/myapp-renamed.md"], "released": true}
notes-vault-mcp log append --cwd ~/wt/wt1.myapp --line "Login fixed" --commit abc1234 --commit def5678
# {"path": "Log/myapp-log.md"}
notes-vault-mcp log path --repo myapp                         # {"path": "Log/myapp-log.md"}
notes-vault-mcp log path --repo myapp --repo docs            # {"myapp": "Log/myapp-log.md", "docs": "Log/docs-log.md"}
notes-vault-mcp snapshot --json [--since ETAG]               # the board's notes and logs in one answer, see below
notes-vault-mcp read Projects/myapp-idea.md [--body]          # the note as stored, or only its text after the frontmatter
notes-vault-mcp status
# {"configured": true, "version": "0.11.0", "backend": "s3", "writable": null}
notes-vault-mcp status --probe
# {"configured": true, "version": "0.11.0", "backend": "s3", "writable": true}
```

The repo is the main checkout's name, also from a worktree, and the area is the system note the
session-start hook would load for that directory, falling back to the repo name (`"area": null` when no
note matched). `task open` writes a task note with the task name as a placeholder title, status
`active`, `path` set to the main checkout, `source: branch <name>`, and the section headings with empty
bodies; the stem is the repo plus the task name, made unique across the whole vault. `--note` claims
an existing task note instead: status `active`, nothing else changed. `--branch` defaults to the branch
checked out in `--cwd`.

`snapshot --json` answers, in one call, what a board needs from the vault: every system and task note
outside the archive and log folders, each with `key`, `stem`, `role` (`system` or `task`, taken from the
folder, so a client needs no folder names), `kind`, `status`, `title`, `summary`, `priority`, `area`,
`paths`, `date`, `updated`, `mtime` and its `claim` (or `null`); `openings`, the first paragraph of each
active or draft task; `logs`, the last 50 lines of each repo log as `{date, text, links}`;
`log_key_template` (such as `Log/{repo}-log.md`, or `null` when the layout keeps no log) and `layout`.
Every answer carries an `etag`; with `--since <etag>` the answer is only `{"unchanged": true, "etag": …}`
until something it covers changes, so a client can ask every few seconds. Through a server it is one
request.

`task claim` says which machine and task work on a task note, in its `kebbie` frontmatter key
(`machine`, `task`, `repo`, `since` in UTC), so a second machine on the same vault sees the note is
taken. It changes only that key, as a conditional write against the version it read, and reads again
when the note changed in between. A note another machine holds is left as it is and answered with
`"claimed": false` and that machine's claim; `--force` takes it over and returns the old claim as
`replaced`. `task release` removes the key; with `--machine` it leaves another machine's claim in place
and answers `"released": false`. Without a path, `--machine` and `--task` clear that task's claims
wherever its notes are, after a move or a rename; `"paths": []` means the note is gone. `close` and a
status change to `backlog` remove the claim too, and `write_file` keeps the stored claim whatever its
content says, so only these commands change it. `context` shows it on the note's row.

`task check` takes the keys `problem`, `solution`, `decisions`, `tests` and `scope`. A section is filled
when it has any text under its heading, before the next heading of the same or a higher level. The
headings are a schema setting, per language, picked by the vault's `language`:

```yaml
task:
  sections:
    en: { problem: Problem, solution: Solution, decisions: Decisions, tests: Test decisions, scope: Out of scope }
    sv: { problem: Problem, solution: Lösning, decisions: Beslut, tests: Testbeslut, scope: Utanför ramen }
```

`status` never fails: with no vault configured it prints `"configured": false, "backend": null` and
exits 0. Through a server it answers `"backend": "server"` with the `server` and whether this machine is
`signed_in`. It is read-only and leaves `writable` null; `--probe` measures it by writing and deleting
`.vault/write-probe`.

These commands read the storage settings from the environment first. When the environment names no
vault, they take the Claude Code plugin's saved user config instead: `pluginConfigs["vault@<marketplace>"].options`
in `$CLAUDE_CONFIG_DIR/settings.json` (default `~/.claude/settings.json`), mapping `vault_path`,
`s3_endpoint`, `s3_bucket`, `s3_access_key`, `s3_secret_key`, `s3_prefix`, `s3_region` and
`vault_server_url` to their variables, with the secrets from Claude Code's credentials store when they are kept there.

## Configuration

Every variable is also read from `CLAUDE_PLUGIN_OPTION_<NAME>`; the bare name wins when both are set.

### Storage and search

| Variable | Required | Meaning |
| --- | --- | --- |
| `VAULT_PATH` | for a local vault | Directory holding the vault. On a server, the folder holding every vault. |
| `S3_ENDPOINT` | for an S3 vault | Endpoint URL, e.g. `https://minio.example.com`. |
| `S3_ACCESS_KEY` | for an S3 vault | Access key. |
| `S3_SECRET_KEY` | for an S3 vault | Secret key. |
| `S3_BUCKET` | for an S3 vault | Bucket holding the vault. |
| `S3_PREFIX` | no | Key prefix inside the bucket. |
| `S3_REGION` | no | Region, default `us-east-1`. |
| `VAULT_SERVER_URL` | for a vault server | The server a machine [signed in to](#sign-a-machine-in); the stdio server, the hooks and the commands work through it. Wins over `VAULT_PATH` and `S3_*`. |
| `VAULT_CACHE_DIR` | no | Where the index, the model and the shared embedding process's socket live, default `~/.cache/notes-vault-mcp`. |
| `VAULT_SCHEMA` | no | Local path to a schema file, overriding the one in the vault. |
| `VAULT_SEMANTIC` | no | `on` (or `1`, `true`, `yes`) adds [search by meaning](#search-by-meaning); needs the `semantic` extra. |
| `VAULT_SEMANTIC_MODEL` | no | The embedding model, default `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`. |
| `VAULT_EMBED_BATCH` | no | Sections the shared embedding process runs through the model at once, default `16`. Fewer lowers its peak memory during a first sync. |
| `VAULT_EMBED_THREADS` | no | Threads the model runs on, default half the cores the process may use, at most `4`, counting a container's CPU limit (cgroup v2 `cpu.max`); ONNX Runtime on its own takes every physical core of the node. |
| `VAULT_EMBED_MAX_TOKENS` | no | Tokens of a section the model reads, default `256`. Prose sections stay under it; a section of links or code is cut there instead of padding its batch to 512. |
| `VAULT_STOP_HOOK` | no | `off` disables the stop hook. |
| `VAULT_STOP_HOOK_COMMITS` | no | `off` turns off the stop hook's checks for unlogged commits and stale notes, and keeps the message check. |
| `VAULT_STOP_HOOK_PROMPTS` | no | How many messages you write after the agent's last vault write before the stop hook asks it to save them, default `3`; `off` turns that check off. |

### Server

`serve --transport http` takes `--host` (default `127.0.0.1`) and `--port` (default `8765`). It loads
the extensions installed beside it, from the Python entry point group `notes_vault_mcp.extensions`.

| Variable | Meaning |
| --- | --- |
| `VAULT_PUBLIC_URL` | Required. The address clients reach, path included when the server is not at the root. Must be `https://`, or `http://localhost` for a local trial. |
| `VAULT_HOSTED_MASTER_KEY` | Required. Base64 of 32 bytes that wraps every vault's data key; `master-key generate` makes one. |
| `VAULT_HOSTED_MASTER_KEY_PREVIOUS` | Older master keys, space or comma separated, still accepted while `master-key rewrap` moves the vault keys off them. |
| `VAULT_REGISTRATION` | `invite` (default): only invited addresses get an account. `open`: any verified email does. |
| `VAULT_PASSKEYS` | `on` (default) or `off`. |
| `VAULT_SMTP_HOST` | Turns on email links. Needs `VAULT_SMTP_FROM`. |
| `VAULT_SMTP_FROM` | The sender, e.g. `Vault <vault@example.com>`. |
| `VAULT_SMTP_USERNAME`, `VAULT_SMTP_PASSWORD` | The SMTP login. |
| `VAULT_SMTP_SECURITY` | `starttls` (default), `ssl` or `none`. |
| `VAULT_SMTP_PORT` | Default 587, 465 or 25 by `VAULT_SMTP_SECURITY`. |
| `VAULT_GITHUB_CLIENT_ID`, `VAULT_GITHUB_CLIENT_SECRET` | Turn on GitHub sign-in. |
| `VAULT_OIDC_ISSUER` | Turns on your identity provider. Needs `VAULT_OIDC_CLIENT_ID` and `VAULT_OIDC_CLIENT_SECRET`. |
| `VAULT_OIDC_NAME` | The provider's button label. Default `your organisation`. |
| `VAULT_OIDC_SCOPES` | Scopes asked of the provider, space separated. Default `openid email profile`. |
| `VAULT_IDENTITY_PUBLIC_KEY` | Turns on a trusted proxy: its PEM public key (RS256 or ES256), raw or base64 on one line. Needs `VAULT_IDENTITY_ISSUER`. |
| `VAULT_IDENTITY_ISSUER` | The `iss` in the proxy's tokens. |
| `VAULT_IDENTITY_HEADER` | The header carrying the token. Default `X-Forwarded-Identity`. |
| `VAULT_IDENTITY_TYP` | When set, the token's `typ` must match. |
| `VAULT_IDENTITY_WRITE_CLAIMS` | Entitlements that grant read and write, space separated. Default `vault:write`. |
| `VAULT_IDENTITY_READ_CLAIMS` | Entitlements that grant read only. Default `vault:read`. A token with neither is refused. |
| `VAULT_IDENTITY_NAME` | The proxy's button label. Default `your company's sign-in`. |
| `VAULT_ALLOWED_HOSTS` | Extra `Host` header values the server answers on, space or comma separated. |
| `VAULT_AUTH_DIR` | Where `hosted.sqlite` lives. Default `~/.cache/notes-vault-mcp/auth`. |
| `VAULT_SUBJECT_PREFIX` | Where the vaults live under the bucket or folder: `<prefix>/<vault id>/`, or `<prefix>/<account id>/` for an account's original vault. Default `users`. |
| `IMPORT_S3_ACCESS_KEY`, `IMPORT_S3_SECRET_KEY` | The keys `accounts import --from-s3-bucket` reads the old bucket with. |

## Deploy

Both examples run the server image with an S3 vault and one sign-in method filled in. For a folder,
drop the `S3_*` settings, set `VAULT_PATH=/vault` and mount the folder there. The container image sets
`VAULT_AUTH_DIR=/data/auth` and `VAULT_CACHE_DIR=/data/cache` and declares `/data` a volume; back up
`/data/auth` together with the master key.

### Caddy

`deploy/caddy/` runs the server behind Caddy, which fetches the certificate for `VAULT_HOST`.

```sh
cd deploy/caddy
cp .env.example .env
$EDITOR .env
echo "VAULT_HOSTED_MASTER_KEY=$(docker compose run --rm --no-deps vault master-key generate)" >> .env
docker compose up -d
docker compose exec vault notes-vault-mcp accounts invite you@example.com
```

`.env.example` lists the email, GitHub and identity-provider settings; fill in at least one. Then open
`https://VAULT_HOST/account` to sign in, and add the connector in claude.ai with the `/mcp` URL.

### Kubernetes

`deploy/k8s/` holds plain manifests: a Deployment with a PVC mounted at `/data`, a Service, an
Ingress and an example Secret.

```sh
cp deploy/k8s/secret.example.yaml secret.yaml
$EDITOR secret.yaml                              # add VAULT_HOSTED_MASTER_KEY from master-key generate
$EDITOR deploy/k8s/ingress.yaml
kubectl apply -f secret.yaml -f deploy/k8s/deployment.yaml -f deploy/k8s/service.yaml -f deploy/k8s/ingress.yaml
kubectl exec deploy/notes-vault-mcp -- notes-vault-mcp accounts invite you@example.com
```

The Deployment runs one replica, as a sealed vault needs. The probes hit
`/.well-known/oauth-protected-resource/mcp`, which answers without a token; when `VAULT_PUBLIC_URL`
carries a path, that path sits in the probe URL too. Fill in `ingressClassName` and point `secretName`
at a TLS certificate.

## License

MIT. See [LICENSE](LICENSE).
