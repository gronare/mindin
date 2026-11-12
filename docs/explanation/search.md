# Search

## By words

Ranking is BM25 with folder weights, recency decay and a status factor, so the living system note outranks a two-year-old archived note on the same words.

The index folds diacritics, stems every word in the vault's languages with Snowball (`äppelpressar` finds `äppelpressen`, `pressing` finds `presses`), and expands the schema's synonyms (`bokning` finds `booking`). A stemmed-only match weighs `stemmed_weight` (0.5), so the exact form ranks first.

- Every term is required. When no note has them all, the result holds the notes matching the most terms, and the header says so.
- A query that is exactly a note's stem, or `[[stem]]`, puts that note first, even from the archive.
- A bare commit sha finds the notes that mention it.
- Quoted phrases match as phrases.
- Archive and superseded notes are hidden unless asked for, and the header says how many.
- When nothing matches, the reply names the vault's languages so the client can translate the terms and search again.

## Across vaults

On a server with accounts, `search` without `vault` covers the connection's main vault and the organisation vaults the account belongs to. The header names them, and each row starts with the vault it comes from:

```text
5 of 5 (archive: 0 hidden, superseded: 0 hidden)
Searched 2 vaults: notes (Personal), wiki (Wiki, Acme). Each row starts with its vault; …
wiki | Resources/klarna.md | Adding Klarna | 3d | active | [[payments]] | The payments team added Klarna …
notes | Resources/klarna-0.md | Klarna notes | 2y | active | [[payments]] | …
```

`read_file(path, vault="wiki")` reads a row from another vault on the same connection, and `search(query, vault="wiki")` searches that vault alone. Writes go to the main vault unless the tool is given `vault`. A vault's name is its slug, which is unique among the vaults one account reaches; were two ever the same, the main vault and then the account's own vault answer it.

The account's other personal vaults and the shared vaults it belongs to are searched, read and written only when a tool names them: `search(query, vault="<slug>")` searches one of them alone. The server's instructions list every vault the connection reaches and say which ones search covers without a name.

Session start lists up to five pages about the repo from the organisation vaults, never from the account's other personal vaults or its shared vaults. The agent treats such a page as a lead, below the code and the user's latest decision: when one contradicts a new decision, it says so and proposes an updated page, written only on the user's yes.

Each vault ranks its own rows as above. The scores of two vaults are not comparable, since each index weighs words by how often its own notes use them, so the rows are interleaved by rank: every vault's first row comes before any vault's second row, and at the same rank the organisation's row comes first. Ten old personal notes on a subject therefore never push the team's guide below the second place. When one vault has rows matching every term, rows from vaults matching only some terms are left out.

The search spans vaults for claude.ai connectors, a trusted proxy's identity and machines signed in with `mindin login`. A key made on a vault's page keeps to that vault, and so does a server without accounts and a local vault. A connector or machine approved before the consent page said it reaches all the account's vaults reaches its own vault, the account's first vault and the organisation's vaults until it connects again; its search covers its own vault and the organisation's vaults.

## By meaning

With [search by meaning](../install/semantic-search.md) on, `search` also finds notes that share no word with the query. Exact names still win. The word ranking and the meaning ranking are fused; a note's stem still puts that note first, a sha still finds exactly the notes that mention it, and the filters and the archive and superseded hiding apply to both. The header counts the rows found by meaning alone (`4 of 9, 2 by meaning only`), and a row that matched one section of a long note names it: `§ OAuth-flödet · …`.

Each note is split at its headings, and each section gets a vector. The vectors live in the same SQLite index: computed for the notes whose version changed, dropped with the note, and computed again for every note when the model changes.

### The shared model process

The model runs in one background process per machine, shared by every session and command that uses the same `VAULT_CACHE_DIR`.

- The first one that needs it starts it. It keeps running when that session ends, and exits by itself once nobody has asked it anything for five minutes. The next search starts it again, waiting under a second.
- An HTTP server keeps it loaded for as long as the server runs, so its first search after a quiet spell finds by meaning too.
- If it crashes, the next search starts a new one.
- A session that never searches, with no new note to embed, never starts it.
- Its log is `VAULT_CACHE_DIR/embed.log`. It needs Unix sockets; without them the server says so once and searches by words.

A server starts embedding the notes still without vectors as soon as it starts, in the background, and searches by words meanwhile. A query is embedded beside a long embed, not behind it; one that would wait more than three seconds for the process, because it is still loading the model, is answered by words.
