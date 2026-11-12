import json
import unittest
from unittest.mock import patch

from live_research import (
    _compact_pickcenter,
    _compact_summary,
    _compact_team_stats,
    _compact_tree,
    _core_team_injuries,
    collect_game_research,
    reset_research_cache,
)


class LiveResearchTests(unittest.TestCase):

    def test_compact_tree_removes_unneeded_fields_and_limits_lists(self):
        data = {
            "name": "Example",
            "irrelevant": "Remove this",
            "items": [{"name": f"Player {i}"} for i in range(12)],
        }

        result = _compact_tree(data)

        self.assertEqual(result["name"], "Example")
        self.assertNotIn("irrelevant", result)
        self.assertEqual(len(result["items"]), 8)

    def test_pickcenter_preserves_market_quotes_and_timestamp(self):
        result = _compact_pickcenter(
            {"pickcenter": [{
                "provider": {"name": "Test Sportsbook"},
                "pointSpread": {
                    "away": {
                        "open": {"line": -3.5, "odds": -110},
                        "close": {"line": -4, "odds": -115},
                    }
                },
                "total": {
                    "over": {"close": {"line": 45.5, "odds": -105}}
                },
            }]},
            "2026-11-12T04:00:00+00:00",
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["provider"], "Test Sportsbook")
        self.assertEqual(result[0]["spread"]["away"]["open"]["line"], -3.5)
        self.assertEqual(result[0]["spread"]["away"]["close"]["line"], -4)
        self.assertEqual(result[0]["retrieved_at_utc"], "2026-11-12T04:00:00+00:00")

    def test_summary_drops_oversized_historical_sections_before_market_data(self):
        with patch("live_research.SUMMARY_MAX_CHARS", 500):
            self._assert_oversized_summary_is_compacted()

    def _assert_oversized_summary_is_compacted(self):
        nested = {"name": "x" * 220}
        for _ in range(4):
            nested = {"name": "Recent game", "items": [nested.copy() for _ in range(8)]}
        payload = {
            "header": {
                "id": "event-1",
                "competitions": [{
                    "date": "2026-11-15T18:00Z",
                    "competitors": [
                        {"team": {"id": "1", "displayName": "Away"}},
                        {"team": {"id": "2", "displayName": "Home"}},
                    ],
                }],
            },
            "lastFiveGames": [nested.copy() for _ in range(8)],
            "againstTheSpread": [
                {"name": "ATS record", "summary": "y" * 220}
                for _ in range(8)
            ],
            "pickcenter": [{
                "provider": {"name": "Verified Book"},
                "pointSpread": {
                    "away": {"close": {"line": -3.5, "odds": -110}}
                },
            }],
        }
        result = json.loads(_compact_summary(payload, "2026-11-12T23:00:00+00:00"))

        self.assertIn("header", result)
        self.assertIn("pickcenter_markets", result)
        self.assertEqual(result["pickcenter_markets"][0]["provider"], "Verified Book")
        self.assertIn("recent_games", result["omitted_oversized_sections"])
        self.assertLessEqual(
            len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))),
            12000,
        )

    def test_team_stats_keep_relevant_current_season_fields(self):
        result = _compact_team_stats({
            "results": {
                "stats": {
                    "displayName": "Example Team",
                    "categories": [
                        {
                            "name": "passing",
                            "stats": [
                                {"name": "completionPct", "displayValue": "65%"},
                                {"name": "passingYardsPerGame", "displayValue": "240"},
                                {"name": "unneededField", "displayValue": "999"},
                            ],
                        }
                    ],
                }
            }
        })

        self.assertEqual(result["team"], "Example Team")
        self.assertEqual(
            len(result["categories"]["passing"]), 2
        )
        self.assertNotIn(
            "unneededField",
            [item["name"] for item in result["categories"]["passing"]],
        )

    def test_core_injuries_preserve_practice_status_conflict(self):
        _core_team_injuries.cache_clear()

        responses = [
            {"items": [{"$ref": "injury-record-url"}]},
            {
                "status": "Questionable",
                "date": "2026-11-11",
                "shortComment": "Full participant in practice " + ("detail " * 80),
                "athlete": {"$ref": "athlete-url"},
                "details": {"type": "Hamstring", "location": "Leg"},
            },
            {
                "displayName": "Example Player",
                "position": {"abbreviation": "WR"},
            },
        ]

        with patch(
            "live_research._get_json",
            side_effect=responses,
        ) as get_json:
            result = _core_team_injuries("123")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["player"], "Example Player")
        self.assertEqual(result[0]["position"], "WR")
        self.assertEqual(result[0]["status"], "Questionable")
        self.assertTrue(result[0]["practice_status_conflict"])
        self.assertEqual(len(result[0]["comment"]), 180)
        self.assertEqual(get_json.call_count, 3)

        _core_team_injuries.cache_clear()

    def test_current_injuries_precede_team_stats_in_research_prompt(self):
        summary = {
            "header": {
                "id": "event-1",
                "competitions": [{
                    "date": "2026-11-15T18:00Z",
                    "competitors": [
                        {"team": {"id": "1", "displayName": "Away"}},
                        {"team": {"id": "2", "displayName": "Home"}},
                    ],
                }],
            },
            "injuries": [{
                "athlete": {"displayName": "Injured Player"},
                "status": "Questionable",
                "details": {"type": "Ankle"},
            }],
        }
        with patch("live_research._get_json", return_value=summary), patch(
            "live_research._team_statistics",
            side_effect=[{"team": "Away"}, {"team": "Home"}],
        ), patch(
            "live_research._team_roster_players", return_value=tuple()
        ), patch(
            "live_research._core_team_injuries",
            side_effect=[
                [{"player": "Away Player", "status": "Out"}],
                [{"player": "Home Player", "status": "Questionable"}],
            ],
        ):
            result = collect_game_research({
                "event_id": "event-1",
                "Away Team": "Away",
                "Home Team": "Home",
                "away_team_id": "1",
                "home_team_id": "2",
            })

        research = result["research_text"]
        self.assertLess(
            research.index("ESPN CURRENT PLAYER INJURIES"),
            research.index("ESPN CURRENT-SEASON TEAM STATISTICS"),
        )
        self.assertLess(
            research.index("ESPN GAME SUMMARY INJURY DATA"),
            research.index("ESPN CURRENT-SEASON TEAM STATISTICS"),
        )
        self.assertIn("MARKET DATA RULE", research)

    def test_team_statistics_are_cached_across_matchups(self):
        from live_research import _team_statistics

        _team_statistics.cache_clear()
        payload = {
            "results": {
                "stats": {
                    "displayName": "Example Team",
                    "categories": [{
                        "name": "passing",
                        "stats": [{
                            "name": "passingYardsPerGame",
                            "displayValue": "240",
                        }],
                    }],
                }
            }
        }

        with patch("live_research._get_json", return_value=payload) as get_json:
            first = _team_statistics("123")
            second = _team_statistics("123")

        self.assertEqual(first, second)
        self.assertEqual(first["team"], "Example Team")
        get_json.assert_called_once()
        _team_statistics.cache_clear()

    def test_reset_research_cache_clears_team_and_injury_data(self):
        from live_research import _team_statistics

        with patch("live_research._get_json", side_effect=[
            {"results": {"stats": {"displayName": "Team", "categories": []}}},
            {"items": []},
        ]):
            _team_statistics("123")
            _core_team_injuries("123")

        from live_research import _player_gamelog, _team_roster_players

        self.assertEqual(_team_statistics.cache_info().currsize, 1)
        self.assertEqual(_core_team_injuries.cache_info().currsize, 1)

        reset_research_cache()

        self.assertEqual(_team_statistics.cache_info().currsize, 0)
        self.assertEqual(_team_roster_players.cache_info().currsize, 0)
        self.assertEqual(_player_gamelog.cache_info().currsize, 0)
        self.assertEqual(_core_team_injuries.cache_info().currsize, 0)


    def test_roster_player_extractor_keeps_ids_and_inherited_positions(self):
        from live_research import _extract_roster_players

        players = _extract_roster_players({
            "athletes": [{
                "position": {"abbreviation": "WR"},
                "items": [
                    {"id": "101", "fullName": "Receiver One"},
                    {"id": "102", "displayName": "Receiver Two"},
                ],
            }, {
                "position": {"abbreviation": "QB"},
                "items": [{"id": "201", "fullName": "Quarterback One"}],
            }],
        })

        self.assertEqual(
            players,
            [
                {"id": "101", "name": "Receiver One", "position": "WR"},
                {"id": "102", "name": "Receiver Two", "position": "WR"},
                {"id": "201", "name": "Quarterback One", "position": "QB"},
            ],
        )

    def test_player_gamelog_is_compacted_and_cached(self):
        from live_research import _player_gamelog

        _player_gamelog.cache_clear()
        payload = {
            "labels": ["OPP", "PASS YDS", "RUSH YDS"],
            "events": [{"name": f"Week {week}", "stats": [250, 12]} for week in range(10)],
            "news": [{"headline": "Discarded"}],
        }
        with patch("live_research._get_json", return_value=payload) as get_json:
            first = _player_gamelog("12345", 2026)
            second = _player_gamelog("12345", 2026)

        self.assertEqual(first, second)
        self.assertEqual(len(first["events"]), 6)
        self.assertNotIn("news", first)
        get_json.assert_called_once()
        _player_gamelog.cache_clear()

    def test_select_research_players_limits_to_skill_position_leaders(self):
        from live_research import _select_research_players

        roster = [
            {"id": "1", "name": "QB", "position": "QB"},
            {"id": "2", "name": "RB1", "position": "RB"},
            {"id": "3", "name": "RB2", "position": "RB"},
            {"id": "4", "name": "WR", "position": "WR"},
            {"id": "5", "name": "TE", "position": "TE"},
            {"id": "6", "name": "K", "position": "K"},
        ]
        self.assertEqual(
            [player["name"] for player in _select_research_players(roster)],
            ["QB", "RB1", "WR", "TE"],
        )


    def test_collect_research_adds_player_game_logs_and_source_links(self):
        summary = {
            "header": {
                "competitions": [{
                    "competitors": [
                        {"team": {"id": "1", "displayName": "Away"}},
                        {"team": {"id": "2", "displayName": "Home"}},
                    ]
                }]
            }
        }
        roster = (
            {"id": "101", "name": "Away QB", "position": "QB"},
            {"id": "102", "name": "Away RB", "position": "RB"},
            {"id": "103", "name": "Away WR", "position": "WR"},
            {"id": "104", "name": "Away TE", "position": "TE"},
        )
        home_roster = tuple(
            {**player, "id": str(int(player["id"]) + 100), "name": player["name"].replace("Away", "Home")}
            for player in roster
        )
        with patch("live_research._get_json", return_value=summary), patch(
            "live_research._team_roster_players",
            side_effect=[roster, home_roster],
        ), patch(
            "live_research._player_gamelog",
            return_value={"events": [{"name": "Week 10", "stats": [250, 2]}]},
        ), patch(
            "live_research._team_statistics",
            side_effect=[{"team": "Away"}, {"team": "Home"}],
        ), patch(
            "live_research._core_team_injuries",
            return_value=[],
        ):
            result = collect_game_research({
                "event_id": "event-1",
                "Away Team": "Away",
                "Home Team": "Home",
                "away_team_id": "1",
                "home_team_id": "2",
                "Game Date (ET)": "Sunday",
            })

        self.assertIn("ESPN CURRENT-SEASON PLAYER GAME LOGS", result["research_text"])
        self.assertIn("Away QB", result["research_text"])
        self.assertIn("Home TE", result["research_text"])
        player_sources = [
            source for source in result["sources"]
            if source["title"].startswith("ESPN player game log")
        ]
        self.assertEqual(len(player_sources), 8)
        self.assertTrue(all("/gamelog?season=2026" in item["url"] for item in player_sources))


    def test_repeated_player_log_errors_stop_extra_requests_for_the_slate(self):
        from live_research import PLAYER_GAMELOG_FAILURE_LIMIT

        reset_research_cache()
        summary = {
            "header": {
                "competitions": [{
                    "competitors": [
                        {"team": {"id": "1", "displayName": "Away"}},
                        {"team": {"id": "2", "displayName": "Home"}},
                    ]
                }]
            }
        }
        roster = tuple(
            {"id": str(i), "name": f"Player {i}", "position": pos}
            for i, pos in enumerate(("QB", "RB", "WR", "TE"), 1)
        )
        with patch("live_research._get_json", return_value=summary), patch(
            "live_research._team_roster_players",
            side_effect=[roster, roster],
        ), patch(
            "live_research._player_gamelog",
            side_effect=RuntimeError("ESPN player log unavailable"),
        ) as get_gamelog, patch(
            "live_research._team_statistics",
            return_value={"team": "Team"},
        ), patch(
            "live_research._core_team_injuries",
            return_value=[],
        ):
            result = collect_game_research({
                "event_id": "event-1",
                "Away Team": "Away",
                "Home Team": "Home",
                "away_team_id": "1",
                "home_team_id": "2",
            })

        self.assertEqual(get_gamelog.call_count, PLAYER_GAMELOG_FAILURE_LIMIT)
        self.assertIn("skipped after repeated ESPN player-log errors", result["research_text"])
        reset_research_cache()

    def test_summary_injuries_are_retained_when_only_one_team_has_core_injuries(self):
        summary = {
            "header": {
                "competitions": [{
                    "competitors": [
                        {"team": {"id": "1", "displayName": "Away"}},
                        {"team": {"id": "2", "displayName": "Home"}},
                    ]
                }]
            },
            "injuries": [{
                "displayName": "Summary Injury Player",
                "status": {"type": "questionable"},
            }],
        }

        core_injury = [{
            "player": "Core Injury Player",
            "status": "Out",
        }]

        with patch(
            "live_research._get_json",
            side_effect=[
                summary,
                {"results": {"stats": {"displayName": "Away", "categories": []}}},
                {"results": {"stats": {"displayName": "Home", "categories": []}}},
            ],
        ), patch(
            "live_research._team_roster_players", return_value=tuple()
        ), patch(
            "live_research._core_team_injuries",
            side_effect=[core_injury, []],
        ):
            result = collect_game_research({
                "event_id": "401873052",
                "Away Team": "Away",
                "Home Team": "Home",
                "Game Date (ET)": "Sunday",
                "Betting Line": "Unavailable",
            })

        self.assertIn("Core Injury Player", result["research_text"])
        self.assertIn("Summary Injury Player", result["research_text"])


if __name__ == "__main__":
    unittest.main()
