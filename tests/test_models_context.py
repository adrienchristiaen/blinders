import json
import os
import unittest
from pathlib import Path
from unittest import mock

from blinders import cli
from blinders.models import choose_model, classify
from blinders.scan import describe_repo
from blinders.skills import discover, select_skills

from helpers import Sandbox, make_repo, session_of
from test_start import run_cli, write_skills


class ClassifyTests(unittest.TestCase):
    def test_short_question_on_one_repo_is_light(self):
        self.assertEqual(classify("où est défini le client Kafka ?", 1)[0], "light")
        self.assertEqual(classify("explain what the retry decorator does", 1)[0], "light")

    def test_edits_are_never_light(self):
        self.assertEqual(classify("explique et corrige le client Kafka", 1)[0], "standard")
        self.assertEqual(classify("add a retry to the client", 1)[0], "standard")

    def test_design_across_repos_is_strong(self):
        self.assertEqual(classify("refactor the architecture of the ingestion", 3)[0], "strong")
        self.assertEqual(classify("trouve la cause racine du bug, bout en bout", 1)[0], "strong")

    def test_one_strong_word_alone_stays_standard(self):
        self.assertEqual(classify("migrate the config file", 1)[0], "standard")

    def test_long_prompt_across_repos_is_strong(self):
        self.assertEqual(classify("x " * 700, 3, 1)[0], "strong")

    def test_many_opened_repos_alone_stay_standard(self):
        self.assertEqual(classify("regarde ce repo", 3)[0], "standard")


class ChooseModelTests(Sandbox):
    def test_standard_passes_no_name(self):
        c = choose_model("gemini", "ajoute un test", 1, 0, self.cfg)
        self.assertEqual((c.tier, c.model), ("standard", None))

    def test_only_strong_has_a_default_light_needs_your_choice(self):
        light = choose_model("gemini", "où est le client ?", 1, 0, self.cfg)
        self.assertEqual((light.tier, light.model), ("light", None))
        self.assertIn("no light model set in [models.gemini]", light.reason)
        self.assertIsNone(choose_model("claude", "où est le client ?", 1, 0, self.cfg).model)
        self.cfg.models = {"gemini": {"light": "flash-lite"}}
        self.assertEqual(choose_model("gemini", "où est le client ?", 1, 0, self.cfg).model, "flash-lite")
        self.assertEqual(choose_model("gemini", "refactor architecture", 1, 0, self.cfg).model, "pro")

    def test_config_overrides_and_unknown_cli(self):
        self.cfg.models = {"gemini": {"light": "gemini-9-lite"}}
        self.assertEqual(choose_model("gemini", "où est le client ?", 1, 0, self.cfg).model, "gemini-9-lite")
        self.assertIsNone(choose_model("codex", "où est le client ?", 1, 0, self.cfg).model)

    def test_specs(self):
        self.assertIsNone(choose_model("gemini", "où est le client ?", 1, 0, self.cfg, "default"))
        self.assertEqual(choose_model("gemini", "x", 0, 0, self.cfg, "strong").model, "pro")
        self.assertEqual(choose_model("gemini", "x", 0, 0, self.cfg, "my-model").model, "my-model")
        self.cfg.models_auto = False
        self.assertIsNone(choose_model("gemini", "où est le client ?", 1, 0, self.cfg))

    def test_no_prompt_no_choice(self):
        self.assertIsNone(choose_model("gemini", "", 0, 0, self.cfg))


class LaunchTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.sales_repos()
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n')

    def test_gemini_workspace_stops_the_upward_search_and_asks_for_a_light_model(self):
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n[models.gemini]\nlight = "flash-lite"\n')
        code, out, err = run_cli("run", "gemini", "--dry-run", "--mcp", "none", "où est la classe principale de sales-api-java ?")
        self.assertEqual(code, 0)
        self.assertIn("-m flash-lite", out)
        self.assertIn("model flash-lite (light)", err)
        conf = json.loads((session_of(out) / ".gemini" / "settings.json").read_text())
        self.assertEqual(conf["context"]["memoryBoundaryMarkers"], [])

    def test_boundary_is_set_even_when_no_skill_is_hidden(self):
        _, out, _ = run_cli("run", "gemini", "--dry-run", "--skills", "all", "hello")
        conf = json.loads((session_of(out) / ".gemini" / "settings.json").read_text())
        self.assertEqual(conf["context"], {"memoryBoundaryMarkers": []})
        self.assertNotIn("skills", conf)

    def test_model_default_and_explicit_model_after_dashes_win(self):
        _, out, _ = run_cli("run", "gemini", "--dry-run", "--model", "default", "où est la classe principale ?")
        self.assertNotIn(" -m ", out)
        _, out, _ = run_cli("run", "gemini", "--dry-run", "où est la classe principale ?", "--", "-m", "pro")
        self.assertEqual(out.count(" -m "), 1)
        self.assertIn("-m pro", out)

    def test_claude_gets_its_own_flag_and_vibe_none(self):
        _, out, _ = run_cli("run", "claude", "--dry-run", "--model", "strong", "hello")
        self.assertIn("--model opus", out)
        _, out, _ = run_cli("run", "vibe", "--dry-run", "--model", "strong", "hello")
        self.assertNotIn("opus", out)

    def test_trust_env_is_set_for_gemini_only(self):
        lp = cli.make_plan(self.cfg, cli.get_adapter("gemini", self.cfg), "hello", [], mock.Mock(
            repos=None, related="auto", mcp="none", skills="all", primary=False, link=False, no_sync=True, sync=None,
            no_hints=True, model=None), [])
        os.environ.pop("GEMINI_CLI_TRUST_WORKSPACE", None)
        with mock.patch("os.execvp") as ex, mock.patch("os.chdir"), mock.patch("shutil.which", return_value="/bin/x"), \
                mock.patch.object(cli, "_record"):
            cli.launch(self.cfg, lp, False)
        self.assertEqual(os.environ.get("GEMINI_CLI_TRUST_WORKSPACE"), "true")
        os.environ.pop("GEMINI_CLI_TRUST_WORKSPACE", None)
        self.cfg.gemini_trust_workspace = False
        with mock.patch("os.execvp"), mock.patch("os.chdir"), mock.patch("shutil.which", return_value="/bin/x"), \
                mock.patch.object(cli, "_record"):
            cli.launch(self.cfg, lp, False)
        self.assertNotIn("GEMINI_CLI_TRUST_WORKSPACE", os.environ)


class RepoAwareSkillTests(Sandbox):
    def setUp(self):
        super().setUp()
        write_skills(self.home / ".claude" / "skills", {
            "dbt-modeler": "Write and review dbt models, tests and sources for the warehouse.",
            "helm-charts": "Edit Helm charts and values for Kubernetes releases.",
            "pdf-tools": "Extract text and tables from PDF documents.",
            "slides-builder": "Build presentation decks and slides.",
            "jira-tickets": "Create Jira tickets, sprints and boards.",
        })
        make_repo(self.work, "warehouse-etl", "Pipelines for the warehouse.", files=("dbt_project.yml",), dirs=("models",))
        make_repo(self.work, "frontend", "A small React storefront.", files=("package.json",))
        self.skills = discover("claude", self.home, self.cfg)

    def repo(self, name):
        return describe_repo(self.work / name, self.cfg.map_globs)

    def names(self, plan):
        return [s.name for s in plan.kept]

    def test_stack_of_the_opened_repo_keeps_its_skill_without_the_prompt_saying_so(self):
        plan = select_skills("regarde ce repo", self.skills, self.cfg, repos=[self.repo("warehouse-etl")])
        self.assertEqual(self.names(plan), ["dbt-modeler"])
        self.assertIn("warehouse-etl", plan.reasons["dbt-modeler"])

    def test_other_repo_other_skills(self):
        plan = select_skills("regarde ce repo", self.skills, self.cfg, repos=[self.repo("frontend")])
        self.assertEqual(self.names(plan), [])

    def test_without_repos_nothing_changes(self):
        self.assertEqual(self.names(select_skills("regarde ce repo", self.skills, self.cfg)), [])

    def test_agents_md_vocabulary_counts(self):
        repo = make_repo(self.work, "platform", "Platform repo.")
        (repo / "GEMINI.md").write_text("We ship with helm and kubernetes releases, edit charts carefully.")
        plan = select_skills("regarde ce repo", self.skills, self.cfg, repos=[self.repo("platform")])
        self.assertEqual(self.names(plan), ["helm-charts"])

    def test_prompt_and_repo_reasons_combine(self):
        plan = select_skills("update the dbt models", self.skills, self.cfg, repos=[self.repo("warehouse-etl")])
        self.assertIn("prompt mentions", plan.reasons["dbt-modeler"])


if __name__ == "__main__":
    unittest.main()
