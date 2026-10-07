import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from blinders.adapters import build_command, get_adapter
from blinders.audit import audit
from blinders.cli import main
from blinders.config import Config
from blinders.scan import build_index
from blinders.workspace import create_session, prune_sessions, render_index, sessions_dir

from helpers import Sandbox, make_repo


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class AdapterTests(Sandbox):
    def test_claude_prompt_precedes_variadic_flag(self):
        cmd = build_command(get_adapter("claude", self.cfg), "fix it", ["/a", "/b"], [])
        self.assertEqual(cmd, ["claude", "fix it", "--add-dir", "/a", "--add-dir", "/b"])

    def test_gemini_comma_and_initial_prompt_flag(self):
        cmd = build_command(get_adapter("gemini", self.cfg), "fix it", ["/a", "/b"], ["--yolo"])
        self.assertEqual(cmd, ["gemini", "--include-directories", "/a,/b", "-i", "fix it", "--yolo"])

    def test_vibe_has_no_dir_flag(self):
        cmd = build_command(get_adapter("vibe", self.cfg), "", ["/a"], [])
        self.assertEqual(cmd, ["vibe"])

    def test_config_override_and_custom_cli(self):
        cfg = Config(adapters={"claude": {"binary": "claude-x"}, "foo": {"dir_style": "repeat", "dir_flag": "-d"}})
        self.assertEqual(get_adapter("claude", cfg).binary, "claude-x")
        self.assertEqual(build_command(get_adapter("foo", cfg), "", ["/a"], []), ["foo", "-d", "/a"])

    def test_unknown_cli(self):
        with self.assertRaises(KeyError):
            get_adapter("nope", self.cfg)


class WorkspaceTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.standard_repos()
        self.repos = build_index(self.cfg)

    def test_workspace_has_only_index_files_no_source(self):
        opened = [r for r in self.repos if r.name == "coolpot"]
        closed = [r for r in self.repos if r.name != "coolpot"]
        session = create_session(opened, closed, self.cfg)
        self.assertEqual(sorted(p.name for p in session.iterdir()), ["AGENTS.md", "CLAUDE.md", "GEMINI.md"])
        text = (session / "AGENTS.md").read_text()
        self.assertIn("## Opened repos", text)
        self.assertIn("coolpot", text)
        self.assertIn("/add-dir", text)
        self.assertIn("/directory add", text)

    def test_index_stays_small_with_many_repos(self):
        for i in range(200):
            make_repo(self.work, f"svc-{i}", "x" * 300)
        repos = build_index(self.cfg)
        text = render_index([], repos, self.cfg)
        self.assertIn("more (run `blind list`)", text)
        self.assertLess(len(text) // 4, 3000)  # ~tokens

    def test_link_mode_symlinks_and_prune_does_not_touch_targets(self):
        opened = [r for r in self.repos if r.name == "LoopDex"]
        session = create_session(opened, [], self.cfg, link=True)
        link = session / "LoopDex"
        self.assertTrue(link.is_symlink())
        self.assertEqual(prune_sessions(self.cfg, everything=True), 1)
        self.assertFalse(session.exists())
        self.assertTrue((self.work / "LoopDex" / "README.md").is_file())

    def test_old_sessions_are_pruned(self):
        import os
        session = create_session([], self.repos, self.cfg)
        os.utime(session, (1, 1))
        self.assertEqual(prune_sessions(self.cfg), 1)


class AuditTests(Sandbox):
    def test_counts_context_skills_and_mcp(self):
        project = self.work / "proj"
        (project / "sub").mkdir(parents=True)
        (project / "GEMINI.md").write_text("a" * 400)
        (project / "sub" / "CLAUDE.md").write_text("b" * 800)
        (self.home / ".gemini").mkdir()
        (self.home / ".gemini" / "GEMINI.md").write_text("c" * 40)
        (self.home / ".gemini" / "settings.json").write_text(json.dumps({"mcpServers": {"a": {}, "b": {}}}))
        (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"c": {}}}))
        skill = project / ".claude" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: demo\ndescription: " + "d" * 80 + "\n---\nbody " * 100)
        report = audit(project, home=self.home)
        self.assertEqual(report["mcp_server_count"], 3)
        self.assertEqual(report["skill_count"], 1)
        self.assertEqual(report["tokens"]["context_startup"], 100 + 10)
        self.assertEqual(report["tokens"]["context_nested"], 200)
        self.assertGreater(report["tokens"]["skills"], 0)

    def test_blind_workspace_is_much_lighter_than_wide_root(self):
        for i in range(10):
            repo = make_repo(self.work, f"r{i}", "readme")
            (repo / "CLAUDE.md").write_text("z" * 4000)
        wide = audit(self.work, home=self.home)
        self.cfg.roots = [str(self.work)]
        repos = build_index(self.cfg)
        session = create_session([], repos, self.cfg)
        blind = audit(session, home=self.home)
        wide_total = wide["tokens"]["context_startup"] + wide["tokens"]["context_nested"]
        self.assertGreater(wide_total, 9000)
        self.assertLess(blind["tokens"]["context_startup"] * 3, wide_total)


class CliTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.standard_repos()
        cfg_dir = Path(self.tmp / "config")
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(f'roots = ["{self.work}"]\n')

    def test_init_and_list(self):
        code, out, _ = run_cli("init")
        self.assertEqual(code, 0)
        self.assertIn("indexed 4 repos", out)
        _, out, _ = run_cli("list")
        self.assertIn("LoopDex", out)

    def test_select_json(self):
        code, out, _ = run_cli("select", "--json", "thermal", "simulation", "coolpot")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["name"], "coolpot")

    def test_run_dry_run_gemini_opens_selected_repo_only(self):
        code, out, err = run_cli("run", "gemini", "--dry-run", "fix", "the", "airflow", "dag", "bigquery")
        self.assertEqual(code, 0)
        self.assertIn("--include-directories", out)
        self.assertIn("carrefour-pipelines", out)
        self.assertNotIn("coolpot", out)
        self.assertIn("opened carrefour-pipelines", err)

    def test_run_without_prompt_is_fully_blind(self):
        code, out, err = run_cli("run", "claude", "--dry-run")
        self.assertEqual(code, 0)
        self.assertNotIn("--add-dir", out)
        self.assertIn("fully blind", err)

    def test_run_forced_repos_and_unknown_repo(self):
        code, out, _ = run_cli("run", "claude", "--dry-run", "-r", "coolpot,jira-cli")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("--add-dir"), 2)
        code, _, err = run_cli("run", "claude", "--dry-run", "-r", "nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown repo", err)

    def test_run_primary_starts_in_repo(self):
        code, out, _ = run_cli("run", "claude", "--dry-run", "--primary", "-r", "coolpot,jira-cli")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(f"cd {self.work}/coolpot"))
        self.assertEqual(out.count("--add-dir"), 1)

    def test_run_vibe_links_repos(self):
        code, out, _ = run_cli("run", "vibe", "--dry-run", "-r", "LoopDex")
        self.assertEqual(code, 0)
        session = Path(out.split("&&")[0].replace("cd", "").strip())
        self.assertTrue((session / "LoopDex").is_symlink())

    def test_passthrough_args(self):
        code, out, _ = run_cli("run", "gemini", "--dry-run", "-r", "coolpot", "--", "--yolo")
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().endswith("--yolo"))

    def test_audit_and_clean(self):
        code, out, _ = run_cli("audit", str(self.work))
        self.assertEqual(code, 0)
        self.assertIn("estimated startup total", out)
        run_cli("run", "claude", "--dry-run")
        self.assertTrue(any(sessions_dir().iterdir()))
        _, out, _ = run_cli("clean")
        self.assertIn("removed", out)


if __name__ == "__main__":
    unittest.main()
