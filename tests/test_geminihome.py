import json
import os
import unittest
from pathlib import Path

from blinders import geminihome
from blinders.models import classify
from blinders.skills import discover

from helpers import Sandbox
from test_start import run_cli, write_skills
from helpers import session_of


class JsonTests(unittest.TestCase):
    def test_comments_and_trailing_commas_are_tolerated(self):
        text = '{\n  // a comment\n  "a": "http://x//y", /* block */\n  "b": [1, 2,],\n}'
        self.assertEqual(json.loads(geminihome.strip_json_comments(text)), {"a": "http://x//y", "b": [1, 2]})


class HomeSandbox(Sandbox):
    def setUp(self):
        super().setUp()
        self.gem = self.home / ".gemini"
        (self.gem / "extensions").mkdir(parents=True)
        (self.gem / "GEMINI.md").write_text("global rules " * 50)
        (self.gem / "oauth_creds.json").write_text("{}")
        (self.gem / "tmp").mkdir()
        self.big = self.work / "unrelated"
        self.big.mkdir()
        (self.big / "GEMINI.md").write_text("unrelated repo rules " * 100)
        (self.gem / "settings.json").write_text(json.dumps({
            "security": {"auth": {"selectedType": "oauth-personal"}},
            "context": {"includeDirectories": [str(self.big)]},
            "mcpServers": {"github": {"command": "gh"}, "bigquery": {"command": "bq"}},
        }))
        self.ext("security-pack", skills={"sec-scan": "Scan code for security issues and secrets."})
        self.ext("jira-pack", mcp=["jira"], skills={"jira-tickets": "Create Jira tickets and boards."})
        write_skills(self.gem / "skills", {"pdf-tools": "Extract text and tables from PDF documents."})

    def ext(self, name, skills=None, mcp=None):
        d = self.gem / "extensions" / name
        d.mkdir(parents=True)
        manifest = {"name": name, "version": "1.0.0", "description": f"{name} extension"}
        if mcp:
            manifest["mcpServers"] = {m: {"command": m} for m in mcp}
        (d / "gemini-extension.json").write_text(json.dumps(manifest))
        (d / "GEMINI.md").write_text(f"{name} context " * 40)
        write_skills(d / "skills", skills or {})


class BuildHomeTests(HomeSandbox):
    def build(self, **kw):
        session = self.tmp / "session"
        session.mkdir()
        return geminihome.build_home(session, self.home, self.cfg, **kw)

    def test_settings_lose_include_directories_and_unkept_mcp(self):
        home = self.build(kept_mcp={"github"})
        conf = json.loads((home / ".gemini" / "settings.json").read_text())
        self.assertNotIn("includeDirectories", conf["context"])
        self.assertEqual(conf["context"]["memoryBoundaryMarkers"], [])
        self.assertEqual(list(conf["mcpServers"]), ["github"])
        self.assertEqual(conf["security"]["auth"]["selectedType"], "oauth-personal")
        # the real settings are untouched
        self.assertIn("includeDirectories", json.loads((self.gem / "settings.json").read_text())["context"])
        self.assertEqual(oct((home / ".gemini" / "settings.json").stat().st_mode & 0o777), "0o600")

    def test_login_history_and_global_memory_are_linked(self):
        home = self.build()
        for name in ("oauth_creds.json", "tmp", "GEMINI.md"):
            self.assertTrue((home / ".gemini" / name).is_symlink(), name)

    def test_global_memory_can_be_dropped(self):
        self.cfg.gemini_keep_global_memory = False
        home = self.build()
        self.assertFalse((home / ".gemini" / "GEMINI.md").exists())
        self.assertTrue((home / ".gemini" / "oauth_creds.json").exists())

    def test_only_kept_extensions_and_skills_are_linked(self):
        exts = {e.name: e for e in geminihome.list_extensions(self.home)}
        skills = {s.name: s for s in discover("gemini", self.home, self.cfg)}
        home = self.build(skills=[skills["pdf-tools"]], extensions=[exts["jira-pack"]])
        self.assertEqual(sorted(p.name for p in (home / ".gemini" / "extensions").iterdir()), ["jira-pack"])
        self.assertEqual([p.name for p in (home / ".gemini" / "skills").iterdir()], ["pdf-tools"])
        self.assertTrue((home / ".gemini" / "extensions" / "jira-pack" / "GEMINI.md").exists())

    def test_none_means_everything(self):
        home = self.build()
        self.assertTrue((home / ".gemini" / "extensions").is_symlink())
        self.assertTrue((home / ".gemini" / "skills").is_symlink())

    def test_unreadable_settings_means_no_isolation(self):
        (self.gem / "settings.json").write_text("{not json")
        self.assertIsNone(self.build())


