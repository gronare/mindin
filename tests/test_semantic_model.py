from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest

from notes_vault_mcp.backends.local import LocalBackend
from notes_vault_mcp.embedding import SharedEmbedder, extra_installed, model_name
from notes_vault_mcp.schema import load_schema
from notes_vault_mcp.search import search
from notes_vault_mcp.vault import Vault

pytestmark = pytest.mark.skipif(
    not os.environ.get("VAULT_TEST_SEMANTIC_MODEL") or not extra_installed(),
    reason="downloads the embedding model: set VAULT_TEST_SEMANTIC_MODEL=1 with the semantic extra installed",
)

NOTES = {
    "Areas/anslutning.md": (
        "Anslutning för claude.ai",
        "## OAuth-flödet\n\nKlienten skickas till leverantören, får en kod tillbaka och byter den mot en åtkomsttoken. "
        "Token förnyas med en refresh-token.\n\n## Drift\n\nServern körs i klustret bakom Traefik.\n",
    ),
    "Areas/kopior.md": (
        "Säkerhetskopior",
        "## Schema\n\nLonghorn-volymerna kopieras varje natt till MinIO och behålls i trettio dagar.\n",
    ),
    "Areas/utskick.md": (
        "Outgoing mail",
        "## Delivery\n\nOutgoing mail is signed with DKIM and the SPF record includes the provider.\n",
    ),
    "Areas/tvatt.md": (
        "Tvättstugan",
        "## Bokning\n\nMedlemmar reserverar tvättstugan i pass om tre timmar "
        "och kan avboka fram till en timme innan.\n",
    ),
}


@pytest.fixture(scope="module")
def vault(tmp_path_factory: pytest.TempPathFactory):
    base = tmp_path_factory.mktemp("semantic")
    for key, (title, body) in NOTES.items():
        path = base / "vault" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        front = f"---\ntitle: {title}\ndate: 2026-10-01\nupdated: 2026-10-01\ntags: []\nstatus: active\n---\n"
        path.write_text(front + body, encoding="utf-8")
    cache = base / "cache"
    cache.mkdir()
    if os.environ.get("VAULT_TEST_MODEL_CACHE"):
        (cache / "models").symlink_to(Path(os.environ["VAULT_TEST_MODEL_CACHE"]))
    embedder = SharedEmbedder(cache, model_name())
    backend = LocalBackend(base / "vault")
    opened = Vault(backend, load_schema(backend), base / "index.sqlite", embedder=embedder)
    opened.index.sync(force=True)
    yield opened
    opened.close()
    if embedder.worker_pid:
        os.kill(embedder.worker_pid, signal.SIGTERM)


def found(vault: Vault, query: str) -> list[str]:
    return [row.note.key for row in search(vault.index, vault.schema, query).rows]


def test_a_swedish_query_finds_the_note_by_meaning_alone(vault: Vault):
    result = search(vault.index, vault.schema, "inloggning")
    assert result.term_count == 0 and result.by_meaning == len(result.rows)
    assert "Areas/anslutning.md" in [row.note.key for row in result.rows]
    assert next(row for row in result.rows if row.note.key == "Areas/anslutning.md").section == "OAuth-flödet"


def test_an_english_query_finds_a_swedish_note(vault: Vault):
    assert found(vault, "book a laundry slot")[0] == "Areas/tvatt.md"


def test_a_swedish_query_finds_an_english_note(vault: Vault):
    assert found(vault, "e-post hamnar i skräpposten")[0] == "Areas/utskick.md"


def test_words_in_the_note_still_rank_it_first(vault: Vault):
    result = search(vault.index, vault.schema, "Longhorn")
    assert result.rows[0].note.key == "Areas/kopior.md" and not result.rows[0].by_meaning
