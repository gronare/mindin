---
description: Sign this machine in to the vault server, so the server, the hooks and the shell commands can reach it
argument-hint: [server URL]
---

Sign this machine in to the vault server with the device code flow of mindin. The plugin's configured server is `${user_config.vault_server_url}`.

1. Take the server URL from "$ARGUMENTS" when it is given, otherwise the configured one above. When neither names a URL (empty, or still a `${...}` placeholder), stop and tell the user to set "Vault server URL" under `/plugin` → Installed → vault → Configure, or to pass the URL: `/vault:login https://vault.example.com`.
2. Run `uvx mindin==0.15.2 login <url>` with the Bash tool in the background. It prints a link and a code, then waits up to ten minutes for the approval.
3. Read its output as soon as the code is there and show the user the link and the code in two lines; say they approve it in the browser, signed in to the server.
4. When the command exits, report its last line. On success, tell the user to reconnect the vault server with `/mcp` (plugin:vault:vault → Reconnect) so its tools load; the hooks use the key from the next session start on.

This command stores the key only in mindin's own credential file. Never write it anywhere else, never print it, and never change another tool's configuration. If the background run is not possible, tell the user to run `uvx mindin==0.15.2 login <url>` in a terminal themselves.
