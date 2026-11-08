from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from hosted_support import FixedLimits
from remote_support import Served, serve

from mindin import credentials, move, places
from mindin.auth.hosted_limits import StorageLimits
from mindin.cli import main
from mindin.config import config_dir
from mindin.vault import SubjectVaults

LIMITS = StorageLimits(
    account_bytes=200_000, file_bytes=10_000, content_types=("text/*", "image/*", "application/pdf", "application/json")
)
NOTE = "---\ntitle: A\ndate: 2026-11-01\nupdated: 2026-11-01\ntags: [x]\nstatus: active\n---\n\nBody.\n"
SOURCE = {
    "Areas/a.md": NOTE.encode(),
    "Welcome.md": b"# My own welcome\n",
    "Areas.base": b"views: []\n",
    "Bilagor/p.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
}


@pytest.fixture
def served(vaults: SubjectVaults, tmp_path: Path) -> Iterator[Served]:
    with serve(vaults, tmp_path, extensions=[FixedLimits(LIMITS)]) as running:
        credentials.save(running.url, {"key": running.key(), "scope": "vault:read vault:write", "name": "laptop"})
        yield running


def folder(root: Path, files: dict[str, bytes]) -> Path:
    for key, data in files.items():
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def on_server(served: Served) -> dict[str, bytes]:
    backend = served.backend()
    return {entry.key: backend.get_bytes(entry.key)[0] for entry in backend.list_all() if not entry.is_dir}


def saved_vault() -> dict:
    try:
        return tomllib.loads((config_dir() / "config.toml").read_text(encoding="utf-8")).get("vault", {})
    except FileNotFoundError:
        return {}


def run(monkeypatch: pytest.MonkeyPatch, source: places.Place, *argv: str) -> int:
    monkeypatch.setattr(move, "current_place", lambda: source)
    return main(["move", "--to", *argv])


def from_folder(root: Path) -> places.Place:
    return places.folder_place(str(root))


