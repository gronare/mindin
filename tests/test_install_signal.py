from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

import httpx2 as httpx
import pytest

from mindin import install_signal, setup
from mindin.cli import build_parser, main
from mindin.config import read_config, write_config
from mindin.install_signal import DAY, MAX_WORK, Install, Refused

NOW = 1_792_540_800
NONCE = bytes(range(32))
VECTOR = Install(id="0123456789abcdef0123456789abcdef", version="0.14.3", os="macos", arch="aarch64", runs="plugin")


def challenge(difficulty: int) -> dict:
    encoded = base64.urlsafe_b64encode(NONCE).rstrip(b"=").decode()
    return {"nonce": encoded, "difficulty": difficulty, "expires": NOW + 600, "tag": encoded[::-1]}


class Endpoint:
    def __init__(self, difficulty: int) -> None:
        self.offered = challenge(difficulty)
        self.posted: list[dict] = []
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == "/v1/reports/challenge":
            return httpx.Response(200, json=self.offered)
        if request.method == "POST" and request.url.path == "/v1/signals":
            self.posted.append(json.loads(request.content))
            return httpx.Response(202, json={})
        return httpx.Response(404)

    def send(self, install: Install) -> None:
        install_signal.send(install, base="https://signals.test", transport=httpx.MockTransport(self))


def saved_signals() -> dict | None:
    return read_config().get("signals")


def zero_bits(digest: bytes) -> int:
    return 256 - int.from_bytes(digest, "big").bit_length()


@pytest.fixture(autouse=True)
def signals_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_SIGNALS")


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_the_digest_matches_the_endpoints_vector():
    assert VECTOR.digest().hex() == "af8872d156ee7ced5e8f1b8312cf6585d16fe5bda5474557e92437c790ad6a7a"


def test_the_signal_holds_the_id_version_os_arch_and_how_it_runs_and_nothing_else(state: Path):
    endpoint = Endpoint(difficulty=4)
    endpoint.send(install_signal.payload(state, "cli"))
    [body] = endpoint.posted
    assert list(body) == ["mindin", "challenge", "counter"], "no key and no signature go with it"
    assert list(body["mindin"]) == ["id", "version", "os", "arch", "runs"]
    assert body["mindin"]["id"] == (state / "install-id").read_text().strip()
    assert body["mindin"]["runs"] == "cli" and body["mindin"]["os"] in {"macos", "linux", "windows"}
    assert body["mindin"]["arch"] in {"aarch64", "x86_64"}
    assert body["challenge"] == endpoint.offered, "the challenge goes back as it came"


def test_the_counter_meets_the_challenges_work(state: Path):
    endpoint = Endpoint(difficulty=12)
    install = install_signal.payload(state, "server")
    endpoint.send(install)
    [body] = endpoint.posted
    counter = body["counter"]
    proof = hashlib.sha256(b"kebbie-report-pow-v1" + NONCE + install.digest() + counter.to_bytes(8, "big")).digest()
    assert zero_bits(proof) >= 12
    assert endpoint.paths == ["GET /v1/reports/challenge", "POST /v1/signals"]


def test_more_work_than_mindin_does_is_refused_and_nothing_is_posted(state: Path):
    endpoint = Endpoint(difficulty=MAX_WORK + 1)
    with pytest.raises(Refused):
        endpoint.send(install_signal.payload(state, "cli"))
    assert endpoint.posted == [] and endpoint.paths == ["GET /v1/reports/challenge"]


def test_the_install_id_is_random_made_once_and_kept(state: Path, tmp_path: Path):
    made = install_signal.install_id(state)
    assert len(made) == 32 and set(made) <= set("0123456789abcdef")
    assert install_signal.install_id(state) == made
    assert install_signal.install_id(tmp_path / "other") != made
    (state / "install-id").write_text("/Users/carl\n")
    replaced = install_signal.install_id(state)
    assert replaced != made and len(replaced) == 32 and (state / "install-id").read_text().strip() == replaced


def test_it_goes_at_most_once_a_utc_day_and_a_failed_send_is_dropped(state: Path):
    sent: list[Install] = []

    def failing(install: Install) -> None:
        sent.append(install)
        assert json.loads((state / "install-signal.json").read_text()) == {"day": NOW // DAY}, "written before it sends"
        raise httpx.ConnectError("the endpoint is down")

    def never(install: Install) -> None:
        raise AssertionError("a second send the same day")

    assert install_signal.tick(state, True, NOW, "cli", failing)
    assert not install_signal.tick(state, True, NOW + 3600, "cli", never), "a failed send is not tried again that day"
    assert install_signal.tick(state, True, NOW + DAY, "cli", sent.append)
    assert len(sent) == 2 and sent[0].id == sent[1].id


def test_never_asked_it_sends_nothing_and_makes_no_id():
    assert install_signal.switch() == (False, None)
    assert not install_signal.beat("cli", send=lambda install: pytest.fail("nobody said yes"))
    assert not install_signal.state_dir().exists()


def test_a_yes_in_setup_writes_true_and_sends(capsys):
    assert main(["setup", "--signals", "yes"]) == 0
    assert saved_signals() == {"install": True} and install_signal.switch() == (True, "config")
    sent: list[Install] = []
    assert install_signal.beat("cli", send=sent.append) and len(sent) == 1
    assert "Anonymous usage data: on" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["n", "", "maybe"])
