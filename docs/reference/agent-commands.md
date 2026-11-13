# Agent commands

For a tool that drives coding agents and records the facts itself at fixed points (start, push, done, reset) instead of relying on the agent to call the MCP tools. Each prints one JSON object on stdout and exits 0; a failure exits non-zero with one line on stderr. Every write goes through the same path as the MCP tools: schema validation, the `updated` stamp and the index. They find the vault as the [other commands](command-line.md#where-commands-find-the-vault) do.

```sh
mindin task open --name "Fix login" --cwd ~/wt/wt1.myapp [--branch fix-login]
# {"path": "Projects/myapp-fix-login.md", "created": true, "repo": "myapp", "area": "myapp"}
mindin task open --note Projects/myapp-idea.md --cwd ~/wt/wt1.myapp
# {"path": "Projects/myapp-idea.md", "created": false, "repo": "myapp", "area": "myapp"}
mindin task check Projects/myapp-fix-login.md [--need background,goal,source]
# {"missing": ["goal", "source"]}
mindin task close Projects/myapp-fix-login.md        # {"path": "Archive/myapp-fix-login.md"}
mindin task backlog Projects/myapp-fix-login.md --priority medium   # {"path": "Projects/…"}
mindin task claim Projects/myapp-idea.md --machine mac --task fix-login --repo myapp [--force]
# {"path": "Projects/myapp-idea.md", "claimed": true, "claim": {"machine": "mac", "task": "fix-login", "repo": "myapp", "since": "2026-10-23T09:12:00Z"}, "replaced": null}
mindin task release Projects/myapp-idea.md [--machine mac]
# {"path": "Projects/myapp-idea.md", "released": true, "claim": null}
mindin task release --machine mac --task fix-login
# {"paths": ["Projects/myapp-renamed.md"], "released": true}
mindin log append --cwd ~/wt/wt1.myapp --line "Login fixed" --commit abc1234 --commit def5678 [--branch fix-login]
# {"path": "Log/myapp-log.md"}
mindin log path --repo myapp                         # {"path": "Log/myapp-log.md"}
mindin log path --repo myapp --repo docs            # {"myapp": "Log/myapp-log.md", "docs": "Log/docs-log.md"}
mindin snapshot --json [--since ETAG]               # the board's notes and logs in one answer
mindin changes --json [--since CURSOR] [--wait SECONDS] [--body] [--limit N]
# {"cursor": "a1b2c3d4.42", "reset": false, "more": false, "changes": [{"key": "Projects/myapp-idea.md", "action": "write", "version": "…", "note": {"frontmatter": {…}}}]}
mindin watch --json [--since CURSOR] [--body]       # one line like changes now, then one per batch, until stopped
mindin read Projects/myapp-idea.md [--body]          # the note as stored in a pipe, or only its text after the frontmatter
mindin read Projects/myapp-idea.md --json
# {"text": "---\ntitle: …", "etag": "9b2f…"}
mindin write Projects/myapp-idea.md --etag 9b2f… < note.md
# {"path": "Projects/myapp-idea.md", "etag": "c41a…"}
mindin template --json --kind task
# {"kind": "task", "language": "en", "sections": [{"key": "background", "heading": "Background", "required": true}, …], "questions": "questions", "skeleton": "## Background\n\n## Goal\n…"}
mindin status
# {"configured": true, "version": "0.17.0", "backend": "s3", "source": "config", "writable": null, "signals": "on", "signals_source": "config", "signals_change": "mindin setup --signals yes|no"}
mindin status --probe
# {"configured": true, "version": "0.17.0", "backend": "s3", "source": "config", "writable": true, "signals": "on", "signals_source": "config", "signals_change": "mindin setup --signals yes|no"}
mindin about --json
# {"name": "mindin", "version": "0.17.0", "contract": 1, "capabilities": ["tasks", "claims", "log", "snapshot", "changes", "watch", "read", "write", "templates"], "former_names": ["notes-vault-mcp"]}
```

## task open

The repo is the main checkout's name, also from a worktree, and the area is the system note the session-start hook would load for that directory, falling back to the repo name (`"area": null` when no note matched).

`task open` writes a task note with the task name as a placeholder title, status `active`, `path` set to the main checkout, `source: branch <name>`, and the headings of the task template with empty bodies; the stem is the repo plus the task name, made unique across the whole vault. `--note` claims an existing task note instead: status `active`, nothing else changed. `--branch` defaults to the branch checked out in `--cwd`.

## task check

`task check` takes the keys of the task template's sections, and checks its required ones when `--need` is left out; in the built-in layout those are `background`, `goal` and `source`. A section is filled when it has any text under its heading, before the next heading of the same or a higher level. The headings are a [schema setting](schema.md#note-templates), per language.

## template

`template --json` answers the [note templates](schema.md#note-templates) of the vault's schema, the same JSON as the `template` tool: with `--kind`, one kind's `sections` (`key`, `heading` in the vault's language, `required`), its `questions` section and a `skeleton` to fill; without it, `{"language": …, "templates": {"task": {…}, "system": {…}}}`. A kind without a template answers `"sections": []`, and a kind the schema does not name fails. Without `--json` it prints one line per kind.

## task claim and release

`task claim` says which machine and task work on a task note, in its `kebbie` frontmatter key (`machine`, `task`, `repo`, `since` in UTC), so a second machine on the same vault sees the note is taken. It changes only that key, as a conditional write against the version it read, and reads again when the note changed in between.

- A note another machine holds is left as it is and answered with `"claimed": false` and that machine's claim; `--force` takes it over and returns the old claim as `replaced`.
- `task release` removes the key; with `--machine` it leaves another machine's claim in place and answers `"released": false`.
- Without a path, `--machine` and `--task` clear that task's claims wherever its notes are, after a move or a rename; `"paths": []` means the note is gone.
- `close` and a status change to `backlog` remove the claim too, and `write_file` keeps the stored claim whatever its content says, so only these commands change it. `context` shows it on the note's row.

## snapshot

`snapshot --json` answers, in one call, what a board needs from the vault:

- every system and task note outside the archive and log folders, each with `key`, `stem`, `role` (`system` or `task`, taken from the folder, so a client needs no folder names), `kind`, `status`, `title`, `summary`, `priority`, `area`, `paths`, `date`, `updated`, `mtime` and its `claim` (or `null`);
- `openings`, the first paragraph of each active or draft task;
- `logs`, the last 50 lines of each repo log as `{date, text, links}`;
- `log_key_template` (such as `Log/{repo}-log.md`, or `null` when the layout keeps no log) and `layout`.

Every answer carries an `etag`; with `--since <etag>` the answer is only `{"unchanged": true, "etag": …}` until something it covers changes, so a client can ask every few seconds. Through a server it is one request.

## read and write

`read --json` answers the whole note as stored, frontmatter included, as `text`, with its `etag`. `write` reads a whole note from stdin and writes it the way `write_file` does: the schema, the [note templates](schema.md#note-templates) and the content checks apply, `updated` is stamped and a claim is kept. With `--etag` it writes only over that version, so a tool edits a note in three steps: `read --json`, change the text, `write --etag`. A note changed in between fails with exit 1 and a message naming the etag; `--etag "*"` creates a note that must not exist yet. Any other refusal fails with exit 1 and the reason `write_file` gives.

## changes and watch

`changes --json` answers the notes written, deleted or moved since `--since`, each once in its latest state, so a client that keeps a copy of the vault fetches only what changed:

- `changes` lists each note as `key`, `action` (`write`, `delete`, or `schema` for `.vault/schema.yml`), `version` (the etag `read_file` gives) and `note`: its `frontmatter`, and with `--body` its `body` too; `null` for a delete. A move is a `delete` of the old key and a `write` of the new one.
- `cursor` is what to pass as `--since` next. It is opaque: keep it as it is.
- `reset: true` means the cursor cannot be followed: there was none, it is from another vault or index, or the changes after it are no longer kept (30 days, or the last 10,000 per vault). Read the whole vault, with `snapshot`, then follow from the `cursor` the reset answer carries; read that cursor first so nothing written in between is missed.
- `more: true` means `--limit` (100) cut the answer short: ask again with the new cursor at once.
- `--wait` holds the answer until something changes or the seconds pass; an answer with no changes keeps the cursor.

`watch --json` is one long-lived process for a client that reads a stream: it prints one line shaped like the answer of `changes` at once, with the cursor to follow from, then one line per batch of changes. When it loses the server it says so on stderr and connects again, waiting 1 second, then twice as long each time up to a minute, and carries on from its last cursor; a refused key or a server without `changes` ends it with exit 1.

How a wait learns of a change depends on where the vault is:

- **Server**: a write on the server wakes the waiting request at once; nothing is polled. One request holds up to 50 seconds, so `watch` and a longer `--wait` ask again.
- **Folder**: the operating system's file events wake it; a file in a dot-folder, such as `.obsidian/`, does not.
- **Bucket**: S3 tells nothing, so the bucket is listed at most every 20 seconds while a client waits.

## status

`status` never fails: with no vault configured it prints `"configured": false, "backend": null, "source": null` and exits 0. `source` names what decided where the vault is: `env`, `config` (mindin's own, from `setup`) or `plugin` ([Where commands find the vault](command-line.md#where-commands-find-the-vault)). Through a server it answers `"backend": "server"` with the `server` and whether this machine is `signed_in`. It is read-only and leaves `writable` null; `--probe` measures it by writing and deleting `.vault/write-probe`. `signals` is `on`, `off` or `not asked` for the [install signal](configuration.md#install-signal), `signals_source` names what decided it, `env` or `config` (`null` while not asked), and `signals_change` is the command that changes it.

## about

`about --json` tells a tool which mindin it found and what it can ask of it:

- `name` is always `mindin`, also when run as `notes-vault-mcp`.
- `contract` is the version of these commands' JSON; it changes only when an answer changes shape.
- `capabilities` lists the commands this install has: `tasks` (`task`), `claims` (`task claim`), `log`, `snapshot`, `changes`, `watch`, `read`, `write` (`read --json` and `write`) and `templates` (`template`).
- `former_names` lists the names the package and command had before.
