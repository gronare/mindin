# Back up and rotate keys

## Back up

A server's vaults are read with two things together, and with neither alone:

- The master key in `VAULT_HOSTED_MASTER_KEY`. Keep it outside the bucket and outside the server's machine.
- `hosted.sqlite` under `VAULT_AUTH_DIR` (`/data/auth` in the image), which holds every vault's key sealed by the master key, and the accounts, ways in, hashed keys and tokens, and registered clients.

Back up the vaults' storage as well: the folder in `VAULT_PATH` or the bucket. The search indexes under `VAULT_CACHE_DIR` are rebuilt from the vaults and need no backup.

Each person can also download a personal vault as a zip from its page at any time.

## Rotate the master key

1. Make a new key with `mindin master-key generate`.
2. Set the new key in `VAULT_HOSTED_MASTER_KEY` and the old one in `VAULT_HOSTED_MASTER_KEY_PREVIOUS`, and restart the server.
3. Wrap every vault key under the new one:

   ```sh
   mindin master-key rewrap
   mindin master-key status    # vault keys per master key
   ```

4. When `status` counts no vault key under the old key, remove `VAULT_HOSTED_MASTER_KEY_PREVIOUS` and restart.

`VAULT_HOSTED_MASTER_KEY_PREVIOUS` takes several keys, space or comma separated.
