from __future__ import annotations

import json
import os
import pty
import select
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install.sh"
FAKE_UV = """#!/bin/sh
echo "uv $*" >> "$STUB_LOG"
case "$1 $2" in
  "tool install")
    mkdir -p "$STUB_BIN"
    printf '#!/bin/sh\\necho "mindin $*" >> "$STUB_LOG"\\n' > "$STUB_BIN/mindin"
    printf '[ "$1" = about ] && echo "mindin 9.9.9"\\nexit 0\\n' >> "$STUB_BIN/mindin"
    chmod +x "$STUB_BIN/mindin" ;;
  "tool dir") echo "$STUB_BIN" ;;
esac
"""
CODEX = '[model]\nname = "kept"\n'
CURSOR = {"mcpServers": {"other": {"command": "other"}}}


@pytest.fixture
def machine(tmp_path: Path) -> dict[str, str]:
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "uv").write_text(FAKE_UV)
    (tools / "uv").chmod(0o755)
    (tools / "claude").write_text('#!/bin/sh\necho "claude $*" >> "$STUB_LOG"\n')
    (tools / "claude").chmod(0o755)
    if jq := shutil.which("jq"):
        (tools / "jq").symlink_to(jq)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(CODEX)
    (home / ".cursor").mkdir()
    (home / ".cursor" / "mcp.json").write_text(json.dumps(CURSOR))
    return {
        "HOME": str(home),
        "PATH": f"{tools}:/usr/bin:/bin",
        "STUB_BIN": str(tmp_path / "bin"),
        "STUB_LOG": str(tmp_path / "log"),
        "NO_COLOR": "1",
    }


def logged(machine: dict[str, str]) -> list[str]:
    return Path(machine["STUB_LOG"]).read_text().splitlines()


def test_without_a_terminal_it_installs_sets_up_and_only_prints_the_client_snippets(machine: dict[str, str]):
    done = subprocess.run(
        ["sh", str(SCRIPT)], env=machine, capture_output=True, text=True, start_new_session=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    mindin = f"{machine['STUB_BIN']}/mindin"
    assert "uv tool install --upgrade mindin" in logged(machine) and "mindin setup" in logged(machine)
    assert not any(line.startswith("claude ") for line in logged(machine))
    assert "claude plugin install vault@gronare" in done.stdout
    assert f'command = "{mindin}"' in done.stdout and f'"command": "{mindin}"' in done.stdout
    home = Path(machine["HOME"])
    assert (home / ".codex" / "config.toml").read_text() == CODEX
    assert json.loads((home / ".cursor" / "mcp.json").read_text()) == CURSOR


def run_answering(machine: dict[str, str], answer: bytes) -> str:
    pid, terminal = pty.fork()
    if pid == 0:
        os.execve("/bin/sh", ["sh", str(SCRIPT)], machine)
    seen = b""
    while True:
        ready, _, _ = select.select([terminal], [], [], 30)
        if not ready:
            break
        try:
            chunk = os.read(terminal, 4096)
        except OSError:
            break
        if not chunk:
            break
        seen += chunk
        if seen.rstrip().endswith(b"[y/N]"):
            os.write(terminal, answer)
    os.waitpid(pid, 0)
    return seen.decode()


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_in_a_terminal_a_yes_adds_the_vault_to_each_client(machine: dict[str, str]):
    if not shutil.which("jq"):
        pytest.skip("merging Cursor's file needs jq")
    run_answering(machine, b"y\n")
    mindin = f"{machine['STUB_BIN']}/mindin"
    home = Path(machine["HOME"])
    assert "claude plugin install vault@gronare" in logged(machine)
    assert (home / ".codex" / "config.toml").read_text() == CODEX + f'\n[mcp_servers.vault]\ncommand = "{mindin}"\n'
    merged = json.loads((home / ".cursor" / "mcp.json").read_text())
    assert merged == {"mcpServers": {**CURSOR["mcpServers"], "vault": {"command": mindin}}}


@pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_in_a_terminal_a_no_leaves_every_client_as_it_was(machine: dict[str, str]):
    out = run_answering(machine, b"n\n")
    home = Path(machine["HOME"])
    assert not any(line.startswith("claude ") for line in logged(machine))
    assert (home / ".codex" / "config.toml").read_text() == CODEX
    assert json.loads((home / ".cursor" / "mcp.json").read_text()) == CURSOR
    assert "[mcp_servers.vault]" in out
