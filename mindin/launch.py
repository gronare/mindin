from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import anyio

from mindin import credentials
from mindin.config import cache_dir, private_dir

OLDEST = "0.14.0"
FRESH_SECONDS = 600.0
PROBE_SECONDS = 5.0

Version = tuple[int, int, int]
Ask = Callable[[str], str | None]
Run = Callable[[list[str]], int]
Replace = Callable[[str, list[str]], object]


class CannotFollow(Exception):
    pass


def parsed(text: object) -> Version | None:
    parts = str(text).split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def cache_path() -> Path:
    return cache_dir() / "server-versions.json"


def _cached() -> dict:
    try:
        found = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def remembered(url: str) -> tuple[str, float] | None:
    entry = _cached().get(url)
    if not isinstance(entry, dict) or parsed(entry.get("version")) is None:
        return None
    at = entry.get("at")
    return str(entry["version"]), float(at) if isinstance(at, int | float) else 0.0


def remember(url: str, version: str, at: float) -> None:
    document = _cached() | {url: {"version": version, "at": at}}
    folder = private_dir(cache_path().parent)
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".server-versions-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2)
        os.replace(temporary, cache_path())
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def any_key(url: str) -> str | None:
    _, entry = credentials.chosen(url)
    if entry is None:
        entry = next(iter(credentials.server_state(url)["vaults"].values()), None)
    return entry["key"] if entry else None


def probe(url: str) -> str | None:
    key = any_key(url)
    if key is None:
        return None
    from mindin.remote import connect

    async def ask() -> str | None:
        with anyio.fail_after(PROBE_SECONDS):
            async with connect(url, key) as remote:
                info = remote.client.server_info
                return info.version if info is not None and info.name == "mindin" else None

    try:
        return anyio.run(ask)
    except Exception:
        return None


def server_version(url: str, ask: Ask = probe, clock: Callable[[], float] = time.time) -> str | None:
    cached = remembered(url)
    now = clock()
    if cached is not None and 0 <= now - cached[1] < FRESH_SECONDS:
        return cached[0]
    found = ask(url)
    if found is not None and parsed(found) is not None:
        remember(url, found, now)
        return found
    return cached[0] if cached is not None else None


def version_to_run(url: str, pinned: str, ask: Ask = probe, clock: Callable[[], float] = time.time) -> str:
    server = server_version(url, ask, clock)
    found, mine = parsed(server), parsed(pinned)
    if server is None or found is None or mine is None or found >= mine:
        return pinned
    oldest = parsed(OLDEST)
    assert oldest is not None
    if found < oldest:
        raise CannotFollow(
            f"{url} runs mindin {server}, older than {OLDEST}, the oldest this plugin runs; upgrade the server"
        )
    return server


def refuse(command: Sequence[str], exc: CannotFollow) -> int:
    if command[:1] == ["hook"]:
        print(f"vault: {exc}")
        return 0
    print(f"mindin: {exc}", file=sys.stderr)
    return 1


def launch(
    command: Sequence[str],
    pinned: str,
    run: Run,
    ask: Ask = probe,
    replace: Replace = os.execv,
    clock: Callable[[], float] = time.time,
) -> int:
    command = list(command[1:] if command[:1] == ["--"] else command)
    url = credentials.configured_server()
    if url is None:
        return run(command)
    try:
        version = version_to_run(url, pinned, ask, clock)
        if version == pinned:
            return run(command)
        uvx = shutil.which("uvx")
        if uvx is None:
            raise CannotFollow(f"{url} runs mindin {version}, and uvx, which runs that version here, is not installed")
    except CannotFollow as exc:
        return refuse(command, exc)
    sys.stdout.flush()
    sys.stderr.flush()
    replace(uvx, [uvx, f"mindin=={version}", *command])
    return 0
