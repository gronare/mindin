from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx2

from mindin import config, credentials, login, places
from mindin.backends import MUST_NOT_EXIST, SCHEMA_KEY, NotFound, VaultBackend, VaultError, VersionConflict
from mindin.config import PLUGIN_OPTIONS, ConfigError
from mindin.hosted.accounts.limits import content_type_of, full, limits_of, refusal, size_text
from mindin.places import Ask, Place
from mindin.presets import PRESETS
from mindin.provision import WELCOME_KEY
from mindin.transfer import count_text, skipped_as, skipped_lines

DOCS = "https://github.com/gronare/mindin/blob/main/docs"
HTTP_TIMEOUT = httpx2.Timeout(30.0, read=120.0)
DAV = "{DAV:}"
PROGRESS_EVERY = 100
PROVISIONED = frozenset({SCHEMA_KEY, WELCOME_KEY, *(name for preset in PRESETS.values() for name in preset.files)})
STORAGE_NAMES = tuple(dict.fromkeys(PLUGIN_OPTIONS.values()))
NOT_MOVED = (
    "Not moved, as they belong to the server and not to the vault: passkeys, API and WebDAV keys,"
    " claude.ai connectors and the history of shared vaults. Make them again on the new place."
)


class MoveError(VaultError):
    pass


@dataclass
class Listing:
    files: dict[str, int] = field(default_factory=dict)
    folders: set[str] = field(default_factory=set)


class Side(Protocol):
    label: str

    def listing(self) -> Listing: ...

    def read(self, key: str) -> bytes: ...

    def create(self, key: str, data: bytes) -> None: ...

    def replace(self, key: str, data: bytes) -> None: ...

    def remove(self, key: str) -> None: ...

    def mkdir(self, key: str) -> None: ...

    def limits(self) -> dict[str, Any] | None: ...


class BackendSide:
    def __init__(self, backend: VaultBackend, label: str) -> None:
        self.backend = backend
        self.label = label

    def listing(self) -> Listing:
        found = Listing()
        for entry in self.backend.list_all():
            if entry.is_dir:
                found.folders.add(entry.key)
            else:
                found.files[entry.key] = entry.size
        return found

    def read(self, key: str) -> bytes:
        try:
            return self.backend.get_bytes(key)[0]
        except NotFound as exc:
            raise MoveError(f"{key} is gone from {self.label}") from exc

    def create(self, key: str, data: bytes) -> None:
        try:
            self.backend.put_checked(key, data, MUST_NOT_EXIST, content_type_of(key))
        except VersionConflict as exc:
            raise MoveError(f"{key} appeared in {self.label} during the move") from exc

    def replace(self, key: str, data: bytes) -> None:
        self.backend.put_bytes(key, data, content_type_of(key))

    def remove(self, key: str) -> None:
        self.backend.delete(key)

    def mkdir(self, key: str) -> None:
        self.backend.mkdir(key)

    def limits(self) -> dict[str, Any] | None:
        return None


