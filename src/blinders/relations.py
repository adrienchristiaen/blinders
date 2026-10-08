"""Relations between repos, computed offline at index time. No LLM, no source parsing.

Two signals, both read from the user's own repos (no list of file names, stacks or words):

- ``refs``: a small text file near the top of a repo (build, deploy, CI, config... whatever it uses)
  mentions another repo by name. A repo is known by its directory name and by the name it declares for
  itself in a root file (``name = "x"``, ``"name": "x"``, ``<artifactId>x</artifactId>``).
- ``name``: one repo name is a hyphen-prefix of another (``sales-api`` / ``sales-api-java``).

Explicit groups come from ``config.toml`` (applied at selection time, see ``select.py``).
Links are stored on each repo as ``{"to": <repo path>, "w": weight, "kind": ...}``. A repo that most
other repos mention says little about any one of them, so its links weigh less (inverse frequency).
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from .files import iter_files
from .text import squash

MAX_FILE_BYTES = 64 * 1024
MAX_TEXT_BYTES = 256 * 1024
MAX_FILES = 150
MAX_DEPTH = 3
MAX_COUNT = 5
MIN_IDENTITY = 5       # characters: shorter names are too likely to be an ordinary word

_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_DECLARED = re.compile(r"""\bname["']?\s*(?::=|=|:)\s*["']?([A-Za-z0-9_.\-]+)""")
_ARTIFACT = re.compile(r"<artifactId>\s*([^<\s]+)\s*</artifactId>")
_PARENT = re.compile(r"<parent>.*?</parent>", re.S)


def normalize(name: str) -> str:
    """``Sales_API.java`` -> ``sales-api-java``."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def _read_text(path: Path, limit: int = MAX_FILE_BYTES) -> str:
    """Head of a text file; empty for a binary file or one that cannot be read."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(limit)
    except OSError:
        return ""
    return "" if b"\x00" in raw[:1024] else raw.decode("utf-8", errors="ignore")


def collect_ref_text(path: Path) -> str:
    """A bounded amount of text from the files nearest the top of the repo (shallowest first). Deep folders,
    where application code lives, are out of reach by design."""
    chunks: list[str] = []
    total = 0
    for file in iter_files(path, MAX_DEPTH, MAX_FILES):
        chunk = _read_text(file)
        if chunk:
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_TEXT_BYTES:
                break
    return "\n".join(chunks)


def identities(path: Path) -> set[str]:
    """Names other repos may use for this one: its directory name and the names its root files declare."""
    ids = {normalize(path.name)}
    try:
        root_files = [p for p in sorted(path.iterdir()) if p.is_file()]
    except OSError:
        root_files = []
    for file in root_files:
        text = _read_text(file)
        if not text:
            continue
        text = _PARENT.sub("", text)
        for pattern in (_DECLARED, _ARTIFACT):
            m = pattern.search(text)
            if m:
                ids.add(normalize(m.group(1)))
    return {i for i in ids if len(squash(i)) >= MIN_IDENTITY}


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

    mentions = {repo.path: _mentions(texts.get(repo.path, ""), id_map) for repo in repos}
    mentioned_by: dict[str, int] = {}
    for found in mentions.values():
        for owner in found:
            mentioned_by[owner] = mentioned_by.get(owner, 0) + 1
    for repo in repos:
        for owner, count in mentions[repo.path].items():
            if owner == repo.path:
                continue
            rarity = math.log(1 + len(repos) / mentioned_by[owner])
            weight = (1.0 + 0.5 * min(count, MAX_COUNT)) * rarity
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
