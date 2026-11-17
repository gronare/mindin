from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from collections.abc import Callable
from typing import TextIO

import httpx2

from mindin import credentials, login, move, provision, setup
from mindin.backends import SCHEMA_KEY, VaultError
from mindin.config import ConfigError, environment_names_a_vault
from mindin.places import CHOICES_BY_KIND, YES, Place, saved_place, server_place

Emit = Callable[..., None]


def emitter(out: TextIO) -> Emit:
    def emit(event: str, **fields: object) -> None:
        out.write(json.dumps({"event": event, **fields}) + "\n")
        out.flush()

    return emit


def errno_of(exc: BaseException) -> int | None:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, OSError) and seen.errno:
            return seen.errno
        seen = seen.__cause__ or seen.__context__
    return None


def told(exc: BaseException) -> str:
    return " ".join(str(exc).split()).removeprefix("mindin: ") or type(exc).__name__


def about(url: str, slug: str | None) -> dict:
    """The vault's layout and note count, as far as the server shows them; empty when it does not answer."""
    try:
        side = move.open_side(Place({"kind": "server", "url": url, "vault": slug or ""}))
        files = side.listing().files
        schema = side.read(SCHEMA_KEY).decode("utf-8") if SCHEMA_KEY in files else None
    except (move.MoveError, VaultError, OSError, httpx2.HTTPError, UnicodeDecodeError):
        return {}
    notes = sum(1 for key in files if key.endswith(".md") and not key.startswith("."))
    return {"layout": provision.preset_of(schema), "notes": notes}


def signed_in(url: str, args: argparse.Namespace, emit: Emit) -> tuple[str | None, dict] | None:
    slug, entry = credentials.chosen(url)
    already = entry is not None and (not args.vault or args.vault == slug)
    if not already:

        def show(device: dict) -> None:
            emit(
                "code",
                verification_uri=device["verification_uri"],
                verification_uri_complete=device.get("verification_uri_complete") or "",
                user_code=device["user_code"],
                expires_in=int(device.get("expires_in") or 600),
            )

        try:
            entry = login.login(url, open_browser=False, vault=args.vault, show=show)
        except login.LoginError as exc:
            emit("error", kind=exc.kind, message=told(exc))
            return None
        except (httpx2.HTTPError, OSError) as exc:
            emit("error", kind="unreachable", message=told(exc), errno=errno_of(exc))
            return None
        slug = entry["vault"] or None
    assert entry is not None
    emit(
        "signed_in",
        server=url,
        vault=slug or "",
        vault_name=entry.get("vault_name") or "",
        access=login.access_words(entry.get("scope", "")),
        already=already,
        **about(url, slug),
    )
    return slug, entry


def moved(previous: Place, target: Place, emit: Emit) -> int:
    said = io.StringIO()
    with contextlib.redirect_stderr(said):
        code = move.move(previous, target, move.Options(no_browser=True), None)
    sys.stderr.write(said.getvalue())
    if code != 0:
        last = next((line for line in reversed(said.getvalue().splitlines()) if line.strip()), "the move stopped")
        emit("error", kind="move", message=told(Exception(last)))
    return code


def run(args: argparse.Namespace, emit: Emit, answers: TextIO) -> int:
    if args.server is None:
        raise ConfigError("mindin: --json goes with --server URL")
    choice = CHOICES_BY_KIND["server"]
    place = server_place(args.server)
    url = place.vault["url"]
    found = setup.movable(saved_place())
    signed = signed_in(url, args, emit)
    if signed is None:
        return 1
    slug, _ = signed
    moving = False
    if found is not None:
        previous, count = found
        emit("move", files=count, source=move.describe(previous))
        moving = answers.readline().strip().lower() in YES
        if moving and moved(previous, Place({**place.vault, "vault": slug or ""}), emit) != 0:
            return 1
        if moving:
            emit("moved", files=count)
    if not moving:
        setup.settle(choice, place, environment_names_a_vault())
    if args.signals is not None:
        setup.record_signals(args.signals == "yes")
    emit("done", vault=choice.describe(place.vault))
    return 0


def command(args: argparse.Namespace, out: TextIO | None = None, answers: TextIO | None = None) -> int:
    """One JSON object a line on stdout, the move answered by a line on stdin, the rest on stderr."""
    emit = emitter(out or sys.stdout)
    with contextlib.redirect_stdout(sys.stderr):
        try:
            return run(args, emit, answers or sys.stdin)
        except ConfigError as exc:
            emit("error", kind="config", message=told(exc))
            return 2
