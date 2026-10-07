"""Pick which user-level skills a session keeps visible.

Every installed skill puts its name and description in the context at startup. Skills are
matched to the prompt by name and description words (rarer words weigh more), plus
``[skills] always`` and ``[skills.keywords]`` from config.toml. Dropped skills are hidden from
the model, never deleted: the filter is applied per launch through the CLI's own settings.

Only user-level skills are discovered (``~/.claude/skills``, ``~/.gemini/skills``,
``~/.agents/skills``). Skills that come from plugins or extensions are not touched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from .audit import _skill_description
from .config import Config
from .text import tokens

# Directories read per adapter ``skills_style`` family.
SKILL_DIRS = {
    "claude": (".claude/skills",),
    "gemini": (".gemini/skills", ".agents/skills"),
}

NAME_WEIGHT = 3.0
MIN_DESC_HITS = 2


@dataclass
class Skill:
    name: str
    description: str
    name_terms: set[str] = field(default_factory=set)  # name words and [skills.keywords]: strong signals
    terms: set[str] = field(default_factory=set)


@dataclass
class SkillPlan:
    kept: list[Skill]
    dropped: list[Skill]
    reasons: dict[str, str]


def discover(family: str, home: Path, cfg: Config) -> list[Skill]:
    """User-level skills for a CLI family (``claude`` or ``gemini``). Unknown family: none."""
    skills: dict[str, Skill] = {}
    for rel in SKILL_DIRS.get(family, ()):
        root = home / rel
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            md = child / "SKILL.md"
            if child.name in skills or not child.is_dir() or not md.is_file():
                continue
            desc = _skill_description(md)
            extra = " ".join(cfg.skills_keywords.get(child.name, []))
            skills[child.name] = Skill(
                child.name, desc,
                name_terms=set(tokens(f"{child.name} {extra}")),
                terms=set(tokens(desc)),
            )
    return sorted(skills.values(), key=lambda s: s.name)


def select_skills(prompt: str, skills: list[Skill], cfg: Config, spec: str = "auto") -> SkillPlan:
    """``spec``: ``auto`` (match the prompt), ``all``, ``none``, or comma-separated skill names."""
    if spec == "all":
        return SkillPlan(list(skills), [], {s.name: "all requested" for s in skills})
    always = set(cfg.skills_always)
    if spec == "none":
        kept = [s for s in skills if s.name in always]
        return SkillPlan(kept, [s for s in skills if s not in kept], {s.name: "always" for s in kept})
    if spec != "auto":
        wanted = {n.strip() for n in spec.split(",") if n.strip()} | always
        kept = [s for s in skills if s.name in wanted]
        return SkillPlan(kept, [s for s in skills if s not in kept], {s.name: "requested" for s in kept})

    q = set(tokens(prompt))
    df: dict[str, int] = {}
    for s in skills:
        for t in s.terms | s.name_terms:
            df[t] = df.get(t, 0) + 1
    n = max(len(skills), 1)

    def idf(t: str) -> float:
        return math.log(1 + n / df.get(t, 1))

    scored: list[tuple[float, Skill, str]] = []
    reasons: dict[str, str] = {}
    kept: list[Skill] = []
    for s in skills:
        if s.name in always:
            kept.append(s)
            reasons[s.name] = "always"
            continue
        name_hits = q & s.name_terms
        desc_hits = (q & s.terms) - name_hits
        if not name_hits and len(desc_hits) < MIN_DESC_HITS:
            continue
        score = sum(NAME_WEIGHT * idf(t) for t in name_hits) + sum(idf(t) for t in desc_hits)
        hits = sorted(name_hits | desc_hits)
        scored.append((score, s, "prompt mentions " + ", ".join(hits[:3])))
    scored.sort(key=lambda x: (-x[0], x[1].name))
    for _, s, why in scored[: cfg.max_skills]:
        kept.append(s)
        reasons[s.name] = why
    kept.sort(key=lambda s: s.name)
    return SkillPlan(kept, [s for s in skills if s not in kept], reasons)


def missing_names(spec: str, skills: list[Skill]) -> list[str]:
    if spec in ("auto", "all", "none"):
        return []
    known = {s.name for s in skills}
    return [n.strip() for n in spec.split(",") if n.strip() and n.strip() not in known]


def claude_settings(plan: SkillPlan) -> dict:
    """``--settings`` payload. ``user-invocable-only`` hides a skill from the model (no startup tokens)
    but keeps it usable by hand with ``/name``."""
    return {"skillOverrides": {s.name: "user-invocable-only" for s in plan.dropped}}


def gemini_settings(plan: SkillPlan) -> dict:
    """Workspace ``.gemini/settings.json`` payload, written inside the blind workspace."""
    return {"skills": {"disabled": [s.name for s in plan.dropped]}}
