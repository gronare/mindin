from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from notes_vault_mcp import __version__, changelog, credentials, hooks, notes, provision, tasks
from notes_vault_mcp.auth import auth_config
from notes_vault_mcp.backends import NotFound
from notes_vault_mcp.config import ConfigError, adopt_plugin_config, local_root, s3_settings
from notes_vault_mcp.embedding import keep_warm
from notes_vault_mcp.presets import PRESETS
from notes_vault_mcp.schema import CLAIM_KEY
from notes_vault_mcp.search import render, search
from notes_vault_mcp.server import run_http, run_stdio
from notes_vault_mcp.vault import SubjectVaults, Vault, open_vault

template = provision.template


def server_url() -> str | None:
    return credentials.configured_server()


def command_serve(args: argparse.Namespace) -> int:
    url = server_url() if args.transport != "http" else None
    if url is not None:
        from notes_vault_mcp.proxy import proxy_stdio

        return proxy_stdio(url)
    if args.transport != "http":
        vault = open_vault(vectors="background")
        vault.start_embedding()
        run_stdio(vault)
        return 0
    auth = dataclasses.replace(auth_config(), plans=args.plans == "on")
    vaults = SubjectVaults(prefix=auth.subject_prefix, dav_url=auth.dav_url, vectors="background")
    keep_warm(vaults.embedder)
    run_http(vaults, args.host, args.port, auth)
    return 0


def command_init(args: argparse.Namespace) -> int:
    url = server_url()
    if url is not None:
        from notes_vault_mcp import remote

        if args.preset or args.force:
            print(f"notes-vault-mcp: a server vault picks and switches its layout on {url}/account", file=sys.stderr)
            return 2
        for line in remote.tool_text(url, "init").splitlines():
            print(f"notes-vault-mcp: {line}" if line.startswith("kept ") else line, file=sys.stderr)
        print(template("claude-md-snippet.md"))
        return 0
    vault = open_vault()
    result = provision.initialize(vault.backend, force=args.force, preset=args.preset)
    print(f"layout: {result.preset}", file=sys.stderr)
    if result.preset not in PRESETS:
        print(
            f"notes-vault-mcp: the schema names {result.preset!r}, which this version does not ship; it is read "
            "as written and kept. Switch with --preset <name> --force",
            file=sys.stderr,
        )
        return 0
    for key in result.kept:
        print(f"notes-vault-mcp: {key} exists, keeping it (--force overwrites)", file=sys.stderr)
    for line in result.lines():
        if not line.startswith("kept "):
            print(line, file=sys.stderr)
    if result.backup:
        print("No note was moved or deleted.", file=sys.stderr)
    print(template("claude-md-snippet.md"))
    return 0


