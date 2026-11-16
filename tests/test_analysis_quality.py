import unittest

from ai_analysis import (
    ANALYSIS_SCHEMA,
    _bounded_research_text,
    _apply_independent_injury_statuses,
    _deduplicate_correlated_game_picks,
    _separate_unverified_player_props,
    _underdog_format_instruction,
    _validate_analysis,
)


class AnalysisQualityTests(unittest.TestCase):
    def _prop_pick(self, line):
        return {
            "market_type": "Player Prop",
            "player_or_team": "Jahmyr Gibbs",
            "bet_description": f"Over {line} rushing yards",
            "market_line": "Not verified",
            "play_threshold": f"Over {line}",
        }

    def _gibbs_quote(self, point, side="Over", market_key="player_rush_yds"):
        return {
            "player": "Jahmyr Gibbs", "market": "rushing yards",
            "market_key": market_key, "side": side, "point": point,
            "line": f"{side} {point}", "price": "-110",
            "sportsbook": "DraftKings", "source_url": "https://the-odds-api.com/",
            "retrieved_at": "2026-11-15T12:00:00+00:00",
        }


    def test_provider_specific_research_budget_is_respected(self):
        research = (
            "HEADER: current game\n\n"
            "INJURIES: current injury report\n\n"
            "PLAYER LOGS: current player usage\n\n"
            "TEAM STATS: current team metrics\n\n"
            + ("OPTIONAL HISTORICAL RECAP: old game details\n\n" * 200)
        )
        bounded, truncated = _bounded_research_text(research, max_chars=1200)
        self.assertLessEqual(len(bounded), 1200)
        self.assertFalse(truncated)
        self.assertIn("HEADER", bounded)
        self.assertIn("INJURIES:", bounded)
        self.assertIn("PLAYER LOGS", bounded)

    def test_player_prop_quote_with_different_threshold_is_rejected_not_model_only(self):
        data = {"picks": [self._prop_pick("85.5")], "sneaky_props": []}
        research = {"verified_player_prop_markets": [self._gibbs_quote("89.5")]}
        gaps = _separate_unverified_player_props(data, research)
        self.assertEqual(data["picks"], [])
        self.assertEqual(data["model_only_picks"], [])
        self.assertIn("the sportsbook offers 89.5", gaps[0])
        self.assertIn("line was not silently changed", gaps[0])

    def test_player_prop_requires_exact_side_and_market_key(self):
        data = {"picks": [self._prop_pick("89.5")], "sneaky_props": []}
        research = {"verified_player_prop_markets": [
            self._gibbs_quote("89.5", side="Under")
        ]}
        _separate_unverified_player_props(data, research)
        self.assertEqual(data["picks"], [])
        self.assertEqual(len(data["model_only_picks"]), 1)

        data = {"picks": [self._prop_pick("89.5")], "sneaky_props": []}
        research = {"verified_player_prop_markets": [
            self._gibbs_quote("89.5", market_key="player_reception_yds")
        ]}
        _separate_unverified_player_props(data, research)
        self.assertEqual(data["picks"], [])
        self.assertEqual(len(data["model_only_picks"]), 1)

    def test_exact_player_prop_quote_remains_verified(self):
        data = {"picks": [self._prop_pick("89.5")], "sneaky_props": []}
        research = {"verified_player_prop_markets": [self._gibbs_quote("89.5")]}
        _separate_unverified_player_props(data, research)
        self.assertEqual(len(data["picks"]), 1)
        self.assertEqual(data["picks"][0]["market_line"], "Over 89.5 (-110) — DraftKings")
        self.assertEqual(data["model_only_picks"], [])

    def test_completely_empty_model_response_is_rejected(self):
        analysis = {
            "summary": "No supported opportunities found.",
            "game_script": "Insufficient evidence to assess this matchup.",
            "picks": [],
            "sneaky_props": [],
            "touchdown_scorers": [],
            "injury_impact": [],
            "underdog_analysis": [],
            "data_gaps": ["No player game logs or market prices were available."],
        }
        with self.assertRaisesRegex(RuntimeError, "contains no usable matchup analysis"):
            _validate_analysis(
                analysis,
                team_names=("San Francisco 49ers", "Dallas Cowboys"),
            )

    def test_missing_underdog_assessment_does_not_discard_otherwise_valid_picks(self):
        pick = {
            "rank": 1, "market_type": "Game Prop",
            "player_or_team": "Detroit Lions",
            "bet_description": "Lions moneyline", "market_line": "Not verified",
            "play_threshold": "Lions to win", "estimated_hit_probability": 55,
            "confidence": 5, "statistical_rationale": "Supported by matchup evidence",
            "risk_factors": "Price must be checked", "data_quality": "Moderate",
        }
        analysis = {
            "summary": "Test", "game_script": "Test",
            "picks": [pick], "touchdown_scorers": [], "injury_impact": [],
            "data_gaps": [],
        }
        _validate_analysis(
            analysis, team_names=("New England Patriots", "Detroit Lions"),
            require_underdog=True, expected_underdog="New England Patriots",
            expected_spread=2.5,
        )
        self.assertEqual(analysis["picks"], [pick])
        self.assertEqual(analysis["underdog_analysis"], [])
        self.assertTrue(any("Underdog assessment unavailable" in gap for gap in analysis["data_gaps"]))

    def test_underdog_format_instruction_uses_matchup_team_and_positive_spread(self):
        instruction = _underdog_format_instruction("Los Angeles Chargers", 4.5)
        self.assertIn('"underdog":"Los Angeles Chargers"', instruction)
        self.assertIn('"spread":"+4.5"', instruction)
        self.assertIn('"cover_probability":NUMBER', instruction)
        self.assertIn('"upset_probability":NUMBER', instruction)
        self.assertIn("do not copy example values", instruction)

    def test_research_budget_preserves_live_sportsbook_quotes(self):
        research = "\n\n".join([
            "INDEPENDENT LIVE NFL RESEARCH: matchup context",
            "ODDS API MARKET DISCOVERY: available event markets=h2h,spreads,player_pass_yds",
            "THE ODDS API VERIFIED PLAYER-PROP QUOTES: Justin Herbert | passing yards | Over 205.5 | -115 | DraftKings",
            "OPTIONAL HISTORICAL RECAP: " + ("old recap " * 500),
        ])
        bounded, truncated = _bounded_research_text(research, max_chars=500)
        self.assertIn("THE ODDS API VERIFIED PLAYER-PROP QUOTES", bounded)
        self.assertIn("Justin Herbert", bounded)
        self.assertNotIn("OPTIONAL HISTORICAL RECAP", bounded)
        self.assertFalse(truncated)

    def test_analysis_schema_does_not_require_underdog_section(self):
        self.assertNotIn("underdog_analysis", ANALYSIS_SCHEMA["required"])
        self.assertEqual(
            ANALYSIS_SCHEMA["properties"]["underdog_analysis"]["minItems"], 0
        )

    def test_underdog_format_instruction_handles_missing_spread(self):
        instruction = _underdog_format_instruction(None, None)
        self.assertIn("otherwise use an empty array", instruction)

    def test_multiple_underdog_rows_keep_one_normalized_row(self):
        analysis = {
            "summary": "Test", "game_script": "Test", "picks": [],
            "touchdown_scorers": [], "injury_impact": [], "data_gaps": [],
            "underdog_analysis": [
                {"underdog": "New England Patriots", "spread": "+2.5",
                 "cover_probability": "54", "upset_probability": "38",
                 "recommendation": "monitor", "rationale": "Pressure matchup",
                 "risk_factors": "Turnover variance", "evidence_quality": "medium"},
                {"underdog": "Detroit Lions", "spread": "-2.5",
                 "cover_probability": 60, "upset_probability": 40,
                 "recommendation": "Pass", "rationale": "Other row",
                 "risk_factors": "Risk", "evidence_quality": "Strong"},
            ],
        }
        _validate_analysis(analysis, team_names=("New England Patriots", "Detroit Lions"))
        self.assertEqual(len(analysis["underdog_analysis"]), 1)
        self.assertEqual(analysis["underdog_analysis"][0]["recommendation"], "Monitor")
        self.assertEqual(analysis["underdog_analysis"][0]["evidence_quality"], "Moderate")


    def test_favorite_spread_row_is_not_mislabeled_as_underdog(self):
        analysis = {
            "summary": "Test", "game_script": "Test", "picks": [],
            "touchdown_scorers": [], "injury_impact": [], "data_gaps": [],
            "underdog_analysis": [
                {"underdog": "Detroit Lions", "spread": "-2.5",
                 "cover_probability": 60, "upset_probability": 40,
                 "recommendation": "Pass", "rationale": "Favorite row",
                 "risk_factors": "Risk", "evidence_quality": "Strong"},
                {"underdog": "New England Patriots", "spread": "+2.5",
                 "cover_probability": 54, "upset_probability": 38,
                 "recommendation": "Monitor", "rationale": "Dog row",
                 "risk_factors": "Risk", "evidence_quality": "Moderate"},
            ],
        }
        _validate_analysis(analysis, team_names=("New England Patriots", "Detroit Lions"))
        self.assertEqual(analysis["underdog_analysis"][0]["underdog"], "New England Patriots")

    def test_invalid_underdog_row_is_dropped_without_rejecting_matchup(self):
        analysis = {
            "summary": "Test", "game_script": "Test",
            "picks": [{
                "rank": 1, "market_type": "Game Prop",
                "player_or_team": "Detroit Lions",
                "bet_description": "Lions moneyline",
                "market_line": "Not verified",
                "play_threshold": "Lions to win",
                "estimated_hit_probability": 60, "confidence": 6,
                "statistical_rationale": "Supported by matchup evidence",
                "risk_factors": "Price must be checked",
                "data_quality": "Moderate",
            }],
            "touchdown_scorers": [], "injury_impact": [], "data_gaps": [],
            "underdog_analysis": [{
                "underdog": "Not a matchup team", "spread": "+2.5",
                "cover_probability": 54, "upset_probability": 38,
                "recommendation": "Maybe", "rationale": "Reason",
                "risk_factors": "Risk", "evidence_quality": "Moderate",
            }],
        }
        _validate_analysis(analysis, team_names=("New England Patriots", "Detroit Lions"))
        self.assertEqual(analysis["underdog_analysis"], [])
        self.assertEqual(len(analysis["picks"]), 1)
        self.assertTrue(any("Underdog assessment unavailable" in gap for gap in analysis["data_gaps"]))

    def test_invalid_underdog_only_response_is_rejected(self):
        analysis = {
            "summary": "Test", "game_script": "Test", "picks": [],
            "touchdown_scorers": [], "injury_impact": [], "data_gaps": [],
            "underdog_analysis": [{
                "underdog": "Not a matchup team", "spread": "+2.5",
                "cover_probability": 54, "upset_probability": 38,
                "recommendation": "Maybe", "rationale": "Reason",
                "risk_factors": "Risk", "evidence_quality": "Moderate",
            }],
        }
        with self.assertRaisesRegex(RuntimeError, "contains no usable matchup analysis"):
            _validate_analysis(
                analysis, team_names=("New England Patriots", "Detroit Lions")
            )

    def test_underdog_analysis_is_validated(self):
        analysis = {
            "summary": "Balanced matchup", "game_script": "Competitive game",
            "picks": [{
                "rank": 1, "market_type": "Game Prop",
                "player_or_team": "Detroit Lions",
                "bet_description": "Lions moneyline",
                "market_line": "Not verified",
                "play_threshold": "Lions to win",
                "estimated_hit_probability": 60, "confidence": 6,
                "statistical_rationale": "Supported by matchup evidence",
                "risk_factors": "Price must be checked",
                "data_quality": "Moderate",
            }],
            "touchdown_scorers": [], "injury_impact": [],
            "data_gaps": [],
            "underdog_analysis": [{
                "underdog": "New England Patriots", "spread": "+2.5",
                "cover_probability": 54, "upset_probability": 38,
                "recommendation": "Monitor", "rationale": "Pass rush creates matchup pressure",
                "risk_factors": "Turnover variance", "evidence_quality": "Moderate",
            }],
        }
        _validate_analysis(
            analysis, team_names=("New England Patriots", "Detroit Lions")
        )
        self.assertEqual(len(analysis["underdog_analysis"]), 1)
        self.assertEqual(len(analysis["picks"]), 1)

        analysis["underdog_analysis"][0]["upset_probability"] = 120
        _validate_analysis(
            analysis, team_names=("New England Patriots", "Detroit Lions")
        )
        self.assertEqual(analysis["underdog_analysis"], [])
        self.assertEqual(len(analysis["picks"]), 1)
        self.assertTrue(any(
            "Underdog assessment unavailable" in gap
            for gap in analysis["data_gaps"]
        ))

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

    def test_questionable_td_candidate_gets_missing_availability_warning(self):
        analysis = self._td_analysis()
        analysis["touchdown_scorers"][0]["risk_factors"] = "Limited practice"
        _validate_analysis(
            analysis,
            rosters={"New England Patriots": ["Rhamondre Stevenson"]},
            team_names=("New England Patriots", "Detroit Lions"),
        )
        self.assertIn(
            "Questionable",
            analysis["touchdown_scorers"][0]["risk_factors"],
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

    def test_validator_allows_more_than_five_total_picks(self):
        analysis = {
            "summary": "Test", "game_script": "Test",
            "touchdown_scorers": [], "injury_impact": [], "data_gaps": [],
            "picks": [{
                "rank": rank, "market_type": "Player Prop",
                "player_or_team": f"Player {rank}", "bet_description": "Over",
                "market_line": "Not verified", "play_threshold": "Model threshold",
                "estimated_hit_probability": 55, "confidence": 6,
                "statistical_rationale": "Recent usage supports the estimate",
                "risk_factors": "Sample size", "data_quality": "Moderate",
            } for rank in range(1, 8)],
        }
        _validate_analysis(analysis)

    def test_independent_out_status_removes_player_prop_and_td_candidate(self):
        analysis = {
            "picks": [{
                "market_type": "Player Prop", "player_or_team": "Josh Jacobs",
                "confidence": 8, "risk_factors": "Finger injury",
            }],
            "touchdown_scorers": [{
                "player": "Josh Jacobs", "confidence": 9,
                "risk_factors": "Strong goal-line role",
            }],
        }
        gaps = _apply_independent_injury_statuses(
            analysis,
            [{"player": "Josh Jacobs", "team": "Green Bay Packers", "status": "Out"}],
        )
        self.assertEqual(analysis["picks"], [])
        self.assertEqual(analysis["touchdown_scorers"], [])
        self.assertEqual(len(gaps), 2)

    def test_questionable_player_prop_is_capped_and_flagged(self):
        analysis = {
            "picks": [{
                "market_type": "Player Prop", "player_or_team": "Josh Jacobs",
                "confidence": 9, "risk_factors": "Finger injury",
            }],
            "touchdown_scorers": [],
        }
        _apply_independent_injury_statuses(
            analysis, [{"player": "Josh Jacobs", "status": "Questionable"}]
        )
        self.assertEqual(analysis["picks"][0]["confidence"], 6)
        self.assertIn("Questionable", analysis["picks"][0]["risk_factors"])

    def test_prompt_trimming_interleaves_player_logs_from_both_teams(self):
        research = [
            "INDEPENDENT LIVE NFL RESEARCH",
            "ESPN CURRENT-SEASON PLAYER GAME LOGS — San Francisco 49ers (starter): "
            + ("A" * 1000),
            "ESPN CURRENT-SEASON PLAYER GAME LOGS — San Francisco 49ers (backup): "
            + ("B" * 1000),
            "ESPN CURRENT-SEASON PLAYER GAME LOGS — Dallas Cowboys (starter): "
            + ("C" * 1000),
            "ESPN CURRENT-SEASON PLAYER GAME LOGS — Dallas Cowboys (backup): "
            + ("D" * 1000),
        ]
        prompt, truncated = _bounded_research_text(
            "\n\n".join(research), max_chars=2300
        )

        self.assertTrue(truncated)
        self.assertIn("San Francisco 49ers", prompt)
        self.assertIn("Dallas Cowboys", prompt)
        self.assertIn("A" * 1000, prompt)
        self.assertIn("C" * 1000, prompt)
        self.assertNotIn("B" * 1000, prompt)
        self.assertNotIn("D" * 1000, prompt)


if __name__ == "__main__":
    unittest.main()
