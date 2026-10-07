"""Estimate what an agent CLI would load at startup from a given directory.

This is an estimate (chars / 4), not a tokenizer. It covers what can be measured
from the filesystem: context files, skill descriptions, and MCP server counts.
MCP tool schemas are not estimated: their size depends on each server.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .scan import SKIP_DIRS

CONTEXT_NAMES = ("GEMINI.md", "CLAUDE.md", "AGENTS.md")


def est_tokens(chars: int) -> int:
    return (chars + 3) // 4


def _size(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return 0


def _context_files(path: Path, home: Path, max_depth: int, max_dirs: int) -> tuple[list[dict], bool]:
    files: list[dict] = []
    seen: set[Path] = set()

    def add(p: Path, scope: str) -> None:
        if p in seen or not p.is_file():
            return
        seen.add(p)
        files.append({"path": str(p), "tokens": est_tokens(_size(p)), "scope": scope})

    for g in (home / ".gemini" / "GEMINI.md", home / ".claude" / "CLAUDE.md"):
        add(g, "global")
    for directory in [path, *path.parents]:
        for name in CONTEXT_NAMES:
            add(directory / name, "eager")

    truncated = False
    visited = 0
    base_depth = len(path.parts)
    for dirpath, dirnames, filenames in os.walk(path):
        visited += 1
        if visited > max_dirs:
            truncated = True
            break
        depth = len(Path(dirpath).parts) - base_depth
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")] if depth < max_depth else []
        if depth == 0:
            continue
        for name in CONTEXT_NAMES:
            if name in filenames:
                add(Path(dirpath) / name, "nested")
    return files, truncated


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _mcp_servers(path: Path, home: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    claude = _json(home / ".claude.json")
    out["claude user"] = sorted((claude.get("mcpServers") or {}).keys())
    project = (claude.get("projects") or {}).get(str(path), {}) or {}
    out["claude project (~/.claude.json)"] = sorted((project.get("mcpServers") or {}).keys())
    out["claude .mcp.json"] = sorted((_json(path / ".mcp.json").get("mcpServers") or {}).keys())
    out["gemini user"] = sorted((_json(home / ".gemini" / "settings.json").get("mcpServers") or {}).keys())
    out["gemini project"] = sorted((_json(path / ".gemini" / "settings.json").get("mcpServers") or {}).keys())
    return {k: v for k, v in out.items() if v}


_KEY = re.compile(r"^[A-Za-z0-9_-]+:")


def _skill_description(skill_md: Path) -> str:
    text = skill_md.read_text(encoding="utf-8", errors="ignore")
    if not text.startswith("---"):
        return ""
    front = text.split("---", 2)[1] if text.count("---") >= 2 else ""
    desc: list[str] = []
    capture = False
    for line in front.splitlines():
        if line.startswith("description:"):
            capture = True
            desc.append(line.split(":", 1)[1].strip().lstrip(">|").strip())
        elif capture:
            if _KEY.match(line):
                break
            desc.append(line.strip())
    return " ".join(d for d in desc if d)


def _skills(path: Path, home: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for label, root in (
        ("claude user", home / ".claude" / "skills"),
        ("claude project", path / ".claude" / "skills"),
        ("gemini user", home / ".gemini" / "skills"),
        ("gemini project", path / ".gemini" / "skills"),
        ("agents user", home / ".agents" / "skills"),
        ("agents project", path / ".agents" / "skills"),
    ):
        if not root.is_dir():
            continue
        count, chars = 0, 0
        for child in sorted(root.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.is_file():
                count += 1
                chars += len(child.name) + len(_skill_description(skill_md))
        if count:
            out[label] = {"count": count, "tokens": est_tokens(chars)}
    return out


def audit(path: Path, home: Path | None = None, max_depth: int = 4, max_dirs: int = 5000) -> dict:
    path = path.expanduser().resolve()
    home = (home or Path.home()).resolve()
    ctx, truncated = _context_files(path, home, max_depth, max_dirs)
    skills = _skills(path, home)
    mcp = _mcp_servers(path, home)
    startup_ctx = sum(f["tokens"] for f in ctx if f["scope"] in ("global", "eager"))
    nested_ctx = sum(f["tokens"] for f in ctx if f["scope"] == "nested")
    skill_tokens = sum(s["tokens"] for s in skills.values())
    return {
        "path": str(path),
        "context_files": ctx,
        "scan_truncated": truncated,
        "mcp_servers": mcp,
        "mcp_server_count": sum(len(v) for v in mcp.values()),
        "skills": skills,
        "skill_count": sum(s["count"] for s in skills.values()),
        "tokens": {
            "context_startup": startup_ctx,
            "context_nested": nested_ctx,
            "skills": skill_tokens,
            "estimated_startup_total": startup_ctx + skill_tokens,
        },
    }


def format_report(report: dict) -> str:
    t = report["tokens"]
    lines = [
        f"== {report['path']}",
        f"context files: {len(report['context_files'])} "
        f"(startup ~{t['context_startup']} tok, nested ~{t['context_nested']} tok)"
        + (" [scan truncated]" if report["scan_truncated"] else ""),
    ]
    for f in sorted(report["context_files"], key=lambda f: -f["tokens"])[:8]:
        lines.append(f"  {f['tokens']:>6} tok  {f['scope']:<6} {f['path']}")
    lines.append(f"skills: {report['skill_count']} (~{t['skills']} tok of descriptions)")
    for label, s in report["skills"].items():
        lines.append(f"  {s['count']:>3} {label}")
    lines.append(f"mcp servers: {report['mcp_server_count']} (tool schemas not estimated)")
    for label, names in report["mcp_servers"].items():
        lines.append(f"  {label}: {', '.join(names)}")
    lines.append(f"estimated startup total (context + skills): ~{t['estimated_startup_total']} tok")
    return "\n".join(lines)
