from __future__ import annotations

from pathlib import Path

import pytest
from hosted_support import INITIALIZE, hosted, key_for_subject, post_mcp, sse_payload, text_of

from mindin.auth.hosted_keys import Keyring, MasterKey
from mindin.vault import SubjectVaults

LIST_FILES = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_files", "arguments": {}}}
FRONT = "date: 2026-09-01\nupdated: 2026-09-01\ntags: [orchard]\nstatus: active\n"
NOTE = f"---\ntitle: Pears\n{FRONT}---\n\nPick in August.\n"

WRITE_FILE = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {"name": "write_file", "arguments": {"path": "Areas/pears.md", "content": NOTE}},
}


def subject_vaults(tmp_path: Path, name: str) -> SubjectVaults:
    return SubjectVaults(cache=tmp_path / name, prefix="users", dav_url="http://localhost/dav/")


@pytest.mark.anyio
async def test_a_stateless_server_answers_a_call_on_another_server_than_the_one_that_initialized(
    storage: Path, tmp_path: Path
):
    keyring = Keyring(MasterKey.generate())
    first, second = subject_vaults(tmp_path, "cache-a"), subject_vaults(tmp_path, "cache-b")
    try:
        async with (
            hosted(first, tmp_path, keyring=keyring, stateless_http=True) as a,
            hosted(second, tmp_path, keyring=keyring, stateless_http=True) as b,
        ):
            token = key_for_subject(a.config.auth_dir, "ann@example.com", "sub-ann")
            async with a.browser() as on_a, b.browser() as on_b:
                initialized = await post_mcp(on_a, token, INITIALIZE)
                assert initialized.status_code == 200 and "mcp-session-id" not in initialized.headers
                written = await post_mcp(on_a, token, WRITE_FILE)
                assert not sse_payload(written.text)["result"].get("isError"), written.text
                listed = await post_mcp(on_b, token, LIST_FILES)
    finally:
        first.close()
        second.close()
    assert listed.status_code == 200, listed.text
    assert "Areas/pears.md" in text_of(sse_payload(listed.text)["result"])


@pytest.mark.anyio
async def test_by_default_a_session_lives_on_the_server_that_opened_it(storage: Path, tmp_path: Path):
    keyring = Keyring(MasterKey.generate())
    first, second = subject_vaults(tmp_path, "cache-a"), subject_vaults(tmp_path, "cache-b")
    try:
        async with hosted(first, tmp_path, keyring=keyring) as a, hosted(second, tmp_path, keyring=keyring) as b:
            token = key_for_subject(a.config.auth_dir, "ann@example.com", "sub-ann")
            async with a.browser() as on_a, b.browser() as on_b:
                initialized = await post_mcp(on_a, token, INITIALIZE)
                session = initialized.headers["mcp-session-id"]
                elsewhere = await post_mcp(on_b, token, LIST_FILES, session)
    finally:
        first.close()
        second.close()
    assert elsewhere.status_code == 404
