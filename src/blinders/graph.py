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
from .gitsync import graph_state, hide_graph_output
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


def cluster_command(cfg: Config, repo: Repo) -> list[str]:
    """``extract`` writes graph.json only; ``cluster-only`` writes GRAPH_REPORT.md.
    ``--no-label`` keeps community names local: without it Graphify may call an LLM backend."""
    return [cfg.graphify_bin, "cluster-only", repo.path, "--no-label", "--no-viz"]


def _run(cmd: list[str], timeout: int) -> str:
    """Empty string on success, else a short error."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"timeout after {timeout}s"
    if proc.returncode == 0:
        return ""
    tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
    return tail[0][:160] or f"exit {proc.returncode}"


def graph_one(repo: Repo, cfg: Config, update: bool = False, dry_run: bool = False) -> GraphResult:
    """Missing graph: extract + cluster. Stale graph: update. Fresh graph: skipped unless ``update`` forces a refresh."""
    state = graph_state(repo.graph_report, repo.path)
    refresh = state != "none" and (update or state == "stale")
    if state != "none" and not refresh:
        return GraphResult(repo, "skipped", "graph is current (use --update to force)")
    cmds = [graph_command(cfg, repo, update=True)] if refresh else [graph_command(cfg, repo, update=False), cluster_command(cfg, repo)]
    if dry_run:
        return GraphResult(repo, "skipped", "dry run: " + " && ".join(" ".join(c) for c in cmds))
    hide_graph_output(repo.path)
    for cmd in cmds:
        error = _run(cmd, cfg.graph_timeout)
        if error:
            return GraphResult(repo, "failed", error)
    return GraphResult(repo, "updated" if refresh else "built")


def build_graphs(repos: list[Repo], cfg: Config, update: bool = False, dry_run: bool = False, log=print) -> list[GraphResult]:
    """Sequential builds with a log line per repo (``blind graph``)."""
    if not dry_run and shutil.which(cfg.graphify_bin) is None:
        raise FileNotFoundError(f"'{cfg.graphify_bin}' not found in PATH (install: uv tool install graphifyy)")
    results: list[GraphResult] = []
    for i, repo in enumerate(repos, 1):
        res = graph_one(repo, cfg, update=update, dry_run=dry_run)
        results.append(res)
        if res.status == "skipped" and not dry_run:
            log(f"[{i}/{len(repos)}] {repo.name}: skipped ({graph_state(repo.graph_report, repo.path)})")
        elif dry_run:
            log(f"[{i}/{len(repos)}] {res.detail.removeprefix('dry run: ')}" if res.detail.startswith("dry run: ") else f"[{i}/{len(repos)}] {repo.name}: skipped")
        else:
            log(f"[{i}/{len(repos)}] {repo.name}: {res.status}")
    return results
