from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy
import pytest
from fake_models import concept_vector

from mindin import cli, embedding, notes, vectors
from mindin.backends.local import LocalBackend
from mindin.embedding import MINILM, MPNET, SharedEmbedder, configured_embedder
from mindin.index import Index
from mindin.schema import load_schema
from mindin.search import render, search
from mindin.vault import Vault, open_vault
from mindin.vectors import chunks_of


class FakeEmbedder:
    min_similarity = 0.3

    def __init__(self, model_id: str = "fake-1") -> None:
        self.model_id = model_id
        self.embedded: list[str] = []

    def passages(self, texts):
        self.embedded.extend(texts)
        return numpy.vstack([concept_vector(text) for text in texts])

    def query(self, text):
        return concept_vector(text)


NOTES = {
    "Areas/auth.md": (
        "---\ntitle: Anslutning\ndate: 2026-10-01\nupdated: 2026-10-01\ntags: [mcp]\nstatus: active\n"
        "kind: system\n---\nHur klienter når servern.\n\n## OAuth-flödet\n\n"
        "Klienten får en token av leverantören och byter den mot oauth token.\n\n"
        "## Drift\n\nServern körs i klustret bakom proxyn.\n"
    ),
    "Areas/board.md": (
        "---\ntitle: Board\ndate: 2026-10-01\nupdated: 2026-10-01\ntags: []\nstatus: active\nkind: system\n---\n"
        "The board lists every lane, column, card and owner; the kebbie-tasks command prints it as json for scripts "
        "and the lead reads it every morning before planning the day with the team around the table.\n"
    ),
    "Resources/kebbie-note.md": (
        "---\ntitle: Kebbie\ndate: 2026-10-01\nupdated: 2026-10-01\ntags: []\nstatus: active\nkind: howto\n"
        'area: "[[board]]"\n---\nkebbie kebbie kebbie\n'
    ),
    "Projects/backup-plan.md": (
        "---\ntitle: Nattlig kopia\ndate: 2026-10-01\nupdated: 2026-10-01\ntags: [backup]\nstatus: active\nkind: task\n"
        'area: "[[auth]]"\n---\n## Plan\n\nsäkerhetskopia varje natt\n'
    ),
    "Archive/old-auth.md": (
        "---\ntitle: Gammal anslutning\ndate: 2025-01-01\nupdated: 2025-01-01\ntags: []\nstatus: complete\nkind: task\n"
        'area: "[[auth]]"\n---\noauth token oauth\n'
    ),
    "Resources/old-login.md": (
        "---\ntitle: Förra flödet\ndate: 2025-01-01\nupdated: 2025-01-01\ntags: []\nstatus: superseded\nkind: howto\n"
        'area: "[[auth]]"\nsuperseded_by: "[[auth]]"\n---\noauth token login\n'
    ),
}


def write_notes(root: Path) -> None:
    for key, text in NOTES.items():
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "vault"
    write_notes(folder)
    return folder


def open_with(root: Path, index_path: Path, embedder, background: bool = False) -> Vault:
    backend = LocalBackend(root)
    vault = Vault(backend, load_schema(backend), index_path, embedder=embedder, background=background)
    vault.index.sync(force=True)
    return vault


@pytest.fixture
def fake() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def vault(root: Path, tmp_path: Path, fake: FakeEmbedder):
    opened = open_with(root, tmp_path / "index.sqlite", fake)
    yield opened
    opened.close()


def keys(result) -> list[str]:
    return [row.note.key for row in result.rows]


def chunk_keys(vault: Vault) -> set[str]:
    return {row[0] for row in vault.index.db.execute("SELECT key FROM chunks")}


def test_sync_embeds_only_the_notes_whose_version_changed(vault: Vault, root: Path, fake: FakeEmbedder):
    fake.embedded.clear()
    path = root / "Projects/backup-plan.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nOch en till.\n", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    vault.index.sync(force=True)
    assert fake.embedded and all(text.startswith("Nattlig kopia") for text in fake.embedded)
    fake.embedded.clear()
    vault.index.sync(force=True)
    assert fake.embedded == []


