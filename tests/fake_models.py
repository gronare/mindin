from __future__ import annotations

import os
import re
import time
import zlib
from pathlib import Path

import numpy

CONCEPTS = {"inloggning": 0, "oauth": 0, "token": 0, "login": 0, "backup": 1, "säkerhetskopia": 1, "kebbie": 2}
DIMS = 32
WORD_RE = re.compile(r"[^\W_]+")


def concept_vector(text: str) -> numpy.ndarray:
    vector = numpy.zeros(DIMS, dtype=numpy.float32)
    for word in WORD_RE.findall(text.casefold()):
        vector[CONCEPTS.get(word, 8 + zlib.crc32(word.encode()) % (DIMS - 8))] += 1.0
    norm = numpy.linalg.norm(vector)
    return vector / norm if norm else vector


class ConceptModel:
    def __init__(self, model: str) -> None:
        self.model = model
        gate = os.environ.get("FAKE_EMBED_GATE")
        self.gate = Path(gate) if gate else None
        self.quick = set((os.environ.get("FAKE_EMBED_QUICK") or "quick").split(","))

    def embed(self, texts: list[str]) -> list[numpy.ndarray]:
        if self.gate is not None and not (len(texts) == 1 and texts[0] in self.quick):
            with self.gate.with_suffix(".entered").open("a") as entered:
                entered.write(f"{texts}\n")
            deadline = time.monotonic() + 30
            while not self.gate.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        return [concept_vector(text) for text in texts]


def concept_model(model: str, folder: str) -> ConceptModel:
    return ConceptModel(model)
