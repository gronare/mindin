# Use it with Claude Code

## With the plugin

The plugin installs the MCP server, the two [hooks](../reference/hooks.md) and the skills, and asks for the vault settings: a local folder, an S3 bucket, or a vault server.

```sh
claude plugin marketplace add https://github.com/gronare/claude-plugins
claude plugin install vault@gronare
```

It passes the settings as `CLAUDE_PLUGIN_OPTION_<NAME>` variables, which the server reads as if they were the bare names; the bare name wins when both are set.

For a vault server, give its address at install, then sign the machine in and reconnect:

```sh
claude plugin install vault@gronare --config vault_server_url=https://vault.example.com
```

Run `/vault:login` (or [`mindin login`](sign-in-a-machine.md)), then reconnect with `/mcp`, plugin:vault:vault, Reconnect. The hooks use the same key.

## Without the plugin

Add the server, here for a local folder:

```sh
claude mcp add vault -s user -e VAULT_PATH=$HOME/vault -- uvx mindin
VAULT_PATH=$HOME/vault uvx mindin init
```

For a bucket, pass `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` and `S3_BUCKET` in place of `VAULT_PATH` ([Storage](../install/storage.md)); for a server, `VAULT_SERVER_URL` after [signing the machine in](sign-in-a-machine.md). With no vault configured, the server still starts and lists its tools, and every tool call answers with how to set one up: `mindin setup`, or the variables above.

`init` writes `.vault/schema.yml` and the Obsidian Bases views into the vault, keeps anything that exists, and prints a CLAUDE.md snippet with the rules an agent follows on its side: `context` at session start, a note before the first edit, `log_append` at the end. Paste it into your CLAUDE.md. `init --preset para` starts from another [layout](../explanation/notes-and-layouts.md#layouts).

Then add the hooks to your Claude Code settings:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "VAULT_PATH=$HOME/vault uvx mindin hook session-start" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "VAULT_PATH=$HOME/vault uvx mindin hook stop" }] }
    ]
  }
}
```

[Hooks](../reference/hooks.md) describes what each prints and when `stop` reminds the agent.