class DavSide:
    def __init__(self, url: str, key: str, label: str, http: httpx2.Client | None = None) -> None:
        self.url = url
        self.label = label
        self.base = f"{url}/dav/vault/"
        self.path = urlsplit(self.base).path
        self.http = http or httpx2.Client(timeout=HTTP_TIMEOUT, headers={"Authorization": f"Bearer {key}"})

    def send(self, method: str, target: str, **options: Any) -> httpx2.Response:
        try:
            response = self.http.request(method, target, **options)
        except httpx2.HTTPError as exc:
            raise MoveError(f"cannot reach {self.url}: {exc}") from exc
        if response.status_code == 401:
            raise MoveError(f"{self.url} refused this machine's key; sign in again with `mindin login {self.url}`")
        return response

    def at(self, key: str) -> str:
        return self.base + quote(key)

    def failed(self, response: httpx2.Response, key: str) -> MoveError:
        reason = response.text.strip() or f"answered {response.status_code}"
        return MoveError(reason if key in reason else f"{key}: {reason}")

    def listing(self) -> Listing:
        found = Listing()
        pending = [""]
        while pending:
            folder = pending.pop()
            response = self.send("PROPFIND", self.at(folder), headers={"Depth": "1"})
            if response.status_code != 207:
                raise self.failed(response, folder or "the vault")
            for key, is_dir, size in self.entries(response.content):
                if key == folder:
                    continue
                if is_dir:
                    found.folders.add(key)
                    pending.append(key)
                else:
                    found.files[key] = size
        return found

    def entries(self, body: bytes) -> list[tuple[str, bool, int]]:
        listed = []
        for response in ElementTree.fromstring(body).iter(f"{DAV}response"):
            href = unquote(urlsplit(response.findtext(f"{DAV}href") or "").path)
            prop = response.find(f"{DAV}propstat/{DAV}prop")
            if prop is None or not href.startswith(self.path):
                continue
            is_dir = prop.find(f"{DAV}resourcetype/{DAV}collection") is not None
            listed.append((href[len(self.path) :], is_dir, int(prop.findtext(f"{DAV}getcontentlength") or 0)))
        return listed

    def read(self, key: str) -> bytes:
        response = self.send("GET", self.at(key))
        if response.status_code != 200:
            raise self.failed(response, key)
        return response.content

    def put(self, key: str, data: bytes, headers: dict[str, str]) -> None:
        response = self.send(
            "PUT", self.at(key), content=data, headers={"Content-Type": content_type_of(key)} | headers
        )
        if response.status_code == 412:
            raise MoveError(f"{key} appeared in {self.label} during the move")
        if response.status_code not in (200, 201, 204):
            raise self.failed(response, key)

    def create(self, key: str, data: bytes) -> None:
        self.put(key, data, {"If-None-Match": "*"})

    def replace(self, key: str, data: bytes) -> None:
        self.put(key, data, {})

    def remove(self, key: str) -> None:
        response = self.send("DELETE", self.at(key))
        if response.status_code not in (200, 204, 404):
            raise self.failed(response, key)

    def mkdir(self, key: str) -> None:
        response = self.send("MKCOL", self.at(key))
        if response.status_code not in (201, 405):
            raise self.failed(response, key)

    def limits(self) -> dict[str, Any] | None:
        response = self.send("GET", f"{self.url}/api/vault/limits")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise self.failed(response, "the vault's limits")
        return response.json()


def describe(place: Place) -> str:
    vault = place.vault
    if vault["kind"] == "server" and vault.get("vault"):
        return f"the vault {vault['vault']} on {vault['url']}"
    if vault["kind"] == "s3":
        prefix = f"/{vault['prefix']}" if vault.get("prefix") else ""
        return f"the bucket {vault['bucket']}{prefix} at {vault['endpoint']}"
    return places.CHOICES_BY_KIND[vault["kind"]].describe(vault)


def identity(place: Place) -> tuple[str, ...]:
    vault = place.vault
    fields = {"folder": ("path",), "s3": ("endpoint", "bucket", "prefix"), "server": ("url", "vault")}
    return (vault["kind"], *(str(vault.get(name) or "") for name in fields[vault["kind"]]))


def server_entry(url: str, slug: str | None) -> tuple[str | None, dict | None]:
    if slug:
        return slug, credentials.server_state(url)["vaults"].get(slug)
    return credentials.chosen(url)


def with_slug(place: Place) -> Place:
    if place.vault["kind"] != "server":
        return place
    slug, _ = server_entry(place.vault["url"], place.vault.get("vault") or None)
    return Place({**place.vault, "vault": slug or ""}, place.keys)


def current_place() -> Place:
    url = credentials.configured_server()
    if url is not None:
        return with_slug(places.server_place(url))
    root = config.local_root()
    if root is not None:
        return places.folder_place(str(root))
    settings = config.s3_settings()
    if settings is not None:
        keys = {"access_key": settings["access_key"], "secret_key": settings["secret_key"]}
        return places.s3_place(settings["endpoint"], settings["bucket"], settings["prefix"], settings["region"], **keys)
    raise ConfigError("mindin: no vault is set up on this machine, so there is nothing to move; see `mindin setup`")


