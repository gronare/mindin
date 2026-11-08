from __future__ import annotations

from functools import cache
from html import escape
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

MARK_CID = "mark@mindin"
SANS = '"Instrument Sans", -apple-system, "Segoe UI", Helvetica, Arial, sans-serif'
MONO = '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
FOOTER = "mindin · memory for coding agents · mindin.dev"

STYLE = """
body { margin: 0; padding: 0; }
a[x-apple-data-detectors] { color: inherit !important; text-decoration: none !important; }
@media (max-width: 480px) {
  .ground-cell { padding: 20px 12px 18px !important; }
  .card { padding: 22px 20px 18px !important; }
}
@media (prefers-color-scheme: dark) {
  .ground, .ground-cell, body { background: #131518 !important; }
  .card { background: #1D2126 !important; border-color: #2C3036 !important; }
  .ink { color: #ECEAE5 !important; }
  .ink2 { color: #B9BCC2 !important; }
  .ink3 { color: #8D9299 !important; }
  .accent { color: #5FD6C2 !important; }
  .host { color: #ECEAE5 !important; background: #15181D !important; border-color: #2C3036 !important; }
  .rule { border-color: #2C3036 !important; }
  .button-cell { background: #3FC2AE !important; }
  .button { color: #131518 !important; }
}
"""

CSS = {
    "ground": "background:#F1EEE6",
    "ground_cell": "padding:28px 20px 22px;background:#F1EEE6",
    "column": f"max-width:480px;font-family:{SANS}",
    "mark": "display:block;border:0;outline:none",
    "wordmark": (
        f"vertical-align:middle;padding-left:6px;font-family:{SANS};font-size:24px;line-height:1;"
        "letter-spacing:-0.03em;font-weight:300;color:#17191C"
    ),
    "card": "background:#FFFFFF;border:1px solid #DDD8CC;border-radius:14px;padding:28px 28px 22px",
    "heading": (
        f"margin:0 0 12px;font-family:{SANS};font-size:24px;line-height:1.15;letter-spacing:-0.025em;"
        "font-weight:600;color:#17191C"
    ),
    "accent": "font-weight:500;color:#09695E",
    "strong": "color:#17191C",
    "lead": "margin:0 0 14px;font-size:15.5px;line-height:1.55;color:#4A4E55",
    "host": (
        f"font-family:{MONO};font-size:13.5px;color:#17191C;background:#F6F4EE;border:1px solid #DDD8CC;"
        "border-radius:6px;padding:1px 6px;white-space:nowrap"
    ),
    "button_cell": "background:#0B7D6F;border-radius:10px",
    "button": (
        f"display:inline-block;padding:12px 26px;font-family:{SANS};font-size:16px;line-height:1.2;font-weight:600;"
        "color:#FFFFFF;text-decoration:none;border-radius:10px"
    ),
    "small": "font-size:13.5px;line-height:1.55;color:#676B73",
    "rule": "border-top:1px solid #DDD8CC;margin-top:18px;padding-top:14px",
    "fallback": f"margin:0 0 14px;font-family:{MONO};font-size:12px;line-height:1.5;word-break:break-all",
    "fallback_link": "color:#676B73;text-decoration:none",
    "footer": "padding:16px 0 0;font-size:12.5px;line-height:1.5;color:#676B73",
    "footer_site": "color:#4A4E55",
    "preheader": "display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;mso-hide:all",
}
TABLE = 'role="presentation" cellpadding="0" cellspacing="0" border="0"'


class Mail(NamedTuple):
    subject: str
    text: str
    html: str


@cache
def images() -> dict[str, bytes]:
    return {MARK_CID: (Path(__file__).parent / "mail-mark.png").read_bytes()}


def host_of(public_url: str) -> str:
    parts = urlsplit(public_url)
    return f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)


def sign_in(public_url: str, link: str) -> Mail:
    host = host_of(public_url)
    text = (
        "Sign in to your vault\n\n"
        f"Someone asked to sign in to the vault server {host} with this address. Open this link to sign in:\n\n"
        f"{link}\n\n"
        "The link works once, for 15 minutes.\n\n"
        "Did not ask for this? Ignore the email. Nobody can sign in without the link.\n\n"
        f"{FOOTER}\n"
    )
    html = document(
        "Sign in to mindin",
        preheader="Your link works once, for 15 minutes.",
        heading="Sign in to your vault",
        lead=f"Someone asked to sign in to the vault server {host_code(host)} with this address.",
        button="Sign in",
        link=link,
        note="The button works once, for 15 minutes.",
        ignore="Did not ask for this? Ignore the email. Nobody can sign in without the link.",
    )
    return Mail("Sign in to mindin", text, html)


