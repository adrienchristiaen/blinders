"""Which model to start a CLI with, and which models that CLI really offers.

blind never guesses a model from the words of a prompt: that needs word lists that only fit one
language and one way of working. The model is what you set: ``--model <name>``, the launcher's selector, or
``[models.<cli>] default = "<name>"`` in the config. Without any of them the CLI keeps its own choice.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from .config import Config, cache_dir


def resolve_model(cli: str, spec: str | None, cfg: Config) -> str | None:
    """Name to pass to the CLI, or ``None`` to leave the CLI alone.

    ``spec`` is the ``--model`` value: a model name, ``default`` (hands off), or nothing (use the config)."""
    spec = (spec or "").strip()
    if spec == "default":
        return None
    if spec:
        return spec
    return cfg.models.get(cli, {}).get("default") or None


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
