import unittest
from unittest.mock import patch

from openpyxl import Workbook

from ai_analysis import (
    _apply_independent_injury_statuses,
    _bounded_research_text,
    _build_player_research_summary,
    _filter_game_level_picks,
    _filter_injury_impacts,
    _filter_player_props_by_usage,
    _move_sneaky_game_picks,
    _remove_contradictory_period_game_picks,
    _separate_unverified_player_props,
    _validate_analysis,
)
from live_research import _adam_schefter_news, _compact_team_stats
from main import _write_matchup_sheet


class OutputQualityRuleTests(unittest.TestCase):
    def test_yardage_thresholds_far_above_season_and_recent_average_are_rejected(self):
        data = {"picks": [
            {
                "market_type": "Player Prop",
                "player_or_team": "Justin Herbert",
                "bet_description": "Over 235.5 passing yards",
                "play_threshold": "Over 235.5 passing yards",
            },
            {
                "market_type": "Player Prop",
                "player_or_team": "Derrick Henry",
                "bet_description": "Over 85.5 rushing yards",
                "play_threshold": "Over 85.5 rushing yards",
            },
        ]}
        usage = {
            "justinherbert": {
                "position": "QB", "games_available": 10,
                "season": {"passingYards": "1990"},
                "recent": [{"stats": {"passingYards": "205"}} for _ in range(3)],
            },
            "derrickhenry": {
                "position": "RB", "games_available": 10,
                "season": {"rushingYards": "766"},
                "recent": [{"stats": {"rushingYards": "75"}} for _ in range(3)],
            },
        }
        gaps = _filter_player_props_by_usage(data, usage)
        self.assertEqual(data["picks"], [])
        self.assertEqual(len(gaps), 2)
        self.assertIn("season average 199.0", gaps[0])
        self.assertIn("season average 76.6", gaps[1])

    def test_team_unit_injury_impacts_keep_only_key_defensive_stars(self):
        impacts = []
        reports = []
        for player in ("All-Pro Safety", "Ordinary Safety", "Starting Guard"):
            impacts.append({
                "impact_type": "Injury",
                "injured_player": player,
                "team": "Team A",
                "availability": "Out",
                "affected_player": "Team unit",
                "affected_team": "Team A",
                "impact_direction": "Negative",
                "statistical_effect": (
                    "All-Pro safety absence affects secondary" if player == "All-Pro Safety"
                    else "Starter absence affects unit"
                ),
                "prop_market": "Opponent passing yards",
                "evidence": "ESPN injury report",
                "confidence": 8,
                "conditions": "If inactive",
            })
            reports.append({"player": player, "team": "Team A", "status": "Out"})
        data = {"injury_impact": impacts}
        usage = {
            "allprosafety": {"position": "S", "season": {"totalTackles": "42"}},
            "ordinarysafety": {"position": "S", "season": {"totalTackles": "28"}},
            "startingguard": {"position": "G", "season": {}},
        }
        gaps = _filter_injury_impacts(
            data, reports, {"Team A": list(("All-Pro Safety", "Ordinary Safety", "Starting Guard"))},
            ("Team A", "Team B"), player_usage=usage,
        )
        self.assertEqual(len(data["injury_impact"]), 1)
        self.assertEqual(data["injury_impact"][0]["injured_player"], "All-Pro Safety")
        self.assertTrue(any("only key defensive stars" in gap for gap in gaps))

    def test_missing_injury_designation_is_labeled_consistently_not_active(self):
        data = {
            "picks": [{
                "market_type": "Player Prop", "player_or_team": "Player A",
                "confidence": 6, "risk_factors": "Usage volatility",
            }],
            "touchdown_scorers": [{
                "player": "Player A", "confidence": 5, "risk_factors": "Low TD rate",
            }],
        }
        _apply_independent_injury_statuses(data, [])
        pick, scorer = data["picks"][0], data["touchdown_scorers"][0]
        self.assertEqual(pick["availability"], "No injury designation found")
        self.assertEqual(scorer["availability"], "No injury designation found")
        self.assertIn("active status is not confirmed", scorer["availability_evidence"])
        self.assertNotIn("not independently verified", scorer["availability"].casefold())

    def test_explicit_espn_available_status_is_not_rewritten_as_missing(self):
        data = {
            "picks": [{
                "market_type": "Player Prop", "player_or_team": "Player A",
                "confidence": 6, "risk_factors": "Usage volatility",
            }],
            "touchdown_scorers": [{
                "player": "Player A", "confidence": 5, "risk_factors": "TD variance",
            }],
        }
        statuses = [{"player": "Player A", "status": "Active"}]
        _apply_independent_injury_statuses(data, statuses)
        self.assertEqual(data["picks"][0]["availability"], "Available (ESPN injury feed)")
        self.assertEqual(data["touchdown_scorers"][0]["availability"], "Available (ESPN injury feed)")

    def test_other_explicit_injury_status_is_not_called_no_designation(self):
        data = {
            "picks": [{
                "market_type": "Player Prop", "player_or_team": "Player A",
                "confidence": 6, "risk_factors": "Workload",
            }],
            "touchdown_scorers": [{
                "player": "Player A", "confidence": 5, "risk_factors": "TD variance",
            }],
        }
        _apply_independent_injury_statuses(
            data, [{"player": "Player A", "status": "Limited"}]
        )
        self.assertEqual(data["picks"][0]["availability"], "ESPN status: Limited")
        self.assertEqual(data["touchdown_scorers"][0]["availability"], "ESPN status: Limited")

    def test_unverified_sneaky_anytime_td_is_removed(self):
        data = {
            "picks": [],
            "sneaky_props": [{
                "market_type": "Player Prop",
                "player_or_team": "Mark Andrews",
                "bet_description": "Andrews anytime TD scorer",
                "play_threshold": "Anytime TD +180 or better",
            }],
        }
        gaps = _separate_unverified_player_props(data, {"verified_player_prop_markets": []})
        self.assertEqual(data["sneaky_props"], [])
        self.assertTrue(any("Removed unverified sneaky player prop" in gap for gap in gaps))

    def test_sneaky_game_lines_move_into_game_level_picks(self):
        data = {
            "picks": [],
            "sneaky_props": [{
                "market_type": "Game Prop",
                "player_or_team": "Baltimore Ravens",
                "bet_description": "BAL 1st half -2.5 spread",
                "play_threshold": "BAL 1H -2.5 or better",
            }],
        }
        self.assertTrue(_move_sneaky_game_picks(data))
        self.assertEqual(len(data["picks"]), 1)
        self.assertEqual(data["sneaky_props"], [])

    def test_unverified_total_is_removed_when_market_quote_disagrees(self):
        data = {"picks": [{
            "market_type": "Game Prop",
            "player_or_team": "Game",
            "bet_description": "Under 43.5 total points",
            "play_threshold": "Under 43.5",
            "market_line": "Not verified",
        }]}
        gaps = _filter_game_level_picks(
            data,
            {"Betting Line": "BAL -4.5"},
            {"verified_game_markets": [{"total": {"over": {"close": {"line": "21.5"}}}}]},
            ("Los Angeles Chargers", "Baltimore Ravens"),
        )
        self.assertEqual(data["picks"], [])
        self.assertTrue(any("no matching ESPN pickcenter total quote" in gap for gap in gaps))

    def test_team_named_total_is_rejected_even_when_player_or_team_says_game(self):
        data = {"picks": [{
            "market_type": "Game Prop",
            "player_or_team": "Game",
            "bet_description": "Ravens under 43.5 total points",
            "play_threshold": "Under 43.5",
            "market_line": "Not verified",
        }]}
        gaps = _filter_game_level_picks(
            data,
            {"Betting Line": "BAL -4.5"},
            {"verified_game_markets": [{"displayed_total": "43.5"}]},
            ("Los Angeles Chargers", "Baltimore Ravens"),
        )
        self.assertEqual(data["picks"], [])
        self.assertTrue(any("ambiguous team total" in gap for gap in gaps))

    def test_combined_game_total_can_match_exact_pickcenter_quote(self):
        data = {"picks": [{
            "market_type": "Game Prop",
            "player_or_team": "Game",
            "bet_description": "Chargers vs Ravens under 43.5 total points",
            "play_threshold": "Under 43.5",
            "market_line": "Not verified",
        }]}
        _filter_game_level_picks(
            data,
            {"Betting Line": "BAL -4.5"},
            {"verified_game_markets": [{"displayed_total": "43.5"}]},
            ("Los Angeles Chargers", "Baltimore Ravens"),
        )
        self.assertEqual(len(data["picks"]), 1)
        self.assertIn("pickcenter total 43.5", data["picks"][0]["market_line"])

    def test_team_total_is_not_mistaken_for_full_game_total(self):
        data = {"picks": [{
            "market_type": "Game Prop",
            "player_or_team": "Baltimore Ravens",
            "bet_description": "Under 21.5 team total points",
            "play_threshold": "Under 21.5",
            "market_line": "Not verified",
        }]}
        gaps = _filter_game_level_picks(
            data,
            {"Betting Line": "BAL -4.5"},
            {"verified_game_markets": [{"displayed_total": "43.5"}]},
            ("Los Angeles Chargers", "Baltimore Ravens"),
        )
        self.assertEqual(data["picks"], [])
        self.assertTrue(any("ambiguous team total" in gap for gap in gaps))

    def test_scoreboard_spread_is_retained_when_team_and_side_match(self):
        data = {"picks": [{
            "market_type": "Game Prop",
            "player_or_team": "Los Angeles Chargers",
            "bet_description": "LAC +4.5 spread",
            "play_threshold": "LAC +4.5 or better",
            "market_line": "Not verified",
        }]}
        _filter_game_level_picks(
            data, {"Betting Line": "BAL -4.5"}, {"verified_game_markets": []},
            ("Los Angeles Chargers", "Baltimore Ravens"),
        )
        self.assertEqual(len(data["picks"]), 1)
        self.assertIn("ESPN scoreboard", data["picks"][0]["market_line"])

    def test_game_spread_uses_exact_sportsbook_quote_when_available(self):
        quote = {
            "player": "Los Angeles Chargers",
            "market_key": "spreads",
            "market_type": "game",
            "side": "Los Angeles Chargers",
            "point": 4.5,
            "line": "Los Angeles Chargers +4.5",
            "price": "-110",
            "sportsbook": "DraftKings",
            "source_url": "https://the-odds-api.com/",
            "retrieved_at": "2026-11-16T17:00:00+00:00",
        }
        data = {"picks": [{
            "market_type": "Game Prop",
            "player_or_team": "Los Angeles Chargers",
            "bet_description": "LAC +4.5 spread",
            "play_threshold": "LAC +4.5",
            "market_line": "Not verified",
        }]}
        _filter_game_level_picks(
            data, {"Betting Line": "BAL -4.5"},
            {"verified_sportsbook_game_markets": [quote]},
            ("Los Angeles Chargers", "Baltimore Ravens"),
        )
        self.assertEqual(len(data["picks"]), 1)
        self.assertIn("DraftKings", data["picks"][0]["market_line"])
        self.assertNotIn("ESPN scoreboard", data["picks"][0]["market_line"])
        self.assertEqual(data["picks"][0]["market_source"], quote["source_url"])

    def test_unquoted_period_game_line_is_limited_and_kept_in_game_picks(self):
        data = {"picks": [{
            "market_type": "Game Prop",
            "player_or_team": "Baltimore Ravens",
            "bet_description": "BAL 1H -2.5",
            "play_threshold": "BAL -2.5",
            "market_line": "Not verified",
            "confidence": 8, "data_quality": "Strong",
            "risk_factors": "First-half matchup edge",
        }]}
        gaps = _filter_game_level_picks(
            data, {"Betting Line": "BAL -4.5"}, {"verified_game_markets": []},
            ("Los Angeles Chargers", "Baltimore Ravens"),
        )
        self.assertEqual(len(data["picks"]), 1)
        self.assertEqual(data["picks"][0]["confidence"], 5)
        self.assertEqual(data["picks"][0]["data_quality"], "Limited")
        self.assertIn("NOT VERIFIED", data["picks"][0]["market_line"])
        self.assertTrue(any("Limited model idea" in gap for gap in gaps))

    def test_opposite_period_spread_is_detected_without_spread_word(self):
        data = {"picks": [
            {
                "market_type": "Game Prop", "player_or_team": "Los Angeles Chargers",
                "bet_description": "LAC +4.5", "play_threshold": "LAC +4.5",
            },
            {
                "market_type": "Game Prop", "player_or_team": "Baltimore Ravens",
                "bet_description": "BAL 1H -2.5", "play_threshold": "BAL -2.5",
            },
        ]}
        gaps = _remove_contradictory_period_game_picks(
            data, ("Los Angeles Chargers", "Baltimore Ravens")
        )
        self.assertEqual(len(data["picks"]), 1)
        self.assertEqual(data["picks"][0]["player_or_team"], "Los Angeles Chargers")
        self.assertTrue(any("opposite team" in gap for gap in gaps))

    def test_opposite_first_half_spread_is_removed_against_full_game_pick(self):
        data = {"picks": [
            {
                "market_type": "Game Prop", "player_or_team": "Los Angeles Chargers",
                "bet_description": "LAC +4.5 spread", "play_threshold": "LAC +4.5",
            },
            {
                "market_type": "Game Prop", "player_or_team": "Baltimore Ravens",
                "bet_description": "BAL 1st half -2.5 spread", "play_threshold": "BAL -2.5",
            },
        ]}
        gaps = _remove_contradictory_period_game_picks(
            data, ("Los Angeles Chargers", "Baltimore Ravens")
        )
        self.assertEqual(len(data["picks"]), 1)
        self.assertEqual(data["picks"][0]["player_or_team"], "Los Angeles Chargers")
        self.assertTrue(any("opposite team" in gap for gap in gaps))

    def test_missing_underdog_assessment_does_not_discard_valid_spread_analysis(self):
        data = {
            "summary": "Summary", "game_script": "Script",
            "picks": [{
                "market_type": "Game Prop", "player_or_team": "Team A",
                "bet_description": "Team A +3.5 spread", "market_line": "Team B -3.5",
                "play_threshold": "Team A +3.5", "estimated_hit_probability": 52,
                "confidence": 5, "statistical_rationale": "Evidence",
                "risk_factors": "Risk", "data_quality": "Moderate",
            }],
            "touchdown_scorers": [], "sneaky_props": [], "injury_impact": [],
            "underdog_analysis": [],
        }
        _validate_analysis(data, team_names=("Team A", "Team B"), require_underdog=True)
        self.assertEqual(data["underdog_analysis"], [])
        self.assertTrue(any("Underdog assessment unavailable" in gap for gap in data["data_gaps"]))

    def test_underdog_must_match_team_receiving_points(self):
        base = {
            "summary": "Summary", "game_script": "Script",
            "picks": [{
                "market_type": "Game Prop", "player_or_team": "Team A",
                "bet_description": "Team A +3.5 spread", "market_line": "Team B -3.5",
                "play_threshold": "Team A +3.5", "estimated_hit_probability": 52,
                "confidence": 5, "statistical_rationale": "Evidence",
                "risk_factors": "Risk", "data_quality": "Moderate",
            }],
            "touchdown_scorers": [], "sneaky_props": [],
            "injury_impact": [],
            "underdog_analysis": [{
                "underdog": "Team A", "spread": "+3.5",
                "cover_probability": 52, "upset_probability": 34,
                "recommendation": "Monitor", "rationale": "Market is efficient",
                "risk_factors": "Limited edge", "evidence_quality": "Moderate",
            }],
        }
        wrong_team = dict(base)
        _validate_analysis(
            wrong_team, team_names=("Team A", "Team B"),
            require_underdog=True, expected_underdog="Team B",
        )
        self.assertEqual(wrong_team["underdog_analysis"], [])
        self.assertTrue(any("wrong underdog" in gap for gap in wrong_team["data_gaps"]))

        wrong_spread = dict(base)
        wrong_spread["underdog_analysis"] = [{
            **base["underdog_analysis"][0], "underdog": "Team B",
        }]
        _validate_analysis(
            wrong_spread, team_names=("Team A", "Team B"),
            require_underdog=True, expected_underdog="Team B", expected_spread=4.5,
        )
        self.assertEqual(wrong_spread["underdog_analysis"], [])
        self.assertTrue(any("spread mismatch" in gap for gap in wrong_spread["data_gaps"]))

        correct = dict(base)
        correct["underdog_analysis"] = [{
            **base["underdog_analysis"][0], "underdog": "Team B",
        }]
        _validate_analysis(
            correct, team_names=("Team A", "Team B"),
            require_underdog=True, expected_underdog="Team B", expected_spread=3.5,
        )
        self.assertEqual(len(correct["underdog_analysis"]), 1)

    def test_player_research_summary_calculates_season_and_recent_averages(self):
        usage = {
            "quarterbackone": {
                "position": "QB", "games_available": 2,
                "season": {
                    "passingYards": "500", "passingAttempts": "70",
                    "passingTouchdowns": "4", "rushingYards": "20",
                    "rushingAttempts": "4", "rushingTouchdowns": "1",
                },
                "recent": [
                    {"stats": {"passingYards": "250", "passingAttempts": "35",
                               "passingTouchdowns": "2", "rushingYards": "10",
                               "rushingAttempts": "2", "rushingTouchdowns": "1"}},
                    {"stats": {"passingYards": "250", "passingAttempts": "35",
                               "passingTouchdowns": "2", "rushingYards": "10",
                               "rushingAttempts": "2", "rushingTouchdowns": "0"}},
                ],
            },
        }
        result = _build_player_research_summary(
            usage, {"Team A": [{"name": "Quarterback One", "position": "QB"}]}
        )[0]
        self.assertEqual(result["passing_yards_per_game"], 250)
        self.assertEqual(result["passing_yards_recent"], 250)
        self.assertEqual(result["passing_attempts_per_game"], 35)
        self.assertEqual(result["passing_tds_per_game"], 2)
        self.assertEqual(result["tds_scored_per_game"], 0.5)

    def test_team_points_per_game_is_derived_from_season_points(self):
        compact = _compact_team_stats({"results": {"stats": {
            "displayName": "Team A",
            "categories": [
                {"name": "general", "stats": [
                    {"name": "gamesPlayed", "displayValue": "10"},
                ]},
                {"name": "scoring", "stats": [
                    {"name": "totalPoints", "displayValue": "210"},
                ]},
            ],
        }}})
        scoring = compact["categories"]["scoring"]
        points_per_game = next(item["value"] for item in scoring if item["name"] == "pointsPerGame")
        self.assertEqual(points_per_game, 21.0)

    def test_player_average_research_is_preserved_when_prompt_is_compacted(self):
        research = (
            "Low-priority historical notes " + ("x" * 500)
            + "\n\nSELECTED PLAYER PER-GAME AVERAGES: "
            + '{"player":"Quarterback One","passing_yards_per_game":199.0}'
        )
        bounded, truncated = _bounded_research_text(research, max_chars=180)
        # Optional historical notes were compacted; the essential player
        # averages survived, so this is compaction rather than essential loss.
        self.assertFalse(truncated)
        self.assertIn("SELECTED PLAYER PER-GAME AVERAGES", bounded)
        self.assertIn("199.0", bounded)
        self.assertIn("[Optional historical matchup recap compacted", bounded)

    def test_adam_schefter_contributor_feed_articles_are_normalized(self):
        payload = {"items": [{
            "headline": "Adam Schefter reports a roster move",
            "description": "A selected player is affected.",
            "published": "2026-11-15T18:00:00Z",
            "links": {"web": {"href": "https://www.espn.com/nfl/story/example"}},
        }]}
        _adam_schefter_news.cache_clear()
        with patch("live_research._get_json", return_value=payload) as request:
            articles = _adam_schefter_news()
        _adam_schefter_news.cache_clear()
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["byline"], "Adam Schefter")
        self.assertEqual(articles[0]["published"], "2026-11-15T18:00:00Z")
        self.assertEqual(articles[0]["url"], "https://www.espn.com/nfl/story/example")
        self.assertIn("contributor=adam-schefter", request.call_args.args[0])

    def test_adam_schefter_falls_back_to_espn_nfl_feed_byline(self):
        fallback_payload = {"articles": [{
            "headline": "Adam Schefter reports a player transaction",
            "description": "The team made a roster move.",
            "published": "2026-11-15T19:00:00Z",
            "byline": "Adam Schefter",
            "links": {"web": {"href": "https://www.espn.com/nfl/story/fallback"}},
        }]}
        _adam_schefter_news.cache_clear()
        with patch(
            "live_research._get_json",
            side_effect=[{"items": []}, fallback_payload],
        ) as request:
            articles = _adam_schefter_news()
        _adam_schefter_news.cache_clear()
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["byline"], "Adam Schefter")
        self.assertEqual(request.call_count, 2)
        self.assertIn("/sports/football/nfl/news", request.call_args_list[1].args[0])

    def test_workbook_removes_usage_table_and_places_averages_beside_picks(self):
        workbook = Workbook()
        workbook.remove(workbook.active)
        game = {
            "Away Team": "Los Angeles Chargers", "Home Team": "Baltimore Ravens",
            "Game Date (ET)": "Monday", "Betting Line": "BAL -4.5",
        }
        analysis = {
            "summary": "Summary", "game_script": "Script",
            "picks": [{
                "market_type": "Player Prop", "player_or_team": "Lamar Jackson",
                "bet_description": "Over 40.5 rushing yards",
                "average_per_game": "Rushing yards: 52.4 season / 48.0 last 3",
                "market_line": "Over 40.5 (-110) — DraftKings",
                "play_threshold": "Over 40.5", "estimated_hit_probability": 61.0,
                "confidence": 7, "statistical_rationale": "Volume supports the line.",
                "risk_factors": "Game script.", "data_quality": "Strong",
            }],
            "model_only_picks": [], "underdog_analysis": [],
            "touchdown_scorers": [{
                "player": "Lamar Jackson", "team": "Baltimore Ravens",
                "availability": "No injury designation found",
                "availability_evidence": "ESPN injury feed.",
                "average_per_game": "Offensive TDs: 0.8 season / 1.0 last 3",
                "estimated_td_probability": 62.0, "confidence": 7,
                "rationale": "Designed runs.", "risk_factors": "Red-zone volume.",
            }],
            "sneaky_props": [], "injury_impact": [], "data_gaps": [], "sources": [],
            "ai_provider": "ensemble: gemini + nvidia", "ai_providers_configured": ["gemini", "nvidia"],
            "research_mode": "test", "research_truncated_for_prompt": False,
            "research_prompt_characters": 1000, "odds_api_status": "test", "odds_api_usage": {},
        }
        _write_matchup_sheet(workbook, game, analysis, set())
        sheet = workbook.worksheets[0]
        row_by_title = {
            sheet.cell(row, 1).value: row
            for row in range(1, sheet.max_row + 1)
            if sheet.cell(row, 1).value
        }
        self.assertNotIn(
            "PLAYER USAGE RESEARCH — PER-GAME AVERAGES (Season average / Last 3 games average)",
            row_by_title,
        )
        self.assertNotIn("PLAYER USAGE RESEARCH", row_by_title)
        verified_header = row_by_title["VERIFIED PLAYER PROPS (SPORTSBOOK LINE REQUIRED)"] + 1
        self.assertEqual(sheet.cell(verified_header, 4).value, "Player Stat Avg/G (Season / Last 3)")
        self.assertEqual(sheet.cell(verified_header + 1, 4).value, "Rushing yards: 52.4 season / 48.0 last 3")
        td_header = row_by_title["ANYTIME TOUCHDOWN SCORERS"] + 1
        self.assertIn("Offensive TDs/G", sheet.cell(td_header, 6).value)
        self.assertEqual(sheet.cell(td_header + 1, 6).value, "Offensive TDs: 0.8 season / 1.0 last 3")
        sneaky = row_by_title["SNEAKY PROPS & LADDER OPPORTUNITIES"]
        injuries = row_by_title["INJURY / NEWS IMPACT ANALYSIS"]
        limitations = row_by_title["DATA LIMITATIONS"]
        self.assertLess(sneaky, injuries)
        self.assertLess(injuries, limitations)


if __name__ == "__main__":
    unittest.main()
