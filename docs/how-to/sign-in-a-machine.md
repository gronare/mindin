# Sign a machine in to a server

Claude Code, Codex, the hooks, kebbie and the command line reach a vault server through a key stored on the machine.

## Sign in

```sh
uvx mindin login https://vault.example.com --vault notes
```

1. It prints a link and a code. Open the link on any device and sign in.
2. Check that the code matches, and approve read and write. Without `--vault` the approval page asks which vault the machine gets.
3. Set `VAULT_SERVER_URL=https://vault.example.com` where your clients run.

To bring a vault you keep in a folder onto the server, see [Move a vault](move-a-vault.md).

The machine gets its own key, named after its host name (`--name` picks another), stored in `~/.config/mindin/credentials.json` (under `XDG_CONFIG_HOME` when set) and readable only by you. Over ssh it works the same way, and `--no-browser` only prints the link.

With `VAULT_SERVER_URL` set, the stdio MCP server, the hooks and every command work through the server as with a folder or a bucket, except `sync`, since the server keeps the index. `VAULT_SERVER_URL` wins over `VAULT_PATH` and `S3_*` when both are set. Until the machine is signed in, the server's instructions tell the agent to ask for `login`.

## Check and sign out

```sh
mindin whoami                       # account, vault, whether the key may write; for a shared vault, the role
mindin logout [--vault <slug>]      # forget the keys on this machine
```

Revoke a key for good on its vault's page.

## Several vaults on one machine

Run `login --vault <slug>` once for each vault. The first one becomes the machine's default. A folder, such as a repo, can use another vault, and so can everything below it:

```sh
mindin login https://vault.example.com --vault work
mindin vault use work --dir ~/projects/client-repo   # this folder and below use work
mindin vault use notes                              # the default everywhere else
mindin vault drop --dir ~/projects/client-repo      # back to the default
mindin vault list                                   # JSON: the vault here, the default, the folders
```

The hooks, the stdio MCP server and every command pick the vault of the folder they run in; the hooks take it from the session's working directory. The keys and folders are kept in `credentials.json`. `--url` on `vault use` and `vault drop` names the server; without it they take `VAULT_SERVER_URL`.

A tool that signs itself in without a person at a terminal uses the [device flow](../reference/device-flow.md).
