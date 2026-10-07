"""Pick which repos a prompt needs. Pure function of (prompt, index): fast and deterministic."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config
from .scan import Repo
from .text import squash, tokens

NAME_BONUS = 1000.0


@dataclass
class Choice:
    repo: Repo
    score: float
    reason: str


def _name_mentioned(repo: Repo, prompt_tokens: set[str], prompt_squashed: str) -> bool:
    name_tokens = tokens(repo.name)
    if name_tokens and all(t in prompt_tokens for t in name_tokens):
        return True
    flat = squash(repo.name)
    return len(flat) >= 4 and flat in prompt_squashed


def rank(prompt: str, repos: list[Repo]) -> list[Choice]:
    q_tokens = tokens(prompt)
    q_set = set(q_tokens)
    q_squashed = squash(prompt)
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
        if _name_mentioned(repo, q_set, q_squashed):
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
