from __future__ import annotations

import time

from notes_vault_mcp import notes
from notes_vault_mcp.search import SearchResult, search
from notes_vault_mcp.vault import Vault

COMMANDS = ("search", "backlog", "context")


def context_bundle(
    vault: Vault, path: str | None = None, repo: str | None = None, query: str | None = None, limit: int = 10
) -> notes.ContextBundle:
    bundle = notes.context(vault, path=path, repo=repo, query=query, limit=limit)
    if vault.rules is not None:
        bundle.notices.append(vault.rules.context_line)
        bundle.shared = True
    return bundle


def search_view(result: SearchResult) -> dict:
    now = time.time()
    return {
        "shown": len(result.rows),
        "total": result.total,
        "by_meaning": result.by_meaning,
        "hidden_archive": result.hidden_archive,
        "hidden_superseded": result.hidden_superseded,
        "term_count": result.term_count,
        "best_terms": result.best_terms,
        "languages": list(result.languages),
        "rows": [
            notes.note_fields(row.note, now)
            | {"snippet": row.snippet, "section": row.section, "by_meaning": row.by_meaning}
            for row in result.rows
        ],
    }


def backlog_view(vault: Vault, area: str | None = None, priority: str | None = None) -> dict:
    now = time.time()
    rows = notes.backlog(vault, area=area, priority=priority)
    return {
        "rows": [notes.note_fields(note, now) for note in rows],
        "empty": "" if rows else notes.render_backlog([], vault),
    }


def view(
    vault: Vault,
    command: str,
    query: str | None = None,
    limit: int = 15,
    area: str | None = None,
    priority: str | None = None,
    path: str | None = None,
    repo: str | None = None,
) -> dict:
    if command == "search":
        return search_view(search(vault.index, vault.schema, query or "", limit=limit))
    if command == "backlog":
        return backlog_view(vault, area=area, priority=priority)
    if command == "context":
        return context_bundle(vault, path=path, repo=repo).view()
    raise ValueError(f"no view named {command!r}; the views are: {', '.join(COMMANDS)}")