class ExtensionSelectionTests(HomeSandbox):
    def select(self, prompt, spec="auto", skills=None, mcp=None):
        from blinders.mcp import discover as mcp_discover, select_mcp
        exts = geminihome.list_extensions(self.home)
        found = discover("gemini", self.home, self.cfg)
        from blinders.skills import select_skills
        splan = select_skills(prompt, found, self.cfg, skills or "auto")
        return geminihome.select_extensions(prompt, exts, splan, None, self.cfg, spec), splan

    def kept(self, plan):
        return sorted(e.name for e in plan.kept)

    def test_extension_follows_its_kept_skill(self):
        plan, _ = self.select("scan this code for security problems")
        self.assertEqual(self.kept(plan), ["security-pack"])
        self.assertIn("bundles skill sec-scan", plan.reasons["security-pack"])

    def test_nothing_relevant_nothing_kept(self):
        plan, _ = self.select("rename this variable")
        self.assertEqual(self.kept(plan), [])

    def test_always_all_none_and_names(self):
        self.cfg.gemini_extensions_always = ["jira-pack"]
        self.assertEqual(self.kept(self.select("rename this variable")[0]), ["jira-pack"])
        self.assertEqual(self.kept(self.select("x", "all")[0]), ["jira-pack", "security-pack"])
        self.assertEqual(self.kept(self.select("x", "security-pack")[0]), ["jira-pack", "security-pack"])
        self.cfg.gemini_extensions_always = []
        self.assertEqual(self.kept(self.select("x", "none")[0]), [])

    def test_memory_sources_lists_the_heavy_files(self):
        labels = {label for label, _p, _t in geminihome.memory_sources(self.home)}
        self.assertIn("global", labels)
        self.assertIn("extension security-pack", labels)
        self.assertIn("includeDirectories unrelated", labels)


class LaunchIntegrationTests(HomeSandbox):
    def setUp(self):
        super().setUp()
        self.sales_repos()
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n')

    def test_gemini_dry_run_points_gemini_at_the_filtered_home(self):
        code, out, err = run_cli("run", "gemini", "--dry-run", "--mcp", "none", "rename a variable in sales-api-java")
        self.assertEqual(code, 0)
        self.assertIn("GEMINI_CLI_HOME=", out)
        home = Path(out.split("GEMINI_CLI_HOME=")[1].split()[0])
        self.assertEqual(home, session_of(out) / "gemini-home")
        self.assertEqual(list((home / ".gemini" / "extensions").iterdir()), [])
        conf = json.loads((home / ".gemini" / "settings.json").read_text())
        self.assertNotIn("includeDirectories", conf["context"])
        self.assertEqual(conf["mcpServers"], {})
        self.assertIn("extensions kept: 0 (hidden 2)", err)

    def test_no_isolate_and_claude_leave_the_home_alone(self):
        _, out, _ = run_cli("run", "gemini", "--dry-run", "--no-isolate", "hello")
        self.assertNotIn("GEMINI_CLI_HOME", out)
        _, out, _ = run_cli("run", "claude", "--dry-run", "hello")
        self.assertNotIn("GEMINI_CLI_HOME", out)

    def test_config_switch_and_extensions_flag(self):
        self.cfg.gemini_isolate_home = False
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n[gemini]\nisolate_home = false\n')
        _, out, _ = run_cli("run", "gemini", "--dry-run", "hello")
        self.assertNotIn("GEMINI_CLI_HOME", out)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n')
        _, out, _ = run_cli("run", "gemini", "--dry-run", "--extensions", "all", "hello")
        home = Path(out.split("GEMINI_CLI_HOME=")[1].split()[0])
        self.assertEqual(len(list((home / ".gemini" / "extensions").iterdir())), 2)


class RouterTests(unittest.TestCase):
    def test_plain_questions_are_light_even_on_two_repos_or_with_a_noun_like_lineage(self):
        self.assertEqual(classify("tu peux me dire le lineage de sales-api-java", 2)[0], "light")
        self.assertEqual(classify("c'est quoi le rôle de ce service ?", 2)[0], "light")
        self.assertEqual(classify("how does the pricing rule work?", 1)[0], "light")

    def test_work_requests_are_not_light(self):
        self.assertEqual(classify("peux-tu refactorer le client ?", 1)[0], "standard")
        self.assertEqual(classify("ajoute un test sur le client", 1)[0], "standard")
        self.assertEqual(classify("une très longue question ? " + "x " * 300, 1)[0], "standard")


if __name__ == "__main__":
    unittest.main()