def sign_in(url: str, slug: str | None, no_browser: bool) -> None:
    print(f"Signing this machine in to {url} for the move.")
    if login.sign_in(url, None, no_browser, slug) != 0:
        raise MoveError(f"not signed in to {url}; nothing moved")


def open_side(place: Place, writing: bool = False, no_browser: bool = False) -> Side:
    vault = place.vault
    label = describe(place)
    if vault["kind"] == "folder":
        from mindin.backends.local import LocalBackend

        root = Path(vault["path"])
        if not writing and not root.is_dir():
            raise MoveError(f"no folder at {root}")
        return BackendSide(LocalBackend(root), label)
    if vault["kind"] == "s3":
        from mindin.backends.s3 import S3Backend

        keys = place.keys or credentials.s3_keys(vault["endpoint"], vault["bucket"])
        if not keys.get("access_key") or not keys.get("secret_key"):
            raise MoveError(f"no keys for {label}; set S3_ACCESS_KEY and S3_SECRET_KEY")
        backend = S3Backend(
            vault["endpoint"],
            keys["access_key"],
            keys["secret_key"],
            vault["bucket"],
            prefix=vault.get("prefix") or "",
            region=vault.get("region") or places.DEFAULT_REGION,
        )
        return BackendSide(backend, label)
    url, slug = vault["url"], vault.get("vault") or None
    _, entry = server_entry(url, slug)
    if entry is None and writing:
        sign_in(url, slug, no_browser)
        _, entry = server_entry(url, slug)
    if entry is None:
        raise MoveError(f"not signed in to {label}; run `mindin login {url}`" + (f" --vault {slug}" if slug else ""))
    return DavSide(url, entry["key"], label)


@dataclass
class Options:
    dry_run: bool = False
    merge: bool = False
    include_skipped: bool = False
    skip_refused: bool = False
    no_browser: bool = False


@dataclass
class Plan:
    carried: dict[str, int] = field(default_factory=dict)
    folders: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)
    write: list[str] = field(default_factory=list)
    replace: set[str] = field(default_factory=set)
    same: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    differing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    full: str | None = None
    digests: dict[str, str] = field(default_factory=dict)
    limits_unknown: bool = False

    @property
    def size(self) -> int:
        return sum(self.carried.values())

    @property
    def brought(self) -> list[str]:
        return [key for key in sorted(self.carried) if key not in self.refused]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def carried_files(listing: Listing, include_skipped: bool) -> tuple[dict[str, int], list[str]]:
    carried: dict[str, int] = {}
    skipped: list[str] = []
    for key, size in sorted(listing.files.items()):
        if not include_skipped and skipped_as(key) is not None:
            skipped.append(key)
        else:
            carried[key] = size
    return carried, skipped


def file_count(place: Place) -> int:
    try:
        return len(carried_files(open_side(place).listing(), False)[0])
    except (MoveError, VaultError, OSError):
        return 0


def refusals(plan: Plan, limits: dict[str, Any] | None, keys: list[str]) -> dict[str, str]:
    if limits is None:
        return {}
    storage = limits_of(limits)
    upload = limits.get("upload_bytes")
    found: dict[str, str] = {}
    for key in keys:
        size = plan.carried[key]
        refused = refusal(storage, key, size)
        if refused is not None:
            found[key] = str(refused)
        elif upload is not None and size > upload:
            found[key] = f"{key} is refused: it is {size_text(size)}, and one upload takes at most {size_text(upload)}"
    return found


def compare(plan: Plan, source: Side, target: Side, key: str, size: int) -> bool:
    if plan.carried[key] != size:
        return False
    theirs = digest(target.read(key))
    plan.digests.setdefault(key, digest(source.read(key)))
    return theirs == plan.digests[key]