def test_a_folder_moves_onto_a_fresh_server_vault_and_the_machine_switches(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    source = folder(tmp_path / "vault", SOURCE | {".vault/schema.yml": b"preset: default\n"})
    assert run(monkeypatch, from_folder(source), "--server", served.url) == 0
    out = capsys.readouterr().out
    assert on_server(served) == tree(source)
    assert "Replaces the new vault's .vault/schema.yml, Areas.base, Welcome.md with the source's own" in out
    assert "Removes the new vault's Backlog.base, Log.base, Open tasks.base, Resources.base" in out
    assert "Verified 5 files" in out and f"{served.url}/mcp?vault=notes" in out and f"{served.url}/dav/" in out
    assert saved_vault() == {"kind": "server", "url": served.url}
    assert tree(source) == SOURCE | {".vault/schema.yml": b"preset: default\n"}


def test_a_mac_folder_leaves_its_ds_store_and_git_behind_and_says_so(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    junk = {".DS_Store": b"\x00\x01", "Areas/.DS_Store": b"\x00", ".git/HEAD": b"ref", ".git/objects/ab": b"x"}
    source = folder(tmp_path / "vault", SOURCE | junk)
    assert run(monkeypatch, from_folder(source), "--server", served.url) == 0
    out = capsys.readouterr().out
    assert "Left behind (--include-skipped brings them):" in out
    assert "    .DS_Store\n    .git/ (2 files)\n    Areas/.DS_Store\n" in out
    assert not any(".git" in key or "DS_Store" in key for key in on_server(served))
    assert on_server(served)["Areas/a.md"] == NOTE.encode()


def test_a_video_over_the_limits_is_listed_and_stops_the_move_before_any_copy(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    source = folder(tmp_path / "vault", SOURCE | {"clip.mp4": b"\x00" * 20, "Bilagor/big.png": b"\x00" * 20_000})
    assert run(monkeypatch, from_folder(source), "--server", served.url) == 1
    captured = capsys.readouterr()
    assert "clip.mp4 is refused: this server stores only" in captured.out and "video/mp4" in captured.out
    assert "Bilagor/big.png is refused: it is 20 kB, and a file is at most 10 kB" in captured.out
    assert "--skip-refused" in captured.err
    assert not {"Areas/a.md", "Bilagor/p.png"} & set(on_server(served)) and saved_vault() == {}


def test_skip_refused_moves_the_rest_and_lists_what_it_left(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    source = folder(tmp_path / "vault", SOURCE | {"clip.mp4": b"\x00" * 20})
    assert run(monkeypatch, from_folder(source), "--server", served.url, "--skip-refused") == 0
    out = capsys.readouterr().out
    assert "Left behind, as the target refuses them:\n  clip.mp4 is refused" in out
    assert "clip.mp4" not in on_server(served) and on_server(served)["Areas/a.md"] == NOTE.encode()
    assert saved_vault()["kind"] == "server"


def test_a_target_with_files_of_its_own_is_refused_and_merge_adds_to_it(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    served.write("Areas/mine.md", NOTE)
    source = folder(tmp_path / "vault", SOURCE)
    assert run(monkeypatch, from_folder(source), "--server", served.url) == 1
    err = capsys.readouterr().err
    assert "already holds files of its own:\n    Areas/mine.md" in err and "`--merge`" in err
    assert "Areas/a.md" not in on_server(served)
    assert run(monkeypatch, from_folder(source), "--server", served.url, "--merge") == 0
    found = on_server(served)
    assert found["Areas/mine.md"] == NOTE.encode() and found["Areas/a.md"] == NOTE.encode()


def test_merge_stops_on_a_file_that_differs_and_lists_it(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    served.write("Areas/a.md", NOTE + "Theirs.\n")
    source = folder(tmp_path / "vault", SOURCE)
    assert run(monkeypatch, from_folder(source), "--server", served.url, "--merge") == 1
    assert "differ between the source and" in capsys.readouterr().err
    assert on_server(served)["Areas/a.md"] == (NOTE + "Theirs.\n").encode()
    assert "Bilagor/p.png" not in on_server(served)


def test_an_interrupted_move_resumes_and_skips_what_it_copied(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    source = folder(tmp_path / "vault", SOURCE)
    create = move.DavSide.create
    sent: list[str] = []

    def drops_after_one(self: move.DavSide, key: str, data: bytes) -> None:
        if sent:
            raise move.MoveError("the connection dropped")
        sent.append(key)
        create(self, key, data)

    monkeypatch.setattr(move.DavSide, "create", drops_after_one)
    assert run(monkeypatch, from_folder(source), "--server", served.url) == 1
    assert "run the same command again" in capsys.readouterr().err
    assert saved_vault() == {}
    monkeypatch.setattr(move.DavSide, "create", create)
    assert run(monkeypatch, from_folder(source), "--server", served.url) == 0
    out = capsys.readouterr().out
    assert "Already there with the same content, skipped: 2 files" in out
    found = on_server(served)
    assert {key: found.get(key) for key in SOURCE} == SOURCE


def test_verify_catches_a_file_that_reads_back_different_and_keeps_the_old_place(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    source = folder(tmp_path / "vault", SOURCE)
    create = move.DavSide.create

    def mangles(self: move.DavSide, key: str, data: bytes) -> None:
        create(self, key, data + b"!" if key == "Areas/a.md" else data)

    monkeypatch.setattr(move.DavSide, "create", mangles)
    assert run(monkeypatch, from_folder(source), "--server", served.url) == 1
    assert "Areas/a.md reads back different from the source" in capsys.readouterr().err
    assert saved_vault() == {}


def test_a_vault_moves_from_a_folder_to_the_server_and_back_to_an_identical_tree(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    files = SOURCE | {".vault/schema.yml": b"preset: default\n", "Projects/Deep/b.md": NOTE.encode()}
    source = folder(tmp_path / "vault", files)
    assert run(monkeypatch, from_folder(source), "--server", served.url) == 0
    back = tmp_path / "back"
    assert run(monkeypatch, places.server_place(served.url), "--folder", str(back)) == 0
    out = capsys.readouterr().out
    assert tree(back) == tree(source)
    assert "passkeys, API and WebDAV keys" in out and f"Obsidian: open {back.resolve()}" in out
    assert saved_vault() == {"kind": "folder", "path": str(back.resolve())}


def test_a_stale_plugin_vault_path_is_reported_and_cleared_on_yes(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    source = folder(tmp_path / "vault", SOURCE)
    settings = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"
    options = {"vault_path": str(source.resolve()), "unrelated": "kept"}
    settings.write_text(json.dumps({"pluginConfigs": {"vault@gronare": {"options": options}}}), encoding="utf-8")
    answers = iter(["y"])
    target = places.Place({"kind": "server", "url": served.url, "vault": ""})
    assert move.move(from_folder(source), target, move.Options(), lambda question, secret: next(answers, "n")) == 0
    out = capsys.readouterr().out
    assert f"Claude Code's vault@gronare plugin (VAULT_PATH={source.resolve()})" in out
    assert 'under pluginConfigs."vault@gronare".options, remove "vault_path"' in out
    assert json.loads(settings.read_text())["pluginConfigs"]["vault@gronare"]["options"] == {"unrelated": "kept"}


def test_a_stale_codex_env_is_reported_with_the_exact_change(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    source = folder(tmp_path / "vault", SOURCE)
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "config.toml").write_text(f'[mcp_servers.vault]\nenv = {{ VAULT_PATH = "{source.resolve()}" }}\n')
    monkeypatch.setenv("CODEX_HOME", str(codex))
    assert run(monkeypatch, from_folder(source), "--server", served.url) == 0
    assert f"in {codex / 'config.toml'}, under [mcp_servers.vault.env], remove VAULT_PATH" in capsys.readouterr().out
    assert "VAULT_PATH" in (codex / "config.toml").read_text()


def test_a_dry_run_lists_the_plan_and_copies_nothing(
    served: Served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    source = folder(tmp_path / "vault", SOURCE)
    assert run(monkeypatch, from_folder(source), "--server", served.url, "--dry-run") == 0
    assert "Dry run: nothing copied. 4 files would be copied." in capsys.readouterr().out
    assert "Areas/a.md" not in on_server(served) and saved_vault() == {}
