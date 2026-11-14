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

    def test_player_research_and_injuries_precede_team_statistics(self):
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
            research.index("ESPN CURRENT-SEASON TEAM STATISTICS"),
            research.index("ESPN GAME SUMMARY INJURY DATA"),
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

        from live_research import _player_gamelog, _team_depth_chart, _team_roster_players

        self.assertEqual(_team_statistics.cache_info().currsize, 1)
        self.assertEqual(_core_team_injuries.cache_info().currsize, 1)

        reset_research_cache()

        self.assertEqual(_team_statistics.cache_info().currsize, 0)
        self.assertEqual(_team_roster_players.cache_info().currsize, 0)
        self.assertEqual(_team_depth_chart.cache_info().currsize, 0)
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

    def test_player_gamelog_joins_stat_arrays_to_recent_game_metadata(self):
        from live_research import _normalize_player_gamelog

        payload = {
            "names": ["passingYards", "passingTouchdowns", "rushingYards"],
            "labels": ["YDS", "TD", "RUSH YDS"],
            "displayNames": ["Passing Yards", "Passing TD", "Rushing Yards"],
            "events": {
                "g1": {
                    "week": 9, "gameDate": "2026-11-08", "atVs": "vs",
                    "gameResult": "W", "score": "24-17",
                    "opponent": {"displayName": "Rams"},
                },
                "g2": {
                    "week": 8, "gameDate": "2026-11-01", "atVs": "@",
                    "gameResult": "L", "score": "20-27",
                    "opponent": {"displayName": "Eagles"},
                },
            },
            "seasonTypes": [{
                "categories": [{
                    "displayName": "Regular Season Stats",
                    "events": [
                        {"eventId": "g1", "stats": ["274", "2", "35"]},
                        {"eventId": "g2", "stats": ["217", "1", "23"]},
                    ],
                    "totals": ["491", "3", "58"],
                }],
            }],
            "filters": [{"unneeded": "metadata"}],
        }
        result = _normalize_player_gamelog(payload)
        self.assertEqual(result["games_available"], 2)
        self.assertEqual(result["recent_games"][0]["opponent"], "Rams")
        self.assertEqual(result["recent_games"][0]["stats"]["passingYards"], "274")
        self.assertEqual(
            result["season_totals"]["Regular Season Stats"]["passingTouchdowns"],
            "3",
        )
        self.assertNotIn("filters", result)

    def test_player_gamelog_keeps_only_five_most_recent_games(self):
        from live_research import _normalize_player_gamelog

        events = {
            f"g{i}": {"week": i, "gameDate": f"2026-10-{i:02d}", "opponent": {"displayName": f"Team {i}"}}
            for i in range(1, 9)
        }
        payload = {
            "names": ["receivingYards"], "labels": ["YDS"],
            "displayNames": ["Receiving Yards"], "events": events,
            "seasonTypes": [{
                "categories": [{
                    "displayName": "Regular Season Stats",
                    "events": [{"eventId": f"g{i}", "stats": [str(i * 10)]} for i in range(1, 9)],
                    "totals": ["360"],
                }],
            }],
        }
        result = _normalize_player_gamelog(payload)
        self.assertEqual(len(result["recent_games"]), 5)
        self.assertEqual(result["recent_games"][0]["week"], 8)
        self.assertEqual(result["games_available"], 8)

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
            {"id": "1", "name": "Quarterback One", "position": "QB"},
            {"id": "2", "name": "RB1", "position": "RB"},
            {"id": "3", "name": "RB2", "position": "RB"},
            {"id": "4", "name": "Wideout One", "position": "WR"},
            {"id": "5", "name": "Tight End One", "position": "TE"},
            {"id": "6", "name": "K", "position": "K"},
        ]
        self.assertEqual(
            [player["name"] for player in _select_research_players(roster)],
            ["Quarterback One", "RB1", "RB2", "Wideout One", "Tight End One"],
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
            "live_research._team_depth_chart", return_value={}
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
        self.assertIn("usage_and_recent_stats", result["research_text"])
        self.assertNotIn("season_game_log", result["research_text"])
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
            "live_research._team_depth_chart", return_value={}
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


    def test_depth_chart_extractor_orders_players_by_position_rank(self):
        from live_research import _extract_depth_chart_ids

        result = _extract_depth_chart_ids({
            "depthCharts": [{
                "positions": {
                    "quarterback": {
                        "position": {"abbreviation": "QB"},
                        "athletes": [
                            {"rank": 2, "athlete": {"id": "qb2"}},
                            {"rank": 1, "athlete": {"id": "qb1"}},
                        ],
                    },
                    "runningBack": {
                        "position": {"abbreviation": "RB"},
                        "athletes": [
                            {"rank": 1, "athlete": {"id": "rb1"}},
                        ],
                    },
                },
            }],
        })
        self.assertEqual(result, {"QB": ["qb1", "qb2"], "RB": ["rb1"]})

    def test_player_selection_uses_depth_chart_and_current_roster_ids(self):
        from live_research import _select_research_players

        roster = [
            {"id": "qb2", "name": "Backup QB", "position": "QB"},
            {"id": "rb1", "name": "Starting RB", "position": "RB"},
            {"id": "qb1", "name": "Starting QB", "position": "QB"},
            {"id": "wr1", "name": "Starting WR", "position": "WR"},
            {"id": "te1", "name": "Starting TE", "position": "TE"},
        ]
        chart = {
            "depthCharts": [{
                "positions": {
                    "quarterback": {
                        "position": {"abbreviation": "QB"},
                        "athletes": [
                            {"rank": 1, "athlete": {"id": "qb1"}},
                            {"rank": 2, "athlete": {"id": "qb2"}},
                        ],
                    },
                    "runningBack": {
                        "position": {"abbreviation": "RB"},
                        "athletes": [{"rank": 1, "athlete": {"id": "rb1"}}],
                    },
                    "wideReceiver": {
                        "position": {"abbreviation": "WR"},
                        "athletes": [{"rank": 1, "athlete": {"id": "wr1"}}],
                    },
                    "tightEnd": {
                        "position": {"abbreviation": "TE"},
                        "athletes": [{"rank": 1, "athlete": {"id": "te1"}}],
                    },
                },
            }],
        }
        selected = _select_research_players(roster, chart)
        self.assertEqual(
            [player["name"] for player in selected],
            ["Starting QB", "Starting RB", "Starting WR", "Starting TE"],
        )

    def test_player_selection_includes_multiple_running_backs_and_receivers(self):
        from live_research import _select_research_players

        roster = [
            {"id": "qb1", "name": "QB1", "position": "QB"},
            {"id": "rb1", "name": "RB1", "position": "RB"},
            {"id": "rb2", "name": "RB2", "position": "RB"},
            {"id": "rb3", "name": "RB3", "position": "RB"},
            {"id": "wr1", "name": "WR1", "position": "WR"},
            {"id": "wr2", "name": "WR2", "position": "WR"},
            {"id": "wr3", "name": "WR3", "position": "WR"},
            {"id": "te1", "name": "TE1", "position": "TE"},
        ]
        positions = {}
        for position, ids in {
            "QB": ["qb1"], "RB": ["rb1", "rb2", "rb3"],
            "WR": ["wr1", "wr2", "wr3"], "TE": ["te1"],
        }.items():
            positions[position.lower()] = {
                "position": {"abbreviation": position},
                "athletes": [
                    {"rank": rank, "athlete": {"id": athlete_id}}
                    for rank, athlete_id in enumerate(ids, 1)
                ],
            }
        selected = _select_research_players(
            roster, {"depthCharts": [{"positions": positions}]}
        )
        self.assertEqual(
            [player["name"] for player in selected],
            ["QB1", "RB1", "RB2", "WR1", "WR2", "WR3", "TE1"],
        )

    def test_player_gamelog_compaction_keeps_role_relevant_usage_only(self):
        from live_research import _compact_player_gamelog

        compact = _compact_player_gamelog({
            "games_available": 5,
            "season_totals": {"Regular Season Stats": {
                "receivingTargets": "42", "receptions": "31",
                "receivingYards": "390", "receivingTouchdowns": "3",
                "rushingAttempts": "2", "rushingYards": "8",
                "passingAttempts": "0", "interceptions": "0",
                "longReception": "55",
            }},
            "recent_games": [
                {"week": week, "date": f"2026-11-{week:02d}",
                 "opponent": "Opponent", "result": "W",
                 "stats": {"receivingTargets": "8", "receptions": "6",
                           "receivingYards": "75", "receivingTouchdowns": "1",
                           "longReception": "30"}}
                for week in range(1, 6)
            ],
        }, "WR")
        self.assertEqual(compact["usage_status"], "established_volume")
        self.assertEqual(compact["season"]["receivingTargets"], "42")
        self.assertNotIn("passingAttempts", compact["season"])
        self.assertNotIn("longReception", compact["recent"][0]["stats"])
        self.assertEqual(len(compact["recent"]), 3)


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
        self.assertEqual(result["injury_statuses"][0]["player"], "Core Injury Player")
        self.assertEqual(result["injury_statuses"][0]["status"], "Out")


    def test_core_injuries_searches_beyond_first_twelve_records(self):
        _core_team_injuries.cache_clear()
        items = [{"$ref": f"active-record-{index}"} for index in range(12)]
        items.append({"$ref":"jacobs-record"})
        responses = [{"items":items}]
        responses.extend({"status":"Active"} for _ in range(12))
        responses.extend([
            {
                "status":"Out","date":"2026-11-13","shortComment":"Finger surgery",
                "athlete":{"$ref":"jacobs-athlete"},
            },
            {"displayName":"Josh Jacobs","position":{"abbreviation":"RB"}},
        ])
        with patch("live_research._get_json", side_effect=responses) as get_json:
            result = _core_team_injuries("123")
        self.assertEqual([item["player"] for item in result], ["Josh Jacobs"])
        self.assertEqual(result[0]["status"], "Out")
        self.assertEqual(get_json.call_count, 15)
        _core_team_injuries.cache_clear()


    def test_select_research_players_fills_secondary_roles_from_roster(self):
        from live_research import _select_research_players

        roster = [
            {"id":"1","name":"Quarterback One","position":"QB"},
            {"id":"2","name":"RB One","position":"RB"},
            {"id":"3","name":"RB Two","position":"RB"},
            {"id":"4","name":"WR One","position":"WR"},
            {"id":"5","name":"WR Two","position":"WR"},
            {"id":"6","name":"Tight End One","position":"TE"},
        ]
        depth = {
            "depthCharts":[{
                "positions":{
                    "QB":{"position":{"abbreviation":"QB"},"athletes":[{"rank":1,"athlete":{"id":"1"}}]},
                    "RB":{"position":{"abbreviation":"RB"},"athletes":[{"rank":1,"athlete":{"id":"2"}}]},
                    "WR":{"position":{"abbreviation":"WR"},"athletes":[{"rank":1,"athlete":{"id":"4"}}]},
                    "TE":{"position":{"abbreviation":"TE"},"athletes":[{"rank":1,"athlete":{"id":"6"}}]},
                }
            }]
        }
        selected = _select_research_players(roster, depth)
        self.assertEqual(
            [player["name"] for player in selected],
            ["Quarterback One","RB One","RB Two","WR One","WR Two","Tight End One"],
        )


    def test_placeholder_depth_chart_label_does_not_block_roster_receiver(self):
        from live_research import _select_research_players

        roster = [
            {"id":"qb","name":"Quarterback One","position":"QB"},
            {"id":"rb","name":"Running Back One","position":"RB"},
            {"id":"wr1","name":"Jordan Addison","position":"WR"},
            {"id":"wr2","name":"Justin Jefferson","position":"WR"},
            {"id":"te","name":"Tight End One","position":"TE"},
        ]
        chart = {"depthCharts":[{
            "positions":{
                "QB":{"position":{"abbreviation":"QB"},"athletes":[{"rank":1,"athlete":{"id":"qb"}}]},
                "RB":{"position":{"abbreviation":"RB"},"athletes":[{"rank":1,"athlete":{"id":"rb"}}]},
                "WR":{"position":{"abbreviation":"WR"},"athletes":[
                    {"rank":1,"athlete":{"id":"wr1"}},
                    {"rank":2,"athlete":{"id":"placeholder"}},
                ]},
                "TE":{"position":{"abbreviation":"TE"},"athletes":[{"rank":1,"athlete":{"id":"te"}}]},
            }
        }]}
        roster_with_placeholder = roster + [
            {"id":"placeholder","name":"Wide Receiver","position":"WR"}
        ]
        selected = _select_research_players(roster_with_placeholder, chart)
        self.assertIn("Justin Jefferson", [item["name"] for item in selected])
        self.assertNotIn("Wide Receiver", [item["name"] for item in selected])

    def test_compact_injury_prompt_limits_long_report_payload(self):
        from live_research import _compact_injury_prompt_reports

        reports = [
            {"player":f"Player {i}","position":"LB","status":"Out",
             "report_date":f"2026-11-{i:02d}","comment":"x"*200,
             "injury_details":{"type":"Knee"}}
            for i in range(1,12)
        ]
        compact = _compact_injury_prompt_reports(reports, limit=8)
        self.assertEqual(len(compact), 8)
        self.assertLess(len(str(compact)), len(str(reports)))
        self.assertEqual(compact[0]["player"], "Player 11")


    def test_prompt_trimming_keeps_both_teams_player_logs_before_injuries(self):
        from ai_analysis import _bounded_research_text

        research = "\n\n".join([
            "INDEPENDENT LIVE NFL RESEARCH",
            "CURRENT ESPN ACTIVE TEAM ROSTER — Minnesota Vikings: " + ("R"*2500),
            "ESPN MATCHUP, PLAYER, INJURY, MARKET AND RECENT-GAME DATA: " + ("S"*2000),
            "ESPN CURRENT-SEASON PLAYER GAME LOGS — Minnesota Vikings: " + ("M"*5000),
            "ESPN CURRENT-SEASON PLAYER GAME LOGS — Green Bay Packers: " + ("G"*5000),
            "ESPN CURRENT-SEASON TEAM STATISTICS — Minnesota Vikings: " + ("T"*1000),
            "ESPN CURRENT PLAYER INJURIES — Minnesota Vikings: " + ("I"*5000),
        ])
        with patch("ai_analysis.MAX_RESEARCH_PROMPT_CHARS", 15000):
            prompt, truncated = _bounded_research_text(research)
        self.assertTrue(truncated)
        self.assertIn("PLAYER GAME LOGS — Minnesota Vikings", prompt)
        self.assertIn("PLAYER GAME LOGS — Green Bay Packers", prompt)
        self.assertNotIn("CURRENT ESPN ACTIVE TEAM ROSTER", prompt)


if __name__ == "__main__":
    unittest.main()
