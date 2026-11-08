from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

SKIPPED_FOLDERS = frozenset({".git", "__MACOSX", ".trash"})
SKIPPED_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def skipped_as(key: str) -> str | None:
    """The folder or file a move leaves behind by default, as listed to the person, or None to carry the key."""
    parts = key.rstrip("/").split("/")
    for index, part in enumerate(parts[:-1]):
        if part in SKIPPED_FOLDERS:
            return "/".join(parts[: index + 1]) + "/"
    if parts[-1] in SKIPPED_FOLDERS and key.endswith("/"):
        return "/".join(parts) + "/"
    name = parts[-1]
    return key if name in SKIPPED_FILES or name.startswith("._") else None


def skipped_lines(keys: Iterable[str]) -> list[str]:
    counted = Counter(found for key in keys if (found := skipped_as(key)) is not None)
    return [f"{found} ({count} files)" if found.endswith("/") else found for found, count in sorted(counted.items())]


def count_text(count: int) -> str:
    return f"{count:,}".replace(",", " ")
