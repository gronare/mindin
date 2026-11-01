from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from notes_vault_mcp import __version__, changelog, hooks, notes, provision, tasks
from notes_vault_mcp.auth import auth_config
from notes_vault_mcp.backends import NotFound
from notes_vault_mcp.config import ConfigError, adopt_plugin_config, local_root, s3_settings
from notes_vault_mcp.search import render, search
from notes_vault_mcp.server import run_http, run_stdio
from notes_vault_mcp.vault import Vault, open_vault

template = provision.template


def command_serve(args: argparse.Namespace) -> int:
    if args.transport != "http":
        run_stdio(open_vault(vectors="background"))
        return 0
    auth = auth_config(args.auth)
    if auth.mode in ("forwarded", "hosted"):
        from notes_vault_mcp.vault import SubjectVaults

        vaults = SubjectVaults(prefix=auth.subject_prefix, dav_url=auth.dav_url, vectors="background")
        run_http(vaults, args.host, args.port, auth)
        return 0
    run_http(open_vault(vectors="background"), args.host, args.port, auth)
    return 0


def command_init(args: argparse.Namespace) -> int:
    vault = open_vault()
    written, kept = provision.initialize(vault.backend, force=args.force)
    for key in kept:
        print(f"notes-vault-mcp: {key} exists, keeping it (--force overwrites)", file=sys.stderr)
    for key in written:
        print(f"wrote {key}", file=sys.stderr)
    print(template("claude-md-snippet.md"))
    return 0


