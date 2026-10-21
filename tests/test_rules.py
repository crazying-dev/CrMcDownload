import unittest

from offcraft.meta.rules import OsInfo, is_allowed, rule_matches


WIN = OsInfo("windows", "10.0.19045", "x86_64")


class RulesTest(unittest.TestCase):
    def test_no_rules_allows(self) -> None:
        self.assertTrue(is_allowed(None, WIN))
        self.assertTrue(is_allowed([], WIN))

    def test_disallow_other_os(self) -> None:
        rules = [{"action": "allow"}, {"action": "disallow", "os": {"name": "osx"}}]
        self.assertTrue(is_allowed(rules, WIN))
        self.assertFalse(is_allowed(rules, OsInfo("osx", "14.0", "arm64")))

    def test_allow_only_windows(self) -> None:
        rules = [{"action": "allow", "os": {"name": "windows"}}]
        self.assertTrue(is_allowed(rules, WIN))
        self.assertFalse(is_allowed(rules, OsInfo("linux", "6.8", "x86_64")))

    def test_arch(self) -> None:
        rules32 = [{"action": "allow", "os": {"arch": "x86"}}]
        self.assertFalse(is_allowed(rules32, WIN))
        self.assertTrue(is_allowed(rules32, OsInfo("windows", "10.0", "x86")))

    def test_os_version_regex(self) -> None:
        rules = [{"action": "allow", "os": {"name": "windows", "version": "^10\\."}}]
        self.assertTrue(is_allowed(rules, WIN))
        self.assertFalse(is_allowed(rules, OsInfo("windows", "6.1", "x86_64")))

    def test_features(self) -> None:
        rules = [{"action": "allow", "features": {"is_demo_user": True}}]
        self.assertFalse(is_allowed(rules, WIN))
        self.assertTrue(is_allowed(rules, WIN, {"is_demo_user": True}))

    def test_last_matching_rule_wins(self) -> None:
        rules = [
            {"action": "allow"},
            {"action": "disallow", "os": {"name": "windows"}},
        ]
        self.assertFalse(is_allowed(rules, WIN))

    def test_rule_matches_without_conditions(self) -> None:
        self.assertTrue(rule_matches({}, WIN))


if __name__ == "__main__":
    unittest.main()
