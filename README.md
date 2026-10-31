<!-- mcp-name: io.github.gronare/notes-vault-mcp -->

# notes-vault-mcp

An MCP server for a vault of markdown notes — the kind Obsidian keeps: a folder of `.md` files with
YAML frontmatter. The vault lives either in a local directory or in an S3 bucket (MinIO included),
and the server gives an agent a cheap, indexed way to read and write it.

The point is that an agent should be able to answer "what do we already know about this?" in one
call, and should be told when the vault has drifted away from the code. So the server does more than
read and write files:

- **A local SQLite index.** Every tool call refreshes it, fetching only the notes whose version
  changed. Search never downloads the vault.
- **Full-text search with BM25 ranking**, folder weights, recency decay and a status factor, so the
  living system note outranks a two-year-old archived plan on the same words.
- **A schema.** The frontmatter contract lives in the vault as `.vault/schema.yml`: which folders
  exist and what each is for, which fields are required, which statuses and kinds are legal, which
  folders must link an `area`. Writes are validated against it and refused when they do not hold.
- **A lifecycle.** `close` archives a finished note and stamps its status; `log_append` writes one
  dated line per repo per session; `lint` reports every kind of drift it can see.
- **Session hooks** for Claude Code: `session-start` hands the agent the system notes for the repo it
  is about to touch — plus the commits made since each note was last updated — and `stop` refuses to
  end a session that left commits unlogged or notes stale.

