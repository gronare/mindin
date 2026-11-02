# Encryption on a server

Every vault on a server is sealed before it reaches storage: file contents with AES-256-GCM, and file and folder names too, so the folder or bucket holds only opaque object names and ciphertext.

Each vault has its own data key, wrapped by the master key in `VAULT_HOSTED_MASTER_KEY` (base64 of 32 bytes, made with `notes-vault-mcp master-key generate`). The wrapped keys live in `hosted.sqlite`. The server refuses to start without the master key, or with one that does not open the vault keys it holds. Keep the master key outside the bucket: a vault is read with the master key and `hosted.sqlite` together, and with neither alone. [Back up and rotate keys](../how-to/back-up-and-rotate.md) covers both.

Deleting a vault destroys its key first, so its files cannot be read even before they are removed.

## What it protects

Sealing protects the contents and the names of every file in storage from whoever holds the storage: a bucket's provider, a storage admin, a stolen disk or a leaked bucket key reads only opaque object names and ciphertext. The sealed copy of a vault's search index that `VAULT_INDEX_SNAPSHOTS=on` keeps in storage is sealed the same way.

It does not protect against:

- **The server itself.** The server decrypts when it answers, so search, connectors and Obsidian over WebDAV see plain notes. It is not end-to-end encryption: whoever runs the server, holds its master key or reads its volume can read the vaults.
- **Anyone who can write the account store.** `hosted.sqlite`, or the database that holds the account store, decides who reaches which vault and holds the wrapped keys.
- **A storage admin who reverts or deletes objects.** Each file is sealed on its own, so an older version of a file put back, or a file removed, goes unnoticed. Turn on versioning for the bucket, so a rolled-back or deleted file can be recovered.
- **Traffic analysis.** Storage still sees each file's size, the length of its path, when it is written and how many files a vault holds.
- **The link to an embed worker.** Sections and queries cross to an `embed-worker` in plaintext; see [The model in its own process](../install/semantic-search.md#the-model-in-its-own-process).

## The search index is a plaintext copy

Each vault's local search index, `VAULT_CACHE_DIR/<id>.sqlite`, is a plaintext copy of the vault's text and its vectors for search by meaning. Put `VAULT_CACHE_DIR` on an encrypted volume, and treat any backup of it as you would the vault in plain markdown. See [Where the vectors live](../install/semantic-search.md#where-the-vectors-live).

## Support access

[Support access](../how-to/support-access-and-import.md) is an audit control, not a cryptographic one: the server can read every vault, and a grant decides only whether support opens one through the server, with every look logged.

A vault on one machine, in a folder or a bucket, is not sealed: it stays plain markdown.
