from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "mindin"
EVERYWHERE = re.compile(
    r"billing|handoff|/v1/admin|--plans\b|VAULT_PLAN|\bplan_(status|until|set_at|kind)\b|\badmin[ _-](token|api)\b"
    r"|subscri(be|ption)|stripe|\bpaid\b|\binvited_at\b|(?<!actions)/checkout"
    r"|vault[-_]service|pricing|\bpaying\b|premium|\btiers?\b|customer|\.gronare\.(com|se)\b|registry\.gronare"
    r"|homelab|home-vault|refund|free[ -]trial|trial[ -](plans?|periods?|ends?|ended)\b"
    r"|VAULT_STORAGE_(PER_ACCOUNT|PER_FILE|TYPES)|VAULT_TURNSTILE|turnstile|VAULT_EMAIL_DOMAIN_BLOCKLIST"
    r"|VAULT_REGISTRATIONS_PER_DAY",
    re.IGNORECASE,
)
SHIPPED = re.compile(r'"checkout"')
IN_AUTH = re.compile(r"\bcheckout\b|\bportal\b|\.plans\b|\bplan_\w+|>plan<", re.IGNORECASE)
IN_PROSE = re.compile(
    r"several servers|multiple servers|other servers|another server|\breplicas\b|high[ -]availability|\bHA\b"
    r"|LISTEN/NOTIFY|postgres",
    re.IGNORECASE,
)


def files_under(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*") if path.is_file() and "__pycache__" not in path.parts)


def prose() -> list[Path]:
    return [*sorted((ROOT / "docs").rglob("*.md")), ROOT / "README.md", ROOT / "CHANGES.md"]


def scanned() -> list[Path]:
    tests = [path for path in files_under(ROOT / "tests") if path != Path(__file__).resolve()]
    top = [ROOT / name for name in ("README.md", "CHANGES.md", "server.json", "pyproject.toml", "Dockerfile")]
    folders = (PACKAGE, *(ROOT / name for name in ("docs", "deploy", "scripts", ".github", "shim", "plugin")))
    return [*(path for folder in folders for path in files_under(folder)), *tests, *top]


@pytest.mark.parametrize("path", scanned(), ids=lambda path: str(path.relative_to(ROOT)))
def test_the_public_package_keeps_to_the_vault(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    found = [match.group(0) for match in EVERYWHERE.finditer(text)]
    if ROOT / "tests" not in path.parents:
        found += [match.group(0) for match in SHIPPED.finditer(text)]
    if PACKAGE / "auth" in path.parents:
        found += [match.group(0) for match in IN_AUTH.finditer(text)]
    if path in prose():
        found += [match.group(0) for match in IN_PROSE.finditer(text)]
    assert not found, f"{path.relative_to(ROOT)}: {sorted(set(found))}"


def test_the_scan_reaches_every_tracked_folder():
    names = {path.relative_to(ROOT).parts[0] for path in scanned()}
    folders = {"mindin", "docs", "deploy", "scripts", ".github", "shim", "plugin", "tests"}
    assert folders | {"README.md", "CHANGES.md"} <= names


def test_the_guard_catches_what_it_names():
    assert EVERYWHERE.search("pip install vault-service") and EVERYWHERE.search("registry.gronare.com/x")
    assert EVERYWHERE.search("one refund per person") and EVERYWHERE.search("your trial ends in 3 days")
    assert not EVERYWHERE.search("http://localhost for a trial on your own machine")
    assert IN_PROSE.search("run several servers behind one address") and not IN_PROSE.search("one replica")
    assert EVERYWHERE.search("VAULT_STORAGE_PER_FILE=5MB") and EVERYWHERE.search("VAULT_REGISTRATIONS_PER_DAY")
    assert EVERYWHERE.search("cf-turnstile-response") and not EVERYWHERE.search("VAULT_STORAGE_DIR")
    assert not EVERYWHERE.search("ghcr.io/gronare/mindin") and not EVERYWHERE.search("actions/checkout@v4")
