# Command line

`mindin --help` and `mindin <command> --help` print every option. The commands for tools that drive agents (`task`, `log`, `snapshot`, `status`) are in [Agent commands](agent-commands.md).

## Setting up a machine

```sh
mindin setup                            # asks where the vault lives, in a terminal
mindin setup --folder ~/vault
mindin setup --s3 <bucket> --endpoint <url> [--prefix <prefix>] [--region <region>]
mindin setup --server <url> [--vault <slug>] [--no-browser]
mindin setup --signals yes|no           # share anonymous usage data, or stop
mindin move --to (--folder <path> | --s3 <bucket> --endpoint <url> [--prefix <prefix>] [--region <region>] | --server <url> [--vault <slug>])
            [--dry-run] [--merge] [--include-skipped] [--skip-refused] [--no-browser]
mindin about [--json]                   # name, version and the commands for tools
```

`setup` saves the choice in `mindin/config.toml` under `XDG_CONFIG_HOME` (default `~/.config/mindin/config.toml`), as a `[vault]` table with `kind` set to `folder`, `s3` or `server`. A folder must exist; asked in a terminal, setup offers to create it. `--s3` reads the bucket's keys from `S3_ACCESS_KEY` and `S3_SECRET_KEY`, or asks for them in a terminal, and keeps them in `credentials.json` next to the config, readable only by you, never in `config.toml`. `--server` signs the machine in with [`login`](../how-to/sign-in-a-machine.md) unless it holds a key already, and when the vault it replaces is a folder or a bucket with notes, asks whether to bring them along. Running `setup` again replaces the choice.

`move` copies this machine's vault to the place it names, reads every file back to check it, and then saves that place as `setup` would; the old place is left as it was. [Move a vault](../how-to/move-a-vault.md) shows each direction.

In a terminal, `setup` also asks once whether to share anonymous usage data, the [install signal](install-signal.md); an empty answer is no. The answer is saved as `[signals] install = true` or `false`, and `setup` asks again only while it is missing. `--signals yes|no` gives the answer from a script, and alone it changes only that.

`about --json` prints the [contract](agent-commands.md#about) tools read.

## Vault and search

```sh
mindin serve --transport stdio          # the default, also with no subcommand
mindin serve --transport http --host 0.0.0.0 --port 8765
mindin init [--preset default|para|simple|journal|custom] [--force]
mindin sync [--rebuild]                 # refresh the index; --rebuild reads every note again
mindin embed-worker [--listen 127.0.0.1:7400] [--model <name>]  # the model in its own process
mindin search "bokning" --limit 5       # the same ranking, from a shell
mindin context [--path ~/projects/myapp] # what a session in this repo starts with
mindin read Projects/myapp-idea.md [--body] [--raw]
mindin lint [--kind orphans] [--write Log/lint.md] [--park-stale]
mindin backlog [--area myapp] [--priority high] [--paths] [--all]
mindin changelog myapp 2026-08 --repo-path ~/projects/myapp [--write]
mindin changelog --all                  # this month, and last month during its first week
mindin hook session-start | stop        # the Claude Code hooks, JSON on stdin
```

`serve --host` defaults to `127.0.0.1` and `--port` to `8765`. `embed-worker` is described in [Search by meaning](../install/semantic-search.md#the-model-in-its-own-process). `search --limit` defaults to the schema's `search.default_limit`. Through a server, `sync` only says that the server keeps the index.

## Signing in to a server

```sh
mindin login [<url>] [--vault <slug>] [--name laptop] [--no-browser]
mindin whoami [<url>]
mindin logout [<url>] [--vault <slug>]
mindin vault list | use <slug> [--dir <folder>] [--url <url>] | drop --dir <folder> [--url <url>]
```

Without `<url>` or `--url` they take `VAULT_SERVER_URL`. See [Sign a machine in](../how-to/sign-in-a-machine.md).

## On the server's machine

```sh
mindin accounts list | invite <email> | suspend <account> | activate <account>
mindin accounts delete <account> --yes
mindin accounts vaults <account>
mindin accounts add-vault <account> <name> [--slug <slug>] [--preset <layout> | --shared]
mindin accounts rename-vault <account> <slug> [--name <name>] [--slug <slug>]
mindin accounts members <owner> <slug>
mindin accounts add-member | remove-member <owner> <slug> <email>
mindin accounts import <account> [--vault <slug>] (--from-path <dir> | --from-s3-bucket <bucket> [--from-s3-prefix <prefix>] [--from-s3-endpoint <url>] [--from-s3-region <region>]) [--dry-run] --operator <name> --reason <text>
mindin support files <account> [--vault <slug>] --operator <name> --reason <text>
mindin support read <account> <path> [--vault <slug>] --operator <name> --reason <text>
mindin master-key generate | status | rewrap
```

### Organisations

```sh
mindin org list | create <name> --admin <email> | show <organisation>
mindin org domain add <organisation> <domain> [--verified] [--allow-public]
mindin org domain check | remove <organisation> <domain>
mindin org admin add | remove <organisation> <email>
mindin org member add | remove <organisation> <email>
mindin org vault create <organisation> <name> [--slug <slug>] [--by <admin>] [--layout wiki | shared]
mindin org vault lock | unlock <organisation> <slug>
mindin org vault editor-add | editor-remove <organisation> <slug> <email>
mindin org removal <organisation> keep | export-and-delete [--days <days>]
mindin org sharing <organisation> allowed | denied
```

`<organisation>` is its name or id. A domain lets people in once it is verified: `domain add` prints the TXT record to publish at `_mindin-verify.<domain>`, and `domain check` looks it up. `--verified` marks it verified at once, also when the organisation already added it unverified, and `--allow-public` accepts a public email provider's domain, which is otherwise refused. `vault create --by` names the admin who owns the vault and defaults to the first admin; `--layout` is `wiki` unless you pick `shared`. `removal` decides what happens to a removed member's personal vaults; `--days`, 1 to 365, starts at 30, and leaving it out keeps the current number. `sharing` decides whether members may invite people outside the organisation to the shared vaults they own; it starts as `denied`. See [Run mindin for a team](../how-to/run-mindin-for-a-team.md).

See [Manage accounts](../how-to/manage-accounts.md), [Support access and import](../how-to/support-access-and-import.md) and [Back up and rotate keys](../how-to/back-up-and-rotate.md).

## Output

In a terminal, `search`, `context`, `read`, `backlog` and `lint` print for a person:

- `search` shows each note's title with its kind and age, its path below it, and the matching text with wikilinks unwrapped; `≈ meaning` marks a note found by meaning rather than by its words.
- `backlog` is one row per idea with its priority, area and age; `--paths` adds each note's path, and past 20 ideas the list is grouped by priority and ends with how many more there are, which `--all` shows.
- `read` turns the frontmatter into a title and one line (kind, area, status, age, tags) above the text, and leaves out a first `# heading` that repeats the title.
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

Every command, the stdio server included, takes the first of these that names a vault:

1. The environment ([Configuration](configuration.md#storage)).
2. mindin's own config, written by [`setup`](#setting-up-a-machine).
3. The Claude Code plugin's saved user config.

`status` says which one decided, in `source`: `env`, `config` or `plugin`. The plugin's config is `pluginConfigs["vault@<marketplace>"].options` in `$CLAUDE_CONFIG_DIR/settings.json` (default `~/.claude/settings.json`), mapping `vault_path`, `s3_endpoint`, `s3_bucket`, `s3_access_key`, `s3_secret_key`, `s3_prefix`, `s3_region` and `vault_server_url` to their variables, with the secrets from Claude Code's credentials store when they are kept there.