def command_hook(args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if args.event == "session-start":
        try:
            url = server_url()
            if url is None:
                print(hooks.session_start(raw))
            else:
                from notes_vault_mcp import remote

                print(remote.session_start(url, raw))
        except Exception as exc:
            print(f"vault: {exc}")
        return 0
    try:
        url = server_url()
        if url is None:
            blocked = hooks.stop(raw)
        else:
            from notes_vault_mcp import remote

            blocked = remote.stop(url, raw)
    except Exception:
        return 0
    if blocked:
        print(blocked)
    return 0


def changelog_remote(url: str, args: argparse.Namespace) -> int:
    from notes_vault_mcp import remote

    if not args.all:
        if not args.repo or not args.period:
            print("changelog needs <repo> <period>, or --all", file=sys.stderr)
            return 2
        if not changelog.PERIOD_RE.match(args.period):
            print(f"notes-vault-mcp: period {args.period!r} is not YYYY or YYYY-MM", file=sys.stderr)
            return 2
    for line in remote.changelog_command(url, args):
        print(line)
    return 0


def command_changelog(args: argparse.Namespace) -> int:
    url = server_url()
    if url is not None:
        return changelog_remote(url, args)
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
    url = server_url()
    if url is not None:
        from notes_vault_mcp import remote

        if args.write or args.park_stale:
            print(remote.tool_text(url, "lint_and_park", write=args.write, park=bool(args.park_stale)))
        else:
            print(remote.tool_text(url, "lint"))
    else:
        vault = open_vault()
        vault.index.sync(force=True)
        print(notes.lint_and_park(vault, write=args.write, park=args.park_stale))
    if args.write:
        print(f"wrote {args.write}", file=sys.stderr)
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


def command_accounts(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth import hosted_store

    return hosted_store.command_accounts(args)


def command_delete_account(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth import hosted_keys

    return hosted_keys.command_delete_account(args)


def command_import(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth import hosted_transfer

    return hosted_transfer.command_import(args)


def command_master_key(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth import hosted_keys

    return hosted_keys.command_master_key(args)


def command_support(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth import hosted_access

    return hosted_access.command_support(args)


def add_operator(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operator", required=True, help="who on the service's staff does this")
    parser.add_argument("--reason", required=True, help="why; the account sees it in its access log")


def command_admin(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth import hosted_store

    return hosted_store.command_admin(args)


REMOTE_BACKLOG_LIMIT = 10000


def command_backlog(args: argparse.Namespace) -> int:
    url = server_url()
    if url is not None:
        from notes_vault_mcp import remote

        print(remote.tool_text(url, "backlog", area=args.area, priority=args.priority, limit=REMOTE_BACKLOG_LIMIT))
        return 0
    vault = open_vault()
    vault.index.sync()
    print(notes.render_backlog(notes.backlog(vault, area=args.area, priority=args.priority), vault))
    return 0


def command_search(args: argparse.Namespace) -> int:
    url = server_url()
    if url is not None:
        from notes_vault_mcp import remote

        print(remote.tool_text(url, "search", query=args.query, limit=args.limit))
        return 0
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
    def local(args: argparse.Namespace) -> dict:
        vault = open_vault()
        try:
            vault.index.sync()
            return action(args, vault)
        finally:
            vault.close()

    def run(args: argparse.Namespace) -> int:
        try:
            adopt_plugin_config()
            url = server_url()
            if url is not None:
                from notes_vault_mcp import remote

                result = remote.json_action(url, action.__name__, args)
            else:
                result = local(args)
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


def remote_status(url: str, probe: bool) -> dict:
    from notes_vault_mcp import remote

    signed_in = credentials.stored(url) is not None
    writable: bool | None = None
    if probe:
        try:
            writable = bool(remote.whoami(url)["write"]) if signed_in else False
        except Exception:
            writable = False
    return {
        "configured": signed_in,
        "version": package_version(),
        "backend": "server",
        "writable": writable,
        "server": url,
        "signed_in": signed_in,
    }


def command_status(args: argparse.Namespace) -> int:
    adopt_plugin_config()
    try:
        url = server_url()
    except ConfigError:
        url = None
    if url is not None:
        print(json.dumps(remote_status(url, args.probe)))
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
    status = {"configured": backend is not None, "version": package_version(), "backend": backend, "writable": writable}
    print(json.dumps(status))
    return 0


def command_login(args: argparse.Namespace) -> int:
    from notes_vault_mcp import login

    url = login.server_argument(args.url)
    try:
        entry = login.login(url, name=args.name, open_browser=not args.no_browser)
    except (login.LoginError, OSError) as exc:
        print(one_line(exc), file=sys.stderr)
        return 1
    print(f"Signed in to {url}: key {entry['name']!r}, {login.access_words(entry['scope'])}.")
    print(f"The key is stored in {credentials.credentials_path()}.")
    return 0


def command_logout(args: argparse.Namespace) -> int:
    from notes_vault_mcp import login

    url = login.server_argument(args.url)
    if not credentials.forget(url):
        print(f"Not signed in to {url}.")
        return 0
    print(f"Forgot the key for {url}. It stays valid until you revoke it at {url}/account.")
    return 0


def command_whoami(args: argparse.Namespace) -> int:
    from notes_vault_mcp import login, remote

    url = login.server_argument(args.url)
    entry = credentials.stored(url)
    if entry is None:
        print(one_line(remote.NotSignedIn(url)), file=sys.stderr)
        return 1
    try:
        found = remote.whoami(url)
    except Exception as exc:
        print(one_line(exc), file=sys.stderr)
        return 1
    print(
        json.dumps({"server": url, "email": found.get("email"), "key": entry.get("name"), "write": found.get("write")})
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notes-vault-mcp",
        description="MCP server and CLI for a markdown notes vault on S3, a local directory or a vault server.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--plans",
        choices=("off", "on"),
        default="off",
        help="http only: on checks each account's billing plan on every request; off ignores plans",
    )
    serve.set_defaults(func=command_serve)

    init = sub.add_parser("init", help="write the schema and the Obsidian bases into the vault")
    init.add_argument("--force", action="store_true", help="overwrite files that already exist")
    init.add_argument(
        "--preset",
        choices=tuple(PRESETS),
        help=(
            "the layout to write; a new vault defaults to default, an existing one keeps its own. "
            "With --force it switches an existing vault's schema and views; notes are never moved or deleted"
        ),
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
    lint.set_defaults(func=command_lint)

    sync = sub.add_parser("sync", help="refresh the index")
    sync.add_argument("--rebuild", action="store_true", help="drop the index and read every note again")
    sync.set_defaults(func=command_sync)

    accounts = sub.add_parser("accounts", help="the accounts of the HTTP server")
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
    deleting = accounts_sub.add_parser(
        "delete", help="destroy the account's data key, objects and index, then the account itself"
    )
    deleting.add_argument("account", help="the account's email or id")
    deleting.add_argument("--yes", action="store_true", help="confirm; the vault cannot be recovered")
    deleting.set_defaults(func=command_delete_account)
    importing = accounts_sub.add_parser(
        "import", help="seal every file of a plaintext vault into the account (needs its support grant)"
    )
    importing.add_argument("account", help="the account's email or id")
    origin = importing.add_mutually_exclusive_group(required=True)
    origin.add_argument("--from-path", help="a local vault folder")
    origin.add_argument("--from-s3-bucket", help="a bucket; credentials from IMPORT_S3_ACCESS_KEY/SECRET_KEY")
    importing.add_argument("--from-s3-prefix", default="", help="the vault's prefix in that bucket")
    importing.add_argument("--from-s3-endpoint", help="defaults to S3_ENDPOINT")
    importing.add_argument("--from-s3-region", help="defaults to S3_REGION, then us-east-1")
    add_operator(importing)
    importing.set_defaults(func=command_import)

    master = sub.add_parser("master-key", help="the master key that wraps each hosted account's data key")
    master_sub = master.add_subparsers(dest="master_key_command", required=True)
    master_sub.add_parser("generate", help="print a fresh base64 master key").set_defaults(func=command_master_key)
    master_sub.add_parser("status", help="count account keys per master key").set_defaults(func=command_master_key)
    master_sub.add_parser(
        "rewrap", help="wrap every account key under VAULT_HOSTED_MASTER_KEY, unwrapping with the previous keys"
    ).set_defaults(func=command_master_key)

    support = sub.add_parser("support", help="open an account's vault under the support grant it gave on /account")
    support_sub = support.add_subparsers(dest="support_command", required=True)
    listing = support_sub.add_parser("files", help="list the vault's paths")
    listing.add_argument("account", help="the account's email or id")
    add_operator(listing)
    listing.set_defaults(func=command_support)
    reading = support_sub.add_parser("read", help="print one file")
    reading.add_argument("account", help="the account's email or id")
    reading.add_argument("path")
    add_operator(reading)
    reading.set_defaults(func=command_support)

    admin = sub.add_parser("admin", help="tokens for the admin API of the HTTP server")
    admin_sub = admin.add_subparsers(dest="admin_command", required=True)
    minting = admin_sub.add_parser("token", help="make (or replace) a named admin token and print it once")
    minting.add_argument("--name", required=True)
    minting.add_argument("--scope", choices=("billing",), required=True)
    minting.set_defaults(func=command_admin)
    revoking = admin_sub.add_parser("revoke", help="revoke the admin token with this name")
    revoking.add_argument("name")
    revoking.set_defaults(func=command_admin)

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
        "claim",
        help=f"write which machine and task work on the note (frontmatter key {CLAIM_KEY}), unless another holds it",
    )
    claiming.add_argument("path")
    claiming.add_argument("--machine", required=True, help="the machine the work runs on")
    claiming.add_argument("--task", required=True, help="the task working on the note")
    claiming.add_argument("--repo", help="the repo the task works in")
    claiming.add_argument("--force", action="store_true", help="take the note over from another machine")
    claiming.set_defaults(func=json_command(task_claim))
    releasing = task_sub.add_parser(
        "release",
        help=f"clear the note's claim (frontmatter key {CLAIM_KEY}), or without a path every claim of --machine --task",
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

    signing_in = sub.add_parser("login", help="sign this machine in to a vault server and store its key")
    signing_in.add_argument("url", nargs="?", help="the server, e.g. https://vault.example.com; VAULT_SERVER_URL")
    signing_in.add_argument("--name", help="the key's name on the server; the host name when omitted")
    signing_in.add_argument("--no-browser", action="store_true", help="only print the link, never open a browser")
    signing_in.set_defaults(func=command_login)
    signing_out = sub.add_parser("logout", help="forget the key this machine holds for a vault server")
    signing_out.add_argument("url", nargs="?", help="the server; VAULT_SERVER_URL when omitted")
    signing_out.set_defaults(func=command_logout)
    asking = sub.add_parser("whoami", help="the account and access of the stored key (JSON out)")
    asking.add_argument("url", nargs="?", help="the server; VAULT_SERVER_URL when omitted")
    asking.set_defaults(func=command_whoami)

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
    except Exception as exc:
        from notes_vault_mcp.remote import RemoteError

        if not isinstance(exc, RemoteError):
            raise
        print(one_line(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
