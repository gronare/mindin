# Changes

What each notes-vault-mcp release brings, one line per change, as you would tell someone who uses it. scripts/release.sh moves the Unreleased entries under the new version, and the GitHub release shows that version's entries.

## Unreleased

### What's new
- The claim field on a task note is called kebbie. This breaks older claims: a note that carries the previous key is not read as claimed.

### Fixed

## 0.9.1

### What's new
- Search by meaning on a server. A query is answered beside a running embed instead of waiting behind it, and the model stays warm while the HTTP server runs, so searches use vectors during a first sync. A server starts embedding notes without vectors as soon as it starts.
- Lower memory while embedding. VAULT_EMBED_BATCH (default 16), VAULT_EMBED_THREADS (half the usable cores, at most 4, counting a container's CPU limit) and VAULT_EMBED_MAX_TOKENS (default 256) bound the embedding process. The S3 connection pool matches the fetch workers.
- Hosted mode. --auth hosted serves one vault per account, with sign-in through an OIDC provider, invitations, an /account page and API keys. A machine can get its key through the OAuth device flow at /device. OAuth clients can identify themselves with a client ID metadata document. --plans on adds a plan per account; the default --plans off ignores plans.

### Fixed
- WebDAV refuses writes from credentials that may only read.
- Notes written through the claude.ai connector count as touched by the session-start hook.

## 0.9.0

### What's new
- Search by meaning. With VAULT_SEMANTIC=on and the [semantic] extra installed, search also finds notes that match the question without sharing its words. Results say how many matched by meaning only. The default model is mpnet; set VAULT_SEMANTIC_MODEL to pick MiniLM instead.
- One embedding process is shared by every session on a machine, so several agents searching at once load the model once.
- Two images: ghcr.io/gronare/notes-vault-mcp:v0.9.0 without the model, and :v0.9.0-semantic with it.
- The README is rewritten around what the server does and how to start it.

## 0.8.0

### What's new
- A task note can carry a claim field naming the machine and task working on it. The server keeps the field as stored when a note is written, so an agent can claim a note without another machine overwriting the claim.
- Every release checks that the tag matches the package version before anything is published.
