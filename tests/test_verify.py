import os
import unittest

from blinders.grep import locate
from blinders.scan import build_index, describe_repo
from blinders.select import plan

from helpers import Sandbox, make_repo


def write(repo, rel, text):
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


class VerifyTests(Sandbox):
    """The names of a repo can match a prompt while its files say nothing about it: the files decide."""

    def setUp(self):
        super().setUp()
        app = make_repo(self.work, "billing-app", "Billing application.")
        write(app, "src/a/b/Invoice.java", "invoice refund rate")
        # names match "invoice" and "refund", contents do not
        tools = make_repo(self.work, "refund-tools", "Misc.")
        write(tools, "invoice_refund.txt", "nothing here")
        # names and contents match
        real = make_repo(self.work, "refund-engine", "Misc.")
        write(real, "invoice_refund.txt", "compute the refund of an invoice")
        make_repo(self.work, "photo-tools", "Photo tools.")
        self.repos = build_index(self.cfg)

    def run_plan(self, prompt, **kw):
        p = plan(prompt, self.repos, self.cfg, **kw)
        return [c.repo.name for c in p.opened], {r.repo.name: r.why for r in p.related}

    def test_a_companion_whose_files_do_not_contain_the_words_is_only_listed(self):
        opened, listed = self.run_plan("in billing-app fix the invoice refund")
        self.assertIn("refund-engine", opened)
        self.assertNotIn("refund-tools", opened)
        self.assertIn("refund-tools", listed)
        self.assertIn("files", listed["refund-tools"])

    def test_the_named_repo_is_never_dropped(self):
        opened, _ = self.run_plan("billing-app unrelatedword")
        self.assertEqual(opened, ["billing-app"])

    def test_the_best_match_stays_even_without_proof_when_nothing_is_named(self):
        for p in self.work.glob("refund-engine/*.txt"):
            p.write_text("empty")
        self.repos = build_index(self.cfg)
        opened, _ = self.run_plan("invoice refund")
        self.assertTrue(opened)   # the top match is kept, the others must prove themselves

    def test_switch_off(self):
        self.cfg.verify_enabled = False
        opened, _ = self.run_plan("in billing-app fix the invoice refund")
        self.assertIn("refund-tools", opened)

    def test_forced_repos_are_kept(self):
        forced = [r for r in self.repos if r.name == "refund-tools"]
        opened, _ = self.run_plan("invoice refund", forced=forced)
        self.assertEqual(opened, ["refund-tools"])

    def test_locate_with_and_without_rg_agree(self):
        paths = locate(["refun", "photo"], self.repos)
        plain = locate(["refun", "photo"], self.repos, use_rg=False)
        self.assertEqual(paths, plain)
        self.assertEqual({r.name for r in self.repos if r.path in paths["refun"]}, {"billing-app", "refund-engine"})


class RelatedNeedsOneWord(Sandbox):
    def test_a_linked_repo_needs_only_one_word_in_its_files(self):
        self.sales_repos()
        repos = build_index(self.cfg)
        p = plan("comment sales-api-java est déployé sur kubernetes", repos, self.cfg)
        self.assertIn("platform-k8s", [c.repo.name for c in p.opened])


class ScriptHeaderTests(Sandbox):
    def test_the_header_of_a_script_describes_the_repo(self):
        repo = make_repo(self.work, "ops-scripts", "")
        os.remove(repo / "README.md")
        script = repo / "replay.sh"
        script.write_text("#!/bin/bash\n# Re-send stuck uploads from the inbound bucket\ngcloud storage ls\n")
        script.chmod(0o755)
        self.assertIn("stuck", describe_repo(repo, []).terms)

    def test_a_plain_data_file_is_not_read_as_a_script(self):
        repo = make_repo(self.work, "notes", "")
        os.remove(repo / "README.md")
        (repo / "data.txt").write_text("secretword here")
        self.assertNotIn("secretword", describe_repo(repo, []).terms)


if __name__ == "__main__":
    unittest.main()
