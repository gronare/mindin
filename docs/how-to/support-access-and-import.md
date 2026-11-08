# Open a vault for support, or import one

The support commands open a vault only while its account allows it; whoever runs the server can still read every vault (see [Encryption](../explanation/encryption.md#support-access)). The operator asks first, with a reason. The person sees the request on the Account page (`/account/settings`), and by email when the server sends email, then approves it for an hour, a day or seven days, or declines it. They can end a grant early, and it ends by itself when the time is up. Every access is logged with time, vault, operator and reason. The Support access box on the Account page shows while a request waits or a grant is open, and whenever the log has entries. Support access to a [shared vault](../explanation/shared-vaults.md) is the owner's to grant.

Each command names who runs it and why, and refuses without the grant. `--vault <slug>` names the vault when the account has more than one.

## Ask for access

```sh
mindin support request ann@example.com --vault workshop --operator sam --reason "lost note"
```

Without `--vault`, the person picks which of their vaults to open when they approve. An account has one request waiting at a time. `support requests` lists the requests that wait for an answer, and `support withdraw <account>` takes one back:

```sh
mindin support requests
mindin support withdraw ann@example.com
```

The email's subject is "Support asks to open your vault"; it carries the reason and a button to the Account page. Without email the request waits on the page until the person signs in.

## Read a vault

```sh
mindin support files ann@example.com --vault workshop --operator sam --reason "lost note"
mindin support read ann@example.com Areas/home.md --vault workshop --operator sam --reason "lost note"
```

## Import a plaintext vault

`accounts import` seals every file of a plaintext vault, from a folder or a bucket, into one of the account's personal vaults:

```sh
mindin accounts import ann@example.com --from-path ~/old-vault --operator sam --reason "move in"
mindin accounts import ann@example.com --from-s3-bucket old-vault --from-s3-prefix ann \
  --operator sam --reason "move in"
```

From a bucket it reads with `IMPORT_S3_ACCESS_KEY` and `IMPORT_S3_SECRET_KEY`, or the server's own `S3_ACCESS_KEY` and `S3_SECRET_KEY` when those are not set. `--from-s3-endpoint` and `--from-s3-region` name the old bucket's endpoint and region when they differ from the server's.

Before it writes, the import lists what it leaves behind (`.git/`, `.DS_Store`, `._*` files, `__MACOSX/`, `.trash/`, `Thumbs.db`, `desktop.ini`) and every file the server's [storage limits](../reference/extensions.md#hooks) refuse. One refused file stops the import with nothing written. `--dry-run` prints the same list and writes nothing.

A person can import a zip into a personal vault themselves on its page; files with the same path are replaced, and a file the server refuses stops the import before anything is written. A shared vault refuses both imports: its notes arrive through its connections, so each change is kept in its [history](../explanation/shared-vaults.md#history).
