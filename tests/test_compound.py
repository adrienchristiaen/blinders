import unittest

from blinders.grep import literals, locate
from blinders.scan import build_index
from blinders.select import _named_repos, plan

from helpers import Sandbox, make_repo


def write(repo, rel, text):
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


class CompoundNameTests(Sandbox):
    """`phenix-cli` is not the repo `phenix`: a name inside a longer hyphenated name is another name."""

    def setUp(self):
        super().setUp()
        make_repo(self.work, "phenix", "Platform.")
        make_repo(self.work, "jira-cli", "CLI automating tickets.")
        self.repos = build_index(self.cfg)

    def named(self, prompt):
        return {r.name for r in self.repos if r.path in _named_repos(self.repos, prompt)}

    def test_a_repo_name_inside_a_longer_hyphenated_name_is_not_named(self):
        self.assertEqual(self.named("quelle est l'utilité de phenix-cli ?"), set())

    def test_the_plain_name_still_counts(self):
        self.assertEqual(self.named("explain phenix please"), {"phenix"})
        self.assertEqual(self.named("explain phenix, please"), {"phenix"})

    def test_a_hyphenated_name_matches_with_hyphen_or_space(self):
        self.assertEqual(self.named("fix jira-cli"), {"jira-cli"})
        self.assertEqual(self.named("fix jira cli"), {"jira-cli"})


class HyphenatedLiteralTests(unittest.TestCase):
    def test_plain_ascii_compound_is_a_literal(self):
        self.assertIn("phenix-cli", literals("quelle est l'utilité de phenix-cli ?"))

    def test_everyday_french_compounds_are_not(self):
        self.assertEqual(literals("est-ce que peut-être c'est-à-dire peut-on"), [])


class StemMatchTests(Sandbox):
    def test_short_stems_match_whole_words_only_and_long_ones_by_prefix(self):
        a = make_repo(self.work, "a-repo", "x")
        write(a, "notes.txt", "a client talks to the server")
        b = make_repo(self.work, "b-repo", "x")
        write(b, "notes.txt", "run the cli tool; deployment is done")
        repos = build_index(self.cfg)
        where = locate(["cli", "deplo"], repos, stems=True)
        names = lambda w: {r.name for r in repos if r.path in where[w]}
        self.assertEqual(names("cli"), {"b-repo"})        # not "client"
        self.assertEqual(names("deplo"), {"b-repo"})      # "deployment" by prefix
        self.assertEqual(locate(["cli"], repos, use_rg=False, stems=True), {"cli": where["cli"]})


class GenericWordsAreNoProof(Sandbox):
    def test_words_found_in_many_repos_do_not_vouch_for_a_repo(self):
        make_repo(self.work, "zebra", "Zebra service.")
        guide = make_repo(self.work, "alpha-docs", "Docs.")
        write(guide, "utility_overview.txt", "utility overview of things")
        for i in range(10):
            f = make_repo(self.work, f"filler-{i}", "Filler.")
            write(f, "doc.txt", "utility overview")
        repos = build_index(self.cfg)
        opened = [c.repo.name for c in plan("zebra utility overview", repos, self.cfg).opened]
        self.assertIn("zebra", opened)
        self.assertNotIn("alpha-docs", opened)


class FindsTheRepoThatContainsTheCompound(Sandbox):
    def test_phenix_cli_brings_the_repo_whose_files_mention_it(self):
        make_repo(self.work, "phenix", "Platform.")
        flow = make_repo(self.work, "phenix-flow", "Flow.")
        write(flow, "docs/guide.md", "The phenix-cli command starts a flow.")
        other = make_repo(self.work, "mep-flow", "Mise en production. Quelle utilité ?")
        write(other, "x.txt", "x")
        repos = build_index(self.cfg)
        opened = [c.repo.name for c in plan("quelle est l'utilité de phenix-cli ?", repos, self.cfg).opened]
        self.assertIn("phenix-flow", opened)


if __name__ == "__main__":
    unittest.main()