def test_a_deleted_note_takes_its_vectors_with_it(vault: Vault, root: Path):
    assert "Projects/backup-plan.md" in chunk_keys(vault)
    (root / "Projects/backup-plan.md").unlink()
    vault.index.sync(force=True)
    assert "Projects/backup-plan.md" not in chunk_keys(vault)
    embedded = vault.index.db.execute("SELECT key FROM embedded").fetchall()
    assert "Projects/backup-plan.md" not in {row[0] for row in embedded}


def test_a_model_change_recomputes_every_note(vault: Vault, root: Path, tmp_path: Path):
    vault.close()
    other = FakeEmbedder("fake-2")
    reopened = open_with(root, tmp_path / "index.sqlite", other)
    try:
        assert {text.split("\n", 1)[0] for text in other.embedded} >= {"Anslutning", "Board", "Nattlig kopia"}
        assert reopened.index.vectors.count() == len(NOTES)
        other.embedded.clear()
        reopened.index.sync(force=True)
        assert other.embedded == []
    finally:
        reopened.close()


def test_a_note_written_through_the_server_is_embedded_on_the_next_call(vault: Vault, fake: FakeEmbedder):
    fake.embedded.clear()
    text = NOTES["Areas/board.md"].replace("title: Board", "title: Inloggningen").replace("tags: []", "tags: [mcp]")
    notes.write(vault, "Areas/login.md", text)
    vault.index.sync()
    assert any(entry.startswith("Inloggningen") for entry in fake.embedded)
    assert "Areas/login.md" in chunk_keys(vault)


def test_chunks_follow_headings_and_ignore_hashes_in_code():
    body = (
        "Intro words.\n\n## Flödet\n\nSteg ett.\n\n```sh\n# not a heading\n```\n\n"
        "### Detalj\n\nMer.\n\n## Drift\n\nKlart.\n"
    )
    chunks = chunks_of("Titel", "En sammanfattning.", body)
    assert [chunk.heading for chunk in chunks] == ["", "Flödet", "Detalj", "Drift"]
    assert chunks[0].text == "Titel\nEn sammanfattning. Intro words."
    assert "# not a heading" in chunks[1].text
    assert chunks[2].text.startswith("Titel\nFlödet > Detalj\n")


def test_a_long_section_is_split_into_windows_that_keep_the_heading():
    chunks = chunks_of("T", "", "## Lång\n\n" + " ".join(f"ord{n}" for n in range(200)))
    assert len(chunks) == 3
    assert {chunk.heading for chunk in chunks} == {"Lång"}
    assert all(chunk.text.startswith("T\nLång\n") for chunk in chunks)


def test_an_empty_note_still_gets_one_chunk_from_its_title():
    assert [chunk.text for chunk in chunks_of("Bara titel", "", "## Rubrik\n")] == ["Bara titel"]


def test_meaning_finds_a_note_that_shares_no_word_with_the_query(vault: Vault):
    result = search(vault.index, vault.schema, "inloggning")
    assert keys(result)[0] == "Areas/auth.md"
    assert result.rows[0].by_meaning
    assert result.by_meaning == len(result.rows)


def test_a_meaning_hit_names_the_section_it_came_from(vault: Vault):
    result = search(vault.index, vault.schema, "inloggning")
    assert result.rows[0].section == "OAuth-flödet"
    text = render(result)
    assert "by meaning only" in text.splitlines()[0]
    assert "Areas/auth.md | Anslutning |" in text and "§ OAuth-flödet" in text


def test_meaning_hits_alone_do_not_read_as_a_partial_word_match(vault: Vault):
    result = search(vault.index, vault.schema, "inloggning xyzzy")
    assert not result.partial
    assert re.match(r"\d+ of \d+, \d+ by meaning only \(", render(result))


def test_an_exact_name_still_ranks_first(vault: Vault):
    result = search(vault.index, vault.schema, "kebbie-tasks")
    assert keys(result)[0] == "Areas/board.md"
    assert "Resources/kebbie-note.md" in keys(result)


def test_a_sha_query_stays_lexical(vault: Vault, root: Path):
    path = root / "Areas/board.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nFixed in 9ca31382.\n", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    vault.index.sync(force=True)
    result = search(vault.index, vault.schema, "9ca31382")
    assert keys(result) == ["Areas/board.md"]
    assert result.by_meaning == 0


