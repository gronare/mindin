from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from hosted_support import NOTE_BODY, Hosted, call_tool, csrf_of, hosted, text_of

from notes_vault_mcp import provision
from notes_vault_mcp.backends import SCHEMA_KEY
from notes_vault_mcp.vault import SubjectVaults


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


def basic(secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"ann@example.com:{secret}".encode()).decode()}


@pytest.mark.anyio
async def test_the_first_visit_asks_for_a_layout_and_writes_the_one_picked(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    async with http:
        first = await http.get("/account")
        assert "Pick a layout" in first.text
        offered = re.findall(r'name="preset" value="([^"]+)"', first.text)
        assert offered == ["para", "simple", "journal", "custom"]
        forged = await http.post("/account/layout", data={"csrf": "wrong", "preset": "para"})
        unknown = await http.post("/account/layout", data={"csrf": csrf_of(first.text), "preset": "nope"})
        picked = await http.post("/account/layout", data={"csrf": csrf_of(first.text), "preset": "para"})
        again = await http.get("/account")
    assert forged.status_code == 403
    assert unknown.status_code == 400 and "Pick one of the layouts." in unknown.text
    assert picked.status_code == 200 and "Your vault now uses PARA." in picked.text
    assert "Your vault uses PARA." in again.text and "No note is moved, renamed or deleted" in again.text
    vault = vaults.backend_factory(site.sub_of("ann@example.com"))[0]
    assert provision.current_preset(vault) == "para"
    assert "Projects.base" in {entry.key for entry in vault.list()}
    assert "`Resources/Logs/`" in vault.get("Welcome.md")[0]


@pytest.mark.anyio
async def test_switching_layout_keeps_every_note_and_the_tools_follow(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    key = site.key_for("ann@example.com")
    async with http:
        written = await call_tool(http, key, "write_file", {"path": "Areas/secret.md", "content": NOTE_BODY})
        assert not written.get("isError"), text_of(written)
        page = await http.get("/account")
        assert "Your vault uses Built-in." in page.text
        switched = await http.post("/account/layout", data={"csrf": csrf_of(page.text), "preset": "custom"})
        refused = await call_tool(http, key, "backlog_add", {"title": "x", "area": "demo", "line": "y"})
        found = await call_tool(http, key, "search", {"query": "Hemligt"})
    assert "No note was moved or deleted." in switched.text
    assert refused.get("isError") and "layout (custom) has no task folder" in text_of(refused)
    assert "Areas/secret.md" in text_of(found)
    vault = vaults.backend_factory(site.sub_of("ann@example.com"))[0]
    assert vault.get("Areas/secret.md")[0].startswith("---\ntitle: Hemligt")
    assert provision.preset_of(vault.get(provision.PREVIOUS_SCHEMA_KEY)[0]) == "default"


@pytest.mark.anyio
async def test_a_schema_broken_over_webdav_is_named_on_the_account_page(site: Hosted):
    http = await site.member("ann@example.com", sub="sub-ann")
    key = site.key_for("ann@example.com")
    async with http:
        page = await http.get("/account")
        await http.post("/account/layout", data={"csrf": csrf_of(page.text), "preset": "simple"})
        put = await http.put(
            "/dav/.vault/schema.yml", content=b"preset: simple\nfolders:\n  Inbox: { kind: todo }\n", headers=basic(key)
        )
        broken = await http.get("/account")
        context = await call_tool(http, key, "context", {"repo": "demo"})
    assert put.status_code in (201, 204), put.text
    assert "the vault runs on the simple layout until it is fixed" in broken.text
    assert "folders.Inbox.kind &#x27;todo&#x27; is not one of" in broken.text
    assert "folders.Inbox.kind 'todo' is not one of" in text_of(context)


@pytest.mark.anyio
async def test_a_vault_on_a_preset_no_longer_shipped_is_named_and_can_switch(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    async with http:
        await http.get("/account")
        vault = vaults.backend_factory(site.sub_of("ann@example.com"))[0]
        vault.put(SCHEMA_KEY, "preset: lyt\nfolders:\n  Atlas: { kind: reference }\n")
        page = await http.get("/account")
        switched = await http.post("/account/layout", data={"csrf": csrf_of(page.text), "preset": "para"})
    assert "names &#x27;lyt&#x27;, a layout this version does not ship; it is read as written" in page.text
    assert "preset &#x27;lyt&#x27; is not one this version ships" in page.text
    assert "Your vault now uses PARA. No note was moved or deleted." in switched.text
    assert provision.current_preset(vault) == "para"
    assert vault.get(provision.PREVIOUS_SCHEMA_KEY)[0].startswith("preset: lyt")