def make_plan(source: Side, target: Side, options: Options) -> Plan:
    listing = source.listing()
    plan = Plan()
    plan.carried, plan.skipped = carried_files(listing, options.include_skipped)
    plan.folders = sorted(
        folder
        for folder in listing.folders
        if (options.include_skipped or skipped_as(folder) is None)
        and not any(key.startswith(folder) for key in plan.carried)
    )
    there = target.listing().files
    own_bases = any(key.endswith(".base") for key in plan.carried)
    for key, size in sorted(there.items()):
        if key in plan.carried:
            if compare(plan, source, target, key, size):
                plan.same.append(key)
            elif key in PROVISIONED:
                plan.replace.add(key)
            else:
                plan.differing.append(key)
        elif key in PROVISIONED:
            (plan.remove if key.endswith(".base") and own_bases else plan.kept).append(key)
        else:
            plan.extra.append(key)
    if (plan.differing or plan.extra) and plan.replace:
        plan.differing = sorted([*plan.differing, *plan.replace])
        plan.replace = set()
    if plan.differing or plan.extra:
        plan.kept = sorted([*plan.kept, *plan.remove])
        plan.remove = []
    unsent = [key for key in plan.carried if key not in plan.same]
    limits = target.limits()
    plan.limits_unknown = limits is None and isinstance(target, DavSide)
    plan.refused = refusals(plan, limits, unsent)
    plan.write = [key for key in unsent if key not in plan.refused]
    account = (limits or {}).get("account_bytes")
    if account is not None:
        growth = sum(plan.carried[key] for key in plan.write)
        growth -= sum(there[key] for key in [*plan.replace, *plan.remove])
        if limits["used_bytes"] + growth > account:
            plan.full = str(full(account, limits["used_bytes"], growth))
    return plan


def print_plan(plan: Plan, source: Side, target: Side) -> None:
    print(f"Moving {source.label} to {target.label}.")
    print(f"  {count_text(len(plan.carried))} files, {size_text(plan.size)}")
    if plan.skipped:
        print("  Left behind (--include-skipped brings them):")
        for line in skipped_lines(plan.skipped):
            print(f"    {line}")
    if plan.same:
        print(f"  Already there with the same content, skipped: {count_text(len(plan.same))} files")
    if plan.replace:
        print(f"  Replaces the new vault's {', '.join(sorted(plan.replace))} with the source's own")
    if plan.remove:
        print(f"  Removes the new vault's {', '.join(plan.remove)}: the source brings its own views")
    if plan.kept:
        print(f"  Keeps the target's {', '.join(plan.kept)}")
    if plan.limits_unknown:
        print("  The server does not tell its limits; a file it refuses stops the copy, and a rerun goes on.")
    if plan.refused:
        print(f"  Refused by {target.label}:")
        for reason in plan.refused.values():
            print(f"    {reason}")


def blocked(plan: Plan, target: Side, options: Options) -> str | None:
    if plan.extra and not options.merge:
        listed = "\n".join(f"    {key}" for key in [*plan.extra, *plan.differing])
        return f"{target.label} already holds files of its own:\n{listed}\n  `--merge` adds the move to them."
    if plan.differing:
        listed = "\n".join(f"    {key}" for key in plan.differing)
        return f"these files differ between the source and {target.label}; nothing was copied:\n{listed}"
    if plan.full:
        return f"{target.label} has no room: {plan.full}"
    if plan.refused and not options.skip_refused:
        return "the files above are refused; `--skip-refused` moves the rest and leaves them behind"
    return None


class Progress:
    def __init__(self, files: int, size: int) -> None:
        self.files, self.size = files, size
        self.done, self.bytes = 0, 0
        self.live = sys.stdout.isatty()

    def step(self, size: int) -> None:
        self.done += 1
        self.bytes += size
        line = f"  copied {count_text(self.done)} of {count_text(self.files)} files, {size_text(self.bytes)}"
        if self.live:
            print(f"\r{line} of {size_text(self.size)}", end="", flush=True)
        elif self.done % PROGRESS_EVERY == 0 or self.done == self.files:
            print(line)

    def end(self) -> None:
        if self.live and self.done:
            print()


def copy(plan: Plan, source: Side, target: Side) -> None:
    progress = Progress(len(plan.write), sum(plan.carried[key] for key in plan.write))
    try:
        for key in plan.write:
            data = source.read(key)
            plan.digests[key] = digest(data)
            (target.replace if key in plan.replace else target.create)(key, data)
            progress.step(len(data))
    finally:
        progress.end()
    for key in plan.remove:
        target.remove(key)
    for folder in plan.folders:
        target.mkdir(folder)


