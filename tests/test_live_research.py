import json
import unittest
from unittest.mock import patch

from live_research import (
    _compact_pickcenter,
    _compact_team_stats,
    _compact_tree,
    _core_team_injuries,
    collect_game_research,
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
                "shortComment": "Full participant in practice",
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
        self.assertEqual(get_json.call_count, 3)

        _core_team_injuries.cache_clear()

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
