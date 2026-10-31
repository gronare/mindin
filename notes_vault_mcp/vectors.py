from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from notes_vault_mcp.embedding import Embedder

log = logging.getLogger(__name__)

VECTOR_SQL = """
CREATE TABLE IF NOT EXISTS chunks (key TEXT, ord INT, heading TEXT, vector BLOB);
CREATE INDEX IF NOT EXISTS chunks_key ON chunks(key);
CREATE TABLE IF NOT EXISTS embedded (key TEXT PRIMARY KEY, version TEXT);
"""
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
WINDOW_WORDS = 90
NOTES_PER_BATCH = 16
MODEL_META = "embed_model"
GENERATION_META = "vectors_generation"
LEASE_META = "embed_lease"
LEASE_SECONDS = 120.0


@dataclass(frozen=True)
class Chunk:
    heading: str
    text: str


@dataclass(frozen=True)
class Hit:
    key: str
    heading: str
    similarity: float


def sections(body: str) -> list[tuple[list[str], list[str]]]:
    found: list[tuple[list[str], list[str]]] = [([], [])]
    trail: list[tuple[int, str]] = []
    fenced = False
    for line in body.splitlines():
        if FENCE_RE.match(line):
            fenced = not fenced
        heading = None if fenced else HEADING_RE.match(line)
        if heading is None:
            found[-1][1].append(line)
            continue
        level = len(heading.group(1))
        trail = [(depth, text) for depth, text in trail if depth < level] + [(level, heading.group(2).strip())]
        found.append(([text for _, text in trail], []))
    return found


def chunks_of(title: str, summary: str, body: str) -> list[Chunk]:
    found: list[Chunk] = []
    for trail, lines in sections(body):
        words = " ".join(lines).split()
        if not trail:
            words = [*summary.split(), *words]
        label = " > ".join(trail)
        for start in range(0, len(words), WINDOW_WORDS):
            window = " ".join(words[start : start + WINDOW_WORDS])
            text = "\n".join(part for part in (title, label, window) if part)
            found.append(Chunk(heading=trail[-1] if trail else "", text=text))
    return found or [Chunk(heading="", text=title)]


