"""``blind`` command line."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .adapters import DEFAULTS, Adapter, build_command, get_adapter
from .audit import audit, format_report
from .config import Config, config_dir, load_config
from .gitsync import SyncResult, graph_state, sync_many
from .graph import build_graphs
from .hints import Hints, find_hints, render_hints
from . import stats as statsmod
from . import skills as skillmod
from .mcp import McpPlan, claude_config, discover, missing_names, select_mcp
from .scan import Repo, build_index, load_index
from .select import Plan, plan, rank
from .workspace import create_session, prune_sessions


def _version_text() -> str:
    ui = "full-screen launcher: available" if ui_available() else "full-screen launcher: Textual missing"
    return f"blinders {__version__} (python {sys.version_info.major}.{sys.version_info.minor} at {sys.executable}; {ui})"


def _err(msg: str) -> int:
    print(f"blind: {msg}", file=sys.stderr)
    return 2


def _split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def _add_launch_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("prompt", nargs="*", help="initial prompt; also used to select repos")
    s.add_argument("-r", "--repos", help="comma-separated repo names (or paths); skips automatic selection")
    s.add_argument("--max", type=int, help="max repos to open automatically")
    s.add_argument("--related", choices=("auto", "all", "none"), default="auto",
                   help="related repos (deploy, data...): auto = open those whose role matches the prompt, "
                        "all = open the strongest ones anyway, none = ignore relations")
    s.add_argument("--mcp", help="MCP servers to keep: auto (match the prompt), all, none, or names a,b")
    s.add_argument("--skills", help="skills to keep visible: auto (match the prompt), all, none, or names a,b")
    s.add_argument("--primary", action="store_true", help="start inside the first opened repo instead of the blind workspace")
    s.add_argument("--link", action="store_true", help="symlink opened repos into the blind workspace instead of using flags")
    s.add_argument("--dry-run", action="store_true", help="print the command and exit")
    s.add_argument("--sync", choices=("off", "safe", "switch"),
                   help="before reading opened repos: off, safe (fast-forward repos already on their root branch), "
                        "switch (check out the root branch first). Default from config ([sync] on_launch)")
    s.add_argument("--no-sync", action="store_true", help="same as --sync off")
    s.add_argument("--no-hints", action="store_true", help="do not add starting points from the code graph")
    s.add_argument("--no-ui", action="store_true", help="use the text prompt instead of the full-screen launcher")
    s.add_argument("--plain", action="store_true", help="start the CLI unmodified here (for comparison); still recorded in `blind stats`")
    s.add_argument("--confirm", action="store_true", help="show the plan and wait for Enter before launching")
    s.add_argument("-y", "--yes", action="store_true", help="never ask for confirmation")


def _add_run_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("cli", help="claude, gemini, codex, vibe, or a CLI defined in config.toml")
    _add_launch_args(s)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="blind", description="Start agent CLIs blind; open only the repos you need.")
    p.add_argument("--version", action="version", version=_version_text())
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="first-run wizard: pick your repo directories, index them, build code graphs")

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

    s = sub.add_parser("sync", help="fetch, check out the root branch and fast-forward repos, then refresh stale code graphs")
    s.add_argument("repos", nargs="*", help="repo names; empty = every indexed repo")
    s.add_argument("--safe", action="store_true", help="do not switch branches: only fast-forward repos already on their root branch")
    s.add_argument("--no-graph", action="store_true", help="skip the Graphify refresh")

    sub.add_parser("status", help="which repos are on which branch, and whose code graph is missing or stale (local, no network)")

    s = sub.add_parser("stats", help="anonymous numbers for sessions started with and without blind (safe to paste)")
    s.add_argument("--last", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.add_argument("note", nargs="*", metavar="note ID key=value",
                   help="attach numbers by hand, e.g. `blind stats note a1b2c3 first_turn_tokens=21000 input_tokens=90000 output_tokens=4000`")

    sub.add_parser("doctor", help="show which Python, UI, Graphify, git and CLIs blind can see")

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


MAX_ARG_PROMPT_BYTES = 100_000


class LaunchError(Exception):
    pass


@dataclass
class LaunchPlan:
    adapter: Adapter
    prompt: str
    repos: list[Repo]
    seed: Plan | None
    opened: list[Repo]
    related: list
    closed: list[Repo]
    link: bool
    primary: bool
    mcp_plan: McpPlan | None = None
    mcp_note: str = ""
    skills_plan: skillmod.SkillPlan | None = None
    skills_note: str = ""
    extra: list[str] = field(default_factory=list)
    sync_mode: str = "off"
    use_hints: bool = False
    hints: dict[str, Hints] = field(default_factory=dict)
    prep: dict[str, int] = field(default_factory=dict)


def make_plan(cfg: Config, adapter: Adapter, prompt: str, repos: list[Repo], opts, extra: list[str],
              forced: list[Repo] | None = None) -> LaunchPlan:
    """Decide repos, MCP servers and skills for one launch. Touches no files."""
    if opts.repos and forced is None:
        forced, missing = _resolve_forced(opts.repos, repos)
        if missing:
            raise LaunchError(f"unknown repo(s): {', '.join(missing)} (see `blind list`)")
    seed = plan(prompt, repos, cfg, related=opts.related, forced=forced) if (forced is not None or prompt) else None
    opened = [c.repo for c in seed.opened] if seed else []
    related = seed.related if seed else []
    closed = [r for r in repos if r not in opened]
    lp = LaunchPlan(adapter, prompt, repos, seed, opened, related, closed,
                    link=(opts.link or adapter.dir_style == "link") and not opts.primary,
                    primary=opts.primary, extra=extra)
    lp.sync_mode = "off" if opts.no_sync else (opts.sync or cfg.sync_on_launch)
    lp.use_hints = cfg.hints_enabled and not opts.no_hints and bool(prompt)

    spec = opts.mcp or ("auto" if adapter.mcp_auto else None)
    if spec and adapter.mcp_style == "none":
        lp.mcp_note = f"--mcp ignored ({adapter.name} has no MCP filter here)"
    elif spec:
        servers = discover(adapter.mcp_style, Path.home(), cfg)
        bad = missing_names(spec, servers)
        if bad:
            raise LaunchError(f"unknown MCP server(s): {', '.join(bad)} (configured: {', '.join(s.name for s in servers) or 'none'})")
        if servers:
            lp.mcp_plan = select_mcp(prompt, servers, cfg, spec)

    sspec = opts.skills or ("auto" if adapter.skills_auto else None)
    if sspec and adapter.skills_style == "none":
        lp.skills_note = f"--skills ignored ({adapter.name} has no skills filter here)"
    elif sspec and adapter.skills_style == "gemini-workspace" and opts.primary:
        lp.skills_note = "skills not filtered with --primary (the filter lives in the blind workspace)"
    elif sspec:
        installed = skillmod.discover(adapter.skills_style.split("-")[0], Path.home(), cfg)
        bad = skillmod.missing_names(sspec, installed)
        if bad:
            raise LaunchError(f"unknown skill(s): {', '.join(bad)}")
        if installed:
            lp.skills_plan = skillmod.select_skills(prompt, installed, cfg, sspec)
    return lp


def describe_plan(lp: LaunchPlan) -> str:
    """What blind is about to do, in a few lines."""
    def names(items) -> str:
        return ", ".join(items) or "none"

    lines = [f"blind -> {lp.adapter.name}"]
    lines.append(f"  repos opened    {names(r.name for r in lp.opened) if lp.opened else 'none (fully blind)'}")
    if lp.related:
        lines.append(f"  related, closed {names(r.repo.name for r in lp.related)}")
    lines.append(f"  repos closed    {len(lp.closed)}")
    if lp.mcp_plan is not None:
        lines.append(f"  MCP kept        {names(s.name for s in lp.mcp_plan.kept)}"
                     + (f"   (dropped {len(lp.mcp_plan.dropped)}: {names(s.name for s in lp.mcp_plan.dropped)})" if lp.mcp_plan.dropped else ""))
    if lp.skills_plan is not None:
        lines.append(f"  skills kept     {names(s.name for s in lp.skills_plan.kept)}"
                     + (f"   (hidden {len(lp.skills_plan.dropped)})" if lp.skills_plan.dropped else ""))
    for note in (lp.mcp_note, lp.skills_note):
        if note:
            lines.append(f"  note            {note}")
    return "\n".join(lines)


def materialize(cfg: Config, lp: LaunchPlan, dry_run: bool) -> tuple[str, list[str], Path]:
    """Create the blind workspace and build the final command: (cwd, argv, session)."""
    adapter, opened = lp.adapter, lp.opened
    if lp.primary and not opened:
        raise LaunchError("--primary needs at least one opened repo (pass -r or a matching prompt)")
    mcp_names = mcp_cfg = None
    if lp.mcp_plan is not None:
        if adapter.mcp_style == "allowlist":
            mcp_names = [s.name for s in lp.mcp_plan.kept]
        else:
            mcp_cfg = claude_config(lp.mcp_plan)
    skills_settings = gemini_settings = None
    if lp.skills_plan is not None and lp.skills_plan.dropped:
        if adapter.skills_style == "claude-settings":
            skills_settings = skillmod.claude_settings(lp.skills_plan)
        elif adapter.skills_style == "gemini-workspace":
            gemini_settings = skillmod.gemini_settings(lp.skills_plan)

    if not dry_run:
        prune_sessions(cfg)
    session = create_session(
        opened, lp.closed, cfg, link=lp.link, related=lp.related,
        mcp_dropped=[s.name for s in lp.mcp_plan.dropped] if lp.mcp_plan else None,
        mcp_config=mcp_cfg, cli=adapter.name,
        skills_dropped=[s.name for s in lp.skills_plan.dropped] if lp.skills_plan else None,
        skills_settings=skills_settings, gemini_settings=gemini_settings,
        hints={name: render_hints(h, "") for name, h in lp.hints.items()},
    )
    if lp.primary:
        cwd, dirs = opened[0].path, [r.path for r in opened[1:]]
    else:
        cwd, dirs = str(session), ([] if lp.link else [r.path for r in opened])
    prompt = lp.prompt
    if len(prompt.encode("utf-8")) > MAX_ARG_PROMPT_BYTES:
        # One argument is capped near 128 KB on Linux: hand a huge paste over as a file instead of failing.
        (session / "PROMPT.md").write_text(prompt, encoding="utf-8")
        prompt = f"My full request is too long to pass directly. Read it in {session / 'PROMPT.md'} first, then carry it out."
        _say(f"blind: prompt is {len(lp.prompt.encode('utf-8')) // 1024} KB, passed as a file: {session / 'PROMPT.md'}")
    cmd = build_command(
        adapter, prompt, dirs, lp.extra, mcp_names=mcp_names,
        mcp_file=str(session / "mcp.json") if mcp_cfg is not None else None,
        settings_file=str(session / "skills-settings.json") if skills_settings is not None else None,
    )
    return cwd, cmd, session


def _family(adapter: Adapter) -> str:
    return adapter.skills_style.split("-")[0] if adapter.skills_style != "none" else ""


def prepare(cfg: Config, lp: LaunchPlan, dry_run: bool) -> None:
    """Bring opened repos up to date, refresh stale code graphs, then compute starting points.

    Dry runs change nothing: no git, no Graphify, only hints from graphs that already exist."""
    opened = lp.opened
    if opened and not dry_run and lp.sync_mode != "off":
        results = sync_many([r.path for r in opened], cfg, switch=lp.sync_mode == "switch")
        by_path = {r.path: r.name for r in opened}
        for res in results:
            name = by_path.get(res.path, res.path)
            if res.status == "updated":
                _say(f"blind: sync {name}: pulled {res.moved} commit(s) on {res.branch}")
            elif res.status in ("left-on-branch", "skipped", "failed"):
                _say(f"blind: sync {name}: {res.status}, {res.detail}")
            lp.prep[res.status] = lp.prep.get(res.status, 0) + 1
    if opened and not dry_run and shutil.which(cfg.graphify_bin) is not None:
        todo = [r for r in opened if graph_state(r.graph_report, r.path) in ("none", "stale")]
        if todo:
            for res in build_graphs(todo, cfg, log=lambda *_: None):
                _say(f"blind: graph {res.repo.name}: {res.status}" + (f", {res.detail}" if res.detail else ""))
                lp.prep["graph_" + res.status] = lp.prep.get("graph_" + res.status, 0) + 1
            build_index(cfg)
            for r in todo:
                report = Path(r.path) / "graphify-out" / "GRAPH_REPORT.md"
                r.graph_report = str(report) if report.is_file() else ""
    if lp.use_hints:
        for r in opened:
            if r.graph_report:
                h = find_hints(lp.prompt, r, cfg)
                if h:
                    lp.hints[r.name] = h


def launch(cfg: Config, lp: LaunchPlan, dry_run: bool) -> int:
    try:
        prepare(cfg, lp, dry_run)
        cwd, cmd, session = materialize(cfg, lp, dry_run)
    except LaunchError as exc:
        return _err(str(exc))
    where = f"primary repo {lp.opened[0].name}" if lp.primary else f"workspace {session}"
    names = ", ".join(r.name for r in lp.opened) or "nothing (fully blind)"
    rel = f"; related, closed: {', '.join(r.repo.name for r in lp.related)}" if lp.related else ""
    notes = ""
    if lp.mcp_plan is not None:
        kept = ", ".join(s.name for s in lp.mcp_plan.kept) or "none"
        notes += f"; MCP kept: {kept}" + (f" (dropped {len(lp.mcp_plan.dropped)})" if lp.mcp_plan.dropped else "")
    if lp.skills_plan is not None:
        notes += f"; skills kept: {len(lp.skills_plan.kept)} (hidden {len(lp.skills_plan.dropped)})"
    if lp.hints:
        notes += f"; starting points: {sum(len(h.files) for h in lp.hints.values())} file(s)"
    for note in (lp.mcp_note, lp.skills_note):
        if note:
            notes += f"; {note}"
    print(f"blind: opened {names}{rel}; {len(lp.closed)} closed{notes}; {where}", file=sys.stderr)
    if dry_run:
        print(f"cd {shlex.quote(cwd)} && {shlex.join(cmd)}")
        return 0
    if shutil.which(cmd[0]) is None:
        return _err(f"'{cmd[0]}' not found in PATH")
    _record(cfg, lp, cwd)
    os.chdir(cwd)
    os.execvp(cmd[0], cmd)
    return 0  # unreachable


def _record(cfg: Config, lp: LaunchPlan, cwd: str) -> None:
    """Anonymous numbers for `blind stats`; never blocks a launch."""
    try:
        hidden = {s.name for s in lp.skills_plan.dropped} if lp.skills_plan else set()
        mcp = lp.mcp_plan
        rec_id = statsmod.record_launch(
            cli=lp.adapter.name, mode="blind", cwd=cwd, prompt=lp.prompt,
            repos_total=len(lp.repos), repos_opened=len(lp.opened),
            mcp_total=(len(mcp.kept) + len(mcp.dropped)) if mcp else None, mcp_kept=len(mcp.kept) if mcp else None,
            estimate=statsmod.estimate_startup(cwd, _family(lp.adapter), hidden, cfg),
            hints_files=sum(len(h.files) for h in lp.hints.values()), sync=lp.prep,
        )
        _say(f"blind: session {rec_id} (compare with `blind stats`)")
    except Exception as exc:  # noqa: BLE001 - statistics must never stop a session
        _say(f"blind: stats not recorded ({exc})")


def launch_plain(cfg: Config, adapter: Adapter, prompt: str, extra: list[str], dry_run: bool) -> int:
    """The CLI exactly as it would start here, for comparison with a blind session."""
    cmd = build_command(adapter, prompt, [], extra)
    cwd = os.getcwd()
    print(f"blind: plain {adapter.name} in the current directory (nothing filtered)", file=sys.stderr)
    if dry_run:
        print(shlex.join(cmd))
        return 0
    if shutil.which(cmd[0]) is None:
        return _err(f"'{cmd[0]}' not found in PATH")
    try:
        rec_id = statsmod.record_launch(
            cli=adapter.name, mode="plain", cwd=cwd, prompt=prompt, repos_total=0, repos_opened=0,
            mcp_total=None, mcp_kept=None, estimate=statsmod.estimate_startup(cwd, _family(adapter), set(), cfg))
        _say(f"blind: session {rec_id} (compare with `blind stats`)")
    except Exception as exc:  # noqa: BLE001
        _say(f"blind: stats not recorded ({exc})")
    os.execvp(cmd[0], cmd)
    return 0  # unreachable


def cmd_run(args, cfg: Config, extra: list[str]) -> int:
    try:
        adapter = get_adapter(args.cli, cfg)
    except (KeyError, ValueError) as exc:
        return _err(str(exc))
    if args.max is not None:
        cfg.max_repos = args.max
    repos = load_index(cfg)
    if not repos:
        return _err("no repos indexed: run `blind setup` (or `blind init <dir>`) first")
    prompt = " ".join(args.prompt).strip()
    try:
        lp = make_plan(cfg, adapter, prompt, repos, args, extra)
    except LaunchError as exc:
        return _err(str(exc))
    return launch(cfg, lp, args.dry_run)


# --- interactive entry point -------------------------------------------------------------------

ROOT_CANDIDATES = ("~/work", "~/projects", "~/dev", "~/code", "~/src", "~/repos", "~/git", "~/github",
                   "~/Documents/GitHub", "~/Documents/git")


def _ask(question: str) -> str:
    """One line from the user. Separate function so tests can replace it."""
    return input(question)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _say(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def _write_roots(roots: list[str]) -> Path:
    cfg_file = config_dir() / "config.toml"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    line = "roots = [" + ", ".join(json.dumps(r) for r in roots) + "]\n"
    old = cfg_file.read_text(encoding="utf-8") if cfg_file.exists() else ""
    kept = [ln for ln in old.splitlines(keepends=True) if not ln.startswith("roots")]
    # a top-level key must come before any [table]
    cfg_file.write_text(line + "".join(kept), encoding="utf-8")
    return cfg_file


def _offer_graphs(cfg: Config, repos: list[Repo]) -> None:
    missing = [r for r in repos if not r.graph_report]
    if not missing:
        return
    if shutil.which(cfg.graphify_bin) is None:
        _say(f"blind: {len(missing)} repo(s) have no Graphify code graph and '{cfg.graphify_bin}' is not installed "
             "(uv tool install graphifyy, then `blind graph --all`). Continuing without graphs.")
        return
    answer = _ask(f"Build Graphify code graphs for {len(missing)} repo(s) now? Local, no LLM, can take a while [Y/n] ").strip().lower()
    if answer in ("n", "no", "non"):
        _say("blind: skipped. Run `blind graph --all` whenever you want.")
        return
    results = build_graphs(missing, cfg, log=_say)
    for r in results:
        if r.status == "failed":
            _say(f"blind: {r.repo.name}: {r.detail}")
    build_index(cfg)


def setup(cfg: Config, force: bool = False) -> Config:
    """First-run wizard: where are your repos, index them, build graphs. Returns the new config."""
    if cfg.roots and not force:
        return cfg
    if not _interactive():
        raise LaunchError("no roots configured: run `blind init <dir> [<dir> ...]`")
    guesses = [str(Path(c).expanduser()) for c in ROOT_CANDIDATES if Path(c).expanduser().is_dir()]
    default = cfg.roots or guesses
    _say("blind: first setup. Which directories contain your git repos?")
    answer = _ask(f"  directories, comma-separated [{', '.join(default)}]: ").strip()
    roots = [str(Path(p.strip()).expanduser().resolve()) for p in answer.split(",") if p.strip()] if answer else list(default)
    if not roots:
        raise LaunchError("no directory given")
    missing = [r for r in roots if not Path(r).is_dir()]
    if missing:
        raise LaunchError(f"not a directory: {', '.join(missing)}")
    cfg_file = _write_roots(roots)
    cfg = load_config(cfg_file)
    t0 = time.perf_counter()
    repos = build_index(cfg)
    if not repos:
        raise LaunchError(f"no git repos found under {', '.join(roots)} (scan_depth={cfg.scan_depth})")
    _say(f"blind: indexed {len(repos)} repos in {(time.perf_counter() - t0) * 1000:.0f} ms ({cfg_file})")
    _offer_graphs(cfg, repos)
    return cfg


def _installed_clis(cfg: Config) -> list[str]:
    out = []
    for name in list(DEFAULTS) + [n for n in cfg.adapters if n not in DEFAULTS]:
        try:
            if shutil.which(get_adapter(name, cfg).binary):
                out.append(name)
        except (KeyError, ValueError):
            continue
    return out


def _choose_cli(cfg: Config) -> str:
    if cfg.default_cli:
        return cfg.default_cli
    found = _installed_clis(cfg)
    if len(found) == 1:
        return found[0]
    pool = found or list(DEFAULTS)
    _say("blind: which CLI? " + ", ".join(f"{i}) {n}" for i, n in enumerate(pool, 1)))
    answer = _ask("  [1] ").strip() or "1"
    if answer.isdigit() and 1 <= int(answer) <= len(pool):
        return pool[int(answer) - 1]
    if answer in pool:
        return answer
    raise LaunchError(f"unknown choice '{answer}'")


def _confirm(cfg: Config, lp: LaunchPlan, opts, extra: list[str]) -> LaunchPlan | None:
    """Show the plan; Enter launches, +repo / -repo adjust, q cancels."""
    while True:
        _say(describe_plan(lp))
        answer = _ask("  Enter = launch, +repo / -repo = adjust, q = cancel: ").strip()
        if answer == "":
            return lp
        if answer.lower() in ("q", "quit", "n", "no"):
            return None
        if answer[0] in "+-":
            wanted = [w.strip() for w in answer[1:].split(",") if w.strip()]
            found, missing = _resolve_forced(",".join(wanted), lp.repos)
            if missing:
                _say(f"  unknown repo(s): {', '.join(missing)}")
                continue
            current = list(lp.opened)
            current = current + [r for r in found if r not in current] if answer[0] == "+" else [r for r in current if r not in found]
            lp = make_plan(cfg, lp.adapter, lp.prompt, lp.repos, opts, extra, forced=current)
            continue
        _say("  ?")


def ui_available() -> bool:
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    return True


def ui_plan(cfg: Config, repos: list[Repo], cli: str, prompt: str):
    """Automatic choice for the full-screen launcher: every repo / MCP server / skill, ticked or not."""
    from .ui import Item, UiPlan
    adapter = get_adapter(cli, cfg)
    seed = plan(prompt, repos, cfg) if prompt else None
    opened = {c.repo.name: c.reason for c in seed.opened} if seed else {}
    related = {r.repo.name: r.why for r in seed.related} if seed else {}
    rest = sorted((r for r in repos if r.name not in opened and r.name not in related), key=lambda r: r.name.lower())
    items = [Item(n, True, why) for n, why in opened.items()]
    items += [Item(n, False, "related") for n in related]
    items += [Item(r.name, False, "") for r in rest]
    out = UiPlan(repos=items)
    out.info += [f"related, closed: {n}: {why}" for n, why in list(related.items())[:3]]

    if adapter.mcp_style != "none":
        servers = discover(adapter.mcp_style, Path.home(), cfg)
        mplan = select_mcp(prompt, servers, cfg, "auto")
        kept = {s.name for s in mplan.kept}
        out.mcp = [Item(s.name, s.name in kept, mplan.reasons.get(s.name, ""), locked=s.name in cfg.mcp_always) for s in servers]
    else:
        out.info.append(f"{adapter.name}: no MCP filter available here")
    if adapter.skills_style != "none":
        installed = skillmod.discover(adapter.skills_style.split("-")[0], Path.home(), cfg)
        splan = skillmod.select_skills(prompt, installed, cfg, "auto")
        kept = {s.name for s in splan.kept}
        out.skills = [Item(s.name, s.name in kept, splan.reasons.get(s.name, ""), locked=s.name in cfg.skills_always) for s in installed]
    else:
        out.info.append(f"{adapter.name}: no skills filter available here")
    return out


def ui_hints(cfg: Config, repos: list[Repo], prompt: str, names: list[str]) -> list[str]:
    by_name = {r.name: r for r in repos}
    lines: list[str] = []
    if not prompt or not cfg.hints_enabled:
        return lines
    for n in names:
        r = by_name.get(n)
        if r and r.graph_report:
            h = find_hints(prompt, r, cfg)
            if h:
                lines.append(f"{n}: starting points")
                lines += [f"  {f.path}" + (f"  ({', '.join(label for label, _ in f.symbols)})" if f.symbols else "") for f in h.files]
    return lines[:10]


def _spec_from_ui(selected: list[str], universe: list[str], when_all: str | None) -> str | None:
    if set(selected) >= set(universe):
        return when_all
    return ",".join(selected) if selected else "none"


def run_launcher(args, cfg: Config, repos: list[Repo], cli: str | None, extra: list[str]):
    """Full-screen choice, then the same launch path as everything else. Returns a LaunchPlan or None (cancelled)."""
    from .ui import run_ui
    installed = _installed_clis(cfg)
    clis = installed or sorted(DEFAULTS)
    start = cli or (cfg.default_cli if cfg.default_cli in clis else clis[0])
    states = [graph_state(r.graph_report, r.path) for r in repos]
    behind = sum(s in ("none", "stale") for s in states)
    status = (f"{len(repos)} repos · graphs: {len(repos) - behind} current, {behind} missing or stale"
              f" · sync: {'off' if args.no_sync else (args.sync or cfg.sync_on_launch)}")
    res = run_ui(clis, start, " ".join(args.prompt).strip(),
                 plan_fn=lambda c, p: ui_plan(cfg, repos, c, p),
                 hints_fn=lambda c, p, names: ui_hints(cfg, repos, p, names),
                 status=status, total_repos=len(repos))
    if res is None:
        return None
    adapter = get_adapter(res.cli, cfg)
    chosen = set(res.repos)
    forced = [r for r in repos if r.name in chosen]
    args.mcp = args.skills = None
    if adapter.mcp_style != "none":
        names = [s.name for s in discover(adapter.mcp_style, Path.home(), cfg)]
        # Claude only filters when something is unticked: its strict mode also drops plugins and connectors.
        args.mcp = _spec_from_ui(res.mcp, names, "all" if adapter.mcp_auto else None) if names else None
    if adapter.skills_style != "none":
        names = [s.name for s in skillmod.discover(adapter.skills_style.split("-")[0], Path.home(), cfg)]
        args.skills = _spec_from_ui(res.skills, names, "all") if names else None
    return make_plan(cfg, adapter, res.prompt, repos, args, extra, forced=forced)


def cmd_start(args, cfg: Config, extra: list[str], cli: str | None) -> int:
    """``blind`` or ``blind <cli> [prompt]``: set up if needed, plan, show, confirm, launch."""
    try:
        if args.plain:
            adapter = get_adapter(cli or _choose_cli(cfg), cfg)
            prompt = " ".join(args.prompt).strip()
            if not prompt and _interactive() and not args.dry_run:
                prompt = _ask(f"{adapter.name} prompt (empty = none): ").strip()
            return launch_plain(cfg, adapter, prompt, extra, args.dry_run)
        cfg = setup(cfg)
        if args.max is not None:
            cfg.max_repos = args.max
        repos = load_index(cfg)
        if not repos:
            repos = build_index(cfg)
        if not repos:
            return _err("no repos found under the configured roots")
        if not args.yes and shutil.which(cfg.graphify_bin) is not None:
            states = [graph_state(r.graph_report, r.path) for r in repos]
            behind = sum(st in ("none", "stale") for st in states)
            if behind:
                _say(f"blind: {behind} of {len(repos)} repos have a missing or stale code graph "
                     "(`blind sync` updates repos and graphs; opened repos are refreshed at launch)")
        if (_interactive() and ui_available() and not args.no_ui and not args.dry_run and not args.prompt
                and not args.confirm and not args.yes):
            lp = run_launcher(args, cfg, repos, cli, extra)
            if lp is None:
                _say("blind: cancelled")
                return 1
            return launch(cfg, lp, False)
        if _interactive() and not args.prompt and not args.no_ui and not args.dry_run and not ui_available():
            _say("blind: text mode, the full-screen launcher needs Textual in this Python "
                 f"({sys.executable}): `{Path(sys.executable).name} -m pip install textual` (or `pip install -e \".[ui]\"`)")
        adapter = get_adapter(cli or _choose_cli(cfg), cfg)
        prompt = " ".join(args.prompt).strip()
        typed = False
        if not prompt and _interactive():
            prompt = _ask(f"{adapter.name} prompt (empty = fully blind session): ").strip()
            typed = True
        lp = make_plan(cfg, adapter, prompt, repos, args, extra)
        if (typed or args.confirm) and not args.yes and not args.dry_run:
            lp = _confirm(cfg, lp, args, extra)
            if lp is None:
                _say("blind: cancelled")
                return 1
    except (LaunchError, KeyError, ValueError) as exc:
        return _err(str(exc))
    except (EOFError, KeyboardInterrupt):
        _say("")
        return 130
    return launch(cfg, lp, args.dry_run)


def cmd_setup(args, cfg: Config) -> int:
    try:
        setup(cfg, force=True)
    except LaunchError as exc:
        return _err(str(exc))
    except (EOFError, KeyboardInterrupt):
        _say("")
        return 130
    return 0


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


def cmd_sync(args, cfg: Config) -> int:
    repos = load_index(cfg)
    if args.repos:
        targets, missing = _resolve_forced(",".join(args.repos), repos)
        if missing:
            return _err(f"unknown repo(s): {', '.join(missing)}")
    else:
        targets = repos
    if not targets:
        return _err("no repos indexed: run `blind setup` first")
    switch = not args.safe
    _say(f"blind: syncing {len(targets)} repo(s) ({'checking out the root branch' if switch else 'safe mode, no branch switch'}) ...")
    names = {r.path: r.name for r in targets}
    results = sync_many([r.path for r in targets], cfg, switch=switch)
    counts: dict[str, int] = {}
    for res in results:
        counts[res.status] = counts.get(res.status, 0) + 1
        name = names.get(res.path, res.path)
        if res.status == "updated":
            print(f"  updated  {name}: {res.moved} commit(s) on {res.branch}")
        elif res.status != "current":
            print(f"  {res.status:<8} {name}: {res.detail}")
    print("sync: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    if args.no_graph:
        return 1 if counts.get("failed") else 0
    if shutil.which(cfg.graphify_bin) is None:
        _say(f"blind: '{cfg.graphify_bin}' not installed: graphs not refreshed (uv tool install graphifyy)")
        return 1 if counts.get("failed") else 0
    todo = [r for r in targets if graph_state(r.graph_report, r.path) in ("none", "stale")]
    if todo:
        _say(f"blind: refreshing {len(todo)} code graph(s) ...")
        out = build_graphs(todo, cfg, log=lambda *_: None)
        for res in out:
            if res.status == "failed":
                print(f"  graph failed {res.repo.name}: {res.detail}")
        print(f"graphs: {sum(r.status in ('built', 'updated') for r in out)} refreshed, {sum(r.status == 'failed' for r in out)} failed")
        build_index(cfg)
    else:
        print("graphs: all current")
    return 1 if counts.get("failed") else 0


def cmd_status(args, cfg: Config) -> int:
    repos = load_index(cfg)
    if not repos:
        return _err("no repos indexed: run `blind setup` first")
    from .gitsync import current_branch, is_dirty
    rows, tally = [], {"none": 0, "stale": 0, "fresh": 0, "unknown": 0}
    for r in repos:
        state = graph_state(r.graph_report, r.path)
        tally[state] += 1
        branch = current_branch(r.path) or "(detached)"
        try:
            dirty = is_dirty(r.path)
        except Exception:  # noqa: BLE001
            dirty = False
        if state in ("none", "stale") or dirty:
            rows.append((r.name, branch, "dirty" if dirty else "clean", state))
    for name, branch, dirty, state in rows[:40]:
        print(f"{name:<32} {branch:<20} {dirty:<6} graph {state}")
    if len(rows) > 40:
        print(f"... and {len(rows) - 40} more")
    print(f"{len(repos)} repos: graphs {tally['fresh']} fresh, {tally['stale']} stale, {tally['none']} missing, "
          f"{tally['unknown']} unknown. `blind sync` updates repos and graphs.")
    return 0


def cmd_stats(args, cfg: Config) -> int:
    if args.note:
        if args.note[0] != "note" or len(args.note) < 3:
            return _err("usage: blind stats note <id> key=value ...")
        values: dict[str, int] = {}
        for item in args.note[2:]:
            key, _, val = item.partition("=")
            if not val.lstrip("-").isdigit():
                return _err(f"not a number: {item}")
            values[key] = int(val)
        statsmod.add_note(args.note[1], values)
        print(f"noted {', '.join(values)} for {args.note[1]}")
        return 0
    rows = statsmod.build_report(args.last)
    print(json.dumps(rows, indent=2) if args.json else statsmod.format_report(rows))
    return 0


def cmd_doctor(args, cfg: Config) -> int:
    def row(label: str, value: str) -> None:
        print(f"{label:<22}{value}")

    row("blinders", f"{__version__}")
    row("python", f"{sys.version_info.major}.{sys.version_info.minor} ({sys.executable})")
    row("full-screen launcher", "Textual found" if ui_available() else "Textual missing: pip install textual")
    row("git", shutil.which("git") or "not found")
    row("graphify", shutil.which(cfg.graphify_bin) or f"not found (uv tool install graphifyy)")
    for name in sorted(_known_clis(cfg)):
        try:
            binary = get_adapter(name, cfg).binary
        except (KeyError, ValueError) as exc:
            row(name, f"bad adapter: {exc}")
            continue
        row(name, shutil.which(binary) or "not found")
    row("config", str(config_dir() / "config.toml") + ("" if (config_dir() / "config.toml").is_file() else " (missing)"))
    row("roots", ", ".join(cfg.roots) or "none")
    return 0


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


def _known_clis(cfg: Config) -> set[str]:
    return set(DEFAULTS) | set(cfg.adapters)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, extra = _split_passthrough(argv)
    cfg = load_config()
    # Options may sit between prompt words; plain parse_args rejects that on Python < 3.12.
    if argv[:1] == ["run"]:
        run_parser = argparse.ArgumentParser(prog="blind run")
        _add_run_args(run_parser)
        return cmd_run(run_parser.parse_intermixed_args(argv[1:]), cfg, extra)
    if not argv or argv[0] in _known_clis(cfg) or argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        start_parser = argparse.ArgumentParser(prog="blind [cli]")
        _add_launch_args(start_parser)
        cli = argv[0] if argv and argv[0] in _known_clis(cfg) else None
        args = start_parser.parse_intermixed_args(argv[1:] if cli else argv)
        if not argv and not _interactive():
            _parser().print_help(sys.stderr)
            return 2
        return cmd_start(args, cfg, extra, cli)
    args = _parser().parse_args(argv)
    handlers = {
        "setup": cmd_setup, "doctor": cmd_doctor, "sync": cmd_sync, "status": cmd_status, "stats": cmd_stats, "init": cmd_init, "list": cmd_list, "select": cmd_select, "mcp": cmd_mcp,
        "graph": cmd_graph, "audit": cmd_audit, "clean": cmd_clean,
    }
    return handlers[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
