from __future__ import annotations

import os
import socket
import sys
import time
import webbrowser
from collections.abc import Callable
from typing import TextIO

import httpx2

from notes_vault_mcp import credentials
from notes_vault_mcp.config import ConfigError

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLIENT_ID = "cli"
SCOPE = "vault:read vault:write"
SLOW_DOWN_STEP = 5
HTTP_TIMEOUT = 20.0


class LoginError(Exception):
    pass


def endpoints(http: httpx2.Client, base: str) -> tuple[str, str]:
    try:
        found = http.get(f"{base}/.well-known/oauth-authorization-server")
        document = found.json() if found.status_code == 200 else {}
    except (httpx2.HTTPError, ValueError):
        document = {}
    device = document.get("device_authorization_endpoint") or f"{base}/device_authorization"
    token = document.get("token_endpoint") or f"{base}/token"
    return device, token


def can_open_browser() -> bool:
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    return (
        sys.platform == "darwin"
        or os.name == "nt"
        or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    )


def error_of(response: httpx2.Response) -> tuple[str, str]:
    try:
        body = response.json()
    except ValueError:
        body = {}
    return str(body.get("error") or response.status_code), str(body.get("error_description") or "")


def device_login(
    base: str,
    name: str,
    out: TextIO,
    http: httpx2.Client,
    sleep: Callable[[float], None] = time.sleep,
    open_browser: bool = True,
) -> dict:
    device_endpoint, token_endpoint = endpoints(http, base)
    started = http.post(device_endpoint, data={"client_id": CLIENT_ID, "scope": SCOPE, "name": name})
    if started.status_code != 200:
        error, description = error_of(started)
        raise LoginError(f"{base} refused to start a sign-in: {error} {description}".rstrip())
    device = started.json()
    link = device.get("verification_uri_complete") or device["verification_uri"]
    print(f"Open {device['verification_uri']} and enter the code {device['user_code']}", file=out)
    print(f"or go straight to {link}", file=out)
    print("Waiting for you to approve it there…", file=out, flush=True)
    if open_browser and can_open_browser():
        webbrowser.open(link)
    interval = float(device.get("interval") or 5)
    deadline = time.monotonic() + float(device.get("expires_in") or 600)
    while time.monotonic() < deadline:
        sleep(interval)
        polled = http.post(
            token_endpoint,
            data={"grant_type": DEVICE_GRANT, "device_code": device["device_code"], "client_id": CLIENT_ID},
        )
        if polled.status_code == 200:
            return polled.json()
        error, description = error_of(polled)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += SLOW_DOWN_STEP
            continue
        if error == "access_denied":
            raise LoginError("the sign-in was denied; no key was stored")
        if error == "expired_token":
            raise LoginError("the code expired before it was approved; run login again")
        raise LoginError(f"{base} answered {error} {description}".rstrip())
    raise LoginError("the code expired before it was approved; run login again")


def login(
    url: str,
    name: str | None = None,
    out: TextIO | None = None,
    http: httpx2.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    open_browser: bool = True,
) -> dict:
    base = credentials.server_base(url)
    out = out or sys.stdout
    client = http or httpx2.Client(timeout=HTTP_TIMEOUT)
    try:
        granted = device_login(base, name or socket.gethostname(), out, client, sleep, open_browser)
    finally:
        if http is None:
            client.close()
    entry = {
        "key": granted["access_token"],
        "scope": granted.get("scope", ""),
        "name": granted.get("key_name", ""),
        "saved_at": int(time.time()),
    }
    credentials.save(base, entry)
    return entry


def access_words(scope: str) -> str:
    return "read and write" if "vault:write" in scope.split() else "read only"


def server_argument(raw: str | None) -> str:
    from notes_vault_mcp.config import adopt_plugin_config

    if raw:
        return credentials.server_base(raw)
    adopt_plugin_config()
    found = credentials.configured_server() or credentials.DEFAULT_SERVER_URL
    if not found:
        raise ConfigError("notes-vault-mcp: name the server, as in `notes-vault-mcp login https://vault.example.com`")
    return credentials.server_base(found)
