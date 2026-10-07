import io
import json
import os
import stat
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from blinders.adapters import NO_MCP_SENTINEL, build_command, get_adapter
from blinders.cli import main
from blinders.config import load_config
from blinders.graph import build_graphs, graph_command
from blinders.mcp import discover, select_mcp
from blinders.scan import build_index, load_index
from blinders.workspace import render_index

from helpers import Sandbox, session_of


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


GEMINI_SETTINGS = {
    "mcpServers": {
        "bigquery": {"command": "npx", "args": ["-y", "@example/mcp-server-bigquery"]},
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
        "jira": {"url": "https://mcp.example.com/jira/sse", "description": "Atlassian Jira tickets"},
    }
}


class McpTests(Sandbox):
    def setUp(self):
        super().setUp()
        (self.home / ".gemini").mkdir()
        (self.home / ".gemini" / "settings.json").write_text(json.dumps(GEMINI_SETTINGS))
        (self.home / ".claude.json").write_text(json.dumps(GEMINI_SETTINGS))
        self.sales_repos()
        cfg_dir = Path(os.environ["BLINDERS_CONFIG_DIR"])
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(f'roots = ["{self.work}"]\n')

    def servers(self, style="allowlist"):
        return discover(style, self.home, self.cfg)

    def names(self, plan):
        return sorted(s.name for s in plan.kept)

    def test_discover_gemini_and_claude(self):
        self.assertEqual([s.name for s in self.servers("allowlist")], ["bigquery", "github", "jira"])
        self.assertEqual([s.name for s in self.servers("config")], ["bigquery", "github", "jira"])
        self.assertEqual(self.servers("none"), [])

    def test_auto_matches_prompt_by_name_args_and_description(self):
        s = self.servers()
        self.assertEqual(self.names(select_mcp("fix the bigquery table loading", s, self.cfg)), ["bigquery"])
        self.assertEqual(self.names(select_mcp("open a pull request on github", s, self.cfg)), ["github"])
        self.assertEqual(self.names(select_mcp("update the jira tickets", s, self.cfg)), ["jira"])
        self.assertEqual(self.names(select_mcp("rename this variable", s, self.cfg)), [])

    def test_launch_noise_words_do_not_match(self):
        self.assertEqual(self.names(select_mcp("npx run the server with the latest args", self.servers(), self.cfg)), [])

    def test_always_and_keywords_and_specs(self):
        self.cfg.mcp_always = ["github"]
        self.cfg.mcp_keywords = {"bigquery": ["lineage"]}
        s = self.servers()
        self.assertEqual(self.names(select_mcp("lineage de la table", s, self.cfg)), ["bigquery", "github"])
        self.assertEqual(self.names(select_mcp("x", s, self.cfg, "none")), ["github"])
        self.assertEqual(self.names(select_mcp("x", s, self.cfg, "all")), ["bigquery", "github", "jira"])
        self.assertEqual(self.names(select_mcp("x", s, self.cfg, "jira")), ["github", "jira"])

    # --- adapters ---
    def test_gemini_allowlist_flags_and_sentinel(self):
        g = get_adapter("gemini", self.cfg)
        cmd = build_command(g, "hi", ["/a"], [], mcp_names=["bigquery", "github"])
        self.assertEqual(cmd, ["gemini", "--include-directories", "/a",
                               "--allowed-mcp-server-names", "bigquery", "--allowed-mcp-server-names", "github", "-i", "hi"])
        self.assertIn(NO_MCP_SENTINEL, build_command(g, "hi", [], [], mcp_names=[]))
        self.assertNotIn("--allowed-mcp-server-names", build_command(g, "hi", [], [], mcp_names=None))

    def test_claude_strict_config_flags(self):
        c = get_adapter("claude", self.cfg)
        cmd = build_command(c, "hi", ["/a"], [], mcp_file="/s/mcp.json")
        self.assertEqual(cmd, ["claude", "hi", "--add-dir", "/a", "--strict-mcp-config", "--mcp-config", "/s/mcp.json"])

    # --- end to end ---
    def test_run_gemini_filters_mcp_by_prompt_and_index_lists_dropped(self):
        code, out, err = run_cli("run", "gemini", "--dry-run", "fix", "the", "bigquery", "load", "in", "warehouse-etl")
        self.assertEqual(code, 0)
        self.assertIn("--allowed-mcp-server-names bigquery", out)
        self.assertNotIn("github", out)
        self.assertIn("MCP kept: bigquery (dropped 2)", err)
        session = session_of(out)
        text = (session / "GEMINI.md").read_text()
        self.assertIn("## MCP servers not loaded", text)
        self.assertIn("github, jira", text)

    def test_run_gemini_mcp_all_and_none_and_unknown(self):
        _, out, _ = run_cli("run", "gemini", "--dry-run", "--mcp", "all", "-r", "sales-api")
        self.assertEqual(out.count("--allowed-mcp-server-names"), 3)
        _, out, _ = run_cli("run", "gemini", "--dry-run", "--mcp", "none", "-r", "sales-api")
        self.assertIn(NO_MCP_SENTINEL, out)
        code, _, err = run_cli("run", "gemini", "--dry-run", "--mcp", "nope", "-r", "sales-api")
        self.assertEqual(code, 2)
        self.assertIn("unknown MCP server", err)

    def test_run_claude_leaves_mcp_alone_unless_asked(self):
        _, out, _ = run_cli("run", "claude", "--dry-run", "-r", "sales-api")
        self.assertNotIn("mcp", out)
        _, out, _ = run_cli("run", "claude", "--dry-run", "--mcp", "github", "-r", "sales-api")
        self.assertIn("--strict-mcp-config --mcp-config", out)
        session = session_of(out)
        conf = json.loads((session / "mcp.json").read_text())
        self.assertEqual(list(conf["mcpServers"]), ["github"])
        self.assertEqual(stat.S_IMODE((session / "mcp.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(session.stat().st_mode), 0o700)

    def test_mcp_flag_on_cli_without_mcp_support_is_noted(self):
        code, _, err = run_cli("run", "codex", "--dry-run", "--mcp", "github", "-r", "sales-api")
        self.assertEqual(code, 0)
        self.assertIn("--mcp ignored", err)

    def test_blind_mcp_command(self):
        code, out, _ = run_cli("mcp", "--cli", "gemini", "update", "the", "jira", "board")
        self.assertEqual(code, 0)
        self.assertIn("keep  jira", out)
        self.assertIn("drop  github", out)

    def test_run_related_in_dry_run(self):
        _, out, err = run_cli("run", "gemini", "--dry-run", "--mcp", "none", "comment", "sales-api-java", "est", "déployé", "sur", "kubernetes")
        self.assertIn("platform-k8s", out)
        self.assertIn("opened sales-api-java, platform-k8s", err)
        _, out, err = run_cli("run", "gemini", "--dry-run", "--mcp", "none", "lineage", "de", "sales-api-java", "--related", "none")
        self.assertNotIn("platform-k8s", out)


class GraphTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.sales_repos()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls.log"
        script = (
            "#!/bin/sh\n"
            f'echo "$@" >> {self.calls}\n'
            '[ "$1" = "extract" ] && [ "$2" = "FAIL" ] && exit 3\n'
            'target="$2"; [ "$1" = "update" ] && target="$2"\n'
            'mkdir -p "$target/graphify-out" && echo "# report" > "$target/graphify-out/GRAPH_REPORT.md"\n'
        )
        (self.bin / "graphify").write_text(script)
        (self.bin / "graphify").chmod(0o755)
        os.environ["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        cfg_dir = Path(os.environ["BLINDERS_CONFIG_DIR"])
        cfg_dir.mkdir()
        (cfg_dir / "config.toml").write_text(f'roots = ["{self.work}"]\n')
        self.repos = build_index(self.cfg)
        self.by = {r.name: r for r in self.repos}

    def test_commands_match_graphify_cli(self):
        r = self.by["sales-api"]
        self.assertEqual(graph_command(self.cfg, r, update=False),
                         ["graphify", "extract", r.path, "--code-only", "--global", "--as", "sales-api"])
        self.assertEqual(graph_command(self.cfg, r, update=True), ["graphify", "update", r.path])

    def test_build_then_skip_then_update(self):
        code, out, _ = run_cli("graph", "sales-api")
        self.assertEqual(code, 0)
        self.assertIn("1 graph(s) built", out)
        self.assertTrue((self.work / "sales-api" / "graphify-out" / "GRAPH_REPORT.md").is_file())
        # the index was refreshed and now knows the report
        reports = {r.name: r.graph_report for r in load_index(self.cfg)}
        self.assertTrue(reports["sales-api"].endswith("GRAPH_REPORT.md"))
        self.assertEqual(reports["sales-api-java"], "")
        _, out, _ = run_cli("graph", "sales-api")
        self.assertIn("1 skipped", out)
        run_cli("graph", "sales-api", "--update")
        lines = self.calls.read_text().splitlines()
        self.assertTrue(lines[0].startswith("extract "))
        self.assertTrue(lines[1].startswith("cluster-only ") and "--no-label" in lines[1])
        self.assertTrue(lines[-1].startswith("update "))
        self.assertEqual(len(lines), 3)

    def test_dry_run_runs_nothing(self):
        code, out, _ = run_cli("graph", "--all", "--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(self.calls.exists())
        self.assertIn("--code-only --global --as", out)

    def test_failure_is_reported(self):
        self.cfg.graphify_bin = str(self.bin / "graphify")
        bad = self.by["warehouse-etl"]
        bad.path = "FAIL"
        results = build_graphs([bad], self.cfg, log=lambda *_: None)
        self.assertEqual(results[0].status, "failed")

    def test_missing_graphify_and_missing_target(self):
        os.environ["PATH"] = "/nonexistent"
        code, _, err = run_cli("graph", "sales-api")
        self.assertEqual(code, 2)
        self.assertIn("uv tool install graphifyy", err)
        code, _, err = run_cli("graph")
        self.assertEqual(code, 2)

    def test_graph_report_shows_in_index_text(self):
        run_cli("graph", "sales-api-java")
        repos = {r.name: r for r in load_index(self.cfg, refresh=True)}
        text = render_index([repos["sales-api-java"]], [], self.cfg)
        self.assertIn("GRAPH_REPORT.md", text)


if __name__ == "__main__":
    unittest.main()
