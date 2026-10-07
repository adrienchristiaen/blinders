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
    """Return the seed repos (named or best content match), best first. Empty: stay fully blind."""
    ranked = rank(prompt, repos)
    if not ranked or ranked[0].score <= 0:
        return []
    top = ranked[0].score
    chosen = [c for c in ranked if c.score > 0 and c.score >= top * cfg.relative_threshold]
    return chosen[: cfg.max_repos]


# --- related repos ---------------------------------------------------------------------------

# Words in a prompt that say "I also need the repo that plays this role".
INTENT_WORDS = {
    "deploy": {
        "deploy", "deploys", "deployed", "deployment", "deploiement", "deploie", "deployer",
        "kubernetes", "k8s", "helm", "chart", "kustomize", "terraform", "infra", "infrastructure",
        "ingress", "pod", "pods", "cluster", "namespace", "rollout", "argocd", "prod", "production",
        "release", "docker", "container", "conteneur",
    },
    "data": {
        "lineage", "dbt", "airflow", "dag", "dags", "bigquery", "pipeline", "pipelines",
        "table", "tables", "schema", "kafka", "topic", "topics", "dataset", "etl",
    },
}


def intents(prompt: str) -> set[str]:
    words = set(tokens(prompt))
    return {role for role, vocab in INTENT_WORDS.items() if words & vocab}


@dataclass
class Related:
    repo: Repo
    weight: float
    why: str
    opens: bool  # opened automatically (intent match or --related) vs only listed


@dataclass
class Plan:
    opened: list[Choice]
    related: list[Related]  # related but closed: listed in the index, not opened


def _why(kind: str, seed: Repo, group: str = "") -> str:
    if kind == "refs":
        return f"{seed.name} references it in its build/deploy files"
    if kind == "refd_by":
        return f"it references {seed.name} in its build/deploy files"
    if kind == "name":
        return f"name close to {seed.name}"
    return f"same group '{group}' as {seed.name}"


def neighbors(seeds: list[Repo], repos: list[Repo], cfg: Config) -> list[tuple[Repo, float, str]]:
    """Repos linked to the seeds (index links + config groups), strongest first."""
    by_path = {r.path: r for r in repos}
    by_name = {r.name: r for r in repos}
    seed_paths = {s.path for s in seeds}
    best: dict[str, tuple[float, str]] = {}

    def offer(path: str, weight: float, why: str) -> None:
        if path in seed_paths or path not in by_path:
            return
        if path not in best or weight > best[path][0]:
            best[path] = (weight, why)

    for seed in seeds:
        for link in seed.links:
            offer(link["to"], float(link["w"]), _why(link["kind"], seed))
        for group, members in cfg.groups.items():
            if seed.name in members:
                for member in members:
                    if member in by_name:
                        offer(by_name[member].path, 4.0, _why("group", seed, group))
    out = [(by_path[p], w, why) for p, (w, why) in best.items()]
    out.sort(key=lambda t: (-t[1], t[0].name))
    return out


def plan(
    prompt: str,
    repos: list[Repo],
    cfg: Config,
    related: str = "auto",
    forced: list[Repo] | None = None,
) -> Plan:
    """Seeds, then their related repos.

    Seeds are ``forced`` repos (``-r``) when given, else the repos the prompt names or matches.
    ``related``: ``auto`` opens neighbors whose role matches the prompt's intent (deploy, data),
    ``all`` opens the strongest neighbors regardless, ``none`` ignores relations.
    """
    if forced is not None:
        seeds = [Choice(r, NAME_BONUS, "requested") for r in forced]
    else:
        seeds = select(prompt, repos, cfg)
    if not seeds or related == "none":
        return Plan(seeds, [])
    wanted = intents(prompt)
    opened = list(seeds)
    listed: list[Related] = []
    budget = cfg.max_related_open
    for repo, weight, why in neighbors([s.repo for s in seeds], repos, cfg):
        takes = related == "all" or bool(wanted & set(repo.roles))
        if takes and budget > 0:
            budget -= 1
            reason = "related: " + why + (" (role matches the prompt)" if related == "auto" else "")
            opened.append(Choice(repo, weight, reason))
        elif len(listed) < cfg.max_related_list:
            listed.append(Related(repo, weight, why, opens=False))
    return Plan(opened, listed)
