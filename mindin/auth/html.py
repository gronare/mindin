from __future__ import annotations

import html
from collections.abc import Iterable


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
