from __future__ import annotations

import re
import textwrap
import time
from datetime import date

from mindin.frontmatter import FrontmatterError, parse
from mindin.hooks import Commit
from mindin.notes import Findings
from mindin.search import DAY_SECONDS, parse_date, partial_hint, translate_hint
from mindin.terminal import (
    Terminal,
    age_words,
    cut,
    link_stem,
    pad,
    pad_left,
    plural,
    unlinked,
    updated_words,
    width_of,
)

DOT = "●"
MEANING = "≈ meaning"
GAP = "  "
BACKLOG_ROWS = 20
COMMITS_SHOWN = 10
LABEL_WIDTH = 12
KIND_COLORS = {
    "system": "blue",
    "task": "cyan",
    "trap": "orange",
    "howto": "green",
    "decision": "magenta",
    "reference": "grey",
    "log": "grey",
}
STATUS_COLORS = {"active": "green", "draft": "yellow", "backlog": "cyan", "complete": "grey", "superseded": "grey"}
PRIORITY_COLORS = {"urgent": "red", "high": "orange", "medium": "yellow", "low": "blue"}
LINT_KINDS = {
    "schema": ("schema problems", "the vault's .vault/schema.yml has errors"),
    "broken_frontmatter": ("broken frontmatter", "the frontmatter does not parse as YAML"),
    "missing_required": ("missing fields", "a field every note needs is absent"),
    "missing_area": ("missing area", "no area link, or one not shaped [[stem]]"),
    "unknown_tags": ("unknown tags", "a tag the schema does not allow"),
    "invalid_field": ("invalid fields", "a field holds a value the schema refuses"),
    "unresolved_links": ("unresolved links", "a [[link]] with no note behind it"),
    "orphans": ("orphans", "no note links here"),
    "stale_active": ("stale open tasks", "open and untouched for more than {stale} days"),
    "done_not_closed": ("done, not closed", "complete or superseded, still outside the archive"),
    "archive_status_mismatch": ("open in the archive", "archived but not marked complete or superseded"),
    "superseded_target_missing": ("missing successors", "superseded by a note that does not exist"),
    "duplicate_stems": ("duplicate names", "two notes share a file name"),
    "parked": ("parked", "moved to the backlog by --park-stale"),
}
ENTRY_RE = re.compile(r"^(?P<head>.+?)(?P<sep>: | -> )(?P<rest>.*)$")
LOG_LINE_RE = re.compile(r"^-?\s*\[(?P<day>\d{4}-\d{2}-\d{2})\]\s*(?P<text>.*)$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
H1_RE = re.compile(r"^ {0,3}#[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")


def kind_cell(term: Terminal, kind: str, width: int = 0) -> str:
    return term.paint(pad(kind, width), KIND_COLORS.get(kind, "grey"))


def priority_dot(term: Terminal, priority: str) -> str:
    return term.paint(DOT, PRIORITY_COLORS.get(priority, "grey"))


def search_header(view: dict) -> str:
    meaning = f" · {view['by_meaning']} found by meaning" if view["by_meaning"] else ""
    hidden = view["hidden_archive"] + view["hidden_superseded"]
    hid = " · archive and superseded hidden" if not hidden else f" · {hidden} hidden in the archive and superseded"
    if view["term_count"]:
        terms, some = view["term_count"], view["total"] - view["by_meaning"]
        found = f"0 matched all {terms} terms · {some} match some (best {view['best_terms']} of {terms})"
        return found + meaning + hid
    shown, total = view["shown"], view["total"]
    count = plural(total, "note") if shown == total else f"{shown} of {plural(total, 'note')}"
    return count + meaning + hid


def render_search(view: dict, term: Terminal) -> str:
    lines = [search_header(view)]
    if view.get("vaults"):
        lines.append(term.paint("in " + ", ".join(view["vaults"]), "dim"))
    rows = view["rows"]
    languages = tuple(view["languages"])
    if not rows:
        lines += ["", "No notes matched."]
        hint = "" if view["hidden_archive"] + view["hidden_superseded"] else translate_hint(languages)
        if hint:
            lines.append(term.paint(hint, "dim"))
        return "\n".join(lines)
    meaning_width = width_of(MEANING) if any(row["by_meaning"] for row in rows) else 0
    kind_width = max(width_of(row["kind"]) for row in rows)
    ages = [age_words(row["age_days"]) for row in rows]
    age_width = max(width_of(age) for age in ages)
    right_width = sum(w + len(GAP) for w in (meaning_width, kind_width) if w) + age_width
    title_width = min(max(width_of(row["title"] or row["key"]) for row in rows), term.width - right_width - len(GAP))
    for row, age in zip(rows, ages, strict=True):
        title = pad(cut(row["title"] or row["key"], title_width), title_width)
        right = []
        if meaning_width:
            right.append(term.paint(pad(MEANING if row["by_meaning"] else "", meaning_width), "magenta"))
        if kind_width:
            right.append(kind_cell(term, row["kind"], kind_width))
        right.append(pad_left(age, age_width))
        where = f"{row['vault']}: {row['key']}" if row.get("vault") else row["key"]
        area = link_stem(row["area"])
        if area:
            where += cut(f" · {area}", max(term.width - width_of(where), 0))
        snippet = unlinked(row["snippet"])
        if row["section"]:
            snippet = f"§ {row['section']} · {snippet}"
        lines += ["", term.paint(title, "bold") + GAP + GAP.join(right), term.paint(where, "dim")]
        if snippet:
            lines.append("  " + term.paint(cut(snippet, term.width - 2), "dim"))
    if view["term_count"] and partial_hint(languages):
        lines += ["", term.paint(partial_hint(languages), "dim")]
    return "\n".join(lines)


def backlog_cells(row: dict, term: Terminal, title_width: int, area_width: int, age_width: int, paths: bool) -> str:
    cells = [
        pad(cut(row["title"], title_width), title_width),
        pad(link_stem(row["area"]), area_width),
        pad_left(age_words(row["age_days"]), age_width),
    ]
    line = GAP.join(cells)
    return line + GAP + term.paint(row["key"], "dim") if paths else line


def render_backlog(view: dict, term: Terminal, paths: bool = False, show_all: bool = False) -> str:
    rows = view["rows"]
    if not rows:
        return view["empty"]
    grouped = len(rows) > BACKLOG_ROWS
    shown = rows if show_all or not grouped else rows[:BACKLOG_ROWS]
    priority_width = 0 if grouped else max(width_of(row["priority"] or "none") for row in rows) + 2
    area_width = max(width_of(link_stem(row["area"])) for row in shown)
    age_width = max(width_of(age_words(row["age_days"])) for row in shown)
    path_width = max(width_of(row["key"]) for row in shown) + len(GAP) if paths else 0
    indent = 2 if grouped else priority_width + len(GAP)
    room = term.width - indent - area_width - age_width - path_width - 2 * len(GAP)
    title_width = max(min(max(width_of(row["title"]) for row in shown), room), 8)
    lines, current = [], None
    counts = {
        priority: sum(1 for row in rows if row["priority"] == priority) for priority in {r["priority"] for r in rows}
    }
    for row in shown:
        priority = row["priority"] or ""
        cells = backlog_cells(row, term, title_width, area_width, age_width, paths)
        if grouped:
            if priority != current:
                current = priority
                heading = f"{priority or 'none'} · {counts[row['priority']]}"
                lines += [""] * bool(lines) + [priority_dot(term, priority) + " " + term.paint(heading, "bold")]
            lines.append("  " + cells)
        else:
            label = pad(priority or "none", priority_width - 2)
            painted = term.paint(label, PRIORITY_COLORS.get(priority, "grey"))
            lines.append(f"{priority_dot(term, priority)} {painted}{GAP}{cells}")
    if len(shown) < len(rows):
        lines.append(term.paint(f"and {len(rows) - len(shown)} more · --all shows them", "dim"))
    area = next((link_stem(row["area"]) for row in rows if row["area"]), "")
    hints = [plural(len(rows), "item")]
    hints.append("mindin read <path> opens one" if paths else "--paths shows where each one lives")
    if area:
        hints.append(f"--area {area} filters")
    return "\n".join([*lines, "", term.paint(" · ".join(hints), "dim")])


def meta_line(frontmatter: dict, term: Terminal) -> str:
    parts = []
    kind = str(frontmatter.get("kind") or "")
    if kind:
        parts.append(kind_cell(term, kind))
    area = link_stem(str(frontmatter.get("area") or ""))
    if area:
        parts.append(area)
    status = str(frontmatter.get("status") or "")
    if status:
        parts.append(term.paint(status, STATUS_COLORS.get(status, "grey")))
    priority = str(frontmatter.get("priority") or "")
    if priority:
        parts.append(priority_dot(term, priority) + " " + term.paint(priority, PRIORITY_COLORS.get(priority, "grey")))
    day = parse_date(str(frontmatter.get("updated") or frontmatter.get("date") or ""))
    if day is not None:
        parts.append(updated_words(max(0, (date.today() - day).days)))
    tags = frontmatter.get("tags")
    if isinstance(tags, list) and tags:
        parts.append(" ".join(f"#{tag}" for tag in tags))
    return " · ".join(parts)


def wrapped(body: str, width: int) -> list[str]:
    lines, fenced = [], False
    for line in body.strip("\n").splitlines():
        if FENCE_RE.match(line):
            fenced = not fenced
        if fenced or FENCE_RE.match(line) or width_of(line) <= width or line.lstrip().startswith("|"):
            lines.append(line)
            continue
        indent = line[: len(line) - len(line.lstrip())]
        bullet = re.match(r"^\s*([-*+]|\d+[.)])\s+", line)
        hanging = " " * len(bullet.group(0)) if bullet else indent
        lines += textwrap.wrap(
            line.strip(), width, initial_indent=indent, subsequent_indent=hanging, break_on_hyphens=False
        ) or [""]
    return lines


def same_words(left: str, right: str) -> bool:
    return " ".join(left.split()).casefold() == " ".join(right.split()).casefold()


def without_title_heading(body: str, title: str) -> str:
    lines = body.splitlines(keepends=True)
    first = next((number for number, line in enumerate(lines) if line.strip()), None)
    heading = H1_RE.match(lines[first]) if first is not None else None
    if heading is None or not same_words(heading.group(1), title):
        return body
    return "".join(lines[first + 1 :])


def render_note(path: str, text: str, term: Terminal, body_only: bool = False) -> str | None:
    if not path.endswith(".md"):
        return None
    try:
        frontmatter, body = parse(text)
    except FrontmatterError:
        return None
    if body_only:
        return "\n".join(wrapped(body, term.width))
    title = str(frontmatter.get("title") or path.rsplit("/", 1)[-1].removesuffix(".md"))
    head = [term.paint(cut(title, term.width), "bold")]
    meta = meta_line(frontmatter, term)
    if meta:
        head.append(meta)
    head.append(term.paint("─" * term.width, "dim"))
    return "\n".join([*head, "", *wrapped(without_title_heading(body, title), term.width)])


def kind_text(kind: str, stale_after_days: int) -> tuple[str, str]:
    name, explanation = LINT_KINDS.get(kind, (kind.replace("_", " "), ""))
    return name, explanation.format(stale=stale_after_days)


def lint_kind_named(name: str) -> str | None:
    wanted = re.sub(r"[\s-]+", "_", name.strip().lower())
    for kind, (plain, _) in LINT_KINDS.items():
        if wanted in (kind, re.sub(r"[\s,-]+", "_", plain)):
            return kind
    return None


def option_name(kind: str) -> str:
    return re.sub(r"[\s,]+", "-", LINT_KINDS[kind][0]) if kind in LINT_KINDS else kind


def render_lint(findings: Findings, term: Terminal, stale_after_days: int) -> str:
    grouped = findings.as_dict()
    if not grouped:
        return "No findings."
    total = sum(len(entries) for entries in grouped.values())
    ordered = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    count_width = max(len(str(len(entries))) for _, entries in ordered)
    names = {kind: kind_text(kind, stale_after_days) for kind, _ in ordered}
    name_width = max(width_of(name) for name, _ in names.values())
    lines = [f"{plural(total, 'finding')} in {plural(len(ordered), 'kind')}", ""]
    for kind, entries in ordered:
        name, explanation = names[kind]
        row = f"  {pad_left(str(len(entries)), count_width)}  {pad(name, name_width)}  "
        lines.append(row + term.paint(cut(explanation, term.width - width_of(row)), "dim"))
    hints = [f"lint --kind {option_name(ordered[0][0])} lists one kind"]
    if "stale_active" in grouped:
        hints.append("--park-stale moves stale tasks to the backlog")
    return "\n".join([*lines, "", term.paint(" · ".join(hints), "dim")])


def render_lint_kind(findings: Findings, kind: str, term: Terminal, stale_after_days: int) -> str:
    entries = findings.as_dict().get(kind, [])
    name, explanation = kind_text(kind, stale_after_days)
    if not entries:
        return f"No {name}."
    lines = [f"{name} ({len(entries)}) · " + term.paint(explanation, "dim"), ""]
    for entry in entries:
        match = ENTRY_RE.match(entry)
        if match is None:
            lines.append(entry)
            continue
        head = match.group("head")
        rest = ("→ " if match.group("sep") == " -> " else "") + unlinked(match.group("rest"))
        lines.append(head + GAP + term.paint(cut(rest, max(term.width - width_of(head) - len(GAP), 1)), "dim"))
    return "\n".join(lines)


def label(term: Terminal, name: str) -> str:
    return term.paint(pad(name, LABEL_WIDTH), "bold")


def log_parts(line: str) -> tuple[str, str]:
    match = LOG_LINE_RE.match(line.strip())
    if match is None:
        return "", line.strip().removeprefix("- ")
    return match.group("day"), match.group("text").split(" | ")[0].strip()


def commit_lines(commits: list[Commit], term: Terminal) -> list[str]:
    now = time.time()
    shown = list(reversed(commits[:COMMITS_SHOWN]))
    ages = [age_words((now - commit.time) / DAY_SECONDS) if commit.time else "" for commit in shown]
    sha_width = max(width_of(commit.sha) for commit in shown)
    age_width = max(width_of(age) for age in ages)
    subject_room = term.width - 2 - sha_width - age_width - 2 * len(GAP)
    subject_width = max(min(max(width_of(commit.subject) for commit in shown), subject_room), 8)
    lines = [term.paint(plural(len(commits), "commit") + " since the note", "orange")]
    if len(commits) > len(shown):
        lines[0] += term.paint(f", the newest {len(shown)}", "orange")
    for commit, age in zip(shown, ages, strict=True):
        subject = pad(cut(commit.subject, subject_width), subject_width)
        lines.append(
            f"  {term.paint(pad(commit.sha, sha_width), 'orange')}{GAP}{subject}{GAP}{pad_left(age, age_width)}"
        )
    return lines


def system_block(note: dict, commits: list[Commit], term: Terminal) -> list[str]:
    title = note["title"] or note["stem"]
    head = term.paint(title, "bold")
    if note["summary"]:
        head += GAP + cut(note["summary"], max(term.width - width_of(title) - len(GAP), 1))
    lines = [head, term.paint(f"{note['key']} · {updated_words(note['age_days'])}", "dim")]
    return lines + ([""] + commit_lines(commits, term) if commits else [])


def open_rows(view: dict, term: Terminal) -> list[str]:
    rows = []
    for note in view["tasks"]:
        rows.append(cut(note["title"] or note["key"], term.width - LABEL_WIDTH))
    for note in view["triage"]:
        stale = f"untouched {age_words(note['age_days'])}"
        title = cut(note["title"] or note["key"], max(term.width - LABEL_WIDTH - width_of(stale) - 2, 8))
        rows.append(title + GAP + term.paint(stale, "orange"))
    return rows or [term.paint("nothing in progress", "dim")]


def backlog_row(view: dict, term: Terminal) -> str:
    room = term.width - LABEL_WIDTH
    more = view["backlog_total"] - len(view["backlog"])
    tail = f"  +{more}" if more > 0 else ""
    parts, used = [], width_of(tail)
    for note in view["backlog"]:
        text = f"{DOT} {note['title']}"
        size = width_of(text) + (len(GAP) if parts else 0)
        if used + size > room:
            hidden = len(view["backlog"]) - len(parts) + max(more, 0)
            tail = f"  +{hidden}"
            break
        parts.append(priority_dot(term, note["priority"]) + " " + note["title"])
        used += size
    return GAP.join(parts) + term.paint(tail, "dim")


def render_context(view: dict, commits: dict[str, list[Commit]], term: Terminal) -> str:
    blocks: list[list[str]] = []
    top = [term.paint(notice, "dim") for notice in view["notices"]]
    if view["schema_problems"]:
        found = plural(len(view["schema_problems"]), "problem")
        top.append(term.paint(f"schema: {found} · mindin lint shows them", "red"))
    if top:
        blocks.append(top)
    blocks += [system_block(note, commits.get(note["key"], []), term) for note in view["system"]]
    traps = [note for note in view["references"] if note["kind"] == "trap"]
    if not (view["system"] or view["tasks"] or view["triage"] or view["backlog"] or traps or view["log_tail"]):
        return "\n\n".join(["\n".join(block) for block in blocks] + ["No vault context for this path or repo."])
    rows = open_rows(view, term)
    table = [label(term, "Open") + rows[0], *(" " * LABEL_WIDTH + row for row in rows[1:])]
    if view["backlog"]:
        table.append(label(term, "Backlog") + backlog_row(view, term))
    for number, note in enumerate(traps):
        table.append(label(term, "Traps" if number == 0 else "") + cut(note["title"], term.width - LABEL_WIDTH))
    if view["log_tail"]:
        day, text = log_parts(view["log_tail"][-1])
        lead = f"{day}{GAP}" if day else ""
        table.append(
            label(term, "Last log") + term.paint(lead, "dim") + cut(text, term.width - LABEL_WIDTH - len(lead))
        )
    blocks.append(table)
    return "\n\n".join("\n".join(block) for block in blocks)
