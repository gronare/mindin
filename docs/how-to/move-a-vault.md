# Move a vault

`mindin move` copies the vault this machine uses to a new place, checks every file, and then makes the new place this machine's vault. The old place stays as it was, so nothing is lost if you change your mind.

```sh
mindin move --to --server https://vault.example.com
mindin move --to --folder ~/vault
mindin move --to --s3 <bucket> --endpoint <url> [--prefix <prefix>]
```

It moves personal vaults. An organisation's vaults move through its admin.

## From a folder onto a server

This gives you claude.ai, the phone and search by meaning for a vault you keep in a folder.

```sh
mindin move --to --server https://vault.example.com --dry-run
mindin move --to --server https://vault.example.com
```

If the machine is not signed in to the server yet, the move signs it in first, the way [`login`](sign-in-a-machine.md) does; `--vault <slug>` picks one of your vaults there.

Before it copies anything, it prints the plan:

```text
Moving the folder /Users/ann/vault to the vault notes on https://vault.example.com.
  1 243 files, 52 MB
  Left behind (--include-skipped brings them):
    .DS_Store
    .git/ (340 files)
  Replaces the new vault's .vault/schema.yml, Welcome.md with the source's own
  Refused by the vault notes on https://vault.example.com:
    Media/talk.mp4 is refused: this server stores only text/*, image/*, application/pdf, application/json, and the file is video/mp4
```

- **Left behind** are `.git/`, `.DS_Store`, `._*` files, `__MACOSX/`, `.trash/`, `Thumbs.db` and `desktop.ini`. `--include-skipped` brings them too.
- **Refused** are files the server will not store: a type it does not keep, or a file over its size limit. One refused file stops the move before anything is copied. `--skip-refused` moves the rest and lists the refused files again at the end, so you can keep them somewhere else. When the files take more than your account has room for, the move stops before copying too.
- A new vault on a server starts with a schema, a few views and a welcome note. Your vault's own schema replaces the server's; your own welcome note and views replace the server's, and the server's stay when you have none.

`--dry-run` prints the plan and copies nothing.

The copy shows how many files and bytes are done. If it stops, because the connection drops or you press Ctrl-C, run the same command again: files already there with the same content are skipped, and it goes on from there. Your machine keeps using the folder until the move is finished, also when the move started from `mindin setup --server`.

When every file is copied, the move reads each one back and compares its SHA-256 with the folder's. Only then does it save the server as this machine's vault.

## Into a vault that already has notes

A server vault, folder or bucket that holds files of its own is refused, with the files listed. `--merge` adds your vault to it: files with the same content are skipped, new files are copied, and the target's own files stay. A file that exists in both with different content stops the move, listed, with nothing copied.

## From a server to a folder or a bucket

```sh
mindin move --to --folder ~/vault
```

It reads the vault through the server with this machine's key, writes the folder, checks every file and switches the machine to the folder. Between a folder and a bucket, both ways, it copies directly.

Moving from one server to a new one works the same way: the machine reads the old server with the key it already has and signs in to the new one during the move.

## What does not move

A move copies the vault's files. These belong to the server, and you make them again on the new place:

- passkeys, which only work on the server they were made on
- API keys and WebDAV keys
- claude.ai connectors
- the history of shared vaults

## Reconnect afterwards

The move ends with what to change.

**Claude Code.** If the vault plugin's options name the old folder (`vault_path`), Claude Code keeps using it, since the plugin hands it to mindin. The move names the setting and asks whether to remove it; on a yes it removes it from `~/.claude/settings.json`, on a no it prints the change. Restart Claude Code afterwards.

**Codex and Cursor.** If you passed the vault in their config, such as `--env VAULT_PATH=...` for Codex ([Codex, Cursor and other clients](other-clients.md)), the move prints the file and the line to remove. It never edits them.

**Your shell.** If your shell profile sets `VAULT_PATH`, `VAULT_SERVER_URL` or `S3_*`, remove it; it decides over the saved place while it is set. `mindin status` shows which place decides.

**claude.ai and the phone.** Add the connector the move prints, `https://vault.example.com/mcp?vault=<slug>`, as in [Connect claude.ai](connect-claude-ai.md). Connectors to the old server keep reaching the old vault; remove them there.

**Obsidian.** On a server, keep your folder and sync it with Remotely Save over `https://vault.example.com/dav/` with a WebDAV key from your account page: [Open a server vault in Obsidian](obsidian.md). Moved to a folder, open that folder as a vault.

**Search.** The server indexes the notes as they arrive. Search by meaning catches up while the server computes the embeddings, which takes a while for a large vault.

## While setting up a server

`mindin setup --server <url>` on a machine whose vault is a folder or a bucket with notes asks whether to bring them along:

```text
Bring 1 243 files from the folder /Users/ann/vault along? [Y/n]
```

Yes runs the same move, and the machine switches to the server only once every file is copied and checked. If the move stops, the machine keeps using the folder, and running `mindin setup --server <url>` again offers the move again and skips the files already copied. No switches the machine to the server and leaves the folder as it was; `mindin move` brings it later. Without a terminal, setup switches to the server and says how many files stayed behind; to move them later, set the folder back with `mindin setup --folder <path>` and run `mindin move --to --server <url>`.
