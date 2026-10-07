"""Relations between repos, computed offline at index time. No LLM, no source parsing.

Three signals:

- ``refs``: a repo's build and deploy files (Helm, Kubernetes, Docker, CI, Terraform,
  pom.xml, package.json, ...) mention another repo's name or artifact name.
- ``name``: one repo name is a hyphen-prefix of another (``sales-api`` / ``sales-api-java``).
- explicit groups from ``config.toml`` (applied at selection time, see ``select.py``).

Links are stored on each repo as ``{"to": <repo path>, "w": weight, "kind": ...}``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .text import squash

SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "target", "dist", "build",
    "__pycache__", ".cache", ".gradle", ".idea", "vendor",
}
DEPLOY_DIRS = {
    "helm", "charts", "chart", "k8s", "kubernetes", "deploy", "deployment", "deployments",
    "manifests", "kustomize", "overlays", "base", "argocd", "flux", "infra", "terraform",
    ".github", "workflows", ".gitlab",
}
REF_FILENAMES = {
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "jenkinsfile", ".gitlab-ci.yml", "chart.yaml", "values.yaml", "kustomization.yaml",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "build.sbt", "package.json", "pyproject.toml", "requirements.txt", "makefile",
    "skaffold.yaml", "cloudbuild.yaml",
}
REF_EXTENSIONS = {".tf", ".tfvars"}
YAML_EXTENSIONS = {".yaml", ".yml"}
MAX_FILE_BYTES = 64 * 1024
MAX_TEXT_BYTES = 256 * 1024
MAX_FILES = 150
MAX_DEPTH = 4
MAX_COUNT = 5

# Too common to mean "this other repo" when they appear in a manifest.
GENERIC = frozenset(
    """
    service services server client common shared utils util core parent default
    application backend frontend library libs project deploy deployment
    """.split()
)

_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def normalize(name: str) -> str:
    """``Sales_API.java`` -> ``sales-api-java``."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def collect_ref_text(path: Path) -> str:
    """Concatenate a bounded amount of build/deploy file text (never application source)."""
    chunks: list[str] = []
    total = files = 0
    base_depth = len(path.parts)
    for dirpath, dirnames, filenames in os.walk(path):
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d not in SKIP_DIRS and (not d.startswith(".") or d in DEPLOY_DIRS)
        ] if depth < MAX_DEPTH else []
        in_deploy_dir = any(p.lower() in DEPLOY_DIRS for p in Path(dirpath).relative_to(path).parts)
        for fname in sorted(filenames):
            low = fname.lower()
            ext = Path(low).suffix
            wanted = (
                low in REF_FILENAMES
                or ext in REF_EXTENSIONS
                or (in_deploy_dir and ext in YAML_EXTENSIONS)
            )
            if not wanted:
                continue
            try:
                with open(Path(dirpath) / fname, "r", encoding="utf-8", errors="ignore") as fh:
                    chunk = fh.read(MAX_FILE_BYTES)
            except OSError:
                continue
            chunks.append(chunk)
            total += len(chunk)
            files += 1
            if total >= MAX_TEXT_BYTES or files >= MAX_FILES:
                return "\n".join(chunks)
    return "\n".join(chunks)


def identities(path: Path) -> set[str]:
    """Names other repos may use to refer to this one: directory name and artifact names."""
    ids = {normalize(path.name)}

    def read(name: str) -> str:
        try:
            return (path / name).read_text(encoding="utf-8", errors="ignore")[:MAX_FILE_BYTES]
        except OSError:
            return ""

    pom = re.sub(r"<parent>.*?</parent>", "", read("pom.xml"), flags=re.S)
    m = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", pom)
    if m:
        ids.add(normalize(m.group(1)))
    for fname in ("settings.gradle", "settings.gradle.kts"):
        m = re.search(r"rootProject\.name\s*=\s*['\"]([^'\"]+)['\"]", read(fname))
        if m:
            ids.add(normalize(m.group(1)))
    m = re.search(r"""name\s*:=\s*["']([^"']+)["']""", read("build.sbt"))
    if m:
        ids.add(normalize(m.group(1)))
    # Chart.yaml is deliberately not read: a deploy repo's chart carries the *app's* name.
    m = re.search(r"""^name\s*=\s*["']([^"']+)["']""", read("pyproject.toml"), flags=re.M)
    if m:
        ids.add(normalize(m.group(1)))
    try:
        pkg = json.loads(read("package.json") or "{}")
        if isinstance(pkg, dict) and isinstance(pkg.get("name"), str):
            ids.add(normalize(pkg["name"].split("/")[-1]))
    except ValueError:
        pass
    return {i for i in ids if len(squash(i)) >= 5 and i not in GENERIC}


def _mentions(text: str, id_map: dict[str, str | None]) -> dict[str, int]:
    """Count mentions of known identities in ``text``; the longest identity at a spot wins."""
    counts: dict[str, int] = {}
    for token in _TOKEN.findall(text.lower().replace("_", "-")):
        segs = token.split("-")
        if len(segs) > 8:
            continue
        found: list[tuple[int, int, str]] = []
        for i in range(len(segs)):
            for j in range(i + 1, len(segs) + 1):
                ident = "-".join(segs[i:j])
                owner = id_map.get(ident)
                if owner:
                    found.append((i, j, owner))
        for i, j, owner in found:
            inside_longer = any(
                (a <= i and j <= b) and (b - a) > (j - i) for a, b, _ in found
            )
            if not inside_longer:
                counts[owner] = counts.get(owner, 0) + 1
    return counts


def compute_links(repos: list, texts: dict[str, str]) -> None:
    """Fill ``repo.links`` in place. ``repos`` are scan.Repo; ``texts`` maps repo path -> ref text."""
    # A directory name always beats an artifact name; any remaining collision is ambiguous: ignored.
    dir_ids: dict[str, str | None] = {}
    for repo in repos:
        ident = normalize(repo.name)
        if ident in repo.identities:
            dir_ids[ident] = None if ident in dir_ids else repo.path
    id_map: dict[str, str | None] = dict(dir_ids)
    for repo in repos:
        for ident in repo.identities:
            if ident in dir_ids:
                continue
            id_map[ident] = None if id_map.get(ident, repo.path) != repo.path else repo.path

    links: dict[str, dict[tuple[str, str], float]] = {r.path: {} for r in repos}

    def add(a: str, b: str, kind: str, weight: float) -> None:
        cur = links[a].get((b, kind), 0.0)
        links[a][(b, kind)] = max(cur, weight)

    for repo in repos:
        for owner, count in _mentions(texts.get(repo.path, ""), id_map).items():
            if owner == repo.path:
                continue
            weight = 1.0 + 0.5 * min(count, MAX_COUNT)
            add(repo.path, owner, "refs", weight)
            add(owner, repo.path, "refd_by", weight)

    normalized = {r.path: normalize(r.name) for r in repos}
    for a in repos:
        for b in repos:
            na, nb = normalized[a.path], normalized[b.path]
            if a.path != b.path and len(squash(na)) >= 5 and nb.startswith(na + "-"):
                add(a.path, b.path, "name", 0.5)
                add(b.path, a.path, "name", 0.5)

    for repo in repos:
        repo.links = [
            {"to": to, "w": w, "kind": kind}
            for (to, kind), w in sorted(links[repo.path].items(), key=lambda kv: (-kv[1], kv[0]))
        ]
