from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Registrant:
    method: str
    subject: str
    emails: tuple[str, ...]
    label: str = ""
    invited: bool = False
