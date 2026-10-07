"""Anonymous launch statistics, to compare sessions started with and without ``blind``.

What is recorded (``~/.cache/blinders/launches.jsonl``, owner-only): counts and estimates only,
plus the working directory of the launch, which stays on this machine and is used to find the CLI's
own session file for token usage. A report (``blind stats``) never contains repo names, paths,
prompts or file contents: only numbers, the CLI name and the mode (blind or plain).

Token usage is read from the CLI's own session files and only numeric ``usage`` fields are
extracted; message text is never kept. Claude Code's format is known (``message.usage``). Gemini
CLI's session files are read on a best-effort basis (``tokens`` objects per message) and that format
was not verified: when nothing is found, add the numbers by hand with ``blind stats --note``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import statistics
import time
from pathlib import Path

from .audit import audit, est_tokens
from .config import cache_dir
from . import skills as skillmod


def log_path() -> Path:
    return cache_dir() / "launches.jsonl"


def _append(record: dict) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def read_log() -> list[dict]:
    try:
        lines = log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def estimate_startup(cwd: str, family: str, hidden: set[str], cfg, home: Path | None = None) -> dict:
    """Chars/4 estimate of what the CLI loads from ``cwd``: context files and visible skill descriptions."""
    home = home or Path.home()
    a = audit(Path(cwd), home)
    installed = skillmod.discover(family, home, cfg)
    visible = [s for s in installed if s.name not in hidden]
    skills_tok = sum(est_tokens(len(s.name) + len(s.description)) for s in visible)
    return {
        "context_eager": a["tokens"]["context_startup"],
        "context_nested": a["tokens"]["context_nested"],
        "skills_total": len(installed),
        "skills_visible": len(visible),
        "skills_tokens": skills_tok,
        "est_startup": a["tokens"]["context_startup"] + skills_tok,
    }


def record_launch(*, cli: str, mode: str, cwd: str, prompt: str, repos_total: int, repos_opened: int,
                  mcp_total: int | None, mcp_kept: int | None, estimate: dict, hints_files: int = 0,
                  sync: dict | None = None) -> str:
    rec_id = secrets.token_hex(3)
    _append({
        "kind": "launch", "id": rec_id, "ts": time.time(), "cli": cli, "mode": mode, "cwd": cwd,
        "prompt_chars": len(prompt), "repos_total": repos_total, "repos_opened": repos_opened,
        "mcp_total": mcp_total, "mcp_kept": mcp_kept, "hints_files": hints_files,
        "sync": sync or {}, **estimate,
    })
    return rec_id


def add_note(rec_id: str, values: dict[str, int]) -> None:
    _append({"kind": "note", "id": rec_id, **values})


# --- reading the CLIs' own session files (numbers only) -------------------------------------------

def _num(v) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _claude_usage(cwd: str, since: float, home: Path) -> dict | None:
    folder = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    if not folder.is_dir():
        return None
    best: tuple[float, list[dict]] | None = None
    for f in folder.glob("*.jsonl"):
        try:
            if f.stat().st_mtime < since:
                continue
            seen: dict[str, dict] = {}
            order: list[str] = []
            first_ts = ""
            with f.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    m = d.get("message") if isinstance(d, dict) else None
                    if d.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                        continue
                    key = str(m.get("id") or f"{f.name}:{len(order)}")
                    if key not in seen:
                        order.append(key)
                        first_ts = first_ts or str(d.get("timestamp", ""))
                    seen[key] = m["usage"]
        except OSError:
            continue
        if not order:
            continue
        started = _parse_ts(first_ts)
        if started < since - 5:
            continue
        if best is None or started < best[0]:
            best = (started, [seen[k] for k in order])
    if best is None:
        return None
    return _summarize(best[1], "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens",
                      cached_keys=("cache_creation_input_tokens", "cache_read_input_tokens"))


def _summarize(usages: list[dict], input_key: str, *rest, cached_keys=()) -> dict:
    out_key = rest[-1]
    in_keys = (input_key, *rest[:-1])
    first = sum(_num(usages[0].get(k)) for k in in_keys)
    total_in = sum(_num(u.get(k)) for u in usages for k in in_keys)
    return {
        "first_turn_tokens": first,
        "turns": len(usages),
        "input_tokens": total_in,
        "output_tokens": sum(_num(u.get(out_key)) for u in usages),
    }


def _parse_ts(value: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _gemini_usage(cwd: str, since: float, home: Path) -> dict | None:
    """Best effort, format not verified: ~/.gemini/tmp/<sha256 of cwd>/chats/*.json with a ``tokens`` object per message."""
    folder = home / ".gemini" / "tmp" / hashlib.sha256(cwd.encode()).hexdigest() / "chats"
    if not folder.is_dir():
        return None
    files = sorted((f for f in folder.glob("*.json") if f.stat().st_mtime >= since), key=lambda f: f.stat().st_mtime)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        msgs = data.get("messages") if isinstance(data, dict) else None
        usages = [m["tokens"] for m in msgs or [] if isinstance(m, dict) and isinstance(m.get("tokens"), dict)]
        if usages:
            return _summarize(usages, "input", "output")
    return None


def session_usage(rec: dict, home: Path | None = None) -> dict | None:
    home = home or Path.home()
    since, cwd = float(rec.get("ts", 0)), str(rec.get("cwd", ""))
    if rec.get("cli") == "claude":
        return _claude_usage(cwd, since, home)
    if rec.get("cli") == "gemini":
        return _gemini_usage(cwd, since, home)
    return None


# --- report ----------------------------------------------------------------------------------------

REPORT_FIELDS = ("id", "when", "cli", "mode", "repos", "mcp", "skills", "est_startup", "first_turn_tokens", "turns", "total_tokens")


def build_report(last: int = 20, home: Path | None = None) -> list[dict]:
    records = read_log()
    notes: dict[str, dict] = {}
    for r in records:
        if r.get("kind") == "note":
            notes.setdefault(str(r.get("id")), {}).update({k: v for k, v in r.items() if k not in ("kind", "id")})
    rows = []
    for r in [x for x in records if x.get("kind") == "launch"][-last:]:
        usage = session_usage(r, home) or {}
        usage = {**usage, **notes.get(str(r["id"]), {})}
        total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0) if usage else None
        rows.append({
            "id": r["id"],
            "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ts"])),
            "cli": r["cli"], "mode": r["mode"],
            "repos": f"{r['repos_opened']}/{r['repos_total']}" if r.get("repos_total") else "-",
            "mcp": f"{r['mcp_kept']}/{r['mcp_total']}" if r.get("mcp_total") is not None and r.get("mcp_kept") is not None else "-",
            "skills": f"{r.get('skills_visible', '-')}/{r.get('skills_total', '-')}",
            "est_startup": r.get("est_startup"),
            "first_turn_tokens": usage.get("first_turn_tokens"),
            "turns": usage.get("turns"),
            "total_tokens": total or None,
        })
    return rows


def format_report(rows: list[dict]) -> str:
    if not rows:
        return "no launches recorded yet (use `blind ...` and `blind <cli> --plain ...`)"
    head = ("id", "when", "cli", "mode", "repos", "mcp", "skills", "est.startup", "1st turn", "turns", "total")
    table = [head] + [tuple("-" if row[k] is None else str(row[k]) for k in REPORT_FIELDS) for row in rows]
    widths = [max(len(r[i]) for r in table) for i in range(len(head))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in table]
    lines.append("")
    lines.append("repos/mcp/skills: kept or opened / total. est.startup: chars/4 estimate of context files + visible skill "
                 "descriptions. 1st turn / total: read from the CLI's own session file (numbers only).")
    by_mode: dict[str, list[int]] = {}
    for row in rows:
        if row["first_turn_tokens"]:
            by_mode.setdefault(row["mode"], []).append(row["first_turn_tokens"])
    for mode, vals in sorted(by_mode.items()):
        lines.append(f"median first-turn tokens, {mode}: {int(statistics.median(vals))} over {len(vals)} session(s)")
    lines.append("No repo names, paths, prompts or file contents are included: safe to paste.")
    return "\n".join(lines)
