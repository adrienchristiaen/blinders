import asyncio
import os
import unittest
from pathlib import Path

try:
    from textual.widgets import Input, SelectionList
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
                app.query_one("#prompt", Input).value = "fix beta with bigquery"
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
                app.query_one("#prompt", Input).value = "alpha again"
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
                app.query_one("#prompt", Input).value = "gamma"
                await pilot.press("enter")          # before the debounce timer fires
            self.assertEqual(app.return_value.repos, ["gamma"])
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
