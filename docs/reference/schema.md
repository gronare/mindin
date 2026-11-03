# The schema file

`.vault/schema.yml` in the vault sets its rules. It is deep-merged over the preset it names (`preset: para`), or over the built-in layout when it names none, so it only needs to carry what differs. `preset: custom` starts from nothing. `VAULT_SCHEMA` points at a local file that overrides the one in the vault. [Notes and layouts](../explanation/notes-and-layouts.md) explains the folders and kinds.

## Changing the built-in rules

- Add a kind by adding an entry: `research: "reading notes on a topic; no task, no decision"`.
- Rewrite a rule by restating it.
- Drop a kind or a folder by setting it to `null`.
- Nest folders by path: `Work/Logs: { kind: log }`.

## The frontmatter contract

The built-in layout's contract:

```yaml
frontmatter:
  required: [title, date, updated, tags, status]
  optional: [kind, area, summary, path, superseded_by, priority, source, kebbie]
  area_required_in: [Projects, Resources, Log]
  status_values: [draft, active, backlog, complete, superseded]
  kind_values: [system, task, trap, howto, decision, reference, log]
  priority_values: [urgent, high, medium, low]
```

`path` ties a note to code: a comma-separated list of directories (`~` is kept as written and also indexed expanded). `context` and the session-start hook match the working directory against it.

## Kinds

Each kind carries a one-line rule, and the server puts the rules in its instructions so every client picks kinds the same way:

```yaml
kinds:
  trap: "a gotcha that bit once and will bite again; nothing to do, nothing was chosen"
  decision: "a choice and its reasons; something was chosen, alternatives were dropped"
  howto: "steps that are known to work; nothing chosen, nothing open"
```

## Folders

```yaml
folders:
  Areas: { kind: system, weight: 3.0, description: "One living note per system or subsystem." }
  Archive: { kind: archive, weight: 0.3, searched: false, description: "History." }
```

| Key | Default | Meaning |
| --- | --- | --- |
| `kind` | `reference` | `system`, `reference`, `task`, `log` or `archive`: which tools write there. |
| `weight` | `1.0` | How much a note in the folder weighs in search, 0 or more. |
| `searched` | `true` | `false` hides the folder from search unless asked for. |
| `description` | | The folder's line in the server's instructions. |

`root_description` is the vault root's line in the same list. `backlog.folder` names which folder of kind `task` holds the backlog when there are several.

## Other keys

| Key | Built-in default | Meaning |
| --- | --- | --- |
| `language` | `en` | The vault's language, as an ISO code. Picks the stemmer and the task headings. |
| `other_languages` | `[]` | More languages to stem, such as `[sv]`. Changing either rebuilds the index. |
| `tags.vocabulary` | `[]` | The vault's tags, named in the server's instructions. |
| `tags.strict` | `false` | `true` refuses a write with a tag outside the vocabulary, and `lint` reports such tags. |
| `synonyms` | `[]` | Groups of words search treats as one, such as `[[bokning, booking]]`. |
| `stale_after_days` | `30` | When an open task note counts as stale. |
| `search.recency_half_life_days` | `90` | How fast an old note's rank decays. |
| `search.title_weight`, `summary_weight`, `tags_weight`, `body_weight`, `stem_weight` | `10.0`, `5.0`, `3.0`, `1.0`, `20.0` | How much a match in the title, summary, tags, body and file name weighs. |
| `search.stemmed_weight` | `0.5` | How much a match on a word's stem alone weighs, so the exact form ranks first. |

## Repo log

```yaml
log:
  folder: Log
  file_format: "{repo}-log.md"
  period_format: "{repo}-{period}.md"
  entry_format: "- [{date}] {line} | branch: {branch} | commits: {commits} | {area}"
```

The default `-log` suffix keeps `Log/myapp-log.md` from colliding with the hub note `Areas/myapp.md`, which Obsidian could not tell apart as `[[myapp]]`. Every place that builds the log path reads `file_format`, so changing it moves all of them at once; rename the existing files to match.

## Task sections

The headings `task check` looks for, picked by the vault's `language`:

```yaml
task:
  sections:
    en: { problem: Problem, solution: Solution, decisions: Decisions, tests: Test decisions, scope: Out of scope }
    sv: { problem: Problem, solution: Lösning, decisions: Beslut, tests: Testbeslut, scope: Utanför ramen }
```

## When the schema does not hold

The schema is checked when it is loaded. A schema that does not hold leaves the vault on its preset, and `context`, `lint` and the vault's page on a server list each problem, for example `folders.Inbox.kind 'todo' is not one of: system, reference, task, log, archive`. A running server picks up an edited schema on its next refresh.

A vault whose schema names a preset this version does not ship is read as written, over the empty custom schema, and `context`, `lint` and the vault's page say so; `init` keeps that schema until `--preset <name> --force` switches it.
