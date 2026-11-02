from __future__ import annotations

import logging
import re
import shlex
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from notes_vault_mcp.index import Index, Note
from notes_vault_mcp.schema import Schema
from notes_vault_mcp.stemming import algorithms_of, stemmed

log = logging.getLogger(__name__)

SHA_TOKEN_RE = re.compile(r"^(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}$")
PREFIX_MIN_LENGTH = 4
SNIPPET_LENGTH = 160
STATUS_FACTORS = {"active": 1.0, "draft": 0.9, "complete": 0.7, "superseded": 0.3}
DAY_SECONDS = 86400.0
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
FUSION_K = 10
MEANING_WEIGHT = 0.8
MEANING_ROWS = 20


class QueryError(ValueError):
    pass


def since_day(since: str | None) -> str | None:
    if not since:
        return None
    try:
        return date.fromisoformat(since.strip()).isoformat()
    except ValueError as exc:
        raise QueryError(f"since must be an ISO date (YYYY-MM-DD), not {since!r}") from exc


def _fts(index: Index, sql: str, expression: str) -> list[sqlite3.Row]:
    try:
        return index.db.execute(sql, (expression,)).fetchall()
    except sqlite3.OperationalError as exc:
        raise QueryError(f"the search index refused the query: {exc}") from exc


@dataclass
class Row:
    note: Note
    score: float
    snippet: str = ""
    terms: int = 0
    section: str = ""
    by_meaning: bool = False


@dataclass
class SearchResult:
    rows: list[Row] = field(default_factory=list)
    total: int = 0
    hidden_archive: int = 0
    hidden_superseded: int = 0
    by_meaning: int = 0
    term_count: int = 0
    best_terms: int = 0
    languages: tuple[str, ...] = ()

    @property
    def partial(self) -> bool:
        return self.term_count > 0


def terms_of(query: str) -> list[tuple[str, bool]]:
    query = CONTROL_RE.sub(" ", query)
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()
    terms: list[tuple[str, bool]] = []
    for token in tokens:
        text = token.strip()
        if text:
            terms.append((text, " " in text))
    return terms


def _quote(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _prefixed(word: str, is_phrase: bool) -> bool:
    return not is_phrase and len(word) >= PREFIX_MIN_LENGTH


def _stemmed_alternatives(words: list[str], is_phrase: bool, schema: Schema) -> list[str]:
    algorithms = algorithms_of(schema.languages)
    covered = {word.casefold() for word in words if _prefixed(word, is_phrase)}
    renderings = dict.fromkeys(rendering for word in words for rendering in stemmed(word, algorithms))
    return [f"stemmed : {_quote(rendering)}" for rendering in renderings if rendering not in covered]


def _alternatives(term: str, is_phrase: bool, schema: Schema) -> list[str]:
    words = [term, *schema.synonyms_of(term)]
    alternatives: list[str] = []
    for word in words:
        alternatives.append(_quote(word))
        if _prefixed(word, is_phrase):
            alternatives.append(f"{_quote(word)} *")
    return alternatives + _stemmed_alternatives(words, is_phrase, schema)


def term_groups(query: str, schema: Schema) -> list[str]:
    return ["(" + " OR ".join(_alternatives(term, is_phrase, schema)) + ")" for term, is_phrase in terms_of(query)]


def match_expression(query: str, schema: Schema) -> str:
    return " AND ".join(term_groups(query, schema))


def stem_query(query: str) -> str | None:
    terms = terms_of(query)
    if len(terms) != 1:
        return None
    text = terms[0][0].strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].split("|", 1)[0].split("#", 1)[0]
    stem = text.strip().rsplit("/", 1)[-1].removesuffix(".md")
    return stem or None


def parse_date(stamp: str) -> date | None:
    try:
        year, month, day = (int(part) for part in stamp[:10].split("-"))
        return date(year, month, day)
    except ValueError:
        return None


def parse_day(stamp: str) -> float | None:
    day = parse_date(stamp)
    return None if day is None else datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()


def age_days(note: Note, now: float) -> float:
    day = parse_date(note.updated or note.date)
    if day is not None:
        return float(max(0, (date.fromtimestamp(now) - day).days))
    if note.mtime:
        return max(0.0, (now - note.mtime) / DAY_SECONDS)
    return 0.0


def humanize_age(days: float) -> str:
    whole = int(days)
    if whole < 30:
        return f"{whole}d"
    if whole < 365:
        return f"{whole // 30}mo"
    return f"{whole // 365}y"


def recency_factor(days: float, half_life: float) -> float:
    return 0.5 + 0.5 * (2.0 ** (-days / half_life))


