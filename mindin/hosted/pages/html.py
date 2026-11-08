from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import UTC, datetime

NBSP = " "


def esc(text: object) -> str:
    return html.escape(str(text))


def hidden_field(name: str, value: str) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def copy_field(field_id: str, text: str) -> str:
    return (
        f'<div class="copy"><code id="{esc(field_id)}">{esc(text)}</code>'
        f'<button class="btn secondary small" type="button" data-copy="{esc(field_id)}" hidden>Copy</button></div>'
    )


def form_head(csrf: str, action: str, extra: str = "") -> str:
    return f'<form method="post" action="{esc(action)}"{extra}>{hidden_field("csrf", csrf)}'


def button(label: str, classes: str = "btn", aria: str = "") -> str:
    labelled = f' aria-label="{esc(aria)}"' if aria else ""
    return f'<button type="submit" class="{classes}"{labelled}>{esc(label)}</button>'


def notice_box(text: str) -> str:
    return f'<div class="notice"><p>{esc(text)}</p></div>'


def error_box(message: str) -> str:
    return f'<p class="error">{esc(message)}</p>' if message else ""


def muted(markup: str) -> str:
    return f'<p class="small muted">{markup}</p>'


def box(markup: str, classes: str = "box") -> str:
    return f'<div class="{classes}">{markup}</div>'


def row(markup: str) -> str:
    return f'<div class="row">{markup}</div>'


def table(headings: Iterable[str], rows: Iterable[str], classes: str = "tablewrap") -> str:
    head = "".join(f"<th>{esc(heading)}</th>" for heading in headings)
    return f'<div class="{classes}"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def counted(number: int, word: str) -> str:
    return f"{number:,}".replace(",", NBSP) + f" {word}{'' if number == 1 else 's'}"


def size_label(size: int) -> str:
    megabytes = size / 1_000_000
    if size and megabytes < 0.1:
        return "0.1 MB"
    shown = f"{megabytes:.1f}" if megabytes < 10 else f"{megabytes:.0f}"
    return f"{shown.removesuffix('.0')} MB"


def ago(moment: float | None, now: float) -> str:
    if not moment:
        return "never"
    seconds = max(now - moment, 0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 2 * 86400:
        return "yesterday"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)} days ago"
    day = datetime.fromtimestamp(moment, tz=UTC)
    return f"{day.day} {day:%b %Y}"