Swedish or English notes both work: the index folds diacritics, stems every word in the vault's
languages so `äppelpressar` finds `äppelpressen` and `pressing` finds `presses`, and the schema carries a
synonym list so `bokning` finds `booking`. Turn on [search by meaning](#search-by-meaning) and `inloggning`
also finds the note about the OAuth flow.

## Install

### As a Claude Code plugin

```sh
claude plugin marketplace add https://github.com/gronare/claude-plugins
claude plugin install vault@gronare
```

The plugin asks for the vault settings and passes them as `CLAUDE_PLUGIN_OPTION_*` environment
variables, which this server reads as if they were the bare names.

### As an MCP server, straight from PyPI

```sh
claude mcp add vault -s user \
  -e VAULT_PATH=$HOME/vault \
  -- uvx notes-vault-mcp
```

Or against S3 / MinIO:

```sh
claude mcp add vault -s user \
  -e S3_ENDPOINT=https://minio.example.com \
  -e S3_ACCESS_KEY=... \
  -e S3_SECRET_KEY=... \
  -e S3_BUCKET=vault \
  -- uvx notes-vault-mcp
```

### As a container

```sh
claude mcp add vault -s user -- \
  docker run --rm -i \
  -e S3_ENDPOINT -e S3_ACCESS_KEY -e S3_SECRET_KEY -e S3_BUCKET \
  ghcr.io/gronare/notes-vault-mcp:latest
```

## Configuration

Every variable is also read from `CLAUDE_PLUGIN_OPTION_<NAME>`, which is how the Claude Code plugin
passes its user config. The bare name wins when both are set.

| Variable | Required | Meaning |
| --- | --- | --- |
| `VAULT_PATH` | for a local vault | Directory holding the vault. Selects the local backend. |
| `S3_ENDPOINT` | for an S3 vault | Endpoint URL, e.g. `https://minio.example.com`. |
| `S3_ACCESS_KEY` | for an S3 vault | Access key. |
| `S3_SECRET_KEY` | for an S3 vault | Secret key. |
| `S3_BUCKET` | for an S3 vault | Bucket holding the vault. |
| `S3_PREFIX` | no | Key prefix inside the bucket. |
| `S3_REGION` | no | Region, default `us-east-1`. |
| `VAULT_CACHE_DIR` | no | Where the index lives, default `~/.cache/notes-vault-mcp`. |
| `VAULT_SCHEMA` | no | Local path to a schema file, overriding the one in the vault. |
| `VAULT_TOKEN` | for `--auth bearer` | Static bearer token for the LAN mode of `--transport http`. |
| `VAULT_ALLOWED_HOSTS` | no | Extra `Host` header values the server answers on, space or comma separated. |
| `VAULT_STOP_HOOK` | no | `off` disables the stop hook. |
| `VAULT_SEMANTIC` | no | `on` adds [search by meaning](#search-by-meaning); needs the `semantic` extra. |

Set `VAULT_PATH` **or** the four `S3_*` variables. With neither, the server exits with one line
saying so.

## First run

```sh
uvx notes-vault-mcp init
```

`init` writes into the vault, and refuses to overwrite anything without `--force`:

- `.vault/schema.yml` — the frontmatter contract, copied from the built-in default so you can edit it.
- `Areas.base`, `Open tasks.base`, `Resources.base`, `Backlog.base`, `Log.base` — Obsidian Bases views over the same structure.

It then prints a CLAUDE.md snippet to stdout: the workflow rules an agent needs on its side of the
conversation.

## The schema

`.vault/schema.yml` is deep-merged over the built-in default, so it only needs to carry what differs.
The default lays out five folders:

| Folder | Kind | Weight | Role |
| --- | --- | --- | --- |
| `Areas/` | system | 3.0 | One living note per system. Current state only. The hubs of the graph. |
| `Resources/` | reference | 2.0 | Traps, how-tos and decisions with their reasons. |
| `Projects/` | task | 1.0 | Open work spanning sessions. Closed with `close`. |
| `Log/` | log | 1.0 | Append-only log per repo, one note per repo. |
| `Archive/` | archive | 0.3 | History. Searched only on request. |

and the contract for a note:

```yaml
frontmatter:
  required: [title, date, updated, tags, status]
  optional: [kind, area, summary, path, superseded_by, priority, source, kelpie]
  area_required_in: [Projects, Resources, Log]
  status_values: [draft, active, complete, superseded]
  kind_values: [system, task, trap, howto, decision, reference, log]
```

Each kind carries a one-line rule saying when a note is that kind, and the server puts the rules in its
instructions so every client picks kinds the same way:

```yaml
kinds:
  trap: "a gotcha that bit once and will bite again; nothing to do, nothing was chosen"
  decision: "a choice and its reasons; something was chosen, alternatives were dropped"
  howto: "steps that are known to work; nothing chosen, nothing open"
```

A vault adds a kind by adding an entry (`research: "reading notes on a topic; no task, no decision"`),
rewrites a rule by restating it, and drops a kind by setting it to `null`. `kind_values` alone, without
rules, still works.

`path` is what ties a note to code: a comma-separated list of directories (`~` is kept as written and
also indexed expanded). That is what `context` and the session hook match against.

The repo log is one note per repo, and both its filename and its line format are schema settings:

```yaml
log:
  folder: Log
  file_format: "{repo}-log.md"
  entry_format: "- [{date}] {line} | commits: {commits} | {area}"
```

`file_format` takes a single `{repo}` placeholder, and the default suffix is what keeps the log clear
of the hub note: with `Areas/myapp.md` and `Log/myapp.md` both in the vault, Obsidian cannot
resolve `[[myapp]]`. Every place that builds the log path reads this setting — `log_append`, the
log tail in `context`, the stop hook's unlogged-commit check, `changelog` and `lint` — so changing it
moves all of them at once. Rename the existing files to match when you change it.

`language` is the vault's primary language and `other_languages` the rest, as ISO codes
(`language: sv`, `other_languages: [en]`); search stems in every one Snowball supports, and changing
them rebuilds the index. When nothing matches, the reply tells the client which languages the vault
is written in so it can translate the terms and search again; a partial result says the same in one
line. A stemmed-only match weighs `stemmed_weight` (0.5 by default, below the body's 1.0), so the exact
form ranks first.

Also configurable: the tag vocabulary and whether it is enforced, the synonym groups search expands,
`stale_after_days`, and the search weights.

## Tools

Every call refreshes the index first, throttled to at most once every 20 seconds.

| Tool | Cost | What it does |
| --- | --- | --- |
| `search` | cheap | Full-text over the index. File stem, title, summary, tags and body, with synonyms, prefixes, stemming, quoted phrases and folded diacritics. A query that is exactly a note's stem (or `[[stem]]`) puts that note first, even from the archive. Every term is required; when no note has them all, it returns the notes matching the most terms and the header says the result is partial. A bare commit sha finds the notes that mention it. Hides archive and superseded notes and says how many. |
| `context` | cheap | The session-start call: the system notes covering a path, the open tasks, a triage list of open notes older than `stale_after_days` with the three ways to settle each, the backlog, the reference notes and the tail of the repo log, in one answer. |
| `list_files` | cheap | Paths only. |
| `read_file` | moderate | One note, prefixed with `etag: <version>`. A superseded note carries a warning callout. |
| `obsidian_access` | write | `--auth forwarded` only. Mints the personal token Obsidian's Remotely Save plugin uses over WebDAV and shows it once; a new token revokes the previous one. |
| `lint` | moderate | Reads every note and reports drift. |
| `init` | write | Sets a vault up: writes `.vault/schema.yml` and the Obsidian Bases views into the root and keeps what exists; `force` overwrites. The plugin's `/vault:init` calls it. |
| `write_file` | write | Validates against the schema and refuses the write if it does not hold. Stamps `updated`, fills `date`. Pass `expected_etag` to make the write conditional, or `"*"` to create a note that must not exist yet. On S3 the condition goes with the PUT itself (`If-Match`, `If-None-Match: *`); a server that refuses conditional PUTs gets a HEAD before the PUT instead, and the server logs that once. Only `.md` notes outside dot-folders are written; the schema and the `.base` views come from `init`, and `read_file` still serves them. |
| `append_file` | write | Appends and bumps `updated`; the note must exist and still satisfy the schema. |
| `set_status` | write | Changes a note's status without rewriting it: `backlog` with a priority parks it, `active` picks it up again. |
| `close` | write | Sets status complete (or superseded, with `superseded_by`, when `merged_into` is given) and moves the note into the archive. |
| `log_append` | write | One dated line in the repo log, with the commits it produced. Creates the log when missing. |
| `move_file` | write | Moves or renames a note; it must satisfy the schema at the destination. |
| `delete_file` | write | Deletes for good. Prefer `close`. |

`search` filters: `folder`, `status`, `tag`, `kind`, `area`, `path_prefix`, `since`,
`include_archive`, `include_superseded`, `limit`.

### What lint reports

`broken_frontmatter`, `missing_required` (per field), `missing_area`, `unknown_tags` (only when the
vocabulary is strict), `unresolved_links`, `orphans` (no inbound wikilink; log and archive ignored),
`stale_active`, `archive_status_mismatch`, `duplicate_stems`, `superseded_target_missing`. With `--park-stale`
the open task notes untouched for longer than `stale_after_days` are set to `backlog` (priority `low`, source
`veckolint <date>`) before the report and listed under `parked`, so `active` always means touched within the
window; reviving one is `set_status(path, "active")`. A note with a claim (`kelpie` key) made or refreshed within
the last 14 days, counting from the later of its `since` and the note's `updated`, is left where it is.

```sh
uvx notes-vault-mcp lint
uvx notes-vault-mcp lint --write "Log/lint-$(date +%F).md"
uvx notes-vault-mcp lint --write Log/lint.md --park-stale
```

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

It runs on your machine. The model, `paraphrase-multilingual-MiniLM-L12-v2` run through fastembed and
ONNX, is downloaded once from Hugging Face into `VAULT_CACHE_DIR/models` and shared by every server on
the machine. No API key, and no note leaves the machine. Each note is split at its headings, each section
gets a vector, and the vectors live in the same SQLite index: computed for the notes whose version
changed, dropped with the note, and computed again for every note when the model changes.

What it costs, measured on an M2 Pro with a vault of 500 notes in 3,300 sections:

| | |
| --- | --- |
| Disk | 250 MB for the model, and about 2 KB per section in the index (7 MB here). |
| Memory | About 1 GB in a worker process while the model is loaded. The worker starts on the first search or the first note to embed and exits after five idle minutes. The server itself grows by about 25 MB. A session that never searches, with nothing new to embed, starts no worker. |
| First sync | About a minute (60 sections a second). The server embeds in the background, and search by words works meanwhile. |
| Query | About 18 ms, against 11 ms by words alone. The first query after the worker exited waits about 0.7 s for it to start. |

With `VAULT_SEMANTIC=on` and the extra missing, the server logs one line saying so and searches by
words. `notes-vault-mcp sync` embeds whatever is pending and says how many notes carry vectors;
`sync --rebuild` computes them all again.

## Hooks

Two Claude Code hooks, both reading the hook JSON on stdin and both exiting 0 whatever happens.

`session-start` prints the context bundle for the working directory, then — for each system note it
returned — the commits touching that note's `path` since the note was last updated. That is the
answer to "is this note still true?" before the agent believes it.

`stop` blocks the end of a session that left work unrecorded: commits from the last 24 hours whose
sha does not appear in the repo log, and open task notes older than the schema's `stale_after_days`
that the session read or wrote (found through the vault tool calls in the session transcript). It
returns `{"decision": "block", "reason": ...}`, or nothing at all when the vault is up to date. Stale
notes the session did not touch are the next session's business: `context` lists them under `triage`
with the three ways to settle each (close, a dated line, or park in the backlog), not at every stop. Set `VAULT_STOP_HOOK=off` to silence the hook, as a tool that runs agent sessions and writes the repo log itself can do in the settings `env` of the sessions it starts.

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "uvx notes-vault-mcp hook session-start" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "uvx notes-vault-mcp hook stop" }] }
    ]
  }
}
```

## Other commands

```sh
notes-vault-mcp serve --transport stdio          # the default
notes-vault-mcp sync --rebuild                   # drop the index and read every note again
notes-vault-mcp search "bokning" --limit 5       # the same ranking, from a shell
notes-vault-mcp changelog myapp 2026-08 --repo-path ~/projects/myapp
notes-vault-mcp changelog myapp 2026-08 --repo-path ~/projects/myapp --write
notes-vault-mcp changelog --all                  # this month (and last month during its first week)
```

`changelog` prints the log lines, the git commits grouped by day, and the repo's notes dated inside
the period. With `--write` it keeps that as a period page, `Log/<repo>-<period>.md`, between the
markers `<!-- changelog:generated -->` and `<!-- /changelog:generated -->`; prose above the markers
(a summary written by an agent at month end) is left alone, and the page's status follows the
calendar. `--all` does it for every repo the session-start hook has seen on this machine, and the
stop hook runs that once a day, so the pages stay current without a cron.

## Backlog

```sh
notes-vault-mcp backlog --area myapp --priority high
```

A backlog item is a task note with `status: backlog`, an `area`, a one-line `summary`, an optional
`priority` (`urgent`, `high`, `medium`, `low`) and an optional `source` (who said it and when, or a
sha). `backlog_add` files one from a conversation the moment something is deferred; `backlog` lists
them by priority then age, filtered by area or family; `context` shows the ones relevant to the
current repo apart from the open tasks. Picking an item up is setting its status to `active`;
finishing it is `close`. Lint leaves backlog notes alone however old they get, and flags a
`complete` note that was never closed. `init` writes `Backlog.base` next to the other Obsidian bases.

## Task and log commands

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
# {"configured": true, "version": "0.5.1", "backend": "s3", "writable": null}
notes-vault-mcp status --probe
# {"configured": true, "version": "0.5.1", "backend": "s3", "writable": true}
```

