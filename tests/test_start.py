import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from blinders import cli
from blinders.skills import claude_settings, discover, gemini_settings, select_skills

from helpers import Sandbox


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


SKILLS = {
    "bigquery-lineage": "Trace table lineage and dataset dependencies in BigQuery warehouses.",
    "jira-tickets": "Create and update Jira tickets, sprints and boards.",
    "pdf-tools": "Extract text and tables from PDF documents and fill PDF forms.",
    "slides-builder": "Build presentation decks and slides from an outline.",
    "terraform-review": "Review Terraform modules, plans and cloud infrastructure changes.",
}


def write_skills(root: Path, skills=SKILLS) -> None:
    for name, desc in skills.items():
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody\n")


class SkillTests(Sandbox):
    def setUp(self):
        super().setUp()
        write_skills(self.home / ".claude" / "skills")
        write_skills(self.home / ".gemini" / "skills", {"gemini-only": "Only for Gemini users."})
        self.sales_repos()
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n')

    def names(self, plan):
        return [s.name for s in plan.kept]

    def test_discover_per_family(self):
        self.assertEqual(len(discover("claude", self.home, self.cfg)), 5)
        self.assertEqual([s.name for s in discover("gemini", self.home, self.cfg)], ["gemini-only"])
        self.assertEqual(discover("other", self.home, self.cfg), [])

    def test_select_by_name_and_description(self):
        s = discover("claude", self.home, self.cfg)
        self.assertEqual(self.names(select_skills("trace the lineage of this table in bigquery", s, self.cfg)), ["bigquery-lineage"])
        self.assertEqual(self.names(select_skills("update the jira board for this sprint", s, self.cfg)), ["jira-tickets"])
        self.assertEqual(self.names(select_skills("rename this variable", s, self.cfg)), [])

    def test_one_shared_description_word_is_not_enough(self):
        s = discover("claude", self.home, self.cfg)
        self.assertEqual(self.names(select_skills("extract the names", s, self.cfg)), [])

    def test_always_keywords_specs_and_cap(self):
        s = discover("claude", self.home, self.cfg)
        self.cfg.skills_always = ["pdf-tools"]
        self.assertEqual(self.names(select_skills("x", s, self.cfg)), ["pdf-tools"])
        self.cfg.skills_keywords = {"slides-builder": ["keynote"]}
        s = discover("claude", self.home, self.cfg)
        self.assertIn("slides-builder", self.names(select_skills("make a keynote deck", s, self.cfg)))
        self.assertEqual(len(select_skills("x", s, self.cfg, "all").kept), 5)
        self.assertEqual(self.names(select_skills("x", s, self.cfg, "jira-tickets")), ["jira-tickets", "pdf-tools"])
        self.cfg.max_skills = 1
        plan = select_skills("jira tickets and terraform plans for infrastructure", s, self.cfg, "auto")
        self.assertEqual(len(plan.kept), 2)  # pdf-tools (always) + one match

    def test_settings_payloads(self):
        s = discover("claude", self.home, self.cfg)
        plan = select_skills("update the jira tickets", s, self.cfg)
        self.assertEqual(claude_settings(plan)["skillOverrides"]["pdf-tools"], "user-invocable-only")
        self.assertNotIn("jira-tickets", claude_settings(plan)["skillOverrides"])
        self.assertIn("pdf-tools", gemini_settings(plan)["skills"]["disabled"])

    def test_run_claude_hides_skills_through_settings_file(self):
        code, out, err = run_cli("run", "claude", "--dry-run", "update", "the", "jira", "tickets")
        self.assertEqual(code, 0)
        self.assertIn("--settings", out)
        self.assertIn("skills kept: 1 (hidden 4)", err)
        session = Path(out.split("&&")[0].replace("cd", "").strip().strip("'"))
        conf = json.loads((session / "skills-settings.json").read_text())
        self.assertEqual(sorted(conf["skillOverrides"]), ["bigquery-lineage", "pdf-tools", "slides-builder", "terraform-review"])
        self.assertIn("## Skills hidden in this session", (session / "CLAUDE.md").read_text())

    def test_run_gemini_disables_skills_in_workspace_settings(self):
        code, out, _ = run_cli("run", "gemini", "--dry-run", "--mcp", "none", "hello")
        session = Path(out.split("&&")[0].replace("cd", "").strip().strip("'"))
        conf = json.loads((session / ".gemini" / "settings.json").read_text())
        self.assertEqual(conf["skills"]["disabled"], ["gemini-only"])

    def test_skills_all_and_unknown_and_unsupported(self):
        _, out, _ = run_cli("run", "claude", "--dry-run", "--skills", "all", "hi")
        self.assertNotIn("--settings", out)
        code, _, err = run_cli("run", "claude", "--dry-run", "--skills", "nope", "hi")
        self.assertEqual(code, 2)
        self.assertIn("unknown skill", err)
        code, _, err = run_cli("run", "codex", "--dry-run", "--skills", "none", "hi")
        self.assertEqual(code, 0)
        self.assertIn("--skills ignored", err)


class StartTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.sales_repos()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in ("gemini", "claude"):
            (self.bin / name).write_text("#!/bin/sh\n")
            (self.bin / name).chmod(0o755)
        os.environ["PATH"] = f"{self.bin}:/usr/bin:/bin"
        self.answers: list[str] = []
        self.asked: list[str] = []
        self.exec = mock.Mock()
        patches = [
            mock.patch.object(cli, "_interactive", return_value=True),
            mock.patch.object(cli, "_ask", side_effect=self._answer),
            mock.patch.object(cli.os, "execvp", self.exec),
            mock.patch.object(cli.os, "chdir"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _answer(self, question):
        self.asked.append(question)
        return self.answers.pop(0)

    def configure(self, extra=""):
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\ndefault_cli = "gemini"\n{extra}')

    def test_first_run_wizard_writes_config_indexes_then_launches(self):
        self.answers = [str(self.work), "2", "fix the bigquery load in warehouse-etl", ""]
        code, _, err = run_cli()
        self.assertEqual(code, 0)
        self.assertIn("indexed 4 repos", err)
        cfg_text = (Path(os.environ["BLINDERS_CONFIG_DIR"]) / "config.toml").read_text()
        self.assertIn(str(self.work), cfg_text)
        argv = self.exec.call_args[0][1]
        self.assertEqual(argv[0], "gemini")
        self.assertIn("warehouse-etl", " ".join(argv))

    def test_wizard_keeps_existing_tables_and_does_not_duplicate_roots(self):
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True)
        (d / "config.toml").write_text('roots = ["/old"]\n[mcp]\nalways = ["github"]\n')
        cli._write_roots([str(self.work)])
        text = (d / "config.toml").read_text()
        self.assertEqual(text.count("roots"), 1)
        self.assertIn('always = ["github"]', text)
        self.assertTrue(text.startswith("roots"))

    def test_wizard_offers_graphs_when_graphify_is_installed(self):
        (self.bin / "graphify").write_text(
            '#!/bin/sh\ntarget="$2"; mkdir -p "$target/graphify-out" && echo r > "$target/graphify-out/GRAPH_REPORT.md"\n')
        (self.bin / "graphify").chmod(0o755)
        self.answers = [str(self.work), "", "", ""]  # roots, graphs (default yes), prompt, confirm
        run_cli("gemini")
        self.assertTrue(any("Graphify code graphs for 4" in q for q in self.asked))
        self.assertTrue((self.work / "sales-api" / "graphify-out" / "GRAPH_REPORT.md").is_file())

    def test_wizard_graphs_can_be_declined(self):
        (self.bin / "graphify").write_text("#!/bin/sh\nexit 1\n")
        (self.bin / "graphify").chmod(0o755)
        self.answers = [str(self.work), "n", "", ""]
        _, _, err = run_cli("gemini")
        self.assertIn("skipped", err)

    def test_prompt_is_asked_plan_shown_and_enter_launches(self):
        self.configure()
        self.answers = ["comment sales-api-java est déployé sur kubernetes", ""]
        code, _, err = run_cli()
        self.assertEqual(code, 0)
        self.assertIn("repos opened    sales-api-java, platform-k8s", err)
        self.assertIn("Enter = launch", self.asked[-1])
        self.exec.assert_called_once()

    def test_adjust_with_plus_and_minus_then_launch(self):
        self.configure()
        self.answers = ["lineage de sales-api-java", "+warehouse-etl", "-sales-api-java", ""]
        run_cli()
        argv = self.exec.call_args[0][1]
        joined = " ".join(argv)
        self.assertIn("warehouse-etl", joined)
        self.assertNotIn("sales-api-java", joined.split("--include-directories")[1].split(" ")[1])

    def test_unknown_repo_in_adjustment_is_refused_and_asked_again(self):
        self.configure()
        self.answers = ["lineage de sales-api-java", "+nope", "q"]
        code, _, err = run_cli()
        self.assertEqual(code, 1)
        self.assertIn("unknown repo(s): nope", err)
        self.exec.assert_not_called()

    def test_cancel_does_not_launch(self):
        self.configure()
        self.answers = ["lineage de sales-api-java", "q"]
        code, _, err = run_cli()
        self.assertEqual(code, 1)
        self.assertIn("cancelled", err)
        self.exec.assert_not_called()

    def test_prompt_on_command_line_launches_without_confirmation(self):
        self.configure()
        code, _, _ = run_cli("gemini", "lineage", "de", "sales-api-java")
        self.assertEqual(code, 0)
        self.assertEqual(self.asked, [])
        self.exec.assert_called_once()

    def test_confirm_flag_asks_even_with_a_prompt(self):
        self.configure()
        self.answers = [""]
        run_cli("claude", "--confirm", "lineage", "de", "sales-api-java")
        self.assertEqual(len(self.asked), 1)

    def test_cli_choice_when_several_installed_and_no_default(self):
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n')
        self.answers = ["1", "", ""]
        run_cli()
        self.assertEqual(self.exec.call_args[0][1][0], "claude")

    def test_empty_prompt_is_a_fully_blind_session(self):
        self.configure()
        self.answers = ["", ""]
        _, _, err = run_cli()
        self.assertIn("none (fully blind)", err)

    def test_no_tty_without_config_is_an_error(self):
        with mock.patch.object(cli, "_interactive", return_value=False):
            code, _, err = run_cli("gemini", "hello")
        self.assertEqual(code, 2)
        self.assertIn("no roots configured", err)

    def test_bare_blind_without_tty_prints_help(self):
        with mock.patch.object(cli, "_interactive", return_value=False):
            code, _, err = run_cli()
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)

    def test_setup_command_reruns_the_wizard(self):
        self.configure()
        self.answers = [str(self.work)]
        code, _, err = run_cli("setup")
        self.assertEqual(code, 0)
        self.assertIn("indexed 4 repos", err)


if __name__ == "__main__":
    unittest.main()
