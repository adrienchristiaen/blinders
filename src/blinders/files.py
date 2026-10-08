"""One way to walk a repo, shared by everything that reads file names or small files."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

# Folders that hold dependencies, caches or build output: never describe a repo.
SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "target", "dist", "build",
    "__pycache__", ".cache", ".gradle", ".idea", "vendor",
    "graphify-out",   # the code-graph tool's own output, not part of the repo
}
YAML_SUFFIXES = (".yml", ".yaml")
YAML_NAME = re.compile(r"^\s*-?\s*name:\s*['\"]?([A-Za-z0-9_.\-]+)", re.M)
YAML_HEAD_BYTES = 4096


def declared_names(file: Path) -> list[str]:
    """``name:`` entries of a small YAML file (dbt models, sources and tables, Helm values, CI jobs...)."""
    try:
        with file.open("r", encoding="utf-8", errors="ignore") as fh:
            return YAML_NAME.findall(fh.read(YAML_HEAD_BYTES))
    except OSError:
        return []


def iter_files(root: Path, max_depth: int, limit: int) -> Iterator[Path]:
    """Files under ``root``, shallowest first (then by name), at most ``limit`` of them and ``max_depth``
    folders deep. Build and deploy files sit near the top of a repo, so this reaches them first."""
    level = [root]
    seen = 0
    for depth in range(max_depth + 1):
        next_level: list[Path] = []
        for directory in level:
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in SKIP_DIRS and depth < max_depth:
                        next_level.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
                    seen += 1
                    if seen >= limit:
                        return
        level = next_level
