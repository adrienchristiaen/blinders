"""Measure the selection on your own prompts: ``blind eval`` (quality) and ``blind bench`` (speed).

A cases file lists prompts and what you expected, so each change to the rules is checked against what you
know to be right instead of against a feeling::

    [[case]]
    prompt = "il faut modifier dans composer-deployment"
    expect = ["composer-deployment"]      # repos that must open
    avoid  = ["k8s"]                      # repos that must not (optional)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .grep import find_hits
from .scan import Repo, labels
from .select import plan

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


@dataclass
class Case:
    prompt: str
    expect: list[str]
    avoid: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    case: Case
    opened: list[str]
    listed: list[str]
    missing: list[str]
    forbidden: list[str]
    ms: float

    @property
    def passed(self) -> bool:
        return not self.missing and not self.forbidden

    @property
    def recall(self) -> float:
        want = len(self.case.expect)
        return (want - len(self.missing)) / want if want else 1.0

    @property
    def precision(self) -> float:
        if not self.opened:
            return 1.0
        return len(set(self.opened) & set(self.case.expect)) / len(self.opened)


def load_cases(path: Path) -> list[Case]:
    with Path(path).open("rb") as fh:
        data = tomllib.load(fh)
    cases = []
    for i, raw in enumerate(data.get("case", []), 1):
        prompt, expect = str(raw.get("prompt", "")).strip(), list(raw.get("expect", []))
        if not prompt or not expect:
            raise ValueError(f"case {i}: needs a `prompt` and a non-empty `expect`")
        cases.append(Case(prompt, expect, list(raw.get("avoid", []))))
    if not cases:
        raise ValueError("no [[case]] found")
    return cases


def run_cases(cases: list[Case], repos: list[Repo], cfg: Config) -> list[CaseResult]:
    lab = labels(repos)
    cache: dict = {}
    out = []
    for case in cases:
        t0 = time.perf_counter()
        p = plan(case.prompt, repos, cfg, cache=cache)
        ms = (time.perf_counter() - t0) * 1000
        opened = [lab[c.repo.path] for c in p.opened]
        listed = [lab[r.repo.path] for r in p.related]
        out.append(CaseResult(
            case, opened, listed,
            missing=[n for n in case.expect if n not in opened],
            forbidden=[n for n in case.avoid if n in opened],
            ms=ms))
    return out


def render(results: list[CaseResult], as_json: bool = False) -> str:
    passed = sum(r.passed for r in results)
    recall = sum(r.recall for r in results) / len(results)
    precision = sum(r.precision for r in results) / len(results)
    if as_json:
        return json.dumps({
            "passed": passed, "total": len(results), "recall": round(recall, 3), "precision": round(precision, 3),
            "cases": [{"prompt": r.case.prompt, "passed": r.passed, "opened": r.opened, "listed": r.listed,
                       "missing": r.missing, "forbidden": r.forbidden, "ms": round(r.ms, 1)} for r in results]})
    lines = []
    for r in results:
        head = r.case.prompt if len(r.case.prompt) <= 70 else r.case.prompt[:67] + "..."
        lines.append(f"{'PASS' if r.passed else 'FAIL'}  {head}  ({r.ms:.0f} ms)")
        lines.append(f"      opened: {', '.join(r.opened) or 'none'}")
        if r.missing:
            lines.append(f"      missing: {', '.join(r.missing)}" + (f"   (listed only: {', '.join(n for n in r.missing if n in r.listed)})" if any(n in r.listed for n in r.missing) else ""))
        if r.forbidden:
            lines.append(f"      should not have opened: {', '.join(r.forbidden)}")
    lines.append(f"{passed}/{len(results)} cases pass; recall {recall:.0%}, precision {precision:.0%}")
    return "\n".join(lines)


def bench(prompt: str, repos: list[Repo], cfg: Config) -> list[tuple[str, float]]:
    """Milliseconds per stage for one prompt, on the real index."""
    def timed(fn) -> float:
        t0 = time.perf_counter()
        fn()
        return (time.perf_counter() - t0) * 1000

    cache: dict = {}
    return [
        ("instant pass (index only)", timed(lambda: plan(prompt, repos, cfg, deep=False))),
        ("identifiers in file contents", timed(lambda: find_hits(prompt, repos, cfg))),
        ("full selection (first run)", timed(lambda: plan(prompt, repos, cfg, cache=cache))),
        ("full selection (answers cached)", timed(lambda: plan(prompt, repos, cfg, cache=cache))),
    ]