The repo is the main checkout's name, also from a worktree, and the area is the system note the
session-start hook would load for that directory, falling back to the repo name (`"area": null` in the
output when no note matched). `task open` writes a task note with the task name as a placeholder title,
status `active`, `path` set to the main checkout, `source: branch <name>`, and the section headings
with empty bodies; the stem is the repo plus the task name, made unique across the whole vault.
`--note` claims an existing task note instead: status `active`, nothing else changed. `--branch`
defaults to the branch checked out in `--cwd`.

`task claim` says which machine and task work on a task note, in its `kelpie` frontmatter key
(`machine`, `task`, `repo`, `since` in UTC), so a second machine on the same vault sees the note is
taken. It changes only that key, as a conditional write against the version it read, and reads again
when the note changed in between. A note another machine holds is left as it is and answered with
`"claimed": false` and that machine's claim; `--force` takes it over and returns the old claim as
`replaced`. The same machine claiming again keeps `since` when the task is the same. `task release`
removes the key; with `--machine` it leaves another machine's claim in place and answers
`"released": false`. Without a path, `--machine` and `--task` clear that task's claims wherever its notes are now, after a move or a rename; `"paths": []` means the note is gone, and its claim with it. `close` and a status change to `backlog` remove it too, and `write_file` keeps
the stored claim whatever its content says, so only these commands change it. The index stores it in
the `claim` column and `context` shows it on the note's row. List `kelpie` under `optional` in the
schema and the server's instructions tell clients the key is not theirs to edit.

