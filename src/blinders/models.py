"""Pick a model tier for a launch from the prompt and the repos, locally (no tokens spent).

Three tiers: ``light`` (questions, lookups), ``standard`` (the CLI's own default, so no flag is
passed) and ``strong`` (design, debugging across repos, big refactors). Only ``light`` and
``strong`` change anything. Model names come from config (``[models.<cli>]``); the defaults are
the aliases each CLI documents (Gemini ``flash-lite`` / ``pro``, Claude ``haiku`` / ``opus``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Config, cache_dir
from .text import words

# Only "strong" has a default: the aliases resolve to the best model your account can use. There is no
# default "light" model: in a real comparison flash-lite answered less thoroughly and used more requests.
# Set one yourself in [models.<cli>] light = "..." if it suits you (see `blind models`).
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "gemini": {"strong": "pro"},
    "claude": {"strong": "opus"},
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
# Strong words that ask for work rather than an answer: never "light", even phrased as a question.
STRONG_ACTIONS = {
    "refactor", "refactoring", "refactorer", "migrate", "migrer", "redesign", "debug", "debugger",
    "deboguer", "optimiser", "optimize", "concevoir",
}
QUESTION_STARTS = {
    "comment", "pourquoi", "quoi", "est", "what", "where", "how", "why", "which", "who", "does", "is", "are",
    "peux", "pouvez", "can", "could", "pourrais", "ou", "quel", "quelle", "quels", "quelles", "combien", "qui",
}
LIGHT_WORDS = {
    "explique", "expliquer", "explain", "where", "ou", "quel", "quelle", "quels", "quelles", "what", "list",
    "liste", "lister", "montre", "show", "find", "trouve", "trouver", "resume", "resumer", "summarize",
    "combien", "lis", "read", "which", "who", "qui", "dis", "tell", "dire", "savoir", "indique", "montrer",
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
    edits = toks & (EDIT_WORDS | STRONG_ACTIONS)
    asks = toks & LIGHT_WORDS
    first = text.split()[0] if text else ""
    question = "?" in prompt or first in QUESTION_STARTS or bool(asks)
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
    if question and not edits and len(prompt) <= SHORT_PROMPT and opened <= 2 and related <= 2:
        return "light", f"short question, {opened} repo(s)"
    blockers = []
    if edits:
        blockers.append(f"asks for work ({sorted(edits)[0]})")
    if not question:
        blockers.append("not phrased as a question")
    if len(prompt) > SHORT_PROMPT:
        blockers.append("long prompt")
    if opened > 2 or related > 2:
        blockers.append(f"{opened} repo(s) opened, {related} related")
    return "standard", "; ".join(blockers) or "default"


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
    if not model:   # nothing configured for this tier: say so, pass nothing
        return ModelChoice(tier, f"{why}; no {tier} model set in [models.{cli}]", None)
    return ModelChoice(tier, why, model)


# --- the models your CLI really offers ---------------------------------------------------------

GEMINI_ALIASES = (
    ("auto", "Gemini's own router picks per prompt (it spends an extra small-model call)"),
    ("pro", "alias: the best Pro model your account can use"),
    ("flash", "alias: the latest Flash model"),
    ("flash-lite", "alias: the latest Flash-Lite model"),
)
CLAUDE_ALIASES = (("opus", "alias"), ("sonnet", "alias"), ("haiku", "alias"))


def _gemini_bundle_dir() -> Path | None:
    exe = shutil.which("gemini")
    if not exe:
        return None
    here = Path(os.path.realpath(exe)).parent
    for d in (here, *here.parents[:4]):
        if (d / "bundle").is_dir():
            return d / "bundle"
        if d.name == "bundle":
            return d
    return None


def cli_models(cli: str, home: Path) -> list[tuple[str, str]]:
    """(model, note) the launcher offers for a CLI; empty when blind knows nothing about its models."""
    if cli == "gemini":
        return gemini_model_list(home)
    if cli == "claude":
        return list(CLAUDE_ALIASES)
    return []


def installed_gemini_ids() -> list[str]:
    """Model ids this Gemini CLI version knows (its VALID_GEMINI_MODELS list), read from the installed
    package. Which of them your account may use is decided by Google, not listed here."""
    bundle = _gemini_bundle_dir()
    if bundle is None:
        return []
    cache = cache_dir() / "gemini-model-ids.json"
    stamp = f"{bundle}:{max((f.stat().st_mtime for f in bundle.glob('*.js')), default=0)}"
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if saved.get("stamp") == stamp:
            return list(saved.get("ids", []))
    except (OSError, ValueError):
        pass
    ids: list[str] = []
    for f in sorted(bundle.glob("*.js")):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = re.search(r"VALID_GEMINI_MODELS\s*=\s*(?:/\*[^*]*\*/\s*)?new Set\(\[(.*?)\]\)", text, re.S)
        if not m:
            continue
        consts = dict(re.findall(r'\b(?:var|const|let)\s+([A-Z0-9_]+)\s*=\s*"([^"]+)"', text))
        for k, v in re.findall(r"\b(?:var|const|let)\s+([A-Z0-9_]+)\s*=\s*([A-Z0-9_]+);", text):
            consts.setdefault(k, consts.get(v, ""))
        for name in (n.strip() for n in m.group(1).split(",")):
            value = consts.get(name, "")
            if value and value != "none" and value not in ids:
                ids.append(value)
        break
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"stamp": stamp, "ids": ids}), encoding="utf-8")
    except OSError:
        pass
    return ids


def settings_model(home: Path) -> str:
    """The model set in ~/.gemini/settings.json (``model.name``), or ''."""
    from .geminihome import load_settings
    data = load_settings(home / ".gemini" / "settings.json") or {}
    model = data.get("model")
    return str(model.get("name") or "") if isinstance(model, dict) else (str(model) if isinstance(model, str) else "")


def recent_gemini_models(home: Path, files: int = 40) -> dict[str, int]:
    """Models that answered in your latest Gemini chats (counted from ``~/.gemini/tmp/*/chats/*.json``)."""
    chats = sorted((home / ".gemini" / "tmp").glob("*/chats/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:files]
    counts: dict[str, int] = {}
    for f in chats:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for msg in data.get("messages", []) if isinstance(data, dict) else []:
            model = msg.get("model") if isinstance(msg, dict) else None
            if isinstance(model, str) and model:
                counts[model] = counts.get(model, 0) + 1
    return counts


def gemini_model_list(home: Path) -> list[tuple[str, str]]:
    """(model id, note) for the launcher and ``blind models``: your default, recently used, aliases, then the rest."""
    default = settings_model(home)
    recent = recent_gemini_models(home)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(model: str, note: str) -> None:
        if model and model not in seen:
            seen.add(model)
            out.append((model, note))

    if default:
        add(default, "your default in settings.json")
    for model, n in sorted(recent.items(), key=lambda kv: -kv[1]):
        add(model, f"used in your recent chats ({n} replies)")
    for alias, note in GEMINI_ALIASES:
        add(alias, note)
    for model in installed_gemini_ids():
        add(model, "known to this Gemini CLI; access depends on your account")
    return out
