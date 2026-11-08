from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TAIL = r"(?:[\x60'\"\s;]|\\[nr]|$)"
PLACEHOLDER_START = ("<", "$", "{", "%", "[")


@dataclass(frozen=True)
class Rule:
    label: str
    pattern: re.Pattern[str]
    check: Callable[[str], bool] | None = None
    group: int | str = 0


@dataclass(frozen=True)
class Finding:
    label: str
    line: int
    excerpt: str


def entropy(value: str) -> float:
    counts = {char: value.count(char) for char in set(value)}
    return -sum(count / len(value) * math.log2(count / len(value)) for count in counts.values())


def random_looking(value: str) -> bool:
    has_digit = any(char.isdigit() for char in value)
    has_letter = any(char.isalpha() for char in value)
    return has_digit and has_letter and entropy(value) >= 3.5


def password_like(value: str) -> bool:
    if value.startswith(PLACEHOLDER_START) or set(value) <= set("*x.•"):
        return False
    return any(not char.isalpha() for char in value)


def not_an_example(value: str) -> bool:
    return not value.endswith("EXAMPLE")


def luhn(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        number = int(char) * (2 if position % 2 else 1)
        total += number - 9 if number > 9 else number
    return total % 10 == 0


def card_number(value: str) -> bool:
    digits = re.sub(r"[ -]", "", value)
    return 13 <= len(digits) <= 19 and digits[0] in "23456" and len(set(digits)) > 1 and luhn(digits)


def personnummer(value: str) -> bool:
    digits = re.sub(r"[-+]", "", value)[-10:]
    month, day = int(digits[2:4]), int(digits[4:6])
    return 1 <= month <= 12 and (1 <= day <= 31 or 61 <= day <= 91) and luhn(digits)


def _rule(label: str, pattern: str, check: Callable[[str], bool] | None = None, group: int | str = 0) -> Rule:
    return Rule(label, re.compile(pattern), check, group)


# The provider and generic key patterns come from gitleaks config/gitleaks.toml, MIT licence, (c) 2019 Zachary Rice.
SECRET_RULES = (
    _rule("an AWS access key", r"\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16})\b", not_an_example, 1),
    _rule(
        "a private key",
        r"(?i)-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----"
        r"[\s\S-]{64,}?KEY(?: BLOCK)?-----",
    ),
    _rule("a GitHub token", r"(?:ghp|gho|ghu|ghs|ghr)_[0-9a-zA-Z]{36}|github_pat_\w{82}"),
    _rule("a GitLab token", r"glpat-[\w-]{20}"),
    _rule(
        "a Slack token",
        r"xoxb-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9-]*"
        r"|xox[pe](?:-[0-9]{10,13}){3}-[a-zA-Z0-9-]{28,34}",
    ),
    _rule("a Slack webhook", r"(?:https?://)?hooks.slack.com/(?:services|workflows|triggers)/[A-Za-z0-9+/]{43,56}"),
    _rule("a secret API key", rf"\b((?:sk|rk)_(?:test|live|prod)_[a-zA-Z0-9]{{10,99}}){TAIL}", group=1),
    _rule("a Google API key", rf"\b(AIza[\w-]{{35}}){TAIL}", group=1),
    _rule("an npm token", rf"(?i)\b(npm_[a-z0-9]{{36}}){TAIL}", group=1),
    _rule("a SendGrid key", rf"\b(SG\.(?i:[a-z0-9=_\-\.]{{66}})){TAIL}", group=1),
    _rule("an Anthropic API key", rf"\b(sk-ant-api03-[a-zA-Z0-9_\-]{{93}}AA){TAIL}", group=1),
    _rule(
        "an OpenAI API key",
        r"\b(sk-(?:proj|svcacct|admin)-(?:[A-Za-z0-9_-]{74}|[A-Za-z0-9_-]{58})T3BlbkFJ(?:[A-Za-z0-9_-]{74}|"
        rf"[A-Za-z0-9_-]{{58}})\b|sk-[a-zA-Z0-9]{{20}}T3BlbkFJ[a-zA-Z0-9]{{20}}){TAIL}",
        group=1,
    ),
    _rule(
        "a JSON web token",
        rf"\b(ey[a-zA-Z0-9]{{17,}}\.ey[a-zA-Z0-9\/\\_-]{{17,}}\.(?:[a-zA-Z0-9\/\\_-]{{10,}}={{0,2}})?){TAIL}",
        group=1,
    ),
    _rule(
        "a key or token",
        r"(?i)[\w.-]{0,50}?(?:access|auth|(?-i:[Aa]pi|API)|credential|creds|key|passw(?:or)?d|secret|token)"
        r"(?:[ \t\w.-]{0,20})[\s'\"]{0,3}(?:=|>|:{1,3}=|\|\||:|=>|\?=|,)[\x60'\"\s=]{0,5}"
        rf"([\w.=-]{{10,150}}|[a-z0-9][a-z0-9+/]{{11,}}={{0,3}}){TAIL}",
        random_looking,
        1,
    ),
    _rule(
        "a password",
        r"(?i)\b(?:password|passwd|passphrase|pwd|lösenord)\b[\"']?[ \t]*[:=][ \t]*"
        r"[\"'\x60]?(?P<value>[^\s\"'\x60]{6,})",
        password_like,
        "value",
    ),
    _rule("a password in an address", r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:(?P<value>[^\s:/@]+)@", password_like, "value"),
)
PERSONAL_RULES = {
    "card_number": _rule("a card number", r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])", card_number),
    "personnummer": _rule(
        "a Swedish personal identity number",
        r"(?<![\w-])(?:(?:18|19|20)?\d{6}[-+]\d{4}|(?:18|19|20)\d{10})(?![\w-])",
        personnummer,
    ),
}


@dataclass(frozen=True)
class GuardSettings:
    secrets: bool = False
    personal_data: tuple[str, ...] = ()

    @property
    def on(self) -> bool:
        return self.secrets or bool(self.personal_data)

    @property
    def rules(self) -> tuple[Rule, ...]:
        chosen = SECRET_RULES if self.secrets else ()
        return chosen + tuple(PERSONAL_RULES[name] for name in self.personal_data if name in PERSONAL_RULES)

    def describe(self) -> str:
        refused = ["secrets such as API keys, tokens, private keys and passwords"] if self.secrets else []
        refused += [PERSONAL_RULES[name].label.removeprefix("a ") + "s" for name in self.personal_data]
        return ", ".join(refused[:-1]) + f" and {refused[-1]}" if len(refused) > 1 else "".join(refused)


def guard_settings(section: Any) -> GuardSettings:
    if not isinstance(section, dict):
        return GuardSettings()
    return GuardSettings(bool(section.get("secrets")), tuple(str(name) for name in section.get("personal_data") or ()))


def guard_problems(section: Any) -> list[str]:
    if not isinstance(section, dict):
        return ["bundle.guard must be a mapping like { secrets: true, personal_data: [card_number] }"]
    problems: list[str] = []
    if "secrets" in section and not isinstance(section["secrets"], bool):
        problems.append("bundle.guard.secrets must be true or false")
    named = section.get("personal_data") or []
    if not isinstance(named, list) or not all(isinstance(name, str) for name in named):
        return [*problems, f"bundle.guard.personal_data must be a list of: {', '.join(PERSONAL_RULES)}"]
    problems += [
        f"bundle.guard.personal_data names '{name}', which is not one of: {', '.join(PERSONAL_RULES)}"
        for name in named
        if name not in PERSONAL_RULES
    ]
    return problems


def masked(value: str) -> str:
    return f"{value[:4]}…" if len(value) > 4 else "…"


def findings(text: str, settings: GuardSettings) -> list[Finding]:
    found: list[Finding] = []
    for rule in settings.rules:
        for match in rule.pattern.finditer(text):
            value = match.group(rule.group) or ""
            if rule.check is not None and not rule.check(value):
                continue
            line = text.count("\n", 0, match.start()) + 1
            if not any(seen.line == line and seen.excerpt == masked(value) for seen in found):
                found.append(Finding(rule.label, line, masked(value)))
    return sorted(found, key=lambda finding: finding.line)


def refusal(key: str, text: str, settings: GuardSettings) -> str:
    found = findings(text, settings) if settings.on else []
    if not found:
        return ""
    lines = [f"- line {finding.line}: {finding.label} ({finding.excerpt})" for finding in found]
    return "\n".join(
        [
            f"{key} was not written: this vault refuses {settings.describe()}, and the note holds:",
            *lines,
            "Remove each one and write again. Name where a secret is kept, such as the secret manager's entry, "
            "instead of the secret, and use a made-up example in place of personal data.",
        ]
    )
