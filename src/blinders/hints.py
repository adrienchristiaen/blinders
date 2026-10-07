"""Second level: starting points *inside* an opened repo, from its Graphify code graph.

Reads ``graphify-out/graph.json`` (nodes with ``label``, ``source_file``, ``source_location``;
``links`` between them) locally, scores symbols and files against the prompt, and returns a few
files to start from. No model is called. The result goes into the index as a hint, never as a verdict:
the agent is told to verify before relying on it.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .scan import Repo
from .text import tokens

MAX_GRAPH_BYTES = 200 * 1024 * 1024
LABEL_WEIGHT = 2.0
MIN_SCORE = 4.0          # one rare symbol-name word, or several common ones
RATIONALE_WEIGHT = 0.5   # docstring nodes help find the file but are never shown as symbols
RELATIVE_THRESHOLD = 0.4
TEST_DAMPING = 0.5
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


def _is_test(path: str) -> bool:
    low = "/" + path.lower()
    return "/test" in low or "/spec" in low or low.rsplit("/", 1)[-1].startswith("test_") or "_test." in low


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


def find_hints(prompt: str, repo: Repo, cfg: Config) -> Hints | None:
    data = _load(repo)
    if data is None:
        return None
    repo_words = set(tokens(repo.name))
    query = set(tokens(prompt)) - repo_words
    if not query:
        return None
    want_tests = "test" in prompt.lower()

    nodes: dict[str, dict] = {}
    for n in data["nodes"]:
        src, nid, kind = n.get("source_file"), n.get("id"), n.get("file_type")
        if isinstance(src, str) and src and isinstance(nid, str) and kind in ("code", "rationale"):
            nodes[nid] = {"file": _rel(src, repo.path), "label": str(n.get("label", "")),
                          "loc": str(n.get("source_location") or ""), "code": kind == "code"}
    if not nodes:
        return None

    label_terms = {nid: set(tokens(n["label"])) for nid, n in nodes.items()}
    path_terms = {f: set(tokens(f)) for f in {n["file"] for n in nodes.values()}}
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
        if _is_test(f) and not want_tests:
            score *= TEST_DAMPING
        scored.append((score, f))
    scored.sort(key=lambda x: (-x[0], x[1]))
    if not scored or scored[0][0] < MIN_SCORE:
        return None
    top = [(s, f) for s, f in scored if s >= scored[0][0] * RELATIVE_THRESHOLD][: cfg.hints_max_files]

    files: list[FileHint] = []
    matched_ids: set[str] = set()
    for score, f in top:
        syms = []
        for _, nid in [x for x in sorted(per_file[f], reverse=True) if nodes[x[1]]["code"]][:MAX_SYMBOLS]:
            syms.append((nodes[nid]["label"].strip(".").rstrip("()") or nodes[nid]["label"], nodes[nid]["loc"]))
            matched_ids.add(nid)
        files.append(FileHint(f, score, syms))

    chosen = {f.path for f in files}
    near: Counter[str] = Counter()
    for link in data.get("links", []):
        a, b = link.get("source"), link.get("target")
        for here, there in ((a, b), (b, a)):
            if here in matched_ids and there in nodes and nodes[there]["file"] not in chosen:
                near[nodes[there]["file"]] += 1
    connected = [f for f, _ in near.most_common(MAX_CONNECTED)]
    return Hints(repo.name, files, connected)


def render_hints(h: Hints, repo_path: str) -> list[str]:
    lines = ["  Starting points from the code graph (hints, not a verdict; check before relying on them):"]
    for f in h.files:
        syms = ", ".join(f"{label} ({loc})" if loc else label for label, loc in f.symbols)
        lines.append(f"  - {f.path}" + (f": {syms}" if syms else ""))
    if h.connected:
        lines.append("  - connected to those: " + ", ".join(h.connected))
    return lines
