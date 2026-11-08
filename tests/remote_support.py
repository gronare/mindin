from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from cimd_support import MetadataHost
from hosted_support import Clock, FakeIdentityProvider, hosted_config

from mindin.auth import MACHINE_KEY_PURPOSE
from mindin.auth.hosted import build_hosted_app
from mindin.auth.hosted_keys import Keyring, MasterKey
from mindin.auth.hosted_store import HostedStore
from mindin.backends import VaultBackend
from mindin.vault import SubjectVaults

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

    def vault_id(self, slug: str | None = None, email: str = EMAIL) -> str:
        vaults = self.store.vaults(self.account_id(email))
        found = [vault for vault in vaults if slug is None or vault.slug == slug]
        assert found, f"{email} has no vault {slug}"
        return found[0].id

    def key(
        self, write: bool = True, email: str = EMAIL, purpose: str = MACHINE_KEY_PURPOSE, vault: str | None = None
    ) -> str:
        scopes = ["vault:read", "vault:write"] if write else ["vault:read"]
        vault_id = self.vault_id(vault, email)
        return self.store.create_key(self.account_id(email), "laptop", scopes, purpose=purpose, vault_id=vault_id)

    def backend(self, vault: str | None = None) -> VaultBackend:
        return self.vaults.backend_factory(self.vault_id(vault))[0]

    def seed(self, source: Path, vault: str | None = None) -> None:
        backend = self.backend(vault)
        for key, text in files_in(source).items():
            backend.put(key, text)

    def read(self, key: str, vault: str | None = None) -> str:
        return self.backend(vault).get(key)[0]

    def write(self, key: str, text: str, vault: str | None = None) -> None:
        self.backend(vault).put(key, text)


@contextmanager
def serve(
    vaults: SubjectVaults,
    tmp_path: Path,
    email: str = EMAIL,
    sub: str = SUB,
    extensions: Any = (),
    **overrides: Any,
) -> Iterator[Served]:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    config = hosted_config(tmp_path, public_url=url, issuer=url, **overrides)
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
        extensions=extensions,
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
