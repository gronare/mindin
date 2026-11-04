from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

from mindin.config import ConfigError, config_dir, env, private_dir

LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
DEFAULT_SERVER_URL = ""
STATE_FIELDS = ("vaults", "default", "dirs")
working_dir: ContextVar[str | None] = ContextVar("working_dir", default=None)


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def server_base(url: str) -> str:
    base = url.strip().rstrip("/").removesuffix("/mcp").rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"mindin: {url!r} is not a server URL; give it as https://vault.example.com")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ConfigError(f"mindin: {url!r} must use https; a key is never sent over plain http")
    return base


def configured_server() -> str | None:
    raw = env("VAULT_SERVER_URL")
    return server_base(raw) if raw else None


def _document() -> dict:
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _section(name: str) -> dict:
    found = _document().get(name)
    return found if isinstance(found, dict) else {}


def _read() -> dict:
    return _section("servers")


def _write_section(name: str, value: dict) -> None:
    document = _document() | {name: value}
    folder = private_dir(config_dir())
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".credentials-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2)
        os.replace(temporary, credentials_path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _write(servers: dict) -> None:
    _write_section("servers", servers)


def _bucket(endpoint: str, bucket: str) -> str:
    return f"{endpoint.rstrip('/')}#{bucket}"


def s3_keys(endpoint: str, bucket: str) -> dict[str, str]:
    found = _section("s3").get(_bucket(endpoint, bucket))
    if not isinstance(found, dict):
        return {}
    return {name: found[name] for name in ("access_key", "secret_key") if isinstance(found.get(name), str)}


def save_s3_keys(endpoint: str, bucket: str, access_key: str, secret_key: str) -> None:
    buckets = _section("s3") | {_bucket(endpoint, bucket): {"access_key": access_key, "secret_key": secret_key}}
    _write_section("s3", buckets)


def _state(raw: object) -> dict:
    entry = raw if isinstance(raw, dict) else {}
    listed = entry.get("vaults")
    vaults = {
        str(slug): key
        for slug, key in (listed.items() if isinstance(listed, dict) else [])
        if isinstance(key, dict) and key.get("key")
    }
    if entry.get("key"):
        vaults.setdefault("", {name: value for name, value in entry.items() if name not in STATE_FIELDS})
    dirs = entry.get("dirs")
    default = entry.get("default")
    return {
        "vaults": vaults,
        "default": default if isinstance(default, str) else None,
        "dirs": {str(path): slug for path, slug in dirs.items() if isinstance(slug, str)}
        if isinstance(dirs, dict)
        else {},
    }


def server_state(url: str) -> dict:
    return _state(_read().get(server_base(url)))


def _save_state(url: str, state: dict) -> None:
    servers = _read()
    servers[server_base(url)] = state
    _write(servers)


@contextlib.contextmanager
def working_in(cwd: str | None) -> Iterator[None]:
    token = working_dir.set(cwd or None)
    try:
        yield
    finally:
        working_dir.reset(token)


def _here(cwd: str | None) -> Path | None:
    try:
        return Path(cwd or working_dir.get() or os.getcwd()).expanduser().resolve()
    except OSError:
        return None


def vault_for(url: str, cwd: str | None = None) -> str | None:
    state = server_state(url)
    here = _here(cwd)
    if here is not None:
        matches = [Path(folder) for folder in state["dirs"] if Path(folder) == here or Path(folder) in here.parents]
        if matches:
            return state["dirs"][str(max(matches, key=lambda folder: len(folder.parts)))]
    return state["default"]


def chosen(url: str, cwd: str | None = None) -> tuple[str | None, dict | None]:
    state = server_state(url)
    slug = vault_for(url, cwd)
    vaults = state["vaults"]
    if slug is not None:
        return slug, vaults.get(slug)
    if "" in vaults:
        return None, vaults[""]
    return None, next(iter(vaults.values())) if len(vaults) == 1 else None


def stored(url: str, cwd: str | None = None) -> dict | None:
    return chosen(url, cwd)[1]


def save(url: str, entry: dict, vault: str | None = None) -> None:
    state = server_state(url)
    slug = vault or ""
    state["vaults"][slug] = entry
    if slug and state["default"] is None:
        state["default"] = slug
    _save_state(url, state)


def use(url: str, slug: str, folder: Path | None = None) -> None:
    state = server_state(url)
    if folder is None:
        state["default"] = slug
    else:
        state["dirs"][str(folder.expanduser().resolve())] = slug
    _save_state(url, state)


def drop_dir(url: str, folder: Path) -> bool:
    state = server_state(url)
    if state["dirs"].pop(str(folder.expanduser().resolve()), None) is None:
        return False
    _save_state(url, state)
    return True


def forget(url: str, vault: str | None = None) -> bool:
    servers = _read()
    base = server_base(url)
    if base not in servers:
        return False
    if vault is None:
        servers.pop(base)
    else:
        state = _state(servers[base])
        if state["vaults"].pop(vault, None) is None:
            return False
        if state["default"] == vault:
            state["default"] = None
        servers[base] = state
    _write(servers)
    return True
