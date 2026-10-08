"""Nothing here relies on a list of words, file names or stacks: it all comes from the user's own repos."""

import unittest
from pathlib import Path

from blinders.files import iter_files
from blinders.relations import identities
from blinders.scan import build_index
from blinders.select import select
from blinders.text import stems, ubiquitous

from helpers import Sandbox, make_repo


class StemTests(unittest.TestCase):
    def test_inflections_and_translations_of_a_word_meet(self):
        self.assertEqual(set(stems("déployé deployment deploy")), {"deplo"})
        self.assertEqual(stems("tables")[0], stems("table")[0])


class UbiquitousTests(unittest.TestCase):
    def test_terms_found_in_half_the_items_or_more_are_noise(self):
        items = [{"npx", "github"}, {"npx", "jira"}, {"uvx", "bigquery"}, {"npx", "slack"}]
        self.assertEqual(ubiquitous(items, min_items=3), {"npx"})

    def test_too_few_items_to_tell(self):
        self.assertEqual(ubiquitous([{"a"}, {"a"}], min_items=3), set())


class FileWalkTests(Sandbox):
    def test_shallow_files_first_and_limits_respected(self):
        repo = make_repo(self.work, "r", "x")
        (repo / "a" / "b" / "c").mkdir(parents=True)
        (repo / "node_modules").mkdir()
        (repo / "node_modules" / "skipped.txt").write_text("x")
        (repo / "a" / "b" / "c" / "deep.txt").write_text("x")
        (repo / "a" / "mid.txt").write_text("x")
        (repo / "top.txt").write_text("x")
        names = [p.name for p in iter_files(repo, max_depth=3, limit=100)]
        self.assertEqual(names.index("top.txt") < names.index("mid.txt") < names.index("deep.txt"), True)
        self.assertNotIn("skipped.txt", names)
        self.assertNotIn("deep.txt", [p.name for p in iter_files(repo, max_depth=2, limit=100)])
        self.assertEqual(len(list(iter_files(repo, max_depth=3, limit=2))), 2)


class SelectionHasNoStoplistOfItsOwnTests(Sandbox):
    def test_a_word_in_most_repos_does_not_select_anything(self):
        for i in range(6):
            make_repo(self.work, f"team-{i}-thing", "A service for the platform.")
        repos = build_index(self.cfg)
        self.assertEqual(select("service", repos, self.cfg), [])

    def test_a_rare_word_still_does(self):
        for i in range(6):
            make_repo(self.work, f"team-{i}-thing", "A service for the platform.")
        make_repo(self.work, "odd-one", "A service that reconciles ledgers.")
        repos = build_index(self.cfg)
        self.assertEqual([c.repo.name for c in select("reconcile the ledger", repos, self.cfg)], ["odd-one"])

    def test_a_repo_with_only_sql_and_yaml_is_found_by_a_table_it_declares(self):
        repo = make_repo(self.work, "bi-dbt", "", dirs=("models",))
        (repo / "README.md").unlink()
        (repo / "models" / "schema.yml").write_text("models:\n  - name: fct_shipments\n")
        make_repo(self.work, "frontend", "A storefront.")
        repos = build_index(self.cfg)
        self.assertEqual([c.repo.name for c in select("lineage of the shipments table", repos, self.cfg)], ["bi-dbt"])


class NamedRepoAndCompanionsTests(Sandbox):
    """Naming one repo must not blind the selection to the others the rest of the prompt describes."""

    def setUp(self):
        super().setUp()
        app = make_repo(self.work, "orders-app", "Spring service producing events.")
        (app / "pom.xml").write_text("<artifactId>orders-app</artifactId>")
        schemas = make_repo(self.work, "schema-manager", "Registry of event schemas.", dirs=("schemas",))
        (schemas / "schemas" / "order_created.avsc").write_text("{}")
        dbt = make_repo(self.work, "bi-dbt", "", dirs=("models",))
        (dbt / "models" / "fct_orders.sql").write_text("select 1")
        for i in range(3):
            make_repo(self.work, f"misc-{i}", "Unrelated tool.")
        self.repos = build_index(self.cfg)

    def names(self, prompt):
        return [c.repo.name for c in select(prompt, self.repos, self.cfg)]

    def test_the_rest_of_the_prompt_brings_in_the_repo_that_describes_it(self):
        self.assertEqual(self.names("modifier le schema OrderCreated de orders-app pour ajouter un champ"),
                         ["orders-app", "schema-manager"])

    def test_words_that_only_restate_the_named_repo_pull_nothing_else(self):
        self.assertEqual(self.names("explique orders-app"), ["orders-app"])

    def test_a_named_repo_comes_first_and_the_cap_still_applies(self):
        self.cfg.max_repos = 1
        self.assertEqual(self.names("modifier le schema OrderCreated de orders-app"), ["orders-app"])


class LinksNeedNoFileNameTableTests(Sandbox):
    def link_kinds(self, repos, name):
        by_path = {r.path: r.name for r in repos}
        return {(by_path[l["to"]], l["kind"]) for r in repos if r.name == name for l in r.links}

    def test_a_repo_declaring_its_name_in_any_root_file_can_be_referenced(self):
        lib = make_repo(self.work, "checkout", "x")
        (lib / "weird.manifest").write_text('name = "ledger-core"\n')
        user = make_repo(self.work, "consumer", "x")
        (user / "deploy").mkdir()
        (user / "deploy" / "anything.cfg").write_text("image = registry/ledger-core:1\n")
        self.assertIn("ledger-core", identities(lib))
        self.assertIn(("checkout", "refs"), self.link_kinds(build_index(self.cfg), "consumer"))

    def test_the_directory_name_still_works_without_any_declaration(self):
        make_repo(self.work, "billing-engine", "x")
        user = make_repo(self.work, "ops", "x")
        (user / "notes.txt").write_text("restart billing-engine nightly")
        self.assertIn(("billing-engine", "refs"), self.link_kinds(build_index(self.cfg), "ops"))

    def test_deep_files_are_not_read(self):
        make_repo(self.work, "billing-engine", "x")
        user = make_repo(self.work, "ops", "x")
        deep = user / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "Thing.src").write_text("calls billing-engine")
        self.assertEqual(self.link_kinds(build_index(self.cfg), "ops"), set())

    def test_binary_files_are_not_read(self):
        make_repo(self.work, "billing-engine", "x")
        user = make_repo(self.work, "ops", "x")
        (user / "blob.bin").write_bytes(b"\x00\x01billing-engine\x00")
        self.assertEqual(self.link_kinds(build_index(self.cfg), "ops"), set())

    def test_a_repo_everyone_mentions_weighs_less_than_one_mentioned_once(self):
        make_repo(self.work, "common-toolkit", "x")
        make_repo(self.work, "special-service", "x")
        for i in range(5):
            r = make_repo(self.work, f"app-number-{i}", "x")
            (r / "deps.txt").write_text("common-toolkit" + (" special-service" if i == 0 else ""))
        repos = build_index(self.cfg)
        app = next(r for r in repos if r.name == "app-number-0")
        by_path = {r.path: r.name for r in repos}
        weights = {by_path[l["to"]]: l["w"] for l in app.links if l["kind"] == "refs"}
        self.assertGreater(weights["special-service"], weights["common-toolkit"])


if __name__ == "__main__":
    unittest.main()
