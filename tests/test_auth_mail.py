from __future__ import annotations

from types import SimpleNamespace

from login_support import FakeSmtp, branded, hrefs, outside_links

from mindin.auth import mail
from mindin.auth.login_email import Mailer

SERVER = "https://vault.example.org/"
LINK = "https://vault.example.org/signin/email/confirm?token=abc-123&x=1"
PNG = b"\x89PNG\r\n\x1a\n"


def sent(found: mail.Mail) -> FakeSmtp:
    smtp = FakeSmtp()
    config = SimpleNamespace(
        sender="vault@example.test", host="smtp.example.test", port=25, security="none", username=None, password=None
    )
    Mailer(config, smtp.factory).send("ann@example.com", *found, mail.images())
    return smtp


def test_the_sign_in_email_is_text_and_html_with_the_mark_inline():
    message = sent(mail.sign_in(SERVER, LINK)).messages[0]
    parts = branded(message)
    assert message["Subject"] == "Sign in to mindin"
    assert parts.image["Content-ID"] == f"<{mail.MARK_CID}>" and parts.image.get_content().startswith(PNG)
    assert parts.image.get_content_disposition() == "inline"
    assert f'src="cid:{mail.MARK_CID}"' in parts.html


def test_the_link_stands_alone_on_its_own_line_in_the_text():
    text = branded(sent(mail.sign_in(SERVER, LINK)).messages[0]).text
    assert f"\n\n{LINK}\n\n" in text
    assert "vault.example.org with this address" in text and "https://vault.example.org " not in text


def test_the_button_is_the_only_link_besides_the_fallback_address():
    for found in (mail.sign_in(SERVER, LINK), mail.invitation(SERVER, LINK, "ann@example.com", "Orchard")):
        assert hrefs(found.html) == [LINK, LINK]
        assert found.html.count(">Sign in</a>") + found.html.count(">Join the vault</a>") == 1


def test_the_host_is_text_a_mail_client_does_not_turn_into_a_link():
    for found in (mail.sign_in(SERVER, LINK), mail.invitation(SERVER, LINK, "ann@example.com", "Orchard")):
        rest = outside_links(found.html)
        assert "vault&#8203;.example&#8203;.org" in rest
        assert "vault.example.org" not in rest and "mindin.dev" not in rest


def test_the_invitation_names_the_owner_and_the_vault():
    found = mail.invitation(SERVER, LINK, "ann@example.com", "Orchard club")
    assert found.subject == "ann@example.com invites you to Orchard club"
    assert "Join the shared vault" in found.html and "Orchard club</span>" in found.html
    assert "ann@example.com invites you to a shared vault on vault.example.org." in found.text
    assert f"\n\n{LINK}\n\n" in found.text


def test_names_from_users_are_escaped_in_the_html():
    found = mail.invitation(SERVER, LINK, "<b>ann</b>@example.com", 'Orchard & <script>"x"</script>')
    assert "<script>" not in found.html and "<b>ann</b>" not in found.html
    assert "Orchard &amp; &lt;script&gt;" in found.html and "&lt;b&gt;ann&lt;/b&gt;@example.com" in found.html


def test_a_port_stays_with_the_host():
    assert mail.host_of("https://vault.example.org:8443/prefix") == "vault.example.org:8443"
    assert mail.host_of(SERVER) == "vault.example.org"