def status_factor(status: str) -> float:
    return STATUS_FACTORS.get(status, 1.0)


def score_of(note: Note, base: float, schema: Schema, now: float) -> float:
    weight = schema.folder_weight(schema.folder_of(note.key))
    recency = recency_factor(age_days(note, now), schema.recency_half_life_days)
    return base * weight * recency * status_factor(note.status)


def _clean_snippet(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:SNIPPET_LENGTH]


def _is_superseded(note: Note) -> bool:
    return bool(note.superseded_by) or note.status == "superseded"


def _matches_filters(
    note: Note,
    folder: str | None,
    status: str | None,
    tag: str | None,
    kind: str | None,
    area: str | None,
    path_prefix: str | None,
    since: str | None,
) -> bool:
    if folder and not note.key.startswith(f"{folder.strip('/')}/"):
        return False
    if status and note.status != status:
        return False
    if tag and tag not in note.tags:
        return False
    if kind and note.kind != kind:
        return False
    if area and area.lower() not in note.area.lower():
        return False
    if path_prefix and not any(path.startswith(path_prefix) or path_prefix.startswith(path) for path in note.paths):
        return False
    return not (since and (note.updated or note.date) < since)


def _summary_row(note: Note) -> Row:
    return Row(note=note, score=1.0, snippet=_clean_snippet(note.summary))


def _fts_rows(index: Index, schema: Schema, query: str) -> list[Row]:
    expression = match_expression(query, schema)
    if not expression:
        return [_summary_row(note) for note in index.all_notes()]
    return _fts_query(index, schema, expression)


def _fts_query(index: Index, schema: Schema, expression: str) -> list[Row]:
    weights = ", ".join(str(weight) for weight in (*schema.bm25_weights, 0.0))
    sql = f"""
        SELECT notes_fts.key AS key,
               -bm25(notes_fts, {weights}) AS base,
               snippet(notes_fts, 3, '', '', '…', 20) AS snip
        FROM notes_fts WHERE notes_fts MATCH ?
    """
    rows = _fts(index, sql, expression)
    found: list[Row] = []
    for row in rows:
        note = index.note(row["key"])
        if note is not None:
            found.append(
                Row(note=note, score=max(row["base"], 0.0001), snippet=_clean_snippet(row["snip"] or note.summary))
            )
    return found


def _matching_keys(index: Index, expression: str) -> set[str]:
    rows = _fts(index, "SELECT key FROM notes_fts WHERE notes_fts MATCH ?", expression)
    return {row["key"] for row in rows}


def _partial_rows(index: Index, schema: Schema, query: str) -> list[Row]:
    groups = term_groups(query, schema)
    found = _fts_query(index, schema, " OR ".join(groups))
    matched = [_matching_keys(index, group) for group in groups]
    for row in found:
        row.terms = sum(row.note.key in keys for keys in matched)
    return found


def _sha_rows(index: Index, query: str) -> list[Row] | None:
    terms = terms_of(query)
    if len(terms) != 1 or terms[0][1] or not SHA_TOKEN_RE.match(terms[0][0]):
        return None
    found = []
    for key in index.keys_for_sha(terms[0][0]):
        note = index.note(key)
        if note is not None:
            found.append(_summary_row(note))
    return found


def _candidates(index: Index, schema: Schema, query: str) -> tuple[list[Row], bool, bool]:
    found = _sha_rows(index, query)
    if found is not None:
        return found, False, True
    found = _fts_rows(index, schema, query)
    if found or len(terms_of(query)) < 2:
        return found, False, False
    return _partial_rows(index, schema, query), True, False


def _meaning_rows(index: Index, query: str) -> list[Row]:
    if index.vectors is None or not terms_of(query):
        return []
    try:
        hits = index.vectors.nearest(query)
    except Exception as exc:
        log.warning("notes-vault-mcp: searching by meaning failed, searching by words only: %s", exc)
        return []
    found = []
    for hit in hits:
        note = index.note(hit.key)
        if note is not None:
            snippet = _clean_snippet(note.summary)
            found.append(Row(note=note, score=hit.similarity, snippet=snippet, section=hit.heading))
    return found


def _fused(lexical: list[Row], meaning: list[Row], term_count: int) -> list[Row]:
    fused: dict[str, float] = {}
    rows: dict[str, Row] = {}
    for rank, row in enumerate(lexical, 1):
        share = row.terms / term_count if term_count else 1.0
        fused[row.note.key] = share / (FUSION_K + rank)
        rows[row.note.key] = row
    for rank, row in enumerate(meaning, 1):
        key = row.note.key
        fused[key] = fused.get(key, 0.0) + MEANING_WEIGHT / (FUSION_K + rank)
        if key in rows:
            rows[key].section = row.section
        else:
            row.by_meaning = True
            rows[key] = row
    return sorted(rows.values(), key=lambda row: (-fused[row.note.key], row.note.key))


