# Connect claude.ai, Claude Desktop and mobile

Each connector opens one vault on a [vault server](../install/docker.md), its main vault, and reaches the account's other vaults by name.

1. Open `VAULT_PUBLIC_URL/account`, pick the vault, and copy its MCP address: `VAULT_PUBLIC_URL/mcp?vault=<slug>`.
2. In claude.ai, open Settings, Connectors, Add custom connector, and paste the address. Name the connector after the vault.
3. The browser signs in to the server and asks for read, or read and write. Approve.

Tools work in that vault unless Claude names another with `vault`, as search rows name them; one connector is enough for all your vaults. A connector approved before the consent page said it reaches all your vaults keeps to its own vault and the organisation's vaults until you connect it again. There is nothing to register on the server: it takes claude.ai's client metadata document, and dynamic client registration for other clients.

Connect once on the web and the same connector appears in Claude Desktop and in the mobile app.

The vault's page lists the connector under Connections and keys, with when it was last used; disconnect it there.
