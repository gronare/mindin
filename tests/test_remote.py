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
from test_cli_hooks import feed, head_sha, make_repo, prompts, transcript_of

from mindin import credentials, login, provision
from mindin.backends import NotFound
from mindin.backends.local import LocalBackend
from mindin.cli import main
from mindin.frontmatter import parse
from mindin.proxy import Upstream, build_proxy, build_signed_out
from mindin.vault import SubjectVaults

FAKE_KEY = "nvm_fake-key-for-tests"
RFC_8628 = "https://www.rfc-editor.org/rfc/rfc8628"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for name in (
        "VAULT_SERVER_URL",
        "CLAUDE_PLUGIN_OPTION_VAULT_SERVER_URL",
        "VAULT_STOP_HOOK",
        "VAULT_STOP_HOOK_PROMPTS",
        "VAULT_STOP_HOOK_COMMITS",
    ):
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
    path = tmp_path / "config" / "mindin" / "credentials.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    stored = json.loads(path.read_text(encoding="utf-8"))["servers"]["https://vault.example.test"]["vaults"][""]
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
    assert not (tmp_path / "config" / "mindin" / "credentials.json").exists()


def test_login_refuses_plain_http_off_this_machine():
    with pytest.raises(login.ConfigError, match="https"):
        credentials.server_base("http://vault.example.test")


def test_login_through_the_hosted_device_grant(served: Served):
    def approve(_: float) -> None:
        with served.store.connect() as connection:
            row = connection.execute("SELECT device_hash FROM devices").fetchone()
        served.store.update_device(
            row["device_hash"],
            status="approved",
            account_id=served.account_id(),
            granted="vault:read vault:write",
            vault_id=served.vault_id(),
        )

    entry = login.login(served.url, name="devbox", out=io.StringIO(), sleep=approve, open_browser=False)
    assert entry["key"].startswith("nvm_") and entry["vault"] == "notes"
    assert credentials.stored(served.url)["key"] == entry["key"]
    assert credentials.server_state(served.url)["default"] == "notes"
    assert [row["name"] for row in served.store.keys(served.account_id())] == ["devbox"]


def test_whoami_and_logout(remote: Served, capsys):
    found = run_json(capsys, "whoami")
    assert found == {
        "server": remote.url,
        "email": EMAIL,
        "vault": "notes",
        "vault_kind": "personal",
        "key": "laptop",
        "write": True,
    }
    assert main(["logout"]) == 0
    assert "Forgot every key" in capsys.readouterr().out
    assert credentials.stored(remote.url) is None
    assert main(["whoami"]) == 1
    assert "mindin login" in capsys.readouterr().err


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
    assert capsys.readouterr().out.startswith(f"vault: mindin: not signed in to {served.url}")


