from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindin.backends import FETCH_WORKERS, SCHEMA_KEY, Entry, VaultBackend
from mindin.frontmatter import CLAIM_KEY, FrontmatterError, claim_fields, folder_of, parse, tags_of
from mindin.stemming import algorithms_of, stemmed

if TYPE_CHECKING:
    from mindin.embedding import Embedder
    from mindin.vectors import Vectors

SYNC_THROTTLE_SECONDS = 20
BUSY_TIMEOUT_MS = 30000
OPEN_RETRY_SECONDS = 60
OPEN_RETRY_PAUSE = 0.5
SHA_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}\b")
WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")
FIELD_TEXT_MAX = 10_000
CLOSE_WAIT_SECONDS = 10.0

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  title, summary, tags, body, stem, stemmed, key UNINDEXED,
  tokenize="unicode61 remove_diacritics 2"
);
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes (
  key TEXT PRIMARY KEY, version TEXT, mtime REAL, folder TEXT, stem TEXT,
  title TEXT, summary TEXT, status TEXT, kind TEXT, area TEXT,
  tags TEXT, paths TEXT, date TEXT, updated TEXT, superseded_by TEXT,
  valid INT, error TEXT, size INT, priority TEXT, source TEXT, claim TEXT, repos TEXT
);
CREATE INDEX IF NOT EXISTS notes_folder ON notes(folder);
CREATE INDEX IF NOT EXISTS notes_stem ON notes(stem);
CREATE TABLE IF NOT EXISTS shas (sha TEXT, key TEXT);
CREATE INDEX IF NOT EXISTS shas_sha ON shas(sha);
CREATE TABLE IF NOT EXISTS links (src TEXT, target TEXT);
CREATE INDEX IF NOT EXISTS links_target ON links(target);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS repos (repo TEXT PRIMARY KEY, root TEXT, seen_at TEXT);
"""

NOTE_COLUMNS = (
    "key, version, mtime, folder, stem, title, summary, status, kind, area, "
    "tags, paths, date, updated, superseded_by, valid, error, size, priority, source, claim, repos"
)
ADDED_COLUMNS = ("priority", "source", "claim", "repos")
FTS_COLUMNS = {"stem", "stemmed"}


@dataclass
class Note:
    key: str
    version: str = ""
    mtime: float = 0.0
    folder: str = ""
    stem: str = ""
    title: str = ""
    summary: str = ""
    status: str = ""
    kind: str = ""
    area: str = ""
    tags: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    date: str = ""
    updated: str = ""
    superseded_by: str = ""
    valid: bool = True
    error: str = ""
    size: int = 0
    priority: str = ""
    source: str = ""
    claim: str = ""
    repos: list[str] = field(default_factory=list)


def stem_of(key: str) -> str:
    return key.rsplit("/", 1)[-1].removesuffix(".md")


def link_targets(text: str) -> list[str]:
    targets = []
    for raw in WIKILINK_RE.findall(text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            targets.append(target.rsplit("/", 1)[-1].lower())
    return sorted(set(targets))


def shas_in(text: str) -> list[str]:
    return sorted(set(SHA_RE.findall(text)))


def expand_home(path: str) -> str:
    return str(Path(path).expanduser()) if path.startswith("~") else path


def path_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    values = raw if isinstance(raw, (list, tuple)) else str(raw).split(", ")
    paths: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        paths.append(text)
        expanded = expand_home(text)
        if expanded != text:
            paths.append(expanded)
    return paths


def text_list(raw: Any) -> list[str]:
    values = raw if isinstance(raw, (list, tuple)) else [] if raw is None else str(raw).split(",")
    return [text for value in values if (text := str(value).strip())]


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)[:FIELD_TEXT_MAX]
    return str(value)[:FIELD_TEXT_MAX]


def broken_note(key: str, version: str, mtime: float, size: int, error: str) -> Note:
    stem = stem_of(key)
    return Note(key, version, mtime, folder_of(key), stem, title=stem, valid=False, error=error, size=size)


def note_from_text(key: str, text: str, version: str, mtime: float, size: int) -> tuple[Note, str]:
    note = Note(key=key, version=version, mtime=mtime, size=size, folder=folder_of(key), stem=stem_of(key))
    try:
        frontmatter, body = parse(text)
    except FrontmatterError as exc:
        return broken_note(key, version, mtime, size, str(exc)), text
    note.title = _as_text(frontmatter.get("title")) or note.stem
    note.summary = _as_text(frontmatter.get("summary"))
    note.status = _as_text(frontmatter.get("status"))
    note.kind = _as_text(frontmatter.get("kind"))
    note.area = _as_text(frontmatter.get("area"))
    note.tags = tags_of(frontmatter)
    note.paths = path_list(frontmatter.get("path"))
    note.date = _as_text(frontmatter.get("date"))
    note.updated = _as_text(frontmatter.get("updated"))
    note.superseded_by = _as_text(frontmatter.get("superseded_by"))
    note.priority = _as_text(frontmatter.get("priority"))
    note.source = _as_text(frontmatter.get("source"))
    note.repos = text_list(frontmatter.get("repos"))
    claim = claim_fields(frontmatter.get(CLAIM_KEY))
    note.claim = json.dumps(claim, ensure_ascii=False) if claim else ""
    return note, body


def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        key=row["key"],
        version=row["version"] or "",
        mtime=row["mtime"] or 0.0,
        folder=row["folder"] or "",
        stem=row["stem"] or "",
        title=row["title"] or "",
        summary=row["summary"] or "",
        status=row["status"] or "",
        kind=row["kind"] or "",
        area=row["area"] or "",
        tags=json.loads(row["tags"] or "[]"),
        paths=json.loads(row["paths"] or "[]"),
        date=row["date"] or "",
        updated=row["updated"] or "",
        superseded_by=row["superseded_by"] or "",
        valid=bool(row["valid"]),
        error=row["error"] or "",
        size=row["size"] or 0,
        priority=row["priority"] or "",
        source=row["source"] or "",
        claim=row["claim"] or "",
        repos=json.loads(row["repos"] or "[]"),
    )


def _casefold(text: str | None) -> str | None:
    return text.casefold() if text else text


NOTE_TABLES = ("notes", "notes_fts", "shas", "links")


class Index:
    def __init__(
        self,
        path: Path,
        backend: VaultBackend,
        languages: tuple[str, ...] = (),
        embedder: Embedder | None = None,
        background: bool = False,
        on_settled: Callable[[], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.backend = backend
        self.languages = tuple(languages)
        self.algorithms = algorithms_of(self.languages)
        self.schema_version: str | None = None
        self.vectors: Vectors | None = None
        if embedder is not None:
            from mindin.vectors import Vectors

            self.vectors = Vectors(
                embedder, lambda: self.db, self._transaction, background=background, on_settled=on_settled
            )
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self._prepare_with_retry()

    @property
    def db(self) -> sqlite3.Connection:
        connection = getattr(self._local, "db", None)
        if connection is None:
            connection = sqlite3.connect(self.path, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
            connection.row_factory = sqlite3.Row
            connection.create_function("casefold", 1, _casefold, deterministic=True)
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self._local.db = connection
            with self._connections_lock:
                self._connections.append(connection)
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            db = self.db
            if db.in_transaction:
                yield db
                return
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            db.commit()

    def _prepare_with_retry(self) -> None:
        deadline = time.time() + OPEN_RETRY_SECONDS
        while True:
            try:
                self._prepare()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.time() >= deadline:
                    raise
                time.sleep(OPEN_RETRY_PAUSE)

    def _prepare(self) -> None:
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA_SQL + FTS_SQL)
        if self.vectors is not None:
            self.vectors.prepare(self.db)
        present = self._columns("notes")
        for column in ADDED_COLUMNS:
            if column not in present:
                self.db.execute(f"ALTER TABLE notes ADD COLUMN {column} TEXT")
                self._forget_notes()
        if not FTS_COLUMNS.issubset(self._columns("notes_fts")):
            self.db.execute("DROP TABLE notes_fts")
            self.db.executescript(FTS_SQL)
            self._forget_notes()
        if self.meta("languages") != ",".join(self.languages):
            self._forget_notes()
            self.set_meta("languages", ",".join(self.languages))
        self.db.commit()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}

    def _forget_notes(self, db: sqlite3.Connection | None = None) -> None:
        db = db or self.db
        for table in NOTE_TABLES:
            db.execute(f"DELETE FROM {table}")
        db.execute("DELETE FROM meta WHERE k = 'last_sync'")

    def close(self) -> None:
        if self.vectors is not None:
            self.vectors.closed = True
            self.vectors.wait(CLOSE_WAIT_SECONDS)
        with self._connections_lock:
            connections, self._connections = self._connections, []
        for connection in connections:
            connection.close()
        self._local = threading.local()

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._transaction() as db:
            db.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))

    def remember_repo(self, repo: str, root: str) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO repos(repo, root, seen_at) VALUES(?, ?, ?) "
                "ON CONFLICT(repo) DO UPDATE SET root = excluded.root, seen_at = excluded.seen_at",
                (repo, root, time.strftime("%Y-%m-%d")),
            )

    def forget_repo(self, repo: str) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM repos WHERE repo = ?", (repo,))

    def known_repos(self) -> list[tuple[str, str]]:
        rows = self.db.execute("SELECT repo, root FROM repos ORDER BY repo").fetchall()
        return [(row["repo"], row["root"]) for row in rows]

    def upsert(self, key: str, text: str, version: str, mtime: float, size: int) -> Note:
        try:
            note, body = note_from_text(key, text, version, mtime, size)
        except Exception as exc:
            note, body = broken_note(key, version, mtime, size, f"could not index: {exc}"), ""
        with self._transaction() as db:
            self._delete(db, key)
            self._insert(db, note, body, text)
        return note

    def _insert(self, db: sqlite3.Connection, note: Note, body: str, text: str) -> None:
        db.execute(
            f"INSERT INTO notes ({NOTE_COLUMNS}) VALUES ({', '.join('?' * 22)})",
            (
                note.key,
                note.version,
                note.mtime,
                note.folder,
                note.stem,
                note.title,
                note.summary,
                note.status,
                note.kind,
                note.area,
                json.dumps(note.tags),
                json.dumps(note.paths),
                note.date,
                note.updated,
                note.superseded_by,
                int(note.valid),
                note.error,
                note.size,
                note.priority,
                note.source,
                note.claim,
                json.dumps(note.repos),
            ),
        )
        db.execute(
            "INSERT INTO notes_fts(title, summary, tags, body, stem, stemmed, key) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (note.title, note.summary, " ".join(note.tags), body, note.stem, self._stemmed(note, body), note.key),
        )
        db.executemany("INSERT INTO shas(sha, key) VALUES(?, ?)", [(sha, note.key) for sha in shas_in(text)])
        db.executemany("INSERT INTO links(src, target) VALUES(?, ?)", [(note.key, t) for t in link_targets(text)])

    def _stemmed(self, note: Note, body: str) -> str:
        texts = (note.title, note.summary, " ".join(note.tags), body, note.stem)
        return "\n".join(stemmed("\n".join(texts), self.algorithms))

    def _delete(self, db: sqlite3.Connection, key: str) -> None:
        db.execute("DELETE FROM notes WHERE key = ?", (key,))
        db.execute("DELETE FROM notes_fts WHERE key = ?", (key,))
        db.execute("DELETE FROM shas WHERE key = ?", (key,))
        db.execute("DELETE FROM links WHERE src = ?", (key,))
        if self.vectors is not None:
            self.vectors.delete(db, key)

    def remove(self, key: str) -> None:
        with self._transaction() as db:
            self._delete(db, key)

    def note(self, key: str) -> Note | None:
        row = self.db.execute(f"SELECT {NOTE_COLUMNS} FROM notes WHERE key = ?", (key,)).fetchone()
        return _row_to_note(row) if row else None

    def all_notes(self) -> list[Note]:
        rows = self.db.execute(f"SELECT {NOTE_COLUMNS} FROM notes ORDER BY key").fetchall()
        return [_row_to_note(row) for row in rows]

    def notes_with_stem(self, stem: str) -> list[Note]:
        rows = self.db.execute(
            f"SELECT {NOTE_COLUMNS} FROM notes WHERE casefold(stem) = ? ORDER BY key", (stem.casefold(),)
        )
        return [_row_to_note(row) for row in rows.fetchall()]

    def bodies(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        marks = ", ".join("?" * len(keys))
        rows = self.db.execute(f"SELECT key, body FROM notes_fts WHERE key IN ({marks})", keys).fetchall()
        return {row["key"]: row["body"] or "" for row in rows}

    def stems(self) -> set[str]:
        return {row["stem"].lower() for row in self.db.execute("SELECT stem FROM notes")}

    def inbound_targets(self) -> set[str]:
        return {row["target"] for row in self.db.execute("SELECT DISTINCT target FROM links")}

    def keys_for_sha(self, prefix: str) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT key FROM shas WHERE sha LIKE ? || '%' OR ? LIKE sha || '%' ORDER BY key",
            (prefix, prefix),
        ).fetchall()
        return [row["key"] for row in rows]

    def _versions(self) -> dict[str, str]:
        return {row["key"]: row["version"] for row in self.db.execute("SELECT key, version FROM notes")}

    def _fetch(self, entries: list[Entry]) -> None:
        if not entries:
            return
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            fetched = list(pool.map(lambda entry: (entry, self._safe_get(entry.key)), entries))
        for entry, text in fetched:
            if text is None:
                continue
            self.upsert(entry.key, text, entry.version, entry.mtime.timestamp(), entry.size)

    def _safe_get(self, key: str) -> str | None:
        try:
            text, _ = self.backend.get(key)
        except Exception:
            return None
        return text

    def sync(self, force: bool = False) -> int:
        last = float(self.meta("last_sync") or 0.0)
        if not force and (time.time() - last) < SYNC_THROTTLE_SECONDS:
            self._refresh_vectors()
            return 0
        listing = self.backend.list()
        self.schema_version = next((entry.version for entry in listing if entry.key == SCHEMA_KEY), "")
        entries = [entry for entry in listing if entry.key.endswith(".md")]
        known = self._versions()
        seen = {entry.key for entry in entries}
        changed = [entry for entry in entries if known.get(entry.key) != entry.version]
        for gone in known.keys() - seen:
            self.remove(gone)
        self._fetch(changed)
        self.set_meta("last_sync", str(time.time()))
        self._refresh_vectors()
        return len(changed)

    def _refresh_vectors(self) -> None:
        if self.vectors is not None:
            self.vectors.refresh()

    def rebuild(self) -> int:
        with self._transaction() as db:
            self._forget_notes(db)
            if self.vectors is not None:
                self.vectors.forget(db)
        return self.sync(force=True)


def copy_index(source: Path, target: Path) -> None:
    from mindin.vectors import LEASE_META

    with (
        closing(sqlite3.connect(source, timeout=BUSY_TIMEOUT_MS / 1000)) as origin,
        closing(sqlite3.connect(target)) as copy,
    ):
        origin.backup(copy)
        copy.execute("DELETE FROM meta WHERE k IN ('last_sync', ?)", (LEASE_META,))
        copy.commit()


def remove_index(cache: Path, identifier: str) -> None:
    for path in cache.glob(f"{identifier}.sqlite*"):
        path.unlink(missing_ok=True)


def index_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000)) as db:
        for table in ("notes", "embedded"):
            try:
                rows = db.execute(f"SELECT key, version FROM {table} ORDER BY key").fetchall()
            except sqlite3.OperationalError:
                rows = []
            digest.update(f"{table}\0".encode())
            for key, version in rows:
                digest.update(f"{key}\0{version}\0".encode())
    return digest.hexdigest()
