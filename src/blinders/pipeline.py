"""The four steps behind a bare ``blind``, independent of how they are displayed.

1. update   fetch, check out the root branch, fast-forward, for every repo
2. graphs   one Graphify code graph per repo (new, or refreshed when the repo moved)
3. select   which repos, MCP servers and skills this request keeps (interactive, done by the screen)
4. map      starting points inside each chosen repo

Each step reports through ``emit(Event)``; the full-screen launcher and the text mode both consume it.
"""

from __future__ import annotations

import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from .config import Config, cache_dir
from .gitsync import graph_state, sync_many
from .graph import graph_one
from .hints import Hints, find_hints, render_hints
from .scan import Repo, build_index, load_index

STEP_TITLES = {
    1: "Update repos",
    2: "Build code graphs",
    3: "Select what to keep",
    4: "Map the chosen repos",
}


@dataclass
class Event:
    step: int
    kind: str            # start | progress | line | done
    text: str = ""
    done: int = 0
    total: int = 0
    level: str = "info"  # info | ok | warn | error


Emit = Callable[[Event], None]
Stop = Callable[[], bool]


def _fleet_file():
    return cache_dir() / "fleet.json"


def minutes_since_sync() -> float | None:
    try:
        stamp = float(json.loads(_fleet_file().read_text())["synced_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (time.time() - stamp) / 60


def _mark_synced() -> None:
    try:
        _fleet_file().parent.mkdir(parents=True, exist_ok=True)
        _fleet_file().write_text(json.dumps({"synced_at": time.time()}))
    except OSError:
        pass


def run_sync(cfg: Config, repos: list[Repo], emit: Emit, force: bool = False, should_stop: Stop | None = None) -> dict:
    """Step 1."""
    mode = cfg.sync_fleet
    if mode == "off":
        emit(Event(1, "done", "off in config ([sync] fleet)", level="warn"))
        return {"skipped": "off"}
    age = minutes_since_sync()
    if not force and age is not None and age < cfg.sync_ttl_minutes:
        emit(Event(1, "done", f"synced {age:.0f} min ago, skipped (--refresh forces)", len(repos), len(repos), "ok"))
        return {"skipped": "recent"}
    names = {r.path: r.name for r in repos}
    total = len(repos)
    counts: dict[str, int] = {}
    n = 0
    emit(Event(1, "start", f"{'checking out the root branch and ' if mode == 'switch' else ''}pulling {total} repos", 0, total))

    def log(res) -> None:
        nonlocal n
        n += 1
        counts[res.status] = counts.get(res.status, 0) + 1
        name = names.get(res.path, res.path)
        if res.status == "updated":
            emit(Event(1, "line", f"{name}: pulled {res.moved} commit(s) on {res.branch}", n, total, "ok"))
        elif res.status == "failed":
            emit(Event(1, "line", f"{name}: {res.detail}", n, total, "error"))
        elif res.status in ("left-on-branch", "skipped"):
            emit(Event(1, "line", f"{name}: left alone, {res.detail}", n, total, "warn"))
        emit(Event(1, "progress", name, n, total))

    sync_many([r.path for r in repos], cfg, switch=mode == "switch", log=log, should_stop=should_stop)
    if not (should_stop and should_stop()):
        _mark_synced()
    parts = [f"{counts.get('updated', 0)} updated", f"{counts.get('current', 0)} already current"]
    for key, label in (("left-on-branch", "on a work branch"), ("skipped", "left alone"), ("failed", "failed")):
        if counts.get(key):
            parts.append(f"{counts[key]} {label}")
    emit(Event(1, "done", ", ".join(parts), total, total, "warn" if counts.get("failed") else "ok"))
    return counts


def run_graphs(cfg: Config, repos: list[Repo], emit: Emit, should_stop: Stop | None = None) -> list[Repo]:
    """Step 2. Returns the repos with their graph reports refreshed."""
    if shutil.which(cfg.graphify_bin) is None:
        emit(Event(2, "done", f"'{cfg.graphify_bin}' not installed, skipped (uv tool install graphifyy)", level="warn"))
        return repos
    todo = [r for r in repos if graph_state(r.graph_report, r.path) in ("none", "stale")]
    total = len(todo)
    if not todo:
        emit(Event(2, "done", f"all {len(repos)} graphs are current", len(repos), len(repos), "ok"))
        return repos
    emit(Event(2, "start", f"building {total} graph(s), {len(repos) - total} already current", 0, total))
    counts: dict[str, int] = {}
    n = 0

    def one(repo: Repo):
        if should_stop and should_stop():
            return repo, None
        return repo, graph_one(repo, cfg)

    with ThreadPoolExecutor(max_workers=max(1, cfg.graph_workers)) as pool:
        for repo, res in pool.map(one, todo):
            n += 1
            if res is None:
                continue
            counts[res.status] = counts.get(res.status, 0) + 1
            if res.status == "failed":
                emit(Event(2, "line", f"{repo.name}: {res.detail}", n, total, "error"))
            else:
                emit(Event(2, "line", f"{repo.name}: graph {res.status}", n, total, "ok"))
            emit(Event(2, "progress", repo.name, n, total))
    fresh = build_index(cfg)
    done = counts.get("built", 0) + counts.get("updated", 0)
    emit(Event(2, "done", f"{done} built or updated, {len(repos) - total} already current"
               + (f", {counts['failed']} failed" if counts.get("failed") else ""), total, total,
               "warn" if counts.get("failed") else "ok"))
    return fresh


def run_map(cfg: Config, repos: list[Repo], prompt: str, emit: Emit) -> dict[str, Hints]:
    """Step 4: starting points inside each chosen repo, from its graph."""
    total = len(repos)
    out: dict[str, Hints] = {}
    if not repos:
        emit(Event(4, "done", "nothing opened: fully blind session", level="ok"))
        return out
    emit(Event(4, "start", f"looking for starting points in {total} repo(s)", 0, total))
    for i, repo in enumerate(repos, 1):
        if not cfg.hints_enabled or not prompt:
            emit(Event(4, "line", f"{repo.name}: " + ("graph ready" if repo.graph_report else "no graph (file names only)"), i, total, "info"))
        else:
            h = find_hints(prompt, repo, cfg)
            if h:
                out[repo.name] = h
                files = ", ".join(f.path for f in h.files[:3])
                emit(Event(4, "line", f"{repo.name}: {len(h.files)} starting point(s): {files}", i, total, "ok"))
            else:
                emit(Event(4, "line", f"{repo.name}: " + ("" if repo.graph_report else "no graph, ") + "nothing specific to point at", i, total, "info"))
        emit(Event(4, "progress", repo.name, i, total))
    emit(Event(4, "done", f"{sum(len(h.files) for h in out.values())} starting point(s) in {len(out)} of {total} repo(s)", total, total, "ok"))
    return out


def reload(cfg: Config) -> list[Repo]:
    return load_index(cfg)
