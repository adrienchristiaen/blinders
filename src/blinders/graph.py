"""Optional Graphify integration: build a code graph per repo, locally, with no LLM.

Commands match ``graphify --help`` (graphifyy on PyPI):

- first build:  ``graphify extract <path> --code-only --global --as <name>``
  (``--code-only`` = local AST, no API key; ``--global`` also merges the repo into
  ``~/.graphify/global-graph.json``, the cross-repo graph)
- refresh:      ``graphify update <path>``  (no LLM needed)

Graphs are never required: blinders only reads ``graphify-out/GRAPH_REPORT.md`` when it exists.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from .config import Config
from .scan import Repo


@dataclass
class GraphResult:
    repo: Repo
    status: str  # built | updated | skipped | failed
    detail: str = ""


def graph_command(cfg: Config, repo: Repo, update: bool) -> list[str]:
    if update:
        return [cfg.graphify_bin, "update", repo.path]
    return [cfg.graphify_bin, "extract", repo.path, "--code-only", "--global", "--as", repo.name]


def build_graphs(repos: list[Repo], cfg: Config, update: bool = False, dry_run: bool = False, log=print) -> list[GraphResult]:
    if not dry_run and shutil.which(cfg.graphify_bin) is None:
        raise FileNotFoundError(f"'{cfg.graphify_bin}' not found in PATH (install: uv tool install graphifyy)")
    results: list[GraphResult] = []
    for i, repo in enumerate(repos, 1):
        has_graph = bool(repo.graph_report)
        if has_graph and not update:
            results.append(GraphResult(repo, "skipped", "graph exists (use --update to refresh)"))
            log(f"[{i}/{len(repos)}] {repo.name}: skipped (graph exists)")
            continue
        cmd = graph_command(cfg, repo, update=update and has_graph)
        if dry_run:
            log(f"[{i}/{len(repos)}] {' '.join(cmd)}")
            results.append(GraphResult(repo, "skipped", "dry run"))
            continue
        log(f"[{i}/{len(repos)}] {repo.name}: {'update' if update and has_graph else 'extract'} ...")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.graph_timeout)
        except subprocess.TimeoutExpired:
            results.append(GraphResult(repo, "failed", f"timeout after {cfg.graph_timeout}s"))
            continue
        if proc.returncode == 0:
            results.append(GraphResult(repo, "updated" if update and has_graph else "built"))
        else:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
            results.append(GraphResult(repo, "failed", tail[0][:160]))
    return results
