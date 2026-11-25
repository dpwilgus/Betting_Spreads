import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import sportsbook_odds


class OddsApiMarketTests(unittest.TestCase):
    def test_pass1_includes_anytime_touchdown_market(self):
        self.assertIn("player_anytime_td", sportsbook_odds.PASS1_MARKETS)

    def test_pass2_excludes_markets_we_will_not_bet(self):
        forbidden = {
            "player_sacks",
            "player_solo_tackles",
            "player_tackles_assists",
            "player_kicking_points",
            "player_field_goals",
        }
        self.assertTrue(forbidden.isdisjoint(sportsbook_odds.PASS2_MARKETS))
        self.assertIn("h2h_q1", sportsbook_odds.PASS2_MARKETS)
        self.assertIn("spreads_h1", sportsbook_odds.PASS2_MARKETS)
        self.assertIn("totals_h1", sportsbook_odds.PASS2_MARKETS)

    def test_pass2_includes_first_touchdown_scorer(self):
        self.assertIn("player_1st_td", sportsbook_odds.PASS2_MARKETS)
        self.assertEqual(sportsbook_odds.PASS2_TD_MARKETS, ("player_1st_td",))

    def test_first_td_market_is_labeled_and_parsed(self):
        event = {
            "id": "odds-event-first-td",
            "bookmakers": [{
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [{
                    "key": "player_1st_td",
                    "outcomes": [{
                        "name": "Yes",
                        "description": "Jordan Addison",
                        "price": 650,
                    }],
                }],
            }],
        }
        quotes = sportsbook_odds._parse_quotes(
            event, "2026-11-14T15:01:00+00:00"
        )
        self.assertEqual(quotes[0]["market"], "first touchdown scorer")
        self.assertEqual(quotes[0]["player"], "Jordan Addison")
        self.assertEqual(quotes[0]["price"], "+650")

    def test_first_td_fetch_requires_researched_scoring_role(self):
        no_role = (
            'ESPN CURRENT-SEASON PLAYER GAME LOGS — Team: '
            '[{"player":"Receiver","position":"WR","season_game_log":'
            '{"games_available":8,"season_totals":{"Regular Season Stats":'
            '{"receivingTargets":"20","receivingTouchdowns":"0"}}}}]'
        )
        strong_role = (
            'ESPN CURRENT-SEASON PLAYER GAME LOGS — Team: '
            '[{"player":"Receiver","position":"WR","season_game_log":'
            '{"games_available":8,"season_totals":{"Regular Season Stats":'
            '{"receivingTargets":"64","receivingTouchdowns":"5"}}}}]'
        )
        self.assertFalse(sportsbook_odds.research_supports_first_td(no_role))
        self.assertTrue(sportsbook_odds.research_supports_first_td(strong_role))

    def test_parse_anytime_td_quote_preserves_player_book_and_price(self):
        event = {
            "id": "odds-event-1",
            "bookmakers": [{
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [{
                    "key": "player_anytime_td",
                    "last_update": "2026-11-14T15:00:00Z",
                    "outcomes": [{
                        "name": "Yes",
                        "description": "Gavin Bartholomew",
                        "price": 250,
                    }],
                }],
            }],
        }
        quotes = sportsbook_odds._parse_quotes(
            event, "2026-11-14T15:01:00+00:00"
        )
        self.assertEqual(len(quotes), 1)
        quote = quotes[0]
        self.assertEqual(quote["player"], "Gavin Bartholomew")
        self.assertEqual(quote["market"], "anytime touchdown")
        self.assertEqual(quote["line"], "Yes")
        self.assertEqual(quote["price"], "+250")
        self.assertEqual(quote["sportsbook"], "DraftKings")

    def test_parse_anytime_td_when_player_is_in_outcome_name(self):
        event = {
            "id": "odds-event-2",
            "bookmakers": [{
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [{
                    "key": "player_anytime_td",
                    "outcomes": [{
                        "name": "Gavin Bartholomew",
                        "description": "Yes",
                        "price": 275,
                    }],
                }],
            }],
        }
        quotes = sportsbook_odds._parse_quotes(
            event, "2026-11-14T15:01:00+00:00"
        )
        self.assertEqual(quotes[0]["player"], "Gavin Bartholomew")
        self.assertEqual(quotes[0]["line"], "Yes")
        self.assertEqual(quotes[0]["price"], "+275")

    def test_parse_full_game_moneyline_spread_and_total_quotes(self):
        event = {
            "id": "odds-game-lines",
            "bookmakers": [{
                "key": "draftkings", "title": "DraftKings",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Baltimore Ravens", "price": -190},
                    ]},
                    {"key": "spreads", "outcomes": [
                        {"name": "Los Angeles Chargers", "point": 4.5, "price": -110},
                    ]},
                    {"key": "totals", "outcomes": [
                        {"name": "Under", "point": 47.5, "price": -105},
                    ]},
                ],
            }],
        }
        quotes = sportsbook_odds._parse_quotes(event, "2026-11-16T16:00:00+00:00")
        by_market = {quote["market_key"]: quote for quote in quotes}
        self.assertEqual(by_market["h2h"]["player"], "Baltimore Ravens")
        self.assertEqual(by_market["spreads"]["line"], "Los Angeles Chargers +4.5")
        self.assertEqual(by_market["spreads"]["market_type"], "game")
        self.assertEqual(by_market["totals"]["line"], "Under 47.5")
        self.assertEqual(by_market["totals"]["player"], "Game Total")

    def test_event_market_discovery_limits_odds_request_to_listed_markets(self):
        game = {
            "Away Team": "Los Angeles Chargers",
            "Home Team": "Baltimore Ravens",
            "event_start_utc": "2026-11-16T01:15:00Z",
        }
        events = [{
            "id": "event-verified-markets",
            "away_team": "Los Angeles Chargers",
            "home_team": "Baltimore Ravens",
            "commence_time": "2026-11-16T01:15:00Z",
        }]
        discovery = {
            "bookmakers": [{
                "key": "draftkings",
                "markets": [
                    {"key": "h2h"}, {"key": "spreads"},
                    {"key": "player_pass_yds"}, {"key": "player_anytime_td"},
                ],
            }],
        }
        odds = {
            "id": "event-verified-markets",
            "bookmakers": [{
                "key": "draftkings", "title": "DraftKings",
                "markets": [
                    {"key": "h2h", "outcomes": [
                        {"name": "Baltimore Ravens", "price": -190},
                    ]},
                    {"key": "spreads", "outcomes": [
                        {"name": "Los Angeles Chargers", "point": 4.5, "price": -110},
                    ]},
                    {"key": "player_pass_yds", "outcomes": [
                        {"name": "Over", "description": "Justin Herbert", "point": 205.5, "price": -115},
                    ]},
                ],
            }],
        }
        responses = [
            (events, {}),
            (discovery, {}),
            (odds, {"x-requests-used": "4", "x-requests-remaining": "199", "x-requests-last": "3"}),
        ]
        with patch.object(sportsbook_odds, "get_api_key", return_value="test-key"), \
             patch.object(sportsbook_odds, "_load_fresh_cache", return_value=None), \
             patch.object(sportsbook_odds, "_known_remaining_credits", return_value=None), \
             patch.object(sportsbook_odds, "_save_quota_state"), \
             patch.object(sportsbook_odds, "_save_cache"), \
             patch.object(sportsbook_odds, "_request_json", side_effect=responses) as request:
            result = sportsbook_odds.fetch_event_quotes(
                game,
                ("h2h", "spreads", "totals", "player_pass_yds", "player_anytime_td"),
            )
        self.assertEqual(request.call_count, 3)
        requested = request.call_args_list[2].args[1]["markets"]
        self.assertEqual(requested, "h2h,spreads,player_pass_yds,player_anytime_td")
        self.assertEqual(len(result["quotes"]), 3)
        self.assertEqual(result["requested_markets"], [
            "h2h", "spreads", "player_pass_yds", "player_anytime_td",
        ])
        self.assertEqual(
            len([quote for quote in result["quotes"] if quote["market_type"] == "game"]),
            2,
        )
        self.assertEqual(
            len([quote for quote in result["quotes"] if quote["market_type"] == "player"]),
            1,
        )

    def test_quote_summary_combines_sides_and_uses_best_book_price(self):
        quotes = [
            {
                "player": "Justin Herbert", "market": "passing yards",
                "market_key": "player_pass_yds", "market_type": "player",
                "side": "Over", "line": "Over 205.5", "point": 205.5,
                "price": "-115", "sportsbook": "DraftKings",
                "retrieved_at": "2026-11-16T17:00:00+00:00",
            },
            {
                "player": "Justin Herbert", "market": "passing yards",
                "market_key": "player_pass_yds", "market_type": "player",
                "side": "Over", "line": "Over 205.5", "point": 205.5,
                "price": "-105", "sportsbook": "FanDuel",
                "retrieved_at": "2026-11-16T17:00:00+00:00",
            },
            {
                "player": "Justin Herbert", "market": "passing yards",
                "market_key": "player_pass_yds", "market_type": "player",
                "side": "Under", "line": "Under 205.5", "point": 205.5,
                "price": "-110", "sportsbook": "DraftKings",
                "retrieved_at": "2026-11-16T17:00:00+00:00",
            },
        ]
        summary = sportsbook_odds.summarize_quotes(quotes)
        self.assertIn("Over 205.5 -105@FanDuel", summary)
        self.assertIn("Under 205.5 -110@DraftKings", summary)
        self.assertEqual(summary.count("Justin Herbert | passing yards"), 1)

    def test_event_matching_uses_both_teams(self):
        game = {
            "Away Team": "Green Bay Packers",
            "Home Team": "Chicago Bears",
            "event_start_utc": "2026-11-15T18:00:00Z",
        }
        events = [{
            "id": "event-1",
            "away_team": "Green Bay Packers",
            "home_team": "Chicago Bears",
            "commence_time": "2026-11-15T18:00:00Z",
        }, {
            "id": "event-2",
            "away_team": "Chicago Bears",
            "home_team": "Green Bay Packers",
            "commence_time": "2026-11-15T18:00:00Z",
        }]
        match = sportsbook_odds._find_event(game, events)
        self.assertEqual(match["id"], "event-1")


if __name__ == "__main__":
    unittest.main()

class OddsApiKeyPromptTests(unittest.TestCase):
    def test_cli_prompt_is_used_when_gui_is_disabled(self):
        from unittest.mock import patch

        with patch.object(sportsbook_odds, "get_api_key", return_value=""), \
             patch.object(sportsbook_odds, "_prompt_for_api_key_cli", return_value=True) as prompt:
            self.assertTrue(sportsbook_odds.ensure_api_key_configured(use_gui=False))
        prompt.assert_called_once_with()

    def test_existing_key_skips_prompt(self):
        from unittest.mock import patch

        with patch.object(sportsbook_odds, "get_api_key", return_value="already-set"), \
             patch.object(sportsbook_odds, "_prompt_for_api_key_cli") as prompt:
            self.assertTrue(sportsbook_odds.ensure_api_key_configured(use_gui=False))
        prompt.assert_not_called()

