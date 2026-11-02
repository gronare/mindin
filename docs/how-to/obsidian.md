# Open a server vault in Obsidian

A vault on a server opens in Obsidian through the Remotely Save plugin over WebDAV. A vault in a local folder is already an Obsidian vault: open the folder.

1. Get a password: make a key on the vault's page, or ask Claude for one, which calls the `obsidian_access` tool and shows the password once. Asking again replaces it.
2. In Remotely Save, choose WebDAV and set:
   - Address: `VAULT_PUBLIC_URL/dav/`
   - Username: your account's email
   - Password: the key from step 1
   - Depth: "only supports depth='1'"
3. Leave the base folder as it is: whatever name Remotely Save puts after `/dav/`, it syncs the vault itself.

The address is the same for every vault: the key decides which vault opens. A new vault's welcome note carries these steps.

Obsidian hides `.vault/`, so edit the schema with another WebDAV client, such as Finder's Connect to Server or Cyberduck. Other WebDAV clients open `VAULT_PUBLIC_URL/dav/vault/`.

A proxy in front of the server must pass `/dav/*` through; see [Behind a reverse proxy](reverse-proxy.md).
