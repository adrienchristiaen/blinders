"""Second level: starting points *inside* an opened repo. No model is called.

Two sources of evidence, one scorer:

- the Graphify code graph (``graphify-out/graph.json``): symbols, the files they live in, and the links
  between them;
- failing that, the repo's own file names and the names declared in its YAML (a dbt project, a folder of
  SQL or notebooks, anything the graph tool cannot read).

Words are compared as stems, so ``orders`` meets ``order``. The result goes into the index as a hint, never as
a verdict: the agent is told to verify before relying on it.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .files import YAML_SUFFIXES, declared_names, iter_files
from .scan import MAX_PATH_DEPTH, MAX_PATH_FILES, Repo
from .text import stems

MAX_GRAPH_BYTES = 200 * 1024 * 1024
LABEL_WEIGHT = 2.0
MIN_SCORE_SHARE = 0.5    # of the best a single rare name could score: scales with the size of the repo
RATIONALE_WEIGHT = 0.5   # docstring nodes help find the file but are never shown as symbols
RELATIVE_THRESHOLD = 0.4
MAX_SYMBOLS = 3
MAX_CONNECTED = 2


@dataclass
class FileHint:
    path: str
    score: float
    symbols: list[tuple[str, str]] = field(default_factory=list)  # (label, "L28")


@dataclass
class Hints:
    repo: str
    files: list[FileHint]
    connected: list[str] = field(default_factory=list)
    source: str = "graph"    # graph | files


def _load(repo: Repo) -> dict | None:
    graph = Path(repo.path) / "graphify-out" / "graph.json"
    try:
        if graph.stat().st_size > MAX_GRAPH_BYTES:
            return None
        data = json.loads(graph.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("nodes"), list) else None


def _rel(path: str, root: str) -> str:
    return path[len(root):].lstrip("/") if path.startswith(root) else path


def _graph_nodes(data: dict, repo: Repo) -> dict[str, dict]:
    nodes: dict[str, dict] = {}
    for n in data["nodes"]:
        src, nid, kind = n.get("source_file"), n.get("id"), n.get("file_type")
        if isinstance(src, str) and src and isinstance(nid, str) and kind in ("code", "rationale"):
            nodes[nid] = {"file": _rel(src, repo.path), "label": str(n.get("label", "")),
                          "loc": str(n.get("source_location") or ""), "code": kind == "code", "symbol": True}
    return nodes


def _file_nodes(repo: Repo) -> dict[str, dict]:
    """One pseudo-node per file: its name plus the names declared inside it. Nothing to show as a symbol."""
    root = Path(repo.path)
    nodes: dict[str, dict] = {}
    for file in iter_files(root, MAX_PATH_DEPTH, MAX_PATH_FILES):
        rel = str(file.relative_to(root))
        label = file.stem
        if file.name.endswith(YAML_SUFFIXES):
            label += " " + " ".join(declared_names(file))
        nodes[rel] = {"file": rel, "label": label, "loc": "", "code": True, "symbol": False}
    return nodes


def find_hints(prompt: str, repo: Repo, cfg: Config) -> Hints | None:
    query = set(stems(prompt)) - set(stems(repo.name))
    if not query:
        return None
    data = _load(repo)
    nodes = _graph_nodes(data, repo) if data is not None else {}
    source = "graph"
    if not nodes:
        nodes, data, source = _file_nodes(repo), None, "files"
    if not nodes:
        return None

    label_terms = {nid: set(stems(n["label"])) for nid, n in nodes.items()}
    path_terms = {f: set(stems(f)) for f in {n["file"] for n in nodes.values()}}
    df: Counter[str] = Counter()
    for nid, terms in label_terms.items():
        df.update(terms | path_terms[nodes[nid]["file"]])
    total = len(nodes)

    def idf(t: str) -> float:
        return math.log(1 + total / df.get(t, 1))

    per_file: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for nid, n in nodes.items():
        hit = query & label_terms[nid]
        if hit:
            weight = LABEL_WEIGHT if n["code"] else LABEL_WEIGHT * RATIONALE_WEIGHT
            per_file[n["file"]].append((weight * sum(idf(t) for t in hit), nid))
    for f, terms in path_terms.items():
        if query & terms and f not in per_file:
            per_file[f] = []

    scored: list[tuple[float, str]] = []
    for f, items in per_file.items():
        items.sort(reverse=True)
        score = sum(s for s, _ in items[:3]) + sum(idf(t) for t in query & path_terms[f])
        scored.append((score, f))
    scored.sort(key=lambda x: (-x[0], x[1]))
    if not scored or scored[0][0] < MIN_SCORE_SHARE * LABEL_WEIGHT * math.log(1 + total):
        return None
    top = [(s, f) for s, f in scored if s >= scored[0][0] * RELATIVE_THRESHOLD][: cfg.hints_max_files]

    files: list[FileHint] = []
    matched_ids: set[str] = set()
    for score, f in top:
        syms = []
        shown = [x for x in sorted(per_file[f], reverse=True) if nodes[x[1]]["code"] and nodes[x[1]]["symbol"]]
        for _, nid in shown[:MAX_SYMBOLS]:
            syms.append((nodes[nid]["label"].strip(".").rstrip("()") or nodes[nid]["label"], nodes[nid]["loc"]))
            matched_ids.add(nid)
        files.append(FileHint(f, score, syms))

    near: Counter[str] = Counter()
    chosen = {f.path for f in files}
    for link in (data or {}).get("links", []):
        a, b = link.get("source"), link.get("target")
        for here, there in ((a, b), (b, a)):
            if here in matched_ids and there in nodes and nodes[there]["file"] not in chosen:
                near[nodes[there]["file"]] += 1
    connected = [f for f, _ in near.most_common(MAX_CONNECTED)]
    return Hints(repo.name, files, connected, source)


def render_hints(h: Hints, repo_path: str) -> list[str]:
    origin = "the code graph" if h.source == "graph" else "file names"
    lines = [f"  Starting points from {origin} (hints, not a verdict; check before relying on them):"]
    for f in h.files:
        syms = ", ".join(f"{label} ({loc})" if loc else label for label, loc in f.symbols)
        lines.append(f"  - {f.path}" + (f": {syms}" if syms else ""))
    if h.connected:
        lines.append("  - connected to those: " + ", ".join(h.connected))
    return lines
