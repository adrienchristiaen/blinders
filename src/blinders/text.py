"""Tokenisation shared by the scanner and the selector."""

from __future__ import annotations

import re
import unicodedata

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Grammar words only. Words that are merely common in *your* repos ("service", "readme", "test") are
# found from the data instead: see ``select.UBIQUITOUS``.
STOPWORDS = frozenset(
    """
    the and for with that this from into your you are not but all can has have will
    how why what when where which should would could
    les des une pour avec dans sur par pas que qui est sont mais tout tous plus
    fait faire fais peux veux dois cette ces aux ses son sa ou et de du le la un en
    """.split()
)
STEM_LENGTH = 5
UBIQUITOUS_SHARE = 0.5
MIN_REPOS_FOR_UBIQUITY = 6   # below this many repos, "found in most of them" says nothing


def _fold(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def tokens(text: str) -> list[str]:
    """Lowercased alphanumeric tokens, camelCase split, accents folded, stopwords dropped."""
    out: list[str] = []

    def keep(tok: str) -> None:
        if len(tok) >= 3 and tok not in STOPWORDS and not tok.isdigit():
            out.append(tok)

    for word in re.split(r"[^A-Za-z0-9]+", _fold(text)):
        if not word:
            continue
        parts = _CAMEL.sub(" ", word).lower().split()
        for part in parts:
            keep(part)
        if len(parts) > 1:  # "BigQuery" must match both "big query" and "bigquery"
            keep(word.lower())
    return out


def words(text: str) -> list[str]:
    """Lowercased alphanumeric words, accents folded, nothing dropped (for exact name matching)."""
    return [w for w in re.split(r"[^a-z0-9]+", _fold(text).lower()) if w]


def squash(text: str) -> str:
    """Lowercase alphanumerics only, for substring matching of repo names."""
    return re.sub(r"[^a-z0-9]", "", _fold(text).lower())


def stem(token: str) -> str:
    """Crude, language-free stem: the first letters. ``deploy``, ``deployed``, ``deployment`` and the French
    ``déploie`` meet; so do ``table`` and ``tables``. Used only to compare words, never shown."""
    return token[:STEM_LENGTH]


def stems(text: str) -> list[str]:
    return [stem(t) for t in tokens(text)]


def ubiquitous(term_sets, min_items: int, share: float = UBIQUITOUS_SHARE) -> set[str]:
    """Terms found in at least ``share`` of the items. They describe none of them in particular, and they
    are learned from the user's own data, so no hand-made stop-list is needed. Empty when there are too few
    items to tell."""
    sets = [set(s) for s in term_sets]
    if len(sets) < min_items:
        return set()
    counts: dict[str, int] = {}
    for s in sets:
        for term in s:
            counts[term] = counts.get(term, 0) + 1
    return {term for term, n in counts.items() if n >= len(sets) * share}
