from __future__ import annotations

import os
import pty
import select
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "server.sh"
KEY = "c2VhbGVkLWJ5LWEtbWFzdGVyLWtleS1pbi10ZXN0cw=="
FAKE_DOCKER = f"""#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
if [ -n "${{STUB_FAIL:-}}" ]; then case "$*" in *"$STUB_FAIL"*) printf '%s\n' "$STUB_ERROR" >&2; exit 1 ;; esac; fi
case "$*" in *"master-key generate"*) echo "{KEY}" ;; esac
exit 0
"""
FAKE_CURL = """#!/bin/sh
echo "curl $*" >> "$STUB_LOG"
name=${4##*/}
[ "$name" = example.env ] && name=.env.example
cp "$STUB_FILES/$name" "$3"
"""
SETTINGS = {
    "VAULT_HOST": "vault.example.org",
    "VAULT_SMTP_HOST": "smtp.example.org",
    "VAULT_SMTP_FROM": "Vault <vault@example.org>",
    "VAULT_SMTP_USERNAME": "vault",
    "VAULT_SMTP_PASSWORD": "pa$$word",
    "FIRST_EMAIL": "you@example.org",
}


@pytest.fixture
def machine(tmp_path: Path) -> dict[str, str]:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        (tools / name).write_text(body)
        (tools / name).chmod(0o755)
    (tmp_path / "here").mkdir()
    return {
        "PATH": f"{tools}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "STUB_LOG": str(tmp_path / "log"),
        "STUB_FILES": str(ROOT / "deploy" / "docker"),
        "NO_COLOR": "1",
    }


def run(machine: dict[str, str], cwd: Path, **settings: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        cwd=cwd,
        env={**machine, **settings},
        capture_output=True,
        text=True,
        start_new_session=True,
        timeout=30,
    )


def run_at_a_terminal(machine: dict[str, str], cwd: Path, answers: list[str]) -> tuple[int, str]:
    pid, terminal = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execve("/bin/sh", ["/bin/sh", str(SCRIPT)], machine)
    os.write(terminal, "".join(f"{answer}\n" for answer in answers).encode())
    output, deadline = b"", time.monotonic() + 30
    while time.monotonic() < deadline:
        if select.select([terminal], [], [], 0.5)[0]:
            try:
                chunk = os.read(terminal, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
    else:
        os.kill(pid, signal.SIGKILL)
    os.close(terminal)
    return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]), output.decode()


def docker_calls(machine: dict[str, str]) -> list[str]:
    return [line for line in Path(machine["STUB_LOG"]).read_text().splitlines() if line.startswith("docker ")]


def test_a_fresh_install_writes_a_private_env_starts_the_stack_and_invites(machine: dict[str, str], tmp_path: Path):
    done = run(machine, tmp_path / "here", **SETTINGS)
    assert done.returncode == 0, done.stderr
    folder = tmp_path / "here" / "mindin"
    env = (folder / ".env").read_text()
    assert stat.S_IMODE((folder / ".env").stat().st_mode) == 0o600
    assert f"\nVAULT_HOSTED_MASTER_KEY={KEY}\n" in env and "\nVAULT_HOST=vault.example.org\n" in env
    assert "\nVAULT_SMTP_HOST=smtp.example.org\n" in env and "\nVAULT_SMTP_PASSWORD='pa$$word'\n" in env
    assert all((folder / name).is_file() for name in ("compose.yml", "Caddyfile", "example.env"))
    assert KEY not in done.stdout + done.stderr
    assert "https://vault.example.org/account" in done.stdout
    calls = docker_calls(machine)
    order = ["master-key generate", "compose up -d", "/ready", "accounts invite you@example.org"]
    found = [next(i for i, call in enumerate(calls) if step in call) for step in order]
    assert found == sorted(found), calls


def test_a_rerun_keeps_the_env_and_its_master_key(machine: dict[str, str], tmp_path: Path):
    here = tmp_path / "here"
    (here / "compose.yml").write_text("services: {}\n")
    kept = "VAULT_HOST=old.example.org\nVAULT_HOSTED_MASTER_KEY=a2VwdA==\nVAULT_SMTP_HOST=smtp.example.org\n"
    (here / ".env").write_text(kept)
    done = run(machine, here, **SETTINGS)
    assert done.returncode == 0, done.stderr
    assert (here / ".env").read_text() == kept
    assert not (here / "mindin").exists()
    calls = docker_calls(machine)
    assert not any("master-key" in call for call in calls)
    assert any("compose up -d" in call for call in calls) and "https://old.example.org/account" in done.stdout


def test_without_a_way_to_sign_in_it_stops_before_starting(machine: dict[str, str], tmp_path: Path):
    done = run(machine, tmp_path / "here", VAULT_HOST="vault.example.org")
    assert done.returncode == 1 and "needs a way to sign in" in done.stderr
    env = (tmp_path / "here" / "mindin" / ".env").read_text()
    assert "\n# VAULT_SMTP_HOST=smtp.example.com\n" in env and f"VAULT_HOSTED_MASTER_KEY={KEY}" in env
    assert not any("compose up" in call for call in docker_calls(machine))