def verify(plan: Plan, target: Side) -> list[str]:
    there = target.listing().files
    wrong = []
    for key in plan.brought:
        if key not in there:
            wrong.append(f"{key} is missing")
        elif digest(target.read(key)) != plan.digests.get(key):
            wrong.append(f"{key} reads back different from the source")
    return wrong


@dataclass
class Stale:
    where: str
    names: dict[str, str]
    change: str
    fix: Callable[[], None] | None = None


def new_settings(place: Place) -> dict[str, str]:
    return {name: value for name, value in places.CHOICES_BY_KIND[place.vault["kind"]].settings(place.vault).items()}


def naming_other(env: object, wanted: dict[str, str]) -> dict[str, str]:
    if not isinstance(env, dict):
        return {}
    return {
        name: str(value)
        for name, value in env.items()
        if name in STORAGE_NAMES and str(value or "").strip() and str(value) != wanted.get(name)
    }


def plugin_stale(wanted: dict[str, str]) -> list[Stale]:
    path = config.claude_settings_path()
    try:
        data = config.read_json(path.read_text(encoding="utf-8"))
    except OSError:
        return []
    found: list[Stale] = []
    configs = data.get("pluginConfigs") or {}
    for plugin, entry in configs.items() if isinstance(configs, dict) else []:
        options = entry.get("options") if isinstance(entry, dict) else None
        if plugin.split("@", 1)[0] != config.PLUGIN_NAME or not isinstance(options, dict):
            continue
        named = {option: str(value) for option, value in options.items() if option in PLUGIN_OPTIONS}
        names = naming_other({PLUGIN_OPTIONS[option]: value for option, value in named.items()}, wanted)
        if not names:
            continue
        stale_options = [option for option in named if PLUGIN_OPTIONS[option] in names]
        removing = ", ".join(f'"{option}"' for option in stale_options)
        change = f'in {path}, under pluginConfigs."{plugin}".options, remove {removing}; then restart Claude Code'

        def fix(plugin: str = plugin, stale_options: list[str] = stale_options) -> None:
            current = config.read_json(path.read_text(encoding="utf-8"))
            for option in stale_options:
                current["pluginConfigs"][plugin]["options"].pop(option, None)
            path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")

        found.append(Stale(f"Claude Code's {plugin} plugin", names, change, fix))
    return found


def mcp_json_stale(label: str, path: Path, wanted: dict[str, str]) -> list[Stale]:
    try:
        data = config.read_json(path.read_text(encoding="utf-8"))
    except OSError:
        return []
    tables = [("mcpServers", data.get("mcpServers"))]
    projects = data.get("projects")
    tables += [
        (f'projects."{name}".mcpServers', project.get("mcpServers"))
        for name, project in (projects.items() if isinstance(projects, dict) else [])
        if isinstance(project, dict)
    ]
    found = []
    for where, servers in tables:
        for name, server in servers.items() if isinstance(servers, dict) else []:
            names = naming_other(server.get("env") if isinstance(server, dict) else None, wanted)
            if names:
                removing = ", ".join(f'"{variable}"' for variable in names)
                found.append(Stale(label, names, f'in {path}, under {where}."{name}".env, remove {removing}'))
    return found


def codex_stale(wanted: dict[str, str]) -> list[Stale]:
    path = Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "config.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    found = []
    servers = data.get("mcp_servers")
    for name, server in servers.items() if isinstance(servers, dict) else []:
        names = naming_other(server.get("env") if isinstance(server, dict) else None, wanted)
        if names:
            removing = ", ".join(names)
            found.append(Stale("Codex", names, f"in {path}, under [mcp_servers.{name}.env], remove {removing}"))
    return found


def shell_stale(wanted: dict[str, str]) -> list[Stale]:
    filled = places.filled_names()
    names = naming_other({name: os.environ.get(name, "") for name in STORAGE_NAMES if name not in filled}, wanted)
    if not names:
        return []
    return [Stale("This shell", names, f"unset {' '.join(names)} and remove it from your shell profile")]


