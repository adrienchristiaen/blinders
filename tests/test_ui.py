import asyncio
import os
import threading
import unittest
from pathlib import Path

try:
    from textual import events
    from textual.widgets import SelectionList, TextArea
    from blinders.pipeline import Event
    from blinders.ui import Backend, BlindApp, Item, UiPlan
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


if HAVE_TEXTUAL:
    class Fake(Backend):
        """Quick by default; ``gate`` holds step 2 until released, ``boom`` makes step 1 raise."""

        def __init__(self, gate=None, boom=False, hints=None):
            self.gate, self.boom, self.hint_fn = gate, boom, hints
            self.map_calls, self.stop_seen = [], False

        def sync(self, emit, stop):
            if self.boom:
                raise RuntimeError("git exploded")
            emit(Event(1, "start", "pulling 3 repos", 0, 3))
            emit(Event(1, "line", "alpha: pulled 2 commit(s) on main", 1, 3, "ok"))
            emit(Event(1, "progress", "alpha", 1, 3))
            emit(Event(1, "done", "1 updated, 2 already current", 3, 3, "ok"))

        def graphs(self, emit, stop):
            emit(Event(2, "start", "building 2 graph(s)", 0, 2))
            if self.gate:
                self.gate.wait(10)
            self.stop_seen = stop()
            emit(Event(2, "progress", "beta", 2, 2))
            emit(Event(2, "done", "2 built or updated, 1 already current", 2, 2, "ok"))

        def plan(self, cli, prompt):
            return fake_plan(cli, prompt)

        def hints(self, cli, prompt, names):
            return self.hint_fn(cli, prompt, names) if self.hint_fn else []

        def models(self, cli):
            return [(f"{cli}-big", "your default"), (f"{cli}-small", "used recently")]

        def tools(self, cli):
            from blinders.tools import ToolStatus
            return [ToolStatus("graphify", "on", "/bin/graphify"),
                    ToolStatus("rtk", "on" if cli == "gemini" else "off", "wired to Gemini only"),
                    ToolStatus("jq", "missing", "not installed")]

        def map(self, prompt, names, emit):
            self.map_calls.append((prompt, list(names)))
            emit(Event(4, "start", "reading 1 graph", 0, 1))
            emit(Event(4, "line", "alpha: 2 starting point(s): src/a.py", 1, 1, "ok"))
            emit(Event(4, "done", "2 starting point(s) in 1 of 1 repo(s)", 1, 1, "ok"))


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed")
class UiTests(unittest.TestCase):
    def app(self, prompt="", backend=None):
        return BlindApp(["gemini", "claude"], "gemini", prompt, backend or Fake())

    async def until(self, pilot, cond, timeout=6.0):
        waited = 0.0
        while not cond() and waited < timeout:
            await pilot.pause(0.1)
            waited += 0.1
        self.assertTrue(cond(), "condition not reached in time")

    async def selecting(self, pilot):
        await self.until(pilot, lambda: pilot.app.phase == "select")
        await pilot.app.workers.wait_for_complete()
        await pilot.pause(0.2)

    def selected(self, app, key):
        return sorted(str(v) for v in app.query_one(f"#{key}", SelectionList).selected)

    def test_tool_pills_are_green_amber_or_red_and_follow_the_cli(self):
        async def go():
            from textual.widgets import Select, Static
            app = self.app("alpha")
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)

                def pills():
                    text = app.query_one("#tools", Static).render()
                    return {span_text: str(style) for span_text, style in
                            ((text.plain[s.start:s.end].strip("● ").strip(), s.style) for s in text.spans)}
                first = pills()
                self.assertEqual(set(first), {"graphify", "rtk", "jq"})
                self.assertEqual(first["graphify"], "rgb(127,209,139)")      # on: green
                self.assertEqual(first["rtk"], "rgb(127,209,139)")
                self.assertEqual(first["jq"], "rgb(255,123,114)")            # missing: red
                app.query_one("#cli", Select).value = "claude"
                await pilot.pause(0.3)
                self.assertEqual(pills()["rtk"], "rgb(255,184,77)")         # installed but unused here: amber
        run(go())

    def test_model_selector_defaults_to_the_cli_default_and_is_returned(self):
        async def go():
            from textual.widgets import Select
            app = self.app("alpha")
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                self.assertEqual(app.query_one("#model", Select).value, "default")
                app.query_one("#model", Select).value = "gemini-small"
                await pilot.pause(0.2)
                self.assertEqual(app.model, "gemini-small")
                self.assertEqual(app.cli, "gemini")   # the model selector must not be mistaken for the CLI one
                await pilot.press("enter")
                await self.until(pilot, lambda: not app.is_running, timeout=8)
            self.assertEqual(app.return_value.model, "gemini-small")
        run(go())

    def test_the_selector_offers_the_models_of_the_chosen_cli(self):
        async def go():
            from textual.widgets import Select
            app = self.app("alpha")
            async with app.run_test(size=(140, 40)) as pilot:
                await self.selecting(pilot)
                values = [v for _label, v in app.query_one("#model", Select)._options if v is not Select.BLANK]
                self.assertEqual(values, ["default", "gemini-big", "gemini-small"])
                app.query_one("#model", Select).value = "gemini-small"
                await pilot.pause(0.2)
                self.assertEqual(app.model, "gemini-small")
                app.query_one("#cli", Select).value = "claude"
                await pilot.pause(0.3)
                values = [v for _label, v in app.query_one("#model", Select)._options if v is not Select.BLANK]
                self.assertIn("claude-big", values)
                self.assertNotIn("gemini-big", values)
                self.assertEqual(app.model, "default")
        run(go())

    def test_steps_one_and_two_run_by_themselves_then_selection_opens(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                self.assertEqual([app.steps[i].status for i in (1, 2, 3, 4)], ["done", "done", "running", "pending"])
                self.assertIn("1 updated", app.steps[1].detail)
                self.assertTrue(app.query_one("#lists").display)
                self.assertFalse(app.query_one("#log").display)
        run(go())

    def test_you_can_type_the_prompt_while_the_graphs_are_still_building(self):
        async def go():
            gate = threading.Event()
            app = self.app(backend=Fake(gate=gate))
            async with app.run_test(size=(130, 40)) as pilot:
                await self.until(pilot, lambda: app.steps[2].status == "running")
                app.query_one("#prompt", TextArea).text = "fix beta"
                await pilot.pause(0.3)
                self.assertEqual(app.phase, "fleet")
                self.assertFalse(app.query_one("#lists").display)
                gate.set()
                await self.selecting(pilot)
                self.assertEqual(self.selected(app, "repos"), ["beta"])
        run(go())

    def test_validating_early_is_queued_and_continues_by_itself(self):
        async def go():
            gate = threading.Event()
            backend = Fake(gate=gate)
            app = self.app("alpha", backend)
            async with app.run_test(size=(130, 40)) as pilot:
                await self.until(pilot, lambda: app.steps[2].status == "running")
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertTrue(app.queued)
                self.assertIn("queued", app.steps[3].detail)
                self.assertEqual(backend.map_calls, [])
                gate.set()
                await self.until(pilot, lambda: not app.is_running, timeout=8)
            self.assertEqual(app.return_value.repos, ["alpha"])
            self.assertEqual(backend.map_calls, [("alpha", ["alpha"])])
        run(go())

    def test_enter_runs_step_four_then_returns_choice_and_recap(self):
        async def go():
            backend = Fake()
            app = self.app("beta pdf", backend)
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                await pilot.press("enter")
                await self.until(pilot, lambda: not app.is_running, timeout=8)
            res = app.return_value
            self.assertEqual((res.cli, res.prompt, res.repos, res.skills), ("gemini", "beta pdf", ["beta"], ["pdf"]))
            self.assertIn("core", res.mcp)
            self.assertEqual(backend.map_calls, [("beta pdf", ["beta"])])
            self.assertEqual(len(res.recap), 4)
            self.assertTrue(res.recap[0].startswith("✔ 1"))
            self.assertIn("1 of 3 repos", res.recap[2])
            self.assertIn("starting point", res.recap[3])
        run(go())

    def test_escape_cancels_and_stops_the_background_steps(self):
        async def go():
            gate = threading.Event()
            backend = Fake(gate=gate)
            app = self.app("alpha", backend)
            async with app.run_test(size=(130, 40)) as pilot:
                await self.until(pilot, lambda: app.steps[2].status == "running")
                await pilot.press("escape")
                gate.set()
            self.assertIsNone(app.return_value)
            for _ in range(40):          # the background thread reads the flag a moment after the screen closed
                if backend.stop_seen:
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(backend.stop_seen)
        run(go())

    def test_an_error_in_step_one_does_not_block_the_rest(self):
        async def go():
            app = self.app(backend=Fake(boom=True))
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                self.assertEqual(app.steps[1].status, "error")
                self.assertIn("git exploded", app.steps[1].detail)
                self.assertEqual(app.phase, "select")
        run(go())

    def test_lists_follow_the_prompt_as_you_type(self):
        async def go():
            app = self.app()
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                self.assertEqual(self.selected(app, "repos"), [])
                app.query_one("#prompt", TextArea).text = "fix beta with bigquery"
                await pilot.pause(0.6)
                await app.workers.wait_for_complete()
                await pilot.pause(0.1)
                self.assertEqual(self.selected(app, "repos"), ["beta"])
                self.assertEqual(self.selected(app, "mcp"), ["bq", "core"])
        run(go())

    def test_a_slow_old_computation_cannot_overwrite_the_latest_prompt(self):
        import time

        class Slow(Fake):
            def plan(self, cli, prompt):
                if prompt == "beta slow":
                    time.sleep(0.8)
                return fake_plan(cli, prompt)

        async def go():
            app = BlindApp(Slow(), cli="gemini", clis=["gemini", "claude"], prompt="") if False else self.app()
            app.backend = Slow()
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                app.query_one("#prompt", TextArea).text = "beta slow"
                await pilot.pause(0.5)            # the slow plan for "beta slow" is now running
                app.query_one("#prompt", TextArea).text = "gamma"
                await pilot.pause(1.6)
                await app.workers.wait_for_complete()
                await pilot.pause(0.1)
                self.assertEqual(self.selected(app, "repos"), ["gamma"])
        run(go())

    def test_manual_choice_survives_a_new_prompt(self):
        async def go():
            app = self.app("alpha")
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                app.query_one("#repos", SelectionList).toggle("gamma")
                app.query_one("#repos", SelectionList).toggle("alpha")
                await pilot.pause(0.1)
                app.query_one("#prompt", TextArea).text = "alpha again"
                await pilot.pause(0.6)
                await app.workers.wait_for_complete()
                await pilot.pause(0.1)
                self.assertEqual(self.selected(app, "repos"), ["gamma"])
        run(go())

    def test_a_pasted_multi_line_paragraph_is_kept_whole(self):
        para = "First line about alpha.\n\nSecond paragraph with details,\n  - bullet one\n  - bullet two\nEnd."
        async def go():
            app = self.app()
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                app.post_message(events.Paste(para))   # as a terminal paste arrives: the app hands it to the focused widget
                await pilot.pause(0.6)
                await app.workers.wait_for_complete()
                self.assertEqual(app.query_one("#prompt", TextArea).text, para)
                self.assertEqual(self.selected(app, "repos"), ["alpha"])
                await pilot.press("ctrl+l")
                await self.until(pilot, lambda: not app.is_running, timeout=8)
            self.assertEqual(app.return_value.prompt, para.strip())
        run(go())

    def test_enter_adds_a_line_once_the_prompt_has_several_lines(self):
        async def go():
            app = self.app("first line\nsecond line")
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                app.query_one("#prompt", TextArea).move_cursor((1, 11))
                await pilot.press("enter")
                await pilot.pause(0.1)
                self.assertEqual(app.phase, "select")
                self.assertEqual(app.query_one("#prompt", TextArea).text, "first line\nsecond line\n")
                await pilot.press("ctrl+l")
                await self.until(pilot, lambda: not app.is_running, timeout=8)
            self.assertEqual(app.return_value.prompt, "first line\nsecond line")
        run(go())

    def test_a_very_long_prompt_is_returned_intact(self):
        long = ("Explain how alpha handles the discount. " * 2500).strip()   # about 100 KB
        async def go():
            app = self.app()
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                app.query_one("#prompt", TextArea).text = long
                await pilot.press("ctrl+l")
                await self.until(pilot, lambda: not app.is_running, timeout=10)
            self.assertEqual(app.return_value.prompt, long)
            self.assertEqual(app.return_value.repos, ["alpha"])
        run(go())

    def test_always_kept_servers_cannot_be_unticked(self):
        async def go():
            app = self.app("alpha")
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                app.query_one("#mcp", SelectionList).toggle("core")
                await pilot.pause(0.2)
                self.assertIn("core", self.selected(app, "mcp"))
        run(go())

    def test_space_toggles_the_highlighted_box(self):
        async def go():
            app = self.app("")
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                app.query_one("#repos", SelectionList).focus()
                await pilot.press("space")
                await pilot.pause(0.1)
                self.assertEqual(self.selected(app, "repos"), ["alpha"])
                await pilot.press("ctrl+l")
                await self.until(pilot, lambda: not app.is_running, timeout=8)
            self.assertEqual(app.return_value.repos, ["alpha"])
        run(go())

    def test_switching_cli_recomputes_the_server_list(self):
        async def go():
            app = self.app("github")
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                self.assertIn("gemini-github", [str(o.value) for o in app.query_one("#mcp", SelectionList).options])
                app.query_one("#cli").value = "claude"
                await pilot.pause(0.5)
                await app.workers.wait_for_complete()
                await pilot.pause(0.1)
                names = [str(o.value) for o in app.query_one("#mcp", SelectionList).options]
                self.assertIn("claude-github", names)
                self.assertNotIn("gemini-github", names)
                await pilot.press("ctrl+l")
                await self.until(pilot, lambda: not app.is_running, timeout=8)
            self.assertEqual(app.return_value.cli, "claude")
        run(go())

    def test_info_and_hints_are_shown_during_selection(self):
        async def go():
            backend = Fake(hints=lambda cli, p, names: [f"{names[0]}: starting points", "  src/a.py"])
            app = self.app("alpha", backend)
            async with app.run_test(size=(130, 40)) as pilot:
                await self.selecting(pilot)
                await pilot.pause(0.3)
                info = app.query_one("#info")
                self.assertTrue(info.display)
                text = str(info.render())
                self.assertIn("note: demo", text)
                self.assertIn("src/a.py", text)
        run(go())

    def test_step_lines_are_written_to_the_log(self):
        async def go():
            gate = threading.Event()
            app = self.app(backend=Fake(gate=gate))
            async with app.run_test(size=(130, 40)) as pilot:
                await self.until(pilot, lambda: app.steps[2].status == "running")
                text = "\n".join(str(line.text) for line in app.query_one("#log").lines)
                self.assertIn("alpha: pulled 2 commit(s) on main", text)
                gate.set()
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
