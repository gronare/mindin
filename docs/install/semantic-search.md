# Search by meaning

Search by meaning is off by default. With it on, `search` also finds notes that share no word with the query: `inloggning` finds the note about the OAuth flow, and an English query reaches a Swedish note. [How search works](../explanation/search.md) describes the results.

It needs the `semantic` extra (the `fastembed` and `numpy` packages) and `VAULT_SEMANTIC=on`. With `VAULT_SEMANTIC=on` and the extra missing, the server logs one line saying so and searches by words.

## Turn it on

### Docker Compose

The `-semantic` image tag carries the extra; the plain tag leaves it out. Create `compose.override.yml` beside `compose.yml`:

```yaml
services:
  vault:
    image: ghcr.io/gronare/notes-vault-mcp:v0.12.0-semantic
    environment:
      VAULT_SEMANTIC: "on"
    mem_limit: 3g
```

Then `docker compose up -d`. Compose reads the override on top of `compose.yml`, so fetching a new `compose.yml` keeps it; change the tag here when you upgrade.

### Kubernetes

Set the image in `deploy/k8s/deployment.yaml` to the `-semantic` tag, add `VAULT_SEMANTIC: "on"` to the Secret, and raise the container's memory limit to at least `3Gi`.

### One machine, without a server

```sh
claude mcp add vault -s user \
  -e VAULT_PATH=$HOME/vault \
  -e VAULT_SEMANTIC=on \
  -- uvx --from 'notes-vault-mcp[semantic]' notes-vault-mcp
```

Any client works the same way: run `notes-vault-mcp[semantic]` and set `VAULT_SEMANTIC=on`.

## The model and Hugging Face

The model turns each section of a note into a vector. It comes from [Hugging Face](https://huggingface.co), a public hub where open machine-learning models are published, and runs on your machine through fastembed and ONNX Runtime.

- **Downloaded once.** The first time the model is needed, the server downloads it into `VAULT_CACHE_DIR/models` (`/data/cache/models` in the image). fastembed puts the size of the default model at 1.0 GB and of the smaller one at 0.22 GB. Later starts load it from that folder.
- **No account, no token, no note text sent.** The download is a plain fetch of public files. Your notes are never sent anywhere: the vectors are computed on the machine and stored in its index.
- **Without internet on the first start**, loading the model fails. The server logs `embedding notes failed, searching by words` once, keeps answering by words, and tries the model again on later refreshes; once it loads, the notes are embedded.
- **A machine that is always offline** can be given the model by copying the `models` folder from a machine that has downloaded the same model into its own `VAULT_CACHE_DIR/models`. fastembed looks there before it reaches the internet.

## Pick a model

`VAULT_SEMANTIC_MODEL` takes a fastembed model name.

| Model | Download | Notes |
| --- | --- | --- |
| `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` | 1.0 GB | The default. Multilingual. |
| `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 0.22 GB | About half the memory; finds fewer notes by meaning. Multilingual. |

These two are tested, and each has its own similarity cutoff below which a section does not count as a match. Any other fastembed text model loads with a generic cutoff of 0.3 and is untested here. Pick a multilingual model when your notes are not all in English.

Changing the model drops every vector and embeds every note again with the new one. Every session on a machine should name the same model, since a session asking for another one restarts the shared model process with its own.

## Memory and cost

Measured on an M2 Pro with a vault of 500 notes in 3,300 sections:

| | mpnet (default) | MiniLM |
| --- | --- | --- |
| Disk | 1 GB for the model, about 4 KB per section in the index (14 MB here) | 250 MB for the model, about 2 KB per section (7 MB here) |
| Shared process | 1.1 to 1.7 GB just after it starts, about 500 MB once the system has paged out what searches do not touch | About 900 MB just after it starts, about 160 MB later |
| Each server | About 35 MB more than searching by words | About 25 MB more |
| First sync | About four minutes (14 sections a second) | About a minute (60 sections a second) |
| Query | About 23 ms, against 11 ms by words alone; about 50 ms each with six sessions searching at once | About 20 ms; about 33 ms with six at once |

On a server the model process runs in the server's container, and the first sync is when it uses the most memory: loading mpnet peaks near 2 GB by itself, and embedding stays at about that peak with the defaults of `VAULT_EMBED_BATCH` and `VAULT_EMBED_MAX_TOKENS`. Give the container a memory limit of at least 3 GiB with mpnet; below that the kernel kills the process, and the server with it, while it embeds. A restarted server goes on from the notes that already carry vectors. Lowering `VAULT_EMBED_BATCH` lowers the peak; see [Configuration](../reference/configuration.md#search-by-meaning).

`notes-vault-mcp sync` embeds whatever is pending and says how many notes carry vectors; `sync --rebuild` computes them all again.

## Where the vectors live

Each vault has its own index, `VAULT_CACHE_DIR/<id>.sqlite`, holding every note's text and its vectors in plaintext. The model process is shared by every vault and session on the machine and stores nothing. On a server, whose vaults are [sealed](../explanation/encryption.md) in storage, put `VAULT_CACHE_DIR` on an encrypted volume.
