from __future__ import annotations

import re
from functools import cache, lru_cache

import snowballstemmer

ALGORITHMS = {
    "ar": "arabic",
    "ca": "catalan",
    "cs": "czech",
    "da": "danish",
    "de": "german",
    "el": "greek",
    "en": "english",
    "eo": "esperanto",
    "es": "spanish",
    "et": "estonian",
    "eu": "basque",
    "fa": "persian",
    "fi": "finnish",
    "fr": "french",
    "ga": "irish",
    "hi": "hindi",
    "hu": "hungarian",
    "hy": "armenian",
    "id": "indonesian",
    "it": "italian",
    "lt": "lithuanian",
    "nb": "norwegian",
    "ne": "nepali",
    "nl": "dutch",
    "nn": "norwegian",
    "no": "norwegian",
    "pl": "polish",
    "pt": "portuguese",
    "ro": "romanian",
    "ru": "russian",
    "sr": "serbian",
    "st": "sesotho",
    "sv": "swedish",
    "ta": "tamil",
    "tr": "turkish",
    "yi": "yiddish",
}
TOKEN_RE = re.compile(r"[^\W_]+")
STEM_CACHE_SIZE = 200_000


def algorithm_of(code: str) -> str | None:
    primary = re.split(r"[-_]", code.strip().lower(), maxsplit=1)[0]
    return ALGORITHMS.get(primary)


def algorithms_of(languages: tuple[str, ...]) -> tuple[str, ...]:
    found = (algorithm_of(code) for code in languages)
    return tuple(dict.fromkeys(name for name in found if name))


@cache
def _stemmer(algorithm: str) -> snowballstemmer.Stemmer:
    return snowballstemmer.stemmer(algorithm)


@lru_cache(maxsize=STEM_CACHE_SIZE)
def stem_word(algorithm: str, word: str) -> str:
    return _stemmer(algorithm).stemWord(word)


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.casefold())


def stemmed(text: str, algorithms: tuple[str, ...]) -> list[str]:
    words = tokens(text)
    renderings = (" ".join(stem_word(algorithm, word) for word in words) for algorithm in algorithms)
    return list(dict.fromkeys(rendering for rendering in renderings if rendering))
