<!-- mcp-name: io.github.gronare/notes-vault-mcp -->

# notes-vault-mcp

Memory for coding agents, kept as a folder of markdown notes you own, with a schema that holds and a tie to your git history.

The vault is an Obsidian-style folder of `.md` files with YAML frontmatter, on disk or in an S3 bucket.
This server indexes it and gives any MCP client (Claude Code, Codex, Cursor, claude.ai, Claude Desktop
and mobile) a cheap way to search it, read it and write it. Writes are checked against a schema the
vault carries. Notes can point at code, and at the start of a session the agent gets the commits made
since each relevant note was last updated, so a stale note is caught before it is believed.

## Why this one

- **The notes are yours.** Plain markdown in a folder or a bucket, open in Obsidian, in git, or in `cat`. No database to export from.
- **A schema with teeth.** Required fields, legal statuses and kinds, folders that must link an area, a tag vocabulary. A write that breaks the contract is refused, not stored.
- **A lifecycle, not a pile.** `close` archives finished work, `backlog_add` files what was deferred, `log_append` writes one line per repo per session, `lint` reports drift and can park stale tasks. A weekly `lint` keeps `active` meaning active.
- **Memory tied to code.** A note names the paths it describes; the session-start hook prints the commits newer than the note, and the stop hook refuses to end a session that left its commits unlogged.
- **Search that reads your languages.** Stemming for every language Snowball knows, folded diacritics, synonyms from the schema, and optional search by meaning that runs on your machine with one model process shared by every session.
- **Your own remote connector.** Serve it over HTTPS with the built-in OAuth login or your OpenID Connect provider, and claude.ai, Claude Desktop and the mobile app connect to it. Self-hosted, with a read-only mode.

