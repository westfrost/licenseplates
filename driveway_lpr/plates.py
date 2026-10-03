"""Plate text normalisation, format correction and multi-frame voting."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

# Characters OCR commonly confuses between letters and digits.
_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "Z": "2", "S": "5", "G": "6", "T": "7", "B": "8"}
_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "4": "A", "5": "S", "6": "G", "7": "T", "8": "B"}


def normalize(text: str) -> str:
    """Upper-case and strip everything that is not A-Z / 0-9 (also Æ/Ø/Å)."""
    return re.sub(r"[^A-Z0-9ÆØÅ]", "", (text or "").upper())


def apply_template(text: str, template: str, max_changes: int = 1) -> str | None:
    """Fix letter/digit confusions so `text` fits e.g. "LLDDDDD".

    Returns the corrected text, or None if it cannot fit the template with at
    most `max_changes` substitutions (more than that is probably a foreign or
    personalised plate, not a misread).
    """
    if len(text) != len(template):
        return None
    out = []
    for ch, kind in zip(text, template):
        if kind == "D":
            if ch.isdigit():
                out.append(ch)
            elif ch in _TO_DIGIT:
                out.append(_TO_DIGIT[ch])
            else:
                return None
        elif kind == "L":
            if ch.isalpha():
                out.append(ch)
            elif ch in _TO_LETTER:
                out.append(_TO_LETTER[ch])
            else:
                return None
        else:
            out.append(ch)
    fixed = "".join(out)
    if sum(a != b for a, b in zip(text, fixed)) > max_changes:
        return None
    return fixed


def format_plate(text: str) -> str:
    """Pretty-print Danish standard plates as "AB 12 345"; others unchanged."""
    if re.fullmatch(r"[A-Z]{2}\d{5}", text):
        return f"{text[:2]} {text[2:4]} {text[4:]}"
    return text


class PlateRules:
    def __init__(self, patterns: list[str], templates: list[str]):
        self.patterns = [re.compile(p) for p in patterns]
        self.templates = templates
        # The first pattern is the "strict" national format.
        self.strict = self.patterns[0] if self.patterns else None

    def clean(self, raw: str) -> str:
        text = normalize(raw)
        if self.strict is not None and self.strict.match(text):
            return text
        for template in self.templates:
            fixed = apply_template(text, template)
            if fixed is not None and (self.strict is None or self.strict.match(fixed)):
                return fixed
        return text

    def is_strict(self, text: str) -> bool:
        return bool(self.strict and self.strict.match(text))

    def is_valid(self, text: str) -> bool:
        if not self.patterns:
            return bool(text)
        return any(p.match(text) for p in self.patterns)


@dataclass
class Read:
    text: str
    confidence: float


@dataclass
class Vote:
    text: str
    confidence: float
    reads: int
    alternatives: list[tuple[str, int]]


def vote(reads: list[Read], rules: PlateRules | None = None) -> Vote | None:
    """Combine many noisy reads of the same plate into one answer.

    1. Pick the most likely length (weighted by confidence, strict format bonus).
    2. For that length, vote per character position, weighted by confidence.
    """
    reads = [r for r in reads if r.text]
    if not reads:
        return None

    def weight(r: Read) -> float:
        bonus = 1.5 if rules is not None and rules.is_strict(r.text) else 1.0
        return max(r.confidence, 0.01) * bonus

    length_score: dict[int, float] = defaultdict(float)
    for r in reads:
        length_score[len(r.text)] += weight(r)
    best_len = max(length_score, key=lambda n: (length_score[n], n))
    same_len = [r for r in reads if len(r.text) == best_len]

    chars = []
    char_conf = []
    for i in range(best_len):
        scores: dict[str, float] = defaultdict(float)
        for r in same_len:
            scores[r.text[i]] += weight(r)
        ch = max(scores, key=lambda c: scores[c])
        chars.append(ch)
        char_conf.append(scores[ch] / sum(scores.values()))
    text = "".join(chars)

    agreeing = [r.confidence for r in same_len if r.text == text]
    base = max(agreeing) if agreeing else max(r.confidence for r in same_len)
    confidence = round(base * min(char_conf), 4)

    counts = Counter(r.text for r in reads)
    alternatives = [(t, n) for t, n in counts.most_common(5) if t != text]
    return Vote(text=text, confidence=confidence, reads=len(reads), alternatives=alternatives)


def similarity(a: str, b: str) -> float:
    """Normalised Levenshtein similarity in [0, 1]."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1.0 - prev[-1] / max(len(a), len(b))
