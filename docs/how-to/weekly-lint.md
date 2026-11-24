# Run lint every week

A weekly `lint` keeps `active` meaning active: it writes the report as a note, closes the task notes whose work landed and parks the ones nobody has touched.

```sh
mindin lint --write Log/lint.md --close-done --park-stale
```

Schedule that line with cron, a systemd timer or a CI job, with the same vault settings your clients use.

- `--write` keeps the report as a note at that path.
- `--close-done` closes open task notes (`active`, `draft` and `backlog`) that a line in a repo log (`Log/<repo>-log.md`, as the schema names it) marks done by starting with `done [[stem]]`, or names by the branch in a `source: branch <name>` field, as the line's start (`<branch>: …`) or its `branch:` field. The note gets that line as evidence in its last line and is listed under `closed_done`. A note is listed under `done_uncertain` and left open when a line names it anywhere else (a session's progress line, `not done [[stem]]` among them), when a later line names it without marking it done, when the latest such line is older than the note's `updated` date, when the line says the work has not landed (`unpushed`, `opushat` and the like), when the note still has an unchecked `- [ ]`, when only a commit the note mentions is in the log, and when `close` refuses it. The line's trailing area link never counts as naming a note. A note with a `kebbie` claim is never touched.
- `--park-stale` sets open task notes untouched for longer than `stale_after_days` (30 by default) to `backlog`, keeping a priority and source they have and otherwise setting priority `low` and source `veckolint <date>`, and lists them under `parked`. A note claimed through `task claim` within the last 14 days is left alone. `set_status(path, "active")` revives a parked note.

For a vault on a server, [sign in](sign-in-a-machine.md) the machine that runs the schedule and set `VAULT_SERVER_URL`; the server closes and parks the notes and writes the report itself.

[Command line](../reference/command-line.md#lint) lists the findings `lint` reports.
