"""Find which repos contain an exact identifier from the prompt, with no model and no tokens.

A request such as "rename ``invoice_vat_rate``" is often best answered by the repos that literally contain
that identifier: the application, but also the deploy config and the batch job that nobody named. Words
that look like identifiers (``snake_case``, ``camelCase``, letters mixed with digits, or anything quoted)
are searched in every repo; only repo *names* come back, never file contents. ``rg`` is used when installed,
a bounded Python walk otherwise. An identifier found in half of the repos or more is ignored: it says
nothing about any one of them. Nothing here knows a language, a framework or a word list.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import Config
from .files import iter_files
from .relations import _read_text, normalize
from .text import MIN_REPOS_FOR_UBIQUITY, STEM_LENGTH, ubiquitous

MIN_LITERAL = 5          # characters, unless the user quoted it
MAX_FILES = 4000
MAX_DEPTH = 8
RG_TIMEOUT = 30          # seconds, for one identifier over all repos
SKIP_GLOBS = ("!graphify-out", "!.blinders")   # our own generated notes would match every identifier

_QUOTED = re.compile(r"`([^`\s]{3,})`|\"([^\"\s]{3,})\"")
_WORD = re.compile(r"[A-Za-z0-9_.\-/]+")
_CAMEL = re.compile(r"[a-z][A-Z]")


def _compound(word: str) -> bool:
    """``phenix-cli``: plain ASCII parts of 3 letters or more joined by hyphens. Everyday compounds
    (``est-ce``, ``peut-on``, ``peut-être``) have a short part or an accent and are left out."""
    parts = word.split("-")
    return len(parts) > 1 and all(len(p) >= 3 and p.isalnum() for p in parts)


def _identifier_shaped(word: str) -> bool:
    return (
        "_" in word
        or bool(_CAMEL.search(word))
        or (any(c.isdigit() for c in word) and any(c.isalpha() for c in word))
    )


def literals(prompt: str, limit: int = 8) -> list[str]:
    """Identifier-shaped words of the prompt, in order, without duplicates."""
    found: list[str] = []
    seen: set[str] = set()

    def add(word: str) -> None:
        key = word.lower()
        if key not in seen and len(found) < limit:
            seen.add(key)
            found.append(word)

    spans = [(m.start(), m.group(0)) for m in _QUOTED.finditer(prompt)]
    for _, text in sorted(spans):
        add(text.strip("`\""))
    rest = _QUOTED.sub(" ", prompt)
    for word in _WORD.findall(rest):
        word = word.strip(".-/_")
        if len(word) >= MIN_LITERAL and (_identifier_shaped(word) or _compound(word)):
            add(word)
            for part in word.split(".") if "." in word else ():   # "raw_ugc_inbound.acme.com" also gives raw_ugc_inbound
                if len(part) >= MIN_LITERAL and _identifier_shaped(part):
                    add(part)
    return found


def _owner(file: str, roots: dict[str, str]) -> str | None:
    for parent in Path(file).parents:
        if str(parent) in roots:
            return str(parent)
    return None


def stem_pattern(word: str) -> str:
    """Regex for a stem: starts a word; a stem shorter than STEM_LENGTH is the whole word (``cli`` is not
    ``client``), a full-length one is a prefix (``deplo`` finds ``deployment``)."""
    return r"\b" + re.escape(word) + (r"\b" if len(word) < STEM_LENGTH else "")


def _rg(literal: str, repos: list, stems: bool = False) -> set[str]:
    cmd = ["rg", "-l", "-i", "--no-messages", "--max-filesize", "1M"]
    cmd += ["-e", stem_pattern(literal)] if stems else ["-F", "-e", literal]
    for glob in SKIP_GLOBS:
        cmd += ["-g", glob]
    cmd += ["--"] + [r.path for r in repos]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=RG_TIMEOUT, check=False).stdout
    roots = {r.path: r.path for r in repos}
    return {o for o in (_owner(line, roots) for line in out.splitlines()) if o}


def _walk(literal_set: list[str], repos: list, stems: bool = False) -> dict[str, set[str]]:
    """Fallback without ``rg``: shallow-first walk of each repo, head of each text file."""
    found: dict[str, set[str]] = {lit: set() for lit in literal_set}
    wanted = [(lit, re.compile(stem_pattern(lit.lower())) if stems else lit.lower()) for lit in literal_set]
    for repo in repos:
        left = dict(wanted)
        for file in iter_files(Path(repo.path), MAX_DEPTH, MAX_FILES):
            if not left:
                break
            text = _read_text(file).lower()
            if not text:
                continue
            for lit, low in list(left.items()):
                if (low.search(text) if stems else low in text):
                    found[lit].add(repo.path)
                    del left[lit]
    return found


def _find(lit: str, repos: list, use_rg: bool, stems: bool) -> set[str]:
    if use_rg:
        try:
            return _rg(lit, repos, stems)
        except (OSError, subprocess.SubprocessError):
            pass
    return _walk([lit], repos, stems)[lit]


def locate(wanted: list[str], repos: list, use_rg: bool = True, cache: dict | None = None,
           stems: bool = False) -> dict[str, set[str]]:
    """text -> paths of the repos whose files contain it (case-insensitive substring, or as stems: see
    ``stem_pattern``).

    ``cache`` (optional, owned by the caller) remembers answers per (text, repo), so typing a prompt letter
    by letter in the launcher searches each word once."""
    if not wanted or not repos:
        return {}
    cache = cache if cache is not None else {}
    out: dict[str, set[str]] = {}
    for lit in wanted:
        key = (lit.lower(), stems)
        missing = [r for r in repos if (key, r.path) not in cache]
        if missing:
            found = _find(lit, missing, use_rg, stems)
            for r in missing:
                cache[(key, r.path)] = r.path in found
        out[lit] = {r.path for r in repos if cache[(key, r.path)]}
    return out


def search(wanted: list[str], repos: list, use_rg: bool = True, cache: dict | None = None,
           broad_limit: int | None = None) -> dict[str, list[str]]:
    """repo path -> identifiers it contains. Identifiers found in most repos are dropped, and so are the
    plain hyphenated names (``phenix-cli``) found in more than ``broad_limit`` repos: too common to pick one."""
    by_literal = locate(wanted, repos, use_rg, cache)
    if not by_literal:
        return {}
    too_common = ubiquitous(
        [{lit for lit in wanted if r.path in by_literal.get(lit, ())} for r in repos], MIN_REPOS_FOR_UBIQUITY
    )
    hits: dict[str, list[str]] = {}
    for lit in wanted:
        if lit in too_common:
            continue
        if broad_limit is not None and not _identifier_shaped(lit) and len(by_literal.get(lit, ())) > broad_limit:
            continue
        for path in sorted(by_literal.get(lit, ())):
            hits.setdefault(path, []).append(lit)
    return hits


def find_hits(prompt: str, repos: list, cfg: Config, cache: dict | None = None) -> dict[str, list[str]]:
    """Repos that contain identifiers from the prompt (repo names themselves are not searched)."""
    if not cfg.grep_enabled:
        return {}
    known = {normalize(i) for r in repos for i in (r.identities + [r.name])}
    wanted = [lit for lit in literals(prompt, cfg.grep_max_literals) if normalize(lit) not in known]
    return search(wanted, repos, cache=cache, broad_limit=cfg.max_repos)
