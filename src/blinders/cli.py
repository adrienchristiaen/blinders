"""``blind`` command line."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .adapters import build_command, get_adapter
from .audit import audit, format_report
from .config import Config, config_dir, load_config
from .scan import Repo, build_index, load_index
from .select import rank, select
from .workspace import create_session, prune_sessions


def _err(msg: str) -> int:
    print(f"blind: {msg}", file=sys.stderr)
    return 2


def _split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="blind", description="Start agent CLIs blind; open only the repos you need.")
    p.add_argument("--version", action="version", version=f"blinders {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write a config (if given roots) and build the repo index")
    s.add_argument("roots", nargs="*", help="directories that contain your repos")

    s = sub.add_parser("list", help="list indexed repos")
    s.add_argument("--refresh", action="store_true")

    s = sub.add_parser("select", help="show which repos a prompt would open")
    s.add_argument("prompt", nargs="+")
    s.add_argument("--json", action="store_true")
    s.add_argument("--all", action="store_true", help="show the full ranking, not just the selection")

    s = sub.add_parser("run", help="start a CLI blind, opening only the selected repos")
    s.add_argument("cli", help="claude, gemini, codex, vibe, or a CLI defined in config.toml")
    s.add_argument("prompt", nargs="*", help="initial prompt; also used to select repos")
    s.add_argument("-r", "--repos", help="comma-separated repo names (or paths); skips automatic selection")
    s.add_argument("--max", type=int, help="max repos to open automatically")
    s.add_argument("--primary", action="store_true", help="start inside the first opened repo instead of the blind workspace")
    s.add_argument("--link", action="store_true", help="symlink opened repos into the blind workspace instead of using flags")
    s.add_argument("--dry-run", action="store_true", help="print the command and exit")

    s = sub.add_parser("audit", help="estimate what a CLI loads at startup from a directory")
    s.add_argument("paths", nargs="*", default=["."])
    s.add_argument("--json", action="store_true")

    sub.add_parser("clean", help="remove all blind workspaces")
    return p


def cmd_init(args, cfg: Config) -> int:
    cfg_file = config_dir() / "config.toml"
    if args.roots:
        if cfg_file.exists():
            return _err(f"{cfg_file} already exists; edit its 'roots' instead")
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        roots = ", ".join(json.dumps(str(Path(r).expanduser().resolve())) for r in args.roots)
        cfg_file.write_text(f"roots = [{roots}]\n", encoding="utf-8")
        print(f"wrote {cfg_file}")
        cfg = load_config(cfg_file)
    if not cfg.roots:
        return _err("no roots configured: run `blind init <dir> [<dir> ...]`")
    t0 = time.perf_counter()
    repos = build_index(cfg)
    print(f"indexed {len(repos)} repos in {(time.perf_counter() - t0) * 1000:.0f} ms")
    return 0


def cmd_list(args, cfg: Config) -> int:
    for r in load_index(cfg, refresh=args.refresh):
        print(f"{r.name}\t{r.path}\t{r.description[:80]}")
    return 0


def cmd_select(args, cfg: Config) -> int:
    prompt = " ".join(args.prompt)
    repos = load_index(cfg)
    t0 = time.perf_counter()
    choices = rank(prompt, repos)[:10] if args.all else select(prompt, repos, cfg)
    ms = (time.perf_counter() - t0) * 1000
    if args.json:
        print(json.dumps([{"name": c.repo.name, "path": c.repo.path, "score": round(c.score, 3), "reason": c.reason} for c in choices]))
        return 0
    if not choices:
        print("no repo matches: the session would stay fully blind")
    for c in choices:
        print(f"{c.score:>9.2f}  {c.repo.name}  ({c.reason})")
    print(f"selection took {ms:.1f} ms over {len(repos)} repos", file=sys.stderr)
    return 0


def _resolve_forced(spec: str, repos: list[Repo]) -> tuple[list[Repo], list[str]]:
    by_name = {r.name: r for r in repos}
    by_path = {r.path: r for r in repos}
    found, missing = [], []
    for item in (s.strip() for s in spec.split(",") if s.strip()):
        repo = by_name.get(item) or by_path.get(str(Path(item).expanduser().resolve()))
        (found if repo else missing).append(repo or item)
    return found, missing


def cmd_run(args, cfg: Config, extra: list[str]) -> int:
    try:
        adapter = get_adapter(args.cli, cfg)
    except (KeyError, ValueError) as exc:
        return _err(str(exc))
    if args.max is not None:
        cfg.max_repos = args.max
    prompt = " ".join(args.prompt).strip()
    repos = load_index(cfg)
    if not repos:
        return _err("no repos indexed: run `blind init <dir>` first")

    if args.repos:
        opened, missing = _resolve_forced(args.repos, repos)
        if missing:
            return _err(f"unknown repo(s): {', '.join(missing)} (see `blind list`)")
    elif prompt:
        opened = [c.repo for c in select(prompt, repos, cfg)]
    else:
        opened = []
    closed = [r for r in repos if r not in opened]
    link = args.link or adapter.dir_style == "link"

    if args.primary:
        if not opened:
            return _err("--primary needs at least one opened repo (pass -r or a matching prompt)")
        cwd = opened[0].path
        dirs = [r.path for r in opened[1:]]
        workspace_note = f"primary repo {opened[0].name}"
        link = False
    else:
        if not args.dry_run:
            prune_sessions(cfg)
        session = create_session(opened, closed, cfg, link=link)
        cwd = str(session)
        dirs = [] if link else [r.path for r in opened]
        workspace_note = f"workspace {session}"

    cmd = build_command(adapter, prompt, dirs, extra)
    names = ", ".join(r.name for r in opened) or "nothing (fully blind)"
    print(f"blind: opened {names}; {len(closed)} closed; {workspace_note}", file=sys.stderr)
    if args.dry_run:
        print(f"cd {shlex.quote(cwd)} && {shlex.join(cmd)}")
        return 0
    if shutil.which(cmd[0]) is None:
        return _err(f"'{cmd[0]}' not found in PATH")
    os.chdir(cwd)
    os.execvp(cmd[0], cmd)
    return 0  # unreachable


def cmd_audit(args, cfg: Config) -> int:
    reports = [audit(Path(p)) for p in args.paths]
    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        print("\n\n".join(format_report(r) for r in reports))
    return 0


def cmd_clean(args, cfg: Config) -> int:
    print(f"removed {prune_sessions(cfg, everything=True)} workspace(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, extra = _split_passthrough(argv)
    args = _parser().parse_args(argv)
    cfg = load_config()
    if args.cmd == "run":
        return cmd_run(args, cfg, extra)
    handlers = {"init": cmd_init, "list": cmd_list, "select": cmd_select, "audit": cmd_audit, "clean": cmd_clean}
    return handlers[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