`task check` takes the canonical keys `problem`, `solution`, `decisions`, `tests` and `scope`. A
section is filled when it has any text under its heading, before the next heading of the same or a
higher level. The headings are a schema setting, per language, picked by the vault's `language`:

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
variables. A value Claude Code keeps outside that file has to come from the environment.

## HTTP transport

```sh
VAULT_TOKEN=$(openssl rand -hex 32) notes-vault-mcp serve --transport http --host 0.0.0.0 --port 8765
```

Streamable HTTP on `/mcp`, with four ways to authenticate: `--auth bearer` (the default),
`--auth oidc`, `--auth builtin` and `--auth forwarded`.

`bearer` is the LAN mode. Every request must carry `Authorization: Bearer $VAULT_TOKEN`; anything
else gets 401 before it reaches the server. `VAULT_TOKEN` is mandatory in this mode — the command
refuses to start without it. It is a single token with full access, and claude.ai cannot use it.

The `Host` header follows what the server listens on. Bound to localhost, only `localhost`,
`127.0.0.1` and `[::1]` are answered, anything else gets `421`; bound to another address, every
`Host` is answered. To keep the check on while serving a name of your own, a Kubernetes Service or
a LAN hostname, list it in `VAULT_ALLOWED_HOSTS` or set `VAULT_PUBLIC_URL`: then that name and
localhost are answered and the rest get `421`.

