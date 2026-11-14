from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from mindin import (
    __version__,
    changelog,
    credentials,
    hooks,
    human,
    install_signal,
    note_templates,
    notes,
    provision,
    setup,
)
from mindin.backends import NotFound, VersionConflict
from mindin.client import ReaderGone, open_client
from mindin.config import FORMER_NAMES, NO_VAULT_YET, ConfigError, cache_dir, env, local_root, s3_settings
from mindin.embedding import keep_warm
from mindin.extensions import installed
from mindin.frontmatter import parse
from mindin.hosted import cli as hosted_cli
from mindin.hosted.config import auth_config
from mindin.places import adopt_config
from mindin.presets import CUSTOM_PRESET, PERSONAL_LAYOUTS, PRESETS, STARTING_POINTS
from mindin.schema import CLAIM_KEY, Schema, capabilities, schema_from_text, template_line
from mindin.server import run_stdio
from mindin.terminal import Terminal, terminal
from mindin.vault import SubjectVaults, Unconfigured, Vault, open_vault

template = provision.template


def server_url() -> str | None:
    return credentials.configured_server()


def command_serve(args: argparse.Namespace) -> int:
    install_signal.start("server" if args.transport == "http" else install_signal.runs_as())
    url = server_url() if args.transport != "http" else None
    if url is not None:
        from mindin.proxy import proxy_stdio

        return proxy_stdio(url)
    if args.transport != "http":
        if local_root() is None and s3_settings() is None:
            run_stdio(Unconfigured(NO_VAULT_YET))
            return 0
        vault = open_vault(vectors="background")
        vault.start_embedding()
        run_stdio(vault)
        return 0
    auth = auth_config()
    vaults = SubjectVaults(prefix=auth.subject_prefix, dav_url=auth.dav_url, vectors="background")
    keep_warm(vaults.embedder)
    from mindin.hosted import app

    app.run_http(vaults, args.host, args.port, auth)
    return 0


def command_init(args: argparse.Namespace) -> int:
    url = server_url()
    if url is not None:
        from mindin import remote

        if args.preset or args.force or args.start_from:
            print(f"mindin: a server vault picks and switches its layout on {url}/account", file=sys.stderr)
            return 2
        for line in remote.tool_text(url, "init").splitlines():
            print(f"mindin: {line}" if line.startswith("kept ") else line, file=sys.stderr)
        print(template("claude-md-snippet.md"))
        return 0
    if args.start_from and args.preset != CUSTOM_PRESET:
        print(f"mindin: --from goes with --preset {CUSTOM_PRESET}", file=sys.stderr)
        return 2
    vault = open_vault()
    result = provision.initialize(vault.backend, force=args.force, preset=args.preset, start_from=args.start_from)
    print(f"layout: {result.preset}", file=sys.stderr)
    if result.preset not in PRESETS:
        print(
            f"mindin: the schema names {result.preset!r}, which this version does not ship; it is read "
            "as written and kept. Switch with --preset <name> --force",
            file=sys.stderr,
        )
        return 0
    for key in result.kept:
        print(f"mindin: {key} exists, keeping it (--force overwrites)", file=sys.stderr)
    for line in result.lines():
        if not line.startswith("kept "):
            print(line, file=sys.stderr)
    if result.backup:
        print("No note was moved or deleted.", file=sys.stderr)
    print(template("claude-md-snippet.md"))
    return 0