def test_stop_blocks_on_an_unlogged_commit_through_the_server(
    remote: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert head_sha(repo) in payload["reason"]


def test_stop_asks_once_about_prompts_through_the_server(
    remote: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    repo = make_repo(tmp_path / "repos", "frame")
    transcript = transcript_of(tmp_path / "t.jsonl", prompts("p1", "p2", "p3"))
    feed(monkeypatch, {"cwd": str(repo), "session_id": "s-frame", "transcript_path": str(transcript)})
    assert main(["hook", "stop"]) == 0
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert head_sha(repo) in reason and "the user wrote 3 messages since" in reason
    outside = tmp_path / "shed"
    outside.mkdir()
    feed(monkeypatch, {"cwd": str(outside), "session_id": "s-frame", "transcript_path": str(transcript)})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out.strip() == ""
    feed(monkeypatch, {"cwd": str(outside), "session_id": "s-shed", "transcript_path": str(transcript)})
    assert main(["hook", "stop"]) == 0
    assert "the user wrote 3 messages since" in json.loads(capsys.readouterr().out)["reason"]


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
    assert run_json(capsys, "log", "path", "--repo", "orchard", "--repo", "frame") == {
        "orchard": "Log/orchard-log.md",
        "frame": "Log/frame-log.md",
    }


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
    assert status == {
        "configured": True,
        "version": status["version"],
        "backend": "local",
        "source": "env",
        "writable": None,
        "signals": "off",
        "signals_source": "env",
        "signals_change": "mindin setup --signals yes|no",
    }


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
            pictured = await client.call_tool("snapshot", {})
            instructions = client.instructions
        group.cancel_scope.cancel()
    assert {"search", "read_file", "context"} <= tools
    hidden_tools = {"claim", "release", "changelog", "lint_and_park", "snapshot"}
    assert not hidden_tools & tools, "the proxied model never sees them"
    assert pictured.is_error and "Unknown tool" in pictured.content[0].text
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
    assert f"mindin login {served.url}" in instructions


@pytest.mark.anyio
async def test_serve_over_stdio_proxies_the_server(remote: Served, tmp_path: Path):
    environment = {
        **os.environ,
        "VAULT_SERVER_URL": remote.url,
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "VAULT_CACHE_DIR": str(tmp_path / "client-cache"),
    }
    launched = StdioServerParameters(command=sys.executable, args=["-m", "mindin.cli"], env=environment)
    async with Client(launched) as client:
        found = await client.call_tool("search", {"query": "Kontrollplanet"})
    assert "Areas/orchard.md" in "".join(block.text for block in found.content)


def test_a_refused_key_ends_a_shell_command_with_one_line(served: Served, monkeypatch, capsys):
    credentials.save(served.url, {"key": FAKE_KEY, "scope": "vault:read", "name": "laptop"})
    monkeypatch.setenv("VAULT_SERVER_URL", served.url)
    assert main(["search", "orchard"]) == 1
    error = capsys.readouterr().err
    assert error.startswith(f"mindin: {served.url} refused the stored key (401)")
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


def without_stamps(snapshot: dict) -> dict:
    kept = {key: value for key, value in snapshot.items() if key not in ("etag", "synced_at")}
    kept["notes"] = [{key: value for key, value in row.items() if key != "mtime"} for row in snapshot["notes"]]
    return kept


def test_the_snapshot_through_the_server_equals_the_local_vault(
    remote: Served, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    run_json(capsys, "task", "claim", "Projects/orchard-fresh.md", "--machine", "devbox", "--task", "t1")
    claimed = remote.read("Projects/orchard-fresh.md")
    (vault_dir / "Projects/orchard-fresh.md").write_text(claimed, encoding="utf-8")
    through_server = run_json(capsys, "snapshot", "--json")
    assert run_json(capsys, "snapshot", "--json", "--since", through_server["etag"]) == {
        "unchanged": True,
        "etag": through_server["etag"],
    }
    monkeypatch.delenv("VAULT_SERVER_URL")
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "local-cache"))
    local = run_json(capsys, "snapshot", "--json")
    assert without_stamps(through_server) == without_stamps(local)
    by_key = {row["key"]: row for row in through_server["notes"]}
    assert by_key["Projects/orchard-fresh.md"]["claim"]["machine"] == "devbox"
    assert through_server["logs"]["Log/orchard-log.md"][0]["links"] == ["orchard"]


def test_read_prints_a_note_through_the_server_as_the_local_vault_does(
    remote: Served, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    key = "Projects/orchard-fresh.md"
    assert main(["read", key]) == 0
    through_server = capsys.readouterr().out
    assert main(["read", key, "--body"]) == 0
    body = capsys.readouterr().out
    assert main(["read", "Projects/nowhere.md"]) == 1
    missing = capsys.readouterr()
    assert missing.out == ""
    assert missing.err.strip() == "mindin: Error executing tool read_file: Projects/nowhere.md"
    monkeypatch.delenv("VAULT_SERVER_URL")
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "local-cache"))
    assert main(["read", key]) == 0
    local = capsys.readouterr().out
    assert through_server == local == (vault_dir / key).read_text(encoding="utf-8")
    assert not local.startswith("etag:")
    assert body == parse(local)[1] and body.lstrip().startswith("Bokningswidget ska renderas")
    assert main(["read", "Projects/nowhere.md"]) == 1
    assert capsys.readouterr().err.strip() == "mindin: no such note: Projects/nowhere.md"
    assert main(["read", "../outside.md"]) == 1
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


WORKSHOP_NOTE = "---\ntitle: lathe\ndate: 2020-01-01\nupdated: 2020-01-01\ntags: [workshop]\nstatus: active\n---\n\n"


def workshop_vault(served: Served) -> str:
    served.store.create_vault(served.account_id(), "Workshop")
    served.write("Areas/lathe.md", WORKSHOP_NOTE + "Oil the bed.\n", vault="workshop")
    credentials.save(served.url, {"key": served.key(vault="workshop"), "scope": "vault:read vault:write"}, "workshop")
    return "workshop"


def test_credentials_keep_a_key_per_vault_and_read_an_older_single_key(tmp_path: Path):
    url = "https://vault.example.test"
    credentials.save(url, {"key": "nvm_legacy", "name": "laptop"})
    assert credentials.stored(url)["key"] == "nvm_legacy", "a key saved before vaults is still this machine's key"
    credentials.save(url, {"key": "nvm_orchard", "name": "laptop"}, "orchard")
    credentials.save(url, {"key": "nvm_workshop", "name": "laptop"}, "workshop")
    state = credentials.server_state(url)
    assert state["default"] == "orchard", "the first vault signed in to becomes the default"
    assert credentials.stored(url, str(tmp_path))["key"] == "nvm_orchard"
    repo = tmp_path / "repos" / "lathe"
    (repo / "src").mkdir(parents=True)
    credentials.use(url, "workshop", repo)
    assert credentials.stored(url, str(repo / "src"))["key"] == "nvm_workshop"
    assert credentials.stored(url, str(tmp_path / "repos"))["key"] == "nvm_orchard"
    credentials.use(url, "orchard", repo / "src")
    assert credentials.vault_for(url, str(repo / "src")) == "orchard", "the deepest folder wins"
    assert credentials.drop_dir(url, repo / "src")
    assert credentials.vault_for(url, str(repo / "src")) == "workshop"
    assert credentials.forget(url, "workshop")
    assert credentials.chosen(url, str(repo)) == ("workshop", None)


