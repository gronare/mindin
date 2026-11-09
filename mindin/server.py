from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import Tool as MCPTool

from mindin import changelog, notes, provision, repo_pages, tasks, views
from mindin.backends import VaultError
from mindin.dav import DavCredentials
from mindin.frontmatter import FrontmatterError
from mindin.hosted.config import MACHINE_KEY_PURPOSE, WRITE_SCOPE
from mindin.hosted.scopes import ScopeError, require_write
from mindin.presets import PERSONAL_LAYOUTS
from mindin.schema import CLAIM_KEY, NoHome, instructions, shared_instructions
from mindin.search import QueryError, Searched, SearchResult, across, render, search
from mindin.snapshot import build_snapshot
from mindin.vault import (
    NoVault,
    Reachable,
    SingleVault,
    Unconfigured,
    Vault,
    VaultResolver,
    open_vault,
)

VERSION = "0.16.0"

EXPECTED_FAILURES = (VaultError, FrontmatterError, notes.ValidationError, ScopeError, QueryError, NoHome)

SEARCH_DESCRIPTION = (
    "CHEAP — start here. Full-text search over the local SQLite index of the vault; it never "
    "downloads the whole vault. Matches file stem, title, summary, tags and body, folds diacritics, expands "
    'synonyms and accepts "quoted phrases". A bare commit sha looks up the notes that mention it. '
    "A query that is exactly a note's stem, or its [[stem]] wikilink, puts that note first wherever it lives. "
    "Every term is required; when no note has them all, it returns the notes matching the most terms and "
    "says so in the header. "
    "Inflected forms match through stemming in the vault's languages. "
    "Search in the vault's language first; when nothing matches, translate the terms into the vault's other "
    "languages and search again, and the reply names them. "
    "Excludes the archive and superseded notes unless you ask for them, and reports how many it hid. "
    "limit defaults to the vault schema's search.default_limit."
)
SPANNING_DESCRIPTION = (
    " When the connection reaches more than one vault, it searches this connection's main vault and every other vault "
    "it reaches that is searched by default: the header names them, each row starts with its vault, and "
    "read_file(path, vault=<vault>) reads a row from another vault without a new connection. vault searches that "
    "one vault alone, also one not searched by default."
)
VAULT_ARGUMENT = (
    " vault names another vault this connection reaches, as search rows name it; leave it out for the main vault."
)
VAULT_TOOLS = "search, read_file, list_files, write_file, append_file, move_file, delete_file and rules"
MEANING_DESCRIPTION = (
    " It also finds notes by meaning, across languages too, that share no word with the query: "
    "the header counts those rows as matched by meaning only, and a row that matched a section names it after §."
)


INSTRUCTED_METHODS = frozenset({"initialize", "server/discover"})
MACHINE_TOOLS = frozenset({"claim", "release", "changelog", "lint_and_park", "snapshot", "view"})


def machine_request() -> bool:
    token = get_access_token()
    return token is not None and (token.claims or {}).get("key_purpose") == MACHINE_KEY_PURPOSE


class VaultServer(MCPServer):
    refusal: str | None = None

    # The machine tools answer only a device-flow key, so no model can list or call them.
    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        return tools if machine_request() else [tool for tool in tools if tool.name not in MACHINE_TOOLS]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        if name in MACHINE_TOOLS and not machine_request():
            raise ToolError(f"Unknown tool: {name}")
        if self.refusal is not None:
            raise ToolError(self.refusal)
        return await super().call_tool(name, arguments, context)


