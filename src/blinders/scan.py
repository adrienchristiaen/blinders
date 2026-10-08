"""Find git repos under the configured roots and build a small, cheap-to-read index.

Only a few KB per repo are ever read (README head, top-level names, optional map
files such as graphify output). Source code is never opened.
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config, cache_dir
from .relations import collect_ref_text, compute_links, identities
from .text import tokens

SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "target", "dist", "build",
    "__pycache__", ".cache", ".gradle", ".idea", "vendor",
}
MARKERS = {
    "pom.xml": "java/maven", "build.gradle": "gradle", "build.gradle.kts": "gradle",
    "build.sbt": "scala", "package.json": "node", "pyproject.toml": "python",
    "requirements.txt": "python", "go.mod": "go", "Cargo.toml": "rust",
    "Dockerfile": "docker", "dbt_project.yml": "dbt", "Chart.yaml": "helm",
    "airflow.cfg": "airflow", "kustomization.yaml": "kustomize", "skaffold.yaml": "kubernetes",
}
APP_MARKERS = {"java/maven", "gradle", "scala", "node", "python", "go", "rust"}
DEPLOY_MARKERS = {"helm", "kustomize", "kubernetes", "terraform"}
DEPLOY_TOP_DIRS = {"k8s", "kubernetes", "helm", "charts", "chart", "deploy", "deployment", "manifests", "terraform", "infra", "argocd"}
DATA_MARKERS = {"dbt", "airflow"}
DATA_TOP_DIRS = {"dags", "models", "sql", "pipelines"}
GRAPH_REPORT = Path("graphify-out") / "GRAPH_REPORT.md"
CONTEXT_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
README_BYTES = 4096
MAP_BYTES = 4096
MAX_TERMS = 250


@dataclass
class Repo:
    name: str
    path: str
    description: str = ""
    markers: list[str] = field(default_factory=list)
    top_dirs: list[str] = field(default_factory=list)
    terms: dict[str, int] = field(default_factory=dict)
    roles: list[str] = field(default_factory=list)       # app | deploy | data
    identities: list[str] = field(default_factory=list)  # names other repos may use for this one
    links: list[dict] = field(default_factory=list)      # {"to": path, "w": weight, "kind": ...}
    graph_report: str = ""                               # path to graphify-out/GRAPH_REPORT.md if present


def _read_head(path: Path, limit: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _first_paragraph(readme: str) -> str:
    for block in readme.split("\n\n"):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        lines = [ln for ln in lines if not ln.startswith(("#", "![", "[!", "<", "---", "```"))]
        if lines:
            return " ".join(lines)[:160]
    return ""


MAX_PATH_ENTRIES = 4000     # files and folders visited per repo
MAX_PATH_TERMS = 300
YAML_NAME = re.compile(r"^\s*-?\s*name:\s*['\"]?([A-Za-z0-9_.\-]+)", re.M)
YAML_DIRS = ("models", "snapshots", "seeds", "dags")


def path_terms(path: Path) -> Counter[str]:
    """Words from folder and file names (``fct_orders.sql`` gives fct, orders) plus the model, source and
    table names declared in the YAML of a dbt project. Reads names and a few small YAML heads, never code.
    This is what lets a repo with no README (only SQL, only manifests) be found by what it contains."""
    counts: Counter[str] = Counter()
    seen = 0
    yamls: list[Path] = []
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel = Path(root).relative_to(path)
        in_yaml_dir = bool(rel.parts) and rel.parts[0] in YAML_DIRS
        for name in dirs + sorted(files):
            seen += 1
            stem = name.rsplit(".", 1)[0] if "." in name[1:] else name
            counts.update(tokens(stem))
            if in_yaml_dir and name.endswith((".yml", ".yaml")) and len(yamls) < 40:
                yamls.append(Path(root) / name)
        if seen >= MAX_PATH_ENTRIES:
            break
    project = path / "dbt_project.yml"
    for f in ([project] if project.is_file() else []) + yamls:
        for declared in YAML_NAME.findall(_read_head(f, 4096)):
            counts.update(tokens(declared))
    return counts


def describe_repo(path: Path, map_globs: list[str]) -> Repo:
    readme = ""
    for name in ("README.md", "readme.md", "README.rst", "README"):
        if (path / name).is_file():
            readme = _read_head(path / name, README_BYTES)
            break
    try:
        entries = sorted(os.listdir(path))
    except OSError:
        entries = []
    top_dirs = [e for e in entries if (path / e).is_dir() and not e.startswith(".") and e not in SKIP_DIRS]
    marker_set = {MARKERS[e] for e in entries if e in MARKERS}
    if any(e.endswith(".tf") for e in entries):
        marker_set.add("terraform")
    markers = sorted(marker_set)
    roles = []
    if marker_set & APP_MARKERS and not (marker_set & DEPLOY_MARKERS and not top_dirs):
        roles.append("app")
    if marker_set & DEPLOY_MARKERS or set(top_dirs) & DEPLOY_TOP_DIRS:
        roles.append("deploy")
    if marker_set & DATA_MARKERS or set(top_dirs) & DATA_TOP_DIRS:
        roles.append("data")

    chunks = [path.name] * 3 + [readme] + top_dirs
    for name in CONTEXT_FILES:
        if name in entries:
            chunks.append(_read_head(path / name, 2048))
    for pattern in map_globs:
        for hit in sorted(glob.glob(str(path / pattern)))[:5]:
            chunks.append(_read_head(Path(hit), MAP_BYTES))

    counts: Counter[str] = Counter()
    for chunk in chunks:
        counts.update(tokens(chunk))
    counts.update(markers)
    terms = dict(counts.most_common(MAX_TERMS))
    for term, n in path_terms(path).most_common(MAX_PATH_TERMS):
        terms[term] = terms.get(term, 0) + n
    return Repo(
        name=path.name,
        path=str(path),
        description=_first_paragraph(readme),
        markers=markers,
        top_dirs=top_dirs[:12],
        terms=terms,
        roles=roles,
        identities=sorted(identities(path)),
        graph_report=str(path / GRAPH_REPORT) if (path / GRAPH_REPORT).is_file() else "",
    )


def find_repos(roots: list[Path], depth: int) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    def walk(directory: Path, level: int) -> None:
        try:
            real = directory.resolve()
        except OSError:
            return
        if real in seen:
            return
        seen.add(real)
        if (directory / ".git").exists():
            found.append(directory)
            return  # do not descend into a repo
        if level >= depth:
            return
        try:
            children = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for entry in children:
            if entry.is_dir(follow_symlinks=False) and entry.name not in SKIP_DIRS and not entry.name.startswith("."):
                walk(Path(entry.path), level + 1)

    for root in roots:
        if root.is_dir():
            walk(root, 0)
    return found


def index_path() -> Path:
    return cache_dir() / "index.json"


def build_index(cfg: Config) -> list[Repo]:
    paths = find_repos(cfg.root_paths, cfg.scan_depth)
    repos = [describe_repo(p, cfg.map_globs) for p in paths]
    texts = {str(p): collect_ref_text(p) for p in paths}
    compute_links(repos, texts)
    save_index(repos)
    return repos


def save_index(repos: list[Repo]) -> None:
    path = index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"built_at": time.time(), "repos": [asdict(r) for r in repos]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def load_index(cfg: Config, refresh: bool = False) -> list[Repo]:
    """Load the cached index, rebuilding it when missing, stale or ``refresh``."""
    path = index_path()
    if not refresh and path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            age_h = (time.time() - payload.get("built_at", 0)) / 3600
            if age_h <= cfg.index_ttl_hours:
                known = set(Repo.__dataclass_fields__)
                return [Repo(**{k: v for k, v in r.items() if k in known}) for r in payload["repos"]]
        except (ValueError, KeyError, TypeError):
            pass
    return build_index(cfg)
