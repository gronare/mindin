# Open a vault for support, or import one

The support commands open a vault only while its account allows it; whoever runs the server can still read every vault (see [Encryption](../explanation/encryption.md#support-access)). A person grants access to one vault on the account page, for an hour, a day or a week, and can end it early. Every access is logged with time, vault, operator and reason, and the log shows on the account page. Support access to a [shared vault](../explanation/shared-vaults.md) is the owner's to grant.

Each command names who runs it and why, and refuses without the grant. `--vault <slug>` names the vault when the account has more than one.

## Read a vault

```sh
notes-vault-mcp support files ann@example.com --vault workshop --operator sam --reason "lost note"
notes-vault-mcp support read ann@example.com Areas/home.md --operator sam --reason "lost note"
```

## Import a plaintext vault

`accounts import` seals every file of a plaintext vault into the account, from a folder or a bucket:

```sh
notes-vault-mcp accounts import ann@example.com --from-path ~/old-vault --operator sam --reason "move in"
notes-vault-mcp accounts import ann@example.com --from-s3-bucket old-vault --from-s3-prefix ann \
  --operator sam --reason "move in"
```

From a bucket it reads with `IMPORT_S3_ACCESS_KEY` and `IMPORT_S3_SECRET_KEY`, or the server's own `S3_ACCESS_KEY` and `S3_SECRET_KEY` when those are not set. `--from-s3-endpoint` and `--from-s3-region` name the old bucket's endpoint and region when they differ from the server's.

A person can import a zip into a vault themselves on its page; files with the same path are replaced.