[kebbie](https://kebbie.dev) uses it for each task's note.

## Quick start

A local folder and Claude Code:

```sh
claude mcp add vault -s user -e VAULT_PATH=$HOME/vault -- uvx notes-vault-mcp
VAULT_PATH=$HOME/vault uvx notes-vault-mcp init
```

`init` writes `.vault/schema.yml` and five Obsidian Bases views into the vault, keeps anything that
exists (`--force` overwrites), and prints a CLAUDE.md snippet with the rules an agent follows on its
side: `context` at session start, a note before the first edit, `log_append` at the end. Paste it into
your CLAUDE.md.

The [Claude Code plugin](#claude-code) does the same and adds the two hooks and the skills. For a
vault in S3 or MinIO, for other clients and for claude.ai, see [Connect it](#connect-it).

- [How it works](#how-it-works)
- [Tools](#tools)
- [Connect it](#connect-it)
- [Search by meaning](#search-by-meaning)
- [Command line](#command-line)
- [Configuration](#configuration)
- [Deploy](#deploy)
- [Development](#development)

## How it works

### Notes and folders

A note is a markdown file with frontmatter. The default schema lays out five folders, and the folder
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
  optional: [kind, area, summary, path, superseded_by, priority, source, kelpie]
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

### The schema

`.vault/schema.yml` is deep-merged over the built-in default, so it only needs to carry what differs.
A vault adds a kind by adding an entry (`research: "reading notes on a topic; no task, no decision"`),
rewrites a rule by restating it, and drops a kind by setting it to `null`. `VAULT_SCHEMA` points at a
local file that overrides the one in the vault.

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
note claimed through `task claim` within the last 14 days is left alone. Run it weekly.

## Tools

| Tool | Cost | What it does |
| --- | --- | --- |
| `search` | cheap | Full-text and, when on, meaning search over the index. Filters: `folder`, `status`, `tag`, `kind`, `area`, `path_prefix`, `since`, `include_archive`, `include_superseded`, `limit`. |
| `context` | cheap | The session-start bundle for a `path` and `repo`. |
| `backlog` | cheap | The backlog by priority then age, filtered by `area` and `priority`. |
| `list_files` | cheap | Paths only. |
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
| `init` | write | Writes the schema and the Bases views into the vault root and keeps what exists; `force` overwrites. |
| `obsidian_access` | write | `--auth forwarded` only. Mints the WebDAV token for Obsidian's Remotely Save and shows it once. |

Over HTTP with OAuth, a token carries `vault:read` or `vault:read vault:write`. A write tool called
with a read-only token fails with an error saying so, rather than half-writing.

## Connect it

Set `VAULT_PATH` for a folder, or `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` and `S3_BUCKET` for a
bucket (MinIO included). With neither, the server exits with one line saying so.

### Claude Code

The plugin installs the server, the two hooks and the skills, and asks for the vault settings:

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
under `triage` at the next start instead. `VAULT_STOP_HOOK=off` silences it, which a tool that drives
agent sessions and writes the log itself sets in the sessions it starts.

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

### Remote: claude.ai, Claude Desktop and mobile

Run the server over HTTPS with `--auth builtin` or `--auth oidc` and claude.ai adds it as a custom
connector. Connect it once on the web and the same connector appears in Claude Desktop and in the
mobile app. One server instance serves one vault.

```sh
notes-vault-mcp serve --transport http --host 0.0.0.0 --port 8765 --auth builtin
```

Streamable HTTP on `/mcp`, with four ways to authenticate:

| `--auth` | Who logs in | Client registration | Read-only clients | State on disk |
| --- | --- | --- | --- | --- |
| `builtin` | one owner, against a password this server holds | dynamic, nothing to configure in claude.ai | two checkboxes on the login page | `auth.sqlite` under `VAULT_AUTH_DIR` |
| `oidc` | anyone your OpenID Connect provider admits | dynamic when the provider supports it, otherwise a client id and secret pasted into claude.ai | group membership | none |
| `bearer` | anything carrying `Authorization: Bearer $VAULT_TOKEN` | none | no | none |
| `forwarded` | whoever a trusted proxy identifies in a signed header | the proxy's | by entitlement | `personal.sqlite` under `VAULT_AUTH_DIR` |

`bearer` is the default and the LAN mode: one token with full access, refused without `VAULT_TOKEN`,
and claude.ai cannot use it. `forwarded` serves one vault per person behind a proxy that did the OAuth;
see [Behind a proxy](#behind-a-proxy-one-vault-per-person).

You need a domain pointing at the machine and TLS in front of the server. The examples under `deploy/`
put Caddy there, which fetches the certificate itself. `VAULT_PUBLIC_URL` is the https address clients
reach, path included when the server is not at the root; the MCP endpoint is that plus `/mcp`, and that
is the URL you paste into claude.ai under Settings, Connectors, Add custom connector.

**Built-in login.** The browser lands on the server's login page, which asks for the owner password and
shows two checkboxes, read and write. Clear write to hand out a read-only connector. Set the password
once with `notes-vault-mcp owner set-password`; `tokens list` shows what is connected and
`tokens revoke <client_id>` disconnects one. The password hash and the tokens live in `auth.sqlite`.
Back it up; losing it means every client has to connect again.

**OIDC.** The server is a resource server: it validates the provider's access tokens and never sees a
password. A user in `VAULT_OIDC_WRITE_GROUP` gets `vault:read` and `vault:write`, a user in
`VAULT_OIDC_READ_GROUP` only `vault:read`, and anyone in neither is refused. When the access token
carries no `groups`, the server asks the provider's userinfo endpoint. With a provider that supports
dynamic client registration you are done after adding the connector. With one that does not (Pocket ID,
Authentik), register the client in the provider first and paste its client id and secret under the
connector's Advanced settings; the redirect URI to allow is the one claude.ai shows in that dialog.

Pocket ID in particular only accepts an OAuth `resource` it knows as an API, and claude.ai sends the
`/mcp` URL as that resource:

1. Settings, APIs, Add API: a name and the resource identifier, exactly `VAULT_PUBLIC_URL` + `/mcp`,
   with two permissions keyed `vault:read` and `vault:write`.
2. Settings, User Groups: create `vault` (read) and `vault-writers` (write) and put users in them.
3. Settings, OIDC Clients, Add: name `claude.ai`, callback URL `https://claude.ai/api/mcp/auth_callback`,
   PKCE on, public client off, allowed user groups `vault`. Under API access, grant the API with both
   permissions (user-delegated). Copy the client id and secret.
4. On the server: `VAULT_OIDC_ISSUER` is Pocket ID's base URL, `VAULT_OIDC_AUDIENCE` is the resource
   identifier from step 1, and `VAULT_OIDC_SCOPES` is `openid profile email groups vault:read vault:write`.
5. In claude.ai, add the connector with the client id and secret under Advanced settings.

The user's groups decide what a token may do; the client's permissions only matter when the provider
reports no groups at all.

**The `Host` header.** Bound to localhost, the server answers `localhost`, `127.0.0.1` and `[::1]` and
gives anything else `421`. With `VAULT_PUBLIC_URL` set, its hostname and localhost are answered and
the rest get `421`; a Service name or a LAN hostname next to the public one goes in
`VAULT_ALLOWED_HOSTS`. Bound to another address with neither set, every `Host` is answered. In
`forwarded` mode the header is not checked.

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
`VAULT_SEMANTIC=on`: `ghcr.io/gronare/notes-vault-mcp:v0.9.0-semantic`. The plain tag leaves it out, and
every client of one server shares its model and index.

It runs on your machine. The model, `paraphrase-multilingual-mpnet-base-v2` run through fastembed and
ONNX, is downloaded once from Hugging Face into `VAULT_CACHE_DIR/models`. No API key, and no note leaves
the machine. Each note is split at its headings, each section gets a vector, and the vectors live in the
same SQLite index: computed for the notes whose version changed, dropped with the note, and computed
again for every note when the model changes.

The model runs in one background process per machine, shared by every server and command that uses the
same `VAULT_CACHE_DIR`, however many sessions are open. The first one that needs it starts it; it keeps
running when that session ends, and exits by itself once nobody has asked it anything for five minutes.
The next search starts it again. If it crashes, the next search starts a new one. A session that never
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

A server embeds in the background and searches by words meanwhile; a query that would wait more than
three seconds for the shared process, because it is still starting or busy with a long embed, is answered
by words. The first query after the process exited waits under a second for it to start again.

With `VAULT_SEMANTIC=on` and the extra missing, the server logs one line saying so and searches by
words. `notes-vault-mcp sync` embeds whatever is pending and says how many notes carry vectors;
`sync --rebuild` computes them all again.

## Command line

```sh
notes-vault-mcp serve --transport stdio          # the default, also with no subcommand
notes-vault-mcp serve --transport http --host 0.0.0.0 --port 8765 --auth builtin
notes-vault-mcp init [--force]
notes-vault-mcp sync [--rebuild]                 # refresh the index; --rebuild reads every note again
notes-vault-mcp search "bokning" --limit 5       # the same ranking, from a shell
notes-vault-mcp lint [--write Log/lint.md] [--park-stale]
notes-vault-mcp backlog --area myapp --priority high
notes-vault-mcp changelog myapp 2026-08 --repo-path ~/projects/myapp [--write]
notes-vault-mcp changelog --all                  # this month, and last month during its first week
notes-vault-mcp hook session-start | stop        # the Claude Code hooks, JSON on stdin
notes-vault-mcp owner set-password [--password ...]
notes-vault-mcp tokens list | revoke <client_id>
notes-vault-mcp status [--probe]
```

`changelog` prints the log lines, the git commits grouped by day, and the repo's notes dated inside
the period (`YYYY` or `YYYY-MM`). With `--write` it keeps that as a period page, `Log/<repo>-<period>.md`,
between the markers `<!-- changelog:generated -->` and `<!-- /changelog:generated -->`; prose above the
markers (a summary written at month end) is left alone, and the page's status follows the calendar.
`--all` does it for every repo the session-start hook has seen on this machine, and the stop hook runs
that once a day, so the pages stay current without a cron.

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
notes-vault-mcp status
# {"configured": true, "version": "0.9.0", "backend": "s3", "writable": null}
notes-vault-mcp status --probe
# {"configured": true, "version": "0.9.0", "backend": "s3", "writable": true}
```

The repo is the main checkout's name, also from a worktree, and the area is the system note the
session-start hook would load for that directory, falling back to the repo name (`"area": null` when no
note matched). `task open` writes a task note with the task name as a placeholder title, status
`active`, `path` set to the main checkout, `source: branch <name>`, and the section headings with empty
bodies; the stem is the repo plus the task name, made unique across the whole vault. `--note` claims
an existing task note instead: status `active`, nothing else changed. `--branch` defaults to the branch
checked out in `--cwd`.

`task claim` says which machine and task work on a task note, in its `kelpie` frontmatter key
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
exits 0. It is read-only and leaves `writable` null; `--probe` measures it by writing and deleting
`.vault/write-probe`.

These commands read the storage settings from the environment first. When the environment names no
vault, they take the Claude Code plugin's saved user config instead: `pluginConfigs["vault@<marketplace>"].options`
in `$CLAUDE_CONFIG_DIR/settings.json` (default `~/.claude/settings.json`), mapping `vault_path`,
`s3_endpoint`, `s3_bucket`, `s3_access_key`, `s3_secret_key`, `s3_prefix` and `s3_region` to their
variables, with the secrets from Claude Code's credentials store when they are kept there.

## Configuration

Every variable is also read from `CLAUDE_PLUGIN_OPTION_<NAME>`; the bare name wins when both are set.

### Storage and search

| Variable | Required | Meaning |
| --- | --- | --- |
| `VAULT_PATH` | for a local vault | Directory holding the vault. |
| `S3_ENDPOINT` | for an S3 vault | Endpoint URL, e.g. `https://minio.example.com`. |
| `S3_ACCESS_KEY` | for an S3 vault | Access key. |
| `S3_SECRET_KEY` | for an S3 vault | Secret key. |
| `S3_BUCKET` | for an S3 vault | Bucket holding the vault. |
| `S3_PREFIX` | no | Key prefix inside the bucket. |
| `S3_REGION` | no | Region, default `us-east-1`. |
| `VAULT_CACHE_DIR` | no | Where the index, the model and the shared embedding process's socket live, default `~/.cache/notes-vault-mcp`. |
| `VAULT_SCHEMA` | no | Local path to a schema file, overriding the one in the vault. |
| `VAULT_SEMANTIC` | no | `on` (or `1`, `true`, `yes`) adds [search by meaning](#search-by-meaning); needs the `semantic` extra. |
| `VAULT_SEMANTIC_MODEL` | no | The embedding model, default `sentence-transformers/paraphrase-multilingual-mpnet-base-v2`. |
| `VAULT_STOP_HOOK` | no | `off` disables the stop hook. |

### HTTP and authentication

`serve --transport http` takes `--host` (default `127.0.0.1`), `--port` (default `8765`) and `--auth`
(`bearer`, the default, `oidc`, `builtin` or `forwarded`).

| Variable | Mode | Meaning |
| --- | --- | --- |
| `VAULT_TOKEN` | bearer | Required. The static token every request must carry. |
| `VAULT_PUBLIC_URL` | oidc, builtin, forwarded | Required. The address clients reach, path included when the server is not at the root. Must be `https://`, or `http://localhost` for a local trial. Optional in bearer mode, where it only widens the `Host` check. |
| `VAULT_ALLOWED_HOSTS` | all | Extra `Host` header values the server answers on, space or comma separated. |
| `VAULT_AUTH_DIR` | builtin, forwarded | Where `auth.sqlite` (the owner's password hash, the registered clients, the hashed tokens) or `personal.sqlite` (the hashed WebDAV tokens) lives. Default `~/.cache/notes-vault-mcp/auth`. |
| `VAULT_OIDC_ISSUER` | oidc | Required. The provider's issuer URL. |
| `VAULT_OIDC_AUDIENCE` | oidc | Required. The value the provider puts in `aud`; a token without it, or for another audience, is refused. |
| `VAULT_OIDC_READ_GROUP` | oidc | Group granting `vault:read`. Default `vault`. |
| `VAULT_OIDC_WRITE_GROUP` | oidc | Group granting `vault:read` and `vault:write`. Default `vault-writers`. |
| `VAULT_OIDC_SCOPES` | oidc | Scopes the resource metadata advertises, space separated, so the client asks the provider for scopes it knows. Default `openid profile email groups`. |
| `VAULT_IDENTITY_PUBLIC_KEY` | forwarded | Required. The proxy's PEM public key (RS256 or ES256), raw or base64 on one line. |
| `VAULT_IDENTITY_ISSUER` | forwarded | Required. The `iss` in the proxy's tokens. |
| `VAULT_IDENTITY_HEADER` | forwarded | The header carrying the token. Default `X-Forwarded-Identity`. |
| `VAULT_IDENTITY_TYP` | forwarded | When set, the token's `typ` must match. |
| `VAULT_IDENTITY_WRITE_CLAIMS` | forwarded | Entitlement names (in the token's `entitlements` list or `scope`) that grant `vault:read` and `vault:write`, space separated. Default `vault:write`. |
| `VAULT_IDENTITY_READ_CLAIMS` | forwarded | Entitlement names that grant `vault:read` only. Default `vault:read`. A token with neither gets `403 insufficient_scope`. |
| `VAULT_SUBJECT_PREFIX` | forwarded | Where the per-person vaults live under the bucket or folder: `<prefix>/<sub>/`. Default `users`. |

## Deploy

### Caddy with the built-in login

```sh
cd deploy/caddy-builtin
cp .env.example .env
$EDITOR .env
docker compose up -d
docker compose exec vault notes-vault-mcp owner set-password
```

Then add the connector in claude.ai with the `/mcp` URL. `docker compose exec vault notes-vault-mcp
tokens list` shows what is connected. `auth.sqlite` lives in the `vault-data` volume; back it up.

### Caddy with OIDC

```sh
cd deploy/caddy-oidc
cp .env.example .env
$EDITOR .env
docker compose up -d
```

Both `.env.example` files take an S3 vault; for a folder, drop the `S3_*` lines, set `VAULT_PATH=/vault`
and bind-mount the folder there.

### Kubernetes

`deploy/k8s/` holds plain manifests: a Deployment with a PVC mounted at `/data`, a Service, an
Ingress and an example Secret.

```sh
cp deploy/k8s/secret.example.yaml secret.yaml
$EDITOR secret.yaml
$EDITOR deploy/k8s/ingress.yaml
kubectl apply -f secret.yaml -f deploy/k8s/deployment.yaml -f deploy/k8s/service.yaml -f deploy/k8s/ingress.yaml
kubectl exec -it deploy/notes-vault-mcp -- notes-vault-mcp owner set-password
```

The Deployment runs `--auth builtin`; for OIDC change the `--auth` argument and fill the
`VAULT_OIDC_*` keys in the Secret. The probes hit `/.well-known/oauth-protected-resource/mcp`, the one
route that answers without a token in both modes; when `VAULT_PUBLIC_URL` carries a path, that path
sits in the probe URL too. Fill in `ingressClassName` and point `secretName` at a TLS certificate.

The container image sets `VAULT_AUTH_DIR=/data/auth` and `VAULT_CACHE_DIR=/data/cache` and declares
`/data` a volume.

### Behind a proxy: one vault per person

`--auth forwarded` is for an organisation that already runs an OAuth authorization server in front
of its MCP servers and forwards the caller's identity as a signed token in a header (the pattern
Cloudflare Access uses with `Cf-Access-Jwt-Assertion`). The server verifies that token with the
proxy's public key, takes the subject from it, and serves that subject's own vault: a prefix per
person in one bucket (or a folder per person under `VAULT_PATH`), with its own index, its own
`.vault/schema.yml` and a welcome note, all created on the first request. Granting access at the proxy
is the whole onboarding.

```sh
notes-vault-mcp serve --transport http --host 0.0.0.0 --port 8765 --auth forwarded
```

The contract with the proxy: a request without a valid identity gets `403` with no
`WWW-Authenticate` challenge, the transport is stateless with plain JSON responses so a buffering
proxy needs no `Mcp-Session-Id`, the `Host` header is not checked, and `/up` answers `ok` without a
token for readiness probes. The OAuth metadata routes are not served; the proxy owns them. The token
needs `iss`, `aud` (the hostname of `VAULT_PUBLIC_URL`), `sub` and `exp`; `email` and `name` are used
when present. Subjects that are not plain identifiers are hashed before they name a folder.
`VAULT_CACHE_DIR` holds one index per subject; give it a volume.

Each vault is also served over WebDAV at `VAULT_PUBLIC_URL/dav/`, for Obsidian's Remotely Save
plugin, which cannot do OAuth. The person asks Claude to run `obsidian_access`; the server mints a
personal token, returns it once and stores only its hash. Remotely Save gets the `/dav/` address, the
email as username and that token as the password. Running `obsidian_access` again rotates the token.
The proxy must pass `/dav/*` straight through, with the client's `Authorization` header and the WebDAV
verbs and headers (`PROPFIND`, `MKCOL`, `MOVE`, `COPY`, `Depth`, `Destination`, `Overwrite`) intact.
The welcome note written on the first request carries these instructions for the person.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```

The suite runs against a fixture vault under `tests/fixtures/vault/` and a moto-mocked S3 bucket; it
never touches a real bucket. Search by meaning is tested with a fake model by default.
`VAULT_TEST_SEMANTIC_MODEL=1 uv run --extra semantic pytest tests/test_semantic_model.py` runs it
against the real model, which it downloads first; `VAULT_TEST_MODEL_CACHE` points it at a model folder
already on disk.

## License

MIT. See [LICENSE](LICENSE).
