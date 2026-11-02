# Lifecycle, backlog and the repo log

A vault stays useful when finished work leaves the active set, deferred work is filed, and each session leaves a line. The tools are built around that.

- `close` sets a note's status to `complete` (or `superseded`, with `superseded_by`, when `merged_into` is given) and moves it into the archive.
- `set_status` changes the status without rewriting the note: `backlog` with a priority parks it, `active` picks it up again.
- `backlog_add` files a task note with `status: backlog`, an area, a one-line summary, an optional priority and an optional source (who said it and when, or a sha). `backlog` lists them by priority then age. Lint leaves backlog notes alone however old they get.
- `log_append` writes one dated line to the repo log with the commits the session produced, and creates the log when it is missing. The [stop hook](../reference/hooks.md#stop) checks these shas.

## Stale notes

An open task note untouched for longer than `stale_after_days` is stale. `context` lists stale notes under `triage` with the three ways to settle each. `lint --park-stale` parks them in the backlog, so `active` means touched within the window; see [Run lint every week](../how-to/weekly-lint.md).

## Claims

When several machines work on one vault, a task note can carry a claim in its `kebbie` frontmatter key saying which machine and task hold it. Only [`task claim` and `task release`](../reference/agent-commands.md#task-claim-and-release) change it; `close` and parking remove it.