def stale_settings(target: Place) -> list[Stale]:
    wanted = new_settings(target)
    home = Path("~").expanduser()
    return [
        *plugin_stale(wanted),
        *mcp_json_stale("Claude Code", home / ".claude.json", wanted),
        *codex_stale(wanted),
        *mcp_json_stale("Cursor", home / ".cursor" / "mcp.json", wanted),
        *shell_stale(wanted),
    ]


def report_stale(target: Place, ask: Ask | None) -> None:
    found = stale_settings(target)
    if not found:
        return
    print("\nStill naming the old vault, and deciding over mindin's own config while they do:")
    for stale in found:
        named = ", ".join(f"{name}={value}" for name, value in stale.names.items())
        print(f"  {stale.where} ({named}): {stale.change}.")
        if stale.fix is None or ask is None:
            continue
        if ask(f"  Remove it from {stale.where}'s settings now? [y/N]", False).strip().lower() in places.YES:
            stale.fix()
            print(f"  Removed; restart Claude Code so {stale.where} hands mindin no vault of its own.")


def reconnect(source: Place, target: Place, slug: str | None) -> list[str]:
    vault = target.vault
    lines = []
    if vault["kind"] == "server":
        url = vault["url"]
        connector = f"{url}/mcp" + (f"?vault={slug}" if slug else "")
        lines.append(
            f"claude.ai, Claude Desktop and the phone: add the connector {connector}"
            f" ({DOCS}/how-to/connect-claude-ai.md)"
        )
        folder = f" {source.vault['path']}" if source.vault["kind"] == "folder" else ""
        lines.append(
            f"Obsidian: keep the folder{folder} and sync it with Remotely Save over {url}/dav/ with a WebDAV key"
            f" from {url}/account ({DOCS}/how-to/obsidian.md)"
        )
        lines.append(
            "Search: the server indexes the notes as they arrive; search by meaning catches up as embedding runs"
        )
    elif vault["kind"] == "folder":
        lines.append(f"Obsidian: open {vault['path']} as a vault")
        lines.append("Search: the index is built again on the next start of the MCP server or `mindin sync`")
    else:
        lines.append("Obsidian: sync with Remotely Save's S3 setting against the bucket")
        lines.append("Search: the index is built again on the next start of the MCP server or `mindin sync`")
    if source.vault["kind"] == "server":
        lines.append(f"claude.ai connectors to {source.vault['url']}/mcp still reach the old vault; remove them there")
    lines.append(
        "Claude Code, Codex and Cursor reach the new vault once restarted; `mindin status` shows which decides"
    )
    return lines


def switch(source: Place, target: Place, ask: Ask | None, shown_slug: str | None) -> None:
    slug = target.vault.get("vault") or None
    saved = Place({name: value for name, value in target.vault.items() if name != "vault"}, target.keys)
    places.save(saved)
    if saved.vault["kind"] == "server" and slug:
        credentials.use(saved.vault["url"], slug)
    print(f"\nThis machine's vault is now {describe(target)}, saved in {config.config_path()}.")
    print(
        f"{describe(source)[0].upper()}{describe(source)[1:]} is left as it was; remove it once you no longer need it."
    )
    if source.vault["kind"] == "server":
        print(NOT_MOVED)
    report_stale(saved, ask)
    print("\nReconnect:")
    for line in reconnect(source, target, slug or shown_slug):
        print(f"  {line}")


def check_vault(side: Side, limits: dict[str, Any] | None, writing: bool) -> None:
    if limits is None:
        return
    if limits.get("kind") == "shared":
        raise MoveError(
            f"{side.label} is a shared vault; `mindin move` moves personal vaults, and an organisation's vaults"
            " move through its admin"
        )
    if writing and not limits.get("write"):
        raise MoveError(f"this machine's key for {side.label} only reads; sign in again with write access")


