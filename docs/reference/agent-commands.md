# Agent commands

For a tool that drives coding agents and records the facts itself at fixed points (start, push, done, reset) instead of relying on the agent to call the MCP tools. Each prints one JSON object on stdout and exits 0; a failure exits non-zero with one line on stderr. Every write goes through the same path as the MCP tools: schema validation, the `updated` stamp and the index. They find the vault as the [other commands](command-line.md#where-commands-find-the-vault) do.

```sh
mindin task open --name "Fix login" --cwd ~/wt/wt1.myapp [--branch fix-login]
# {"path": "Projects/myapp-fix-login.md", "created": true, "repo": "myapp", "area": "myapp"}
mindin task open --note Projects/myapp-idea.md --cwd ~/wt/wt1.myapp
# {"path": "Projects/myapp-idea.md", "created": false, "repo": "myapp", "area": "myapp"}
mindin task check Projects/myapp-fix-login.md --need problem,solution,decisions
# {"missing": ["solution", "decisions"]}
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
mindin read Projects/myapp-idea.md [--body]          # the note as stored in a pipe, or only its text after the frontmatter
mindin status
# {"configured": true, "version": "0.15.2", "backend": "s3", "source": "config", "writable": null, "signals": "on", "signals_source": "config", "signals_change": "mindin setup --signals yes|no"}
mindin status --probe
# {"configured": true, "version": "0.15.2", "backend": "s3", "source": "config", "writable": true, "signals": "on", "signals_source": "config", "signals_change": "mindin setup --signals yes|no"}
mindin about --json
# {"name": "mindin", "version": "0.15.2", "contract": 1, "capabilities": ["tasks", "claims", "log", "snapshot", "read"], "former_names": ["notes-vault-mcp"]}
```

## task open

The repo is the main checkout's name, also from a worktree, and the area is the system note the session-start hook would load for that directory, falling back to the repo name (`"area": null` when no note matched).

`task open` writes a task note with the task name as a placeholder title, status `active`, `path` set to the main checkout, `source: branch <name>`, and the section headings with empty bodies; the stem is the repo plus the task name, made unique across the whole vault. `--note` claims an existing task note instead: status `active`, nothing else changed. `--branch` defaults to the branch checked out in `--cwd`.

## task check

`task check` takes the keys `problem`, `solution`, `decisions`, `tests` and `scope`. A section is filled when it has any text under its heading, before the next heading of the same or a higher level. The headings are a [schema setting](schema.md#task-sections), per language.

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

## status

`status` never fails: with no vault configured it prints `"configured": false, "backend": null, "source": null` and exits 0. `source` names what decided where the vault is: `env`, `config` (mindin's own, from `setup`) or `plugin` ([Where commands find the vault](command-line.md#where-commands-find-the-vault)). Through a server it answers `"backend": "server"` with the `server` and whether this machine is `signed_in`. It is read-only and leaves `writable` null; `--probe` measures it by writing and deleting `.vault/write-probe`. `signals` is `on`, `off` or `not asked` for the [install signal](configuration.md#install-signal), `signals_source` names what decided it, `env` or `config` (`null` while not asked), and `signals_change` is the command that changes it.

## about

`about --json` tells a tool which mindin it found and what it can ask of it:

- `name` is always `mindin`, also when run as `notes-vault-mcp`.
- `contract` is the version of these commands' JSON; it changes only when an answer changes shape.
- `capabilities` lists the commands this install has: `tasks` (`task`), `claims` (`task claim`), `log`, `snapshot` and `read`.
- `former_names` lists the names the package and command had before.
