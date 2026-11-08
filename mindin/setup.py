from __future__ import annotations

import argparse
import sys

from mindin import move
from mindin.config import ConfigError, config_path, environment_names_a_vault, read_config, write_config
from mindin.install_signal import CHANGE, QUESTION, answered
from mindin.places import CHOICES, DEFAULT_REGION, YES, Ask, Choice, Place, save, saved_place, terminal_ask
from mindin.transfer import count_text


def movable(previous: Place | None) -> tuple[Place, int] | None:
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
