from __future__ import annotations

import re
from dataclasses import dataclass

from mindin.backends import NotFound, VersionConflict
from mindin.index import Note
from mindin.notes import BACKLOG_STATUS, OPEN_STATUSES, ValidationError, close, fetch
from mindin.vault import Vault

SWEPT_STATUSES = (*OPEN_STATUSES, BACKLOG_STATUS)
LINE_DATE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2})\]")
SHA_RE = re.compile(r"(?<![0-9A-Za-z])(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![0-9A-Za-z])")
BRANCH_SOURCE_RE = re.compile(r"^branch:?\s+(\S+)$", re.IGNORECASE)
OPEN_CHECKBOX_RE = re.compile(r"^\s*[-*] \[ \]", re.MULTILINE)
NOT_LANDED_RE = re.compile(
    r"unpushed|not pushed|not merged|kept local|opushad|opushat|ej pushad|ej pushat|inte pushad|inte pushat"
    r"|lokalt|lokal main|pushas inte",
    re.IGNORECASE,
)
AREA_FIELD_RE = re.compile(r"\s*\|\s*\[\[[^\]]*\]\]\s*$")
BRANCH_FIELD_RE = re.compile(r"\|\s*branch:\s*(\S+)\s*\|")
DEFAULT_BRANCHES = frozenset({"main", "master", "trunk", "develop"})


@dataclass(frozen=True)
class LogLine:
    key: str
    day: str
    text: str


@dataclass(frozen=True)
class Verdict:
    key: str
    evidence: str
    sure: bool
    reason: str = ""


def _log_pattern(vault: Vault) -> re.Pattern[str] | None:
    folder = vault.schema.log_folder
    if folder is None:
        return None
    name = re.escape(vault.schema.log_file_format).replace(re.escape("{repo}"), ".+")
    return re.compile(rf"^{re.escape(folder)}/{name}$")


def log_lines(vault: Vault, texts: dict[str, str]) -> list[LogLine]:
    pattern = _log_pattern(vault)
    if pattern is None:
        return []
    lines = []
    for key, text in sorted(texts.items()):
        if not pattern.match(key):
            continue
        for line in text.splitlines():
            day = LINE_DATE_RE.search(line)
            if day:
                lines.append(LogLine(key, day.group(1), line.strip()))
    return lines


def _branch(note: Note) -> str | None:
    found = BRANCH_SOURCE_RE.match(note.source.strip())
    branch = found.group(1) if found else None
    return branch if branch and branch.lower() not in DEFAULT_BRANCHES else None


def _link(note: Note) -> str:
    return rf"\[\[{re.escape(note.stem)}(\|[^\]]*)?\]\]"


def _names_note(note: Note, line: str) -> bool:
    line = AREA_FIELD_RE.sub("", line)
    if re.search(_link(note), line, re.IGNORECASE):
        return True
    stem = re.escape(note.stem)
    return "-" in note.stem and re.search(rf"(?<![\w/-]){stem}(?![\w/-])", line, re.IGNORECASE) is not None


def _entry(line: str) -> str:
    return LINE_DATE_RE.split(line, maxsplit=1)[-1].strip()


def _marks_done(note: Note, line: str) -> bool:
    return re.match(rf"done {_link(note)}(?![^\s:;,.|])", _entry(line), re.IGNORECASE) is not None


def _branch_field(line: str) -> str | None:
    found = BRANCH_FIELD_RE.search(line)
    return found.group(1) if found and found.group(1) != "-" else None


def _pushed(branch: str, line: str) -> bool:
    return _entry(line).startswith(f"{branch}:") and _branch_field(line) in (None, branch)


def _on_branch(branch: str, line: str) -> bool:
    return _branch_field(line) == branch


def _shared_shas(body: str, line: str) -> list[str]:
    ours = {sha[:7] for sha in SHA_RE.findall(body)}
    return [sha for sha in SHA_RE.findall(line) if sha[:7] in ours]


def judge(note: Note, body: str, lines: list[LogLine]) -> Verdict | None:
    branch = _branch(note)
    pushed, marked, mentioned, on_branch, by_sha = [], [], [], [], []
    for order, line in enumerate(lines):
        if _marks_done(note, line.text):
            marked.append((line.day, order, line))
        elif branch and _pushed(branch, line.text):
            pushed.append((line.day, order, line))
        elif _names_note(note, line.text):
            mentioned.append((line.day, order, line))
        elif branch and _on_branch(branch, line.text):
            on_branch.append((line.day, order, line))
        elif _shared_shas(body, line.text):
            by_sha.append((line.day, order, line))
    found = pushed + marked or mentioned or on_branch or by_sha
    if not found:
        return None
    newest = max(found)
    last = newest[2]
    evidence = f"{last.key}: {last.text}"
    updated = (note.updated or note.date or "")[:10]
    if not updated:
        return Verdict(note.key, evidence, False, "the note has no updated date to weigh the log line against")
    if last.day < updated:
        return Verdict(note.key, evidence, False, f"the note changed on {updated}, after the log line")
    if found is mentioned:
        return Verdict(note.key, evidence, False, "the log line names the note without marking it done")
    if found is on_branch:
        reason = "the log line names the branch its commits were made on, not a push of it"
        return Verdict(note.key, evidence, False, reason)
    if found is by_sha:
        return Verdict(note.key, evidence, False, "only a commit the note mentions is in the log, not the note itself")
    later = [entry for entry in mentioned if entry > newest]
    if later:
        evidence = f"{max(later)[2].key}: {max(later)[2].text}"
        return Verdict(note.key, evidence, False, "a later log line names the note without marking it done")
    later = [entry for entry in on_branch if entry > newest]
    if later:
        evidence = f"{max(later)[2].key}: {max(later)[2].text}"
        return Verdict(note.key, evidence, False, "a later log line shows work on the branch after the push")
    if NOT_LANDED_RE.search(last.text):
        return Verdict(note.key, evidence, False, "the log line says the work has not landed")
    if OPEN_CHECKBOX_RE.search(body):
        return Verdict(note.key, evidence, False, "the note still has unchecked items")
    return Verdict(note.key, evidence, True)


def verdicts(vault: Vault) -> list[Verdict]:
    task_folders = set(vault.schema.task_folders)
    texts = vault.index.note_texts()
    lines = log_lines(vault, texts)
    found = []
    for note in vault.index.all_notes():
        if not note.valid or note.claim or note.status not in SWEPT_STATUSES:
            continue
        if vault.schema.folder_of(note.key) not in task_folders:
            continue
        text = texts.get(note.key) or fetch(vault, note.key)[0]
        verdict = judge(note, text, lines)
        if verdict is not None:
            found.append(verdict)
    return found


def close_done(vault: Vault) -> tuple[list[Verdict], list[Verdict]]:
    closed, uncertain = [], []
    for verdict in verdicts(vault):
        if not verdict.sure:
            uncertain.append(verdict)
            continue
        try:
            close(vault, verdict.key, evidence=f"lint --close-done found {verdict.evidence}", unclaimed=True)
        except (ValidationError, VersionConflict, NotFound) as refused:
            reason = f"close refused it: {(str(refused) or type(refused).__name__).splitlines()[0]}"
            uncertain.append(Verdict(verdict.key, verdict.evidence, False, reason))
            continue
        closed.append(verdict)
    return closed, uncertain
