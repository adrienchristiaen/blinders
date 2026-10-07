"""Pick which repos a prompt needs. Pure function of (prompt, index): fast and deterministic."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .config import Config
from .scan import Repo
from .text import squash, tokens, words

NAME_BONUS = 1000.0


@dataclass
class Choice:
    repo: Repo
    score: float
    reason: str


def _name_spans(repo: Repo, prompt_norm: str) -> list[tuple[int, int]]:
    """Whole-word occurrences of the repo name in the prompt ("jira-cli" matches "jira cli")."""
    parts = words(repo.name)
    if len(squash(repo.name)) < 4 or not parts:
        return []
    pattern = r"\b" + " ?".join(re.escape(p) for p in parts) + r"\b"
    return [m.span() for m in re.finditer(pattern, prompt_norm)]


def _named_repos(repos: list[Repo], prompt: str) -> set[str]:
    """Paths of repos explicitly named in the prompt.

    A name that only occurs inside a longer repo name that is also matched does not count:
    "sales-api-java" must not also open "sales-api".
    """
    prompt_norm = " ".join(words(prompt))
    spans = {r.path: _name_spans(r, prompt_norm) for r in repos}
    length = {r.path: len(squash(r.name)) for r in repos}
    named: set[str] = set()
    for path, own in spans.items():
        for s in own:
            covered = any(
                length[other] > length[path] and o0 <= s[0] and s[1] <= o1
                for other, other_spans in spans.items()
                if other != path
                for o0, o1 in other_spans
            )
            if not covered:
                named.add(path)
                break
    return named


def rank(prompt: str, repos: list[Repo]) -> list[Choice]:
    q_tokens = tokens(prompt)
    q_set = set(q_tokens)
    named = _named_repos(repos, prompt)
    n = max(len(repos), 1)
    df: dict[str, int] = {}
    for repo in repos:
        for term in repo.terms:
            df[term] = df.get(term, 0) + 1

    ranked: list[Choice] = []
    for repo in repos:
        score = 0.0
        hits: list[str] = []
        for term in q_set:
            tf = repo.terms.get(term, 0)
            if tf:
                score += math.log(1 + n / df[term]) * math.log(1 + tf)
                hits.append(term)
        reason = "terms: " + ", ".join(sorted(hits)) if hits else "no match"
        if repo.path in named:
            score += NAME_BONUS
            reason = "repo named in prompt"
        ranked.append(Choice(repo, score, reason))
    ranked.sort(key=lambda c: (-c.score, c.repo.name))
    return ranked


def select(prompt: str, repos: list[Repo], cfg: Config) -> list[Choice]:
    """Return the repos to open, best first. Empty list means: stay fully blind."""
    ranked = rank(prompt, repos)
    if not ranked or ranked[0].score <= 0:
        return []
    top = ranked[0].score
    chosen = [c for c in ranked if c.score > 0 and c.score >= top * cfg.relative_threshold]
    return chosen[: cfg.max_repos]
