import io
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from blinders import cli, pipeline
from blinders.pipeline import run_graphs, run_map, run_sync
from blinders.scan import build_index
from blinders.uimodel import UiResult

from test_sync import GitSandbox, git


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Collect:
    def __init__(self):
        self.events = []

    def __call__(self, ev):
        self.events.append(ev)

    def lines(self, step, kind="line"):
        return [e.text for e in self.events if e.step == step and e.kind == kind]

    def last(self, step):
        return [e for e in self.events if e.step == step and e.kind == "done"][-1]


class PipelineTests(GitSandbox):
    def setUp(self):
        super().setUp()
        self.cfg.sync_fleet = "switch"
        self.cfg.sync_ttl_minutes = 30
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls.log"
        (self.bin / "graphify").write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> {self.calls}\n'
            'mkdir -p "$2/graphify-out"\n'
            'c=$(git -C "$2" rev-parse HEAD | cut -c1-8)\n'
            'printf \'## Graph Freshness\\n- Built from commit: `%s`\\n\' "$c" > "$2/graphify-out/GRAPH_REPORT.md"\n')
        (self.bin / "graphify").chmod(0o755)
        os.environ["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        self.repos = build_index(self.cfg)

    # --- step 1 -----------------------------------------------------------------------------------
    def test_sync_reports_updates_and_summary(self):
        self.push_new()
        ev = Collect()
        counts = run_sync(self.cfg, self.repos, ev)
        self.assertEqual(counts["updated"], 1)
        self.assertIn("app: pulled 1 commit(s) on main", ev.lines(1))
        self.assertIn("1 updated", ev.last(1).text)
        self.assertEqual(ev.last(1).level, "ok")

    def test_work_branch_is_switched_in_switch_mode_and_left_in_safe_mode(self):
        git(self.app, "checkout", "-q", "-b", "feature")
        self.cfg.sync_fleet = "safe"
        ev = Collect()
        run_sync(self.cfg, self.repos, ev, force=True)
        self.assertTrue(any("left alone" in t and "feature" in t for t in ev.lines(1)))
        self.assertEqual(git(self.app, "symbolic-ref", "--short", "HEAD"), "feature")
        self.cfg.sync_fleet = "switch"
        run_sync(self.cfg, self.repos, Collect(), force=True)
        self.assertEqual(git(self.app, "symbolic-ref", "--short", "HEAD"), "main")

    def test_recent_sync_is_skipped_unless_forced(self):
        run_sync(self.cfg, self.repos, Collect())
        self.push_new()
        ev = Collect()
        self.assertEqual(run_sync(self.cfg, self.repos, ev), {"skipped": "recent"})
        self.assertIn("skipped", ev.last(1).text)
        self.assertFalse((self.app / "b.txt").exists())
        run_sync(self.cfg, self.repos, Collect(), force=True)
        self.assertTrue((self.app / "b.txt").exists())

    def test_old_sync_runs_again_and_off_mode_never_touches_git(self):
        run_sync(self.cfg, self.repos, Collect())
        stamp = pipeline._fleet_file()
        stamp.write_text('{"synced_at": %f}' % (time.time() - 3 * 3600))
        self.push_new()
        run_sync(self.cfg, self.repos, Collect())
        self.assertTrue((self.app / "b.txt").exists())
        self.cfg.sync_fleet = "off"
        self.push_new("c.txt")
        run_sync(self.cfg, self.repos, Collect(), force=True)
        self.assertFalse((self.app / "c.txt").exists())

    def test_stop_cancels_repos_that_have_not_started(self):
        ev = Collect()
        run_sync(self.cfg, self.repos, ev, should_stop=lambda: True)
        self.assertIn("cancelled", " ".join(ev.lines(1)))
        self.assertIsNone(pipeline.minutes_since_sync())   # a cancelled run is not remembered as a sync

    # --- step 2 -----------------------------------------------------------------------------------
    def test_graphs_are_built_then_refreshed_only_when_the_repo_moved(self):
        ev = Collect()
        repos = run_graphs(self.cfg, self.repos, ev)
        self.assertIn("app: graph built", ev.lines(2))
        self.assertTrue(repos[0].graph_report.endswith("GRAPH_REPORT.md"))
        self.assertEqual(git(self.app, "status", "--porcelain"), "")
        ev = Collect()
        run_graphs(self.cfg, repos, ev)
        self.assertIn("all 1 graphs are current", ev.last(2).text)
        self.push_new()
        run_sync(self.cfg, repos, Collect(), force=True)
        ev = Collect()
        run_graphs(self.cfg, repos, ev)
        self.assertIn("app: graph updated", ev.lines(2))
        self.assertTrue(any(c.startswith("update ") for c in self.calls.read_text().splitlines()))

    def test_missing_graphify_is_a_warning_not_an_error(self):
        os.environ["PATH"] = "/nonexistent"
        ev = Collect()
        out = run_graphs(self.cfg, self.repos, ev)
        self.assertEqual(out, self.repos)
        self.assertEqual(ev.last(2).level, "warn")
        self.assertIn("uv tool install graphifyy", ev.last(2).text)

    def test_progress_events_count_up(self):
        ev = Collect()
        run_graphs(self.cfg, self.repos, ev)
        progress = [(e.done, e.total) for e in ev.events if e.step == 2 and e.kind == "progress"]
        self.assertEqual(progress, [(1, 1)])

    # --- step 4 -----------------------------------------------------------------------------------
    def test_map_names_the_starting_points(self):
        graph = self.app / "graphify-out"
        graph.mkdir()
        (graph / "GRAPH_REPORT.md").write_text("x")
        import json
        nodes = [{"id": "r", "label": "PricingRule", "source_file": "src/rule.py", "source_location": "L3", "file_type": "code"}]
        nodes += [{"id": f"u{i}", "label": f"helper_{i}()", "source_file": "src/util.py", "source_location": f"L{i}", "file_type": "code"} for i in range(60)]
        (graph / "graph.json").write_text(json.dumps({"nodes": nodes, "links": []}))
        repos = build_index(self.cfg)
        ev = Collect()
        hints = run_map(self.cfg, repos, "why does PricingRule fail", ev)
        self.assertIn("app", hints)
        self.assertTrue(any("src/rule.py" in t for t in ev.lines(4)))
        self.assertIn("1 starting point(s)", ev.last(4).text)

    def test_map_with_no_repo_is_a_blind_session(self):
        ev = Collect()
        run_map(self.cfg, [], "x", ev)
        self.assertIn("fully blind", ev.last(4).text)

    def test_map_without_a_graph_says_so(self):
        ev = Collect()
        run_map(self.cfg, self.repos, "something", ev)
        self.assertTrue(any("no graph" in t for t in ev.lines(4)))


def fake_ui(result):
    """Stand-in for blinders.ui so these tests run without Textual installed."""
    import sys
    import types
    module = types.ModuleType("blinders.ui")
    module.run_ui = lambda *a, **k: result
    return mock.patch.dict(sys.modules, {"blinders.ui": module})


class CliFlowTests(GitSandbox):
    def setUp(self):
        super().setUp()
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\ndefault_cli = "gemini"\n')
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for n in ("gemini", "graphify"):
            (self.bin / n).write_text("#!/bin/sh\nmkdir -p \"$2/graphify-out\"; echo 'Built from commit: `abc12345`' > \"$2/graphify-out/GRAPH_REPORT.md\"\n")
            (self.bin / n).chmod(0o755)
        os.environ["PATH"] = f"{self.bin}:{os.environ['PATH']}"
        self.answers = []
        patches = [
            mock.patch.object(cli, "_interactive", return_value=True),
            mock.patch.object(cli.os, "execvp"),
            mock.patch.object(cli.os, "chdir"),
        ]
        self.execvp = patches[1].start()
        for p in (patches[0], patches[2]):
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def test_text_mode_prints_steps_one_and_two_before_asking_for_the_prompt(self):
        self.push_new()
        order = []
        with mock.patch.object(cli, "ui_available", return_value=False), \
             mock.patch.object(cli, "_ask", side_effect=lambda q: order.append(q) or ""):
            code, _, err = run_cli()
        self.assertEqual(code, 0)
        self.assertIn("1/4 Update repos: checking out the root branch and pulling 1 repos", err)
        self.assertIn("1/4 Update repos: 1 updated", err)
        self.assertIn("2/4 Build code graphs: building 1 graph(s)", err)
        self.assertLess(err.index("1/4"), err.index("2/4"))
        self.assertTrue((self.app / "b.txt").exists())
        self.assertTrue(order[0].startswith("gemini prompt"))

    def test_no_fleet_and_direct_prompt_skip_steps_one_and_two(self):
        self.push_new()
        with mock.patch.object(cli, "ui_available", return_value=False), mock.patch.object(cli, "_ask", return_value=""):
            _, _, err = run_cli("--no-fleet")
        self.assertNotIn("1/4", err)
        _, _, err = run_cli("gemini", "hello", "-y")
        self.assertNotIn("1/4", err)

    def test_screen_result_drives_the_launch_and_leaves_the_recap_in_the_scrollback(self):
        fake = UiResult("gemini", "fix the app", ["app"], [], [],
                        ["✔ 1  Update repos          1 updated", "✔ 2  Build code graphs     all current",
                         "✔ 3  Select what to keep   1 of 1 repos", "✔ 4  Map the chosen repos  0 starting points"])
        with mock.patch.object(cli, "ui_available", return_value=True), fake_ui(fake):
            code, _, err = run_cli()
        self.assertEqual(code, 0)
        for line in fake.recap:
            self.assertIn(line, err)
        argv = self.execvp.call_args[0][1]
        self.assertEqual(argv[0], "gemini")
        self.assertIn(str(self.app), " ".join(argv))
        self.assertNotIn("sync app", err)    # step 1 already did it: no second sync at launch

    def test_cancelling_the_screen_launches_nothing(self):
        with mock.patch.object(cli, "ui_available", return_value=True), fake_ui(None):
            code, _, err = run_cli()
        self.assertEqual(code, 1)
        self.assertIn("cancelled", err)
        self.execvp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
