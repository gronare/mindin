# Notes and layouts

## Notes and folders

A note is a markdown file with frontmatter. The built-in layout has five folders, and the folder decides the kind of note and how much it weighs in search:

| Folder | Kind | Weight | Role |
| --- | --- | --- | --- |
| `Areas/` | system | 3.0 | One living note per system. Current state only. The hubs of the graph. |
| `Resources/` | reference | 2.0 | Traps, how-tos and decisions with their reasons. |
| `Projects/` | task | 1.0 | Open work spanning sessions, and the backlog. Closed with `close`. |
| `Log/` | log | 1.0 | One append-only log per repo, plus period pages. |
| `Archive/` | archive | 0.3 | History. Searched only on request. |

Every note must satisfy the frontmatter contract: required fields, legal statuses and kinds, folders that must link an area, and an optional tag vocabulary. [The schema file](../reference/schema.md) lists the contract and every setting.

## Layouts

The folders above are the built-in layout, which `init` writes when it is given no preset. `init --preset <name>` starts a vault from another layout instead, each with its own folders, frontmatter contract, kinds, tags and Bases views:

| Preset | Folders |
| --- | --- |
| `para` | Projects, Areas, Resources (with Resources/Logs), Archive |
| `simple` | Most notes in the root, typed by a `categories` link to an overview note; Projects (open work and the backlog), References, Clippings, Daily, Templates, Attachments. No log, no archive |
| `journal` | Daily (one note per day, `Daily/YYYY/YYYY-MM-DD.md`), Notes, Projects, Logs, Templates, Archive |
| `custom` | An empty schema you fill in |

`simple` follows Steph Ango's vault: a category note such as `Books.md`, made from `Templates/Category Template`, embeds `Templates/Bases/Category.base` and lists every note whose `categories` links to it, and the daily note template embeds the notes written or linked that day. Open work is the one exception to notes in the root: it lives in `Projects/`, so the tools can file it, and `Open work.base` and `Backlog.base` show it.

In Obsidian, point the core Daily notes plugin at the `Daily` folder and `Templates/Daily Note Template`; for `journal`, set its date format to `YYYY/YYYY-MM-DD`.

## Where each tool writes

Each tool finds its home by folder kind: open tasks and the backlog go to the folder of kind `task`, `log_append` writes to the folder of kind `log`, and `close` moves notes to the folder of kind `archive`. In a layout without one of these, the tool says which folder kind is missing.

## Switching layouts

`init --preset <name> --force` switches an existing vault: the schema and the views are rewritten, the old schema is kept as `.vault/schema.previous.yml`, and no note is moved or deleted. Notes in folders the new layout does not name stay where they are and stay searchable; each is checked against the new rules the next time it is written, and `lint` lists the ones that do not fit.

The `init` tool sets up only a vault without a schema, so an MCP client cannot change the rules that check its own writes.

On a server, a vault picks PARA, Simple, Journal or Custom when it is made, and switches to another of them on its page later, without moving any note. A vault made without one, such as an account's first vault, asks on its page. A vault in the built-in layout keeps it; the page names it, and it is switched from the command line only.
