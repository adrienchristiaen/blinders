"""Bring repos up to date before reading them: fetch, check out the root branch, fast-forward.

Guard rails (this touches working trees, so it refuses rather than guesses):

- never with uncommitted changes to tracked files, a merge/rebase/cherry-pick in progress, or a detached HEAD;
- never a merge commit: ``--ff-only``; a diverged branch is reported and left alone;
- in ``safe`` mode a repo that sits on another branch is left on it (its graph is built from that branch);
  only ``switch`` mode checks out the root branch, which is what ``blind sync`` does;
- never prompts for credentials (``GIT_TERMINAL_PROMPT=0``) and every call has a timeout.

The branch it switches *away from* is never deleted or modified, so no commit is lost.
"""

from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .config import Config

IN_PROGRESS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "BISECT_LOG")
REPORT_COMMIT = re.compile(r"Built from commit: `([0-9a-f]{7,40})`")


@dataclass
class SyncResult:
    path: str
    status: str   # updated | current | left-on-branch | skipped | failed
    detail: str = ""
    branch: str = ""
    moved: int = 0  # commits fast-forwarded


def _git(path: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true", LC_ALL="C")
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=timeout, env=env)


def head_commit(path: str) -> str:
    try:
        p = _git(path, "rev-parse", "HEAD", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def current_branch(path: str) -> str:
    try:
        p = _git(path, "symbolic-ref", "--short", "-q", "HEAD", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def is_dirty(path: str) -> bool:
    """Uncommitted changes to tracked files (untracked files never block a fast-forward)."""
    p = _git(path, "status", "--porcelain", "--untracked-files=no", timeout=30)
    return p.returncode != 0 or bool(p.stdout.strip())


def _git_dir(path: str) -> Path:
    p = _git(path, "rev-parse", "--git-dir", timeout=10)
    d = Path(p.stdout.strip() or ".git")
    return d if d.is_absolute() else Path(path) / d


def remote_of(path: str) -> str:
    p = _git(path, "remote", timeout=10)
    remotes = p.stdout.split()
    return "origin" if "origin" in remotes else (remotes[0] if remotes else "")


def root_branch(path: str, remote: str, cfg: Config) -> str:
    """The remote's default branch, else the first of main/master/develop that exists."""
    p = _git(path, "symbolic-ref", "--short", "-q", f"refs/remotes/{remote}/HEAD", timeout=10)
    if p.returncode == 0 and p.stdout.strip().startswith(remote + "/"):
        return p.stdout.strip()[len(remote) + 1:]
    for name in cfg.root_branches:
        if _git(path, "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{name}", timeout=10).returncode == 0:
            return name
    return ""


def sync_repo(path: str, cfg: Config, switch: bool) -> SyncResult:
    try:
        return _sync(path, cfg, switch)
    except subprocess.TimeoutExpired:
        return SyncResult(path, "failed", "git timed out")
    except OSError as exc:
        return SyncResult(path, "failed", str(exc))


def _sync(path: str, cfg: Config, switch: bool) -> SyncResult:
    branch = current_branch(path)
    if not branch:
        return SyncResult(path, "skipped", "detached HEAD")
    gitdir = _git_dir(path)
    if any((gitdir / marker).exists() for marker in IN_PROGRESS):
        return SyncResult(path, "skipped", "merge/rebase in progress", branch)
    if is_dirty(path):
        return SyncResult(path, "skipped", "uncommitted changes", branch)
    remote = remote_of(path)
    if not remote:
        return SyncResult(path, "skipped", "no remote", branch)
    fetched = _git(path, "fetch", "--quiet", "--prune", remote, timeout=cfg.sync_timeout)
    if fetched.returncode != 0:
        tail = (fetched.stderr.strip().splitlines() or ["fetch failed"])[-1][:120]
        return SyncResult(path, "failed", tail, branch)
    root = root_branch(path, remote, cfg)
    if not root:
        return SyncResult(path, "skipped", "no root branch found (main/master/develop)", branch)
    if branch != root:
        if not switch:
            return SyncResult(path, "left-on-branch", f"on {branch}, root branch is {root}", branch)
        co = _git(path, "checkout", "--quiet", root, timeout=60)
        if co.returncode != 0:
            tail = (co.stderr.strip().splitlines() or ["checkout failed"])[-1][:120]
            return SyncResult(path, "failed", tail, branch)
        branch = root
    before = head_commit(path)
    merged = _git(path, "merge", "--ff-only", "--quiet", f"{remote}/{root}", timeout=60)
    if merged.returncode != 0:
        return SyncResult(path, "failed", f"{root} cannot fast-forward (diverged?)", branch)
    after = head_commit(path)
    if before == after:
        return SyncResult(path, "current", "", branch)
    count = _git(path, "rev-list", "--count", f"{before}..{after}", timeout=10).stdout.strip()
    return SyncResult(path, "updated", "", branch, moved=int(count) if count.isdigit() else 0)


def sync_many(paths: list[str], cfg: Config, switch: bool, log=None, should_stop=None) -> list[SyncResult]:
    """Sync in parallel; ``log`` gets each result as it completes; ``should_stop()`` cancels what has not started."""
    results: list[SyncResult] = []
    with ThreadPoolExecutor(max_workers=max(1, cfg.sync_workers)) as pool:
        def one(path: str) -> SyncResult:
            if should_stop and should_stop():
                return SyncResult(path, "skipped", "cancelled")
            return sync_repo(path, cfg, switch)

        futures = [pool.submit(one, p) for p in paths]
        for fut in futures:
            result = fut.result()
            results.append(result)
            if log:
                log(result)
    return results


# --- graph freshness ---------------------------------------------------------------------------

def graph_commit(report_path: str) -> str:
    """Commit a Graphify graph was built from, read from GRAPH_REPORT.md (cheap: no graph.json parse)."""
    if not report_path:
        return ""
    try:
        with open(report_path, encoding="utf-8", errors="ignore") as fh:
            head = fh.read(4096)
    except OSError:
        return ""
    m = REPORT_COMMIT.search(head)
    return m.group(1) if m else ""


def graph_state(report_path: str, repo_path: str) -> str:
    """``none`` (no graph), ``stale`` (built from another commit), ``fresh``, or ``unknown`` (no commit info)."""
    if not report_path:
        return "none"
    built = graph_commit(report_path)
    head = head_commit(repo_path)
    if not built or not head:
        return "unknown"
    return "fresh" if head.startswith(built) or built.startswith(head) else "stale"


def hide_graph_output(repo_path: str) -> None:
    """Keep ``graphify-out/`` out of ``git status`` using the repo-local exclude file (never committed)."""
    try:
        info = _git_dir(repo_path) / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if "graphify-out" not in text:
            with exclude.open("a", encoding="utf-8") as fh:
                fh.write(("" if text.endswith("\n") or not text else "\n") + "graphify-out/\n")
    except OSError:
        pass
