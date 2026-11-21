import unittest

from main import _best_overall_bets


class BestOverallBetsTests(unittest.TestCase):
    def test_ranks_recommendations_across_games_and_bet_types(self):
        games = [
            {"Away Team": "Away A", "Home Team": "Home A", "Game Date (ET)": "Sunday"},
            {"Away Team": "Away B", "Home Team": "Home B", "Game Date (ET)": "Monday"},
        ]
        analyses = [
            {"picks": [{
                "market_type": "Player Prop",
                "player_or_team": "Player A",
                "bet_description": "Anytime Touchdown",
                "market_line": "Yes (+150) — FanDuel",
                "play_threshold": "Anytime",
                "estimated_hit_probability": 48,
                "confidence": 8,
                "data_quality": "Strong",
                "statistical_rationale": "Consistent red-zone role",
                "risk_factors": "Team may spread scoring",
            }]},
            {"picks": [{
                "market_type": "Game Prop",
                "player_or_team": "Home B",
                "bet_description": "Home B -2.5",
                "market_line": "-2.5",
                "play_threshold": "-2.5",
                "estimated_hit_probability": 55,
                "confidence": 9,
                "data_quality": "Moderate",
                "statistical_rationale": "Line matchup advantage",
                "risk_factors": "Price not checked",
            }]},
        ]

        ranked = _best_overall_bets(games, analyses)

        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0]["player_or_team"], "Home B")
        self.assertEqual(ranked[0]["verification"], "Confirm current sportsbook line")
        self.assertEqual(ranked[1]["player_or_team"], "Player A")
        self.assertEqual(ranked[1]["verification"], "Sportsbook quote verified")
        self.assertEqual([item["rank"] for item in ranked], [1, 2])

    def test_shortlist_is_capped_at_ten(self):
        games = [{"Away Team": "Away", "Home Team": "Home"}]
        picks = [{
            "market_type": "Game Prop",
            "player_or_team": f"Team {i}",
            "bet_description": f"Bet {i}",
            "market_line": "Not verified",
            "estimated_hit_probability": 50,
            "confidence": 5,
            "data_quality": "Moderate",
        } for i in range(12)]

        ranked = _best_overall_bets(games, [{"picks": picks}])

        self.assertEqual(len(ranked), 10)
        self.assertEqual([item["rank"] for item in ranked], list(range(1, 11)))


if __name__ == "__main__":
    unittest.main()
