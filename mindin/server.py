from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import Tool as MCPTool

from mindin import changelog, notes, provision, tasks, views
from mindin.auth import MACHINE_KEY_PURPOSE, WRITE_SCOPE, AuthConfig
from mindin.auth.scopes import ScopeError, require_write
from mindin.backends import VaultError
from mindin.dav import DavCredentials
from mindin.frontmatter import FrontmatterError
from mindin.presets import PERSONAL_LAYOUTS
from mindin.schema import CLAIM_KEY, NoHome, instructions, shared_instructions
from mindin.search import QueryError, render, search
from mindin.snapshot import build_snapshot
from mindin.vault import SingleVault, SubjectVaults, Vault, VaultResolver, open_vault

VERSION = "0.13.0"

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
    "Excludes the archive and superseded notes unless you ask for them, and reports how many it hid."
)
MEANING_DESCRIPTION = (
    " It also finds notes by meaning, across languages too, that share no word with the query: "
    "the header counts those rows as matched by meaning only, and a row that matched a section names it after §."
)


MACHINE_TOOLS = frozenset({"claim", "release", "changelog", "lint_and_park", "snapshot", "view"})


def machine_request() -> bool:
    token = get_access_token()
    return token is not None and (token.claims or {}).get("key_purpose") == MACHINE_KEY_PURPOSE


class VaultServer(MCPServer):
    # The machine tools answer only a device-flow key, so no model can list or call them.
    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        return tools if machine_request() else [tool for tool in tools if tool.name not in MACHINE_TOOLS]

    async def call_tool(self, name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        if name in MACHINE_TOOLS and not machine_request():
            raise ToolError(f"Unknown tool: {name}")
        return await super().call_tool(name, arguments, context)


def answer_instructions_per_vault(server: MCPServer, answer: Callable[[], str]) -> None:
    low = server._lowlevel_server
    low.__class__ = type("PerVaultServer", (type(low),), {"instructions": property(lambda _: answer())})


def build_server(
    vaults: Vault | VaultResolver,
    personal_tokens: DavCredentials | None = None,
    dav_url: str = "",
    machine_tools: bool = False,
) -> MCPServer:
    resolver: VaultResolver = SingleVault(vaults) if isinstance(vaults, Vault) else vaults
    general = instructions(resolver.instruction_schema)
    server = VaultServer("mindin", instructions=general, version=VERSION)

    def instructions_now() -> str:
        try:
            vault = resolver.current()
        except VaultError:
            return general
        return shared_instructions(vault.schema, vault.rules.name) if vault.rules is not None else general

    answer_instructions_per_vault(server, instructions_now)

    def run(
        action: Callable[[Vault], str],
        force: bool = False,
        write: bool = False,
        expected: tuple[type[Exception], ...] = EXPECTED_FAILURES,
        shared_refuses: str | None = None,
    ) -> str:
        try:
            if write:
                require_write()
            with resolver.using() as vault:
                if shared_refuses is not None and vault.rules is not None:
                    raise vault.rules.refuse_tool(shared_refuses)
                vault.sync(force=force)
                return action(vault)
        except expected as exc:
            raise ToolError(str(exc)) from exc

    description = SEARCH_DESCRIPTION + (MEANING_DESCRIPTION if resolver.searches_by_meaning else "")

    @server.tool(name="search", description=description)
    def search_tool(
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
    ) -> str:
        return run(
            lambda vault: render(
                search(
                    vault.index,
                    vault.schema,
                    query,
                    limit=limit,
                    folder=folder,
                    status=status,
                    tag=tag,
                    kind=kind,
                    area=area,
                    include_archive=include_archive,
                    include_superseded=include_superseded,
                    path_prefix=path_prefix,
                    since=since,
                )
            )
        )

    @server.tool(
        name="read_file",
        description=(
            "MODERATE — one round trip to storage. Returns the note with an `etag:` first line; pass "
            "that etag back to write_file to refuse a write over someone else's change. Search first. "
            "`.vault/schema.yml` reads the vault's schema."
        ),
    )
    def read_file(path: str) -> str:
        return run(lambda vault: notes.read(vault, path))

    @server.tool(
        name="write_file",
        description=(
            "WRITE — validates the frontmatter against the vault schema and refuses the write if it "
            "does not hold. Stamps `updated` with today and fills `date` when missing. Pass "
            'expected_etag from read_file to make the write conditional, or "*" to create a note '
            "that must not exist yet."
        ),
    )
    def write_file(path: str, content: str, expected_etag: str | None = None) -> str:
        return run(lambda vault: notes.write(vault, path, content, expected_etag=expected_etag), write=True)

    @server.tool(
        name="append_file",
        description=(
            "WRITE — appends to an existing note and bumps `updated`; the note must still satisfy the schema. "
            "A missing note is refused: create it with write_file."
        ),
    )
    def append_file(path: str, content: str) -> str:
        return run(lambda vault: notes.append(vault, path, content), write=True)

    @server.tool(
        name="move_file",
        description=(
            "WRITE — moves or renames a note; the note must satisfy the schema at the destination. "
            "Use `close` to archive finished work."
        ),
    )
    def move_file(source: str, dest: str) -> str:
        return run(lambda vault: notes.move(vault, source, dest), write=True)

    @server.tool(name="delete_file", description="WRITE — deletes a note for good. Prefer `close`, which keeps it.")
    def delete_file(path: str) -> str:
        return run(lambda vault: notes.delete(vault, path), write=True)

    @server.tool(
        name="list_files",
        description="CHEAP — paths only, no metadata and no bodies. `search` answers context questions better.",
    )
    def list_files(prefix: str = "") -> str:
        return run(lambda vault: _listing(vault, prefix))

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
            "keys, for hooks that read it."
        ),
    )
    def context_tool(
        path: str | None = None,
        repo: str | None = None,
        query: str | None = None,
        limit: int = 10,
        format: str = "text",
    ) -> str:
        def answer(vault: Vault) -> str:
            bundle = views.context_bundle(vault, path=path, repo=repo, query=query, limit=limit)
            return json.dumps(bundle.as_dict(), ensure_ascii=False) if format == "json" else bundle.render()

        return run(answer)

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
            limit: int = 15,
            area: str | None = None,
            priority: str | None = None,
            path: str | None = None,
            repo: str | None = None,
        ) -> str:
            def answer(vault: Vault) -> str:
                found = views.view(vault, command, query, limit, area, priority, path, repo)
                return json.dumps(found, ensure_ascii=False)

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


def run_stdio(vault: Vault) -> None:
    build_server(vault).run("stdio")


class WithoutQuery(logging.Filter):
    """Drops the query string from uvicorn's access lines, where sign-in links carry their tokens."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].partition("?")[0], *args[3:])
        return True


def uvicorn_options(auth: AuthConfig) -> dict[str, Any]:
    from uvicorn.config import LOGGING_CONFIG

    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config["filters"] = {"without_query": {"()": WithoutQuery}}
    log_config["handlers"]["access"]["filters"] = ["without_query"]
    options: dict[str, Any] = {"log_level": "info", "log_config": log_config, "proxy_headers": True}
    if auth.forwarded_allow_ips:
        options["forwarded_allow_ips"] = list(auth.forwarded_allow_ips)
    return options


def run_http(vaults: SubjectVaults, host: str, port: int, auth: AuthConfig) -> None:
    import uvicorn

    from mindin.auth.hosted import build_hosted_app

    uvicorn.run(build_hosted_app(vaults, auth, host), host=host, port=port, **uvicorn_options(auth))


def create_server() -> MCPServer:
    return build_server(open_vault(vectors="background"))