def command_hook(args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    with credentials.working_in(str(hooks.read_hook_input(raw).get("cwd") or "")):
        return run_hook(args.event, raw)


def run_hook(event: str, raw: str) -> int:
    if event == "session-start":
        try:
            print(hooks.session_start(raw))
        except Exception as exc:
            print(f"vault: {exc}")
        return 0
    try:
        blocked = hooks.stop(raw)
    except Exception:
        return 0
    if blocked:
        print(blocked)
    return 0


def command_changelog(args: argparse.Namespace) -> int:
    if not args.all:
        if not args.repo or not args.period:
            print("changelog needs <repo> <period>, or --all", file=sys.stderr)
            return 2
        if not changelog.PERIOD_RE.match(args.period):
            print(f"mindin: period {args.period!r} is not YYYY or YYYY-MM", file=sys.stderr)
            return 2
    with open_client() as client:
        if args.all:
            period = args.period or (args.repo if args.repo and changelog.PERIOD_RE.match(args.repo) else None)
            lines = [f"wrote {key}" for key in client.write_changelogs(period)]
        else:
            repo_path = Path(args.repo_path).expanduser() if args.repo_path else None
            lines = [client.changelog(args.repo, args.period, repo_path, bool(args.write))]
    for line in lines:
        print(line)
    return 0


def lint_text(findings: notes.Findings, kind: str | None, term: Terminal, stale_after_days: int) -> str:
    if not term.pretty:
        return (findings.only(kind) if kind else findings).render()
    if kind:
        return human.render_lint_kind(findings, kind, term, stale_after_days)
    return human.render_lint(findings, term, stale_after_days)


def command_lint(args: argparse.Namespace) -> int:
    if args.write:
        try:
            notes.note_key(args.write)
        except notes.InvalidKey as exc:
            print(one_line(exc), file=sys.stderr)
            return 2
    kind = human.lint_kind_named(args.kind) if args.kind else None
    if args.kind and kind is None:
        known = ", ".join(human.option_name(name) for name in human.LINT_KINDS)
        print(f"mindin: no finding kind named {args.kind!r}; the kinds are: {known}", file=sys.stderr)
        return 2
    term = terminal(args.plain)
    with open_client() as client:
        text, findings = client.lint(args.write, bool(args.park_stale))
        if not term.pretty and not kind:
            print(text)
        else:
            print(lint_text(findings, kind, term, client.stale_after_days()))
    if args.write:
        print(f"wrote {args.write}", file=sys.stderr)
    return 0


def schema_report(schema: Schema) -> dict:
    return {
        "layout": schema.preset,
        "problems": list(schema.problems),
        "notices": list(schema.notices),
        "folders": [{"name": folder.name, "kind": folder.kind} for folder in schema.folders.values()],
        "capabilities": [{"tool": c.tool, "on": c.on, "reason": c.reason} for c in capabilities(schema)],
    }


def schema_report_text(report: dict) -> str:
    lines = [f"note: {notice}" for notice in report["notices"]]
    if report["problems"]:
        lines.append(f"problems, so the vault runs on the {report['layout']} layout until they are fixed:")
        lines += [f"- {problem}" for problem in report["problems"]]
    else:
        lines.append(f"no problems; layout {report['layout']}")
    folders = ", ".join(f"{folder['name']} ({folder['kind']})" for folder in report["folders"]) or "none"
    lines.append(f"folders: {folders}")
    for found in report["capabilities"]:
        lines.append(f"{found['tool']}: {'on' if found['on'] else 'off'} ({found['reason']})")
    return "\n".join(lines)


def command_schema_check(args: argparse.Namespace) -> int:
    if args.path:
        try:
            schema = schema_from_text(Path(args.path).expanduser().read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"mindin: cannot read {args.path}: {exc.strerror or exc}", file=sys.stderr)
            return 2
    else:
        with open_client() as client:
            schema = client.schema()
    report = schema_report(schema)
    print(json.dumps(report, ensure_ascii=False) if args.json else schema_report_text(report))
    return 1 if report["problems"] else 0


def command_template(args: argparse.Namespace) -> int:
    try:
        adopt_config()
        with open_client() as client:
            schema = client.schema()
        found = note_templates.template_answer(schema, args.kind)
    except Exception as exc:
        print(one_line(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(found, ensure_ascii=False))
        return 0
    chosen = [found] if args.kind else list(found["templates"].values())
    for entry in chosen:
        template = schema.template(entry["kind"])
        print(template_line(template, schema.language)[2:] if template else f"{entry['kind']}: no template")
    if not chosen:
        print("This vault's notes have no templates.")
    return 0


def command_embed_worker(args: argparse.Namespace) -> int:
    from mindin import embed_worker
    from mindin.embedding import model_name

    host, port = embed_worker.listen_address(args.listen)
    token = env(embed_worker.TOKEN_ENV)
    embed_worker.check_token(host, token)
    folder = cache_dir() / "models"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    embed_worker.listen(host, port, args.model or model_name(), str(folder), token)
    return 0


def command_sync(args: argparse.Namespace) -> int:
    url = server_url()
    if url is not None:
        print(f"{url} keeps the index; nothing to refresh on this machine")
        return 0
    vault = open_vault(vectors="wait")
    count = vault.index.rebuild() if args.rebuild else vault.index.sync(force=True)
    print(f"indexed {count} notes of {len(vault.index.all_notes())}")
    if vault.index.vectors is not None:
        print(f"{vault.index.vectors.count()} notes carry vectors for search by meaning")
    return 0


def command_backlog(args: argparse.Namespace) -> int:
    term = terminal(args.plain)
    with open_client() as client:
        found = client.backlog_view(args.area, args.priority) if term.pretty else None
        if found is not None:
            print(human.render_backlog(found, term, paths=args.paths, show_all=args.all))
            return 0
        print(client.backlog_text(args.area, args.priority))
    return 0


def command_search(args: argparse.Namespace) -> int:
    term = terminal(args.plain)
    with open_client(vectors="wait") as client:
        found = client.search_view(args.query, args.limit) if term.pretty else None
        if found is not None:
            print(human.render_search(found, term))
            return 0
        print(client.search_text(args.query, args.limit))
    return 0


def context_view(cwd: str) -> dict | None:
    with open_client() as client:
        return client.context_view(cwd)


def command_context(args: argparse.Namespace) -> int:
    cwd = os.path.abspath(os.path.expanduser(args.path)) if args.path else os.getcwd()
    term = terminal(args.plain)
    with credentials.working_in(cwd):
        found = context_view(cwd) if term.pretty else None
        if found is None:
            print(hooks.session_start(json.dumps({"cwd": cwd})))
            return 0
        print(human.render_context(found, hooks.note_commits(cwd, found["system"]), term))
    return 0


WRITE_PROBE_KEY = ".vault/write-probe"


def package_version() -> str:
    try:
        return metadata.version("mindin")
    except metadata.PackageNotFoundError:
        return __version__


def one_line(exc: Exception) -> str:
    missing = isinstance(exc, NotFound) and not isinstance(exc, notes.InvalidKey)
    message = f"no such note: {exc}" if missing else " ".join(str(exc).split())
    return message if message.startswith("mindin:") else f"mindin: {message}"


def json_command(action: str) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        try:
            adopt_config()
            with credentials.working_in(getattr(args, "cwd", None)), open_client() as client:
                result = getattr(client, action)(args)
        except Exception as exc:
            print(one_line(exc), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0

    return run


def quiet_stdout() -> None:
    with contextlib.suppress(OSError, ValueError):
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def command_watch(args: argparse.Namespace) -> int:
    def emit(found: dict) -> None:
        try:
            print(json.dumps(found, ensure_ascii=False), flush=True)
        except OSError as exc:
            raise ReaderGone(str(exc)) from exc

    try:
        adopt_config()
        with open_client() as client:
            client.watch(args, emit)
    except KeyboardInterrupt:
        return 0
    except ReaderGone:
        quiet_stdout()
        return 0
    except Exception as exc:
        print(one_line(exc), file=sys.stderr)
        return 1
    return 0


def command_write(args: argparse.Namespace) -> int:
    content = sys.stdin.read()
    try:
        adopt_config()
        with open_client() as client:
            found = client.write_note(args.path, content, args.etag)
    except Exception as exc:
        message = one_line(exc)
        if args.etag and (isinstance(exc, VersionConflict) or "changed since it was read" in message):
            message = f"mindin: the etag {args.etag} no longer matches {args.path}; it changed since it was read"
        print(message, file=sys.stderr)
        return 1
    print(json.dumps(found, ensure_ascii=False))
    return 0


def read_note(path: str) -> str:
    with open_client() as client:
        return client.read(path)


def command_read(args: argparse.Namespace) -> int:
    try:
        adopt_config()
        if args.json:
            with open_client() as client:
                print(json.dumps(client.read_stored(args.path), ensure_ascii=False))
            return 0
        text = read_note(args.path)
    except Exception as exc:
        print(one_line(exc), file=sys.stderr)
        return 1
    term = terminal(args.plain or args.raw)
    pretty = human.render_note(args.path, text, term, body_only=args.body) if term.pretty else None
    if pretty is not None:
        print(pretty)
        return 0
    if args.body:
        text = parse(text)[1]
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def is_writable(vault: Vault) -> bool:
    try:
        vault.backend.put(WRITE_PROBE_KEY, "")
        vault.backend.delete(WRITE_PROBE_KEY)
    except Exception:
        return False
    return True


def remote_status(url: str, probe: bool, source: str | None = None) -> dict:
    from mindin import remote

    vault, entry = credentials.chosen(url)
    signed_in = entry is not None
    writable: bool | None = None
    found: dict = {}
    if probe:
        try:
            found = remote.whoami(url) if signed_in else {}
            writable = bool(found["write"]) if signed_in else False
        except Exception:
            writable = False
    return {
        "configured": signed_in,
        "version": package_version(),
        "backend": "server",
        "source": source,
        "writable": writable,
        "server": url,
        "vault": vault,
        "signed_in": signed_in,
    } | vault_kind(found)


def vault_kind(found: dict) -> dict:
    if not found.get("vault_kind"):
        return {}
    kind = {"vault_kind": found["vault_kind"]}
    return kind | {"role": found.get("role")} if found["vault_kind"] == "shared" else kind


def signals_status() -> dict:
    on, decided = install_signal.switch()
    shown = "not asked" if decided is None else "on" if on else "off"
    return {"signals": shown, "signals_source": decided, "signals_change": install_signal.CHANGE}


def command_status(args: argparse.Namespace) -> int:
    found = adopt_config()
    source = found if found != "none" else None
    try:
        url = server_url()
    except ConfigError:
        url = None
    if url is not None:
        print(json.dumps(remote_status(url, args.probe, source) | signals_status()))
        return 0
    backend = "local" if local_root() is not None else "s3" if s3_settings() is not None else None
    writable: bool | None = None if not args.probe else False
    if backend is not None and args.probe:
        try:
            vault = open_vault()
            try:
                writable = is_writable(vault)
            finally:
                vault.close()
        except Exception:
            writable = False
    status = {
        "configured": backend is not None,
        "version": package_version(),
        "backend": backend,
        "source": source,
        "writable": writable,
    }
    print(json.dumps(status | signals_status()))
    return 0


def command_login(args: argparse.Namespace) -> int:
    from mindin import login

    return login.sign_in(server_argument(args.url), args.name, args.no_browser, args.vault)


def server_argument(raw: str | None) -> str:
    if raw:
        return credentials.server_base(raw)
    adopt_config()
    found = credentials.configured_server() or credentials.DEFAULT_SERVER_URL
    if not found:
        raise ConfigError("mindin: name the server, as in `mindin login https://vault.example.com`")
    return credentials.server_base(found)


def command_logout(args: argparse.Namespace) -> int:

    url = server_argument(args.url)
    if not credentials.forget(url, args.vault):
        print(f"Not signed in to {url}{f' for the vault {args.vault}' if args.vault else ''}.")
        return 0
    which = f"the key for the vault {args.vault}" if args.vault else "every key"
    print(f"Forgot {which} on {url}. A key stays valid until you revoke it at {url}/account.")
    return 0


def command_whoami(args: argparse.Namespace) -> int:
    from mindin import remote

    url = server_argument(args.url)
    vault, entry = credentials.chosen(url)
    if entry is None:
        print(one_line(remote.NotSignedIn(url, vault)), file=sys.stderr)
        return 1
    try:
        found = remote.whoami(url)
    except Exception as exc:
        print(one_line(exc), file=sys.stderr)
        return 1
    answer = {"server": url, "email": found.get("email"), "vault": found.get("vault") or vault or None}
    print(json.dumps(answer | vault_kind(found) | {"key": entry.get("name"), "write": found.get("write")}))
    return 0


def command_vault(args: argparse.Namespace) -> int:

    url = server_argument(args.url)
    if args.vault_command == "use":
        folder = Path(args.dir) if args.dir else None
        state = credentials.server_state(url)
        credentials.use(url, args.slug, folder)
        where = f"{folder.expanduser().resolve()} and below" if folder else "this machine, by default"
        print(f"{where} uses the vault {args.slug} on {url}.")
        if args.slug not in state["vaults"]:
            print(f"Sign in to it with `mindin login {url} --vault {args.slug}`.")
        return 0
    if args.vault_command == "drop":
        folder = Path(args.dir)
        if not credentials.drop_dir(url, folder):
            print(f"{folder.expanduser().resolve()} names no vault.")
            return 0
        print(f"{folder.expanduser().resolve()} uses the default vault again.")
        return 0
    state = credentials.server_state(url)
    here, _ = credentials.chosen(url)
    listed = {
        "server": url,
        "here": here,
        "default": state["default"],
        "vaults": sorted(slug for slug in state["vaults"] if slug),
        "dirs": state["dirs"],
    }
    print(json.dumps(listed))
    return 0


CONTRACT = 1
CAPABILITIES = {
    "tasks": ("task",),
    "claims": ("task", "claim"),
    "log": ("log",),
    "snapshot": ("snapshot",),
    "changes": ("changes",),
    "watch": ("watch",),
    "read": ("read",),
    "write": ("write",),
    "templates": ("template",),
}


def subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def has_command(parser: argparse.ArgumentParser, path: tuple[str, ...]) -> bool:
    for name in path:
        found = subcommands(parser).get(name)
        if found is None:
            return False
        parser = found
    return True


def about(parser: argparse.ArgumentParser) -> dict:
    return {
        "name": "mindin",
        "version": package_version(),
        "contract": CONTRACT,
        "capabilities": [name for name, path in CAPABILITIES.items() if has_command(parser, path)],
        "former_names": list(FORMER_NAMES),
    }


def command_about(args: argparse.Namespace) -> int:
    found = about(build_parser())
    if args.json:
        print(json.dumps(found))
        return 0
    print(f"{found['name']} {found['version']}, formerly {', '.join(found['former_names'])}")
    print(f"commands for tools: {', '.join(found['capabilities'])} (contract {found['contract']})")
    return 0


def command_setup(args: argparse.Namespace) -> int:
    return setup.command_setup(args)


def command_move(args: argparse.Namespace) -> int:
    from mindin import move

    return move.command_move(args)


def add_feed_arguments(parser: argparse.ArgumentParser, since: str) -> None:
    parser.add_argument("--json", action="store_true", help="answer JSON, the only output there is")
    parser.add_argument("--since", metavar="CURSOR", help=since)
    parser.add_argument("--body", action="store_true", help="each note's body too, besides its frontmatter")
    parser.add_argument("--limit", type=int, help="the most notes in one answer (default 100)")


def add_plain(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plain", action="store_true", help="the plain output a pipe gets, also in a terminal")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mindin",
        description="MCP server and CLI for a markdown notes vault on S3, a local directory or a vault server.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=command_serve)

    worker = sub.add_parser(
        "embed-worker", help="run the search-by-meaning model in a process of its own that a server reaches over TCP"
    )
    worker.add_argument("--listen", default="127.0.0.1:7400", help="host:port to listen on (default 127.0.0.1:7400)")
    worker.add_argument("--model", help="the model to load (default VAULT_SEMANTIC_MODEL)")
    worker.set_defaults(func=command_embed_worker)

    init = sub.add_parser("init", help="write the schema and the Obsidian bases into the vault")
    init.add_argument("--force", action="store_true", help="overwrite files that already exist")
    init.add_argument(
        "--preset",
        choices=PERSONAL_LAYOUTS,
        help=(
            "the layout to write; a new vault defaults to default, an existing one keeps its own. "
            "With --force it switches an existing vault's schema and views; notes are never moved or deleted"
        ),
    )
    init.add_argument(
        "--from",
        dest="start_from",
        choices=STARTING_POINTS,
        help="with --preset custom: copy this layout's schema and views to edit, instead of an empty schema",
    )
    init.set_defaults(func=command_init)

    hook = sub.add_parser("hook", help="Claude Code hook entry points, reading the hook JSON on stdin")
    hook.add_argument("event", choices=("session-start", "stop"))
    hook.set_defaults(func=command_hook)

    log = sub.add_parser("changelog", help="print or write the log lines, commits and notes for a period")
    log.add_argument("repo", nargs="?")
    log.add_argument("period", nargs="?", help="YYYY-MM or YYYY")
    log.add_argument("--repo-path", help="the git checkout to read commits from")
    log.add_argument("--write", action="store_true", help="write the period page into the vault instead of printing")
    log.add_argument("--all", action="store_true", help="write the period pages for every repo the hooks have seen")
    log.set_defaults(func=command_changelog)

    lint = sub.add_parser("lint", help="report vault drift")
    lint.add_argument("--write", metavar="VAULT_PATH", help="also write the findings as a note")
    lint.add_argument(
        "--park-stale",
        action="store_true",
        help="set status backlog on open task notes untouched for longer than the schema's stale_after_days",
    )
    lint.add_argument("--kind", help="list the findings of one kind, e.g. orphans or unresolved-links")
    add_plain(lint)
    lint.set_defaults(func=command_lint)

    schema = sub.add_parser("schema", help="the vault's .vault/schema.yml")
    schema_sub = schema.add_subparsers(dest="schema_command", required=True)
    checking = schema_sub.add_parser(
        "check", help="list the schema's problems and what its layout supports; exit 1 when it has problems"
    )
    checking.add_argument("path", nargs="?", help="a schema file to check instead of the vault's own")
    checking.add_argument("--json", action="store_true", help="the report as JSON, for tools")
    checking.set_defaults(func=command_schema_check)

    sync = sub.add_parser("sync", help="refresh the index")
    sync.add_argument("--rebuild", action="store_true", help="drop the index and read every note again")
    sync.set_defaults(func=command_sync)

    hosted_cli.add_commands(sub)

    queue = sub.add_parser("backlog", help="list the backlog, sorted by priority then age")
    queue.add_argument("--area", help="a system note stem, or the prefix a family of stems shares")
    queue.add_argument("--priority", help="urgent, high, medium or low")
    queue.add_argument("--paths", action="store_true", help="show each note's vault path")
    queue.add_argument("--all", action="store_true", help="show every item, not only the first 20")
    add_plain(queue)
    queue.set_defaults(func=command_backlog)

    printing = sub.add_parser("read", help="print one note, as read_file reads it, without the etag line")
    printing.add_argument("path", help="the note's vault path, e.g. Projects/export.md")
    printing.add_argument("--body", action="store_true", help="only the text after the frontmatter")
    printing.add_argument("--raw", action="store_true", help="print the file exactly as stored")
    printing.add_argument(
        "--json", action="store_true", help='answer {"text": the whole note, "etag": its version} for a later write'
    )
    add_plain(printing)
    printing.set_defaults(func=command_read)

    writing = sub.add_parser(
        "write", help="write one note from stdin through the same checks as write_file (JSON out: path and etag)"
    )
    writing.add_argument("path", help="the note's vault path, e.g. Projects/export.md")
    writing.add_argument(
        "--etag", help="write only over this version, from read --json; * creates a note that must not exist yet"
    )
    writing.set_defaults(func=command_write)

    find = sub.add_parser("search", help="search the index from the shell")
    find.add_argument("query")
    find.add_argument("--limit", type=int, help="the most rows to show; the schema's search.default_limit when omitted")
    add_plain(find)
    find.set_defaults(func=command_search)

    briefing = sub.add_parser("context", help="what a session in this repo starts with: its notes, commits and log")
    briefing.add_argument("--path", help="the folder to read the context for; the current directory when omitted")
    add_plain(briefing)
    briefing.set_defaults(func=command_context)

    task = sub.add_parser("task", help="the task note of one piece of work, for tools that drive agents (JSON out)")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    opening = task_sub.add_parser("open", help="create a task note for the repo, or claim a backlog note with --note")
    opening.add_argument("--name", help="the task's name; the placeholder title and the stem")
    opening.add_argument("--cwd", required=True, help="the worktree or checkout the work happens in")
    opening.add_argument("--branch", help="the branch; read from --cwd when omitted")
    opening.add_argument("--note", metavar="VAULT_PATH", help="claim this existing task note instead of creating one")
    opening.set_defaults(func=json_command("task_open"))
    check = task_sub.add_parser("check", help="list the sections of a task note that are still empty")
    check.add_argument("path")
    check.add_argument(
        "--need",
        help="comma-separated keys of the task template's sections (mindin template --kind task); its required ones "
        "when omitted",
    )
    check.set_defaults(func=json_command("task_check"))
    closing = task_sub.add_parser("close", help="set status complete and move the note to the archive")
    closing.add_argument("path")
    closing.set_defaults(func=json_command("task_close"))
    parking = task_sub.add_parser("backlog", help="put the note back in the backlog")
    parking.add_argument("path")
    parking.add_argument("--priority", default="medium", help="urgent, high, medium or low")
    parking.set_defaults(func=json_command("task_backlog"))
    claiming = task_sub.add_parser(
        "claim",
        help=f"write which machine and task work on the note (frontmatter key {CLAIM_KEY}), unless another holds it",
    )
    claiming.add_argument("path")
    claiming.add_argument("--machine", required=True, help="the machine the work runs on")
    claiming.add_argument("--task", required=True, help="the task working on the note")
    claiming.add_argument("--repo", help="the repo the task works in")
    claiming.add_argument("--force", action="store_true", help="take the note over from another machine")
    claiming.set_defaults(func=json_command("task_claim"))
    releasing = task_sub.add_parser(
        "release",
        help=f"clear the note's claim (frontmatter key {CLAIM_KEY}), or without a path every claim of --machine --task",
    )
    releasing.add_argument("path", nargs="?")
    releasing.add_argument("--machine", help="clear it only when this machine holds it")
    releasing.add_argument("--task", help="without a path: the task whose claims to clear, wherever its notes moved")
    releasing.set_defaults(func=json_command("task_release"))

    repo_log = sub.add_parser("log", help="the repo log, for tools that drive agents (JSON out)")
    log_sub = repo_log.add_subparsers(dest="log_command", required=True)
    appending = log_sub.add_parser("append", help="append a line to the log of the repo --cwd belongs to")
    appending.add_argument("--cwd", required=True, help="a worktree or checkout; the log is the main checkout's")
    appending.add_argument("--branch", help="the branch the commits were made on; read from --cwd when omitted")
    appending.add_argument("--line", required=True)
    appending.add_argument("--commit", action="append", help="a commit sha; repeat for several")
    appending.set_defaults(func=json_command("log_append"))
    locating = log_sub.add_parser("path", help="the vault path of a repo's log, from the schema")
    locating.add_argument(
        "--repo", required=True, action="append", help='repeat for several; then the answer is {"<repo>": "<path>"}'
    )
    locating.set_defaults(func=json_command("log_path"))

    shaping = sub.add_parser(
        "template", help="the headings each kind of note is written to, from the vault's schema (JSON with --json)"
    )
    shaping.add_argument("--kind", help="one kind, such as task or system; every kind with a template when omitted")
    shaping.add_argument("--json", action="store_true", help="answer JSON, for tools that read it")
    shaping.set_defaults(func=command_template)

    board = sub.add_parser(
        "snapshot",
        help="the system and task notes, open-task openings and repo log tails in one answer, for boards (JSON out)",
    )
    board.add_argument("--json", action="store_true", help="answer JSON, the only output there is")
    board.add_argument(
        "--since", metavar="ETAG", help='answer only {"unchanged": true, "etag": ...} while the etag still holds'
    )
    board.set_defaults(func=json_command("snapshot"))

    following = sub.add_parser(
        "changes",
        help="the notes changed since a cursor, each once in its latest state, and the next cursor (JSON out)",
    )
    add_feed_arguments(
        following,
        "the cursor of the previous answer; without it, reset is true and the answer carries the cursor to follow from",
    )
    following.add_argument(
        "--wait", type=float, default=0, metavar="SECONDS", help="hold the answer until something changes or this ends"
    )
    following.set_defaults(func=json_command("changes"))
    watching = sub.add_parser(
        "watch", help="follow the vault: one JSON line now with the cursor, then one per batch of changes"
    )
    add_feed_arguments(watching, "the cursor to follow from; without it, the first line has reset true")
    watching.set_defaults(func=command_watch)

    signing_in = sub.add_parser("login", help="sign this machine in to a vault server and store its key")
    signing_in.add_argument("url", nargs="?", help="the server, e.g. https://vault.example.com; VAULT_SERVER_URL")
    signing_in.add_argument("--name", help="the key's name on the server; the host name when omitted")
    signing_in.add_argument("--no-browser", action="store_true", help="only print the link, never open a browser")
    signing_in.add_argument(
        "--vault", help="the vault's slug, as in /mcp?vault=<slug>; the approval page asks without it"
    )
    signing_in.set_defaults(func=command_login)
    signing_out = sub.add_parser("logout", help="forget the keys this machine holds for a vault server")
    signing_out.add_argument("url", nargs="?", help="the server; VAULT_SERVER_URL when omitted")
    signing_out.add_argument("--vault", help="forget only this vault's key")
    signing_out.set_defaults(func=command_logout)
    vault = sub.add_parser("vault", help="which of your vaults on a server this machine and each folder use")
    vault_sub = vault.add_subparsers(dest="vault_command", required=True)
    showing = vault_sub.add_parser("list", help="the signed-in vaults, the default and the folders (JSON out)")
    showing.add_argument("--url", help="the server; VAULT_SERVER_URL when omitted")
    showing.set_defaults(func=command_vault)
    using = vault_sub.add_parser("use", help="make a vault the default, or the one a folder and its subfolders use")
    using.add_argument("slug")
    using.add_argument("--dir", help="a folder, such as a repo; without it the vault becomes the default")
    using.add_argument("--url", help="the server; VAULT_SERVER_URL when omitted")
    using.set_defaults(func=command_vault)
    dropping = vault_sub.add_parser("drop", help="let a folder use the default vault again")
    dropping.add_argument("--dir", required=True)
    dropping.add_argument("--url", help="the server; VAULT_SERVER_URL when omitted")
    dropping.set_defaults(func=command_vault)
    asking = sub.add_parser("whoami", help="the account and access of the stored key (JSON out)")
    asking.add_argument("url", nargs="?", help="the server; VAULT_SERVER_URL when omitted")
    asking.set_defaults(func=command_whoami)

    choosing = sub.add_parser("setup", help="choose where this machine's vault lives, saved in mindin's own config")
    setup.add_arguments(choosing)
    choosing.set_defaults(func=command_setup)

    moving = sub.add_parser(
        "move", help="copy this machine's vault to a folder, a bucket or a server, check it, then switch to it"
    )
    from mindin.move import add_arguments as add_move_arguments

    add_move_arguments(moving)
    moving.set_defaults(func=command_move)

    telling = sub.add_parser("about", help="the name, version and commands for tools of this mindin")
    telling.add_argument("--json", action="store_true", help="answer JSON, for tools that read it")
    telling.set_defaults(func=command_about)

    status = sub.add_parser("status", help="whether a vault is configured (JSON out, never fails, never writes)")
    status.add_argument(
        "--probe", action="store_true", help="also write and delete .vault/write-probe to measure writable"
    )
    status.set_defaults(func=command_status)

    for extension in installed():
        extension.commands(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["serve", *(argv or [])])
    try:
        if args.command != "setup":
            adopt_config()
        return int(args.func(args))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        from mindin.remote import RemoteError

        if not isinstance(exc, RemoteError):
            raise
        print(one_line(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
