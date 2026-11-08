from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest
from forwarded_support import Proxy

from mindin.auth.hosted_keys import Keyring, MasterKey, SealedVaults, VaultKeys
from mindin.auth.hosted_store import HostedStore
from mindin.backends import s3
from mindin.vault import SubjectVaults, Vault, open_vault

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"
TOKEN_RE = re.compile(r"@@(TODAY|D(\d+))@@")
STORAGE_VARS = (
    "VAULT_PATH",
    "S3_ENDPOINT",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_BUCKET",
    "S3_PREFIX",
    "S3_REGION",
    "VAULT_SERVER_URL",
)


def _substitute(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        days = int(match.group(2)) if match.group(2) else 0
        return (date.today() - timedelta(days=days)).isoformat()

    return TOKEN_RE.sub(replace, text)


@pytest.fixture(autouse=True)
def away_from_the_users_own_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path_factory.mktemp("home")
    (home / ".claude").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(home / ".cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("VAULT_SIGNALS", "off")
    for name in ("VAULT_SERVER_URL", "CLAUDE_PLUGIN_OPTION_VAULT_SIGNALS", "CLAUDE_PLUGIN_ROOT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_remembered_delete_probes() -> None:
    s3._delete_probes.clear()


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    destination = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, destination)
    for path in destination.rglob("*.md"):
        path.write_text(_substitute(path.read_text(encoding="utf-8")), encoding="utf-8")
    return destination


@pytest.fixture
def vault(vault_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("VAULT_SCHEMA", raising=False)
    opened = open_vault()
    opened.index.sync(force=True)
    yield opened
    opened.close()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def proxy() -> Proxy:
    return Proxy()


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "store"
    monkeypatch.setenv("VAULT_PATH", str(root))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    for name in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET", "VAULT_SCHEMA"):
        monkeypatch.delenv(name, raising=False)
    return root


@pytest.fixture
def vaults(storage: Path, tmp_path: Path) -> SubjectVaults:
    opened = SubjectVaults(cache=tmp_path / "cache", prefix="users", dav_url="http://localhost/dav/")
    yield opened
    opened.close()


@pytest.fixture
def sealed(storage: Path, tmp_path: Path) -> SealedVaults:
    store = HostedStore(tmp_path / "auth", extensions=())
    return SealedVaults(store, VaultKeys(store, Keyring(MasterKey.generate())))


@pytest.fixture
def vault_id(sealed: SealedVaults) -> str:
    account, _ = sealed.store.invite("ann@example.com")
    vault = sealed.store.first_vault(account.id)
    assert vault is not None
    return vault.id


@pytest.fixture
def forget_storage(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    def forget() -> None:
        for name in STORAGE_VARS:
            monkeypatch.setenv(name, "")
            monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_" + name, "")

    return forget


@pytest.fixture
def nothing_configured(forget_storage: Callable[[], None]) -> Path:
    forget_storage()
    return Path(os.environ["CLAUDE_CONFIG_DIR"])
