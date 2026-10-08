import time
import unittest
from pathlib import Path

from blinders.scan import build_index, find_repos, load_index
from blinders.select import select
from blinders.text import squash, tokens

from helpers import Sandbox, make_repo


class TextTests(unittest.TestCase):
    def test_tokens_split_camel_snake_and_fold_accents(self):
        self.assertEqual(tokens("BigQueryLoader_v2 réseau"), ["big", "query", "loader", "bigqueryloader", "reseau"])
        self.assertIn("bigquery", tokens("BigQuery"))
        self.assertIn("query", tokens("BigQuery"))

    def test_squash(self):
        self.assertEqual(squash("Loop-Dex!"), "loopdex")


class ScanTests(Sandbox):
    def test_finds_repos_and_does_not_descend_into_them(self):
        self.standard_repos()
        make_repo(self.work / "repos" / "nested", "deep", "deep repo")
        (self.work / "carrefour-pipelines" / "inner" / ".git").mkdir(parents=True)
        found = {p.name for p in find_repos([self.work], depth=3)}
        self.assertEqual(found, {"carrefour-pipelines", "LoopDex", "coolpot", "jira-cli", "deep"})

    def test_skips_node_modules_and_hidden(self):
        make_repo(self.work / "node_modules", "pkg", "x")
        make_repo(self.work / ".hidden", "secret", "x")
        self.assertEqual(find_repos([self.work], depth=3), [])

    def test_index_describes_without_reading_code(self):
        self.standard_repos()
        repos = {r.name: r for r in build_index(self.cfg)}
        cp = repos["carrefour-pipelines"]
        self.assertIn("dbt", cp.terms)   # from the file name dbt_project.yml, no marker table
        self.assertTrue(cp.description.startswith("Airflow DAGs"))
        self.assertIn("bigquery", cp.terms)

    def test_map_files_are_indexed(self):
        repo = make_repo(self.work, "mystery", "Nothing here.")
        (repo / "graphify-out").mkdir()
        (repo / "graphify-out" / "GRAPH_REPORT.md").write_text("god node: SchedulerCore handles cron", encoding="utf-8")
        repos = {r.name: r for r in build_index(self.cfg)}
        self.assertIn("scheduler", repos["mystery"].terms)

    def test_index_cached_and_refreshed(self):
        self.standard_repos()
        self.assertEqual(len(load_index(self.cfg)), 4)
        make_repo(self.work, "late", "added after indexing")
        self.assertEqual(len(load_index(self.cfg)), 4)  # served from cache
        self.assertEqual(len(load_index(self.cfg, refresh=True)), 5)


class PathVocabularyTests(Sandbox):
    """A repo with no README (only SQL and YAML) must be found by the names it contains."""

    def dbt_repo(self, name="bi-dbt"):
        repo = make_repo(self.work, name, "", files=("dbt_project.yml",), dirs=("models", "macros"))
        (repo / "README.md").unlink()
        (repo / "models/marts/sales").mkdir(parents=True)
        (repo / "dbt_project.yml").write_text("name: bi_warehouse\n")
        (repo / "models/marts/sales/fct_orders.sql").write_text("select 1")
        (repo / "models/marts/sales/schema.yml").write_text(
            "sources:\n  - name: erp\n    tables:\n      - name: raw_shipments\nmodels:\n  - name: dim_customers\n")
        return repo

    def test_file_names_and_declared_dbt_names_become_terms(self):
        from blinders.scan import describe_repo
        terms = describe_repo(self.dbt_repo(), []).terms
        for word in ("orders", "fct", "shipments", "customers", "erp", "warehouse", "sales"):
            self.assertIn(word, terms, word)

    def test_prompt_naming_a_table_finds_the_dbt_repo(self):
        self.dbt_repo()
        make_repo(self.work, "frontend", "A small React storefront.", files=("package.json",))
        repos = build_index(self.cfg)
        chosen = select("quel est le lineage de raw_shipments ?", repos, self.cfg)
        self.assertEqual([c.repo.name for c in chosen], ["bi-dbt"])

    def test_walk_skips_hidden_and_vendored_folders(self):
        from blinders.scan import path_terms
        repo = self.dbt_repo()
        (repo / "node_modules" / "leftpad").mkdir(parents=True)
        (repo / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
        terms = path_terms(repo)
        self.assertNotIn("leftpad", terms)
        self.assertNotIn("hooks", terms)


class SelectTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.standard_repos()
        self.repos = build_index(self.cfg)

    def names(self, prompt):
        return [c.repo.name for c in select(prompt, self.repos, self.cfg)]

    def test_content_match(self):
        self.assertEqual(self.names("fix the airflow dag that loads bigquery from kafka")[0], "carrefour-pipelines")
        self.assertEqual(self.names("simulation thermique du module PCM")[0], "coolpot")

    def test_name_in_prompt_wins(self):
        self.assertEqual(self.names("update loopdex homepage")[0], "LoopDex")
        self.assertEqual(self.names("bug in jira cli release step")[0], "jira-cli")

    def test_name_contained_in_longer_repo_name_is_not_opened(self):
        make_repo(self.work, "sales-api", "Python sales API gateway.")
        make_repo(self.work, "sales-api-java", "Spring Boot sales API.")
        self.repos = build_index(self.cfg)
        self.assertEqual(self.names("tu peux me dire le lineage de sales-api-java"), ["sales-api-java"])
        self.assertEqual(self.names("lineage de sales-api"), ["sales-api"])
        self.assertEqual(set(self.names("compare sales-api and sales-api-java")), {"sales-api", "sales-api-java"})

    def test_no_match_stays_blind(self):
        self.assertEqual(self.names("what is the capital of France"), [])
        self.assertEqual(self.names(""), [])

    def test_max_repos_cap(self):
        self.cfg.max_repos = 1
        self.cfg.relative_threshold = 0.0
        self.assertEqual(len(self.names("loopdex coolpot jira-cli carrefour-pipelines")), 1)

    def test_multi_repo_prompt(self):
        got = self.names("compare loopdex and coolpot")
        self.assertEqual(set(got), {"LoopDex", "coolpot"})

    def test_selection_is_fast_on_many_repos(self):
        for i in range(300):
            make_repo(self.work, f"svc-{i}", f"service number {i} handling topic{i % 17} data pipeline")
        repos = build_index(self.cfg)
        t0 = time.perf_counter()
        for _ in range(20):
            select("fix the topic3 data pipeline", repos, self.cfg)
        per_call_ms = (time.perf_counter() - t0) * 1000 / 20
        self.assertLess(per_call_ms, 50, f"selection too slow: {per_call_ms:.1f} ms")


if __name__ == "__main__":
    unittest.main()
