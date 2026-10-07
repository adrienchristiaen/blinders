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
from .graph import build_graphs
from .mcp import claude_config, discover, missing_names, select_mcp
from .scan import Repo, build_index, load_index
from .select import plan, rank
from .workspace import create_session, prune_sessions


def _err(msg: str) -> int:
    print(f"blind: {msg}", file=sys.stderr)
    return 2


def _split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def _add_run_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("cli", help="claude, gemini, codex, vibe, or a CLI defined in config.toml")
    s.add_argument("prompt", nargs="*", help="initial prompt; also used to select repos")
    s.add_argument("-r", "--repos", help="comma-separated repo names (or paths); skips automatic selection")
    s.add_argument("--max", type=int, help="max repos to open automatically")
    s.add_argument("--related", choices=("auto", "all", "none"), default="auto",
                   help="related repos (deploy, data...): auto = open those whose role matches the prompt, "
                        "all = open the strongest ones anyway, none = ignore relations")
    s.add_argument("--mcp", help="MCP servers to keep: auto (match the prompt), all, none, or names a,b")
    s.add_argument("--primary", action="store_true", help="start inside the first opened repo instead of the blind workspace")
    s.add_argument("--link", action="store_true", help="symlink opened repos into the blind workspace instead of using flags")
    s.add_argument("--dry-run", action="store_true", help="print the command and exit")


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
    s.add_argument("--related", choices=("auto", "all", "none"), default="auto")

    _add_run_args(sub.add_parser("run", help="start a CLI blind, opening only the selected repos"))

    s = sub.add_parser("mcp", help="show which MCP servers a prompt would keep")
    s.add_argument("--cli", default="gemini")
    s.add_argument("prompt", nargs="*")

    s = sub.add_parser("graph", help="build Graphify code graphs for repos (local, no LLM)")
    s.add_argument("repos", nargs="*", help="repo names; empty with --all for every indexed repo")
    s.add_argument("--all", action="store_true")
    s.add_argument("--update", action="store_true", help="refresh existing graphs")
    s.add_argument("--dry-run", action="store_true")

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
    links = sum(len(r.links) for r in repos)
    print(f"indexed {len(repos)} repos ({links} links) in {(time.perf_counter() - t0) * 1000:.0f} ms")
    return 0


def cmd_list(args, cfg: Config) -> int:
    for r in load_index(cfg, refresh=args.refresh):
        roles = ",".join(r.roles) or "-"
        print(f"{r.name}\t{roles}\t{r.path}\t{r.description[:70]}")
    return 0


