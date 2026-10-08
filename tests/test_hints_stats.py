import io
import json
import os
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from blinders import cli, stats
from blinders.hints import find_hints, render_hints
from blinders.scan import build_index, load_index

from helpers import Sandbox, session_of


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def node(nid, label, src, loc="L1", kind="code"):
    return {"id": nid, "label": label, "source_file": src, "source_location": loc, "file_type": kind, "community": 0}


# Shape of Graphify 0.9 graph.json (nodes with label/source_file/source_location/file_type, links).
GRAPH = {
    "directed": False, "multigraph": False, "graph": {"schema_version": 1},
    "built_at_commit": "abc",
    "nodes": [
        node("pricing_rule", "PricingRule", "src/pricing/rule.py", "L12"),
        node("pricing_apply", "apply_discount()", "src/pricing/rule.py", "L40"),
        node("pricing_doc", "Computes the discount applied to a basket", "src/pricing/rule.py", "L1", "rationale"),
        node("basket", "Basket", "src/basket.py", "L5"),
        node("checkout", "checkout()", "src/checkout.py", "L9"),
        node("test_rule", "test_apply_discount()", "tests/test_rule.py", "L3"),
        node("readme", "Discount section", "README.md", "L1", "document"),
        node("ext", "json", "", "", "code"),
        *[node(f"util{i}", f"helper_{i}()", "src/util.py", f"L{i}") for i in range(30)],
    ],
    "links": [
        {"source": "pricing_apply", "target": "basket", "relation": "calls"},
        {"source": "checkout", "target": "pricing_rule", "relation": "calls"},
    ],
}


class HintTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.repo_dir = self.work / "shop"
        (self.repo_dir / ".git").mkdir(parents=True)
        (self.repo_dir / "README.md").write_text("Shop backend.")
        out = self.repo_dir / "graphify-out"
        out.mkdir()
        (out / "graph.json").write_text(json.dumps(GRAPH))
        (out / "GRAPH_REPORT.md").write_text("# report\n")
        self.repo = {r.name: r for r in build_index(self.cfg)}["shop"]

    def test_finds_the_file_and_symbols_for_a_prompt(self):
        h = find_hints("why does apply_discount compute the wrong PricingRule", self.repo, self.cfg)
        self.assertEqual(h.files[0].path, "src/pricing/rule.py")
        labels = [label for label, _ in h.files[0].symbols]
        self.assertIn("PricingRule", labels)
        self.assertIn("apply_discount", labels)

    def test_docstrings_help_find_files_but_are_not_shown_as_symbols(self):
        h = find_hints("where is the basket discount computed", self.repo, self.cfg)
        shown = [label for f in h.files for label, _ in f.symbols]
        self.assertNotIn("Computes the discount applied to a basket", shown)
        self.assertEqual(h.files[0].path, "src/pricing/rule.py")

    def test_documents_and_external_nodes_are_ignored(self):
        h = find_hints("discount json", self.repo, self.cfg)
        self.assertNotIn("README.md", [f.path for f in h.files])

    def test_connected_files_come_from_graph_links(self):
        h = find_hints("fix PricingRule apply_discount", self.repo, self.cfg)
        self.assertIn("src/checkout.py", h.connected + [f.path for f in h.files])

    def test_nothing_relevant_gives_no_hints(self):
        self.assertIsNone(find_hints("translate the onboarding emails", self.repo, self.cfg))
        self.assertIsNone(find_hints("", self.repo, self.cfg))

    def test_repo_name_alone_is_not_a_hint(self):
        self.assertIsNone(find_hints("shop", self.repo, self.cfg))

    def test_max_files_and_rendering(self):
        self.cfg.hints_max_files = 1
        h = find_hints("PricingRule apply_discount basket checkout", self.repo, self.cfg)
        self.assertEqual(len(h.files), 1)
        text = "\n".join(render_hints(h, ""))
        self.assertIn("hints, not a verdict", text)
        self.assertIn("src/pricing/rule.py", text)

    def test_missing_or_broken_graph_is_not_an_error(self):
        (self.repo_dir / "graphify-out" / "graph.json").write_text("{not json")
        self.assertIsNone(find_hints("PricingRule", self.repo, self.cfg))

    def test_hints_reach_the_index_in_a_dry_run(self):
        code, out, err = run_cli("run", "gemini", "--dry-run", "--mcp", "none", "fix", "apply_discount", "in", "shop")
        self.assertEqual(code, 0)
        self.assertIn("starting points: ", err)
        text = (session_of(out) / "GEMINI.md").read_text()
        self.assertIn("Starting points from the code graph", text)
        self.assertIn("src/pricing/rule.py", text)
        code, out, err = run_cli("run", "gemini", "--dry-run", "--mcp", "none", "--no-hints", "fix", "apply_discount", "in", "shop")
        self.assertNotIn("Starting points", (session_of(out) / "GEMINI.md").read_text())