def answer_instructions_per_vault(server: MCPServer, answer: Callable[[], str]) -> None:
    async def per_vault(ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method in INSTRUCTED_METHODS and isinstance(result, dict):
            return {**result, "instructions": answer()}
        return result

    server.middleware.append(per_vault)


def build_server(
    vaults: Vault | VaultResolver,
    personal_tokens: DavCredentials | None = None,
    dav_url: str = "",
    machine_tools: bool = False,
) -> MCPServer:
    resolver: VaultResolver = SingleVault(vaults) if isinstance(vaults, Vault) else vaults
    general = instructions(resolver.instruction_schema)
    server = VaultServer("mindin", instructions=general, version=VERSION)
    if isinstance(resolver, Unconfigured):
        server.refusal = resolver.reason

    def instructions_now() -> str:
        try:
            vault = resolver.current()
        except NoVault as exc:
            return f"{exc}\n\n{general}"
        except VaultError:
            return general
        rules = vault_rules(vault) if vault.rules is not None else general
        try:
            reached = resolver.around()
        except VaultError:
            return rules
        return f"{rules}\n\n{reach_section(reached)}" if reached else rules

    answer_instructions_per_vault(server, instructions_now)

    def run(
        action: Callable[[Vault], str],
        force: bool = False,
        write: bool = False,
        expected: tuple[type[Exception], ...] = EXPECTED_FAILURES,
        shared_refuses: str | None = None,
        vault_slug: str | None = None,
    ) -> str:
        try:
            if write:
                require_write()
            with resolver.using() if vault_slug is None else resolver.using_named(vault_slug) as vault:
                if shared_refuses is not None and vault.rules is not None:
                    raise vault.rules.refuse_tool(shared_refuses)
                vault.sync(force=force)
                return action(vault)
        except expected as exc:
            raise ToolError(str(exc)) from exc

    description = SEARCH_DESCRIPTION + (MEANING_DESCRIPTION if resolver.searches_by_meaning else "")
    description += SPANNING_DESCRIPTION if resolver.spans_vaults else ""
    vault_note = VAULT_ARGUMENT if resolver.spans_vaults else ""

    def search_everywhere(
        query: str, limit: int | None, filters: dict[str, Any], named: str | None = None
    ) -> SearchResult:
        reached = resolver.around()
        if named is None:
            chosen = [item for item in reached if item.searched]
        else:
            chosen = [item for item in reached if item.slug == named][:1]
        alone = not chosen or (len(chosen) == 1 and chosen[0].main)
        main = named is None or (bool(chosen) and chosen[0].main)
        with resolver.using() if main else resolver.using_named(named or "") as vault:
            vault.sync()
            if alone:
                return search(vault.index, vault.schema, query, limit=limit, **filters)
            wanted = vault.schema.default_limit if limit is None else limit
        return across([searched_in(item, query, wanted, filters) for item in chosen], wanted)

    def searched_in(item: Reachable, query: str, limit: int, filters: dict[str, Any]) -> Searched:
        try:
            with resolver.using_reached(item) as vault:
                vault.sync()
                found = search(vault.index, vault.schema, query, limit=limit, **filters)
        except VaultError as exc:
            return Searched(item.slug, f"{item.name}, not searched: {exc}", item.organisation, SearchResult())
        return Searched(item.slug, item.name, item.organisation, found)

    def answered(action: Callable[[], str], expected: tuple[type[Exception], ...] = EXPECTED_FAILURES) -> str:
        try:
            return action()
        except expected as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(name="search", description=description)
    def search_tool(
        query: str,
        limit: int | None = None,
        folder: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        kind: str | None = None,
        area: str | None = None,
        include_archive: bool = False,
        include_superseded: bool = False,
        path_prefix: str | None = None,
        since: str | None = None,
        vault: str | None = None,
    ) -> str:
        filters = {
            "folder": folder,
            "status": status,
            "tag": tag,
            "kind": kind,
            "area": area,
            "include_archive": include_archive,
            "include_superseded": include_superseded,
            "path_prefix": path_prefix,
            "since": since,
        }
        return answered(lambda: render(search_everywhere(query, limit, filters, vault)))

    @server.tool(
        name="read_file",
        description=(
            "MODERATE — one round trip to storage. Returns the note with an `etag:` first line; pass "
            "that etag back to write_file to refuse a write over someone else's change. Search first. "
            "`.vault/schema.yml` reads the vault's schema." + vault_note
        ),
    )
    def read_file(path: str, vault: str | None = None) -> str:
        return run(lambda opened: notes.read(opened, path), vault_slug=vault)

    @server.tool(
        name="write_file",
        description=(
            "WRITE — validates the frontmatter against the vault schema and refuses the write if it "
            "does not hold. Stamps `updated` with today and fills `date` when missing. Pass "
            'expected_etag from read_file to make the write conditional, or "*" to create a note '
            "that must not exist yet." + vault_note
        ),
    )
    def write_file(path: str, content: str, expected_etag: str | None = None, vault: str | None = None) -> str:
        return run(
            lambda opened: notes.write(opened, path, content, expected_etag=expected_etag), write=True, vault_slug=vault
        )

    @server.tool(
        name="append_file",
        description=(
            "WRITE — appends to an existing note and bumps `updated`; the note must still satisfy the schema. "
            "A missing note is refused: create it with write_file." + vault_note
        ),
    )
    def append_file(path: str, content: str, vault: str | None = None) -> str:
        return run(lambda opened: notes.append(opened, path, content), write=True, vault_slug=vault)

    @server.tool(
        name="move_file",
        description=(
            "WRITE — moves or renames a note; the note must satisfy the schema at the destination. "
            "Use `close` to archive finished work." + vault_note
        ),
    )
    def move_file(source: str, dest: str, vault: str | None = None) -> str:
        return run(lambda opened: notes.move(opened, source, dest), write=True, vault_slug=vault)

    @server.tool(
        name="delete_file", description="WRITE — deletes a note for good. Prefer `close`, which keeps it." + vault_note
    )
    def delete_file(path: str, vault: str | None = None) -> str:
        return run(lambda opened: notes.delete(opened, path), write=True, vault_slug=vault)

    @server.tool(
        name="list_files",
        description="CHEAP — paths only, no metadata and no bodies. `search` answers context questions better."
        + vault_note,
    )
    def list_files(prefix: str = "", vault: str | None = None) -> str:
        return run(lambda opened: _listing(opened, prefix), vault_slug=vault)

    @server.tool(
        name="set_status",
        description=(
            "WRITE — changes a note's status without rewriting it: backlog (with a priority: urgent, high, "
            "medium, low) parks it, active picks it up again, draft. The triage list at session start is "
            "settled with this, append_file or close."
        ),
    )
    def set_status_tool(path: str, status: str, priority: str | None = None, source: str | None = None) -> str:
        return run(
            lambda vault: notes.set_status(vault, path, status, priority=priority, source=source),
            write=True,
            shared_refuses="set_status",
        )

    @server.tool(
        name="close",
        description=(
            "WRITE — finishes a note: sets status complete (or superseded with superseded_by when "
            "merged_into is given) and moves it into the archive. Run it in the same pass as the last "
            "commit of the work; a note left open is what makes the vault drift."
        ),
    )
    def close_tool(path: str, merged_into: str | None = None, status: str | None = None) -> str:
        return run(
            lambda vault: f"Closed: {notes.close(vault, path, merged_into=merged_into, status=status)}", write=True
        )

    @server.tool(
        name="backlog_add",
        description=(
            "WRITE — files an idea as a backlog note in the task folder: title, area (stem or [[stem]]), "
            "one line saying what and why, optional priority (urgent, high, medium, low) and source (who "
            "said it and when, or a sha). Call it the moment something is deferred, in so many words or in "
            "passing; picking the idea up later is setting its status to active."
        ),
    )
    def backlog_add(title: str, area: str, line: str, priority: str | None = None, source: str | None = None) -> str:
        return run(
            lambda vault: f"Filed: {notes.backlog_add(vault, title, area, line, priority=priority, source=source)}",
            write=True,
            shared_refuses="backlog_add",
        )

    @server.tool(
        name="backlog",
        description=(
            "CHEAP — the backlog sorted by priority then age. Filter by area (a stem, or the prefix a family "
            "of stems shares) and by priority."
        ),
    )
    def backlog_tool(area: str | None = None, priority: str | None = None, limit: int = 50) -> str:
        return run(
            lambda vault: notes.render_backlog(notes.backlog(vault, area=area, priority=priority)[:limit], vault),
            shared_refuses="backlog",
        )

    @server.tool(
        name="log_append",
        description=(
            "WRITE — appends one dated line to the repo log, with the commits it produced. Run once at "
            "the end of a session per repo. Creates the log note when it is missing. repo is the main checkout's "
            "name, also from a worktree; branch is the branch the commits were made on. area is the stem of "
            "the system note (or [[stem]]); it defaults to the repo name."
        ),
    )
    def log_append(
        repo: str, line: str, commits: list[str] | None = None, area: str | None = None, branch: str | None = None
    ) -> str:
        return run(
            lambda vault: notes.log_append(vault, repo, line, commits=tuple(commits or ()), area=area, branch=branch),
            write=True,
            shared_refuses="log_append",
        )

    @server.tool(
        name="context",
        description=(
            "CHEAP — the session-start call. Returns the system notes covering the code at `path`, the "
            "open tasks for it, the reference notes and the tail of the repo log, in one answer. Use it "
            'before searching blind. format "json" adds the system notes\' paths and dates and the triage '
            "keys, for hooks that read it. remote is the repo's origin URL; for a member of an organisation the "
            "bundle adds the organisation's pages that name the repo or its area."
        ),
    )
    def context_tool(
        path: str | None = None,
        repo: str | None = None,
        query: str | None = None,
        limit: int = 10,
        format: str = "text",
        remote: str | None = None,
    ) -> str:
        def answer(vault: Vault) -> str:
            bundle = views.context_bundle(vault, path=path, repo=repo, query=query, limit=limit)
            bundle.organisation_pages = organisation_pages(vault, bundle, path, repo, remote)
            return json.dumps(bundle.as_dict(), ensure_ascii=False) if format == "json" else bundle.render()

        return run(answer)

    def organisation_pages(
        current: Vault, bundle: notes.ContextBundle, path: str | None, repo: str | None, remote: str | None
    ) -> list[notes.RepoPage]:
        if not (path or repo):
            return []
        areas = {note.stem for note, _ in bundle.system} | {note.stem for note in bundle.system_rows}
        found = []
        for item in resolver.around():
            if not item.organisation:
                continue
            try:
                with resolver.using_reached(item) as vault:
                    if vault is current:
                        continue
                    vault.sync()
                    found += repo_pages.pages_for(vault, item.slug, path, repo, remote, areas)
            except VaultError:
                continue
        return repo_pages.strongest(found)

    if machine_tools:

        @server.tool(
            name="lint_and_park",
            description=(
                "WRITE — the weekly lint for a scheduled job: parks stale open task notes in the backlog when park "
                "is set, and writes the findings as the note at write when given; answers the findings."
            ),
        )
        def lint_and_park_tool(write: str | None = None, park: bool = False) -> str:
            return run(
                lambda vault: notes.lint_and_park(vault, write=write, park=park),
                force=True,
                write=True,
                shared_refuses="lint_and_park",
            )

        @server.tool(
            name="claim",
            description=(
                f"WRITE — for tools that run agents on task notes, such as kebbie: writes which machine and task work "
                f"on the note (frontmatter key {CLAIM_KEY}), unless another machine holds it; force takes it over. "
                "An agent never calls it on its own. Answers JSON."
            ),
        )
        def claim_tool(path: str, machine: str, task: str, repo: str = "", force: bool = False) -> str:
            return run(
                lambda vault: json.dumps(tasks.claim_note(vault, path, machine, task, repo=repo, force=force)),
                write=True,
                expected=(*EXPECTED_FAILURES, ValueError),
                shared_refuses="claim",
            )

        @server.tool(
            name="release",
            description=(
                f"WRITE — for tools that run agents on task notes, such as kebbie: clears the note's claim "
                f"(frontmatter key {CLAIM_KEY}), only when machine holds it if machine is given; without a path, "
                "clears every claim of machine and task. Answers JSON."
            ),
        )
        def release_tool(path: str | None = None, machine: str | None = None, task: str | None = None) -> str:
            def answer(vault: Vault) -> str:
                if path is None:
                    return json.dumps(tasks.release_claims_of(vault, machine or "", task or ""))
                return json.dumps(tasks.release_note(vault, path, machine=machine))

            return run(answer, write=True, expected=(*EXPECTED_FAILURES, ValueError), shared_refuses="release")

        @server.tool(
            name="snapshot",
            description=(
                "CHEAP — for tools that draw a board from the vault, such as kebbie: every system and task note "
                "outside the archive and log folders with its metadata and claim, the opening paragraph of open "
                "tasks and the last lines of each repo log, under an etag. Pass since=<etag> to get only "
                '{"unchanged": true} when nothing changed. Answers JSON.'
            ),
        )
        def snapshot_tool(since: str | None = None) -> str:
            return run(lambda vault: json.dumps(build_snapshot(vault, since=since), ensure_ascii=False))

        @server.tool(
            name="view",
            description=(
                "CHEAP — for the command line's human output: the data behind search, backlog or context "
                "(command), with the arguments that command takes. Answers JSON."
            ),
        )
        def view_tool(
            command: str,
            query: str | None = None,
            limit: int | None = None,
            area: str | None = None,
            priority: str | None = None,
            path: str | None = None,
            repo: str | None = None,
        ) -> str:
            def answer(vault: Vault) -> str:
                found = views.view(vault, command, query, limit, area, priority, path, repo)
                return json.dumps(found, ensure_ascii=False)

            if command == "search":
                return answered(
                    lambda: json.dumps(views.search_view(search_everywhere(query or "", limit, {})), ensure_ascii=False)
                )
            return run(
                answer,
                expected=(*EXPECTED_FAILURES, ValueError),
                shared_refuses="backlog" if command == "backlog" else None,
            )

        @server.tool(
            name="changelog",
            description=(
                "CHEAP — a repo's period page: its log lines, its commits and the notes dated in the period "
                '(YYYY-MM or YYYY). commits maps a day (YYYY-MM-DD) to its "sha subject" lines, read from the '
                "repo's git log by the caller; leave it out to show no commits. write stores the page as the period "
                "note instead of returning it."
            ),
        )
        def changelog_tool(
            repo: str, period: str, commits: dict[str, list[str]] | None = None, write: bool = False
        ) -> str:
            def answer(vault: Vault) -> str:
                if not write:
                    return changelog.render(vault, repo, period, commits=commits)
                changed = changelog.write_page(vault, repo, period, commits=commits)
                return ("wrote " if changed else "unchanged ") + vault.schema.period_key(repo, period)

            return run(
                answer,
                write=write,
                expected=(*EXPECTED_FAILURES, ValueError),
                shared_refuses="changelog" if write else None,
            )

    @server.tool(
        name="rules",
        description=(
            "CHEAP — a vault's own rules for agents: its folders, the frontmatter contract, when and how to write "
            "there, and what session start shows from it. Read them before the first write to a vault reached "
            "through `vault`; for the main vault they are the server's instructions." + vault_note
        ),
    )
    def rules_tool(vault: str | None = None) -> str:
        return run(vault_rules, vault_slug=vault)

    @server.tool(
        name="whoami",
        description="CHEAP — who this connection signs in as and whether it may write. Answers JSON.",
    )
    def whoami_tool() -> str:
        token = get_access_token()
        if token is None:
            return json.dumps({"email": None, "write": True})
        claims = token.claims or {}
        answer = {
            "email": claims.get("email"),
            "vault": claims.get("vault"),
            "vault_name": claims.get("vault_name"),
            "vault_kind": claims.get("vault_kind"),
            "write": WRITE_SCOPE in token.scopes,
        }
        if claims.get("vault_kind") == "shared":
            answer["role"] = claims.get("role")
        return json.dumps(answer)

    @server.tool(
        name="lint",
        description=(
            "MODERATE — reads every note to report drift: broken frontmatter, missing required fields, "
            "missing area, unresolved wikilinks, orphans, stale open tasks, archived notes still marked "
            "active, duplicate stems and superseded targets that do not exist."
        ),
    )
    def lint_tool() -> str:
        return run(lambda vault: notes.lint(vault).render(), force=True)

    @server.tool(
        name="init",
        description=(
            "WRITE — sets a vault up: writes `.vault/schema.yml` and the Obsidian Bases views into the vault "
            "root. Run once per vault, right after installing. preset picks the layout of a vault without a "
            f"schema: {', '.join(PERSONAL_LAYOUTS)}; without it, the vault gets default. On a vault that has a "
            "schema, it only writes the views that are missing and keeps the rest; a layout is switched with the CLI "
            "(`init --preset <name> --force`) or on the account page, never through this tool."
        ),
    )
    def init_tool(force: bool = False, preset: str | None = None) -> str:
        return run(lambda vault: _init(vault, force, preset), write=True, shared_refuses="init")

    if personal_tokens is not None:

        @server.tool(
            name="obsidian_access",
            description=(
                "WRITE — mints the personal token Obsidian's Remotely Save plugin uses to reach this vault over "
                "WebDAV, and shows it once. Issuing a new token revokes the previous one. The username is the "
                "email on your identity."
            ),
        )
        def obsidian_access() -> str:
            return run(lambda vault: _obsidian_access(personal_tokens, dav_url), write=True)

    return server


def reach_section(reached: list[Reachable]) -> str:
    main = next((item for item in reached if item.main), reached[0])
    lines = [
        "## Vaults this connection reaches",
        f"The main vault is {main.name} (`{main.slug}`): every tool works there unless it is given `vault`. "
        f"The connection also reaches these, named with `vault=<slug>` on {VAULT_TOOLS}; "
        "search without `vault` covers the main vault and the ones searched by default:",
    ]
    for item in reached:
        if item is main:
            continue
        where = f"{item.name}, {item.organisation}" if item.organisation else item.name
        searched = "searched by default" if item.searched else "not searched by default"
        lines.append(f"- `{item.slug}` ({where}): {searched}")
    return "\n".join(lines)


def vault_rules(vault: Vault) -> str:
    return (
        shared_instructions(vault.schema, vault.rules.name) if vault.rules is not None else instructions(vault.schema)
    )


def _init(vault: Vault, force: bool, preset: str | None) -> str:
    if preset is not None and preset not in PERSONAL_LAYOUTS:
        raise notes.ValidationError(f"no preset named {preset!r}; the presets are: {', '.join(PERSONAL_LAYOUTS)}")
    current = provision.current_preset(vault.backend)
    if current is not None and (force or preset not in (None, current)):
        raise notes.ValidationError(
            f"This vault already has a layout ({current}). The init tool only sets up a vault without a schema, "
            "so a client cannot change the rules that check its own writes. Switch layouts with the CLI "
            f"(mindin init --preset {preset or '<name>'} --force) or on the account page."
        )
    result = provision.initialize(vault.backend, force=force and current is None, preset=preset)
    vault.reload_schema()
    lines = [f"layout: {result.preset}"]
    lines += [f"wrote {key}" for key in result.written] + [f"kept {key}" for key in result.kept]
    return "\n".join(lines)


def _obsidian_access(tokens: DavCredentials, dav_url: str) -> str:
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    if token is None or not token.subject:
        raise ScopeError("no authenticated subject on this request")
    email = str((token.claims or {}).get("email") or token.subject)
    account = (token.claims or {}).get("account")
    secret = tokens.issue(token.subject, label=email, account_id=account)
    return (
        "Remotely Save → WebDAV\n"
        f"Server address: {dav_url}\n"
        f"Username: {email}\n"
        f"Password: {secret}\n\n"
        "Shown once. Issuing a new token revokes this one."
    )


def _listing(vault: Vault, prefix: str) -> str:
    keys = [entry.key for entry in vault.backend.list() if entry.key.startswith(prefix)]
    return "\n".join(keys) if keys else f"No files under '{prefix}'"


def run_stdio(vault: Vault | VaultResolver) -> None:
    build_server(vault).run("stdio")


def create_server() -> MCPServer:
    return build_server(open_vault(vectors="background"))
