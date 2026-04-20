"""Language detection for feed items.

Primary: langdetect (Google port, 55+ languages, stable).
Fallback: simple heuristic based on Turkish diacritics and common English bigrams.
We only need to distinguish {tr, en, other}, not full ISO 639 accuracy.
"""

from __future__ import annotations

import re
from typing import Optional

ALLOWED_LANGUAGES = {"tr", "en"}

TR_CHARS = set("çğıöşüÇĞİÖŞÜ")
TR_STOPWORDS = {
    "ve",
    "bir",
    "bu",
    "için",
    "ile",
    "da",
    "de",
    "ki",
    "ama",
    "ya",
    "gibi",
    "kadar",
    "çok",
    "ne",
    "var",
    "yok",
    "olan",
    "olarak",
    "daha",
}
EN_STOPWORDS = {
    "the",
    "and",
    "is",
    "in",
    "to",
    "of",
    "a",
    "for",
    "on",
    "with",
    "that",
    "this",
    "it",
    "as",
    "are",
    "was",
    "be",
    "by",
    "from",
    "or",
}

_WORD_RE = re.compile(r"[a-zA-ZçğıöşüÇĞİÖŞÜ]+")


def _heuristic(text: str) -> str:
    """Cheap, dependency-free fallback. Not as accurate as langdetect."""
    if not text:
        return "und"
    words = [w.lower() for w in _WORD_RE.findall(text)[:200]]
    if not words:
        return "und"
    tr_hits = sum(1 for ch in text[:2000] if ch in TR_CHARS)
    tr_stop = sum(1 for w in words if w in TR_STOPWORDS)
    en_stop = sum(1 for w in words if w in EN_STOPWORDS)
    tr_score = tr_hits * 0.5 + tr_stop
    en_score = en_stop
    if tr_score >= 3 and tr_score > en_score:
        return "tr"
    if en_score >= 3 and en_score > tr_score:
        return "en"
    return "other"


def detect(text: str) -> str:
    """Return a language code ('tr', 'en', 'other', or 'und' if empty)."""
    if not text or len(text.strip()) < 20:
        return "und"
    try:
        from langdetect import DetectorFactory, detect as _ld_detect  # type: ignore

        DetectorFactory.seed = 0
        code = _ld_detect(text[:4000])
        if code in ALLOWED_LANGUAGES:
            return code
        return "other"
    except Exception:
        return _heuristic(text)


def is_allowed(language: str | None) -> bool:
    return (language or "") in ALLOWED_LANGUAGES
