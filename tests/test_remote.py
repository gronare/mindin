from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

import anyio
import httpx2
import pytest
from mcp import Client, StdioServerParameters
from remote_support import EMAIL, Served, serve
from test_cli_hooks import feed, head_sha, make_repo

from notes_vault_mcp import credentials, login, provision
from notes_vault_mcp.backends import NotFound
from notes_vault_mcp.backends.local import LocalBackend
from notes_vault_mcp.cli import main
from notes_vault_mcp.frontmatter import parse
from notes_vault_mcp.proxy import Upstream, build_proxy, build_signed_out
from notes_vault_mcp.vault import SubjectVaults

FAKE_KEY = "nvm_fake-key-for-tests"
RFC_8628 = "https://www.rfc-editor.org/rfc/rfc8628"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for name in ("VAULT_SERVER_URL", "CLAUDE_PLUGIN_OPTION_VAULT_SERVER_URL", "VAULT_STOP_HOOK"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def server(vaults: SubjectVaults, storage: Path, tmp_path: Path):
    with serve(vaults, tmp_path) as running:
        yield running


@pytest.fixture
def served(server: Served, vault_dir: Path) -> Served:
    server.seed(vault_dir)
    return server


@pytest.fixture
def remote(served: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Served:
    return signed_in(served, monkeypatch, tmp_path)


def signed_in(served: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Served:
    credentials.save(served.url, {"key": served.key(), "scope": "vault:read vault:write", "name": "laptop"})
    monkeypatch.setenv("VAULT_SERVER_URL", served.url)
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "client-cache"))
    return served


def run_json(capsys: pytest.CaptureFixture, *argv: str) -> dict:
    assert main(list(argv)) == 0, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


def note_on_server(served: Served, key: str) -> tuple[dict, str]:
    return parse(served.read(key))


def on_both(served: Served, local: Path, key: str, text: str) -> None:
    served.write(key, text)
    (local / key).parent.mkdir(parents=True, exist_ok=True)
    (local / key).write_text(text, encoding="utf-8")


def system_note(repo: Path, kind: str = "system") -> str:
    return (
        "---\ntitle: frame\ndate: 2020-01-01\nupdated: 2020-01-01\n"
        f"tags: [frame]\nstatus: active\nkind: {kind}\npath: {repo}\n---\n\nKärnan.\n"
    )


def hook_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, repo: Path) -> str:
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "session-start"]) == 0
    return capsys.readouterr().out


def local_hook_output(monkeypatch, capsys, repo: Path, folder: Path, tmp_path: Path) -> str:
    monkeypatch.delenv("VAULT_SERVER_URL")
    monkeypatch.setenv("VAULT_PATH", str(folder))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "local-cache"))
    return hook_output(monkeypatch, capsys, repo)


