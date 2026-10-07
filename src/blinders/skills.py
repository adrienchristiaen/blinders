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

# Directories read per adapter ``skills_style`` family (relative to the home directory).
SKILL_DIRS = {
    "claude": (".claude/skills",),
    "gemini": (".gemini/skills", ".agents/skills"),
}
# Gemini CLI extensions bundle skills at <extension>/skills/<skill>/SKILL.md (documented layout).
EXTENSION_DIRS = {"gemini": ".gemini/extensions"}

NAME_WEIGHT = 3.0
MIN_DESC_HITS = 2


@dataclass
class Skill:
    name: str
    description: str
    source: str = ""     # "" for user skills, "extension <name>" for skills bundled in an extension
    name_terms: set[str] = field(default_factory=set)  # name words and [skills.keywords]: strong signals
    terms: set[str] = field(default_factory=set)


@dataclass
class SkillPlan:
    kept: list[Skill]
    dropped: list[Skill]
    reasons: dict[str, str]


def _frontmatter_name(md: Path) -> str:
    """The skill name the CLI uses (frontmatter ``name``), which can differ from the directory name."""
    try:
        text = md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    for line in text.split("---", 2)[1].splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return ""


def _skill_roots(family: str, home: Path) -> list[tuple[Path, str]]:
    roots = [(home / rel, "") for rel in SKILL_DIRS.get(family, ())]
    ext_dir = EXTENSION_DIRS.get(family)
    if ext_dir and (home / ext_dir).is_dir():
        for ext in sorted((home / ext_dir).iterdir()):
            roots.append((ext / "skills", f"extension {ext.name}"))
    return roots


def discover(family: str, home: Path, cfg: Config) -> list[Skill]:
    """User-level and extension skills for a CLI family (``claude`` or ``gemini``). Unknown family: none.

    Built-in skills that ship with a CLI have no directory to read and are not listed."""
    skills: dict[str, Skill] = {}
    for root, source in _skill_roots(family, home):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            md = child / "SKILL.md"
            if not child.is_dir() or not md.is_file():
                continue
            name = _frontmatter_name(md) or child.name
            if name in skills:
                continue
            desc = _skill_description(md)
            extra = " ".join(cfg.skills_keywords.get(name, []))
            skills[name] = Skill(
                name, desc, source,
                name_terms=set(tokens(f"{name} {extra}")),
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
        # A word shared by half the skills ("tool", "builder") says little about this one.
        distinctive = {t for t in name_hits if df.get(t, 1) <= max(1, n // 2)}
        if not distinctive and len(desc_hits) < MIN_DESC_HITS:
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
