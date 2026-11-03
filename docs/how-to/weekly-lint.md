# Run lint every week

A weekly `lint` keeps `active` meaning active: it writes the report as a note and parks the task notes nobody has touched.

```sh
notes-vault-mcp lint --write Log/lint.md --park-stale
```

Schedule that line with cron, a systemd timer or a CI job, with the same vault settings your clients use.

- `--write` keeps the report as a note at that path.
- `--park-stale` sets open task notes untouched for longer than `stale_after_days` (30 by default) to `backlog`, keeping a priority and source they have and otherwise setting priority `low` and source `veckolint <date>`, and lists them under `parked`. A note claimed through `task claim` within the last 14 days is left alone. `set_status(path, "active")` revives a parked note.

For a vault on a server, [sign in](sign-in-a-machine.md) the machine that runs the schedule and set `VAULT_SERVER_URL`; the server parks the notes and writes the report itself.

[Command line](../reference/command-line.md#lint) lists the findings `lint` reports.
