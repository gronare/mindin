from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from hosted_support import NOTE_BODY, Hosted, call_tool, csrf_of, hosted, text_of

from mindin import provision
from mindin.backends import SCHEMA_KEY
from mindin.vault import SubjectVaults


@pytest.fixture
async def site(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as running:
        yield running


def basic(secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"ann@example.com:{secret}".encode()).decode()}


@pytest.mark.anyio
async def test_the_first_vault_asks_for_a_layout_and_writes_the_one_picked(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    async with http:
        first = await http.get("/account/vaults/notes")
        assert "Pick a layout" in first.text and "no layout yet" in first.text
        offered = re.findall(r'name="preset" value="([^"]+)"', first.text)
        assert offered == ["para", "simple", "journal", "custom"]
        forged = await http.post("/account/vaults/notes/layout", data={"csrf": "wrong", "preset": "para"})
        csrf = csrf_of(first.text)
        unknown = await http.post("/account/vaults/notes/layout", data={"csrf": csrf, "preset": "nope"})
        picked = await http.post("/account/vaults/notes/layout", data={"csrf": csrf, "preset": "para"})
        again = await http.get("/account/vaults/notes")
    assert forged.status_code == 403
    assert unknown.status_code == 400 and "Pick one of the layouts." in unknown.text
    assert picked.status_code == 200 and "This vault now uses PARA." in picked.text
    assert "PARA layout" in again.text and "Pick a layout" not in again.text
    assert "This vault uses PARA." in again.text and "No note is moved, renamed or deleted" in again.text
    assert "Switch layout" in again.text
    vault = vaults.backend_factory(site.vault_of("ann@example.com"))[0]
    assert provision.current_preset(vault) == "para"
    assert "Projects.base" in {entry.key for entry in vault.list()}
    assert "`Resources/Logs/`" in vault.get("Welcome.md")[0]


@pytest.mark.anyio
async def test_switching_layout_keeps_every_note_and_the_tools_follow(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    key = site.key_for("ann@example.com")
    async with http:
        page = await http.get("/account/vaults/notes")
        await http.post("/account/vaults/notes/layout", data={"csrf": csrf_of(page.text), "preset": "para"})
        written = await call_tool(http, key, "write_file", {"path": "Areas/secret.md", "content": NOTE_BODY})
        assert not written.get("isError"), text_of(written)
        same = await http.post("/account/vaults/notes/layout", data={"csrf": csrf_of(page.text), "preset": "para"})
        switched = await http.post(
            "/account/vaults/notes/layout", data={"csrf": csrf_of(page.text), "preset": "custom"}
        )
        refused = await call_tool(http, key, "backlog_add", {"title": "x", "area": "demo", "line": "y"})
        found = await call_tool(http, key, "search", {"query": "Hemligt"})
    assert "This vault already uses PARA." in same.text
    assert switched.status_code == 200 and "No note was moved or deleted." in switched.text
    assert refused.get("isError") and "layout (custom) has no task folder" in text_of(refused)
    assert "Areas/secret.md" in text_of(found)
    vault = vaults.backend_factory(site.vault_of("ann@example.com"))[0]
    assert vault.get("Areas/secret.md")[0].startswith("---\ntitle: Hemligt")
    assert provision.preset_of(vault.get(provision.PREVIOUS_SCHEMA_KEY)[0]) == "para"


@pytest.mark.anyio
async def test_a_vault_in_the_built_in_layout_names_it_and_offers_no_switch(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    async with http:
        vaults.for_subject(site.vault_of("ann@example.com"))
        page = await http.get("/account/vaults/notes")
        refused = await http.post("/account/vaults/notes/layout", data={"csrf": csrf_of(page.text), "preset": "para"})
    assert "This vault uses Built-in." in page.text and "built-in layout" in page.text
    assert "Switch layout" not in page.text and 'name="preset"' not in page.text
    assert refused.status_code == 409
    assert provision.current_preset(vaults.backend_factory(site.vault_of("ann@example.com"))[0]) == "default"


@pytest.mark.anyio
async def test_a_new_vault_gets_the_layout_picked_with_its_name(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    async with http:
        form = await http.get("/account/vaults/new")
        csrf = csrf_of(form.text)
        offered = re.findall(r'name="preset" value="([^"]+)"', form.text)
        unnamed = await http.post("/account/vaults", data={"csrf": csrf, "name": "  ", "preset": "simple"})
        made = await http.post("/account/vaults", data={"csrf": csrf, "name": "Orchard Notes", "preset": "simple"})
        page = await http.get(made.headers["location"])
        taken = await http.post("/account/vaults", data={"csrf": csrf, "name": "orchard notes", "preset": "para"})
    assert offered == ["para", "simple", "journal", "custom"]
    assert 'data-slug-from="vault-name"' in form.text and 'data-base="http://localhost/mcp?vault="' in form.text
    assert unnamed.status_code == 400 and "Give the vault a name." in unnamed.text
    assert made.status_code == 303 and made.headers["location"] == "/account/vaults/orchardnotes"
    assert "Simple layout" in page.text and "http://localhost/mcp?vault=orchardnotes" in page.text
    assert taken.status_code == 303 and taken.headers["location"] == "/account/vaults/orchardnotes2"
    backend = vaults.backend_factory(site.vault_of("ann@example.com", "orchardnotes"))[0]
    assert provision.current_preset(backend) == "simple"


@pytest.mark.anyio
async def test_a_schema_broken_over_webdav_is_named_on_the_vault_page(site: Hosted):
    http = await site.member("ann@example.com", sub="sub-ann")
    key = site.key_for("ann@example.com")
    async with http:
        page = await http.get("/account/vaults/notes")
        await http.post("/account/vaults/notes/layout", data={"csrf": csrf_of(page.text), "preset": "simple"})
        put = await http.put(
            "/dav/vault/.vault/schema.yml",
            content=b"preset: simple\nfolders:\n  Inbox: { kind: todo }\n",
            headers=basic(key),
        )
        broken = await http.get("/account/vaults/notes")
        context = await call_tool(http, key, "context", {"repo": "demo"})
    assert put.status_code in (201, 204), put.text
    assert "the vault runs on the simple layout until it is fixed" in broken.text
    assert "folders.Inbox.kind &#x27;todo&#x27; is not one of" in broken.text
    assert "folders.Inbox.kind 'todo' is not one of" in text_of(context)


@pytest.mark.anyio
async def test_a_vault_on_a_preset_no_longer_shipped_is_named_and_can_switch(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    async with http:
        vault = vaults.backend_factory(site.vault_of("ann@example.com"))[0]
        vault.put(SCHEMA_KEY, "preset: lyt\nfolders:\n  Atlas: { kind: reference }\n")
        page = await http.get("/account/vaults/notes")
        switched = await http.post("/account/vaults/notes/layout", data={"csrf": csrf_of(page.text), "preset": "para"})
    assert "lyt layout" in page.text and "Pick a layout" not in page.text
    assert "names &#x27;lyt&#x27;, a layout this version does not ship; it is read as written" in page.text
    assert "preset &#x27;lyt&#x27; is not one this version ships" in page.text
    assert "This vault now uses PARA. No note was moved or deleted." in switched.text
    assert provision.current_preset(vault) == "para"
    assert vault.get(provision.PREVIOUS_SCHEMA_KEY)[0].startswith("preset: lyt")


@pytest.mark.anyio
async def test_the_layout_box_shows_the_folders_and_fields_the_vault_schema_names(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    async with http:
        vaults.for_subject(site.vault_of("ann@example.com"))
        built_in = await http.get("/account/vaults/notes")
        backend = vaults.backend_factory(site.vault_of("ann@example.com"))[0]
        edited = provision.stored(backend, SCHEMA_KEY).replace("Hub of the graph.", "Where the orchard lives.")
        backend.put(SCHEMA_KEY, edited)
        changed = await http.get("/account/vaults/notes")
    assert "<td><code>Areas/</code></td><td>One living note per system or subsystem." in built_in.text
    assert "<td><code>Archive/</code></td><td>History. Searched only on request. Not searched unless" in built_in.text
    assert "<dt>Every note needs</dt><dd>title, date, updated, tags, status</dd>" in built_in.text
    assert "<dt>Also an area in</dt><dd>Projects/, Resources/, Log/</dd>" in built_in.text
    assert '<summary class="btn secondary small">Show .vault/schema.yml</summary>' in built_in.text
    assert '<pre class="diff">version: 1\nlanguage: en' in built_in.text
    assert "Where the orchard lives." in changed.text and "Hub of the graph." not in changed.text


@pytest.mark.anyio
async def test_the_layout_box_names_the_kinds_each_folder_takes_and_the_root(site: Hosted, vaults: SubjectVaults):
    http = await site.member("ann@example.com", sub="sub-ann")
    async with http:
        backend = vaults.backend_factory(site.vault_of("ann@example.com"))[0]
        backend.put(SCHEMA_KEY, "preset: wiki\n")
        wiki = await http.get("/account/vaults/notes")
        backend.put(SCHEMA_KEY, "preset: custom\n")
        custom = await http.get("/account/vaults/notes")
    assert "<th>Kinds</th>" in wiki.text and "<td>the root</td><td>The Bases views." in wiki.text
    assert "How to do something, written once the work behind it is finished.</td><td>howto</td>" in wiki.text
    assert "<dt>Every note needs</dt><dd>title, date, updated, tags, status, team, author</dd>" in wiki.text
    assert "This layout names no folders; a note can go anywhere." in custom.text
    assert "<th>Kinds</th>" not in custom.text and "Switch layout" in custom.text