def test_anything_but_yes_in_a_terminal_writes_false(answer: str, vault_dir: Path):
    args = build_parser().parse_args(["setup", "--folder", str(vault_dir)])
    asked: list[str] = []
    assert setup.command_setup(args, ask=lambda question, secret: asked.append(question) or answer) == 0
    assert asked == [f"{install_signal.QUESTION} [y/N]"] and saved_signals() == {"install": False}
    assert not install_signal.beat("cli", send=lambda install: pytest.fail("the answer was no"))


def test_setup_asks_only_while_the_answer_is_missing(vault_dir: Path):
    write_config({"signals": {"install": False}})
    args = build_parser().parse_args(["setup", "--folder", str(vault_dir)])
    assert setup.command_setup(args, ask=lambda question, secret: pytest.fail(f"asked again: {question}")) == 0
    assert saved_signals() == {"install": False}


def test_setup_without_a_terminal_asks_nothing(vault_dir: Path):
    assert main(["setup", "--folder", str(vault_dir)]) == 0
    assert saved_signals() is None and install_signal.switch() == (False, None)


@pytest.mark.parametrize("name", ["VAULT_SIGNALS", "CLAUDE_PLUGIN_OPTION_VAULT_SIGNALS"])
def test_off_in_the_environment_sends_nothing_and_forgets_the_install(name: str, monkeypatch):
    write_config({"signals": {"install": True}})
    state = install_signal.state_dir()
    assert install_signal.beat("cli", send=lambda install: None)
    made = install_signal.install_id(state)
    monkeypatch.setenv(name, "off")
    assert install_signal.switch() == (False, "env")
    assert not install_signal.beat("cli", send=lambda install: pytest.fail("signals are off"))
    assert not (state / "install-id").exists() and not (state / "install-signal.json").exists()
    monkeypatch.delenv(name)
    assert install_signal.beat("cli", send=lambda install: None)
    assert install_signal.install_id(state) != made, "turned on again, it is a new install"


def test_a_no_after_a_yes_sends_nothing_and_forgets_the_install():
    state = install_signal.state_dir()
    assert main(["setup", "--signals", "yes"]) == 0
    assert install_signal.beat("cli", send=lambda install: None)
    assert main(["setup", "--signals", "no"]) == 0
    assert install_signal.switch() == (False, "config")
    assert not install_signal.beat("cli", send=lambda install: pytest.fail("signals are off"))
    assert not (state / "install-id").exists() and not (state / "install-signal.json").exists()


def test_the_environment_wins_over_the_config(monkeypatch):
    write_config({"signals": {"install": False}})
    monkeypatch.setenv("VAULT_SIGNALS", "on")
    assert install_signal.switch() == (True, "env")
    write_config({"signals": {"install": True}})
    monkeypatch.setenv("VAULT_SIGNALS", "off")
    assert install_signal.switch() == (False, "env")


def test_the_state_survives_clearing_the_cache(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "data" / "cache"))
    assert install_signal.state_dir() == tmp_path / "data" / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert install_signal.state_dir() == tmp_path / "xdg" / "mindin"


def test_it_names_how_mindin_runs(monkeypatch):
    assert install_signal.runs_as() == "cli"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugins/vault")
    assert install_signal.runs_as() == "plugin"


@pytest.mark.parametrize(
    ("argv", "env", "runs"),
    [
        (["serve"], {}, "cli"),
        (["serve"], {"CLAUDE_PLUGIN_ROOT": "/plugins/vault"}, "plugin"),
        (["serve", "--transport", "http"], {}, "server"),
    ],
)
def test_the_long_running_servers_start_the_signal(argv: list[str], env: dict, runs: str, monkeypatch):
    from mindin import cli

    started: list[str] = []
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(install_signal, "start", started.append)
    monkeypatch.setattr(cli, "run_stdio", lambda vault: None)
    monkeypatch.setattr("mindin.hosted.app.run_http", lambda *args: None)
    monkeypatch.setattr(cli, "auth_config", lambda: argparse.Namespace(subject_prefix="users", dav_url=None))
    monkeypatch.setattr(cli, "SubjectVaults", lambda **kwargs: argparse.Namespace(embedder=None))
    monkeypatch.setattr(cli, "keep_warm", lambda embedder: None)
    assert cli.main(argv) == 0
    assert started == [runs]


def test_nothing_is_sent_under_the_tests_or_in_ci(monkeypatch):
    monkeypatch.setattr(install_signal, "send", lambda install: pytest.fail("sent from a test run"))
    assert install_signal.quiet() and install_signal.start("cli") is None
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setenv("CI", "true")
    assert install_signal.quiet()
    monkeypatch.delenv("CI")
    assert not install_signal.quiet()


def test_status_says_whether_signals_are_on_and_what_decided(monkeypatch, capsys):
    def shown() -> tuple:
        main(["status"])
        found = json.loads(capsys.readouterr().out)
        assert found["signals_change"] == "mindin setup --signals yes|no"
        return found["signals"], found["signals_source"]

    assert shown() == ("not asked", None)
    write_config({"signals": {"install": True}})
    assert shown() == ("on", "config")
    monkeypatch.setenv("VAULT_SIGNALS", "off")
    assert shown() == ("off", "env")
