from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ENV_PREFIX = "CLAUDE_PLUGIN_OPTION_"
APP_NAME = "mindin"
FORMER_NAMES = ("notes-vault-mcp",)


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


def adopted(base: Path) -> Path:
    """mindin's folder under base; a former name's folder moves there first and stays reachable by that name."""
    new = base / APP_NAME
    if new.exists():
        return new
    for former in FORMER_NAMES:
        old = base / former
        if not old.is_dir():
            continue
        try:
            os.rename(old, new)
        except OSError:
            return new if new.exists() else old
        with contextlib.suppress(OSError):
            os.symlink(new, old, target_is_directory=True)
        return new
    return new


def config_dir() -> Path:
    return adopted(Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser())


def cache_dir() -> Path:
    raw = env("VAULT_CACHE_DIR")
    return private_dir(Path(raw).expanduser() if raw else adopted(Path("~/.cache").expanduser()))


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
    "mindin: no vault configured. Run `mindin setup`, or set VAULT_PATH to a directory, "
    "S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY and S3_BUCKET for an S3 bucket, "
    "or VAULT_SERVER_URL for a vault server."
)

NO_VAULT_YET = (
    "No vault is set up. Run `mindin setup` to choose where it lives, a folder, an S3 bucket or a vault server, "
    "or set VAULT_PATH, the S3_* settings or VAULT_SERVER_URL in the server's environment, then restart the client."
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


def environment_names_a_vault() -> bool:
    return local_root() is not None or s3_settings() is not None or bool(env("VAULT_SERVER_URL"))


_filled: tuple[str, dict[str, str]] = ("none", {})


def filled_names() -> set[str]:
    """The storage variables adopt_config filled from mindin's config or the plugin's, as opposed to the shell's."""
    return set(_filled[1])


def adopt_config() -> str:
    """Fill the storage variables when the environment names no vault: mindin's own config first, then the plugin's."""
    global _filled
    source, values = _filled
    if values and all(os.environ.get(name) == value for name, value in values.items()):
        return source
    if environment_names_a_vault():
        return "env"
    from mindin.places import saved_settings

    for source, found in (("config", saved_settings()), ("plugin", plugin_options())):
        if found:
            filled = {name: value for name, value in found.items() if not env(name)}
            os.environ.update(filled)
            _filled = (source, filled)
            return source
    return "none"
