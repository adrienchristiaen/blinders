"""A per-session Gemini home, so a launch loads only what it needs.

Gemini reads, from ``~/.gemini`` and ``~/.agents``: the global ``GEMINI.md``, the ``GEMINI.md`` of every
extension (and its skills and MCP servers), every skill, and the directories listed in
``context.includeDirectories`` of ``settings.json`` (each one brings its own ``GEMINI.md``). Hiding skills
or MCP servers by name does not stop the extension files or the directories. ``GEMINI_CLI_HOME``
(Gemini 0.63: ``homedir()`` returns it) points all of that elsewhere, so blind builds a small home:

- ``settings.json``: your settings, minus ``context.includeDirectories``, with only the kept MCP servers;
- ``skills`` and ``extensions``: symlinks to the kept ones only;
- ``GEMINI.md``: linked unless ``[gemini] global_memory = false``;
- everything else (login, chat history in ``tmp``, trusted folders, custom commands): symlinked, so
  login and ``gemini --resume`` keep working.

Nothing in your real ``~/.gemini`` is modified or deleted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .text import tokens

REPLACED = {"settings.json", "extensions", "skills"}
CONTEXT_DEFAULT = "GEMINI.md"


@dataclass
class Extension:
    name: str
    path: str
    description: str = ""
    skills: set[str] = field(default_factory=set)        # frontmatter names of the skills it bundles
    mcp: set[str] = field(default_factory=set)           # MCP server names it declares
    context_files: list[str] = field(default_factory=list)


@dataclass
class ExtPlan:
    kept: list[Extension]
    dropped: list[Extension]
    reasons: dict[str, str]


def strip_json_comments(text: str) -> str:
    """Gemini accepts // and /* */ comments in settings.json; json.loads does not."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_settings(path: Path) -> dict | None:
    """``{}`` when the file is absent, ``None`` when it exists but cannot be understood."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(strip_json_comments(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _names(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def list_extensions(home: Path) -> list[Extension]:
    from .skills import _frontmatter_name
    root = home / ".gemini" / "extensions"
    found: list[Extension] = []
    if not root.is_dir():
        return found
    for ext in sorted(root.iterdir()):
        manifest = ext / "gemini-extension.json"
        if not ext.is_dir() or not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        info = Extension(str(data.get("name") or ext.name), str(ext), str(data.get("description") or ""))
        info.mcp = set((data.get("mcpServers") or {}).keys()) if isinstance(data.get("mcpServers"), dict) else set()
        info.context_files = _names(data.get("contextFileName")) or [CONTEXT_DEFAULT]
        skills_dir = ext / "skills"
        if skills_dir.is_dir():
            for child in skills_dir.iterdir():
                if (child / "SKILL.md").is_file():
                    info.skills.add(_frontmatter_name(child / "SKILL.md") or child.name)
        found.append(info)
    return found


def select_extensions(prompt: str, exts: list[Extension], skills_plan, mcp_plan, cfg: Config, spec: str = "auto") -> ExtPlan:
    """``spec``: ``auto``, ``all``, ``none``, or names. ``auto`` keeps an extension when it is listed in
    ``[gemini] extensions_always``, bundles a kept skill, declares a kept MCP server, or its name is in the prompt."""
    always = set(cfg.gemini_extensions_always)
    if spec == "all":
        return ExtPlan(list(exts), [], {e.name: "all requested" for e in exts})
    if spec not in ("auto", "none"):
        wanted = {n.strip() for n in spec.split(",") if n.strip()} | always
        kept = [e for e in exts if e.name in wanted]
        return ExtPlan(kept, [e for e in exts if e not in kept], {e.name: "requested" for e in kept})
    kept, reasons = [], {}
    q = set(tokens(prompt))
    kept_skills = {s.name for s in skills_plan.kept} if skills_plan is not None else None
    kept_mcp = {s.name for s in mcp_plan.kept} if mcp_plan is not None else None
    for e in exts:
        why = ""
        if e.name in always:
            why = "always"
        elif spec == "auto":
            if kept_skills is not None and e.skills & kept_skills:
                why = "bundles skill " + sorted(e.skills & kept_skills)[0]
            elif kept_mcp is not None and e.mcp & kept_mcp:
                why = "declares MCP server " + sorted(e.mcp & kept_mcp)[0]
            elif q & set(tokens(e.name)):
                why = "prompt mentions " + sorted(q & set(tokens(e.name)))[0]
        if why:
            kept.append(e)
            reasons[e.name] = why
    return ExtPlan(kept, [e for e in exts if e not in kept], reasons)


def _link(target: Path, source: Path) -> None:
    try:
        target.symlink_to(source, target_is_directory=source.is_dir())
    except OSError:
        pass


def build_home(
    session: Path,
    real_home: Path,
    cfg: Config,
    skills=None,           # list[Skill] kept; None = keep every skill
    extensions=None,       # list[Extension] kept; None = keep every extension
    kept_mcp: set[str] | None = None,
) -> Path | None:
    """Create ``<session>/gemini-home`` and return it, or ``None`` when settings.json cannot be read
    (then the launch continues with the real home, unfiltered)."""
    real = real_home / ".gemini"
    settings = load_settings(real / "settings.json")
    if settings is None:
        return None
    home = session / "gemini-home"
    gem = home / ".gemini"
    gem.mkdir(parents=True, exist_ok=True)

    ctx = settings.get("context")
    if not isinstance(ctx, dict):
        ctx = {}
    settings["context"] = ctx
    ctx.pop("includeDirectories", None)
    settings.pop("includeDirectories", None)
    ctx["memoryBoundaryMarkers"] = []
    if kept_mcp is not None and isinstance(settings.get("mcpServers"), dict):
        settings["mcpServers"] = {k: v for k, v in settings["mcpServers"].items() if k in kept_mcp}
    target = gem / "settings.json"
    target.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    target.chmod(0o600)   # MCP entries may hold tokens

    context_names = set(_names(ctx.get("fileName"))) | {CONTEXT_DEFAULT}
    if real.is_dir():
        for entry in sorted(real.iterdir()):
            if entry.name in REPLACED:
                continue
            if entry.name in context_names and not cfg.gemini_keep_global_memory:
                continue
            _link(gem / entry.name, entry)

    ext_dir = gem / "extensions"
    if extensions is None:
        if (real / "extensions").is_dir():
            _link(ext_dir, real / "extensions")
    else:
        ext_dir.mkdir(exist_ok=True)
        for e in extensions:
            _link(ext_dir / Path(e.path).name, Path(e.path))
        enablement = real / "extensions" / "extension-enablement.json"
        if enablement.is_file():
            (ext_dir / enablement.name).write_bytes(enablement.read_bytes())

    for rel in (".gemini/skills", ".agents/skills"):
        source = real_home / rel
        if not source.is_dir():
            continue
        dest = home / rel
        if skills is None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _link(dest, source)
            continue
        dest.mkdir(parents=True, exist_ok=True)
        for s in skills:
            if s.rel_root == rel and s.path:
                _link(dest / Path(s.path).name, Path(s.path))
    return home


def memory_sources(real_home: Path) -> list[tuple[str, str, int]]:
    """What Gemini would load from the user level: (label, path, estimated tokens = chars / 4)."""
    out: list[tuple[str, str, int]] = []

    def add(label: str, path: Path) -> None:
        try:
            out.append((label, str(path), len(path.read_text(encoding="utf-8", errors="ignore")) // 4))
        except OSError:
            pass

    real = real_home / ".gemini"
    settings = load_settings(real / "settings.json") or {}
    add("global", real / CONTEXT_DEFAULT)
    for e in list_extensions(real_home):
        for name in e.context_files:
            add(f"extension {e.name}", Path(e.path) / name)
    ctx = settings.get("context", {}) if isinstance(settings.get("context"), dict) else {}
    dirs = _names(ctx.get("includeDirectories")) + _names(settings.get("includeDirectories"))
    for d in dirs:
        base = Path(d).expanduser()
        for name in {CONTEXT_DEFAULT, *_names(ctx.get("fileName"))}:
            add(f"includeDirectories {base.name}", base / name)
    return out
