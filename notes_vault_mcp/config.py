from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ENV_PREFIX = "CLAUDE_PLUGIN_OPTION_"


class ConfigError(Exception):
    pass


def env(name: str, default: str | None = None) -> str | None:
    for key in (name, PLUGIN_ENV_PREFIX + name):
        value = os.environ.get(key)
        if value:
            return value
    return default


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def cache_dir() -> Path:
    raw = env("VAULT_CACHE_DIR") or "~/.cache/notes-vault-mcp"
    return private_dir(Path(raw).expanduser())


def vault_id(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def local_root() -> Path | None:
    raw = env("VAULT_PATH")
    return Path(raw).expanduser().resolve() if raw else None


def s3_settings() -> dict[str, str] | None:
    endpoint = env("S3_ENDPOINT")
    access_key = env("S3_ACCESS_KEY")
    secret_key = env("S3_SECRET_KEY")
    bucket = env("S3_BUCKET")
    if not (endpoint and access_key and secret_key and bucket):
        return None
    return {
        "endpoint": endpoint,
        "access_key": access_key,
        "secret_key": secret_key,
        "bucket": bucket,
        "prefix": env("S3_PREFIX", "") or "",
        "region": env("S3_REGION", "us-east-1") or "us-east-1",
    }


NO_BACKEND = (
    "notes-vault-mcp: no vault configured. Set VAULT_PATH to a directory, "
    "S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY and S3_BUCKET for an S3 bucket, "
    "or VAULT_SERVER_URL for a vault server."
)


PLUGIN_NAME = "vault"
PLUGIN_OPTIONS = {
    "vault_path": "VAULT_PATH",
    "s3_endpoint": "S3_ENDPOINT",
    "s3_bucket": "S3_BUCKET",
    "s3_access_key": "S3_ACCESS_KEY",
    "s3_secret_key": "S3_SECRET_KEY",
    "s3_prefix": "S3_PREFIX",
    "s3_region": "S3_REGION",
    "vault_server_url": "VAULT_SERVER_URL",
}


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def claude_settings_path() -> Path:
    return claude_dir() / "settings.json"


def read_json(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def claude_credentials() -> dict:
    """Claude Code's .credentials.json, or on macOS the default config's keychain item."""
    try:
        return read_json((claude_dir() / ".credentials.json").read_text(encoding="utf-8"))
    except OSError:
        pass
    if sys.platform != "darwin" or os.environ.get("CLAUDE_CONFIG_DIR"):
        return {}
    try:
        found = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    return read_json(found.stdout) if found.returncode == 0 else {}


def storage_values(options: object) -> dict[str, str]:
    return {
        PLUGIN_OPTIONS[name]: value.strip()
        for name, value in (options.items() if isinstance(options, dict) else [])
        if name in PLUGIN_OPTIONS and isinstance(value, str) and value.strip()
    }


def plugin_options(settings_path: Path | None = None) -> dict[str, str]:
    """The vault plugin's options keyed by the variable each feeds; sensitive ones from pluginSecrets."""
    try:
        data = read_json((settings_path or claude_settings_path()).read_text(encoding="utf-8"))
    except OSError:
        data = {}
    configs = data.get("pluginConfigs") or {}
    for plugin, config in configs.items() if isinstance(configs, dict) else []:
        if plugin.split("@", 1)[0] != PLUGIN_NAME or not isinstance(config, dict):
            continue
        found = storage_values(config.get("options"))
        complete = {"VAULT_PATH", "VAULT_SERVER_URL"} & found.keys() or {
            "S3_ACCESS_KEY",
            "S3_SECRET_KEY",
        } <= found.keys()
        if not complete:
            found = storage_values((claude_credentials().get("pluginSecrets") or {}).get(plugin)) | found
        if found:
            return found
    return {}


def adopt_plugin_config() -> str:
    """Fill the storage variables from the vault plugin's saved config when the environment names no vault."""
    if local_root() is not None or s3_settings() is not None or env("VAULT_SERVER_URL"):
        return "env"
    found = plugin_options()
    for name, value in found.items():
        if not env(name):
            os.environ[name] = value
    return "plugin" if found else "none"
