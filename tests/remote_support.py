from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from cimd_support import MetadataHost
from hosted_support import Clock, FakeIdentityProvider, hosted_config

from notes_vault_mcp.auth import MACHINE_KEY_PURPOSE
from notes_vault_mcp.auth.hosted import build_hosted_app
from notes_vault_mcp.auth.hosted_keys import Keyring, MasterKey
from notes_vault_mcp.auth.hosted_store import HostedStore
from notes_vault_mcp.backends import VaultBackend
from notes_vault_mcp.vault import SubjectVaults

EMAIL = "ann@example.com"
SUB = "sub-ann"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def files_in(folder: Path) -> dict[str, str]:
    return {
        path.relative_to(folder).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(folder.rglob("*"))
        if path.is_file() and ".obsidian" not in path.parts
    }


@dataclass
class Served:
    url: str
    store: HostedStore
    vaults: SubjectVaults
    clock: Clock

    def account_id(self, email: str = EMAIL) -> str:
        account = self.store.account(email)
        assert account is not None
        return account.id

    def key(self, write: bool = True, email: str = EMAIL, purpose: str = MACHINE_KEY_PURPOSE) -> str:
        scopes = ["vault:read", "vault:write"] if write else ["vault:read"]
        return self.store.create_key(self.account_id(email), "laptop", scopes, purpose=purpose)

    def backend(self, sub: str = SUB) -> VaultBackend:
        return self.vaults.backend_factory(sub)[0]

    def seed(self, source: Path, sub: str = SUB) -> None:
        backend = self.backend(sub)
        for key, text in files_in(source).items():
            backend.put(key, text)

    def read(self, key: str, sub: str = SUB) -> str:
        return self.backend(sub).get(key)[0]

    def write(self, key: str, text: str, sub: str = SUB) -> None:
        self.backend(sub).put(key, text)


@contextmanager
def serve(vaults: SubjectVaults, tmp_path: Path, email: str = EMAIL, sub: str = SUB) -> Iterator[Served]:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    config = hosted_config(tmp_path, public_url=url, issuer=url)
    clock = Clock()
    metadata = MetadataHost()
    app = build_hosted_app(
        vaults,
        config,
        oidc_transport=FakeIdentityProvider().transport(),
        metadata_transport=metadata.transport(),
        resolver=metadata.resolve,
        clock=clock,
        keyring=Keyring(MasterKey.generate()),
    )
    store = HostedStore(config.auth_dir)
    account, _ = store.invite(email)
    with store.connect() as connection:
        connection.execute("UPDATE accounts SET status = 'active', sub = ? WHERE id = ?", (sub, account.id))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "the test server did not start"
        time.sleep(0.02)
    try:
        yield Served(url=url, store=store, vaults=vaults, clock=clock)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