def test_a_stem_query_keeps_its_note_pinned_first(vault: Vault):
    assert keys(search(vault.index, vault.schema, "backup-plan"))[0] == "Projects/backup-plan.md"


def test_meaning_hits_hide_the_archive_and_superseded_notes(vault: Vault):
    found = keys(search(vault.index, vault.schema, "inloggning"))
    assert "Archive/old-auth.md" not in found
    assert "Resources/old-login.md" not in found
    assert "Archive/old-auth.md" in keys(search(vault.index, vault.schema, "inloggning", include_archive=True))
    assert "Resources/old-login.md" in keys(search(vault.index, vault.schema, "inloggning", include_superseded=True))


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"folder": "Areas"}, ["Areas/auth.md"]),
        ({"folder": "Projects"}, []),
        ({"kind": "task", "include_archive": True}, ["Archive/old-auth.md"]),
        ({"status": "superseded", "include_superseded": True}, ["Resources/old-login.md"]),
        ({"tag": "mcp"}, ["Areas/auth.md"]),
        ({"area": "auth", "include_superseded": True}, ["Resources/old-login.md"]),
        ({"since": "2026-01-01", "include_archive": True, "include_superseded": True}, ["Areas/auth.md"]),
    ],
)
def test_filters_apply_to_meaning_hits(vault: Vault, filters: dict, expected: list[str]):
    assert keys(search(vault.index, vault.schema, "inloggning", **filters)) == expected


def test_another_process_embedding_the_same_index_is_left_to_it(root: Path, tmp_path: Path):
    backend = LocalBackend(root)
    fake = FakeEmbedder()
    opened = Vault(backend, load_schema(backend), tmp_path / "leased.sqlite", embedder=fake)
    try:
        opened.index.set_meta("embed_lease", f"{os.getpid() + 1} {time.time()}")
        opened.index.sync(force=True)
        assert fake.embedded == [] and opened.index.vectors.count() == 0
        opened.index.set_meta("embed_lease", f"{os.getpid() + 1} {time.time() - 600}")
        opened.index.sync(force=True)
        assert opened.index.vectors.count() == len(NOTES)
        assert opened.index.meta("embed_lease") is None
    finally:
        opened.close()


def test_vectors_are_computed_in_the_background_for_the_server(root: Path, tmp_path: Path):
    fake = FakeEmbedder()
    opened = open_with(root, tmp_path / "bg.sqlite", fake, background=True)
    try:
        opened.index.vectors.wait()
        assert opened.index.vectors.count() == len(NOTES)
    finally:
        opened.close()


class FailingEmbedder(FakeEmbedder):
    def passages(self, texts):
        raise RuntimeError("the embedding process went away")

    def query(self, text):
        raise RuntimeError("the embedding process went away")


def test_a_failing_embedder_leaves_search_by_words(vault: Vault):
    vault.index.vectors.embedder = FailingEmbedder()
    result = search(vault.index, vault.schema, "kebbie-tasks")
    assert keys(result) == ["Areas/board.md"] and result.by_meaning == 0


class FlakyEmbedder(FakeEmbedder):
    def __init__(self, failure) -> None:
        super().__init__()
        self.failure = failure

    def query(self, text):
        failure, self.failure = self.failure, None
        return super().query(text) if failure is None else failure()


def killed():
    raise RuntimeError("the embedding process was killed")


def timed_out():
    return None


@pytest.mark.parametrize("failure", [killed, timed_out])
def test_a_failed_query_vector_does_not_keep_the_next_search_from_meaning(vault: Vault, failure):
    vault.index.vectors.embedder = FlakyEmbedder(failure)
    assert search(vault.index, vault.schema, "inloggning").by_meaning == 0
    assert keys(search(vault.index, vault.schema, "inloggning")) == ["Areas/auth.md"]


class DyingEmbedder(FakeEmbedder):
    def __init__(self, calls: int) -> None:
        super().__init__()
        self.calls = calls

    def passages(self, texts):
        if self.calls == 0:
            raise RuntimeError("the embedding process was killed")
        self.calls -= 1
        return super().passages(texts)


def embedded_keys(index_path: Path) -> set[str]:
    with sqlite3.connect(index_path) as db:
        return {row[0] for row in db.execute("SELECT key FROM embedded")}


