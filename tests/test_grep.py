import unittest

from blinders.grep import find_hits, literals, search
from blinders.scan import build_index
from blinders.select import plan

from helpers import Sandbox, make_repo


def deep(repo, rel, text):
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


class LiteralTests(unittest.TestCase):
    def test_identifier_shaped_tokens_only(self):
        found = literals("rename customer_id and orderTotal, see ABC123 and also the plain words")
        self.assertEqual(found, ["customer_id", "orderTotal", "ABC123"])

    def test_quoted_and_backticked_text_counts(self):
        self.assertEqual(literals("drop `sales-api-v2` then \"raw.events\""), ["sales-api-v2", "raw.events"])

    def test_plain_hyphenated_words_are_not_identifiers(self):
        self.assertEqual(literals("peut-être que c'est bien"), [])

    def test_a_dotted_name_is_also_searched_by_its_identifier_parts(self):
        self.assertEqual(literals("bucket raw_ugc_inbound.acme.com en prod"),
                         ["raw_ugc_inbound.acme.com", "raw_ugc_inbound"])

    def test_duplicates_and_cap(self):
        text = " ".join(f"col_{i}" for i in range(20)) + " col_1"
        found = literals(text, limit=5)
        self.assertEqual(len(found), 5)
        self.assertEqual(len(set(found)), 5)


class SearchTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.app = make_repo(self.work, "billing-app", "Billing application.")
        deep(self.app, "src/main/java/com/acme/billing/deep/Invoice.java", "String col = \"invoice_vat_rate\";")
        self.k8s = make_repo(self.work, "cluster-config", "Manifests.")
        deep(self.k8s, "overlays/prod/cm.yaml", "INVOICE_VAT_RATE: '0.2'")
        self.other = make_repo(self.work, "photo-tools", "Photo tools.")
        deep(self.other, "a/b.py", "print('hello')")
        self.repos = build_index(self.cfg)

    def paths(self, hits):
        return {r.name for r in self.repos if r.path in hits}

    def test_finds_repos_containing_a_literal_even_deep_in_the_tree(self):
        hits = search(["invoice_vat_rate"], self.repos)
        self.assertEqual(self.paths(hits), {"billing-app", "cluster-config"})

    def test_python_fallback_gives_the_same_answer(self):
        with_rg = search(["invoice_vat_rate"], self.repos)
        without = search(["invoice_vat_rate"], self.repos, use_rg=False)
        self.assertEqual(set(with_rg), set(without))

    def test_unknown_literal_finds_nothing(self):
        self.assertEqual(search(["no_such_thing_here"], self.repos), {})

    def test_literal_found_in_most_repos_is_ignored(self):
        for i in range(6):
            r = make_repo(self.work, f"svc-{i}", "A service.")
            deep(r, "conf.txt", "shared_setting=1")
        repos = build_index(self.cfg)
        self.assertEqual(search(["shared_setting"], repos), {})

    def test_find_hits_respects_the_switch(self):
        self.cfg.grep_enabled = False
        self.assertEqual(find_hits("rename invoice_vat_rate", self.repos, self.cfg), {})
        self.cfg.grep_enabled = True
        self.assertEqual(self.paths(find_hits("rename invoice_vat_rate", self.repos, self.cfg)),
                         {"billing-app", "cluster-config"})


class PlanWithHitsTests(Sandbox):
    def setUp(self):
        super().setUp()
        app = make_repo(self.work, "billing-app", "Billing application.")
        deep(app, "src/a/b/c/Invoice.java", "invoice_vat_rate")
        k8s = make_repo(self.work, "cluster-config", "Manifests.")
        deep(k8s, "overlays/prod/cm.yaml", "INVOICE_VAT_RATE: '0.2'")
        batch = make_repo(self.work, "nightly-batch", "Jobs.")
        deep(batch, "jobs/x/y/z/Job.scala", "val c = \"invoice_vat_rate\"")
        make_repo(self.work, "photo-tools", "Photo tools.")
        self.repos = build_index(self.cfg)

    def opened(self, prompt):
        return [c.repo.name for c in plan(prompt, self.repos, self.cfg).opened]

    def test_literal_brings_every_repo_that_contains_it(self):
        self.cfg.max_repos = 3
        self.assertEqual(set(self.opened("change invoice_vat_rate to 0.21")),
                         {"billing-app", "cluster-config", "nightly-batch"})

    def test_named_repo_stays_first_and_hits_follow(self):
        self.assertEqual(self.opened("in billing-app change invoice_vat_rate")[0], "billing-app")
        self.assertIn("cluster-config", self.opened("in billing-app change invoice_vat_rate"))

    def test_cap_is_respected(self):
        self.cfg.max_repos = 2
        self.assertEqual(len(self.opened("change invoice_vat_rate")), 2)

    def test_a_repo_name_is_not_searched_as_a_literal(self):
        self.assertEqual(self.opened("explain billing_app"), ["billing-app"])

    def test_switch_off(self):
        self.cfg.grep_enabled = False
        opened = self.opened("change invoice_vat_rate")
        self.assertNotIn("nightly-batch", opened)
        self.assertNotIn("cluster-config", opened)

    def test_forced_repos_skip_the_search(self):
        forced = [r for r in self.repos if r.name == "photo-tools"]
        p = plan("change invoice_vat_rate", self.repos, self.cfg, forced=forced)
        self.assertEqual([c.repo.name for c in p.opened], ["photo-tools"])


if __name__ == "__main__":
    unittest.main()