def cmd_select(args, cfg: Config) -> int:
    prompt = " ".join(args.prompt)
    repos = load_index(cfg)
    t0 = time.perf_counter()
    if args.all:
        rows = [(c.score, c.repo.name, c.reason, "rank") for c in rank(prompt, repos)[:10]]
        related_rows: list = []
    else:
        p = plan(prompt, repos, cfg, related=args.related)
        rows = [(c.score, c.repo.name, c.reason, "open") for c in p.opened]
        related_rows = [(r.weight, r.repo.name, r.why, "related, closed") for r in p.related]
    ms = (time.perf_counter() - t0) * 1000
    if args.json:
        print(json.dumps([{"name": n, "score": round(s, 3), "reason": why, "state": st} for s, n, why, st in rows + related_rows]))
        return 0
    if not rows:
        print("no repo matches: the session would stay fully blind")
    for s, n, why, st in rows + related_rows:
        print(f"{s:>9.2f}  {n:<28} {st:<16} ({why})")
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

    forced = None
    if args.repos:
        forced, missing = _resolve_forced(args.repos, repos)
        if missing:
            return _err(f"unknown repo(s): {', '.join(missing)} (see `blind list`)")
    p = plan(prompt, repos, cfg, related=args.related, forced=forced) if (forced is not None or prompt) else None
    opened_choices = p.opened if p else []
    opened = [c.repo for c in opened_choices]
    related = p.related if p else []
    closed = [r for r in repos if r not in opened]
    link = args.link or adapter.dir_style == "link"

    # MCP servers
    mcp_names = None
    mcp_cfg = None
    mcp_dropped: list[str] = []
    mcp_note = ""
    spec = args.mcp or ("auto" if adapter.mcp_auto else None)
    if spec and adapter.mcp_style == "none":
        mcp_note = f"; --mcp ignored ({adapter.name} has no MCP filter here)"
    elif spec:
        servers = discover(adapter.mcp_style, Path.home(), cfg)
        bad = missing_names(spec, servers)
        if bad:
            return _err(f"unknown MCP server(s): {', '.join(bad)} (configured: {', '.join(s.name for s in servers) or 'none'})")
        if servers:
            mplan = select_mcp(prompt, servers, cfg, spec)
            mcp_dropped = [s.name for s in mplan.dropped]
            if adapter.mcp_style == "allowlist":
                mcp_names = [s.name for s in mplan.kept]
            else:
                mcp_cfg = claude_config(mplan)
            kept = ", ".join(s.name for s in mplan.kept) or "none"
            mcp_note = f"; MCP kept: {kept}" + (f" (dropped {len(mcp_dropped)})" if mcp_dropped else "")

    if not args.dry_run:
        prune_sessions(cfg)
    session = create_session(
        opened, closed, cfg, link=link and not args.primary, related=related,
        mcp_dropped=mcp_dropped, mcp_config=mcp_cfg, cli=adapter.name,
    )
    if args.primary:
        if not opened:
            return _err("--primary needs at least one opened repo (pass -r or a matching prompt)")
        cwd = opened[0].path
        dirs = [r.path for r in opened[1:]]
        workspace_note = f"primary repo {opened[0].name}"
    else:
        cwd = str(session)
        dirs = [] if link else [r.path for r in opened]
        workspace_note = f"workspace {session}"

    mcp_file = str(session / "mcp.json") if mcp_cfg is not None else None
    cmd = build_command(adapter, prompt, dirs, extra, mcp_names=mcp_names, mcp_file=mcp_file)
    names = ", ".join(r.name for r in opened) or "nothing (fully blind)"
    rel = f"; related, closed: {', '.join(r.repo.name for r in related)}" if related else ""
    print(f"blind: opened {names}{rel}; {len(closed)} closed{mcp_note}; {workspace_note}", file=sys.stderr)
    if args.dry_run:
        print(f"cd {shlex.quote(cwd)} && {shlex.join(cmd)}")
        return 0
    if shutil.which(cmd[0]) is None:
        return _err(f"'{cmd[0]}' not found in PATH")
    os.chdir(cwd)
    os.execvp(cmd[0], cmd)
    return 0  # unreachable


def cmd_mcp(args, cfg: Config) -> int:
    try:
        adapter = get_adapter(args.cli, cfg)
    except (KeyError, ValueError) as exc:
        return _err(str(exc))
    servers = discover(adapter.mcp_style, Path.home(), cfg)
    if not servers:
        print(f"no MCP servers found for {adapter.name} (or {adapter.name} has no MCP filter)")
        return 0
    mplan = select_mcp(" ".join(args.prompt), servers, cfg, "auto")
    for s in mplan.kept:
        print(f"keep  {s.name}  ({mplan.reasons.get(s.name, '')})")
    for s in mplan.dropped:
        print(f"drop  {s.name}")
    return 0


def cmd_graph(args, cfg: Config) -> int:
    repos = load_index(cfg)
    if args.all:
        targets = repos
    elif args.repos:
        targets, missing = _resolve_forced(",".join(args.repos), repos)
        if missing:
            return _err(f"unknown repo(s): {', '.join(missing)}")
    else:
        return _err("name repos to graph, or pass --all")
    try:
        results = build_graphs(targets, cfg, update=args.update, dry_run=args.dry_run)
    except FileNotFoundError as exc:
        return _err(str(exc))
    failed = [r for r in results if r.status == "failed"]
    for r in failed:
        print(f"blind: {r.repo.name}: {r.detail}", file=sys.stderr)
    if not args.dry_run:
        build_index(cfg)  # pick up the new graph reports
    print(f"{sum(r.status in ('built', 'updated') for r in results)} graph(s) built, "
          f"{sum(r.status == 'skipped' for r in results)} skipped, {len(failed)} failed")
    return 1 if failed else 0


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
    cfg = load_config()
    if argv[:1] == ["run"]:
        # Options may sit between prompt words; plain parse_args rejects that on Python < 3.12.
        run_parser = argparse.ArgumentParser(prog="blind run")
        _add_run_args(run_parser)
        args = run_parser.parse_intermixed_args(argv[1:])
        return cmd_run(args, cfg, extra)
    args = _parser().parse_args(argv)
    handlers = {
        "init": cmd_init, "list": cmd_list, "select": cmd_select, "mcp": cmd_mcp,
        "graph": cmd_graph, "audit": cmd_audit, "clean": cmd_clean,
    }
    return handlers[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
