# Connect claude.ai, Claude Desktop and mobile

Each connector reaches one vault on a [vault server](../install/docker.md).

1. Open `VAULT_PUBLIC_URL/account`, pick the vault, and copy its MCP address: `VAULT_PUBLIC_URL/v/<slug>/mcp`.
2. In claude.ai, open Settings, Connectors, Add custom connector, and paste the address. Name the connector after the vault.
3. The browser signs in to the server and asks for read, or read and write. Approve.

The connector reaches that vault and no other; add one connector per vault you want in claude.ai. There is nothing to register on the server: it takes claude.ai's client metadata document, and dynamic client registration for other clients.

Connect once on the web and the same connector appears in Claude Desktop and in the mobile app.

The vault's page lists the connector under Connections and keys, with when it was last used; disconnect it there.
