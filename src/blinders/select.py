"""Pick which repos a prompt needs. Pure function of (prompt, index): fast and deterministic."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace

from .config import Config
from .scan import Repo
from .grep import find_hits
from .verify import verify
from .text import MIN_REPOS_FOR_UBIQUITY, squash, stem, stems, ubiquitous, words

NAME_BONUS = 1000.0


MIN_EVIDENCE = 2   # distinct prompt words a repo must match to join a repo the prompt already names


@dataclass
class Choice:
    repo: Repo
    score: float
    reason: str
    hits: tuple[str, ...] = ()   # prompt stems found in the repo
    in_name: bool = False        # one of them is a word of the repo's own name
    kind: str = "match"          # named | hit (contains an identifier) | match | related

    @property
    def solid(self) -> bool:
        """Enough to be a companion: several words agree, or the repo's own name is in the prompt's words.
        A single stray word ("faut", "modifier") in a README is not."""
        return len(self.hits) >= MIN_EVIDENCE or self.in_name


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


def _stemmed(repo: Repo) -> dict[str, int]:
    out: dict[str, int] = {}
    for term, n in repo.terms.items():
        s = stem(term)
        out[s] = out.get(s, 0) + n
    return out


def rank(prompt: str, repos: list[Repo], ignore: set[str] | None = None) -> list[Choice]:
    """Every repo, best first. ``ignore`` holds stems the prompt should not be scored on."""
    q_set = set(stems(prompt)) - (ignore or set())
    named = _named_repos(repos, prompt)
    n = max(len(repos), 1)
    vocab = {repo.path: _stemmed(repo) for repo in repos}
    df: dict[str, int] = {}
    for terms in vocab.values():
        for term in terms:
            df[term] = df.get(term, 0) + 1
    q_set -= ubiquitous(vocab.values(), MIN_REPOS_FOR_UBIQUITY)

    ranked: list[Choice] = []
    for repo in repos:
        score = 0.0
        hits: list[str] = []
        for term in q_set:
            tf = vocab[repo.path].get(term, 0)
            if tf:
                score += math.log(1 + n / df[term]) * math.log(1 + tf)
                hits.append(term)
        reason = "terms: " + ", ".join(sorted(hits)) if hits else "no match"
        if repo.path in named:
            score += NAME_BONUS
            reason = "repo named in prompt"
        in_name = bool(set(hits) & set(stems(repo.name)))
        ranked.append(Choice(repo, score, reason, tuple(sorted(hits)), in_name))
    ranked.sort(key=lambda c: (-c.score, c.repo.name))
    return ranked


def _keep_relative(ranked: list[Choice], cfg: Config) -> list[Choice]:
    if not ranked or ranked[0].score <= 0:
        return []
    top = ranked[0].score
    return [c for c in ranked if c.score > 0 and c.score >= top * cfg.relative_threshold]


def select(prompt: str, repos: list[Repo], cfg: Config, ranked: list[Choice] | None = None) -> list[Choice]:
    """Return the seed repos, best first. Empty: stay fully blind.

    Two lanes. Repos named in the prompt always come first. Then the rest of the prompt (what is left once
    the named repos' own names are set aside) is matched against every other repo, so a request that names
    the application but talks about a schema also finds the schema repo."""
    ranked = ranked if ranked is not None else rank(prompt, repos)
    named = [replace(c, kind="named") for c in ranked if c.score >= NAME_BONUS]
    if not named:
        return _keep_relative(ranked, cfg)[: cfg.max_repos]
    taken = {c.repo.path for c in named}
    leftover = {s for c in named for s in stems(c.repo.name)}
    companions = [c for c in rank(prompt, repos, ignore=leftover) if c.repo.path not in taken and c.solid]
    return (named + _keep_relative(companions, cfg))[: cfg.max_repos]


def with_hits(seeds: list[Choice], hits: dict[str, list[str]], repos: list[Repo], cfg: Config) -> list[Choice]:
    """Named repos first, then repos that contain the prompt's exact identifiers (most identifiers first),
    then the rest of the seeds."""
    if not hits:
        return seeds
    by_path = {r.path: r for r in repos}
    named = [c for c in seeds if c.kind == "named"]
    taken = {c.repo.path for c in named}
    found = sorted((p for p in hits if p in by_path and p not in taken), key=lambda p: (-len(hits[p]), by_path[p].name))
    contains = [Choice(by_path[p], float(len(hits[p])), "contains " + ", ".join(hits[p]), kind="hit") for p in found]
    rest = [c for c in seeds if c.kind != "named" and c.repo.path not in set(found)]
    return (named + contains + rest)[: cfg.max_repos]


# --- related repos ---------------------------------------------------------------------------

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


def _verified(prompt: str, p: Plan, cfg: Config, forced: list[Repo] | None, deep: bool, cache: dict | None) -> Plan:
    """Second pass (see ``verify.py``): chosen repos whose files do not mention the prompt are only listed."""
    if forced is not None or not deep:
        return p
    kept, dropped = verify(prompt, p.opened, cfg, cache)
    if not dropped:
        return p
    listed = [Related(c.repo, c.score, why, opens=False) for c, why in dropped] + p.related
    return Plan(kept, listed[: max(cfg.max_related_list, len(dropped))])


def plan(
    prompt: str,
    repos: list[Repo],
    cfg: Config,
    related: str = "auto",
    forced: list[Repo] | None = None,
    deep: bool = True,
    cache: dict | None = None,
) -> Plan:
    """Seeds, then their related repos.

    Seeds are ``forced`` repos (``-r``) when given, else the repos the prompt names or matches.
    ``related``: ``auto`` opens a neighbor when the *rest* of the prompt (what is left once the seeds' own
    names are set aside) also matches what that neighbor contains, ``all`` opens the strongest neighbors
    regardless, ``none`` ignores relations. No word list decides this: the neighbor's own files and names do.

    ``deep=False`` skips the two passes that read file contents (identifier search, verification): instant,
    for the launcher while you type. ``cache`` is passed to them so that repeated calls reuse answers.
    """
    if forced is not None:
        seeds = [Choice(r, NAME_BONUS, "requested", kind="named") for r in forced]
    else:
        seeds = with_hits(select(prompt, repos, cfg), find_hits(prompt, repos, cfg, cache) if deep else {}, repos, cfg)
    if not seeds or related == "none":
        return _verified(prompt, Plan(seeds, []), cfg, forced, deep, cache)
    seed_names = {s for seed in seeds for s in stems(seed.repo.name)}
    affinity = {c.repo.path: c.score for c in rank(prompt, repos, ignore=seed_names)}   # a link is already evidence: one word completes it
    opened = list(seeds)
    listed: list[Related] = []
    budget = cfg.max_related_open
    for repo, weight, why in neighbors([s.repo for s in seeds], repos, cfg):
        takes = related == "all" or affinity.get(repo.path, 0.0) > 0
        if takes and budget > 0:
            budget -= 1
            reason = "related: " + why + (" (the rest of the prompt matches it)" if related == "auto" else "")
            opened.append(Choice(repo, weight, reason, kind="related" if related == "auto" else "asked"))
        elif len(listed) < cfg.max_related_list:
            listed.append(Related(repo, weight, why, opens=False))
    return _verified(prompt, Plan(opened, listed), cfg, forced, deep, cache)