def _stem_rows(index: Index, query: str, candidates: list[Row]) -> list[Row]:
    stem = stem_query(query)
    if stem is None:
        return []
    by_key = {row.note.key: row for row in candidates}
    return [by_key.get(note.key) or _summary_row(note) for note in index.notes_with_stem(stem)]


def search(
    index: Index,
    schema: Schema,
    query: str,
    limit: int = 15,
    folder: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    kind: str | None = None,
    area: str | None = None,
    include_archive: bool = False,
    include_superseded: bool = False,
    path_prefix: str | None = None,
    since: str | None = None,
) -> SearchResult:
    since = since_day(since)
    candidates, partial, by_sha = _candidates(index, schema, query)
    pinned = _stem_rows(index, query, candidates)
    pinned_keys = {row.note.key for row in pinned}
    lexical_keys = {row.note.key for row in candidates}
    meaning = [] if by_sha else [row for row in _meaning_rows(index, query) if row.note.key not in lexical_keys]

    def wanted(note: Note) -> bool:
        return _matches_filters(note, folder, status, tag, kind, area, path_prefix, since)

    now = time.time()
    unsearched = set(schema.unsearched_folders)
    result = SearchResult(languages=schema.languages)

    def shown(rows: list[Row], counted: bool = True) -> list[Row]:
        kept: list[Row] = []
        for row in rows:
            note = row.note
            if note.key in pinned_keys or not wanted(note):
                continue
            if schema.folder_of(note.key) in unsearched and not include_archive:
                result.hidden_archive += counted
                continue
            if _is_superseded(note) and not include_superseded:
                result.hidden_superseded += counted
                continue
            row.score = score_of(note, row.score, schema, now)
            kept.append(row)
        return kept

    visible = shown(candidates)
    visible.sort(key=lambda row: (-row.terms, -row.score, row.note.key))
    close = sorted(shown(meaning, counted=False), key=lambda row: (-row.score, row.note.key))[:MEANING_ROWS]
    if close:
        visible = _fused(visible, close, len(terms_of(query)) if partial else 0)
    visible = [row for row in pinned if wanted(row.note)] + visible
    if partial and any(row.terms for row in visible):
        result.term_count = len(terms_of(query))
        result.best_terms = max(row.terms for row in visible)
    result.total = len(visible)
    result.by_meaning = sum(row.by_meaning for row in visible)
    result.rows = visible[:limit]
    return result


def render_row(row: Row, now: float) -> str:
    note = row.note
    age = humanize_age(age_days(note, now))
    area = note.area or "-"
    status = note.status or "-"
    snippet = f"§ {row.section} · {row.snippet}" if row.section else row.snippet
    return f"{note.key} | {note.title} | {age} | {status} | {area} | {snippet}"


def render_header(result: SearchResult) -> str:
    hidden = f"(archive: {result.hidden_archive} hidden, superseded: {result.hidden_superseded} hidden)"
    meaning = f", {result.by_meaning} by meaning only" if result.by_meaning else ""
    if result.partial:
        terms, some = result.term_count, result.total - result.by_meaning
        return f"0 matched all {terms} terms; {some} match some (best {result.best_terms} of {terms}){meaning} {hidden}"
    return f"{len(result.rows)} of {result.total}{meaning} {hidden}"


def translate_hint(languages: tuple[str, ...]) -> str:
    if not languages:
        return ""
    primary, others = languages[0], ", ".join(languages[1:])
    if not others:
        return f"The vault is written in {primary}: search again with other words or the terms in {primary}."
    return f"The vault is written in {primary}, also {others}: search again with the terms translated into {others}."


def partial_hint(languages: tuple[str, ...]) -> str:
    if len(languages) < 2:
        return ""
    return f"The terms translated into {', '.join(languages[1:])} may match them all."


def _hidden(result: SearchResult) -> int:
    return result.hidden_archive + result.hidden_superseded


def render(result: SearchResult) -> str:
    now = time.time()
    lines = [render_header(result)]
    if not result.rows:
        lines.append("No notes matched.")
        if not _hidden(result):
            lines.append(translate_hint(result.languages))
    else:
        lines.extend(render_row(row, now) for row in result.rows)
        if result.partial:
            lines.append(partial_hint(result.languages))
    return "\n".join(line for line in lines if line)