class Vectors:
    def __init__(
        self,
        embedder: Embedder,
        db: Callable[[], sqlite3.Connection],
        transaction: Callable[[], AbstractContextManager[sqlite3.Connection]],
        background: bool = False,
    ) -> None:
        self.embedder = embedder
        self._db = db
        self._transaction = transaction
        self.background = background
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._matrix: Any = None
        self._rows: list[tuple[str, str]] = []
        self._generation: str | None = None
        self.closed = False
        self._failing = False

    def prepare(self, db: sqlite3.Connection) -> None:
        db.executescript(VECTOR_SQL)

    def forget(self, db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM chunks")
        db.execute("DELETE FROM embedded")
        self._bump(db)

    def delete(self, db: sqlite3.Connection, key: str) -> None:
        db.execute("DELETE FROM chunks WHERE key = ?", (key,))
        db.execute("DELETE FROM embedded WHERE key = ?", (key,))
        self._bump(db)

    @staticmethod
    def _bump(db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO meta(k, v) VALUES(?, '1') ON CONFLICT(k) DO UPDATE SET v = CAST(v AS INTEGER) + 1",
            (GENERATION_META,),
        )

    def _meta(self, key: str) -> str | None:
        row = self._db().execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row[0] if row else None

    def refresh(self) -> None:
        if self.closed or not self._needs_work():
            return
        if not self.background:
            self._embed_logged()
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._embed_logged, name="vault-vectors", daemon=True)
            self._worker.start()

    def wait(self, timeout: float | None = None) -> None:
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def _embed_logged(self) -> None:
        try:
            self.embed_pending()
        except Exception as exc:
            if not self.closed and not self._failing:
                log.warning("notes-vault-mcp: embedding notes failed, searching by words: %s", exc)
            self._failing = True
        else:
            self._failing = False

    def embed_pending(self) -> int:
        self._drop_stale()
        done = 0
        try:
            while not self.closed and self._take_lease():
                batch = self._pending(NOTES_PER_BATCH)
                if not batch:
                    break
                self._embed(batch)
                done += len(batch)
        finally:
            self._release_lease()
        return done

    def _take_lease(self) -> bool:
        mine = str(os.getpid())
        with self._transaction() as db:
            held = (self._meta(LEASE_META) or "").split()
            if len(held) == 2 and held[0] != mine and time.time() - float(held[1]) < LEASE_SECONDS:
                return False
            db.execute(
                "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (LEASE_META, f"{mine} {time.time()}"),
            )
        return True

    def _release_lease(self) -> None:
        if self.closed:
            return
        with self._transaction() as db:
            db.execute("DELETE FROM meta WHERE k = ? AND v LIKE ? || ' %'", (LEASE_META, str(os.getpid())))

    def _needs_work(self) -> bool:
        return self._model_changed() or bool(self._stale()) or bool(self._pending(1))

    def _model_changed(self) -> bool:
        return self._meta(MODEL_META) != self.embedder.model_id

    def _stale(self) -> list[str]:
        rows = self._db().execute(
            "SELECT e.key FROM embedded e LEFT JOIN notes n ON n.key = e.key AND n.version = e.version "
            "WHERE n.key IS NULL"
        )
        return [row[0] for row in rows.fetchall()]

    def _drop_stale(self) -> None:
        if not self._model_changed() and not self._stale():
            return
        with self._transaction() as db:
            if self._model_changed():
                self.forget(db)
                db.execute(
                    "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (MODEL_META, self.embedder.model_id),
                )
            for key in self._stale():
                self.delete(db, key)

    def _pending(self, limit: int) -> list[tuple[str, str, str, str, str]]:
        return (
            self._db()
            .execute(
                "SELECT n.key, n.version, n.title, n.summary, f.body FROM notes n "
                "JOIN notes_fts f ON f.key = n.key "
                "LEFT JOIN embedded e ON e.key = n.key AND e.version = n.version "
                "WHERE e.key IS NULL ORDER BY n.key LIMIT ?",
                (limit,),
            )
            .fetchall()
        )

    def _embed(self, batch: list[tuple[str, str, str, str, str]]) -> None:
        planned = [
            (key, version, chunks_of(title or "", summary or "", body or ""))
            for key, version, title, summary, body in batch
        ]
        texts = [chunk.text for _, _, chunks in planned for chunk in chunks]
        matrix = self.embedder.passages(texts)
        with self._transaction() as db:
            offset = 0
            for key, version, chunks in planned:
                vectors = matrix[offset : offset + len(chunks)]
                offset += len(chunks)
                current = db.execute("SELECT version FROM notes WHERE key = ?", (key,)).fetchone()
                if current is None or current[0] != version:
                    continue
                self.delete(db, key)
                rows = [
                    (key, position, chunk.heading, vector.tobytes())
                    for position, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
                ]
                db.executemany("INSERT INTO chunks(key, ord, heading, vector) VALUES(?, ?, ?, ?)", rows)
                db.execute("INSERT INTO embedded(key, version) VALUES(?, ?)", (key, version))

    def _load_matrix(self) -> None:
        import numpy

        generation = self._meta(GENERATION_META)
        if generation == self._generation and self._matrix is not None:
            return
        rows = self._db().execute("SELECT key, heading, vector FROM chunks ORDER BY key, ord").fetchall()
        self._rows = [(row[0], row[1] or "") for row in rows]
        vectors = [numpy.frombuffer(row[2], dtype=numpy.float32) for row in rows]
        self._matrix = numpy.vstack(vectors) if vectors else numpy.zeros((0, 0), dtype=numpy.float32)
        self._generation = generation

    def nearest(self, query: str) -> list[Hit]:
        import numpy

        self._load_matrix()
        if not self._rows:
            return []
        embedded = self.embedder.query(query)
        if embedded is None:
            return []
        vector = numpy.asarray(embedded, dtype=numpy.float32)
        if vector.shape[0] != self._matrix.shape[1]:
            return []
        similarities = self._matrix @ vector
        best: dict[str, Hit] = {}
        for position in numpy.argsort(-similarities):
            similarity = float(similarities[position])
            if similarity < self.embedder.min_similarity:
                break
            key, heading = self._rows[position]
            if key not in best:
                best[key] = Hit(key=key, heading=heading, similarity=similarity)
        return list(best.values())

    def count(self) -> int:
        return self._db().execute("SELECT COUNT(*) FROM embedded").fetchone()[0]
