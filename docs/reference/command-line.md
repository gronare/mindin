# Command line

`notes-vault-mcp --help` and `notes-vault-mcp <command> --help` print every option. The commands for tools that drive agents (`task`, `log`, `snapshot`, `status`) are in [Agent commands](agent-commands.md).

## Vault and search

```sh
notes-vault-mcp serve --transport stdio          # the default, also with no subcommand
notes-vault-mcp serve --transport http --host 0.0.0.0 --port 8765
notes-vault-mcp init [--preset default|para|simple|journal|custom] [--force]
notes-vault-mcp sync [--rebuild]                 # refresh the index; --rebuild reads every note again
notes-vault-mcp search "bokning" --limit 5       # the same ranking, from a shell
notes-vault-mcp context [--path ~/projects/myapp] # what a session in this repo starts with
notes-vault-mcp read Projects/myapp-idea.md [--body] [--raw]
notes-vault-mcp lint [--kind orphans] [--write Log/lint.md] [--park-stale]
notes-vault-mcp backlog [--area myapp] [--priority high] [--paths] [--all]
notes-vault-mcp changelog myapp 2026-08 --repo-path ~/projects/myapp [--write]
notes-vault-mcp changelog --all                  # this month, and last month during its first week
notes-vault-mcp hook session-start | stop        # the Claude Code hooks, JSON on stdin
```

`serve --host` defaults to `127.0.0.1` and `--port` to `8765`. Through a server, `sync` only says that the server keeps the index.

## Signing in to a server

```sh
notes-vault-mcp login [<url>] [--vault <slug>] [--name laptop] [--no-browser]
notes-vault-mcp whoami [<url>]
notes-vault-mcp logout [<url>] [--vault <slug>]
notes-vault-mcp vault list | use <slug> [--dir <folder>] [--url <url>] | drop --dir <folder> [--url <url>]
```

Without `<url>` or `--url` they take `VAULT_SERVER_URL`. See [Sign a machine in](../how-to/sign-in-a-machine.md).

## On the server's machine

```sh
notes-vault-mcp accounts list | invite <email> | suspend <account> | activate <account>
notes-vault-mcp accounts delete <account> --yes
notes-vault-mcp accounts vaults <account>
notes-vault-mcp accounts add-vault <account> <name> [--slug <slug>] [--preset <layout> | --shared]
notes-vault-mcp accounts rename-vault <account> <slug> [--name <name>] [--slug <slug>]
notes-vault-mcp accounts members <owner> <slug>
notes-vault-mcp accounts add-member | remove-member <owner> <slug> <email>
notes-vault-mcp accounts import <account> [--vault <slug>] (--from-path <dir> | --from-s3-bucket <bucket> [--from-s3-prefix <prefix>] [--from-s3-endpoint <url>] [--from-s3-region <region>]) --operator <name> --reason <text>
notes-vault-mcp support files <account> [--vault <slug>] --operator <name> --reason <text>
notes-vault-mcp support read <account> <path> [--vault <slug>] --operator <name> --reason <text>
notes-vault-mcp master-key generate | status | rewrap
```

See [Manage accounts](../how-to/manage-accounts.md), [Support access and import](../how-to/support-access-and-import.md) and [Back up and rotate keys](../how-to/back-up-and-rotate.md).

## Output

In a terminal, `search`, `context`, `read`, `backlog` and `lint` print for a person:

- `search` shows each note's title with its kind and age, its path below it, and the matching text with wikilinks unwrapped; `≈ meaning` marks a note found by meaning rather than by its words.
- `backlog` is one row per idea with its priority, area and age; `--paths` adds each note's path, and past 20 ideas the list is grouped by priority and ends with how many more there are, which `--all` shows.
- `read` turns the frontmatter into a title and one line (kind, area, status, age, tags) above the text.
- `context` sums up what the session-start hook gives an agent for the repo you stand in, or for `--path`: the system note, the commits newer than it, open tasks, the backlog, traps and the last log line.
- `lint` counts the findings per kind with a plain explanation, and `--kind` lists one kind.

Rows are cut at the terminal's width, and a path is always printed whole so you can copy it.

In a pipe, a script or a hook, and with `--plain`, every command prints plain lines, and `context` prints what the session-start hook prints. `read --raw` prints the file exactly as stored, and `read --body` only the text after the frontmatter. `NO_COLOR=1` keeps the layout without colour.

## lint

`lint` reads every note and reports `broken_frontmatter`, `missing_required` (per field), `invalid_field`, `missing_area`, `unknown_tags` (only with a strict vocabulary), `unresolved_links`, `orphans` (no inbound wikilink; log and archive ignored), `stale_active`, `done_not_closed`, `archive_status_mismatch`, `duplicate_stems` and `superseded_target_missing`.

`--write <path>` also keeps the report as a note. `--park-stale` parks stale task notes; see [Run lint every week](../how-to/weekly-lint.md).

## changelog

`changelog <repo> <period>` prints the log lines, the git commits grouped by day, and the repo's notes dated inside the period (`YYYY` or `YYYY-MM`), each day whole from midnight to midnight. `--repo-path` is the git checkout to read commits from.

With `--write` it keeps that as a period page, `Log/<repo>-<period>.md`, between the markers `<!-- changelog:generated -->` and `<!-- /changelog:generated -->`. Prose above the markers, such as a summary written at month end, is left alone, and the page's status follows the calendar. `--all` does it for every repo the session-start hook has seen on this machine, and the [stop hook](hooks.md#stop) runs that once a day, so the pages stay current without a cron. Through a server, the commits are read on this machine and the page is written on the server.

## Where commands find the vault

Every command reads the storage settings from the environment first ([Configuration](configuration.md#storage)). When the environment names no vault, it takes the Claude Code plugin's saved user config instead: `pluginConfigs["vault@<marketplace>"].options` in `$CLAUDE_CONFIG_DIR/settings.json` (default `~/.claude/settings.json`), mapping `vault_path`, `s3_endpoint`, `s3_bucket`, `s3_access_key`, `s3_secret_key`, `s3_prefix`, `s3_region` and `vault_server_url` to their variables, with the secrets from Claude Code's credentials store when they are kept there.
