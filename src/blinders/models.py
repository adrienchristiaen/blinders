"""Pick a model tier for a launch from the prompt and the repos, locally (no tokens spent).

Three tiers: ``light`` (questions, lookups), ``standard`` (the CLI's own default, so no flag is
passed) and ``strong`` (design, debugging across repos, big refactors). Only ``light`` and
``strong`` change anything. Model names come from config (``[models.<cli>]``); the defaults are
the aliases each CLI documents (Gemini ``flash-lite`` / ``pro``, Claude ``haiku`` / ``opus``).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .text import words

DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "gemini": {"light": "flash-lite", "strong": "pro"},
    "claude": {"light": "haiku", "strong": "opus"},
}
TIERS = ("light", "standard", "strong")

# Whole words (accents folded, lowercase). French and English.
STRONG_WORDS = {
    "architecture", "refactor", "refactoring", "refactorer", "migrate", "migration", "migrer", "redesign",
    "design", "concevoir", "conception", "debug", "debugger", "deboguer", "investigate", "investiguer",
    "diagnostic", "diagnose", "audit", "securite", "security", "concurrency", "concurrence", "performance",
    "optimiser", "optimize", "lineage", "transverse",
}
STRONG_PHRASES = ("root cause", "cause racine", "bout en bout", "end to end", "race condition", "trade off")
EDIT_WORDS = {
    "implement", "implemente", "implementer", "add", "ajoute", "ajouter", "fix", "corrige", "corriger",
    "write", "ecris", "ecrire", "create", "cree", "creer", "modifie", "modifier", "supprime", "supprimer",
    "delete", "remove", "change", "update", "deploy", "deploie", "renomme", "rename", "genere", "generate",
    "build", "construis", "commit", "push",
}
LIGHT_WORDS = {
    "explique", "expliquer", "explain", "where", "ou", "quel", "quelle", "quels", "quelles", "what", "list",
    "liste", "lister", "montre", "show", "find", "trouve", "trouver", "resume", "resumer", "summarize",
    "combien", "lis", "read", "which", "who", "qui", "dis", "tell",
}
LONG_PROMPT = 1200
SHORT_PROMPT = 400


@dataclass
class ModelChoice:
    tier: str            # light | standard | strong
    reason: str
    model: str | None    # name passed to the CLI; None leaves the CLI's default alone


def models_for(cli: str, cfg: Config) -> dict[str, str]:
    merged = dict(DEFAULT_MODELS.get(cli, {}))
    merged.update({t: m for t, m in cfg.models.get(cli, {}).items() if m})
    return merged


def classify(prompt: str, opened: int, related: int = 0) -> tuple[str, str]:
    """(tier, reason). Conservative: ``standard`` unless the signals are clear."""
    text = " ".join(words(prompt))
    toks = set(text.split())
    strong = sorted(toks & STRONG_WORDS) + [p for p in STRONG_PHRASES if p in text]
    edits = toks & EDIT_WORDS
    asks = toks & LIGHT_WORDS
    score = 2 * len(strong)
    if opened >= 3:
        score += 1
    if opened + related >= 4:
        score += 1
    if len(prompt) > LONG_PROMPT:
        score += 1
    if score >= 3:
        why = ", ".join(strong[:3]) or "large request"
        return "strong", f"{why}; {opened} repo(s) opened"
    if not strong and not edits and asks and len(prompt) <= SHORT_PROMPT and opened <= 1:
        return "light", f"short question ({', '.join(sorted(asks)[:2])}), {opened} repo(s)"
    return "standard", "default"


def choose_model(cli: str, prompt: str, opened: int, related: int, cfg: Config, spec: str | None = None) -> ModelChoice | None:
    """``spec``: ``auto`` (default), ``default`` (leave the CLI alone), a tier, or an explicit model name.
    ``None`` when nothing should be passed to the CLI."""
    spec = (spec or "auto").strip()
    if spec == "default" or (spec == "auto" and not cfg.models_auto):
        return None
    table = models_for(cli, cfg)
    if spec in TIERS:
        tier, why = spec, "requested"
    elif spec == "auto":
        if not prompt.strip():
            return None
        tier, why = classify(prompt, opened, related)
    else:
        return ModelChoice("custom", "requested", spec)
    if tier == "standard":
        return ModelChoice("standard", why, table.get("standard"))
    model = table.get(tier)
    if not model:
        return None   # no known name for this tier with this CLI: leave it alone
    return ModelChoice(tier, why, model)
