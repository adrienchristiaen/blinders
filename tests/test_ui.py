import asyncio
import os
import unittest
from pathlib import Path

try:
    from textual import events
    from textual.widgets import SelectionList, TextArea
    from blinders.ui import BlindApp, Item, UiPlan
    HAVE_TEXTUAL = True
except ImportError:  # the UI is an optional extra
    HAVE_TEXTUAL = False

from helpers import Sandbox


def fake_plan(cli, prompt):
    """Ticks the repo whose name appears in the prompt; MCP/skills depend on the CLI to prove a CLI switch recomputes."""
    repos = [Item(n, n in prompt, "named" if n in prompt else "") for n in ("alpha", "beta", "gamma")]
    mcp = [Item(f"{cli}-github", "github" in prompt), Item("bq", "bigquery" in prompt), Item("core", True, locked=True)]
    skills = [Item("pdf", "pdf" in prompt), Item("slides", False)]
    return UiPlan(repos, mcp, skills, info=["note: demo"])


def run(coro):
    return asyncio.run(coro)


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class UiTests(unittest.TestCase):
    def app(self, prompt="", hints=None):
        return BlindApp(["gemini", "claude"], "gemini", prompt, fake_plan, hints)

    async def settle(self, pilot):
        await pilot.pause(0.5)
        await pilot.app.workers.wait_for_complete()
        await pilot.pause(0.1)

    def selected(self, app, key):
        return sorted(str(v) for v in app.query_one(f"#{key}", SelectionList).selected)

    def test_lists_follow_the_prompt_as_you_type(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                self.assertEqual(self.selected(app, "repos"), [])
                app.query_one("#prompt", TextArea).text = "fix beta with bigquery"
                await self.settle(pilot)
                self.assertEqual(self.selected(app, "repos"), ["beta"])
                self.assertEqual(self.selected(app, "mcp"), ["bq", "core"])
        run(go())

    def test_manual_choice_survives_a_new_prompt(self):
        async def go():
            app = self.app("alpha")
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                app.query_one("#repos", SelectionList).toggle("gamma")
                app.query_one("#repos", SelectionList).toggle("alpha")   # unticked by hand
                await pilot.pause(0.1)
                app.query_one("#prompt", TextArea).text = "alpha again"
                await self.settle(pilot)
                self.assertEqual(self.selected(app, "repos"), ["gamma"])
        run(go())

    def test_enter_returns_the_choice_and_escape_cancels(self):
        async def go():
            app = self.app("beta pdf")
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                await pilot.press("enter")
            res = app.return_value
            self.assertEqual((res.cli, res.prompt, res.repos, res.skills), ("gemini", "beta pdf", ["beta"], ["pdf"]))
            self.assertIn("core", res.mcp)
            app2 = self.app("beta")
            async with app2.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                await pilot.press("escape")
            self.assertIsNone(app2.return_value)
        run(go())

    def test_enter_right_after_typing_uses_the_final_prompt(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                app.query_one("#prompt", TextArea).text = "gamma"
                await pilot.press("enter")          # before the debounce timer fires
            self.assertEqual(app.return_value.repos, ["gamma"])
        run(go())

    def test_a_pasted_multi_line_paragraph_is_kept_whole(self):
        para = "First line about alpha.\n\nSecond paragraph with details,\n  - bullet one\n  - bullet two\nEnd."
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                app.post_message(events.Paste(para))   # as a terminal paste arrives: the app hands it to the focused widget
                await self.settle(pilot)
                self.assertEqual(app.query_one("#prompt", TextArea).text, para)
                self.assertEqual(self.selected(app, "repos"), ["alpha"])   # the engine saw the whole text
                await pilot.press("ctrl+l")
            self.assertEqual(app.return_value.prompt, para.strip())
        run(go())

    def test_enter_adds_a_line_once_the_prompt_has_several_lines(self):
        async def go():
            app = self.app("first line\nsecond line")
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                app.query_one("#prompt", TextArea).move_cursor((1, 11))
                await pilot.press("enter")
                await pilot.pause(0.1)
                self.assertTrue(app.is_running)
                self.assertEqual(app.query_one("#prompt", TextArea).text, "first line\nsecond line\n")
                await pilot.press("ctrl+l")
            self.assertEqual(app.return_value.prompt, "first line\nsecond line")
        run(go())

    def test_a_very_long_prompt_is_returned_intact(self):
        long = ("Explain how alpha handles the discount. " * 2500).strip()   # about 100 KB
        async def go():
            app = self.app()
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                app.query_one("#prompt", TextArea).text = long
                await pilot.press("ctrl+l")
            self.assertEqual(app.return_value.prompt, long)
            self.assertEqual(app.return_value.repos, ["alpha"])
        run(go())

    def test_always_kept_servers_cannot_be_unticked(self):
        async def go():
            app = self.app("alpha")
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                app.query_one("#mcp", SelectionList).toggle("core")
                await pilot.pause(0.2)
                self.assertIn("core", self.selected(app, "mcp"))
        run(go())

    def test_space_toggles_the_highlighted_box(self):
        async def go():
            app = self.app("")
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                app.query_one("#repos", SelectionList).focus()
                await pilot.press("space")
                await pilot.pause(0.1)
                self.assertEqual(self.selected(app, "repos"), ["alpha"])
                await pilot.press("ctrl+l")
            self.assertEqual(app.return_value.repos, ["alpha"])
        run(go())

    def test_switching_cli_recomputes_the_server_list(self):
        async def go():
            app = self.app("github")
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                self.assertIn("gemini-github", [str(o.value) for o in app.query_one("#mcp", SelectionList).options])
                app.query_one("#cli").value = "claude"
                await self.settle(pilot)
                names = [str(o.value) for o in app.query_one("#mcp", SelectionList).options]
                self.assertIn("claude-github", names)
                self.assertNotIn("gemini-github", names)
                await pilot.press("ctrl+l")
            self.assertEqual(app.return_value.cli, "claude")
        run(go())

    def test_hints_and_info_are_shown(self):
        async def go():
            app = self.app("alpha", hints=lambda cli, p, names: [f"{names[0]}: starting points", "  src/a.py"])
            async with app.run_test(size=(120, 40)) as pilot:
                await self.settle(pilot)
                info = app.query_one("#info")
                self.assertTrue(info.display)
                text = str(info.render())
                self.assertIn("note: demo", text)
                self.assertIn("src/a.py", text)
        run(go())


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class RealEngineTests(Sandbox):
    def test_ui_plan_uses_the_real_selection_and_orders_chosen_first(self):
        from blinders import cli
        from blinders.scan import build_index
        self.sales_repos()
        repos = build_index(self.cfg)
        p = cli.ui_plan(self.cfg, repos, "gemini", "lineage de sales-api-java")
        names = [i.name for i in p.repos]
        self.assertEqual(names[0], "sales-api-java")
        self.assertTrue(p.repos[0].selected)
        self.assertEqual(len(names), 4)
        self.assertEqual(sum(i.selected for i in p.repos), 1)
        self.assertEqual([i.name for i in cli.ui_plan(self.cfg, repos, "gemini", "").repos if i.selected], [])

    def test_spec_from_ui(self):
        from blinders.cli import _spec_from_ui
        self.assertEqual(_spec_from_ui(["a", "b"], ["a", "b"], None), None)
        self.assertEqual(_spec_from_ui(["a", "b"], ["a", "b"], "all"), "all")
        self.assertEqual(_spec_from_ui([], ["a", "b"], "all"), "none")
        self.assertEqual(_spec_from_ui(["a"], ["a", "b"], "all"), "a")


if __name__ == "__main__":
    unittest.main()


class DiagnosticsTests(Sandbox):
    def test_doctor_and_version_report_python_and_ui(self):
        import io
        from contextlib import redirect_stdout
        from blinders import cli
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["doctor"]), 0)
        self.assertIn("full-screen launcher", out.getvalue())
        self.assertIn("python", out.getvalue())
        self.assertIn("full-screen launcher", cli._version_text())

    def test_text_fallback_says_why_the_screen_did_not_open(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock
        from blinders import cli
        self.sales_repos()
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\ndefault_cli = "gemini"\n')
        err = io.StringIO()
        with mock.patch.object(cli, "_interactive", return_value=True), \
             mock.patch.object(cli, "ui_available", return_value=False), \
             mock.patch.object(cli, "_ask", return_value="q"), \
             redirect_stderr(err), redirect_stdout(io.StringIO()):
            cli.main([])
        self.assertIn("needs Textual in this Python", err.getvalue())
