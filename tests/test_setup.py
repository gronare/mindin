from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest
from test_cli_tasks import write_settings

from mindin import credentials, places
from mindin.cli import build_parser, main
from mindin.config import adopt_config, config_dir

pytestmark = pytest.mark.usefixtures("nothing_configured")


def saved() -> dict:
    return tomllib.loads((config_dir() / "config.toml").read_text(encoding="utf-8"))


def status(capsys: pytest.CaptureFixture) -> dict:
    capsys.readouterr()
    assert main(["status"]) == 0
    return json.loads(capsys.readouterr().out)


def test_setup_saves_a_folder_and_every_command_finds_it(vault_dir: Path, forget_storage, capsys):
    assert main(["setup", "--folder", str(vault_dir)]) == 0
    assert saved() == {"vault": {"kind": "folder", "path": str(vault_dir.resolve())}}
    forget_storage()
    found = status(capsys)
    assert found["configured"] and found["backend"] == "local" and found["source"] == "config"
    assert main(["read", "Areas/orchard.md", "--plain"]) == 0
    assert capsys.readouterr().out


def test_setup_refuses_a_folder_that_does_not_exist(tmp_path: Path, capsys):
    assert main(["setup", "--folder", str(tmp_path / "nowhere")]) == 2
    assert "no folder at" in capsys.readouterr().err
    assert not (config_dir() / "config.toml").exists()


def test_setup_keeps_the_bucket_keys_out_of_the_config(forget_storage, monkeypatch, capsys):
    monkeypatch.setenv("S3_ACCESS_KEY", "ak-orchard")
    monkeypatch.setenv("S3_SECRET_KEY", "sk-orchard")
    argv = ["setup", "--s3", "notes", "--endpoint", "https://s3.example.test/", "--prefix", "me"]
    assert main(argv) == 0
    assert saved() == {
        "vault": {
            "kind": "s3",
            "endpoint": "https://s3.example.test",
            "bucket": "notes",
            "prefix": "me",
            "region": "us-east-1",
        }
    }
    assert "sk-orchard" not in (config_dir() / "config.toml").read_text(encoding="utf-8")
    assert credentials.s3_keys("https://s3.example.test", "notes") == {
        "access_key": "ak-orchard",
        "secret_key": "sk-orchard",
    }
    assert stat.S_IMODE(credentials.credentials_path().stat().st_mode) == 0o600
    forget_storage()
    assert adopt_config() == "config"
    assert os.environ["S3_SECRET_KEY"] == "sk-orchard" and os.environ["S3_PREFIX"] == "me"


def test_saving_bucket_keys_keeps_the_server_keys(monkeypatch):
    credentials.save("https://vault.example.com", {"key": "nvm_kept"})
    monkeypatch.setenv("S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("S3_SECRET_KEY", "sk")
    assert main(["setup", "--s3", "notes", "--endpoint", "https://s3.example.test"]) == 0
    assert credentials.stored("https://vault.example.com")["key"] == "nvm_kept"


def test_setup_for_a_bucket_from_a_script_needs_the_keys(capsys):
    assert main(["setup", "--s3", "notes", "--endpoint", "https://s3.example.test"]) == 2
    assert "S3_ACCESS_KEY and S3_SECRET_KEY" in capsys.readouterr().err
    assert main(["setup", "--s3", "notes"]) == 2
    assert "--endpoint" in capsys.readouterr().err


def test_setup_saves_a_server_and_signs_the_machine_in(forget_storage, monkeypatch, capsys):
    signed = []

    def sign_in(url: str, **options: object) -> dict:
        signed.append((url, options["vault"]))
        entry = {"key": "nvm_new", "scope": "vault:read vault:write", "name": "laptop", "vault": "workshop"}
        credentials.save(url, entry, "workshop")
        return entry

    monkeypatch.setattr("mindin.login.login", sign_in)
    assert main(["setup", "--server", "https://vault.example.com/mcp", "--vault", "workshop", "--no-browser"]) == 0
    assert saved() == {"vault": {"kind": "server", "url": "https://vault.example.com"}}
    assert signed == [("https://vault.example.com", "workshop")]
    forget_storage()
    found = status(capsys)
    assert found["backend"] == "server" and found["source"] == "config" and found["signed_in"]
    assert main(["setup", "--server", "https://vault.example.com"]) == 0
    assert len(signed) == 1 and "Already signed in" in capsys.readouterr().out


def test_setup_asks_in_a_terminal(vault_dir: Path, capsys):
    answers = iter(["7", "1", str(vault_dir), ""])
    args = build_parser().parse_args(["setup"])
    assert places.command_setup(args, ask=lambda question, secret: next(answers)) == 0
    out = capsys.readouterr().out
    assert "1  A folder on this machine" in out and "Answer a number from 1 to 3." in out
    assert saved()["vault"]["path"] == str(vault_dir.resolve())
    assert saved()["signals"] == {"install": False}, "an empty answer is a no"


def test_setup_asks_for_the_secret_key_without_echoing_it(capsys):
    answers = iter(["2", "https://s3.example.test", "notes", "", "", "ak", "sk", "n"])
    secrets = []

    def ask(question: str, secret: bool) -> str:
        secrets.append(secret)
        return next(answers)

    assert places.command_setup(build_parser().parse_args(["setup"]), ask=ask) == 0
    assert secrets == [False, False, False, False, False, False, True, False]
    assert credentials.s3_keys("https://s3.example.test", "notes")["secret_key"] == "sk"


def test_setup_without_a_terminal_names_the_flags(capsys):
    assert main(["setup"]) == 2
    assert "--folder PATH, --s3 BUCKET, --server URL" in capsys.readouterr().err


def test_setup_says_when_the_environment_still_decides(vault_dir: Path, tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    assert main(["setup", "--folder", str(vault_dir)]) == 0
    assert "The environment names another vault" in capsys.readouterr().out


def test_the_environment_wins_over_mindins_config(vault_dir: Path, tmp_path: Path, monkeypatch, capsys):
    assert main(["setup", "--folder", str(tmp_path)]) == 0
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    assert adopt_config() == "env"
    assert os.environ["VAULT_PATH"] == str(vault_dir)
    assert status(capsys)["source"] == "env"


def test_mindins_config_wins_over_the_plugin(nothing_configured: Path, vault_dir: Path, tmp_path: Path, forget_storage):
    write_settings(nothing_configured, {"vault_path": str(tmp_path / "from-plugin")})
    assert main(["setup", "--folder", str(vault_dir)]) == 0
    forget_storage()
    assert adopt_config() == "config"
    assert os.environ["VAULT_PATH"] == str(vault_dir.resolve())


def test_the_plugin_decides_when_mindin_has_no_config(nothing_configured: Path, tmp_path: Path, capsys):
    write_settings(nothing_configured, {"vault_path": str(tmp_path)})
    found = status(capsys)
    assert found["source"] == "plugin" and found["backend"] == "local"


def test_status_names_no_source_when_nothing_decides(capsys):
    found = status(capsys)
    assert found["configured"] is False and found["source"] is None


def test_each_choice_is_one_entry_with_its_own_flag():
    setup = build_parser()._subparsers._group_actions[0].choices["setup"]
    flags = {option for action in setup._actions for option in action.option_strings}
    assert {choice.flag for choice in places.CHOICES} <= flags
    assert [choice.kind for choice in places.CHOICES] == ["folder", "s3", "server"]
