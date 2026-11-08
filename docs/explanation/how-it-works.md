# How it works

The vault is an Obsidian-style folder of `.md` files with YAML frontmatter, kept on disk, in an S3 bucket or on a vault server. mindin indexes it and gives any MCP client a cheap way to search it, read it and write it. Writes are checked against a [schema](notes-and-layouts.md) the vault carries. Notes can point at code, and at the start of a session the agent gets the commits made since each relevant note was last updated, so a stale note is caught before it is believed.

## Three ways to run it

- **On one machine.** The client starts the server over stdio, with `VAULT_PATH` or the `S3_*` settings. The notes stay plain markdown in your folder or bucket.
- **As a vault server.** `serve --transport http` serves many accounts, each with its own vaults, sealed in storage. claude.ai, Claude Desktop and the mobile app connect to it as a connector. See [Accounts and vaults](accounts-and-vaults.md).
- **Through a server.** A machine [signed in](../how-to/sign-in-a-machine.md) with `VAULT_SERVER_URL` runs the stdio server, the hooks and the commands against its vault on the server.

## The index

A local SQLite index under `VAULT_CACHE_DIR` holds every note's frontmatter and body. Every tool call refreshes it first, at most once every 20 seconds, fetching only the notes whose version changed. Search never downloads the vault. [Search](search.md) explains the ranking.

## Session start

`context` is the one call at the start of a session. For a working directory and repo name it returns:

- the system notes whose `path` covers the directory;
- the open tasks for it;
- a triage list of open notes older than `stale_after_days`, with the three ways to settle each: close it, add a dated line, or park it in the backlog;
- the backlog items for the area, the reference notes and the tail of the repo log;
- for a member of an [organisation](../how-to/run-mindin-for-a-team.md), up to five pages from the organisation's vaults about the repo, as title and one line, each labelled with its vault.

An organisation page is about the repo when its `repos` list names the repo or its origin remote, when its `path` covers the directory, when it is the system note named after the repo, or, ranked last, when its `area` is one of the member's system notes for the directory. The pages that name the repo come first, the newest first among equals.

The Claude Code session-start [hook](../reference/hooks.md) prints that bundle and then, for each system note, the commits touching the note's paths since the note was last updated. That answers "is this note still true?" before the agent trusts it. The stop hook refuses to end a session that left its commits unlogged.

## Writes

Every write goes through the schema: a write that breaks the contract is refused, not stored, with a message the agent can act on. `write_file` stamps `updated` and fills `date`, and can be made conditional on the version the agent read. [Lifecycle](lifecycle.md) covers how notes move from open to archived.
