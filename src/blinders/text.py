"""Tokenisation shared by the scanner and the selector."""

from __future__ import annotations

import re
import unicodedata

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

STOPWORDS = frozenset(
    """
    the and for with that this from into your you are not but all can has have will
    use used using add new get set run how why what when where which should would could
    les des une pour avec dans sur par pas que qui est sont mais tout tous plus
    fait faire fais peux veux dois cette ces aux ses son sa ou et de du le la un en
    file files code repo repository project readme todo test tests src main
    """.split()
)


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
