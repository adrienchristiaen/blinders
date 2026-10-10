"""Second pass over the chosen repos: do their files really talk about the prompt?

Selection works from names, READMEs and links, which can agree with a prompt by chance ("faut" in a README).
This pass looks inside the files of the few chosen repos, with no model. A repo the prompt names, a repo that
contains one of its identifiers, and the user's own picks are never questioned; the best match stays when
nothing else is certain. The others must show their proof: a repo linked to a chosen one needs one of the
prompt's words in its files (the link is already a clue; so is a word of its own name), any other needs two. Those that do not are only
listed, with the reason, and can be ticked back. Words found in every chosen repo prove nothing and are
ignored, and so are words found in the files of more than a tenth of all your repos (everyday language).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import Config
from .grep import locate
from .text import MIN_REPOS_FOR_UBIQUITY, stem, tokens

if TYPE_CHECKING:
    from .select import Choice

JUDGED = ("related", "match")   # kinds of choice that must show proof
PROOF = 2                        # distinct prompt words to find in the files...
PROOF_WITH_CLUE = 1              # ...or one, when another clue already points to the repo (a link, a word of its name)
MIN_FOR_COMMON = 3                   # with fewer repos than this, "found in all of them" says nothing
GENERIC_SHARE = 0.1                  # a word found in the files of more than this share of all repos...
MIN_GENERIC = 3                      # ...(and of more than this many) is everyday language: no proof


def verify(prompt: str, choices: list[Choice], cfg: Config, cache: dict | None = None,
           everyone: list | None = None) -> tuple[list[Choice], list[tuple[Choice, str]]]:
    """(kept, [(dropped choice, reason)]). Nothing is dropped when the prompt gives no word to look for."""
    if not cfg.verify_enabled or not choices:
        return choices, []
    certain = any(c.kind in ("named", "hit") for c in choices)
    first_match = next((c for c in choices if c.kind == "match"), None)
    judged = [c for c in choices if c.kind in JUDGED and not (c is first_match and not certain)]
    if not judged:
        return choices, []
    own = {stem(t) for c in choices if c.kind == "named" for t in tokens(c.repo.name)}   # the names the user typed
    words = sorted({stem(t) for t in tokens(prompt)} - own)
    if not words:
        return choices, []
    repos = [c.repo for c in choices]
    where = locate(words, repos, cache=cache, stems=True)
    if everyone and len(everyone) >= MIN_REPOS_FOR_UBIQUITY:
        wide = locate(words, everyone, cache=cache, stems=True)
        limit = max(MIN_GENERIC, GENERIC_SHARE * len(everyone))
        where = {w: p for w, p in where.items() if len(wide[w]) <= limit}
    if len(repos) >= MIN_FOR_COMMON:
        where = {w: p for w, p in where.items() if len(p) < len(repos)}
    kept: list[Choice] = []
    dropped: list[tuple[Choice, str]] = []
    for c in choices:
        if c not in judged:
            kept.append(c)
            continue
        found = sorted(w for w, paths in where.items() if c.repo.path in paths)
        if len(found) >= (PROOF_WITH_CLUE if c.kind == "related" or c.in_name else PROOF):
            c.reason += " (its files mention " + ", ".join(found[:4]) + ")"
            kept.append(c)
        else:
            dropped.append((c, "its names match, but its files do not mention the prompt's words"))
    return kept, dropped
