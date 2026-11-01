# Changes

What each notes-vault-mcp release brings, one line per change, as you would tell someone who uses it. scripts/release.sh moves the Unreleased entries under the new version, and the GitHub release shows that version's entries.

## Unreleased

### What's new
- The HTTP server always serves accounts, one vault each, with an /account page. This breaks older setups: the --auth flag and the bearer, builtin, oidc and forwarded modes are removed, and so are the owner and tokens commands.
- Sign in with a passkey, an email link over SMTP, GitHub, your OpenID Connect provider or a trusted proxy's signed identity header. Each method is on when its settings are present, and an account can use several.
- The provider settings are called VAULT_OIDC_ISSUER, VAULT_OIDC_CLIENT_ID, VAULT_OIDC_CLIENT_SECRET and VAULT_OIDC_SCOPES. This breaks older setups: the VAULT_HOSTED_OIDC_* names are not read.
- Registration is by invitation, or open to any verified email with VAULT_REGISTRATION=open.
- Every account's vault is encrypted in storage, file contents and names, under its own key wrapped by a master key. This breaks older setups: the server needs VAULT_HOSTED_MASTER_KEY to start, made with notes-vault-mcp master-key generate.
- Encrypted vaults are stored under new paths, so an existing plaintext vault is not read. Bring it in with notes-vault-mcp accounts import, or as a zip on /account.
- master-key status and master-key rewrap rotate the master key, with the old one in VAULT_HOSTED_MASTER_KEY_PREVIOUS.
- An account exports its vault as a zip, imports one, and deletes itself on /account. notes-vault-mcp accounts delete does the same from the shell.
- An account can allow support access for an hour, a day or a week. The support files and support read commands and accounts import refuse without it, and every access shows in the account's log.
- Layout presets: init --preset para, simple, journal or custom, each with its folders, kinds, tags and Bases views. An account picks its layout on /account and can switch later.
- A schema can nest folders and is checked when loaded. One that does not hold leaves the vault on its preset, and context, lint and the account page list each problem.
- notes-vault-mcp login signs a machine in to a vault server through the device flow, and whoami and logout go with it. With VAULT_SERVER_URL set, the stdio server, the hooks and every command work through the server.
- The Claude Code plugin offers a vault server as a third choice beside a folder and a bucket.
- New tools on a server: whoami for every connection, and claim, release and changelog for a signed-in machine. context answers JSON with format json.
- lint --write and --park-stale run through a vault server when VAULT_SERVER_URL is set, so a scheduled lint can keep a hosted vault tidy.

### Fixed
- A repo log that log_append creates is titled Log — <repo>.
- A changelog period takes whole days at both ends, so it keeps commits from early on its first day and leaves out those from early on the next period's first day.

## 0.9.2

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
