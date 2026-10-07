import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from blinders import cli
from blinders.config import Config
from blinders.gitsync import graph_state, head_commit, hide_graph_output, sync_repo

from helpers import Sandbox


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


class GitSandbox(Sandbox):
    def setUp(self):
        super().setUp()
        for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
            os.environ[k] = "t"
        for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            os.environ[k] = "t@example.com"
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in (
            "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")])
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.app = self.work / "app"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.app)], check=True, capture_output=True)
        git(self.app, "checkout", "-q", "-B", "main")
        self.commit(self.app, "a.txt", "1")
        git(self.app, "push", "-q", "-u", "origin", "main")
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        subprocess.run(["git", "-C", str(self.app), "remote", "set-head", "origin", "main"], check=True, capture_output=True)
        self.pusher = self.tmp / "pusher"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.pusher)], check=True, capture_output=True)

    def commit(self, repo, name, content):
        (Path(repo) / name).write_text(content)
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", f"add {name}")

    def push_new(self, name="b.txt"):
        self.commit(self.pusher, name, "x")
        git(self.pusher, "push", "-q", "origin", "main")


class SyncTests(GitSandbox):
    def sync(self, switch=False):
        return sync_repo(str(self.app), self.cfg, switch)

    def test_fast_forwards_new_commits(self):
        self.push_new()
        r = self.sync()
        self.assertEqual((r.status, r.moved, r.branch), ("updated", 1, "main"))
        self.assertTrue((self.app / "b.txt").exists())

    def test_current_when_nothing_new(self):
        self.assertEqual(self.sync().status, "current")

    def test_uncommitted_changes_are_never_touched(self):
        self.push_new()
        (self.app / "a.txt").write_text("edited")
        r = self.sync(switch=True)
        self.assertEqual(r.status, "skipped")
        self.assertIn("uncommitted", r.detail)
        self.assertEqual((self.app / "a.txt").read_text(), "edited")
        self.assertFalse((self.app / "b.txt").exists())

    def test_untracked_files_do_not_block(self):
        (self.app / "scratch.txt").write_text("x")
        self.push_new()
        self.assertEqual(self.sync().status, "updated")
        self.assertTrue((self.app / "scratch.txt").exists())

    def test_safe_mode_leaves_a_feature_branch_alone(self):
        git(self.app, "checkout", "-q", "-b", "feature")
        self.push_new()
        r = self.sync(switch=False)
        self.assertEqual(r.status, "left-on-branch")
        self.assertEqual(git(self.app, "symbolic-ref", "--short", "HEAD"), "feature")
        self.assertFalse((self.app / "b.txt").exists())

    def test_switch_mode_checks_out_root_branch_and_keeps_the_feature_branch(self):
        git(self.app, "checkout", "-q", "-b", "feature")
        self.commit(self.app, "wip.txt", "work in progress")
        self.push_new()
        r = self.sync(switch=True)
        self.assertEqual((r.status, r.branch), ("updated", "main"))
        self.assertEqual(git(self.app, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertIn("feature", git(self.app, "branch", "--list", "feature"))
        git(self.app, "checkout", "-q", "feature")
        self.assertTrue((self.app / "wip.txt").exists())

    def test_detached_head_is_skipped(self):
        git(self.app, "checkout", "-q", "--detach")
        self.assertEqual(self.sync(switch=True).detail, "detached HEAD")

    def test_diverged_root_branch_is_reported_and_local_commit_survives(self):
        self.commit(self.app, "local.txt", "mine")
        self.push_new()
        r = self.sync()
        self.assertEqual(r.status, "failed")
        self.assertIn("fast-forward", r.detail)
        self.assertTrue((self.app / "local.txt").exists())

    def test_merge_in_progress_is_skipped(self):
        (self.app / ".git" / "MERGE_HEAD").write_text(head_commit(str(self.app)))
        self.assertIn("in progress", self.sync().detail)

    def test_no_remote_is_skipped(self):
        git(self.app, "remote", "remove", "origin")
        self.assertEqual(self.sync().detail, "no remote")

    def test_root_branch_falls_back_to_master(self):
        git(self.app, "remote", "set-head", "origin", "-d")
        git(self.app, "branch", "-m", "main", "master")
        git(self.origin, "branch", "-m", "main", "master")
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/master")
        git(self.app, "fetch", "-q", "--prune", "origin")
        git(self.app, "branch", "-u", "origin/master")
        r = self.sync()
        self.assertIn(r.status, ("current", "updated"), r.detail)

    def test_fetch_failure_is_reported(self):
        git(self.app, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        r = self.sync()
        self.assertEqual(r.status, "failed")


class GraphFreshnessTests(GitSandbox):
    def report(self, commit):
        d = self.app / "graphify-out"
        d.mkdir(exist_ok=True)
        (d / "GRAPH_REPORT.md").write_text(f"# Graph Report\n\n## Graph Freshness\n- Built from commit: `{commit}`\n")
        return str(d / "GRAPH_REPORT.md")

    def test_states(self):
        head = head_commit(str(self.app))
        self.assertEqual(graph_state("", str(self.app)), "none")
        self.assertEqual(graph_state(self.report(head[:8]), str(self.app)), "fresh")
        self.assertEqual(graph_state(self.report("deadbeef"), str(self.app)), "stale")
        (self.app / "graphify-out" / "GRAPH_REPORT.md").write_text("# no commit info\n")
        self.assertEqual(graph_state(str(self.app / "graphify-out" / "GRAPH_REPORT.md"), str(self.app)), "unknown")

    def test_new_commit_makes_a_fresh_graph_stale(self):
        report = self.report(head_commit(str(self.app))[:8])
        self.commit(self.app, "c.txt", "x")
        self.assertEqual(graph_state(report, str(self.app)), "stale")

    def test_graphify_out_is_hidden_from_git_status_once(self):
        self.report("abc1234")
        self.assertIn("graphify-out", git(self.app, "status", "--porcelain"))
        hide_graph_output(str(self.app))
        hide_graph_output(str(self.app))
        self.assertEqual(git(self.app, "status", "--porcelain"), "")
        self.assertEqual((self.app / ".git" / "info" / "exclude").read_text().count("graphify-out"), 1)


class SyncCommandTests(GitSandbox):
    def setUp(self):
        super().setUp()
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n')
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls.log"
        script = (
            "#!/bin/sh\n"
            f'echo "$@" >> {self.calls}\n'
            'mkdir -p "$2/graphify-out"\n'
            'c=$(git -C "$2" rev-parse HEAD | cut -c1-8)\n'
            'printf \'## Graph Freshness\\n- Built from commit: `%s`\\n\' "$c" > "$2/graphify-out/GRAPH_REPORT.md"\n'
        )
        (self.bin / "graphify").write_text(script)
        (self.bin / "graphify").chmod(0o755)
        (self.bin / "gemini").write_text("#!/bin/sh\n")
        (self.bin / "gemini").chmod(0o755)
        os.environ["PATH"] = f"{self.bin}:{os.environ['PATH']}"

    def test_sync_updates_repo_then_builds_graph_then_skips_when_current(self):
        self.push_new()
        code, out, _ = run_cli("sync")
        self.assertEqual(code, 0)
        self.assertIn("updated  app: 1 commit(s) on main", out)
        self.assertIn("graphs: 1 refreshed", out)
        self.assertEqual(git(self.app, "status", "--porcelain"), "")  # graphify-out hidden
        code, out, _ = run_cli("sync")
        self.assertIn("graphs: all current", out)
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)  # extract + cluster-only, nothing more

    def test_sync_switches_to_root_branch_unless_safe(self):
        git(self.app, "checkout", "-q", "-b", "feature")
        _, out, _ = run_cli("sync", "--safe", "--no-graph")
        self.assertIn("left-on-branch", out)
        self.assertEqual(git(self.app, "symbolic-ref", "--short", "HEAD"), "feature")
        run_cli("sync", "--no-graph")
        self.assertEqual(git(self.app, "symbolic-ref", "--short", "HEAD"), "main")

    def test_status_reports_missing_graph_and_dirty_repo(self):
        (self.app / "a.txt").write_text("edited")
        code, out, _ = run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("app", out)
        self.assertIn("dirty", out)
        self.assertIn("1 missing", out)

    def test_launch_syncs_opened_repo_in_safe_mode_and_not_on_dry_run(self):
        self.push_new()
        run_cli("run", "gemini", "--dry-run", "--mcp", "none", "-r", "app")
        self.assertFalse((self.app / "b.txt").exists())
        with mock.patch.object(cli.os, "execvp"), mock.patch.object(cli.os, "chdir"):
            code, _, err = run_cli("run", "gemini", "--mcp", "none", "-r", "app", "hello")
        self.assertEqual(code, 0)
        self.assertIn("sync app: pulled 1 commit(s) on main", err)
        self.assertTrue((self.app / "b.txt").exists())
        self.assertIn("graph app: built", err)

    def test_no_sync_flag_and_non_git_repos_do_not_crash(self):
        self.push_new()
        with mock.patch.object(cli.os, "execvp"), mock.patch.object(cli.os, "chdir"):
            run_cli("run", "gemini", "--mcp", "none", "--no-sync", "-r", "app", "hello")
        self.assertFalse((self.app / "b.txt").exists())
        fake = self.work / "fake"
        (fake / ".git").mkdir(parents=True)
        (fake / "README.md").write_text("not a real repo")
        run_cli("init")
        with mock.patch.object(cli.os, "execvp"), mock.patch.object(cli.os, "chdir"):
            code, _, err = run_cli("run", "gemini", "--mcp", "none", "-r", "fake", "hello")
        self.assertEqual(code, 0)
        self.assertIn("sync fake: skipped", err)


if __name__ == "__main__":
    unittest.main()