class FakeDeviceServer:
    def __init__(self, answers: list[tuple[int, dict]]) -> None:
        self.answers = answers
        self.started: dict = {}
        self.polls: list[dict] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        form = dict(httpx2.QueryParams(request.content.decode()))
        if request.url.path == "/device_authorization":
            self.started = form
            return httpx2.Response(
                200,
                json={
                    "device_code": "device-code",
                    "user_code": "BCDF-GHJK",
                    "verification_uri": "https://vault.example.test/device",
                    "verification_uri_complete": "https://vault.example.test/device?user_code=BCDF-GHJK",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        if request.url.path == "/token":
            self.polls.append(form)
            status, body = self.answers.pop(0)
            return httpx2.Response(status, json=body)
        return httpx2.Response(404)


def fake_login(server: FakeDeviceServer, waits: list[float], out) -> dict:
    with httpx2.Client(transport=httpx2.MockTransport(server.handle)) as http:
        return login.login(
            "https://vault.example.test/mcp", name="devbox", out=out, http=http, sleep=waits.append, open_browser=False
        )


def test_login_stores_the_key_with_mode_600(tmp_path: Path, capsys):
    server = FakeDeviceServer(
        [
            (400, {"error": "authorization_pending"}),
            (400, {"error": "slow_down"}),
            (200, {"access_token": FAKE_KEY, "scope": "vault:read vault:write", "key_name": "devbox"}),
        ]
    )
    waits: list[float] = []
    fake_login(server, waits, sys.stdout)
    path = tmp_path / "config" / "notes-vault-mcp" / "credentials.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    stored = json.loads(path.read_text(encoding="utf-8"))["servers"]["https://vault.example.test"]
    assert stored["key"] == FAKE_KEY and stored["name"] == "devbox"
    assert server.started == {"client_id": "cli", "scope": "vault:read vault:write", "name": "devbox"}
    assert server.polls[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert waits == [5, 5, 10], RFC_8628
    printed = capsys.readouterr().out
    assert "BCDF-GHJK" in printed and "https://vault.example.test/device" in printed
    assert FAKE_KEY not in printed


def test_a_denied_login_stores_nothing(tmp_path: Path):
    server = FakeDeviceServer([(400, {"error": "access_denied"})])
    with pytest.raises(login.LoginError, match="denied"):
        fake_login(server, [], io.StringIO())
    assert not (tmp_path / "config" / "notes-vault-mcp" / "credentials.json").exists()


def test_login_refuses_plain_http_off_this_machine():
    with pytest.raises(login.ConfigError, match="https"):
        credentials.server_base("http://vault.example.test")


def test_login_through_the_hosted_device_grant(served: Served):
    def approve(_: float) -> None:
        with served.store.connect() as connection:
            row = connection.execute("SELECT device_hash FROM devices").fetchone()
        served.store.update_device(
            row["device_hash"], status="approved", account_id=served.account_id(), granted="vault:read vault:write"
        )

    entry = login.login(served.url, name="devbox", out=io.StringIO(), sleep=approve, open_browser=False)
    assert entry["key"].startswith("nvm_")
    assert credentials.stored(served.url)["key"] == entry["key"]
    assert [row["name"] for row in served.store.keys(served.account_id())] == ["devbox"]


def test_whoami_and_logout(remote: Served, capsys):
    found = run_json(capsys, "whoami")
    assert found == {"server": remote.url, "email": EMAIL, "key": "laptop", "write": True}
    assert main(["logout"]) == 0
    assert "Forgot the key" in capsys.readouterr().out
    assert credentials.stored(remote.url) is None
    assert main(["whoami"]) == 1
    assert "notes-vault-mcp login" in capsys.readouterr().err


def test_session_start_through_the_server_equals_the_local_vault(
    remote: Served, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    on_both(remote, vault_dir, "Areas/frame.md", system_note(repo))
    through_server = hook_output(monkeypatch, capsys, repo)
    local = local_hook_output(monkeypatch, capsys, repo, vault_dir, tmp_path)
    assert through_server == local
    assert "commits since the note — Areas/frame.md" in through_server
    assert "First commit" in through_server


def preset_vault(folder: Path, preset: str) -> Path:
    provision.initialize(LocalBackend(folder), preset=preset)
    return folder


def test_session_start_through_the_server_equals_local_on_the_para_layout(
    server: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    local = preset_vault(tmp_path / "para", "para")
    server.seed(local)
    signed_in(server, monkeypatch, tmp_path)
    on_both(server, local, "Areas/frame.md", system_note(repo, kind="area"))
    task = (
        "---\ntitle: Frame the grove\ndate: @@\nupdated: @@\ntags: [work]\nstatus: active\nkind: task\n"
        'area: "[[frame]]"\n---\n\n## Outcome\n'
    ).replace("@@", date.today().isoformat())
    on_both(server, local, "Projects/frame-grove.md", task)
    assert run_json(capsys, "log", "append", "--cwd", str(repo), "--line", "Ramade in lunden") == {
        "path": "Resources/Logs/frame-log.md"
    }
    (local / "Resources" / "Logs").mkdir(parents=True, exist_ok=True)
    (local / "Resources/Logs/frame-log.md").write_text(server.read("Resources/Logs/frame-log.md"), encoding="utf-8")
    through_server = hook_output(monkeypatch, capsys, repo)
    assert through_server == local_hook_output(monkeypatch, capsys, repo, local, tmp_path)
    assert "### Areas/frame.md" in through_server and "Projects/frame-grove.md" in through_server
    assert "## log — frame" in through_server and "Ramade in lunden" in through_server
    assert "commits since the note — Areas/frame.md" in through_server


def test_a_layout_without_a_home_answers_in_one_line_through_the_server(
    server: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    server.seed(preset_vault(tmp_path / "custom", "custom"))
    signed_in(server, monkeypatch, tmp_path)
    repo = make_repo(tmp_path / "repos", "frame")
    assert main(["task", "open", "--name", "Prune", "--cwd", str(repo)]) == 1
    opened = capsys.readouterr().err
    assert "has no task folder" in opened and len(opened.strip().splitlines()) == 1
    assert main(["log", "append", "--cwd", str(repo), "--line", "x"]) == 1
    logged = capsys.readouterr().err
    assert "has no" in logged and len(logged.strip().splitlines()) == 1
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    stopped = json.loads(capsys.readouterr().out)
    assert stopped["decision"] == "block" and head_sha(repo) in stopped["reason"]


def test_session_start_without_a_key_says_how_to_sign_in(
    served: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    monkeypatch.setenv("VAULT_SERVER_URL", served.url)
    feed(monkeypatch, {"cwd": str(tmp_path)})
    assert main(["hook", "session-start"]) == 0
    assert capsys.readouterr().out.startswith(f"vault: notes-vault-mcp: not signed in to {served.url}")


def test_stop_blocks_on_an_unlogged_commit_through_the_server(
    remote: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert head_sha(repo) in payload["reason"]


def test_claim_and_release_round_trip_through_the_server(remote: Served, capsys):
    key = "Projects/orchard-fresh.md"
    claimed = run_json(capsys, "task", "claim", key, "--machine", "devbox", "--task", "t1", "--repo", "orchard")
    assert claimed["claimed"] is True
    assert note_on_server(remote, key)[0]["kebbie"]["machine"] == "devbox"
    elsewhere = run_json(capsys, "task", "claim", key, "--machine", "laptop", "--task", "t2")
    assert elsewhere == {"path": key, "claimed": False, "claim": claimed["claim"]}
    refused = run_json(capsys, "task", "release", key, "--machine", "laptop")
    assert refused["released"] is False
    released = run_json(capsys, "task", "release", key, "--machine", "devbox")
    assert released == {"path": key, "released": True, "claim": None}
    assert "kebbie" not in note_on_server(remote, key)[0]


def test_release_by_task_clears_every_claim_through_the_server(remote: Served, capsys):
    for key in ("Projects/orchard-fresh.md", "Projects/orchard-draft.md"):
        run_json(capsys, "task", "claim", key, "--machine", "devbox", "--task", "t1")
    cleared = run_json(capsys, "task", "release", "--machine", "devbox", "--task", "t1")
    assert sorted(cleared["paths"]) == ["Projects/orchard-draft.md", "Projects/orchard-fresh.md"]
    assert all("kebbie" not in note_on_server(remote, key)[0] for key in cleared["paths"])


def test_log_append_round_trips_through_the_server(remote: Served, tmp_path: Path, capsys):
    repo = make_repo(tmp_path / "repos", "orchard")
    sha = head_sha(repo)
    logged = run_json(capsys, "log", "append", "--cwd", str(repo), "--line", "Byggde lunden", "--commit", sha)
    assert logged == {"path": "Log/orchard-log.md"}
    _, body = note_on_server(remote, "Log/orchard-log.md")
    last = body.rstrip().splitlines()[-1]
    assert "Byggde lunden" in last and sha in last and current(repo) in last
    assert run_json(capsys, "log", "path", "--repo", "orchard") == {"path": "Log/orchard-log.md"}


def current(repo: Path) -> str:
    return subprocess.run(["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True).stdout.strip()


def test_task_open_check_and_close_through_the_server(remote: Served, tmp_path: Path, capsys):
    repo = make_repo(tmp_path / "repos", "orchard")
    opened = run_json(capsys, "task", "open", "--name", "Prune the grove", "--cwd", str(repo))
    assert opened == {
        "path": "Projects/orchard-prune-the-grove.md",
        "created": True,
        "repo": "orchard",
        "area": "orchard",
    }
    frontmatter, _ = note_on_server(remote, opened["path"])
    assert frontmatter["area"] == "[[orchard]]" and frontmatter["status"] == "active"
    missing = run_json(capsys, "task", "check", opened["path"])
    assert missing == {"missing": ["problem", "solution", "decisions"]}
    again = run_json(capsys, "task", "open", "--name", "Prune the grove", "--cwd", str(repo))
    assert again["path"] == "Projects/orchard-prune-the-grove-2.md"
    parked = run_json(capsys, "task", "backlog", again["path"], "--priority", "low")
    assert note_on_server(remote, parked["path"])[0]["status"] == "backlog"
    closed = run_json(capsys, "task", "close", opened["path"])
    assert closed["path"].startswith("Archive/")
    assert note_on_server(remote, closed["path"])[0]["status"] == "complete"


def test_changelog_writes_the_period_page_with_local_commits(remote: Served, tmp_path: Path, capsys):
    repo = make_repo(tmp_path / "repos", "orchard")
    dated = {"GIT_AUTHOR_DATE": "2026-08-15T12:00:00", "GIT_COMMITTER_DATE": "2026-08-15T12:00:00"}
    identity = ["-c", "user.email=t@example.com", "-c", "user.name=T"]
    subprocess.run(
        ["git", *identity, "commit", "-q", "--allow-empty", "-m", "Mid-month commit"],
        cwd=repo,
        check=True,
        env={**os.environ, **dated},
    )
    sha, period = head_sha(repo), "2026-08"
    assert main(["changelog", "orchard", period, "--repo-path", str(repo), "--write"]) == 0
    assert capsys.readouterr().out.strip() == f"wrote Log/orchard-{period}.md"
    _, body = note_on_server(remote, f"Log/orchard-{period}.md")
    assert f"{sha} Mid-month commit" in body


def test_a_read_only_key_is_refused_writes_clearly(served: Served, monkeypatch, tmp_path: Path, capsys):
    credentials.save(served.url, {"key": served.key(write=False), "scope": "vault:read", "name": "laptop"})
    monkeypatch.setenv("VAULT_SERVER_URL", served.url)
    repo = make_repo(tmp_path / "repos", "orchard")
    assert main(["task", "claim", "Projects/orchard-fresh.md", "--machine", "m", "--task", "t"]) == 1
    assert "may only read the vault; writing needs the vault:write scope" in capsys.readouterr().err
    assert main(["log", "append", "--cwd", str(repo), "--line", "x"]) == 1
    assert "may only read the vault" in capsys.readouterr().err
    assert run_json(capsys, "status", "--probe")["writable"] is False
    assert run_json(capsys, "whoami")["write"] is False


def test_status_names_the_server(remote: Served, capsys):
    status = run_json(capsys, "status", "--probe")
    assert status["backend"] == "server" and status["server"] == remote.url
    assert status["configured"] is True and status["writable"] is True


def test_a_stored_key_changes_nothing_without_a_server_url(served: Served, vault_dir: Path, monkeypatch, capsys):
    credentials.save(served.url, {"key": served.key(), "scope": "vault:read vault:write", "name": "laptop"})
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    status = run_json(capsys, "status")
    assert status == {"configured": True, "version": status["version"], "backend": "local", "writable": None}


def test_the_plugin_config_points_kebbie_at_the_server(remote: Served, monkeypatch, tmp_path: Path, capsys):
    run_json(capsys, "task", "check", "Projects/orchard-fresh.md")
    storage = os.environ["VAULT_PATH"]
    monkeypatch.delenv("VAULT_SERVER_URL")
    monkeypatch.delenv("VAULT_PATH")
    (tmp_path / "claude").mkdir(exist_ok=True)
    (tmp_path / "claude" / "settings.json").write_text(
        json.dumps({"pluginConfigs": {"vault@gronare": {"options": {"vault_server_url": remote.url}}}}),
        encoding="utf-8",
    )
    claimed = run_json(capsys, "task", "claim", "Projects/orchard-fresh.md", "--machine", "devbox", "--task", "t1")
    assert claimed["claimed"] is True
    monkeypatch.setenv("VAULT_PATH", storage)
    assert note_on_server(remote, "Projects/orchard-fresh.md")[0]["kebbie"]["task"] == "t1"


@pytest.mark.anyio
async def test_the_proxy_serves_the_servers_tools(remote: Served):
    upstream = Upstream(remote.url)
    async with anyio.create_task_group() as group:
        group.start_soon(upstream.supervise)
        await upstream.ready.wait()
        proxy = build_proxy(upstream, upstream.remote.client.instructions)
        async with Client(proxy) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            found = await client.call_tool("read_file", {"path": "Areas/orchard.md"})
            hidden = await client.call_tool("claim", {"path": "Projects/orchard-fresh.md", "machine": "m", "task": "t"})
            linted = await client.call_tool("lint_and_park", {"write": "Log/lint.md", "park": True})
            instructions = client.instructions
        group.cancel_scope.cancel()
    assert {"search", "read_file", "context"} <= tools
    assert not {"claim", "release", "changelog", "lint_and_park"} & tools, "the proxied model never sees them"
    assert hidden.is_error and "Unknown tool" in hidden.content[0].text
    assert linted.is_error and "Unknown tool" in linted.content[0].text
    with pytest.raises(NotFound):
        remote.read("Log/lint.md")
    assert "kebbie" not in note_on_server(remote, "Projects/orchard-fresh.md")[0]
    assert instructions and "Areas/" in instructions
    assert "Kontrollplanet" in "".join(block.text for block in found.content)


@pytest.mark.anyio
async def test_the_proxy_reconnects_when_asked(remote: Served):
    upstream = Upstream(remote.url)
    async with anyio.create_task_group() as group:
        group.start_soon(upstream.supervise)
        await upstream.ready.wait()
        first = upstream.remote
        upstream.ask_again()
        found = await upstream.use(lambda server: server.call("whoami"))
        group.cancel_scope.cancel()
    assert upstream.remote is not first
    assert json.loads(found)["email"] == EMAIL


@pytest.mark.anyio
async def test_a_signed_out_proxy_says_how_to_sign_in(served: Served):
    async with Client(build_signed_out(served.url)) as client:
        tools = (await client.list_tools()).tools
        instructions = client.instructions
    assert tools == []
    assert f"notes-vault-mcp login {served.url}" in instructions


@pytest.mark.anyio
async def test_serve_over_stdio_proxies_the_server(remote: Served, tmp_path: Path):
    environment = {
        **os.environ,
        "VAULT_SERVER_URL": remote.url,
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "VAULT_CACHE_DIR": str(tmp_path / "client-cache"),
    }
    launched = StdioServerParameters(command=sys.executable, args=["-m", "notes_vault_mcp.cli"], env=environment)
    async with Client(launched) as client:
        found = await client.call_tool("search", {"query": "Kontrollplanet"})
    assert "Areas/orchard.md" in "".join(block.text for block in found.content)


def test_a_refused_key_ends_a_shell_command_with_one_line(served: Served, monkeypatch, capsys):
    credentials.save(served.url, {"key": FAKE_KEY, "scope": "vault:read", "name": "laptop"})
    monkeypatch.setenv("VAULT_SERVER_URL", served.url)
    assert main(["search", "orchard"]) == 1
    error = capsys.readouterr().err
    assert error.startswith(f"notes-vault-mcp: {served.url} refused the stored key (401)")
    assert len(error.strip().splitlines()) == 1


def test_login_without_a_url_names_the_server_or_takes_the_default(monkeypatch, capsys):
    assert main(["login"]) == 2
    assert "name the server" in capsys.readouterr().err
    monkeypatch.setattr(credentials, "DEFAULT_SERVER_URL", "https://vault.example.test")
    assert login.server_argument(None) == "https://vault.example.test"
    monkeypatch.setenv("VAULT_SERVER_URL", "https://own.example.test/mcp")
    assert login.server_argument(None) == "https://own.example.test"


def test_init_through_the_server_sends_a_layout_choice_to_the_account_page(
    server: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    signed_in(server, monkeypatch, tmp_path)
    before = server.read(".vault/schema.yml") if main(["init"]) == 0 else None
    capsys.readouterr()
    assert main(["init", "--preset", "para"]) == 2
    assert f"{server.url}/account" in capsys.readouterr().err
    assert server.read(".vault/schema.yml") == before


def lint_run(capsys: pytest.CaptureFixture, *options: str) -> tuple[str, str]:
    assert main(["lint", *options]) == 0
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_the_weekly_lint_through_the_server_equals_the_local_run(
    remote: Served, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    options = ("--write", "Log/lint.md", "--park-stale")
    storage = os.environ["VAULT_PATH"]
    through_server = lint_run(capsys, *options)
    monkeypatch.delenv("VAULT_SERVER_URL")
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "local-cache"))
    local = lint_run(capsys, *options)
    assert through_server == local
    assert "parked" in through_server[0] and "Projects/workshop-stale.md" in through_server[0]
    assert through_server[1] == "wrote Log/lint.md\n"
    monkeypatch.setenv("VAULT_PATH", storage)
    assert remote.read("Log/lint.md") == (vault_dir / "Log" / "lint.md").read_text(encoding="utf-8")
    stale = "Projects/workshop-stale.md"
    assert remote.read(stale) == (vault_dir / stale).read_text(encoding="utf-8")
    assert note_on_server(remote, stale)[0]["status"] == "backlog"


def test_a_read_only_key_cannot_run_the_weekly_lint(served: Served, monkeypatch, tmp_path: Path, capsys):
    credentials.save(served.url, {"key": served.key(write=False), "scope": "vault:read", "name": "laptop"})
    monkeypatch.setenv("VAULT_SERVER_URL", served.url)
    assert main(["lint", "--park-stale"]) == 1
    assert "may only read the vault" in capsys.readouterr().err
