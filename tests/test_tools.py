import unittest

from blinders.config import Config
from blinders.tools import tool_statuses


def by_name(statuses):
    return {s.name: s for s in statuses}


def only(*found):
    return lambda name: f"/bin/{name}" if name in found else None


class ToolStatusTests(unittest.TestCase):
    def test_nothing_installed_means_both_missing_with_a_way_to_install(self):
        s = by_name(tool_statuses(Config(), gemini=True, which=only()))
        self.assertEqual((s["graphify"].state, s["rtk"].state), ("missing", "missing"))
        self.assertIn("uv tool install graphifyy", s["graphify"].detail)
        self.assertIn("cargo install", s["rtk"].detail)

    def test_found_tools_are_on(self):
        s = by_name(tool_statuses(Config(), gemini=True, which=only("graphify", "rtk")))
        self.assertEqual((s["graphify"].state, s["rtk"].state), ("on", "on"))
        self.assertIn("/bin/rtk", s["rtk"].detail)

    def test_rtk_is_only_wired_to_gemini(self):
        s = by_name(tool_statuses(Config(), gemini=False, which=only("rtk")))
        self.assertEqual(s["rtk"].state, "off")
        self.assertIn("Gemini", s["rtk"].detail)

    def test_rtk_switched_off_in_the_config(self):
        s = by_name(tool_statuses(Config(gemini_rtk=False), gemini=True, which=only("rtk")))
        self.assertEqual(s["rtk"].state, "off")
        self.assertIn("rtk = false", s["rtk"].detail)

    def test_rtk_needs_the_isolated_gemini_home(self):
        s = by_name(tool_statuses(Config(gemini_isolate_home=False), gemini=True, which=only("rtk")))
        self.assertEqual(s["rtk"].state, "off")
        self.assertIn("isolate_home", s["rtk"].detail)

    def test_the_configured_graphify_binary_is_the_one_looked_up(self):
        seen = []
        tool_statuses(Config(graphify_bin="my-graphify"), gemini=True, which=lambda n: seen.append(n))
        self.assertIn("my-graphify", seen)


if __name__ == "__main__":
    unittest.main()
