# Shared vaults

A personal vault is its owner's alone. A shared vault belongs to its owner and the members the owner invites.

## What a shared vault holds

A shared vault holds only the current state of things, in one of two layouts:

- **Shared**: system notes in `Areas/`, howto, decision, reference and trap notes in `Resources/`, and retired notes in `Archive/`.
- **Wiki**: one page per system or product area in `Areas/`, with its owning team and a `repos` list; `Teams/` (who, what they own, how to reach them); `Guides/` written once the work behind them is finished; `Decisions/` with the alternatives dropped and why; `Projects/` for finished projects only; `Incidents/` for reviews of closed incidents; and `Archive/`. Every page names its `team` and `author`, and every page in `Guides/`, `Decisions/`, `Projects/` and `Incidents/` its `area`. A page in `Projects/` or `Incidents/` has status `complete` or `superseded`.

The layout is set when the vault is made, and its owner or an organisation admin can change its rules, such as the wiki's [bundle](../reference/schema.md#bundle). Other members cannot write, move or delete anything under `.vault/`. A shared vault whose schema is missing takes no writes until its owner or an organisation admin puts it back. The server refuses, with a message the agent can act on, a note in any other folder or of another kind, and `backlog_add`, `log_append`, `set_status`, `claim`, `release` and changelog writes. Tasks, the backlog and logs stay in each member's personal vault. `context` in a shared vault says so instead of listing tasks, a backlog or a log.

## Members

The owner invites an email on the vault's page, or with [`accounts add-member`](../how-to/manage-accounts.md#share-a-vault). With SMTP set up the address gets one email with a sign-in link. Signing in through it, or any other way with that verified address, makes the person a member, and makes them an account when they have none, also under `VAULT_REGISTRATION=invite`. A pending invitation can be withdrawn.

Members sign in with their own account and make their own keys and connections to the vault; it shows under Shared on their Vaults page. Removing a member, or a member leaving, stops their keys, tokens and signed-in computers for that vault on their next request. The owner can make a member the owner instead, and deletes the vault for everyone. Support access to a shared vault is the owner's to grant.

## Organisation vaults

An organisation's vault is a shared vault whose members are the organisation's members: everyone who joins the organisation reaches it, and someone removed from the organisation leaves it at once. Its admins manage it from the organisation's page instead of inviting people one by one. An admin can lock it, and then only its editors write; everyone else reads. It is never shared with anyone outside the organisation. A member's connections search it without naming it and list its pages about the repo at session start; any other shared vault is searched only when a tool names it. The organisation also decides whether its members may invite outsiders to the shared vaults they own themselves. See [Run mindin for a team](../how-to/run-mindin-for-a-team.md).

## History

Every change to a shared vault applies at once and is kept. Each write, move and delete through MCP or WebDAV records the path, what happened, the member's email, the key, computer or connector it came through and the time, and keeps the version before it, sealed with the vault's key. The vault's page shows the recent changes and the full history; each change opens as a diff, and "Restore previous" writes the version before it back as a new change. Files in dot-folders, such as `.obsidian/`, are not kept.

An account that owns a shared vault with other members cannot be deleted until each such vault is handed over or deleted. A deleted member's past changes stay in the history under their email.