`oidc` and `builtin` speak OAuth, which is what a Claude connector needs. See
[Remote: claude.ai, Claude Desktop and mobile](#remote-claudeai-claude-desktop-and-mobile).

`forwarded` is for a server that sits behind a proxy which already did the OAuth: it trusts a
signed identity header and serves one vault per subject. See
[Behind a proxy: one vault per person](#behind-a-proxy-one-vault-per-person).

## Remote: claude.ai, Claude Desktop and mobile

Run the server over HTTPS with `--auth builtin` or `--auth oidc` and claude.ai can add it as a
custom connector. Connect it once on the web and the same connector appears in Claude Desktop and
in the mobile app.

One server instance serves one vault.

### Prerequisites

- A vault: an S3-compatible bucket with a key scoped to it, or a folder on the host.
- Docker, or `uv` on the host.
- A domain pointing at the machine, with TLS in front of the server. The examples under `deploy/`
  put Caddy there, which fetches the certificate itself.
- For `--auth oidc`: an OpenID Connect provider you already run.

### The two modes

| | `--auth builtin` | `--auth oidc` |
| --- | --- | --- |
| Who logs in | one owner, against a password this server holds | anyone the provider admits |
| Client registration | dynamic, nothing to configure in claude.ai | dynamic when the provider supports it, otherwise a client id and secret pasted into claude.ai |
| Read-only clients | two checkboxes on the login page | group membership |
| State on disk | `auth.sqlite` under `VAULT_AUTH_DIR` | none |

### Environment

| Variable | Mode | Meaning |
| --- | --- | --- |
| `VAULT_PUBLIC_URL` | oidc, builtin | Required. The https address clients reach, path included when the server is not at the root. No default: the server refuses to start without it, and refuses anything that is not `https://` (or `http://localhost` for a local trial). |
| `VAULT_AUTH_DIR` | builtin | Where `auth.sqlite` lives — the owner's password hash, the registered clients, the hashed tokens. Default `~/.cache/notes-vault-mcp/auth`. |
| `VAULT_OIDC_ISSUER` | oidc | Required. The provider's issuer URL. |
| `VAULT_OIDC_AUDIENCE` | oidc | The client id the provider puts in `aud`. Required; a token without it, or for another audience, is refused. |
| `VAULT_OIDC_READ_GROUP` | oidc | Group granting `vault:read`. Default `vault`. |
| `VAULT_OIDC_WRITE_GROUP` | oidc | Group granting `vault:read` and `vault:write`. Default `vault-writers`. |
| `VAULT_OIDC_SCOPES` | oidc | Scopes the resource metadata advertises, so the client asks the provider for scopes it knows. Default `openid profile email groups`. When the access token carries no `groups`, the server asks the provider's userinfo endpoint. |
| `VAULT_TOKEN` | bearer | The static token. |
| `VAULT_ALLOWED_HOSTS` | all | Extra `Host` header values the server answers on, space or comma separated, for a Service name or a LAN hostname next to the public one. |
| `VAULT_CACHE_DIR` | all | Where the index lives. Default `~/.cache/notes-vault-mcp`. |
| `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | all | The vault, unless `VAULT_PATH` names a local folder. |

The MCP endpoint is `VAULT_PUBLIC_URL` + `/mcp`. That is the URL you paste into claude.ai.

### Scopes

- `vault:read` — `search`, `read_file`, `list_files`, `context`, `lint`, `backlog`.
- `vault:write` — `write_file`, `append_file`, `move_file`, `delete_file`, `close`, `log_append`,
  `backlog_add`.

A write tool called with a read-only token fails with an error saying the token may only read the
vault, rather than half-writing or silently doing nothing.

### Built-in login, from zero

```sh
cd deploy/caddy-builtin
cp .env.example .env
$EDITOR .env
docker compose up -d
docker compose exec vault notes-vault-mcp owner set-password
```

Then, in claude.ai: Settings → Connectors → Add custom connector → the `/mcp` URL, e.g.
`https://vault.example.com/mcp`. Nothing else is needed: the server registers the client itself.
The browser lands on the server's login page, which asks for the owner password and shows two
checkboxes, read and write. Clear write to hand out a read-only connector.

What has been connected, and how to disconnect it:

```sh
docker compose exec vault notes-vault-mcp tokens list
docker compose exec vault notes-vault-mcp tokens revoke <client_id>
```

The password hash and the tokens live in `auth.sqlite` in the `vault-data` volume. Back that volume
up; losing it means every client has to connect again.

### OIDC, from zero

```sh
cd deploy/caddy-oidc
cp .env.example .env
$EDITOR .env
docker compose up -d
```

The server is a resource server here: it validates the provider's access tokens and never sees a
password. A user in the write group gets `vault:read` and `vault:write`, a user in the read group
only `vault:read`, and anyone in neither is refused.

Add the connector in claude.ai the same way. With a provider that supports dynamic client
registration you are done. With one that does not (Pocket ID, Authentik), register the client in
the provider first and paste its client id and secret under the connector's Advanced settings; the
redirect URI to allow in the provider is the one claude.ai shows in that dialog.

### Pocket ID

claude.ai sends the MCP resource (the `/mcp` URL) as an OAuth `resource` parameter, and Pocket ID
only accepts a resource it knows as an API. So:

1. Settings → APIs → Add API: a name and the resource identifier, exactly `VAULT_PUBLIC_URL` + `/mcp`.
   Add two permissions: key `vault:read` and key `vault:write`, with the names your family will see on
   the consent screen.
2. Settings → User Groups: create `vault` (read) and `vault-writers` (write) and put users in them.
3. Settings → OIDC Clients → Add: name `claude.ai`, callback URL `https://claude.ai/api/mcp/auth_callback`,
   PKCE on, public client off, allowed user groups `vault`. Under API access, grant the API with both
   permissions (user-delegated). Copy the client id and secret.
4. On the server: `VAULT_OIDC_ISSUER` is Pocket ID's base URL, `VAULT_OIDC_AUDIENCE` is the resource
   identifier from step 1 (Pocket ID puts it in `aud`), and `VAULT_OIDC_SCOPES` is
   `openid profile email groups vault:read vault:write`.
5. In claude.ai, add the connector with the client id and secret under Advanced settings.

The user's groups decide what a token may do; the client's permissions only matter when the
provider reports no groups at all, in the token or through userinfo.

### Kubernetes

`deploy/k8s/` holds plain manifests: a Deployment with a PVC mounted at `/data`, a Service, an
Ingress and an example Secret.

```sh
cp deploy/k8s/secret.example.yaml secret.yaml
$EDITOR secret.yaml
$EDITOR deploy/k8s/ingress.yaml
kubectl apply -f secret.yaml -f deploy/k8s/deployment.yaml -f deploy/k8s/service.yaml -f deploy/k8s/ingress.yaml
```

The Deployment runs `--auth builtin`; for OIDC change the `--auth` argument and fill the
`VAULT_OIDC_*` keys in the Secret. The readiness probe hits
`/.well-known/oauth-protected-resource/mcp`, the one route that answers without a token in both
modes; when `VAULT_PUBLIC_URL` carries a path, that path sits in the probe URL too. Set the owner
password once the pod is up:

```sh
kubectl exec -it deploy/notes-vault-mcp -- notes-vault-mcp owner set-password
```

Fill in `ingressClassName` and point `secretName` at a TLS certificate — an existing secret, or one
cert-manager issues.

## Behind a proxy: one vault per person

`--auth forwarded` is for an organisation that already runs an OAuth authorization server in front
of its MCP servers and forwards the caller's identity as a signed token in a header (the pattern
Cloudflare Access uses with `Cf-Access-Jwt-Assertion`). The server verifies that token with the
proxy's public key, takes the subject from it, and serves that subject's own vault: a prefix per
person in one bucket (or a folder per person under `VAULT_PATH`), with its own index, its own
`.vault/schema.yml` and a welcome note, all created on the first request. Nothing is provisioned
by hand; granting access at the proxy is the whole onboarding.

```sh
notes-vault-mcp serve --transport http --host 0.0.0.0 --port 8765 --auth forwarded
```

The contract with the proxy: a request without a valid identity gets `403` with no
`WWW-Authenticate` challenge (advertising an authorization server here would point clients past
the proxy), the transport is stateless with plain JSON responses so a buffering proxy needs no
`Mcp-Session-Id`, the `Host` header is not checked, and `/up` answers `ok` without a token for
readiness probes. The OAuth metadata routes are not served in this mode; the proxy owns them.

| Variable | Meaning |
| --- | --- |
| `VAULT_PUBLIC_URL` | Required. The address clients reach the server on; its hostname is the `aud` the token must carry, its path (if any) prefixes every route. |
| `VAULT_IDENTITY_PUBLIC_KEY` | Required. The proxy's PEM public key (RS256 or ES256), raw or base64 on one line. |
| `VAULT_IDENTITY_ISSUER` | Required. The `iss` in the proxy's tokens. |
| `VAULT_IDENTITY_HEADER` | The header carrying the token. Default `X-Forwarded-Identity`. |
| `VAULT_IDENTITY_TYP` | When set, the token's `typ` must match. |
| `VAULT_IDENTITY_WRITE_CLAIMS` | Entitlement names (in the token's `entitlements` list or `scope`) that grant `vault:read` and `vault:write`. Default `vault:write`. |
| `VAULT_IDENTITY_READ_CLAIMS` | Entitlement names that grant `vault:read` only. Default `vault:read`. A token with neither gets `403 insufficient_scope`. |
| `VAULT_SUBJECT_PREFIX` | Where the vaults live under the bucket or folder: `<prefix>/<sub>/`. Default `users`. |
| `VAULT_AUTH_DIR` | Where `personal.sqlite` keeps the hashed personal tokens. |
| `VAULT_CACHE_DIR` | One SQLite index per subject lives here; give it a volume. |

The token needs `iss`, `aud`, `sub` and `exp`; `email` and `name` are used when present. Subjects
that are not plain identifiers are hashed before they name a folder.

### Obsidian through the same server

Each vault is also served over WebDAV at `VAULT_PUBLIC_URL/dav/`, for Obsidian's Remotely Save
plugin, which cannot do OAuth. The person asks Claude to run `obsidian_access`; the server mints a
personal token, returns it once and stores only its hash. Remotely Save gets the `/dav/` address,
any username (the email is the convention) and that token as the password. Running
`obsidian_access` again rotates the token. The proxy must pass `/dav/*` straight through, with the
client's `Authorization` header and the WebDAV verbs and headers (`PROPFIND`, `MKCOL`, `MOVE`,
`COPY`, `Depth`, `Destination`, `Overwrite`) intact. The welcome note written on the first request
carries these instructions for the person.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```

`VAULT_TEST_SEMANTIC_MODEL=1 uv run --extra semantic pytest tests/test_semantic_model.py` runs search by
meaning against the real model, which it downloads first; `VAULT_TEST_MODEL_CACHE` points it at a model
folder already on disk.

The test suite runs against a fixture vault under `tests/fixtures/vault/` and a moto-mocked S3
bucket. It never touches a real bucket.

## License

MIT. See [LICENSE](LICENSE).