def test_a_login_for_a_named_vault_asks_the_server_for_it(tmp_path: Path):
    server = FakeDeviceServer(
        [(200, {"access_token": FAKE_KEY, "scope": "vault:read", "key_name": "devbox", "vault": "workshop"})]
    )
    with httpx2.Client(transport=httpx2.MockTransport(server.handle)) as http:
        entry = login.login(
            "https://vault.example.test",
            name="devbox",
            out=io.StringIO(),
            http=http,
            sleep=lambda _: None,
            open_browser=False,
            vault="workshop",
        )
    assert server.started["vault"] == "workshop"
    assert entry["vault"] == "workshop"
    assert credentials.server_state("https://vault.example.test")["vaults"]["workshop"]["key"] == FAKE_KEY


def test_the_hooks_and_commands_pick_the_vault_of_the_folder(
    remote: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    credentials.save(remote.url, {"key": remote.key(), "scope": "vault:read vault:write"}, "notes")
    workshop_vault(remote)
    repo = make_repo(tmp_path / "repos", "lathe")
    other = make_repo(tmp_path / "repos", "frame")
    remote.write("Areas/lathe.md", system_note(repo).replace("frame", "lathe-system"), vault="workshop")
    assert main(["vault", "use", "workshop", "--dir", str(repo)]) == 0
    capsys.readouterr()
    in_repo = hook_output(monkeypatch, capsys, repo)
    elsewhere = hook_output(monkeypatch, capsys, other)
    assert "Areas/lathe.md" in in_repo and "Kontrollplanet" not in in_repo
    assert "Areas/lathe.md" not in elsewhere
    listed = run_json(capsys, "vault", "list")
    assert listed["default"] == "notes" and listed["dirs"] == {str(repo.resolve()): "workshop"}
    assert sorted(listed["vaults"]) == ["notes", "workshop"]
    opened = run_json(capsys, "task", "open", "--name", "oil-the-lathe", "--cwd", str(repo))
    assert remote.read(opened["path"], vault="workshop")
    with pytest.raises(NotFound):
        remote.read(opened["path"])


def test_a_folder_mapped_to_a_vault_without_a_key_says_how_to_sign_in(
    remote: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    repo = make_repo(tmp_path / "repos", "lathe")
    assert main(["vault", "use", "workshop", "--dir", str(repo)]) == 0
    assert "login" in capsys.readouterr().out
    monkeypatch.chdir(repo)
    assert main(["whoami"]) == 1
    assert f"mindin login {remote.url} --vault workshop" in capsys.readouterr().err


@pytest.mark.anyio
async def test_the_proxy_serves_the_vault_of_its_folder(remote: Served, tmp_path: Path, monkeypatch):
    workshop_vault(remote)
    repo = make_repo(tmp_path / "repos", "lathe")
    credentials.use(remote.url, "workshop", repo)
    monkeypatch.chdir(repo)
    upstream = Upstream(remote.url)
    async with anyio.create_task_group() as group:
        group.start_soon(upstream.supervise)
        await upstream.ready.wait()
        found = await upstream.use(lambda server: server.call("whoami"))
        group.cancel_scope.cancel()
    assert json.loads(found)["vault"] == "workshop"


def human_outputs(capsys, repo: Path) -> list[str]:
    outputs = []
    for argv in (["search", "kärnan"], ["backlog", "--paths"], ["lint"], ["read", "Areas/orchard.md"]):
        assert main(argv) == 0, capsys.readouterr().err
        outputs.append(capsys.readouterr().out)
    assert main(["context", "--path", str(repo)]) == 0
    return [*outputs, capsys.readouterr().out]


def test_the_terminal_output_through_the_server_equals_the_local_vault(
    remote: Served, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    from test_cli_output import in_a_terminal

    repo = make_repo(tmp_path / "repos", "frame")
    on_both(remote, vault_dir, "Areas/frame.md", system_note(repo))
    in_a_terminal(monkeypatch)
    through_server = human_outputs(capsys, repo)
    monkeypatch.delenv("VAULT_SERVER_URL")
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "local-cache"))
    assert through_server == human_outputs(capsys, repo)
    assert through_server[0].startswith("4 notes · ")
    assert through_server[2].startswith("15 findings in 9 kinds")
    assert "1 commit since the note" in through_server[4]


def test_a_server_without_the_view_answers_the_plain_form(
    served: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    from test_cli_output import SEARCH_KARNAN, in_a_terminal

    credentials.save(served.url, {"key": served.key(purpose="api"), "scope": "vault:read vault:write"})
    monkeypatch.setenv("VAULT_SERVER_URL", served.url)
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "client-cache"))
    in_a_terminal(monkeypatch)
    assert main(["search", "kärnan"]) == 0
    assert capsys.readouterr().out == SEARCH_KARNAN
