# The schema file

`.vault/schema.yml` in the vault sets its rules. It is deep-merged over the preset it names (`preset: para`), or over the built-in layout when it names none, so it only needs to carry what differs. `preset: custom` starts from nothing, or from a copy of another layout's schema and views when you pick one to start from (`mindin init --preset custom --from para`, or Start from on the vault's page), which records it as `made_from: para`. `VAULT_SCHEMA` points at a local file that overrides the one in the vault. [Notes and layouts](../explanation/notes-and-layouts.md) explains the folders and kinds.

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

`repos` ties a note in an organisation's vault to repositories: a list of repo names, such as `payments`, or remotes, such as `acme/payments` or `github.com/acme/payments`. A member's session start in a matching repo lists the note; see [Session start](../explanation/how-it-works.md#session-start). The shared layout lists it among its optional fields.

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
| `kinds` | | The note kinds the folder takes, such as `[howto]`; a note of another kind is refused. |
| `statuses` | | The statuses a note in the folder may have, such as `[complete, superseded]`. |

`root_description` is the vault root's line in the same list. `backlog.folder` names which folder of kind `task` holds the backlog when there are several.

## Bundle

`bundle` tells every agent that works in the vault the same thing. The server adds it to the vault's instructions and to the `rules` tool's answer, so each member's Claude reads it from the vault itself. The wiki layout ships one.

```yaml
bundle:
  instructions: |-
    Write a page here once the work behind it is finished, after the user said yes to the draft.
  session:
    folders: [Areas, Guides, Decisions]
    limit: 5
  guard:
    secrets: true
    personal_data: [card_number, personnummer]
```

| Key | Meaning |
| --- | --- |
| `instructions` | When to write to this vault and how, in the agent's words. |
| `session.folders` | The folders whose pages session start shows when their `repos` name the repo. |
| `session.limit` | At most this many pages from this vault, 5 when left out. Session start shows 5 pages in total across every vault, so a higher limit shows no more. |
| `guard.secrets` | `true` refuses a write that holds a secret. |
| `guard.personal_data` | The personal data a write is refused for: `card_number`, `personnummer`. |

### The content guard

With the guard on, the server refuses a write that holds what it looks for, writes nothing, and answers with the line and kind of each find and the first characters of it, so the agent knows what to remove. In a shared vault, an organisation's vault among them, that is every write of a file whose path has no part starting with a dot, through any tool or WebDAV. In a personal vault it is `write_file` and `append_file`; a WebDAV write to a personal vault is not checked.

- **Secrets**: AWS access keys, private key blocks, GitHub, GitLab, Slack, npm, SendGrid, Google, Anthropic and OpenAI tokens, `sk_live_` style API keys, JSON web tokens, and a `key`, `token`, `secret` or `password` assignment whose value looks random. These patterns are adapted from the default rules of [gitleaks](https://github.com/gitleaks/gitleaks/blob/master/config/gitleaks.toml) (MIT licence). Beside them, a `password:` or `password=` with a value that has a digit or a symbol, and a password in an address such as `https://app:s3cret@example.com`. A placeholder such as `<your password>` passes, and so does an AWS key ending in `EXAMPLE`.
- **`card_number`**: 13 to 19 digits, spaced or not, that pass the Luhn check.
- **`personnummer`**: a Swedish personal identity or coordination number, `YYMMDD-NNNN` or `YYYYMMDDNNNN`, with a valid date and check digit.

The wiki layout turns all of them on.

In a shared vault, the schema and its bundle are changed by the vault's owner or an organisation admin, over WebDAV like any file in the vault. The server refuses a schema that does not hold or that names a personal layout, and keeps the one it replaces as `.vault/schema.previous.yml`. Members' writes to it are refused.

## Other keys

| Key | Built-in default | Meaning |
| --- | --- | --- |
| `language` | `en` | The vault's language, as an ISO code. Picks the stemmer and the headings of the note templates. |
| `other_languages` | `[]` | More languages to stem, such as `[sv]`. Changing either rebuilds the index. |
| `tags.vocabulary` | `[]` | The vault's tags, named in the server's instructions. |
| `tags.strict` | `false` | `true` refuses a write with a tag outside the vocabulary, and `lint` reports such tags. |
| `synonyms` | `[]` | Groups of words search treats as one, such as `[[bokning, booking]]`. |
| `stale_after_days` | `30` | When an open task note counts as stale. |
| `search.default_limit` | `15` | How many notes `search` returns when the call names no `limit`. |
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

## Note templates

`templates` gives a kind of note the headings its body is written to. The built-in layout's:

```yaml
templates:
  task:
    questions: questions
    sections:
      - { key: background, heading: { en: Background, sv: Bakgrund }, required: true }
      - { key: goal, heading: { en: Goal, sv: Mål }, required: true }
      - { key: known, heading: { en: What we know, sv: Det vi vet } }
      - { key: questions, heading: { en: Open questions, sv: Öppna frågor } }
      - { key: scope, heading: { en: Out of scope, sv: Utanför ramen } }
      - { key: source, heading: { en: Source, sv: Källa }, required: true }
  system:
    questions: null
    sections:
      - { key: what, heading: { en: What it does, sv: Vad det gör }, required: true }
      - { key: objects, heading: { en: The objects that hold the truth, sv: Objekten som bär sanningen } }
      - { key: flows, heading: { en: Flows, sv: Flödena } }
      - { key: limits, heading: { en: Limits, sv: Gränser } }
      - { key: exceptions, heading: { en: Exceptions, sv: Undantag } }
      - { key: traps, heading: { en: Traps, sv: Fällor } }
      - { key: left, heading: { en: Left, sv: Kvar } }
      - { key: history, heading: { en: History, sv: Historik } }
```

| Key | Meaning |
| --- | --- |
| `sections` | The headings in order. `key` is a lowercase word that `backlog_add`, `task check` and `template` name the section by. `heading` is the heading per language, one line that does not end in `#`; the vault's `language` picks the one the tools write, falling back to `en`. `required: true` means the section needs text. |
| `questions` | The section that holds open questions, or `null`. A line `- [ ]` under it is a question that blocks the start of the work, a line `- ` one the work settles. |

`write_file` holds a note to its kind's template when it creates the note, as `backlog_add` and `task open` do, when it rewrites a note whose stored body already follows the template, so a note that follows it stays that way, when the stored note was a `backlog` or `draft` note or still carries the mark `backlog_add` puts in a draft, and when the rewrite changes the note's kind. A rewrite that leaves `kind` out is held to the stored kind's template. It refuses such a write, naming the headings that are missing and the required sections left empty, unless:

- its status is `backlog` or `draft`: it is a draft, such as a one-line idea filed with `backlog_add`;
- its status is `complete` or `superseded`;
- its body is the headings alone, as `task open` writes it.

A note of the same kind written before its template, or one whose stored frontmatter does not parse, keeps taking rewrites that do not follow it yet, and `append_file` never checks the body. `lint` lists each open note that does not follow its kind's template under `off_template`, with what it lacks, so those notes can be rewritten over time.

A heading counts in any of its languages, so a vault that writes `## Vad det gör` passes under `language: en`. A template restated in the schema replaces the layout's whole template for that kind; `templates: { system: null }` drops one. The para, simple and journal layouts carry a task template, and the custom, shared and wiki layouts carry none.

The server lists each template in its instructions and in the `rules` tool, one line per kind with the required sections marked. The `template` tool and `mindin template --json` answer it as JSON; see [Agent commands](agent-commands.md#template).

A schema that still sets the earlier `task.sections` (headings by language, such as `sv: { problem: Varför }`) keeps working: its keys `problem`, `solution`, `decisions`, `tests` and `scope` become the task template, with `problem`, `solution` and `decisions` required, unless the schema also sets `templates.task`.

## When the schema does not hold

The schema is checked when it is loaded. A schema that does not hold leaves the vault on its preset, and `context`, `lint` and the vault's page on a server list each problem, for example `folders.Inbox.kind 'todo' is not one of: system, reference, task, log, archive`. A running server picks up an edited schema on its next refresh. `mindin schema check` lists the same problems before you save, together with the tools the layout turns on; see [Command line](command-line.md#vault-and-search).

A vault whose schema names a preset this version does not ship is read as written, over the empty custom schema, and `context`, `lint` and the vault's page say so; `init` keeps that schema until `--preset <name> --force` switches it.
