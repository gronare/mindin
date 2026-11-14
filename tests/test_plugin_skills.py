from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from mcp.client import Client

from mindin.server import build_server
from mindin.vault import Vault

SKILLS = Path(__file__).resolve().parents[1] / "plugin" / "skills"


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} opens without frontmatter"
    return yaml.safe_load(text.split("---\n", 2)[1])


@pytest.mark.parametrize("folder", sorted(path for path in SKILLS.iterdir() if path.is_dir()), ids=lambda p: p.name)
def test_every_plugin_skill_names_itself_and_has_a_description(folder: Path):
    found = frontmatter(folder / "SKILL.md")
    assert found["name"] == folder.name
    assert 0 < len(found["description"]) <= 1024


@pytest.mark.anyio
async def test_the_team_wiki_skill_calls_only_tools_the_server_has(vault: Vault):
    text = (SKILLS / "team-wiki" / "SKILL.md").read_text(encoding="utf-8")
    async with Client(build_server(vault)) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
    named = set(re.findall(r"`(\w+)(?:\(|`| with)", text)) & {"rules", "search", "read_file", "write_file", "context"}
    assert named == {"rules", "search", "read_file", "write_file"} and named <= tools
    assert "explicit yes" in text and "Projects/" in text and "Incidents/" in text


def test_the_vault_workflow_skill_loads_on_the_stop_hooks_word_and_finds_commits_as_the_hook_does():
    from mindin import hooks

    text = (SKILLS / "vault-workflow" / "SKILL.md").read_text(encoding="utf-8")
    description = frontmatter(SKILLS / "vault-workflow" / "SKILL.md")["description"]
    assert "Stop hook" in description and f"`{hooks.STOP_REASON}`" in description
    assert "--author" not in text and f"--since='{hooks.RECENT}'" in text and "the whole log" in text
    assert "Decisions line" not in text


def test_the_claude_md_snippet_says_what_the_stop_hooks_word_asks_for():
    from mindin import hooks
    from mindin.presets import template

    assert f"**Stop hook `{hooks.STOP_REASON}`**" in template("claude-md-snippet.md")
