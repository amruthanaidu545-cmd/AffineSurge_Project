"""Text diff + 'does this change matter?' classification.

classify_change() is deliberately a heuristic: it flags changes in numbers (with units),
error codes, comparison symbols and modal/negation words as CRITICAL; any other textual
difference is MINOR. See APPROACH.md for what it cannot see.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter

WORD_RE = re.compile(r"\w+(?:[.\-–]\w+)*|[<>≥≤±%]")
NUM_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:\s*(?:mmhg|bpm|cm|%|seconds?|s|minutes?|years?|v|x))?(?![\w])", re.I)
CODE_RE = re.compile(r"\bE\d+\b")
SYMBOL_RE = re.compile(r"[<>≥≤±]")
SEMANTIC_WORDS = {"shall", "must", "should", "may", "not", "never", "no", "cannot", "only",
                  "except", "before", "after", "within", "exceeds", "longer", "below", "above",
                  "less", "more", "default", "always", "disables", "aborts", "silenced"}


def _canon(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("|", " ")).strip()


def critical_tokens(text: str) -> Counter:
    t = _canon(text)
    toks = [re.sub(r"\s+", "", m.group(0)).lower() for m in NUM_RE.finditer(t)]
    toks += [m.group(0) for m in CODE_RE.finditer(t)]
    toks += SYMBOL_RE.findall(t)
    toks += [w for w in re.findall(r"[a-z]+", t.lower()) if w in SEMANTIC_WORDS]
    return Counter(toks)


def diff_summary(old: str, new: str, max_items: int = 12) -> dict:
    a, b = WORD_RE.findall(_canon(old)), WORD_RE.findall(_canon(new))
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    added, removed = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("replace", "delete"):
            removed.append(" ".join(a[i1:i2]))
        if op in ("replace", "insert"):
            added.append(" ".join(b[j1:j2]))
    co, cn = critical_tokens(old), critical_tokens(new)
    return {
        "changed": a != b,
        "similarity": round(sm.ratio(), 3),
        "added": added[:max_items],
        "removed": removed[:max_items],
        "classification": classify_change(old, new),
        "critical_added": sorted((cn - co).elements()),
        "critical_removed": sorted((co - cn).elements()),
    }


def classify_change(old: str, new: str) -> str:
    """'none' | 'minor' | 'critical'."""
    if WORD_RE.findall(_canon(old)) == WORD_RE.findall(_canon(new)):
        return "none"
    return "critical" if critical_tokens(old) != critical_tokens(new) else "minor"
