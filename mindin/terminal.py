from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass

ELLIPSIS = "…"
MIN_WIDTH = 40
STYLES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "grey": "90",
    "orange": "38;5;208",
}
WIKILINK_RE = re.compile(r"\[\[([^\]|#]*)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")


@dataclass(frozen=True)
class Terminal:
    pretty: bool
    color: bool
    width: int

    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not text or not styles:
            return text
        return f"\x1b[{';'.join(STYLES[style] for style in styles)}m{text}\x1b[0m"


def stdout_is_terminal() -> bool:
    return sys.stdout.isatty()


def terminal(plain: bool = False) -> Terminal:
    pretty = not plain and stdout_is_terminal()
    color = pretty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
    width = shutil.get_terminal_size((80, 24)).columns
    return Terminal(pretty=pretty, color=color, width=max(width, MIN_WIDTH))


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def width_of(text: str) -> int:
    return sum(char_width(char) for char in text)


def cut(text: str, width: int) -> str:
    if width_of(text) <= width:
        return text
    if width <= 0:
        return ""
    kept, used = [], 0
    for char in text:
        size = char_width(char)
        if used + size > width - 1:
            break
        kept.append(char)
        used += size
    return "".join(kept).rstrip() + ELLIPSIS


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - width_of(text))


def pad_left(text: str, width: int) -> str:
    return " " * max(0, width - width_of(text)) + text


def unlinked(text: str) -> str:
    return WIKILINK_RE.sub(lambda match: (match.group(2) or match.group(1)).strip(), text)


def link_stem(area: str) -> str:
    return area.strip().strip("[]").split("|")[0].split("#")[0].strip()


def age_words(days: float) -> str:
    whole = int(days)
    if whole < 1:
        return "today"
    if whole == 1:
        return "1 day"
    if whole < 60:
        return f"{whole} days"
    if whole < 730:
        return f"{whole // 30} months"
    return f"{whole // 365} years"


def updated_words(days: float) -> str:
    words = age_words(days)
    return "updated today" if words == "today" else f"updated {words} ago"


def plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else many or one + 's'}"