def test_without_docker_it_says_so(machine: dict[str, str], tmp_path: Path):
    tools = machine["PATH"].split(":")[0]
    Path(tools, "docker").unlink()
    done = run({**machine, "PATH": tools}, tmp_path / "here", **SETTINGS)
    assert done.returncode == 1 and "Docker" in done.stderr
    assert not (tmp_path / "here" / "mindin").exists()


def test_a_pull_without_this_platform_shows_dockers_error_and_the_platform(machine: dict[str, str], tmp_path: Path):
    error = "no matching manifest for linux/arm64/v8 in the manifest list entries"
    done = run(machine, tmp_path / "here", **SETTINGS, STUB_FAIL="master-key generate", STUB_ERROR=error)
    assert done.returncode == 1
    assert "did not make a master key" in done.stderr and error in done.stderr
    assert "DOCKER_DEFAULT_PLATFORM=linux/amd64" in done.stderr
    assert not any("compose up" in call for call in docker_calls(machine))


def test_a_failing_start_shows_dockers_error(machine: dict[str, str], tmp_path: Path):
    error = "Error response from daemon: Bind for 0.0.0.0:443 failed: port is already allocated"
    done = run(machine, tmp_path / "here", **SETTINGS, STUB_FAIL="compose up", STUB_ERROR=error)
    assert done.returncode == 1
    assert "docker compose up stopped" in done.stderr and error in done.stderr
    assert "DOCKER_DEFAULT_PLATFORM" not in done.stderr


def written_env(tmp_path: Path) -> str:
    return (tmp_path / "here" / "mindin" / ".env").read_text()


@pytest.mark.parametrize(
    ("port", "security", "expected"),
    [("465", "", "ssl"), ("25", "none", "none"), ("2525", "starttls", "starttls")],
)
def test_the_smtp_security_comes_from_the_environment(
    machine: dict[str, str], tmp_path: Path, port: str, security: str, expected: str
):
    settings = {**SETTINGS, "VAULT_SMTP_PORT": port, "VAULT_SMTP_SECURITY": security}
    if security == "none":
        settings |= {"VAULT_SMTP_USERNAME": "", "VAULT_SMTP_PASSWORD": ""}
    done = run(machine, tmp_path / "here", **settings)
    assert done.returncode == 0, done.stderr
    env = written_env(tmp_path)
    assert f"\nVAULT_SMTP_SECURITY={expected}\n" in env and f"\nVAULT_SMTP_PORT={port}\n" in env


def test_smtp_security_none_with_a_login_stops_before_writing(machine: dict[str, str], tmp_path: Path):
    done = run(machine, tmp_path / "here", **SETTINGS, VAULT_SMTP_PORT="25", VAULT_SMTP_SECURITY="none")
    assert done.returncode == 1 and "username or password" in done.stderr
    assert not (tmp_path / "here" / "mindin" / ".env").exists()


def test_an_unknown_smtp_security_is_refused(machine: dict[str, str], tmp_path: Path):
    done = run(machine, tmp_path / "here", **SETTINGS, VAULT_SMTP_SECURITY="tls")
    assert done.returncode == 1 and "starttls, ssl or none" in done.stderr


@pytest.mark.parametrize(("answer", "expected"), [("none", "none"), ("", "starttls")])
def test_at_a_terminal_another_port_asks_for_the_smtp_security(
    machine: dict[str, str], tmp_path: Path, answer: str, expected: str
):
    answers = ["vault.example.org", "relay.internal", "25", answer, "", "", "", "you@example.org"]
    code, output = run_at_a_terminal(machine, tmp_path / "here", answers)
    assert code == 0, output
    assert "Security, starttls, ssl or none [starttls]:" in output
    env = written_env(tmp_path)
    assert f"\nVAULT_SMTP_SECURITY={expected}\n" in env and "\nVAULT_SMTP_PORT=25\n" in env


def test_at_a_terminal_port_587_asks_no_security(machine: dict[str, str], tmp_path: Path):
    answers = ["vault.example.org", "smtp.example.org", "587", "", "vault", "secret", "you@example.org"]
    code, output = run_at_a_terminal(machine, tmp_path / "here", answers)
    assert code == 0, output
    assert "Security" not in output
    assert "\n# VAULT_SMTP_SECURITY=starttls\n" in written_env(tmp_path)


def test_a_name_under_localhost_is_a_host(machine: dict[str, str], tmp_path: Path):
    done = run(machine, tmp_path / "here", **{**SETTINGS, "VAULT_HOST": "vault.localhost"})
    assert done.returncode == 0, done.stderr
    assert "\nVAULT_HOST=vault.localhost\n" in written_env(tmp_path)
    assert "https://vault.localhost/account" in done.stdout


@pytest.mark.parametrize("host", ["localhost", "vault"])
def test_a_single_word_is_refused_with_the_localhost_way(machine: dict[str, str], tmp_path: Path, host: str):
    done = run(machine, tmp_path / "here", **{**SETTINGS, "VAULT_HOST": host})
    assert done.returncode == 1 and f"{host} is not a domain name" in done.stderr
    assert "vault.localhost" in done.stderr
