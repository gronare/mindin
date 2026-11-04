# Run a server without Docker

The server is a Python package (Python 3.11 or later). Install it with uv or pip:

```sh
uv tool install mindin        # or: pip install mindin
```

Add the `semantic` extra (`mindin[semantic]`) for [search by meaning](semantic-search.md).

1. Set the settings in the environment. These are the ones it needs; [Configuration](../reference/configuration.md) lists the rest.

   ```sh
   export VAULT_PUBLIC_URL=https://vault.example.com
   export VAULT_HOSTED_MASTER_KEY=$(mindin master-key generate)
   export VAULT_PATH=/srv/notes-vault/vaults
   export VAULT_SMTP_HOST=smtp.example.com VAULT_SMTP_FROM="Vault <vault@example.com>" \
     VAULT_SMTP_USERNAME=... VAULT_SMTP_PASSWORD=...
   ```

   Keep the master key: once it has sealed a vault, the server refuses to start with any other. For a bucket in place of `VAULT_PATH`, see [Storage](storage.md).

2. Start it and invite yourself:

   ```sh
   mindin serve --transport http --host 127.0.0.1 --port 8765
   mindin accounts invite you@example.com
   ```

   `--host` defaults to `127.0.0.1` and `--port` to `8765`. Run `accounts` with the same environment as the server, so it opens the same `hosted.sqlite` under `VAULT_AUTH_DIR` (default `~/.cache/mindin/auth`).

3. Put TLS in front of it. `VAULT_PUBLIC_URL` must be `https://`, or `http://localhost` for a trial on your own machine. Any reverse proxy works; see [Behind a reverse proxy](../how-to/reverse-proxy.md).

4. Open `VAULT_PUBLIC_URL/account` and sign in.

The server loads the extensions installed beside it, from the Python entry point group `mindin.extensions`.
