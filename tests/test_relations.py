import json
import unittest

from blinders.relations import identities, normalize
from blinders.scan import build_index, load_index
from blinders.select import intents, plan, select
from blinders.workspace import render_index

from helpers import Sandbox, make_repo


class RelationTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.sales_repos()
        self.repos = {r.name: r for r in build_index(self.cfg)}
        self.path = {n: r.path for n, r in self.repos.items()}

    def links(self, name):
        return {(self.repos_by_path(l["to"]), l["kind"]) for l in self.repos[name].links}

    def repos_by_path(self, path):
        return next(n for n, p in self.path.items() if p == path)

    def test_normalize(self):
        self.assertEqual(normalize("Sales_API.java"), "sales-api-java")

    def test_identities_from_build_files(self):
        self.assertIn("sales-api-java", identities(self.work / "sales-api-java"))
        self.assertNotIn("spring-boot-starter-parent", identities(self.work / "sales-api-java"))

    def test_deploy_repo_references_app_not_prefix_repo(self):
        k8s = self.links("platform-k8s")
        self.assertIn(("sales-api-java", "refs"), k8s)
        # "sales-api-java-prod" must not also count as a mention of the shorter sales-api
        self.assertNotIn(("sales-api", "refs"), k8s)

    def test_links_are_symmetric_and_name_prefix_is_linked(self):
        self.assertIn(("platform-k8s", "refd_by"), self.links("sales-api-java"))
        self.assertIn(("sales-api", "name"), self.links("sales-api-java"))
        self.assertIn(("sales-api-java", "name"), self.links("sales-api"))

    def test_unrelated_repo_has_no_links(self):
        self.assertEqual(self.repos["warehouse-etl"].links, [])

    def test_roles(self):
        self.assertIn("deploy", self.repos["platform-k8s"].roles)
        self.assertIn("data", self.repos["warehouse-etl"].roles)
        self.assertIn("app", self.repos["sales-api-java"].roles)

    def test_old_index_without_new_fields_still_loads(self):
        from blinders.scan import index_path
        raw = json.loads(index_path().read_text())
        for r in raw["repos"]:
            for k in ("roles", "identities", "links", "graph_report"):
                r.pop(k)
            r["future_field"] = 1
        index_path().write_text(json.dumps(raw))
        self.assertEqual(len(load_index(self.cfg)), 4)

    def test_ambiguous_artifact_name_is_ignored(self):
        for n in ("fork-a", "fork-b"):
            repo = make_repo(self.work, n, "x")
            (repo / "package.json").write_text('{"name": "shared-lib-name"}')
        user = make_repo(self.work, "consumer-app", "x")
        (user / "package.json").write_text('{"name": "consumer-app", "dependencies": {"shared-lib-name": "1"}}')
        repos = {r.name: r for r in build_index(self.cfg)}
        self.assertEqual([l for l in repos["consumer-app"].links if l["kind"] == "refs"], [])


class PlanTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.sales_repos()
        self.repos = build_index(self.cfg)

    def names(self, p):
        return [c.repo.name for c in p.opened], [r.repo.name for r in p.related]

    def test_intents(self):
        self.assertEqual(intents("comment est déployé sur kubernetes"), {"deploy"})
        self.assertEqual(intents("lineage of the bigquery tables"), {"data"})
        self.assertEqual(intents("fix the typo"), set())

    def test_lineage_question_opens_app_and_lists_neighbors(self):
        opened, related = self.names(plan("tu peux me dire le lineage de sales-api-java", self.repos, self.cfg))
        self.assertEqual(opened, ["sales-api-java"])
        self.assertEqual(set(related), {"platform-k8s", "sales-api"})

    def test_deploy_question_opens_the_deploy_repo(self):
        opened, related = self.names(plan("comment sales-api-java est déployé sur kubernetes", self.repos, self.cfg))
        self.assertEqual(opened, ["sales-api-java", "platform-k8s"])
        self.assertEqual(related, ["sales-api"])

    def test_related_none_and_all(self):
        p = plan("comment sales-api-java est déployé sur kubernetes", self.repos, self.cfg, related="none")
        self.assertEqual(self.names(p), (["sales-api-java"], []))
        p = plan("lineage de sales-api-java", self.repos, self.cfg, related="all")
        self.assertEqual(set(self.names(p)[0]), {"sales-api-java", "platform-k8s", "sales-api"})

    def test_related_open_cap(self):
        self.cfg.max_related_open = 1
        opened, related = self.names(plan("lineage de sales-api-java", self.repos, self.cfg, related="all"))
        self.assertEqual(len(opened), 2)
        self.assertEqual(len(related), 1)

    def test_explicit_group(self):
        self.cfg.groups = {"billing": ["sales-api-java", "warehouse-etl"]}
        # no data intent in the prompt: the group member is listed, not opened
        p = plan("explain sales-api-java", self.repos, self.cfg)
        opened, related = self.names(p)
        self.assertEqual(opened, ["sales-api-java"])
        self.assertIn("warehouse-etl", related)
        reason = next(r.why for r in p.related if r.repo.name == "warehouse-etl")
        self.assertIn("billing", reason)
        # data intent ("lineage"): the data repo of the group is opened
        opened, _ = self.names(plan("lineage de sales-api-java", self.repos, self.cfg))
        self.assertIn("warehouse-etl", opened)

    def test_forced_seeds(self):
        p = plan("", self.repos, self.cfg, forced=[r for r in self.repos if r.name == "platform-k8s"])
        opened, related = self.names(p)
        self.assertEqual(opened, ["platform-k8s"])
        self.assertIn("sales-api-java", related)

    def test_no_seed_no_related(self):
        self.assertEqual(self.names(plan("what is the capital of France", self.repos, self.cfg)), ([], []))

    def test_index_text_lists_related_with_reason_and_graph_report(self):
        p = plan("lineage de sales-api-java", self.repos, self.cfg)
        opened = [c.repo for c in p.opened]
        opened[0].graph_report = "/x/graphify-out/GRAPH_REPORT.md"
        text = render_index(opened, [r for r in self.repos if r not in opened], self.cfg, related=p.related)
        self.assertIn("## Related repos", text)
        self.assertIn("platform-k8s", text)
        self.assertIn("references sales-api-java", text)
        self.assertIn("GRAPH_REPORT.md", text)
        # related repos are not repeated in the generic closed list
        self.assertEqual(text.count("- platform-k8s:"), 1)


if __name__ == "__main__":
    unittest.main()
