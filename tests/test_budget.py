import time
import unittest

from blinders.grep import SearchTimeout, find_hits, locate
from blinders.scan import build_index
from blinders.select import plan

from helpers import Sandbox, make_repo


def write(repo, rel, text):
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


class BudgetTests(Sandbox):
    def setUp(self):
        super().setUp()
        app = make_repo(self.work, "billing-app", "Billing application.")
        write(app, "src/Invoice.java", "invoice_vat_rate refund")
        other = make_repo(self.work, "cluster-config", "Manifests.")
        write(other, "cm.yaml", "INVOICE_VAT_RATE: 1")
        self.repos = build_index(self.cfg)

    def test_a_search_past_its_deadline_stops(self):
        with self.assertRaises(SearchTimeout):
            locate(["invoice"], self.repos, deadline=time.monotonic() - 1)

    def test_answers_found_before_the_deadline_are_kept_for_the_next_try(self):
        cache = {}
        with self.assertRaises(SearchTimeout):
            locate(["invoice", "refund"], self.repos, cache=cache, deadline=time.monotonic() - 1)
        self.assertEqual(len(locate(["invoice"], self.repos, cache=cache)["invoice"]), 2)

    def test_identifier_search_gives_nothing_rather_than_failing(self):
        self.cfg.deep_budget = 1e-9
        self.assertEqual(find_hits("change invoice_vat_rate", self.repos, self.cfg, deadline=time.monotonic() - 1), {})

    def test_plan_still_answers_when_the_budget_is_spent(self):
        self.cfg.deep_budget = 1e-9
        p = plan("in billing-app fix the invoice refund", self.repos, self.cfg)
        self.assertEqual([c.repo.name for c in p.opened][0], "billing-app")

    def test_no_budget_means_no_limit(self):
        self.cfg.deep_budget = 0
        p = plan("change invoice_vat_rate", self.repos, self.cfg)
        self.assertEqual({c.repo.name for c in p.opened}, {"billing-app", "cluster-config"})


if __name__ == "__main__":
    unittest.main()
