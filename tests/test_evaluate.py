import json
import unittest

from blinders.evaluate import bench, load_cases, render, run_cases
from blinders.scan import build_index

from helpers import Sandbox


class EvaluateTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.sales_repos()
        self.repos = build_index(self.cfg)

    def cases_file(self, text):
        f = self.tmp / "cases.toml"
        f.write_text(text, encoding="utf-8")
        return f

    def test_cases_are_read_from_toml(self):
        cases = load_cases(self.cases_file(
            '[[case]]\nprompt = "lineage de sales-api-java"\nexpect = ["sales-api-java"]\navoid = ["warehouse-etl"]\n'))
        self.assertEqual(cases[0].prompt, "lineage de sales-api-java")
        self.assertEqual(cases[0].expect, ["sales-api-java"])
        self.assertEqual(cases[0].avoid, ["warehouse-etl"])

    def test_a_case_without_prompt_or_expect_is_rejected(self):
        with self.assertRaises(ValueError):
            load_cases(self.cases_file('[[case]]\nprompt = "x"\n'))

    def test_pass_and_fail_are_told_apart(self):
        cases = load_cases(self.cases_file(
            '[[case]]\nprompt = "lineage de sales-api-java"\nexpect = ["sales-api-java"]\n'
            '[[case]]\nprompt = "lineage de sales-api-java"\nexpect = ["warehouse-etl"]\n'
            '[[case]]\nprompt = "lineage de sales-api-java"\nexpect = ["sales-api-java"]\navoid = ["sales-api-java"]\n'))
        results = run_cases(cases, self.repos, self.cfg)
        self.assertEqual([r.passed for r in results], [True, False, False])
        self.assertEqual(results[1].missing, ["warehouse-etl"])
        self.assertEqual(results[2].forbidden, ["sales-api-java"])

    def test_recall_and_precision(self):
        cases = load_cases(self.cases_file(
            '[[case]]\nprompt = "lineage de sales-api-java"\nexpect = ["sales-api-java", "warehouse-etl"]\n'))
        (r,) = run_cases(cases, self.repos, self.cfg)
        self.assertEqual(r.recall, 0.5)
        self.assertEqual(r.precision, 1.0)

    def test_render_has_a_summary_and_json_is_valid(self):
        cases = load_cases(self.cases_file('[[case]]\nprompt = "lineage de sales-api-java"\nexpect = ["sales-api-java"]\n'))
        results = run_cases(cases, self.repos, self.cfg)
        self.assertIn("1/1 cases pass", render(results))
        self.assertEqual(json.loads(render(results, as_json=True))["cases"][0]["passed"], True)

    def test_bench_times_each_stage(self):
        labels = [label for label, _ in bench("lineage de sales-api-java", self.repos, self.cfg)]
        self.assertEqual(labels, ["instant pass (index only)", "identifiers in file contents",
                                  "full selection (first run)", "full selection (answers cached)"])


class EvaluateCommandTests(Sandbox):
    def run_cli(self, *argv):
        import contextlib
        import io
        from blinders import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        self.sales_repos()
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "config.toml").write_text(f'roots = ["{self.work}"]\n')
        self.cases = self.tmp / "cases.toml"

    def test_eval_exit_code_follows_the_cases(self):
        self.cases.write_text('[[case]]\nprompt = "lineage de sales-api-java"\nexpect = ["sales-api-java"]\n')
        code, out = self.run_cli("eval", str(self.cases))
        self.assertEqual(code, 0)
        self.assertIn("1/1 cases pass", out)
        self.cases.write_text('[[case]]\nprompt = "lineage de sales-api-java"\nexpect = ["warehouse-etl"]\n')
        self.assertEqual(self.run_cli("eval", str(self.cases))[0], 1)

    def test_bench_prints_four_stages(self):
        code, out = self.run_cli("bench", "lineage de sales-api-java")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 4)


if __name__ == "__main__":
    unittest.main()
