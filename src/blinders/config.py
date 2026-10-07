"""Configuration and paths.

Config lives in ``$BLINDERS_CONFIG_DIR`` (default ``~/.config/blinders``) as
``config.toml``. Cache (repo index, session workspaces) lives in
``$BLINDERS_CACHE_DIR`` (default ``~/.cache/blinders``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: same API, installed as a conditional dependency
    import tomli as tomllib

DEFAULT_MAP_GLOBS = ["graphify-out/*.md", ".blinders/*.md"]


def config_dir() -> Path:
    return Path(os.environ.get("BLINDERS_CONFIG_DIR", "~/.config/blinders")).expanduser()


def cache_dir() -> Path:
    return Path(os.environ.get("BLINDERS_CACHE_DIR", "~/.cache/blinders")).expanduser()


@dataclass
class Config:
    roots: list[str] = field(default_factory=list)
    scan_depth: int = 3
    max_repos: int = 3
    relative_threshold: float = 0.4
    index_ttl_hours: float = 24.0
    index_max_closed: int = 40
    session_ttl_days: int = 7
    map_globs: list[str] = field(default_factory=lambda: list(DEFAULT_MAP_GLOBS))
    adapters: dict[str, dict] = field(default_factory=dict)
    # related repos
    max_related_list: int = 5      # related-but-closed repos shown in the index
    max_related_open: int = 2      # related repos opened automatically (intent match or --related)
    groups: dict[str, list[str]] = field(default_factory=dict)  # explicit "these repos belong together"
    # MCP servers
    mcp_always: list[str] = field(default_factory=list)          # always kept
    mcp_keywords: dict[str, list[str]] = field(default_factory=dict)  # extra match words per server
    # skills
    skills_always: list[str] = field(default_factory=list)
    skills_keywords: dict[str, list[str]] = field(default_factory=dict)
    max_skills: int = 5            # skills kept visible when matching the prompt
    # graphify
    graphify_bin: str = "graphify"
    graph_timeout: int = 900
    # interactive entry point
    default_cli: str = ""          # used by a bare `blind`; empty = ask or autodetect

    @property
    def root_paths(self) -> list[Path]:
        return [Path(r).expanduser() for r in self.roots]


def load_config(path: Path | None = None) -> Config:
    path = path or (config_dir() / "config.toml")
    cfg = Config()
    if not path.is_file():
        return cfg
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    for key in (
        "roots",
        "scan_depth",
        "max_repos",
        "relative_threshold",
        "index_ttl_hours",
        "index_max_closed",
        "session_ttl_days",
        "map_globs",
        "max_related_list",
        "max_related_open",
        "groups",
        "graphify_bin",
        "graph_timeout",
        "max_skills",
        "default_cli",
    ):
        if key in data:
            setattr(cfg, key, data[key])
    cfg.adapters = dict(data.get("adapters", {}))
    mcp = data.get("mcp", {})
    cfg.mcp_always = list(mcp.get("always", []))
    cfg.mcp_keywords = {k: list(v) for k, v in mcp.get("keywords", {}).items()}
    skills = data.get("skills", {})
    cfg.skills_always = list(skills.get("always", []))
    cfg.skills_keywords = {k: list(v) for k, v in skills.get("keywords", {}).items()}
    return cfg