def test_an_interrupted_bulk_embed_continues_where_it_stopped(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(vectors, "NOTES_PER_BATCH", 1)
    index_path = tmp_path / "resume.sqlite"
    open_with(root, index_path, DyingEmbedder(calls=2)).close()
    finished = embedded_keys(index_path)
    assert len(finished) == 2
    resumed = FakeEmbedder()
    opened = open_with(root, index_path, resumed)
    try:
        assert opened.index.vectors.count() == len(NOTES)
        titles = {text.split("\n")[0] for text in resumed.embedded}
        for key in finished:
            assert opened.index.db.execute("SELECT title FROM notes WHERE key = ?", (key,)).fetchone()[0] not in titles
    finally:
        opened.close()


def test_serving_embeds_pending_notes_before_any_request(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    index_path = tmp_path / "startup.sqlite"
    open_with(root, index_path, None).close()
    backend = LocalBackend(root)
    vault = Vault(backend, load_schema(backend), index_path, embedder=FakeEmbedder(), background=True)
    served: list[Vault] = []
    monkeypatch.setenv("VAULT_PATH", str(root))
    monkeypatch.setattr(cli, "open_vault", lambda vectors: vault)
    monkeypatch.setattr(cli, "run_stdio", served.append)
    try:
        cli.command_serve(argparse.Namespace(transport="stdio"))
        vault.index.vectors.wait(10)
        assert served == [vault] and vault.index.vectors.count() == len(NOTES)
    finally:
        vault.close()


@pytest.mark.parametrize(("transport", "warmed"), [("http", 1), ("stdio", 0)])
def test_an_http_server_keeps_the_embedder_warm(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str, warmed: int
):
    embedder = SharedEmbedder(tmp_path / "cache")
    warms: list[SharedEmbedder] = []
    monkeypatch.setattr(SharedEmbedder, "keep_warm", lambda self: warms.append(self))
    backend = LocalBackend(root)
    vault = Vault(backend, load_schema(backend), tmp_path / "warm.sqlite", embedder=embedder, background=True)
    vault.start_embedding = lambda: None
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault.example.com")
    monkeypatch.setenv("VAULT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("VAULT_SMTP_FROM", "vault@example.com")
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setattr(cli, "open_vault", lambda vectors: vault)
    monkeypatch.setattr(cli, "SubjectVaults", lambda **_: SimpleNamespace(embedder=embedder))
    monkeypatch.setattr(cli, "run_stdio", lambda served: None)
    monkeypatch.setattr("mindin.hosted.app.run_http", lambda served, host, port, auth: None)
    try:
        cli.command_serve(argparse.Namespace(transport=transport, host="", port=0))
        assert warms == [embedder] * warmed
    finally:
        vault.close()


def test_a_failing_embedder_leaves_sync_working_by_words(root: Path, tmp_path: Path):
    opened = open_with(root, tmp_path / "failing.sqlite", FailingEmbedder())
    try:
        assert opened.index.vectors.count() == 0
        assert "Areas/board.md" in keys(search(opened.index, opened.schema, "kebbie-tasks"))
    finally:
        opened.close()


def test_a_session_with_nothing_new_to_embed_never_asks_for_the_model(root: Path, tmp_path: Path):
    open_with(root, tmp_path / "shared.sqlite", FakeEmbedder()).close()
    quiet = FakeEmbedder()
    quiet.query = lambda text: pytest.fail("asked for a query vector")
    second = open_with(root, tmp_path / "shared.sqlite", quiet)
    try:
        second.index.sync(force=True)
        assert quiet.embedded == []
    finally:
        second.close()


def test_the_setting_is_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("VAULT_SEMANTIC", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_VAULT_SEMANTIC", raising=False)
    assert configured_embedder(tmp_path) is None


def test_the_setting_is_read_from_the_plugin_option_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("VAULT_SEMANTIC", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_VAULT_SEMANTIC", "on")
    monkeypatch.setattr(embedding, "extra_installed", lambda: True)
    assert isinstance(configured_embedder(tmp_path), SharedEmbedder)


def test_the_model_is_chosen_by_its_own_setting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("VAULT_SEMANTIC", "on")
    monkeypatch.setattr(embedding, "extra_installed", lambda: True)
    default = configured_embedder(tmp_path)
    assert default.model_id == MPNET and default.min_similarity == 0.35
    monkeypatch.setenv("VAULT_SEMANTIC_MODEL", MINILM)
    chosen = configured_embedder(tmp_path)
    assert chosen.model_id == MINILM and chosen.min_similarity == 0.25


def test_on_without_the_extra_searches_by_words_and_says_so_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, vault_dir: Path, tmp_path: Path
):
    monkeypatch.setenv("VAULT_SEMANTIC", "on")
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(embedding, "_reported", set())
    monkeypatch.setattr(embedding, "EXTRA_MODULES", ("numpy", "fastembed_is_not_installed"))
    with caplog.at_level(logging.WARNING, logger="mindin.embedding"):
        first = open_vault(vectors="wait")
        second = open_vault(vectors="background")
    try:
        assert first.embedder is None and second.embedder is None
        first.index.sync(force=True)
        assert first.index.vectors is None
        assert "Areas/orchard.md" in keys(search(first.index, first.schema, "domän"))
        notices = [record for record in caplog.records if "semantic extra is not installed" in record.getMessage()]
        assert len(notices) == 1
    finally:
        first.close()
        second.close()


OFF_SCRIPT = """
import json, sys
from mindin.search import render, search
from mindin.vault import open_vault
vault = open_vault(vectors=sys.argv[1])
vault.index.sync(force=True)
queries = ("orchard", "domän", "inloggning", "9ca31382", "orchard bokning")
out = [render(search(vault.index, vault.schema, q)) for q in queries]
tables = [row[0] for row in vault.index.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
modules = ("fastembed", "numpy", "onnxruntime", "mindin.vectors", "mindin.embed_worker")
loaded = [m for m in modules if m in sys.modules]
print(json.dumps({"out": out, "tables": sorted(tables), "loaded": loaded}))
"""


def _run_off(vault_dir: Path, cache: Path, vectors: str) -> dict:
    environment = {k: v for k, v in os.environ.items() if "VAULT_" not in k and not k.startswith("S3_")}
    environment.update(VAULT_PATH=str(vault_dir), VAULT_CACHE_DIR=str(cache))
    done = subprocess.run(
        [sys.executable, "-c", OFF_SCRIPT, vectors], env=environment, capture_output=True, text=True, check=True
    )
    return json.loads(done.stdout)


def test_off_leaves_search_and_the_index_as_they_were_and_imports_nothing(vault_dir: Path, tmp_path: Path):
    served = _run_off(vault_dir, tmp_path / "a", "background")
    plain = _run_off(vault_dir, tmp_path / "b", "off")
    assert served == plain
    assert served["loaded"] == []
    assert "chunks" not in served["tables"] and "embedded" not in served["tables"]
    assert not list((tmp_path / "a").glob("embed*"))


def test_an_index_without_an_embedder_has_no_vectors(vault_dir: Path, tmp_path: Path):
    index = Index(tmp_path / "plain.sqlite", LocalBackend(vault_dir))
    try:
        assert index.vectors is None
        index.sync(force=True)
    finally:
        index.close()


async def _search_tool(vault: Vault):
    from mcp.client import Client

    from mindin.server import build_server

    async with Client(build_server(vault)) as client:
        tools = await client.list_tools()
        found = await client.call_tool("search", {"query": "inloggning"})
    description = next(tool.description for tool in tools.tools if tool.name == "search")
    return description, "".join(block.text for block in found.content if getattr(block, "text", None))


@pytest.mark.anyio
async def test_the_search_tool_says_it_finds_by_meaning_only_when_it_does(vault: Vault, root: Path, tmp_path: Path):
    description, text = await _search_tool(vault)
    assert "by meaning" in description
    assert text.startswith("1 of 1, 1 by meaning only") and "§ OAuth-flödet" in text
    backend = LocalBackend(root)
    plain = Vault(backend, load_schema(backend), tmp_path / "plain.sqlite")
    try:
        description, text = await _search_tool(plain)
    finally:
        plain.close()
    assert "by meaning" not in description
    assert text.startswith("0 of 0")
