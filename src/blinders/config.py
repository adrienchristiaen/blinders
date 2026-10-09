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
    # sync (git) and hints
    root_branches: list[str] = field(default_factory=lambda: ["main", "master", "develop"])
    sync_on_launch: str = "safe"   # off | safe (pull only repos already on their root branch) | switch (check it out too)
    sync_timeout: int = 60
    sync_workers: int = 8
    sync_fleet: str = "switch"     # step 1 of a bare `blind`: off | safe | switch
    sync_ttl_minutes: float = 30   # skip step 1 when the repos were synced less than this long ago
    graph_workers: int = 2         # graphs built in parallel in step 2
    hints_enabled: bool = True
    hints_max_files: int = 4
    # exact identifiers of the prompt searched in every repo (no tokens)
    grep_enabled: bool = True
    grep_max_literals: int = 8
    # check in the files of the chosen repos that the prompt's words are really there
    verify_enabled: bool = True
    # interactive entry point
    default_cli: str = ""          # used by a bare `blind`; empty = ask or autodetect
    models: dict[str, dict[str, str]] = field(default_factory=dict)  # per CLI: {"default": "<model>"}
    # Gemini only applies workspace settings in a trusted folder; the blind workspace is ours
    gemini_trust_workspace: bool = True
    # a per-session Gemini home: only the kept skills, extensions and MCP servers, no includeDirectories
    gemini_isolate_home: bool = True
    gemini_keep_global_memory: bool = True   # keep ~/.gemini/GEMINI.md
    gemini_extensions: str = "auto"          # auto | all | none
    gemini_extensions_always: list[str] = field(default_factory=list)
    gemini_tool_output_chars: int = 12000    # cut larger tool outputs (Gemini's default is 40000); 0 = leave it
    gemini_rtk: bool = True                  # use rtk's Gemini hook in the session when rtk is installed

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
        "graph_workers",
        "default_cli",
    ):
        if key in data:
            setattr(cfg, key, data[key])
    cfg.adapters = dict(data.get("adapters", {}))
    mcp = data.get("mcp", {})
    cfg.mcp_always = list(mcp.get("always", []))
    cfg.mcp_keywords = {k: list(v) for k, v in mcp.get("keywords", {}).items()}
    sync = data.get("sync", {})
    for key, attr in (("on_launch", "sync_on_launch"), ("fleet", "sync_fleet"), ("ttl_minutes", "sync_ttl_minutes"), ("timeout", "sync_timeout"), ("workers", "sync_workers"),
                      ("root_branches", "root_branches")):
        if key in sync:
            setattr(cfg, attr, sync[key])
    hints = data.get("hints", {})
    cfg.hints_enabled = bool(hints.get("enabled", cfg.hints_enabled))
    cfg.hints_max_files = int(hints.get("max_files", cfg.hints_max_files))
    models = data.get("models", {})
    cfg.models = {k: {t: str(m) for t, m in v.items()} for k, v in models.items() if isinstance(v, dict)}
    gem = data.get("gemini", {})
    cfg.gemini_trust_workspace = bool(gem.get("trust_workspace", cfg.gemini_trust_workspace))
    cfg.gemini_isolate_home = bool(gem.get("isolate_home", cfg.gemini_isolate_home))
    cfg.gemini_keep_global_memory = bool(gem.get("global_memory", cfg.gemini_keep_global_memory))
    cfg.gemini_extensions = str(gem.get("extensions", cfg.gemini_extensions))
    cfg.gemini_extensions_always = list(gem.get("extensions_always", cfg.gemini_extensions_always))
    cfg.gemini_tool_output_chars = int(gem.get("tool_output_chars", cfg.gemini_tool_output_chars))
    cfg.gemini_rtk = bool(gem.get("rtk", cfg.gemini_rtk))
    grep = data.get("grep", {})
    cfg.grep_enabled = bool(grep.get("enabled", cfg.grep_enabled))
    cfg.grep_max_literals = int(grep.get("max_literals", cfg.grep_max_literals))
    cfg.verify_enabled = bool(data.get("verify", {}).get("enabled", cfg.verify_enabled))
    skills = data.get("skills", {})
    cfg.skills_always = list(skills.get("always", []))
    cfg.skills_keywords = {k: list(v) for k, v in skills.get("keywords", {}).items()}
    return cfg
