from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from hosted_support import Hosted, hosted

from mindin.extensions import Extension, SignInCheck
from mindin.hosted.accounts.store import Account
from mindin.hosted.pages.templates import content_security_policy
from mindin.vault import SubjectVaults


async def ask_for_link(http: httpx2.AsyncClient, email: str, **fields: str) -> httpx2.Response:
    return await http.post("/signin/email", data={"email": email, "next": "/account", **fields})


async def github_sign_in(site: Hosted, http: httpx2.AsyncClient, user_id: int, email: str) -> httpx2.Response:
    started = await http.get("/signin/github", params={"next": "/account"})
    state, code = site.github.approve(started.headers["location"], user_id, email.split("@")[0], [(email, True, True)])
    return await http.get("/signin/github/callback", params={"state": state, "code": code})


class Riddle(Extension):
    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    def sign_in_check(self) -> SignInCheck:
        return SignInCheck(
            html='<input name="riddle" data-check="riddle">',
            sources={"script-src": ("https://check.example.test",), "frame-src": ("https://check.example.test",)},
            passed=self.passed,
        )

    async def passed(self, form, address: str) -> bool:
        self.asked.append((form.get("riddle", ""), address))
        return form.get("riddle") == "person"


@pytest.mark.anyio
async def test_an_extensions_check_guards_the_email_link_and_widens_the_page_policy(
    vaults: SubjectVaults, tmp_path: Path
):
    riddle = Riddle()
    async with hosted(vaults, tmp_path, registration="open", extensions=[riddle]) as site, site.browser() as http:
        page = await http.get("/signin")
        missing = await ask_for_link(http, "new@example.com")
        wrong = await ask_for_link(http, "new@example.com", riddle="robot")
        passed = await ask_for_link(http, "new@example.com", riddle="person")
    assert 'data-check="riddle"' in page.text
    directives = dict(part.strip().split(" ", 1) for part in page.headers["content-security-policy"].split(";"))
    assert directives["script-src"] == "'self' https://check.example.test"
    assert directives["frame-src"] == "https://check.example.test"
    assert missing.status_code == 400 and wrong.status_code == 400 and "did not pass" in wrong.text
    assert passed.status_code == 200
    assert [message["To"] for message in site.smtp.messages] == ["new@example.com"]
    assert [answer for answer, _ in riddle.asked] == ["", "robot", "person"]


@pytest.mark.anyio
async def test_without_a_check_the_sign_in_page_keeps_its_own_policy(vaults: SubjectVaults, tmp_path: Path):
    async with hosted(vaults, tmp_path) as site, site.browser() as http:
        page = await http.get("/signin")
    assert page.headers["content-security-policy"] == content_security_policy()


class Gate(Extension):
    def __init__(self) -> None:
        self.made: list[tuple[str, str, str, bool]] = []

    def admits(self, registrant) -> str:
        return "Not this one." if registrant.subject == "666" or "devil@example.com" in registrant.emails else ""

    def registered(self, connection, account: Account, registrant) -> None:
        connection.execute("UPDATE accounts SET status_source = 'marked' WHERE id = ?", (account.id,))
        self.made.append((account.email, registrant.method, registrant.subject, registrant.invited))


@pytest.mark.anyio
async def test_an_extension_vetoes_a_registration_and_marks_a_new_account(vaults: SubjectVaults, tmp_path: Path):
    gate = Gate()
    async with hosted(vaults, tmp_path, registration="open", extensions=[gate]) as site, site.browser() as http:
        vetoed = await github_sign_in(site, http, 666, "devil@example.com")
        linked = await ask_for_link(http, "devil@example.com")
        made = await github_sign_in(site, http, 7, "new@example.com")
        again = await github_sign_in(site, http, 7, "new@example.com")
    assert vetoed.status_code == 403 and "Not this one." in vetoed.text
    assert linked.status_code == 403 and "Not this one." in linked.text and not site.smtp.messages
    assert made.status_code == 303 and again.status_code == 303
    assert gate.made == [("new@example.com", "github", "7", False)]
    assert site.store.account("new@example.com").status_source == "marked"
