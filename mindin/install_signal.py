from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import platform
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

import httpx2

from mindin import __version__
from mindin.config import adopted, env, private_dir

log = logging.getLogger(__name__)

ENDPOINT = "https://signals.mindin.dev"
DOMAIN = b"mindin-install-v1"
POW_DOMAIN = b"kebbie-report-pow-v1"
MAX_WORK = 26
DAY = 86_400
EVERY = 3600
ID = "install-id"
LEDGER = "install-signal.json"
LOCK = "install-signal.lock"
ID_RE = re.compile(r"[0-9a-f]{32}")
NAME_RE = re.compile(r"[a-z0-9._+-]{1,64}")
OFF = ("off", "false", "0", "no")
ON = ("on", "true", "1", "yes")
OS_NAMES = {"darwin": "macos", "win32": "windows", "cygwin": "windows"}
ARCH_NAMES = {"arm64": "aarch64", "amd64": "x86_64", "x64": "x86_64", "i386": "x86", "i686": "x86"}
TIMEOUT = httpx2.Timeout(20.0)
QUESTION = (
    "Share anonymous usage data once a day? A random install id, mindin's version, the OS and CPU type and how "
    "mindin runs (plugin, CLI or server) go to signals.mindin.dev; never notes, names, paths or your vault's address."
)
CHANGE = "mindin setup --signals yes|no"


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Install:
    id: str
    version: str
    os: str
    arch: str
    runs: str

    def digest(self) -> bytes:
        data = bytearray(DOMAIN)
        for text in (self.id, self.version, self.os, self.arch, self.runs):
            encoded = text.encode("utf-8")
            data += len(encoded).to_bytes(4, "big") + encoded
        return hashlib.sha256(bytes(data)).digest()


def state_dir() -> Path:
    """XDG_STATE_HOME/mindin, else beside VAULT_CACHE_DIR (the image's volume), else ~/.local/state/mindin."""
    if os.environ.get("XDG_STATE_HOME"):
        return adopted(Path(os.environ["XDG_STATE_HOME"]).expanduser())
    cache = env("VAULT_CACHE_DIR")
    if cache:
        return Path(cache).expanduser().parent / "state"
    return adopted(Path("~/.local/state").expanduser())


def answered() -> bool | None:
    from mindin.places import read_config

    table = read_config().get("signals")
    return table["install"] if isinstance(table, dict) and isinstance(table.get("install"), bool) else None


def switch() -> tuple[bool, str | None]:
    """Whether the install signal is on, and what decided it: env, config, or None while nobody was asked."""
    raw = (env("VAULT_SIGNALS") or "").strip().lower()
    if raw in OFF or raw in ON:
        return raw in ON, "env"
    found = answered()
    return (found, "config") if found is not None else (False, None)


def quiet() -> bool:
    return bool(os.environ.get("CI")) or "pytest" in sys.modules


def runs_as() -> str:
    return "plugin" if os.environ.get("CLAUDE_PLUGIN_ROOT") else "cli"


def name(text: str) -> str:
    text = text.strip().lower()
    return text if NAME_RE.fullmatch(text) else "unknown"


def os_name() -> str:
    return name(OS_NAMES.get(sys.platform) or sys.platform.rstrip("0123456789"))


def arch_name() -> str:
    machine = platform.machine().lower()
    return name(ARCH_NAMES.get(machine, machine))


def version() -> str:
    try:
        return name(metadata.version("mindin"))
    except metadata.PackageNotFoundError:
        return name(__version__)


def install_id(state: Path) -> str:
    file = state / ID
    with contextlib.suppress(OSError, UnicodeDecodeError):
        found = file.read_text(encoding="utf-8").strip()
        if ID_RE.fullmatch(found):
            return found
    made = secrets.token_hex(16)
    private_dir(state)
    file.write_text(made + "\n", encoding="utf-8")
    return made


def payload(state: Path, runs: str) -> Install:
    return Install(id=install_id(state), version=version(), os=os_name(), arch=arch_name(), runs=name(runs))


def forget(state: Path) -> None:
    for file in (ID, LEDGER):
        with contextlib.suppress(OSError):
            (state / file).unlink()


def ledger_day(state: Path) -> int | None:
    with contextlib.suppress(OSError, ValueError, AttributeError):
        day = json.loads((state / LEDGER).read_text(encoding="utf-8")).get("day")
        return day if isinstance(day, int) else None
    return None


# The day is written down before the send, so a failed or interrupted one is dropped rather than tried again that day.
def tick(state: Path, enabled: bool, now: float, runs: str, send: Callable[[Install], object]) -> bool:
    if not enabled:
        forget(state)
        return False
    today = int(now) // DAY
    if ledger_day(state) == today:
        return False
    try:
        private_dir(state)
        (state / LEDGER).write_text(json.dumps({"day": today}), encoding="utf-8")
        install = payload(state, runs)
    except OSError:
        return False
    with contextlib.suppress(Exception):
        send(install)
    return True


def nonce_bytes(text: str) -> bytes:
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if len(raw) != 32:
        raise Refused("the challenge's nonce is not 32 bytes")
    return raw


def solve(nonce: bytes, digest: bytes, difficulty: int) -> int:
    prefix = hashlib.sha256(POW_DOMAIN + nonce + digest)
    shift = 256 - difficulty
    counter = 0
    while True:
        attempt = prefix.copy()
        attempt.update(counter.to_bytes(8, "big"))
        if int.from_bytes(attempt.digest(), "big") >> shift == 0:
            return counter
        counter += 1


def send(install: Install, base: str = ENDPOINT, transport: httpx2.BaseTransport | None = None) -> None:
    with httpx2.Client(base_url=base, timeout=TIMEOUT, transport=transport) as client:
        answer = client.get("/v1/reports/challenge")
        answer.raise_for_status()
        challenge = answer.json()
        if not isinstance(challenge, dict) or not isinstance(challenge.get("difficulty"), int):
            raise Refused("the signals endpoint answered no challenge")
        if not 0 <= challenge["difficulty"] <= MAX_WORK:
            raise Refused(f"the signals endpoint asks for {challenge['difficulty']} bits of work, more than {MAX_WORK}")
        counter = solve(nonce_bytes(str(challenge.get("nonce", ""))), install.digest(), challenge["difficulty"])
        body = {"mindin": asdict(install), "challenge": challenge, "counter": counter}
        client.post("/v1/signals", json=body).raise_for_status()


@contextlib.contextmanager
def one_sender(state: Path) -> Iterator[bool]:
    try:
        import fcntl
    except ImportError:
        yield True
        return
    private_dir(state)
    with open(state / LOCK, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def beat(runs: str, send: Callable[[Install], object] = send) -> bool:
    state = state_dir()
    if not switch()[0]:
        return tick(state, False, time.time(), runs, send)
    with one_sender(state) as alone:
        return alone and tick(state, True, time.time(), runs, send)


def keep_sending(runs: str) -> None:
    while True:
        try:
            beat(runs)
        except Exception:
            log.debug("mindin: the install signal was skipped", exc_info=True)
        time.sleep(EVERY)


def start(runs: str) -> threading.Thread | None:
    if quiet():
        return None
    thread = threading.Thread(target=keep_sending, args=(runs,), name="mindin-install-signal", daemon=True)
    thread.start()
    return thread
