import unittest

from prop_averages import attach_per_game_averages

from ai_analysis import (
    ANALYSIS_SCHEMA,
    _bounded_research_text,
    _build_unquoted_market_watchlist,
    _rank_quoted_player_markets,
    _apply_independent_injury_statuses,
    _deduplicate_correlated_game_picks,
    _separate_unverified_player_props,
    _underdog_format_instruction,
    _merge_provider_analyses,
    _validate_analysis,
)


class AnalysisQualityTests(unittest.TestCase):

    def test_research_quote_block_survives_prompt_trimming(self):
        research = (
            "ODDS QUOTES RETRIEVED: 2026-11-16T20:00:00Z\n"
            "THE ODDS API VERIFIED PLAYER-PROP QUOTES: C.J. Stroud | passing yards | Over 245.5 -110@DraftKings"
            "\n\n" + "OPTIONAL OLD RECAP: " + ("x" * 1200)
        )
        bounded, truncated = _bounded_research_text(research, max_chars=220)
        self.assertIn("ODDS QUOTES RETRIEVED", bounded)
        self.assertIn("DraftKings", bounded)
        self.assertFalse(truncated)

    def test_prop_average_is_stat_specific_and_td_average_is_offensive_tds(self):
        usage = {
            "playera": {
                "games_available": 2,
                "season": {
                    "rushingYards": 100,
                    "rushingTouchdowns": 1,
                    "receivingTouchdowns": 1,
                },
                "recent": [
                    {"stats": {"rushingYards": 40, "rushingTouchdowns": 0, "receivingTouchdowns": 1}},
                    {"stats": {"rushingYards": 60, "rushingTouchdowns": 1, "receivingTouchdowns": 0}},
                ],
            }
        }
        data = {
            "picks": [{
                "market_type": "Player Prop", "player_or_team": "Player A",
                "bet_description": "Over 50.5 rushing yards",
            }],
            "touchdown_scorers": [{"player": "Player A"}],
        }
        attach_per_game_averages(
            data, usage,
            lambda description: "player_rush_yds" if "rushing yards" in description else None,
        )
        self.assertEqual(data["picks"][0]["average_per_game"], "Rushing yards: 50.0 season / 50.0 last 2")
        self.assertEqual(data["touchdown_scorers"][0]["average_per_game"], "Offensive TDs: 1.0 season / 1.0 last 2")

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

    def test_model_ensemble_merges_votes_for_exact_verified_quote(self):
        first = {
            "summary": "Gemini summary", "game_script": "Gemini script",
            "picks": [dict(self._prop_pick("89.5"), estimated_hit_probability=58.0,
                confidence=6, statistical_rationale="Season and recent usage align.",
                risk_factors="Opponent pressure.", data_quality="Moderate")],
            "sneaky_props": [], "touchdown_scorers": [],
            "injury_impact": [], "data_gaps": [],
        }
        second = {
            "summary": "NVIDIA summary", "game_script": "NVIDIA script",
            "picks": [dict(self._prop_pick("89.5"), estimated_hit_probability=64.0,
                confidence=8, statistical_rationale="Volume supports the over.",
                risk_factors="Game-script risk.", data_quality="Strong")],
            "sneaky_props": [], "touchdown_scorers": [],
            "injury_impact": [], "data_gaps": [],
        }
        best_quote = self._gibbs_quote("89.5")
        best_quote["price"] = "-105"
        best_quote["sportsbook"] = "FanDuel"
        merged, primary = _merge_provider_analyses(
            [(first, "gemini"), (second, "nvidia")],
            [self._gibbs_quote("89.5"), best_quote],
        )
        self.assertEqual(primary, "nvidia")
        self.assertEqual(len(merged["picks"]), 1)
        self.assertEqual(merged["picks"][0]["model_votes"], 2)
        self.assertEqual(merged["picks"][0]["estimated_hit_probability"], 61.0)
        self.assertIn("FanDuel", merged["picks"][0]["market_line"])
        self.assertIn("-105", merged["picks"][0]["market_line"])

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

    def test_quoted_shortlist_requires_roster_and_real_line(self):
        quotes = [
            {"player": "Player A", "market_key": "player_rush_yds",
             "side": "Over", "point": 50.5, "price": "-110",
             "sportsbook": "DraftKings", "line": "Over 50.5",
             "retrieved_at": "2026-11-21T12:00:00Z"},
            {"player": "Wrong Team", "market_key": "player_rush_yds",
             "side": "Over", "point": 40.5, "price": "-110",
             "sportsbook": "DraftKings"},
        ]
        usage = {
            "playera": {"games_available": 5, "season": {"rushingYards": 350}},
            "wrongteam": {"games_available": 5, "season": {"rushingYards": 400}},
        }
        result = _rank_quoted_player_markets(
            quotes, usage, {"Jaguars": ["Player A"], "Giants": []},
            ("Jaguars", "Giants"),
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["player"], "Player A")
        self.assertEqual(result[0]["point"], 50.5)
        self.assertEqual(result[0]["season_average"], 70)

    def test_no_odds_watchlist_uses_espn_averages_without_invented_bets(self):
        rows = [
            {"player": "Player A", "position": "RB", "games": 8,
             "rushing_yards_per_game": 71.5, "rushing_yards_recent": 84.0},
            {"player": "Player B", "position": "WR", "games": 8,
             "receiving_yards_per_game": 60.0, "receiving_yards_recent": 55.0},
        ]
        result = _build_unquoted_market_watchlist(
            rows, [{"player": "Player B", "status": "Out"}]
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["player_or_team"], "Player A")
        self.assertIsNone(result[0]["estimated_hit_probability"])
        self.assertIsNone(result[0]["confidence"])
        self.assertIn("NOT VERIFIED", result[0]["market_line"])
        self.assertIn("71.5", result[0]["average_per_game"])
        self.assertIn("Await exact sportsbook line", result[0]["play_threshold"])

    def test_missing_nvidia_narratives_do_not_erase_valid_picks(self):
        analysis = {
            "picks": [{
                "market_type": "Game Prop", "player_or_team": "Houston Texans",
                "bet_description": "Houston moneyline",
                "market_line": "Not verified", "play_threshold": "Houston to win",
                "estimated_hit_probability": 55, "confidence": 5,
                "statistical_rationale": "Example research-based rationale",
                "risk_factors": "Price unavailable", "data_quality": "Limited",
            }],
            "touchdown_scorers": [], "sneaky_props": [],
            "injury_impact": [], "data_gaps": [],
        }
        _validate_analysis(analysis)
        self.assertIn("unavailable", analysis["summary"])
        self.assertIn("unavailable", analysis["game_script"])
        self.assertEqual(len(analysis["picks"]), 1)
        self.assertTrue(any("omitted summary" in gap for gap in analysis["data_gaps"]))

    def test_invalid_pick_probability_does_not_discard_valid_verified_candidate(self):
        valid = {
            "rank": 1, "market_type": "Player Prop",
            "player_or_team": "Jahmyr Gibbs",
            "bet_description": "Over 89.5 rushing yards",
            "market_line": "Over 89.5 (-110) — DraftKings",
            "play_threshold": "Over 89.5", "estimated_hit_probability": 58,
            "confidence": 6, "statistical_rationale": "Usage supports the line",
            "risk_factors": "Game script", "data_quality": "Moderate",
        }
        invalid = dict(valid, player_or_team="Other Player",
                       estimated_hit_probability="not a number")
        analysis = {
            "summary": "Test", "game_script": "Test",
            "picks": [invalid, valid], "sneaky_props": [],
            "touchdown_scorers": [], "injury_impact": [], "data_gaps": [],
        }
        _validate_analysis(analysis)
        self.assertEqual(analysis["picks"], [valid])
        self.assertTrue(any("Removed model pick" in gap for gap in analysis["data_gaps"]))

    def test_all_invalid_picks_still_fail_validation(self):
        analysis = {
            "summary": "Test", "game_script": "Test",
            "picks": [{
                "market_type": "Player Prop", "player_or_team": "Unknown",
                "estimated_hit_probability": "not a number",
            }],
            "sneaky_props": [], "touchdown_scorers": [],
            "injury_impact": [], "data_gaps": [],
        }
        with self.assertRaisesRegex(RuntimeError, "no usable matchup analysis"):
            _validate_analysis(analysis)
        self.assertEqual(analysis["picks"], [])

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

    def test_unsupported_injury_direction_is_dropped_without_rejecting_valid_pick(self):
        pick = {
            "rank": 1, "market_type": "Game Prop",
            "player_or_team": "Houston Texans",
            "bet_description": "Texans moneyline", "market_line": "Not verified",
            "play_threshold": "Texans to win", "estimated_hit_probability": 55,
            "confidence": 5, "statistical_rationale": "Supported by matchup evidence",
            "risk_factors": "Price must be checked", "data_quality": "Moderate",
        }
        analysis = {
            "summary": "Test", "game_script": "Test",
            "picks": [pick], "sneaky_props": [], "touchdown_scorers": [],
            "injury_impact": [{
                "impact_type": "Injury", "injured_player": "Defender A",
                "team": "Indianapolis Colts", "availability": "Out",
                "affected_player": "Receiver B", "affected_team": "Houston Texans",
                "impact_direction": "Boosted", "statistical_effect": "More targets",
                "prop_market": "Receiving yards", "evidence": "Injury report",
                "confidence": 7, "conditions": "If role expands",
            }],
            "underdog_analysis": [], "data_gaps": [],
        }
        _validate_analysis(
            analysis,
            team_names=("Indianapolis Colts", "Houston Texans"),
        )
        self.assertEqual(len(analysis["picks"]), 1)
        self.assertEqual(analysis["injury_impact"], [])
        self.assertTrue(any("unsupported impact direction 'Boosted'" in gap for gap in analysis["data_gaps"]))


    def test_invalid_td_scorer_is_dropped_without_rejecting_valid_pick(self):
        pick = {
            "rank": 1, "market_type": "Game Prop",
            "player_or_team": "Baltimore Ravens",
            "bet_description": "Ravens moneyline",
            "market_line": "Not verified", "play_threshold": "Ravens to win",
            "estimated_hit_probability": 58, "confidence": 6,
            "statistical_rationale": "Supported by matchup evidence",
            "risk_factors": "Confirm price", "data_quality": "Moderate",
        }
        analysis = {
            "summary": "Competitive matchup",
            "game_script": "Baltimore has a modest home-field edge.",
            "picks": [pick], "sneaky_props": [], "touchdown_scorers": [{
                "rank": 1, "player": "Invalid Candidate", "team": "Baltimore Ravens",
                "availability": "Active", "availability_evidence": "Test evidence",
                "estimated_td_probability": 140, "confidence": 11,
                "rationale": "Test", "risk_factors": "Test",
            }],
            "injury_impact": [], "data_gaps": [],
        }
        _validate_analysis(
            analysis, team_names=("Los Angeles Chargers", "Baltimore Ravens")
        )
        self.assertEqual(analysis["picks"], [pick])
        self.assertEqual(analysis["touchdown_scorers"], [])
        self.assertTrue(any(
            "invalid touchdown probability/confidence" in gap
            for gap in analysis["data_gaps"]
        ))

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
