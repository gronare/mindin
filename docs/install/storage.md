# Storage

A vault lives in a local folder or in an S3 bucket. The same two settings serve one machine and a server; on a server they hold every account's vaults.

## A local folder

Set `VAULT_PATH` to a directory. It is made when missing.

- On one machine, the folder is the vault: plain `.md` files you can open in Obsidian, keep in git or read with `cat`.
- On a server, the folder holds every vault, each under `VAULT_SUBJECT_PREFIX/<vault id>/` (default `users/`), sealed. The Docker example uses `VAULT_PATH=/data/vaults`, inside the container's volume.

`VAULT_PATH` wins over the `S3_*` settings when both are set.

## An S3 bucket

Set all four of these; with any one missing, the bucket is not used:

| Variable | Example |
| --- | --- |
| `S3_ENDPOINT` | `https://s3.example.com` |
| `S3_BUCKET` | `vault` |
| `S3_ACCESS_KEY` | |
| `S3_SECRET_KEY` | |

`S3_PREFIX` puts the vault under a key prefix inside the bucket. `S3_REGION` defaults to `us-east-1`; some providers want another value, usually the bucket's region. `S3_ENDPOINT` is always needed, also for AWS S3, where it is the regional endpoint such as `https://s3.eu-north-1.amazonaws.com`.

Any S3-compatible store works that supports the calls below, for example AWS S3 or MinIO.

### The bucket

Create the bucket before the first start: the server never creates one. Give its key these permissions on that bucket:

| Permission | Used for |
| --- | --- |
| `s3:ListBucket` | Listing the vault (`ListObjectsV2`). |
| `s3:GetObject` | Reading a note (`GetObject`, `HeadObject`). |
| `s3:PutObject` | Writing a note (`PutObject`), and the copy half of a move on one machine (`CopyObject`). |
| `s3:DeleteObject` | Deleting a note, and the other half of a move. |

A write that must not overwrite a newer version sends `If-Match` or `If-None-Match: *` with the `PutObject`. A store that refuses conditional writes gets a `HeadObject` before the write instead, and the server logs that once.

### One server per bucket

Run one server per bucket. A server keeps each sealed vault's listing for five seconds, so writes from a second server to the same vault are seen late. The Kubernetes Deployment runs one replica for this reason.

## What is stored

On one machine, the folder or bucket holds the notes as written: markdown files under their own names.

On a server, every vault is sealed before it reaches storage: file contents with AES-256-GCM, and file and folder names too, so the folder or bucket holds only opaque object names and ciphertext. [Encryption](../explanation/encryption.md) explains the keys and what sealing protects. Turn on versioning for the bucket: sealing does not notice a file put back to an older version or removed, and versioning lets you recover it.

Each machine or server also keeps a search index per vault under `VAULT_CACHE_DIR`, which is not sealed; see [Where the vectors live](semantic-search.md#where-the-vectors-live).
