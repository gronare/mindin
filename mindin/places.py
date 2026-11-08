from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from mindin import credentials
from mindin.config import ConfigError, config_dir, env, environment_names_a_vault, private_dir
from mindin.install_signal import CHANGE, QUESTION, answered

CONFIG_NAME = "config.toml"
DEFAULT_REGION = "us-east-1"
YES = ("y", "yes")

Ask = Callable[[str, bool], str]


@dataclass(frozen=True)
class Place:
    vault: dict[str, str]
    keys: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Choice:
    kind: str
    label: str
    flag: str
    metavar: str
    help: str
    given: Callable[[str, argparse.Namespace, Ask | None], Place]
    ask: Callable[[Ask], Place]
    settings: Callable[[dict], dict[str, str]]
    describe: Callable[[dict], str]
    after: Callable[[Place, argparse.Namespace], int] = lambda place, args: 0


def config_path() -> Path:
    return config_dir() / CONFIG_NAME


def read_config() -> dict:
    try:
        return tomllib.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def render_config(config: dict) -> str:
    lines: list[str] = []
    for table, values in config.items():
        if isinstance(values, dict):
            lines += [f"[{table}]", *(f"{key} = {toml_value(value)}" for key, value in values.items()), ""]
    return "\n".join(lines)


def write_config(config: dict) -> None:
    folder = private_dir(config_dir())
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".config-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(render_config(config))
        os.replace(temporary, config_path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def saved_vault() -> dict:
    found = read_config().get("vault")
    return found if isinstance(found, dict) else {}


def saved_settings() -> dict[str, str]:
    """The storage variables mindin's own config names, empty when it names no vault."""
    vault = saved_vault()
    choice = CHOICES_BY_KIND.get(str(vault.get("kind")))
    return {name: value for name, value in choice.settings(vault).items() if value} if choice else {}


def saved_place() -> Place | None:
    vault = {name: str(value) for name, value in saved_vault().items()}
    return Place(vault) if vault.get("kind") in CHOICES_BY_KIND else None


def movable(previous: Place | None) -> tuple[Place, int] | None:
    from mindin import move

    if previous is None or previous.vault["kind"] not in ("folder", "s3"):
        return None
    count = move.file_count(previous)
    return (previous, count) if count else None


def settle(choice: Choice, place: Place, overridden: bool) -> None:
    save(place)
    print(f"This machine's vault is {choice.describe(place.vault)}, saved in {config_path()}.")
    if overridden:
        print("The environment names another vault, and it decides while it is set; `mindin status` shows which.")


def offer_move(
    found: tuple[Place, int], choice: Choice, place: Place, args: argparse.Namespace, ask: Ask | None, overridden: bool
) -> int:
    from mindin import move
    from mindin.transfer import count_text

    previous, count = found
    where = move.describe(previous)
    url = place.vault["url"]
    if ask is None:
        settle(choice, place, overridden)
        print(
            f"{where[0].upper()}{where[1:]} still holds {count_text(count)} files and stays as it was. To bring them"
            " here, run `mindin setup` in a terminal, or set it back and run"
            f" `mindin move --to --server {url}`."
        )
        return 0
    if ask(f"Bring {count_text(count)} files from {where} along? [Y/n]", False).strip().lower() not in ("", *YES):
        settle(choice, place, overridden)
        print(f"{where[0].upper()}{where[1:]} stays as it was; `mindin move` brings it later.")
        return 0
    target = Place({**place.vault, "vault": args.vault or ""})
    code = move.move(previous, target, move.Options(no_browser=args.no_browser), ask)
    if code != 0:
        vault = f" --vault {args.vault}" if args.vault else ""
        print(
            f"mindin: this machine still uses {where}; `mindin setup --server {url}{vault}` offers the move again"
            " and skips the files already copied.",
            file=sys.stderr,
        )
    elif overridden:
        print("The environment names another vault, and it decides while it is set; `mindin status` shows which.")
    return code


def save(place: Place) -> None:
    if place.vault["kind"] == "s3" and place.keys:
        credentials.save_s3_keys(place.vault["endpoint"], place.vault["bucket"], **place.keys)
    write_config(read_config() | {"vault": place.vault})


def required(answer: str, what: str) -> str:
    if not answer.strip():
        raise ConfigError(f"mindin: setup needs {what}")
    return answer.strip()


def folder_place(raw: str) -> Place:
    return Place({"kind": "folder", "path": str(Path(raw).expanduser().resolve())})


def folder_given(raw: str, args: argparse.Namespace, ask: Ask | None) -> Place:
    place = folder_place(raw)
    if not Path(place.vault["path"]).is_dir():
        raise ConfigError(f"mindin: no folder at {place.vault['path']}; make it first, or run setup in a terminal")
    return place


def folder_asked(ask: Ask) -> Place:
    place = folder_place(required(ask("The vault's folder", False), "a folder"))
    folder = Path(place.vault["path"])
    if not folder.is_dir():
        if ask(f"{folder} does not exist. Create it? [y/N]", False).strip().lower() not in YES:
            raise ConfigError(f"mindin: no folder at {folder}; nothing saved")
        folder.mkdir(parents=True)
    return place


def s3_place(endpoint: str, bucket: str, prefix: str, region: str, access_key: str, secret_key: str) -> Place:
    vault = {"kind": "s3", "endpoint": endpoint.rstrip("/"), "bucket": bucket, "prefix": prefix, "region": region}
    return Place(vault, {"access_key": access_key, "secret_key": secret_key})


def s3_given(bucket: str, args: argparse.Namespace, ask: Ask | None) -> Place:
    if not args.endpoint:
        raise ConfigError("mindin: --s3 needs --endpoint, the S3 or MinIO address")
    keys = [env("S3_ACCESS_KEY"), env("S3_SECRET_KEY")]
    if not all(keys) and ask is None:
        raise ConfigError("mindin: --s3 reads the keys from S3_ACCESS_KEY and S3_SECRET_KEY; set both")
    access_key = keys[0] or required(ask("Access key", False), "an access key")
    secret_key = keys[1] or required(ask("Secret key", True), "a secret key")
    return s3_place(args.endpoint, bucket, args.prefix or "", args.region or DEFAULT_REGION, access_key, secret_key)


def s3_asked(ask: Ask) -> Place:
    endpoint = required(ask("Endpoint, e.g. https://s3.example.com", False), "an endpoint")
    bucket = required(ask("Bucket", False), "a bucket")
    prefix = ask("Prefix inside the bucket (empty for none)", False).strip()
    region = ask(f"Region [{DEFAULT_REGION}]", False).strip() or DEFAULT_REGION
    access_key = required(ask("Access key", False), "an access key")
    secret_key = required(ask("Secret key", True), "a secret key")
    return s3_place(endpoint, bucket, prefix, region, access_key, secret_key)


def s3_variables(vault: dict) -> dict[str, str]:
    endpoint, bucket = str(vault.get("endpoint") or ""), str(vault.get("bucket") or "")
    keys = credentials.s3_keys(endpoint, bucket) if endpoint and bucket else {}
    return {
        "S3_ENDPOINT": endpoint,
        "S3_BUCKET": bucket,
        "S3_PREFIX": str(vault.get("prefix") or ""),
        "S3_REGION": str(vault.get("region") or ""),
        "S3_ACCESS_KEY": keys.get("access_key", ""),
        "S3_SECRET_KEY": keys.get("secret_key", ""),
    }


def s3_describe(vault: dict) -> str:
    prefix = f"/{vault['prefix']}" if vault.get("prefix") else ""
    where = f"the bucket {vault.get('bucket')}{prefix} at {vault.get('endpoint')}"
    return f"{where}, its keys in {credentials.credentials_path()}"


def server_place(raw: str) -> Place:
    return Place({"kind": "server", "url": credentials.server_base(raw)})


def server_asked(ask: Ask) -> Place:
    return server_place(required(ask("The server's address, e.g. https://vault.example.com", False), "an address"))


def server_signed_in(place: Place, args: argparse.Namespace) -> int:
    from mindin.cli import command_login

    url = place.vault["url"]
    vault, entry = credentials.chosen(url)
    if entry is not None and (not args.vault or args.vault == vault):
        print(f"Already signed in to {url}; `mindin login {url}` signs in again.")
        return 0
    login = argparse.Namespace(url=url, name=None, no_browser=args.no_browser, vault=args.vault)
    return command_login(login)


CHOICES: tuple[Choice, ...] = (
    Choice(
        kind="folder",
        label="A folder on this machine",
        flag="--folder",
        metavar="PATH",
        help="the vault is this folder",
        given=folder_given,
        ask=folder_asked,
        settings=lambda vault: {"VAULT_PATH": str(vault.get("path") or "")},
        describe=lambda vault: f"the folder {vault.get('path')}",
    ),
    Choice(
        kind="s3",
        label="An S3 bucket",
        flag="--s3",
        metavar="BUCKET",
        help="the vault is this bucket; with --endpoint, the keys from S3_ACCESS_KEY and S3_SECRET_KEY",
        given=s3_given,
        ask=s3_asked,
        settings=s3_variables,
        describe=s3_describe,
    ),
    Choice(
        kind="server",
        label="A vault server",
        flag="--server",
        metavar="URL",
        help="the vault is on this server; signs the machine in when it is not yet",
        given=lambda raw, args, ask: server_place(raw),
        ask=server_asked,
        settings=lambda vault: {"VAULT_SERVER_URL": str(vault.get("url") or "")},
        describe=lambda vault: f"the server {vault.get('url')}",
        after=server_signed_in,
    ),
)
CHOICES_BY_KIND = {choice.kind: choice for choice in CHOICES}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    where = parser.add_mutually_exclusive_group()
    for choice in CHOICES:
        where.add_argument(choice.flag, dest=choice.kind, metavar=choice.metavar, help=choice.help)
    parser.add_argument("--endpoint", help="with --s3: the S3 or MinIO address")
    parser.add_argument("--prefix", help="with --s3: the vault's prefix inside the bucket")
    parser.add_argument("--region", help=f"with --s3: the region (default {DEFAULT_REGION})")
    parser.add_argument("--vault", help="with --server: the vault's slug, as in /mcp?vault=<slug>")
    parser.add_argument("--no-browser", action="store_true", help="with --server: only print the sign-in link")
    parser.add_argument(
        "--signals", choices=("yes", "no"), help="share anonymous usage data once a day (asked in a terminal)"
    )


def record_signals(share: bool) -> int:
    write_config(read_config() | {"signals": {"install": share}})
    print(f"Anonymous usage data: {'on' if share else 'off'}; `{CHANGE}` changes it.")
    return 0


def ask_signals(ask: Ask) -> int:
    return record_signals(ask(f"{QUESTION} [y/N]", False).strip().lower() in YES)


def terminal_ask(question: str, secret: bool) -> str:
    try:
        return getpass.getpass(f"{question}: ") if secret else input(f"{question}: ")
    except EOFError as exc:
        raise ConfigError("mindin: setup stopped before it was done; nothing saved") from exc


def pick(ask: Ask) -> Choice:
    print("Where does your vault live?")
    for number, choice in enumerate(CHOICES, 1):
        print(f"  {number}  {choice.label}")
    while True:
        answer = ask(f"Choose 1-{len(CHOICES)}", False).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(CHOICES):
            return CHOICES[int(answer) - 1]
        print(f"Answer a number from 1 to {len(CHOICES)}.")


def command_setup(args: argparse.Namespace, ask: Ask | None = None) -> int:
    interactive = ask or (terminal_ask if sys.stdin.isatty() else None)
    given = [(choice, getattr(args, choice.kind)) for choice in CHOICES if getattr(args, choice.kind) is not None]
    if args.signals is not None and not given:
        return record_signals(args.signals == "yes")
    if given:
        choice, value = given[0]
        place = choice.given(value, args, interactive)
    elif interactive is None:
        flags = ", ".join(f"{choice.flag} {choice.metavar}" for choice in CHOICES)
        raise ConfigError(f"mindin: setup asks in a terminal; a script names the vault with one of {flags}")
    else:
        choice = pick(interactive)
        place = choice.ask(interactive)
    overridden = environment_names_a_vault()
    found = movable(saved_place()) if choice.kind == "server" else None
    if found is None:
        settle(choice, place, overridden)
    code = choice.after(place, args)
    if code == 0 and found is not None:
        code = offer_move(found, choice, place, args, interactive, overridden)
    if code == 0 and args.signals is not None:
        record_signals(args.signals == "yes")
    elif code == 0 and interactive is not None and answered() is None:
        ask_signals(interactive)
    return code
