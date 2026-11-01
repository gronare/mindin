from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from notes_vault_mcp.config import ConfigError, env, private_dir

LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
DEFAULT_SERVER_URL = ""


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "notes-vault-mcp"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def server_base(url: str) -> str:
    base = url.strip().rstrip("/").removesuffix("/mcp").rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"notes-vault-mcp: {url!r} is not a server URL; give it as https://vault.example.com")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ConfigError(f"notes-vault-mcp: {url!r} must use https; a key is never sent over plain http")
    return base


def configured_server() -> str | None:
    raw = env("VAULT_SERVER_URL")
    return server_base(raw) if raw else None


def _read() -> dict:
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def _write(servers: dict) -> None:
    folder = private_dir(config_dir())
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".credentials-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"servers": servers}, stream, indent=2)
        os.replace(temporary, credentials_path())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def stored(url: str) -> dict | None:
    entry = _read().get(server_base(url))
    return entry if isinstance(entry, dict) and entry.get("key") else None


def save(url: str, entry: dict) -> None:
    servers = _read()
    servers[server_base(url)] = entry
    _write(servers)


def forget(url: str) -> bool:
    servers = _read()
    if servers.pop(server_base(url), None) is None:
        return False
    _write(servers)
    return True
