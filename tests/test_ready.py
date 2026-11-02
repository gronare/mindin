from __future__ import annotations

from pathlib import Path

import pytest
from hosted_support import hosted

from notes_vault_mcp.auth.hosted_ready import Cached, account_store_check
from notes_vault_mcp.extensions import Extension, Server
from notes_vault_mcp.vault import SubjectVaults


class Checks(Extension):
    def __init__(self) -> None:
        self.database_up = True

    def attach(self, server: Server) -> None:
        def database() -> bool:
            return self.database_up

        server.health_checks.append(database)


@pytest.mark.anyio
async def test_ready_answers_200_without_checks_and_up_is_unchanged(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as site, site.browser() as http:
        ready = await http.get("/ready")
        up = await http.get("/up")
    assert ready.status_code == 200
    assert ready.json() == {"ready": True, "failing": []}
    assert ready.headers["cache-control"] == "no-store"
    assert (up.status_code, up.text) == (200, "ok")


@pytest.mark.anyio
async def test_ready_answers_503_naming_the_failing_check(vaults: SubjectVaults, tmp_path: Path):
    checks = Checks()
    async with hosted(vaults, tmp_path, extensions=[checks]) as site, site.browser() as http:
        assert (await http.get("/ready")).status_code == 200
        checks.database_up = False
        down = await http.get("/ready")
        up = await http.get("/up")
    assert down.status_code == 503
    assert down.json() == {"ready": False, "failing": ["database"]}
    assert up.status_code == 200


@pytest.mark.anyio
async def test_a_raising_check_counts_as_failing(vaults: SubjectVaults, tmp_path: Path):
    class Raising(Extension):
        def attach(self, server: Server) -> None:
            def bus() -> bool:
                raise ConnectionError("the bus is unreachable")

            server.health_checks.append(bus)

    async with hosted(vaults, tmp_path, extensions=[Raising()]) as site, site.browser() as http:
        down = await http.get("/ready")
    assert down.status_code == 503
    assert down.json()["failing"] == ["bus"]


@pytest.mark.anyio
async def test_ready_sits_under_the_public_url_path(vaults: SubjectVaults, tmp_path: Path):
    url = "http://localhost/vault"
    async with hosted(vaults, tmp_path, public_url=url, issuer=url) as site, site.browser() as http:
        ready = await http.get("/vault/ready")
    assert ready.status_code == 200


@pytest.mark.anyio
async def test_ready_fails_on_its_own_when_storage_cannot_be_reached(
    vaults: SubjectVaults, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    blocker = tmp_path / "frame"
    blocker.write_text("not a folder")
    monkeypatch.setenv("VAULT_PATH", str(blocker / "store"))
    async with hosted(vaults, tmp_path) as site, site.browser() as http:
        down = await http.get("/ready")
    assert down.status_code == 503
    assert down.json()["failing"] == ["storage"]


def test_the_account_store_check_fails_when_the_store_does_not_answer():
    class Unreachable:
        def connect(self) -> None:
            raise ConnectionError("the account store is down")

    assert not Cached(account_store_check(Unreachable()))()  # type: ignore[arg-type]


def test_a_built_in_check_is_asked_once_per_window():
    now = [0.0]
    asked: list[float] = []

    def orchard() -> bool:
        asked.append(now[0])
        return True

    probe = Cached(orchard, seconds=5.0, clock=lambda: now[0])
    assert probe() and probe()
    now[0] = 4.9
    assert probe()
    now[0] = 5.0
    assert probe()
    assert asked == [0.0, 5.0]
    assert probe.__name__ == "orchard"
