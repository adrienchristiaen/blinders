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


class ModelListTests(HomeSandbox):
    def test_blind_models_lists_default_recent_use_and_aliases_and_warns_about_the_router(self):
        chats = self.gem / "tmp" / "abc" / "chats"
        chats.mkdir(parents=True)
        (chats / "s.json").write_text(json.dumps({"messages": [
            {"type": "user"}, {"type": "gemini", "model": "gemini-3.8-flash"}, {"type": "gemini", "model": "gemini-3.8-flash"},
            {"type": "gemini", "model": "gemini-2.5-pro"}]}))
        (self.gem / "settings.json").write_text(json.dumps({"model": {"name": "auto"}}))
        code, out, _ = run_cli("models")
        self.assertEqual(code, 0)
        self.assertIn("gemini-3.8-flash", out)
        self.assertIn("used in your recent chats (2 replies)", out)
        self.assertIn("gemini-2.5-pro", out)
        for alias in ("pro", "flash", "flash-lite"):
            self.assertRegex(out, rf"\n  {alias} ")
        self.assertIn("Gemini's own router runs", out)

    def test_a_concrete_default_is_listed_first_and_no_router_warning(self):
        (self.gem / "settings.json").write_text(json.dumps({"model": {"name": "gemini-2.5-pro"}}))
        _, out, _ = run_cli("models")
        self.assertIn("settings.json: gemini-2.5-pro", out)
        self.assertNotIn("Gemini's own router runs", out)
        from blinders.models import gemini_model_list
        self.assertEqual(gemini_model_list(self.home)[0], ("gemini-2.5-pro", "your default in settings.json"))

    def test_installed_ids_come_from_the_cli_bundle(self):
        bundle = self.tmp / "lib" / "gemini-cli" / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "gemini.js").write_text("")
        (bundle / "gemini.js").chmod(0o755)
        (bundle / "chunk.js").write_text(
            'var PRO = "gemini-9-pro";\nvar FLASH = "gemini-9-flash";\nvar NONE = "none";\n'
            'var VALID_GEMINI_MODELS = /* @__PURE__ */ new Set([\n  PRO,\n  FLASH,\n  NONE,\n]);\n')
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "gemini").symlink_to(bundle / "gemini.js")
        os.environ["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
        from blinders.models import installed_gemini_ids
        self.assertEqual(installed_gemini_ids(), ["gemini-9-pro", "gemini-9-flash"])

    def test_without_gemini_installed_the_aliases_still_show(self):
        os.environ["PATH"] = str(self.tmp)   # no gemini on PATH
        from blinders.models import gemini_model_list
        self.assertEqual([m for m, _ in gemini_model_list(self.home)][:4], ["auto", "pro", "flash", "flash-lite"])


class ToolOutputTests(HomeSandbox):
    def build(self):
        session = self.tmp / "session"
        session.mkdir(exist_ok=True)
        home = geminihome.build_home(session, self.home, self.cfg)
        return json.loads((home / ".gemini" / "settings.json").read_text())

    def test_large_tool_outputs_are_cut_by_default(self):
        self.assertEqual(self.build()["tools"]["truncateToolOutputThreshold"], 12000)

    def test_a_threshold_you_set_yourself_wins_and_zero_disables(self):
        (self.gem / "settings.json").write_text(json.dumps({"tools": {"truncateToolOutputThreshold": 5000}}))
        self.assertEqual(self.build()["tools"]["truncateToolOutputThreshold"], 5000)
        (self.gem / "settings.json").write_text("{}")
        self.cfg.gemini_tool_output_chars = 0
        self.assertNotIn("tools", self.build())


class RtkTests(HomeSandbox):
    def build(self, rtk="/opt/bin/rtk"):
        session = self.tmp / "session"
        session.mkdir(exist_ok=True)
        home = geminihome.build_home(session, self.home, self.cfg, rtk=rtk)
        return home, json.loads((home / ".gemini" / "settings.json").read_text())

    def test_the_session_gets_rtk_s_before_tool_hook(self):
        home, conf = self.build()
        hook = conf["hooks"]["BeforeTool"][0]
        self.assertEqual(hook["matcher"], "run_shell_command")
        wrapper = Path(hook["hooks"][0]["command"])
        self.assertEqual(wrapper, home / "rtk-hook-gemini.sh")
        self.assertEqual(wrapper.read_text(), "#!/bin/bash\nexec /opt/bin/rtk hook gemini\n")
        self.assertTrue(os.access(wrapper, os.X_OK))
        # the real settings are untouched
        self.assertNotIn("hooks", json.loads((self.gem / "settings.json").read_text()))

    def test_other_hooks_are_kept_and_an_existing_rtk_hook_is_not_doubled(self):
        other = {"matcher": "write_file", "hooks": [{"type": "command", "command": "/x/audit.sh"}]}
        (self.gem / "settings.json").write_text(json.dumps({"hooks": {"BeforeTool": [other]}}))
        _, conf = self.build()
        self.assertEqual(len(conf["hooks"]["BeforeTool"]), 2)
        mine = {"matcher": "run_shell_command", "hooks": [{"type": "command", "command": "/h/rtk-hook-gemini.sh"}]}
        (self.gem / "settings.json").write_text(json.dumps({"hooks": {"BeforeTool": [mine]}}))
        _, conf = self.build()
        self.assertEqual(len(conf["hooks"]["BeforeTool"]), 1)

    def test_without_rtk_no_hook(self):
        _, conf = self.build(rtk=None)
        self.assertNotIn("hooks", conf)


class RtkLaunchTests(HomeSandbox):
    def setUp(self):
        super().setUp()
        self.sales_repos()
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n')
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "rtk"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)

    def test_rtk_on_path_adds_the_hook_and_a_note_for_the_agent(self):
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        _, out, _ = run_cli("run", "gemini", "--dry-run", "hello")
        session = session_of(out)
        conf = json.loads((session / "gemini-home" / ".gemini" / "settings.json").read_text())
        self.assertIn("BeforeTool", conf["hooks"])
        self.assertIn("rtk proxy", (session / "GEMINI.md").read_text())

    def test_no_rtk_or_switched_off_means_nothing(self):
        _, out, _ = run_cli("run", "gemini", "--dry-run", "hello")
        conf = json.loads((session_of(out) / "gemini-home" / ".gemini" / "settings.json").read_text())
        self.assertNotIn("hooks", conf)
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n[gemini]\nrtk = false\n')
        _, out, _ = run_cli("run", "gemini", "--dry-run", "hello")
        session = session_of(out)
        self.assertNotIn("hooks", json.loads((session / "gemini-home" / ".gemini" / "settings.json").read_text()))
        self.assertNotIn("rtk proxy", (session / "GEMINI.md").read_text())


class ModelCommandTests(HomeSandbox):
    def setUp(self):
        super().setUp()
        self.sales_repos()
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n')

    def test_blind_model_explains_a_light_choice(self):
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n[models.gemini]\nlight = "flash-lite"\n')
        code, out, _ = run_cli("model", "où", "est", "la", "classe", "principale", "?", "-r", "sales-api-java")
        self.assertEqual(code, 0)
        self.assertIn("tier:   light", out)
        self.assertIn("flag:   -m flash-lite", out)

    def test_blind_model_explains_why_it_stayed_standard(self):
        _, out, _ = run_cli("model", "ajoute", "un", "test", "-r", "sales-api-java")
        self.assertIn("tier:   standard", out)
        self.assertIn("asks for work (ajoute)", out)
        self.assertIn("none (the CLI picks", out)

    def test_a_configured_standard_model_is_always_passed(self):
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n[models.gemini]\nstandard = "flash"\n')
        _, out, _ = run_cli("model", "ajoute", "un", "test", "-r", "sales-api-java")
        self.assertIn("flag:   -m flash", out)


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
