"""Pick which MCP servers a session needs, from the user's own MCP config.

Every MCP server adds its tool schemas to the context. Servers are matched to the prompt by
name, command/args/url, an optional ``description``, and ``[mcp.keywords]`` from config.toml.
Servers listed in ``[mcp] always`` are always kept. Nothing is ever deleted from the user's config:
filtering happens per launch through the CLI's own options.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .text import tokens, ubiquitous

MIN_SERVERS_FOR_UBIQUITY = 3   # below this, "found in most servers" says nothing


@dataclass
class McpServer:
    name: str
    config: dict
    terms: set[str] = field(default_factory=set)


@dataclass
class McpPlan:
    kept: list[McpServer]
    dropped: list[McpServer]
    reasons: dict[str, str]


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _terms(name: str, conf: dict, extra: list[str]) -> set[str]:
    parts = [name, str(conf.get("command", "")), str(conf.get("url", "")),
             str(conf.get("httpUrl", "")), str(conf.get("description", ""))]
    parts += [str(a) for a in conf.get("args", []) if isinstance(a, str)]
    parts += extra
    return set(tokens(" ".join(parts)))


def discover(style: str, home: Path, cfg: Config) -> list[McpServer]:
    """User-level MCP servers. ``style`` is the adapter's mcp_style: ``allowlist`` (Gemini) or ``config`` (Claude)."""
    if style == "allowlist":
        servers = _load(home / ".gemini" / "settings.json").get("mcpServers")
    elif style == "config":
        servers = _load(home / ".claude.json").get("mcpServers")
    else:
        return []
    if not isinstance(servers, dict):
        return []
    return [
        McpServer(name, conf if isinstance(conf, dict) else {}, _terms(name, conf if isinstance(conf, dict) else {}, cfg.mcp_keywords.get(name, [])))
        for name, conf in sorted(servers.items())
    ]


def select_mcp(prompt: str, servers: list[McpServer], cfg: Config, spec: str = "auto") -> McpPlan:
    """``spec``: ``auto`` (match the prompt), ``all``, ``none``, or comma-separated server names."""
    reasons: dict[str, str] = {}
    if spec == "all":
        return McpPlan(list(servers), [], {s.name: "all requested" for s in servers})
    if spec == "none":
        kept = [s for s in servers if s.name in cfg.mcp_always]
        return McpPlan(kept, [s for s in servers if s not in kept], {s.name: "always" for s in kept})
    if spec != "auto":
        wanted = {n.strip() for n in spec.split(",") if n.strip()}
        kept = [s for s in servers if s.name in wanted or s.name in cfg.mcp_always]
        return McpPlan(kept, [s for s in servers if s not in kept], {s.name: "requested" for s in kept})
    # Launcher plumbing (npx, uvx, docker, mcp...) is what most of YOUR servers share: learned, not listed.
    q = set(tokens(prompt)) - ubiquitous((s.terms for s in servers), MIN_SERVERS_FOR_UBIQUITY)
    kept = []
    for s in servers:
        if s.name in cfg.mcp_always:
            kept.append(s)
            reasons[s.name] = "always"
            continue
        hits = sorted(q & s.terms)
        if hits:
            kept.append(s)
            reasons[s.name] = "prompt mentions " + ", ".join(hits[:3])
    return McpPlan(kept, [s for s in servers if s not in kept], reasons)


def missing_names(spec: str, servers: list[McpServer]) -> list[str]:
    """Names in an explicit ``--mcp a,b`` that match no configured server."""
    if spec in ("auto", "all", "none"):
        return []
    known = {s.name for s in servers}
    return [n.strip() for n in spec.split(",") if n.strip() and n.strip() not in known]


def claude_config(plan: McpPlan) -> dict:
    return {"mcpServers": {s.name: s.config for s in plan.kept}}
