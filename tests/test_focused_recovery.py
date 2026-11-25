"""Regression tests for the one-call focused recovery of incomplete betting sections."""
import unittest
from unittest.mock import patch

from ai_analysis import _recover_verified_props_from_focused_review


class FocusedRecoveryTests(unittest.TestCase):
    def test_existing_three_props_are_preserved_while_filling_two_and_sneaky_td_summary(self):
        teams = ("Away Team", "Home Team")
        players = ["Player A", "Player B", "Player C", "Player D", "Player E", "Player F"]
        quotes = [
            {
                "player": player, "market_key": "player_reception_yds",
                "side": "Over", "point": 40.5 + index,
                "line": f"Over {40.5 + index:g}", "price": "-110",
                "sportsbook": "DraftKings", "source_url": "https://the-odds-api.com/",
                "retrieved_at": "2026-11-21T10:00:00Z",
            }
            for index, player in enumerate(players)
        ]
        quotes.append({
            "player": "Player A", "market_key": "player_anytime_td",
            "side": "Yes", "point": None, "line": "Yes", "price": "+140",
            "sportsbook": "DraftKings", "source_url": "https://the-odds-api.com/",
            "retrieved_at": "2026-11-21T10:00:00Z",
        })
        shortlist = [
            {
                "player": player, "market_key": "player_reception_yds",
                "side": "Over", "point": 40.5 + index, "season_average": 62,
                "games": 9, "line": f"Over {40.5 + index:g}",
                "price": "-110", "sportsbook": "DraftKings",
            }
            for index, player in enumerate(players)
        ]
        usage = {
            "playera": {
                "position": "RB", "games_available": 9,
                "season": {"rushingAttempts": 115, "receivingTargets": 25,
                           "rushingTouchdowns": 5},
            }
        }
        research = {
            "rosters": {"Away Team": players[:3], "Home Team": players[3:]},
            "injury_statuses": [], "verified_player_prop_markets": quotes,
        }
        def pick(player, point, prob=65):
            return {
                "market_type": "Player Prop", "player_or_team": player,
                "bet_description": f"Over {point:g} receiving yards",
                "market_line": f"Over {point:g} (-110) — DraftKings",
                "play_threshold": f"Over {point:g}",
                "estimated_hit_probability": prob, "confidence": 6,
                "statistical_rationale": "Strong volume and a matchup-based edge.",
                "risk_factors": "Defensive adjustments.", "data_quality": "Moderate",
            }
        data = {
            "picks": [pick(players[i], 40.5 + i) for i in range(3)],
            "sneaky_props": [], "touchdown_scorers": [],
            "summary": "Matchup summary unavailable from this AI provider.",
            "game_script": "Balanced offenses drive the matchup.",
            "data_gaps": [],
        }
        answer = {
            "picks": [pick("Player D", 43.5), pick("Player E", 44.5)],
            "sneaky_props": [pick("Player F", 45.5, prob=58)],
            "touchdown_scorers": [{
                "player": "Player A", "team": "Away Team",
                "estimated_td_probability": 50, "confidence": 6,
                "rationale": "5 rushing TDs over 9 games and high carry volume.",
                "risk_factors": "Touchdown variance.",
            }],
            "matchup_summary": "The away team has reliable offensive usage while "
                "the home team is defending multiple receiving threats.",
        }
        with patch("ai_analysis.generate_json", return_value=(answer, "nvidia")) as generate:
            _recover_verified_props_from_focused_review(
                data, shortlist, research, usage, teams, "nvidia"
            )
        generate.assert_called_once()
        self.assertEqual(len(data["picks"]), 5)
        self.assertEqual([p["player_or_team"] for p in data["picks"][:3]], players[:3])
        self.assertEqual(len(data["sneaky_props"]), 1)
        self.assertEqual(data["sneaky_props"][0]["player_or_team"], "Player F")
        self.assertEqual(len(data["touchdown_scorers"]), 1)
        self.assertIn("+140", data["touchdown_scorers"][0]["market_line"])
        self.assertIn("reliable offensive usage", data["summary"])
        request = generate.call_args.args[1]
        self.assertIn('"existing_verified_prop_count":3', request)
        self.assertEqual(data["td_quote_diagnostics"]["eligible_players"], 1)


if __name__ == "__main__":
    unittest.main()
