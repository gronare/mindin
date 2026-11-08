# MCP tools

| Tool | Cost | What it does |
| --- | --- | --- |
| `search` | cheap | Full-text and, when on, meaning search over the index. Filters: `folder`, `status`, `tag`, `kind`, `area`, `path_prefix`, `since`, `include_archive`, `include_superseded`, `limit` (the schema's `search.default_limit` when left out). |
| `context` | cheap | The session-start bundle for a `path` and `repo`, and the repo's origin `remote` for an organisation's pages; `format: "json"` answers it as data. |
| `backlog` | cheap | The backlog by priority then age, filtered by `area` and `priority`. |
| `list_files` | cheap | Paths only. Takes `vault`, like `read_file`. |
| `whoami` | cheap | The account a connection signs in as and whether it may write. |
| `rules` | cheap | A vault's rules for agents: its folders, the frontmatter contract and its [bundle](schema.md#bundle). Takes `vault`, like `read_file`. |
| `read_file` | moderate | One note, prefixed with `etag: <version>`. A superseded note carries a warning callout. `vault` reads from another vault the connection reaches, named as search rows name it. |
| `lint` | moderate | Reads every note and reports drift. |
| `write_file` | write | Validates against the schema and refuses the write if it does not hold. Stamps `updated`, fills `date`. `expected_etag` makes the write conditional; `"*"` creates a note that must not exist yet. Only `.md` notes outside dot-folders are written. `vault` writes to another vault the connection reaches. |
| `append_file` | write | Appends and bumps `updated`; the note must exist and still satisfy the schema. Takes `vault`, like `write_file`. |
| `set_status` | write | Changes a note's status, with `priority` and `source` for the backlog. |
| `close` | write | Finishes a note and moves it into the archive. |
| `backlog_add` | write | Files a backlog note. |
| `log_append` | write | One dated line in the repo log, with the commits and the branch. |
| `move_file` | write | Moves or renames a note; it must satisfy the schema at the destination. |
| `delete_file` | write | Deletes for good. Prefer `close`. |
| `init` | write | Sets up a vault without a schema: the schema of the `preset` it is given and the Bases views. On a vault with a schema it only writes the missing views. |
| `obsidian_access` | write | Server only. Mints the WebDAV password for [Obsidian's Remotely Save](../how-to/obsidian.md) and shows it once. |

Without `vault`, every tool works on the connection's own vault. On a server where the account belongs to an [organisation](../how-to/run-mindin-for-a-team.md), `search` also covers the account's own vault and the organisation's vaults, and `vault` reaches those; anywhere else, naming a vault other than the connection's is refused. See [Search across vaults](../explanation/search.md#across-vaults).

On S3, a conditional `write_file` sends its condition with the PUT; see [Storage](../install/storage.md#the-bucket).

The `init` tool sets up only a vault without a schema, so an MCP client cannot change the rules that check its own writes.

A server also serves `claim`, `release`, `snapshot`, `changelog`, `lint_and_park` and `view` to a machine signed in with [`login`](../how-to/sign-in-a-machine.md), for the [task, log and snapshot commands](agent-commands.md), `changelog`, `lint --write --park-stale` and the terminal output of the [command line](command-line.md) that run through it. Connectors and keys made on a vault's page never see them.

Over HTTP, a connection carries `vault:read` or `vault:read vault:write`. A write tool called on a read-only connection fails with an error saying so, rather than half-writing.

[Lifecycle](../explanation/lifecycle.md) explains how `close`, `set_status`, `backlog_add` and `log_append` work together.
