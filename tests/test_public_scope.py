from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "notes_vault_mcp"
EVERYWHERE = re.compile(
    r"billing|handoff|/v1/admin|--plans\b|VAULT_PLAN|\bplan_(status|until|set_at|kind)\b|\badmin[ _-](token|api)\b"
    r"|subscri(be|ption)|stripe|\bpaid\b|\binvited_at\b|/checkout|\"checkout\"",
    re.IGNORECASE,
)
IN_AUTH = re.compile(r"\bcheckout\b|\bportal\b|\.plans\b|\bplan_\w+|>plan<", re.IGNORECASE)


def scanned() -> list[Path]:
    files = [path for path in PACKAGE.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    docs = sorted((ROOT / "docs").rglob("*.md"))
    deploy = sorted(path for path in (ROOT / "deploy").rglob("*") if path.is_file())
    top = [ROOT / name for name in ("README.md", "CHANGES.md", "server.json", "pyproject.toml")]
    return [*files, *docs, *deploy, *top]


@pytest.mark.parametrize("path", scanned(), ids=lambda path: str(path.relative_to(ROOT)))
def test_the_public_package_keeps_to_the_vault(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    found = [match.group(0) for match in EVERYWHERE.finditer(text)]
    if PACKAGE / "auth" in path.parents:
        found += [match.group(0) for match in IN_AUTH.finditer(text)]
    assert not found, f"{path.relative_to(ROOT)}: {sorted(set(found))}"