class FileNameHintTests(Sandbox):
    """Level 2 without a code graph (SQL, YAML, notebooks, anything the graph tool cannot parse)."""

    def setUp(self):
        super().setUp()
        self.repo_dir = self.work / "bi-dbt"
        (self.repo_dir / ".git").mkdir(parents=True)
        for rel in ("models/marts/sales/fct_shipments.sql", "models/staging/stg_orders.sql", "macros/util.sql"):
            f = self.repo_dir / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("select 1")
        (self.repo_dir / "models" / "schema.yml").write_text("models:\n  - name: dim_customers\n")
        self.repo = {r.name: r for r in build_index(self.cfg)}["bi-dbt"]

    def test_a_file_name_is_a_starting_point(self):
        h = find_hints("why is fct_shipments wrong", self.repo, self.cfg)
        self.assertEqual(h.files[0].path, "models/marts/sales/fct_shipments.sql")
        self.assertEqual(h.files[0].symbols, [])

    def test_a_name_declared_inside_a_yaml_file_points_to_it(self):
        h = find_hints("the customers dimension looks wrong", self.repo, self.cfg)
        self.assertEqual(h.files[0].path, "models/schema.yml")

    def test_plural_and_singular_meet(self):
        h = find_hints("which order tables feed this", self.repo, self.cfg)
        self.assertEqual(h.files[0].path, "models/staging/stg_orders.sql")

    def test_nothing_relevant_or_only_the_repo_name_gives_nothing(self):
        self.assertIsNone(find_hints("translate the onboarding emails", self.repo, self.cfg))
        self.assertIsNone(find_hints("bi dbt", self.repo, self.cfg))

    def test_the_label_says_where_the_hint_comes_from(self):
        h = find_hints("why is fct_shipments wrong", self.repo, self.cfg)
        self.assertIn("file names", "\n".join(render_hints(h, "")))
        self.assertNotIn("code graph", "\n".join(render_hints(h, "")))

    def test_hints_reach_the_index_without_any_graph(self):
        code, out, err = run_cli("run", "gemini", "--dry-run", "--mcp", "none", "why", "is", "fct_shipments", "wrong", "in", "bi-dbt")
        self.assertEqual(code, 0)
        text = (session_of(out) / "GEMINI.md").read_text()
        self.assertIn("models/marts/sales/fct_shipments.sql", text)


class StatsTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.sales_repos()
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\ndefault_cli = "claude"\n')
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for n in ("claude", "gemini"):
            (self.bin / n).write_text("#!/bin/sh\n")
            (self.bin / n).chmod(0o755)
        os.environ["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        p = [mock.patch.object(cli.os, "execvp"), mock.patch.object(cli.os, "chdir")]
        self.execvp = p[0].start()
        p[1].start()
        self.addCleanup(lambda: [x.stop() for x in p])

    def claude_transcript(self, cwd, entries):
        folder = self.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
        folder.mkdir(parents=True)
        lines = []
        for i, (mid, inp, cc, cr, out) in enumerate(entries):
            lines.append(json.dumps({
                "type": "assistant", "timestamp": "2099-01-01T00:00:00.000Z", "cwd": cwd,
                "message": {"id": mid, "content": [{"type": "text", "text": "SECRET reply"}],
                            "usage": {"input_tokens": inp, "cache_creation_input_tokens": cc,
                                      "cache_read_input_tokens": cr, "output_tokens": out}}}))
        lines.insert(0, json.dumps({"type": "user", "message": {"content": "SECRET prompt"}}))
        (folder / "s1.jsonl").write_text("\n".join(lines) + "\n")

    def test_blind_launch_is_recorded_without_names_paths_or_prompt(self):
        code, _, err = run_cli("claude", "-y", "--mcp", "none", "lineage", "de", "sales-api-java", "SECRETWORD")
        self.assertEqual(code, 0)
        self.assertRegex(err, r"blind: session [0-9a-f]{6}")
        rows = stats.build_report()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "blind")
        self.assertEqual(rows[0]["repos"].split("/")[1], "4")
        blob = json.dumps(rows) + stats.format_report(rows)
        for forbidden in ("sales-api-java", "SECRETWORD", str(self.work), str(self.home), "sessions"):
            self.assertNotIn(forbidden, blob)

    def test_log_file_is_owner_only(self):
        run_cli("claude", "-y", "hello")
        self.assertEqual(oct(stats.log_path().stat().st_mode & 0o777), "0o600")

    def test_plain_mode_starts_the_cli_unfiltered_and_is_recorded(self):
        code, _, err = run_cli("claude", "--plain", "hello", "world")
        self.assertEqual(code, 0)
        self.assertEqual(self.execvp.call_args[0][1], ["claude", "hello world"])
        self.assertIn("plain claude", err)
        self.assertEqual(stats.build_report()[0]["mode"], "plain")

    def test_plain_dry_run_prints_command_and_records_nothing(self):
        code, out, _ = run_cli("gemini", "--plain", "--dry-run", "hello")
        self.assertEqual(out.strip(), "gemini -i hello")
        self.assertEqual(stats.read_log(), [])

    def test_claude_usage_is_read_as_numbers_only_and_deduplicated(self):
        run_cli("claude", "-y", "--mcp", "none", "hello")
        cwd = stats.read_log()[0]["cwd"]
        self.claude_transcript(cwd, [("m1", 2, 9000, 100, 50), ("m1", 2, 9000, 100, 50), ("m2", 3, 0, 9100, 70)])
        row = stats.build_report()[0]
        self.assertEqual(row["first_turn_tokens"], 2 + 9000 + 100)
        self.assertEqual(row["turns"], 2)
        self.assertEqual(row["total_tokens"], (2 + 9000 + 100 + 3 + 9100) + (50 + 70))
        self.assertNotIn("SECRET", json.dumps(row) + stats.format_report([row]))

    def test_gemini_usage_best_effort_format(self):
        # Format assumed, not verified against a real Gemini CLI session file.
        import hashlib
        run_cli("gemini", "-y", "--mcp", "none", "hello")
        cwd = stats.read_log()[0]["cwd"]
        folder = self.home / ".gemini" / "tmp" / hashlib.sha256(cwd.encode()).hexdigest() / "chats"
        folder.mkdir(parents=True)
        (folder / "session-1.json").write_text(json.dumps({"messages": [
            {"type": "user", "content": "SECRET"},
            {"type": "gemini", "content": "SECRET", "tokens": {"input": 12000, "output": 80, "cached": 0}},
            {"type": "gemini", "content": "SECRET", "tokens": {"input": 12100, "output": 60}}]}))
        row = stats.build_report()[0]
        self.assertEqual((row["first_turn_tokens"], row["turns"]), (12000, 2))

    def test_no_session_file_means_dashes_not_errors(self):
        run_cli("claude", "-y", "hello")
        row = stats.build_report()[0]
        self.assertIsNone(row["first_turn_tokens"])
        self.assertIn("-", stats.format_report([row]))

    def test_manual_note_fills_in_numbers(self):
        run_cli("claude", "-y", "hello")
        rid = stats.read_log()[0]["id"]
        code, out, _ = run_cli("stats", "note", rid, "first_turn_tokens=21000", "input_tokens=90000", "output_tokens=4000")
        self.assertEqual(code, 0)
        row = stats.build_report()[0]
        self.assertEqual((row["first_turn_tokens"], row["total_tokens"]), (21000, 94000))
        self.assertEqual(run_cli("stats", "note", rid, "oops=x")[0], 2)

    def test_stats_command_json_and_empty(self):
        _, out, _ = run_cli("stats")
        self.assertIn("no launches recorded", out)
        run_cli("claude", "-y", "hello")
        code, out, _ = run_cli("stats", "--json")
        self.assertEqual(len(json.loads(out)), 1)

    def test_estimate_counts_only_visible_skills(self):
        d = self.home / ".claude" / "skills" / "alpha"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: alpha\ndescription: " + "word " * 200 + "\n---\n")
        everything = stats.estimate_startup(str(self.tmp), "claude", set(), self.cfg)
        hidden = stats.estimate_startup(str(self.tmp), "claude", {"alpha"}, self.cfg)
        self.assertGreater(everything["skills_tokens"], 100)
        self.assertEqual(hidden["skills_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