def command_hook(args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if args.event == "session-start":
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
    vault = open_vault()
    vault.index.sync()
    if args.all:
        period = args.period or (args.repo if args.repo and changelog.PERIOD_RE.match(args.repo) else None)
        for key in changelog.write_all(vault, periods=[period] if period else None):
            print(f"wrote {key}")
        return 0
    if not args.repo or not args.period:
        print("changelog needs <repo> <period>, or --all", file=sys.stderr)
        return 2
    if not changelog.PERIOD_RE.match(args.period):
        print(f"notes-vault-mcp: period {args.period!r} is not YYYY or YYYY-MM", file=sys.stderr)
        return 2
    repo_path = Path(args.repo_path).expanduser() if args.repo_path else None
    if args.write:
        changed = changelog.write_page(vault, args.repo, args.period, repo_path)
        print(("wrote " if changed else "unchanged ") + vault.schema.period_key(args.repo, args.period))
        return 0
    print(changelog.render(vault, args.repo, args.period, repo_path))
    return 0


def command_lint(args: argparse.Namespace) -> int:
    if args.write:
        try:
            notes.note_key(args.write)
        except notes.InvalidKey as exc:
            print(one_line(exc), file=sys.stderr)
            return 2
    vault = open_vault()
    vault.index.sync(force=True)
    parked = notes.park_stale(vault) if args.park_stale else []
    findings = notes.lint(vault)
    for key in parked:
        findings.add("parked", f"{key}: status backlog, untouched for more than {vault.schema.stale_after_days} days")
    print(findings.render())
    if args.write:
        text = notes.lint_note_text(findings)
        notes._reindex(vault, args.write, text, vault.backend.put(args.write, text))
        print(f"wrote {args.write}", file=sys.stderr)
    return 0


def command_sync(args: argparse.Namespace) -> int:
    vault = open_vault(vectors="wait")
    count = vault.index.rebuild() if args.rebuild else vault.index.sync(force=True)
    print(f"indexed {count} notes of {len(vault.index.all_notes())}")
    if vault.index.vectors is not None:
        print(f"{vault.index.vectors.count()} notes carry vectors for search by meaning")
    return 0


def command_owner(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth import builtin

    return builtin.command_owner(args)


def command_tokens(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth import builtin

    return builtin.command_tokens(args)


def command_accounts(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth import hosted_store

    return hosted_store.command_accounts(args)


def command_backlog(args: argparse.Namespace) -> int:
    vault = open_vault()
    vault.index.sync()
    print(notes.render_backlog(notes.backlog(vault, area=args.area, priority=args.priority)))
    return 0


def command_search(args: argparse.Namespace) -> int:
    vault = open_vault(vectors="wait")
    vault.index.sync()
    print(render(search(vault.index, vault.schema, args.query, limit=args.limit)))
    return 0


WRITE_PROBE_KEY = ".vault/write-probe"


def package_version() -> str:
    try:
        return metadata.version("notes-vault-mcp")
    except metadata.PackageNotFoundError:
        return __version__


def one_line(exc: Exception) -> str:
    missing = isinstance(exc, NotFound) and not isinstance(exc, notes.InvalidKey)
    message = f"no such note: {exc}" if missing else " ".join(str(exc).split())
    return message if message.startswith("notes-vault-mcp:") else f"notes-vault-mcp: {message}"


def json_command(action: Callable[[argparse.Namespace, Vault], dict]) -> Callable[[argparse.Namespace], int]:
    def run(args: argparse.Namespace) -> int:
        try:
            adopt_plugin_config()
            vault = open_vault()
            try:
                vault.index.sync()
                result = action(args, vault)
            finally:
                vault.close()
        except Exception as exc:
            print(one_line(exc), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0

    return run


def task_open(args: argparse.Namespace, vault: Vault) -> dict:
    if args.note:
        return tasks.claim_task(vault, args.note, args.cwd)
    return tasks.open_task(vault, args.name or "", args.cwd, branch=args.branch)


def task_check(args: argparse.Namespace, vault: Vault) -> dict:
    need = [key.strip() for key in args.need.split(",") if key.strip()]
    return {"missing": tasks.missing_sections(vault, args.path, need)}


def task_close(args: argparse.Namespace, vault: Vault) -> dict:
    return {"path": notes.close(vault, args.path)}


def task_backlog(args: argparse.Namespace, vault: Vault) -> dict:
    notes.set_status(vault, args.path, notes.BACKLOG_STATUS, priority=args.priority)
    return {"path": args.path}


def task_claim(args: argparse.Namespace, vault: Vault) -> dict:
    return tasks.claim_note(vault, args.path, args.machine, args.task, repo=args.repo or "", force=args.force)


def task_release(args: argparse.Namespace, vault: Vault) -> dict:
    if args.path is None:
        return tasks.release_claims_of(vault, args.machine or "", args.task or "")
    return tasks.release_note(vault, args.path, machine=args.machine)


def log_append(args: argparse.Namespace, vault: Vault) -> dict:
    return tasks.append_log(vault, args.cwd, args.line, commits=tuple(args.commit or ()), branch=args.branch)


def log_path(args: argparse.Namespace, vault: Vault) -> dict:
    return {"path": vault.schema.log_key(args.repo)}


def is_writable(vault: Vault) -> bool:
    try:
        vault.backend.put(WRITE_PROBE_KEY, "")
        vault.backend.delete(WRITE_PROBE_KEY)
    except Exception:
        return False
    return True


def command_status(args: argparse.Namespace) -> int:
    adopt_plugin_config()
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
    status = {"configured": backend is not None, "version": package_version(), "backend": backend, "writable": writable}
    print(json.dumps(status))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notes-vault-mcp",
        description="MCP server and CLI for a markdown notes vault on S3 or a local directory.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--auth",
        choices=("bearer", "oidc", "builtin", "forwarded", "hosted"),
        default="bearer",
        help=(
            "http only: a static VAULT_TOKEN, an identity provider (VAULT_OIDC_ISSUER), the built-in owner login, "
            "a signed identity header from a trusted proxy (VAULT_IDENTITY_*), one vault per subject, or accounts "
            "signing in through an identity provider (VAULT_HOSTED_OIDC_*), one vault per account"
        ),
    )
    serve.set_defaults(func=command_serve)

    init = sub.add_parser("init", help="write the schema and the Obsidian bases into the vault")
    init.add_argument("--force", action="store_true", help="overwrite files that already exist")
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
    lint.set_defaults(func=command_lint)

    sync = sub.add_parser("sync", help="refresh the index")
    sync.add_argument("--rebuild", action="store_true", help="drop the index and read every note again")
    sync.set_defaults(func=command_sync)

    owner = sub.add_parser("owner", help="the built-in login's owner (--auth builtin)")
    owner_sub = owner.add_subparsers(dest="owner_command", required=True)
    set_password = owner_sub.add_parser("set-password", help="set or replace the owner's password")
    set_password.add_argument("--password", help="read from the terminal when omitted")
    set_password.set_defaults(func=command_owner)

    tokens = sub.add_parser("tokens", help="the clients the built-in login has authorized")
    tokens_sub = tokens.add_subparsers(dest="tokens_command", required=True)
    tokens_sub.add_parser("list", help="list authorized clients and their tokens").set_defaults(func=command_tokens)
    revoke = tokens_sub.add_parser("revoke", help="revoke a client's tokens")
    revoke.add_argument("client_id")
    revoke.set_defaults(func=command_tokens)

    accounts = sub.add_parser("accounts", help="the accounts of --auth hosted")
    accounts_sub = accounts.add_subparsers(dest="accounts_command", required=True)
    accounts_sub.add_parser("list", help="list accounts with status and key count").set_defaults(func=command_accounts)
    inviting = accounts_sub.add_parser("invite", help="let this email address sign in and get a vault")
    inviting.add_argument("email")
    inviting.set_defaults(func=command_accounts)
    for name, summary in (
        ("suspend", "refuse the account's keys and tokens at once"),
        ("activate", "lift a suspension"),
    ):
        changing = accounts_sub.add_parser(name, help=summary)
        changing.add_argument("account", help="the account's email or id")
        changing.set_defaults(func=command_accounts)

    queue = sub.add_parser("backlog", help="list the backlog, sorted by priority then age")
    queue.add_argument("--area", help="a system note stem, or the prefix a family of stems shares")
    queue.add_argument("--priority", help="urgent, high, medium or low")
    queue.set_defaults(func=command_backlog)

    find = sub.add_parser("search", help="search the index from the shell")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=15)
    find.set_defaults(func=command_search)

    task = sub.add_parser("task", help="the task note of one piece of work, for tools that drive agents (JSON out)")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    opening = task_sub.add_parser("open", help="create a task note for the repo, or claim a backlog note with --note")
    opening.add_argument("--name", help="the task's name; the placeholder title and the stem")
    opening.add_argument("--cwd", required=True, help="the worktree or checkout the work happens in")
    opening.add_argument("--branch", help="the branch; read from --cwd when omitted")
    opening.add_argument("--note", metavar="VAULT_PATH", help="claim this existing task note instead of creating one")
    opening.set_defaults(func=json_command(task_open))
    check = task_sub.add_parser("check", help="list the sections of a task note that are still empty")
    check.add_argument("path")
    check.add_argument(
        "--need",
        default=",".join(tasks.DEFAULT_NEED),
        help="comma-separated keys among problem, solution, decisions, tests, scope",
    )
    check.set_defaults(func=json_command(task_check))
    closing = task_sub.add_parser("close", help="set status complete and move the note to the archive")
    closing.add_argument("path")
    closing.set_defaults(func=json_command(task_close))
    parking = task_sub.add_parser("backlog", help="put the note back in the backlog")
    parking.add_argument("path")
    parking.add_argument("--priority", default="medium", help="urgent, high, medium or low")
    parking.set_defaults(func=json_command(task_backlog))
    claiming = task_sub.add_parser(
        "claim", help="write which machine and task work on the note (frontmatter key kelpie), unless another holds it"
    )
    claiming.add_argument("path")
    claiming.add_argument("--machine", required=True, help="the machine the work runs on")
    claiming.add_argument("--task", required=True, help="the task working on the note")
    claiming.add_argument("--repo", help="the repo the task works in")
    claiming.add_argument("--force", action="store_true", help="take the note over from another machine")
    claiming.set_defaults(func=json_command(task_claim))
    releasing = task_sub.add_parser(
        "release",
        help="clear the note's claim (frontmatter key kelpie), or without a path every claim of --machine --task",
    )
    releasing.add_argument("path", nargs="?")
    releasing.add_argument("--machine", help="clear it only when this machine holds it")
    releasing.add_argument("--task", help="without a path: the task whose claims to clear, wherever its notes moved")
    releasing.set_defaults(func=json_command(task_release))

    repo_log = sub.add_parser("log", help="the repo log, for tools that drive agents (JSON out)")
    log_sub = repo_log.add_subparsers(dest="log_command", required=True)
    appending = log_sub.add_parser("append", help="append a line to the log of the repo --cwd belongs to")
    appending.add_argument("--cwd", required=True, help="a worktree or checkout; the log is the main checkout's")
    appending.add_argument("--branch", help="the branch the commits were made on; read from --cwd when omitted")
    appending.add_argument("--line", required=True)
    appending.add_argument("--commit", action="append", help="a commit sha; repeat for several")
    appending.set_defaults(func=json_command(log_append))
    locating = log_sub.add_parser("path", help="the vault path of a repo's log, from the schema")
    locating.add_argument("--repo", required=True)
    locating.set_defaults(func=json_command(log_path))

    status = sub.add_parser("status", help="whether a vault is configured (JSON out, never fails, never writes)")
    status.add_argument(
        "--probe", action="store_true", help="also write and delete .vault/write-probe to measure writable"
    )
    status.set_defaults(func=command_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["serve", *(argv or [])])
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
