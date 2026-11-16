import unittest
from openpyxl import Workbook

from main import (
    _cached_analysis_is_complete,
    _write_matchup_sheet,
    filter_requested_matchup,
)


class MatchupFilterTests(unittest.TestCase):
    def setUp(self):
        self.games = [
            {"Away Team": "New England Patriots", "Home Team": "Detroit Lions"},
            {"Away Team": "Kansas City Chiefs", "Home Team": "Atlanta Falcons"},
        ]

    def test_empty_analysis_cache_is_not_considered_complete(self):
        cached = {
            "picks": [],
            "touchdown_scorers": [],
            "sneaky_props": [],
            "injury_impact": [],
            "underdog_analysis": [],
            "player_research_summary": [],
            "analysis_version": 12,
            "ai_provider": "groq",
        }
        self.assertFalse(_cached_analysis_is_complete(cached))

    def test_cache_with_any_usable_analysis_section_is_complete(self):
        base = {
            "picks": [],
            "touchdown_scorers": [],
            "sneaky_props": [],
            "injury_impact": [],
            "underdog_analysis": [],
            "player_research_summary": [],
            "analysis_version": 12,
            "ai_provider": "groq",
        }
        for section in (
            "picks", "touchdown_scorers", "sneaky_props",
            "injury_impact", "underdog_analysis",
        ):
            with self.subTest(section=section):
                cached = {**base, section: [{"evidence": "validated output"}]}
                self.assertTrue(_cached_analysis_is_complete(cached))

    def test_cache_without_current_version_or_provider_is_incomplete(self):
        cached = {
            "picks": [{"bet_description": "Lions moneyline"}],
            "touchdown_scorers": [],
            "underdog_analysis": [],
            "analysis_version": 10,
            "ai_provider": "groq",
        }
        self.assertFalse(_cached_analysis_is_complete(cached))
        cached["analysis_version"] = 12
        cached["ai_provider"] = ""
        self.assertFalse(_cached_analysis_is_complete(cached))

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

    def test_matchup_sheet_displays_underdog_watch_and_compacted_status(self):
        workbook = Workbook()
        workbook.remove(workbook.active)
        game = {
            "Away Team": "New England Patriots",
            "Home Team": "Detroit Lions",
            "Game Date (ET)": "Sunday",
            "Betting Line": "DET -2.5",
        }
        analysis = {
            "summary": "Balanced matchup",
            "game_script": "Competitive game",
            "ai_provider": "nvidia",
            "research_mode": "independent_espn_plus_ai",
            "research_truncated_for_prompt": False,
            "research_compacted_for_prompt": True,
            "research_prompt_characters": 12000,
            "odds_api_status": "fetched",
            "odds_api_usage": {},
            "picks": [],
            "model_only_picks": [],
            "underdog_analysis": [{
                "underdog": "New England Patriots",
                "spread": "+2.5",
                "cover_probability": 54,
                "upset_probability": 38,
                "recommendation": "Monitor",
                "rationale": "Pressure mismatch keeps the game close",
                "risk_factors": "Turnover variance",
                "evidence_quality": "Moderate",
            }],
            "touchdown_scorers": [],
            "injury_impact": [],
            "sneaky_props": [],
            "data_gaps": [],
            "sources": [],
        }
        _write_matchup_sheet(workbook, game, analysis, set())
        sheet = workbook.worksheets[0]
        values = [cell.value for row in sheet.iter_rows() for cell in row if cell.value]
        self.assertIn("UNDERDOG / UPSET WATCH", values)
        self.assertIn("New England Patriots", values)
        self.assertIn("+2.5", values)
        self.assertIn("COMPACTED (12,000 chars)", sheet["A3"].value)

    def test_filter_rejects_unknown_matchup(self):
        with self.assertRaisesRegex(ValueError, "No matchup matched"):
            filter_requested_matchup(self.games, ["--matchup", "Bills at Jets"])

    def test_filter_requires_matchup_value(self):
        with self.assertRaisesRegex(ValueError, "Use --matchup"):
            filter_requested_matchup(self.games, ["--matchup"])


if __name__ == "__main__":
    unittest.main()