def move(source_place: Place, target_place: Place, options: Options, ask: Ask | None = None) -> int:
    source_place = with_slug(source_place)
    if identity(source_place) == identity(with_slug(target_place)):
        print(f"mindin: the vault already is {describe(source_place)}; nothing to move", file=sys.stderr)
        return 1
    try:
        source = open_side(source_place)
        check_vault(source, source.limits(), writing=False)
        target = open_side(target_place, writing=True, no_browser=options.no_browser)
        target_place = with_slug(target_place)
        target.label = describe(target_place)
        limits = target.limits()
        check_vault(target, limits, writing=True)
        plan = make_plan(source, target, options)
    except (MoveError, VaultError, OSError) as exc:
        print(f"mindin: {exc}", file=sys.stderr)
        return 1
    print_plan(plan, source, target)
    stop = blocked(plan, target, options)
    if stop is not None:
        print(f"mindin: {stop}", file=sys.stderr)
        return 1
    if options.dry_run:
        print(f"Dry run: nothing copied. {count_text(len(plan.write))} files would be copied.")
        return 0
    try:
        copy(plan, source, target)
    except (MoveError, VaultError, OSError, KeyboardInterrupt) as exc:
        reason = "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
        print(
            f"mindin: the move stopped: {reason}. The source is unchanged and this machine still uses it;"
            " run the same command again to go on, the files already copied are skipped.",
            file=sys.stderr,
        )
        return 1
    wrong = verify(plan, target)
    if wrong:
        listed = "\n".join(f"  {line}" for line in wrong)
        print(f"mindin: the copy does not match the source; this machine still uses it:\n{listed}", file=sys.stderr)
        return 1
    print(f"Verified {count_text(len(plan.brought))} files in {target.label}: each reads back with the same sha256.")
    if plan.refused:
        print("Left behind, as the target refuses them:")
        for reason in plan.refused.values():
            print(f"  {reason}")
    switch(source_place, target_place, ask, (limits or {}).get("vault"))
    return 0


def target_place(args: argparse.Namespace, ask: Ask | None) -> Place:
    if args.folder:
        return places.folder_place(args.folder)
    if args.s3:
        return places.s3_given(args.s3, args, ask)
    if args.server:
        place = places.server_place(args.server)
        return Place({**place.vault, "vault": args.vault or ""})
    raise ConfigError("mindin: move needs a target: --to --folder PATH, --to --s3 BUCKET or --to --server URL")


def command_move(args: argparse.Namespace, ask: Ask | None = None) -> int:
    interactive = ask or (places.terminal_ask if sys.stdin.isatty() else None)
    options = Options(
        dry_run=args.dry_run,
        merge=args.merge,
        include_skipped=args.include_skipped,
        skip_refused=args.skip_refused,
        no_browser=args.no_browser,
    )
    return move(current_place(), target_place(args, interactive), options, interactive)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--to", action="store_true", help="reads as `mindin move --to --server URL`; optional")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--folder", metavar="PATH", help="move the vault into this folder")
    where.add_argument("--s3", metavar="BUCKET", help="move it into this bucket; with --endpoint, keys from S3_*")
    where.add_argument("--server", metavar="URL", help="move it onto this server; signs the machine in when needed")
    parser.add_argument("--endpoint", help="with --s3: the S3 or MinIO address")
    parser.add_argument("--prefix", help="with --s3: the vault's prefix inside the bucket")
    parser.add_argument("--region", help=f"with --s3: the region (default {places.DEFAULT_REGION})")
    parser.add_argument("--vault", help="with --server: the vault's slug; this machine's default when omitted")
    parser.add_argument("--no-browser", action="store_true", help="with --server: only print the sign-in link")
    parser.add_argument("--dry-run", action="store_true", help="print what would move, be left behind and refused")
    parser.add_argument("--merge", action="store_true", help="add to a target that already holds files of its own")
    parser.add_argument(
        "--include-skipped",
        action="store_true",
        help="also bring .git/, .DS_Store, ._*, __MACOSX/, .trash/, Thumbs.db and desktop.ini",
    )
    parser.add_argument(
        "--skip-refused", action="store_true", help="move what the target takes and leave the refused files behind"
    )
