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

## By meaning

With [search by meaning](../install/semantic-search.md) on, `search` also finds notes that share no word with the query. Exact names still win. The word ranking and the meaning ranking are fused; a note's stem still puts that note first, a sha still finds exactly the notes that mention it, and the filters and the archive and superseded hiding apply to both. The header counts the rows found by meaning alone (`4 of 9, 2 by meaning only`), and a row that matched one section of a long note names it: `§ OAuth-flödet · …`.

Each note is split at its headings, and each section gets a vector. The vectors live in the same SQLite index: computed for the notes whose version changed, dropped with the note, and computed again for every note when the model changes.

### The shared model process

The model runs in one background process per machine, shared by every server and command that uses the same `VAULT_CACHE_DIR`, however many sessions are open.

- The first one that needs it starts it. It keeps running when that session ends, and exits by itself once nobody has asked it anything for five minutes. The next search starts it again, waiting under a second.
- An HTTP server keeps it loaded for as long as the server runs, so its first search after a quiet spell finds by meaning too.
- If it crashes, the next search starts a new one.
- A session that never searches, with no new note to embed, never starts it.
- Its log is `VAULT_CACHE_DIR/embed.log`. It needs Unix sockets; without them the server says so once and searches by words.

A server starts embedding the notes still without vectors as soon as it starts, in the background, and searches by words meanwhile. A query is embedded beside a long embed, not behind it; one that would wait more than three seconds for the process, because it is still loading the model, is answered by words.
