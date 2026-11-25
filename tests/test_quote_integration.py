"""End-to-end fixture: exact sportsbook quotes must survive model and Excel stages."""
import json
import unittest
from unittest.mock import patch

from ai_analysis import analyze_game
from main import _write_matchup_sheet
from openpyxl import Workbook


class PropReportIntegrationTests(unittest.TestCase):
    def test_focused_review_populates_real_verified_player_and_touchdown_sections(self):
        game = {
            "Away Team": "Jacksonville Jaguars",
            "Home Team": "New York Giants",
            "Game Date (ET)": "Sun Nov 22, 2026 04:25 PM ET",
            "Betting Line": "JAX -6",
            "Game Status": "Scheduled",
        }
        player = "Alpha Receiver"
        roster = {
            "Jacksonville Jaguars": [player],
            "New York Giants": ["Giants Quarterback"],
        }
        stats = {
            "games_available": 9,
            "season": {
                "receivingYards": 630, "receivingTargets": 81,
                "receivingTouchdowns": 5, "receptions": 54,
            },
            "recent": [
                {"stats": {"receivingYards": 82, "receivingTargets": 10,
                           "receivingTouchdowns": 1, "receptions": 7}},
                {"stats": {"receivingYards": 71, "receivingTargets": 9,
                           "receivingTouchdowns": 0, "receptions": 6}},
                {"stats": {"receivingYards": 65, "receivingTargets": 9,
                           "receivingTouchdowns": 1, "receptions": 6}},
            ],
        }
        player_record = {
            "player": player, "position": "WR",
            "usage_and_recent_stats": stats,
        }
        research_text = (
            "ESPN CURRENT-SEASON PLAYER GAME LOGS — Jacksonville Jaguars (offense): "
            + json.dumps([player_record])
        )
        yards = {
            "player": player, "market_key": "player_reception_yds",
            "side": "Over", "point": 50.5, "line": "Over 50.5", "price": "-110",
            "sportsbook": "DraftKings",
            "source_url": "https://the-odds-api.com/",
            "retrieved_at": "2026-11-21T12:00:00Z",
        }
        td = {
            "player": player, "market_key": "player_anytime_td",
            "side": "Yes", "point": None, "line": "Yes", "price": "+145",
            "sportsbook": "DraftKings",
            "source_url": "https://the-odds-api.com/",
            "retrieved_at": "2026-11-21T12:00:00Z",
        }
        research = {
            "research_text": research_text,
            "rosters": roster,
            "injury_statuses": [],
            "selected_player_candidates": {
                "Jacksonville Jaguars": [
                    {"name": player, "position": "WR"}
                ],
            },
            "verified_player_prop_markets": [yards, td],
            "verified_sportsbook_game_markets": [],
            "verified_game_markets": [],
            "sources": [],
        }
        main_model = {
            "summary": "A short game matchup summary.",
            "game_script": "The road team favors efficient passing.",
            "underdog_analysis": [{
                "underdog": "New York Giants", "spread": "+6",
                "cover_probability": 43, "upset_probability": 25,
                "recommendation": "Monitor", "rationale": "Uncertain matchup.",
                "risk_factors": "Game-script uncertainty.",
                "evidence_quality": "Limited",
            }],
            "picks": [], "sneaky_props": [],
            "touchdown_scorers": [], "injury_impact": [], "data_gaps": [],
        }
        prop_review = {
            "picks": [{
                "rank": 1, "market_type": "Player Prop", "player_or_team": player,
                "bet_description": "Over 50.5 receiving yards",
                "market_line": "Over 50.5 (-110) — DraftKings",
                "play_threshold": "Over 50.5",
                "estimated_hit_probability": 61, "confidence": 6,
                "statistical_rationale": "Season 70.0 receiving yards per game, recent 72.7.",
                "risk_factors": "Target volatility and defensive matchup.",
                "data_quality": "Moderate",
            }],
            "touchdown_scorers": [{
                "rank": 1, "player": player,
                "team": "Jacksonville Jaguars",
                "availability": "No injury designation found",
                "availability_evidence": "Final game-day active status pending.",
                "estimated_td_probability": 48, "confidence": 5,
                "rationale": "Five receiving touchdowns over nine games, nine targets per game.",
                "risk_factors": "Red-zone opportunity variance.",
            }],
        }
        with patch(
            "ai_analysis.generate_json_ensemble",
            return_value=([(main_model, "nvidia")], [], ["nvidia"])
        ), patch(
            "ai_analysis.generate_json",
            return_value=(prop_review, "nvidia")
        ) as review:
            analysis = analyze_game(game, research=research)
        review.assert_called_once()
        self.assertEqual(analysis["analysis_version"], 15)
        self.assertEqual(len(analysis["picks"]), 1)
        self.assertEqual(len(analysis["touchdown_scorers"]), 1)
        self.assertIn("50.5", analysis["picks"][0]["market_line"])
        self.assertIn("+145", analysis["touchdown_scorers"][0]["market_line"])
        self.assertEqual(analysis["picks"][0]["market_source"], yards["source_url"])
        self.assertIn("Receiving yards:", analysis["picks"][0]["average_per_game"])
        self.assertIn("Offensive TDs:", analysis["touchdown_scorers"][0]["average_per_game"])
        workbook = Workbook()
        _write_matchup_sheet(workbook, game, analysis, set())
        ws = workbook.worksheets[-1]
        values = [
            (str(cell.value) if cell.value is not None else "")
            for row in ws.iter_rows() for cell in row
        ]
        self.assertIn("VERIFIED PLAYER PROPS (SPORTSBOOK LINE REQUIRED)", values)
        self.assertIn("ANYTIME TOUCHDOWN SCORERS", values)
        self.assertIn("Over 50.5 receiving yards", values)
        self.assertTrue(any("+145" in cell for cell in values))

    def test_fabricated_point_is_rejected_even_when_model_returns_it(self):
        game = {
            "Away Team": "Jacksonville Jaguars",
            "Home Team": "New York Giants",
            "Game Date (ET)": "Sun Nov 22, 2026 04:25 PM ET",
            "Betting Line": "JAX -6", "Game Status": "Scheduled",
        }
        stats = {
            "games_available": 9, "season": {
                "receivingYards": 630, "receivingTargets": 81,
                "receptions": 54,
            },
            "recent": [{"stats": {"receivingYards": 70}}] * 3,
        }
        player_record = {
            "player": "Alpha Receiver", "position": "WR",
            "usage_and_recent_stats": stats,
        }
        quote = {
            "player": "Alpha Receiver", "market_key": "player_reception_yds",
            "side": "Over", "point": 50.5, "line": "Over 50.5",
            "price": "-110", "sportsbook": "DraftKings",
            "source_url": "https://the-odds-api.com/",
            "retrieved_at": "2026-11-21T12:00:00Z",
        }
        research = {
            "research_text": "ESPN CURRENT-SEASON PLAYER GAME LOGS — Jaguars: "
            + json.dumps([player_record]),
            "rosters": {
                "Jacksonville Jaguars": ["Alpha Receiver"],
                "New York Giants": [],
            },
            "verified_player_prop_markets": [quote],
            "injury_statuses": [], "sources": [],
        }
        main = {
            "summary": "Matchup", "game_script": "Game script",
            "underdog_analysis": [{
                "underdog": "New York Giants", "spread": "+6",
                "cover_probability": 40, "upset_probability": 20,
                "recommendation": "Pass", "rationale": "Limited evidence.",
                "risk_factors": "No edge.", "evidence_quality": "Limited",
            }],
            "picks": [], "sneaky_props": [], "touchdown_scorers": [],
            "injury_impact": [], "data_gaps": [],
        }
        invented = {
            "picks": [{
                "rank": 1, "market_type": "Player Prop",
                "player_or_team": "Alpha Receiver",
                "bet_description": "Over 60.5 receiving yards",
                "market_line": "Over 60.5 (-110) — DraftKings",
                "play_threshold": "Over 60.5", "estimated_hit_probability": 65,
                "confidence": 7, "statistical_rationale": "Model estimate.",
                "risk_factors": "Uncertainty.", "data_quality": "Moderate",
            }],
            "touchdown_scorers": [],
        }
        with patch(
            "ai_analysis.generate_json_ensemble",
            return_value=([(main, "nvidia")], [], ["nvidia"])
        ), patch(
            "ai_analysis.generate_json",
            return_value=(invented, "nvidia")
        ):
            analysis = analyze_game(game, research=research)
        self.assertEqual(analysis["picks"], [])
        self.assertEqual(len(analysis["verified_market_candidates"]), 1)


if __name__ == "__main__":
    unittest.main()
