"""Configuration and paths.

Config lives in ``$BLINDERS_CONFIG_DIR`` (default ``~/.config/blinders``) as
``config.toml``. Cache (repo index, session workspaces) lives in
``$BLINDERS_CACHE_DIR`` (default ``~/.cache/blinders``).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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
    ):
        if key in data:
            setattr(cfg, key, data[key])
    cfg.adapters = dict(data.get("adapters", {}))
    return cfg
