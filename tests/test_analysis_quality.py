import unittest

from ai_analysis import (
    _deduplicate_correlated_game_picks,
    _validate_analysis,
)


class AnalysisQualityTests(unittest.TestCase):
    def test_same_team_moneyline_and_spread_keep_only_higher_confidence_pick(self):
        moneyline = {
            "rank": 1, "market_type": "Game Prop", "player_or_team": "Detroit Lions",
            "bet_description": "Lions moneyline", "market_line": "Not verified",
            "play_threshold": "Lions to win", "estimated_hit_probability": 72,
            "confidence": 7, "data_quality": "Strong",
        }
        spread = {
            "rank": 2, "market_type": "Game Prop", "player_or_team": "Lions",
            "bet_description": "Lions -2.5 spread", "market_line": "-2.5",
            "play_threshold": "Win by 3+", "estimated_hit_probability": 72,
            "confidence": 8, "data_quality": "Strong",
        }
        result, removed = _deduplicate_correlated_game_picks(
            [moneyline, spread], ("New England Patriots", "Detroit Lions")
        )
        self.assertTrue(removed)
        self.assertEqual(len(result), 1)
        self.assertIn("spread", result[0]["bet_description"])
        self.assertEqual(result[0]["rank"], 1)

    def test_unrelated_game_and_player_props_are_preserved(self):
        picks = [
            {
                "rank": 1, "market_type": "Game Prop", "player_or_team": "Detroit Lions",
                "bet_description": "Lions moneyline", "market_line": "Not verified",
                "play_threshold": "Lions to win", "estimated_hit_probability": 70,
                "confidence": 7, "data_quality": "Strong",
            },
            {
                "rank": 2, "market_type": "Player Prop", "player_or_team": "Amon-Ra St. Brown",
                "bet_description": "Receiving yards over", "market_line": "Not verified",
                "play_threshold": "80+ yards", "estimated_hit_probability": 58,
                "confidence": 7, "data_quality": "Moderate",
            },
        ]
        result, removed = _deduplicate_correlated_game_picks(
            picks, ("New England Patriots", "Detroit Lions")
        )
        self.assertFalse(removed)
        self.assertEqual(len(result), 2)

    def _td_analysis(self, confidence=6, availability="Questionable"):
        return {
            "summary": "Test", "game_script": "Test",
            "picks": [], "injury_impact": [], "data_gaps": [],
            "touchdown_scorers": [{
                "rank": 1, "player": "Rhamondre Stevenson",
                "team": "New England Patriots",
                "availability": availability,
                "availability_evidence": "Latest practice report: limited participation",
                "estimated_td_probability": 25, "confidence": confidence,
                "rationale": "Goal-line role when active",
                "risk_factors": "Questionable status creates availability risk",
            }],
        }

    def test_questionable_td_candidate_is_allowed_with_capped_confidence(self):
        _validate_analysis(
            self._td_analysis(),
            rosters={"New England Patriots": ["Rhamondre Stevenson"]},
            team_names=("New England Patriots", "Detroit Lions"),
        )

    def test_questionable_td_candidate_cannot_have_high_confidence(self):
        with self.assertRaisesRegex(RuntimeError, "confidence of 6/10 or lower"):
            _validate_analysis(
                self._td_analysis(confidence=8),
                rosters={"New England Patriots": ["Rhamondre Stevenson"]},
                team_names=("New England Patriots", "Detroit Lions"),
            )

    def test_doubtful_td_candidate_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "unavailable or doubtful"):
            _validate_analysis(
                self._td_analysis(availability="Doubtful"),
                rosters={"New England Patriots": ["Rhamondre Stevenson"]},
                team_names=("New England Patriots", "Detroit Lions"),
            )


if __name__ == "__main__":
    unittest.main()
