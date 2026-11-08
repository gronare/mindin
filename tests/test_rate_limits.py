from __future__ import annotations

import logging
import logging.config
from pathlib import Path

import pytest
import yaml
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from mindin.config import ConfigError
from mindin.hosted.accounts.store import HostedStore
from mindin.hosted.app import uvicorn_options
from mindin.hosted.config import AuthConfig, auth_config
from mindin.hosted.pages.web import Limits, client_address

ORCHARD = "203.0.113.7"
EDGE = "198.51.100.20"
INGRESS = "10.20.0.5"


class Clock:
    def __init__(self, moment: float = 6_000.0) -> None:
        self.moment = moment

    def __call__(self) -> float:
        return self.moment


def store(tmp_path: Path) -> HostedStore:
    return HostedStore(tmp_path / "auth", extensions=())


def test_a_restarted_server_keeps_counting_where_the_last_one_stopped(tmp_path: Path):
    clock = Clock()
    first = Limits(store(tmp_path), clock)
    assert all(first.sign_in.allow(ORCHARD) for _ in range(20))
    restarted = Limits(store(tmp_path), clock)
    assert not restarted.sign_in.allow(ORCHARD)
    assert restarted.sign_in.allow("203.0.113.8")


def test_limits_on_one_store_share_their_counts(tmp_path: Path):
    clock = Clock()
    shared = store(tmp_path)
    one, other = Limits(shared, clock), Limits(shared, clock)
    for _ in range(10):
        assert one.sign_in.allow(ORCHARD)
        assert other.sign_in.allow(ORCHARD)
    assert not one.sign_in.allow(ORCHARD)
    assert not other.sign_in.allow(ORCHARD)


def test_scopes_count_apart(tmp_path: Path):
    limits = Limits(store(tmp_path), Clock())
    assert all(limits.device_lookup.allow(ORCHARD) for _ in range(5))
    assert not limits.device_lookup.allow(ORCHARD)
    assert limits.device_start.allow(ORCHARD)


def test_a_window_opens_again_once_its_hits_have_aged_out(tmp_path: Path):
    clock = Clock()
    limits = Limits(store(tmp_path), clock)
    assert all(limits.sign_in.allow(ORCHARD) for _ in range(20))
    assert not limits.sign_in.allow(ORCHARD)
    clock.moment += 121
    assert limits.sign_in.allow(ORCHARD)


def test_a_blocked_address_is_refused_without_asking_the_store(tmp_path: Path):
    limits = Limits(store(tmp_path), Clock())
    assert all(limits.sign_in.allow(ORCHARD) for _ in range(20))
    assert not limits.sign_in.allow(ORCHARD)

    def unreachable(*_: object) -> bool:
        raise AssertionError("the store was asked about a blocked address")

    limits.sign_in.counter.hit = unreachable  # type: ignore[method-assign, union-attr]
    assert not limits.sign_in.allow(ORCHARD)


def test_the_store_keeps_no_client_address_in_the_clear(tmp_path: Path):
    accounts = store(tmp_path)
    Limits(accounts, Clock()).sign_in.allow(ORCHARD)
    with accounts.connect() as connection:
        rows = [tuple(row) for row in connection.execute("SELECT * FROM rate_limit_hits").fetchall()]
    assert rows and ORCHARD not in repr(rows)


def test_old_slots_are_swept(tmp_path: Path):
    clock = Clock()
    accounts = store(tmp_path)
    limits = Limits(accounts, clock)
    limits.sign_in.allow(ORCHARD)
    clock.moment += 600
    limits.sign_in.allow("203.0.113.8")
    with accounts.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM rate_limit_hits").fetchone()[0]
    assert count == 1


def configured(monkeypatch: pytest.MonkeyPatch, allowed: str) -> AuthConfig:
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://notes.example.com")
    monkeypatch.setenv("VAULT_GITHUB_CLIENT_ID", "workshop")
    monkeypatch.setenv("VAULT_GITHUB_CLIENT_SECRET", "frame")
    monkeypatch.setenv("VAULT_FORWARDED_ALLOW_IPS", allowed)
    return auth_config()


def test_proxy_addresses_take_a_comma_separated_list_of_addresses_and_ranges(monkeypatch: pytest.MonkeyPatch):
    config = configured(monkeypatch, f"{EDGE}, 10.20.0.0/16,2001:db8::/32")
    assert config.forwarded_allow_ips == (EDGE, "10.20.0.0/16", "2001:db8::/32")
    assert uvicorn_options(config)["forwarded_allow_ips"] == [EDGE, "10.20.0.0/16", "2001:db8::/32"]
    assert uvicorn_options(config)["proxy_headers"] is True


def test_a_proxy_address_that_is_no_address_stops_the_server(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ConfigError, match="VAULT_FORWARDED_ALLOW_IPS"):
        configured(monkeypatch, "orchard.example")


def test_without_the_setting_uvicorn_keeps_its_own_default():
    assert "forwarded_allow_ips" not in uvicorn_options(AuthConfig())


async def seen_client(allowed: tuple[str, ...], peer: str, forwarded_for: str) -> str:
    seen: list[str] = []

    async def app(scope, receive, send) -> None:
        seen.append(client_address(Request(scope)))

    trusted = uvicorn_options(AuthConfig(forwarded_allow_ips=allowed))["forwarded_allow_ips"]
    scope = {
        "type": "http",
        "client": (peer, 40000),
        "headers": [(b"x-forwarded-for", forwarded_for.encode())],
        "scheme": "http",
    }
    await ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None)  # type: ignore[arg-type]
    return seen[0]


@pytest.mark.anyio
async def test_two_trusted_hops_hand_the_real_client_to_the_rate_limit():
    assert await seen_client((EDGE, "10.20.0.0/16"), INGRESS, f"{ORCHARD}, {EDGE}") == ORCHARD


@pytest.mark.anyio
async def test_a_hop_left_out_of_the_list_becomes_the_client():
    assert await seen_client(("10.20.0.0/16",), INGRESS, f"{ORCHARD}, {EDGE}") == EDGE


@pytest.mark.anyio
async def test_an_untrusted_peer_cannot_claim_an_address():
    assert await seen_client((EDGE,), ORCHARD, "192.0.2.1") == ORCHARD


def test_access_lines_carry_the_path_but_never_the_query(capsys: pytest.CaptureFixture[str]):
    logging.config.dictConfig(uvicorn_options(AuthConfig())["log_config"])
    access = logging.getLogger("uvicorn.access")
    try:
        access.info(
            '%s - "%s %s HTTP/%s" %d', f"{ORCHARD}:40000", "GET", "/signin/email?token=orchard-ticket", "1.1", 303
        )
        line = capsys.readouterr().out
    finally:
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).handlers.clear()
            logging.getLogger(name).propagate = True
    assert "/signin/email" in line and ORCHARD in line and "303" in line
    assert "orchard-ticket" not in line and "?" not in line


def test_a_lone_star_trusts_every_peer(monkeypatch: pytest.MonkeyPatch):
    assert configured(monkeypatch, "*").forwarded_allow_ips == ("*",)


def test_the_docker_example_trusts_caddy_for_the_client_address():
    compose = yaml.safe_load((Path(__file__).parents[1] / "deploy" / "docker" / "compose.yml").read_text())
    assert compose["services"]["vault"]["environment"]["VAULT_FORWARDED_ALLOW_IPS"]
