# Hooks

`mindin hook session-start` and `mindin hook stop` are Claude Code hooks. Both read the hook JSON on stdin and exit 0 whatever happens. The [plugin](../how-to/claude-code.md) installs them; without it, add them to your settings as shown there.

## session-start

Prints the [context bundle](../explanation/how-it-works.md#session-start) for the session's working directory, and then, for each system note, the commits touching the note's paths since the note was last updated.

## stop

Blocks the end of a session that left work unrecorded:

- commits made in this checkout in the last 24 hours whose sha is not in the repo log;
- open task notes older than `stale_after_days` that the session read or wrote through the vault tools.

It answers `{"decision": "block", "reason": ...}` with the `log_append` call to make, or nothing when the vault is up to date. Stale notes the session did not touch are listed by `context` under `triage` at the next start instead.

It also blocks, in any directory, once you have written three messages since the session's last write through the vault tools, asking the agent to save the decisions, deferrals and ideas in them. It asks once per message, so an agent with nothing to save stops on its next try and is asked again after three more.

Once a day it writes the [period pages](command-line.md#changelog) for every repo the session-start hook has seen, as `changelog --all` does.

`VAULT_STOP_HOOK`, `VAULT_STOP_HOOK_COMMITS` and `VAULT_STOP_HOOK_PROMPTS` turn these checks off or tune them; see [Configuration](configuration.md#hooks).
