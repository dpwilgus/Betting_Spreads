import unittest
from datetime import datetime, timezone

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
