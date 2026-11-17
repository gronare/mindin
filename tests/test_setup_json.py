from __future__ import annotations

import io
import json
import tomllib

import httpx2
import pytest

from mindin import credentials, login, setup_json
from mindin.cli import build_parser, main
from mindin.config import config_dir

pytestmark = pytest.mark.usefixtures("nothing_configured")

URL = "https://vault.example.com"
DEVICE = {
    "device_code": "dc-1",
    "user_code": "WVVD-XQNR",
    "verification_uri": f"{URL}/device",
    "verification_uri_complete": f"{URL}/device?code=WVVD-XQNR",
    "interval": 1,
    "expires_in": 600,
}
GRANTED = {"access_token": "nvm_new", "scope": "vault:read vault:write", "vault": "work", "vault_name": "Work"}


def server(*answers: tuple[int, dict]) -> httpx2.MockTransport:
    polls = iter(answers)

    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/device_authorization":
            return httpx2.Response(200, json=DEVICE)
        if request.url.path == "/token":
            status, body = next(polls)
            return httpx2.Response(status, json=body)
        return httpx2.Response(404)

    return httpx2.MockTransport(handle)


@pytest.fixture
def signing(monkeypatch: pytest.MonkeyPatch):
    def answering(transport: httpx2.BaseTransport) -> None:
        real = login.login
        client = httpx2.Client(transport=transport)
        monkeypatch.setattr(
            login, "login", lambda url, **options: real(url, http=client, sleep=lambda seconds: None, **options)
        )

    monkeypatch.setattr(setup_json, "about", lambda url, slug: {"layout": "default", "notes": 12})
    return answering


def events(*argv: str, answers: str = "") -> tuple[int, list[dict]]:
    out = io.StringIO()
    code = setup_json.command(build_parser().parse_args(["setup", *argv, "--json"]), out, io.StringIO(answers))
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


def saved() -> dict:
    path = config_dir() / "config.toml"
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_json_setup_shows_the_code_then_the_vault_it_signed_in_to(signing, capsys: pytest.CaptureFixture):
    signing(server((400, {"error": "authorization_pending"}), (200, GRANTED)))
    code, said = events("--server", URL, "--signals", "no")
    assert code == 0
    assert [event["event"] for event in said] == ["code", "signed_in", "done"]
    assert said[0] == {
        "event": "code",
        "verification_uri": f"{URL}/device",
        "verification_uri_complete": f"{URL}/device?code=WVVD-XQNR",
        "user_code": "WVVD-XQNR",
        "expires_in": 600,
    }
    assert said[1] == {
        "event": "signed_in",
        "server": URL,
        "vault": "work",
        "vault_name": "Work",
        "access": "read and write",
        "already": False,
        "layout": "default",
        "notes": 12,
    }
    assert saved() == {"vault": {"kind": "server", "url": URL}, "signals": {"install": False}}
    assert credentials.stored(URL)["key"] == "nvm_new"
    assert "Anonymous usage data: off" in capsys.readouterr().err, "mindin's own lines go to stderr, never stdout"


@pytest.mark.parametrize(
    ("answer", "kind"),
    [((400, {"error": "access_denied"}), "denied"), ((400, {"error": "expired_token"}), "expired")],
)
def test_json_setup_names_a_denied_or_expired_sign_in_and_saves_nothing(signing, answer, kind):
    signing(server(answer))
    code, said = events("--server", URL)
    assert code == 1
    assert [event["event"] for event in said] == ["code", "error"]
    assert said[1]["kind"] == kind and said[1]["message"]
    assert saved() == {}


def test_json_setup_names_an_unreachable_server_with_its_errno(signing):
    def refuse(request: httpx2.Request) -> httpx2.Response:
        try:
            raise OSError(65, "No route to host")
        except OSError as exc:
            raise httpx2.ConnectError("[Errno 65] No route to host") from exc

    signing(httpx2.MockTransport(refuse))
    code, said = events("--server", URL)
    assert code == 1
    assert said == [{"event": "error", "kind": "unreachable", "message": "[Errno 65] No route to host", "errno": 65}]


def test_json_setup_on_a_machine_already_signed_in_shows_the_vault_without_a_code(signing):
    credentials.save(URL, {"key": "nvm_kept", "scope": "vault:read", "vault": "work", "vault_name": "Work"}, "work")
    code, said = events("--server", URL)
    assert code == 0
    assert [event["event"] for event in said] == ["signed_in", "done"]
    assert said[0]["already"] and said[0]["access"] == "read only" and said[0]["vault_name"] == "Work"


def test_json_setup_needs_a_server(capsys: pytest.CaptureFixture):
    assert main(["setup", "--folder", "/tmp", "--json"]) == 2
    said = json.loads(capsys.readouterr().out)
    assert said == {"event": "error", "kind": "config", "message": "--json goes with --server URL"}
