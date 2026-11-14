import unittest

from main import filter_requested_matchup


class MatchupFilterTests(unittest.TestCase):
    def setUp(self):
        self.games = [
            {"Away Team": "New England Patriots", "Home Team": "Detroit Lions"},
            {"Away Team": "Kansas City Chiefs", "Home Team": "Atlanta Falcons"},
        ]

    def test_filter_selects_one_matchup_case_insensitively(self):
        result = filter_requested_matchup(self.games, ["--matchup", "patriots at lions"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["Away Team"], "New England Patriots")


    def test_filter_accepts_full_team_names(self):
        result = filter_requested_matchup(
            self.games, ["--matchup", "New England Patriots at Detroit Lions"]
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["Home Team"], "Detroit Lions")

    def test_filter_accepts_city_names(self):
        result = filter_requested_matchup(
            self.games, ["--matchup", "New England at Detroit"]
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["Away Team"], "New England Patriots")

    def test_filter_without_option_keeps_full_slate(self):
        self.assertEqual(filter_requested_matchup(self.games, []), self.games)

    def test_filter_rejects_unknown_matchup(self):
        with self.assertRaisesRegex(ValueError, "No matchup matched"):
            filter_requested_matchup(self.games, ["--matchup", "Bills at Jets"])

    def test_filter_requires_matchup_value(self):
        with self.assertRaisesRegex(ValueError, "Use --matchup"):
            filter_requested_matchup(self.games, ["--matchup"])


if __name__ == "__main__":
    unittest.main()