def invitation(public_url: str, link: str, owner: str, vault: str) -> Mail:
    host = host_of(public_url)
    text = (
        f"Join the shared vault {vault}\n\n"
        f"{owner} invites you to a shared vault on {host}. You get your own account and your own keys. "
        "Open this link to join the vault:\n\n"
        f"{link}\n\n"
        f"The link works once, for seven days. After that, sign in on {host} with this address to join.\n\n"
        "Not expecting this? Ignore the email. Nothing happens without the link.\n\n"
        f"{FOOTER}\n"
    )
    html = document(
        f"{owner} invites you to {vault}",
        preheader="Join the shared vault. The invitation works for seven days.",
        heading=f'Join the shared vault <span class="accent" {style("accent")}>{escape(vault)}</span>',
        lead=(
            f'<b class="ink" {style("strong")}>{escape(owner)}</b> invites you to a shared vault on '
            f"{host_code(host)}. You get your own account and your own keys."
        ),
        button="Join the vault",
        link=link,
        note=(
            f"The button works once, for seven days. After that, sign in on {host_code(host)} "
            "with this address to join."
        ),
        ignore="Not expecting this? Ignore the email. Nothing happens without the link.",
    )
    return Mail(f"{owner} invites you to {vault}", text, html)


def branded(subject: str, heading: str, lead: str, button: str, link: str, note: str, ignore: str = "") -> Mail:
    """A mail in the sign-in mails' look, from plain text: one heading, one paragraph, one button."""
    parts = (heading, lead, f"{button}: {link}", note, ignore, FOOTER)
    text = "\n\n".join(part for part in parts if part) + "\n"
    html = document(
        subject,
        preheader=note,
        heading=escape(heading),
        lead=escape(lead),
        button=button,
        link=link,
        note=escape(note),
        ignore=escape(ignore),
    )
    return Mail(subject, text, html)


def unlinked(text: str) -> str:
    return escape(text).replace(".", "&#8203;.")


def style(name: str) -> str:
    return f'style="{escape(CSS[name])}"'


def host_code(host: str) -> str:
    return f'<span class="host" {style("host")}>{unlinked(host)}</span>'


def small(content: str, bottom: int = 10) -> str:
    return f'<p class="ink3" style="margin:0 0 {bottom}px;{CSS["small"]}">{content}</p>'


def document(
    title: str, *, preheader: str, heading: str, lead: str, button: str, link: str, note: str, ignore: str
) -> str:
    href = escape(link)
    padding = "&#847;&zwnj;&nbsp;" * 60
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<meta name="format-detection" content="telephone=no, date=no, address=no, email=no, url=no">
<title>{escape(title)}</title>
<style>{STYLE}</style>
</head>
<body class="ground" style="margin:0;padding:0;{CSS["ground"]}">
<div {style("preheader")}>{escape(preheader)}{padding}</div>
<table {TABLE} class="ground" width="100%" bgcolor="#F1EEE6" {style("ground")}>
<tr><td class="ground-cell" align="center" {style("ground_cell")}>
<table {TABLE} width="100%" {style("column")}>
<tr><td style="padding:0 0 16px">
<table {TABLE}><tr>
<td style="vertical-align:middle"><img src="cid:{MARK_CID}" width="48" height="40" alt="" {style("mark")}></td>
<td class="ink" {style("wordmark")}><span style="font-weight:600">mind</span>in</td>
</tr></table>
</td></tr>
<tr><td class="card" bgcolor="#FFFFFF" {style("card")}>
<h1 class="ink" {style("heading")}>{heading}</h1>
<p class="ink2" {style("lead")}>{lead}</p>
<table {TABLE} style="margin:20px 0 10px"><tr>
<td class="button-cell" bgcolor="#0B7D6F" {style("button_cell")}>
<a class="button" href="{href}" {style("button")}>{escape(button)}</a>
</td></tr></table>
{small(note)}
<div class="rule" {style("rule")}>
{small("If the button does not open, paste this address into your browser:")}
<p {style("fallback")}><a class="ink3" href="{href}" {style("fallback_link")}>{href}</a></p>
</div>
{small(ignore, 0)}
</td></tr>
<tr><td class="ink3" align="center" {style("footer")}>
mindin · memory for coding agents · <span class="ink2" {style("footer_site")}>{unlinked("mindin.dev")}</span>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
"""
