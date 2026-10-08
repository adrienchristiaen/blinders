import json
import os
import unittest
from pathlib import Path
from unittest import mock

from blinders import cli
from blinders.models import resolve_model
from blinders.scan import describe_repo
from blinders.skills import discover, select_skills

from helpers import Sandbox, make_repo, session_of
from test_start import run_cli, write_skills


class ResolveModelTests(Sandbox):
    """No word lists: the model is what you set, never guessed from the prompt."""

    def test_nothing_set_means_the_cli_keeps_its_own(self):
        self.assertIsNone(resolve_model("gemini", None, self.cfg))

    def test_config_default_is_used_per_cli(self):
        self.cfg.models = {"gemini": {"default": "flash"}}
        self.assertEqual(resolve_model("gemini", None, self.cfg), "flash")
        self.assertIsNone(resolve_model("claude", None, self.cfg))

    def test_explicit_name_wins_and_default_means_hands_off(self):
        self.cfg.models = {"gemini": {"default": "flash"}}
        self.assertEqual(resolve_model("gemini", "pro", self.cfg), "pro")
        self.assertIsNone(resolve_model("gemini", "default", self.cfg))


class LaunchTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.sales_repos()
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        d.mkdir(parents=True)
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n')

    def test_gemini_workspace_stops_the_upward_search_and_passes_the_configured_model(self):
        d = Path(os.environ["BLINDERS_CONFIG_DIR"])
        (d / "config.toml").write_text(f'roots = ["{self.work}"]\n[models.gemini]\ndefault = "flash-lite"\n')
        code, out, err = run_cli("run", "gemini", "--dry-run", "--mcp", "none", "où est la classe principale de sales-api-java ?")
        self.assertEqual(code, 0)
        self.assertIn("-m flash-lite", out)
        self.assertIn("model flash-lite", err)
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
        _, out, _ = run_cli("run", "claude", "--dry-run", "--model", "opus", "hello")
        self.assertIn("--model opus", out)
        _, out, _ = run_cli("run", "vibe", "--dry-run", "--model", "opus", "hello")
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
