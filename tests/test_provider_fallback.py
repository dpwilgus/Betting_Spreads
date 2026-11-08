import unittest
import urllib.error
from unittest.mock import patch

from ai_analysis import _validate_analysis
from ai_providers import Provider, _response_text, generate_json
from live_research import collect_game_research


def valid_analysis():
    pick = {
        "rank": 1, "market_type": "Player Prop", "player_or_team": "Player A",
        "bet_description": "Over 50.5 receiving yards", "market_line": "Not verified",
        "play_threshold": "50.5 yards", "estimated_hit_probability": 55.0,
        "confidence": 5, "statistical_rationale": "Example rationale",
        "risk_factors": "Example risk", "data_quality": "Limited",
    }
    scorer = {
        "rank": 1, "player": "Player A", "team": "Team A",
        "estimated_td_probability": 20.0, "confidence": 4,
        "rationale": "Example role", "risk_factors": "Example risk",
    }
    return {
        "summary": "Example summary", "game_script": "Example script",
        "picks": [dict(pick, rank=i) for i in range(1, 6)],
        "touchdown_scorers": [dict(scorer, rank=i) for i in range(1, 4)],
        "data_gaps": [],
    }


class ProviderFallbackTests(unittest.TestCase):
    def test_falls_back_after_quota_error(self):
        first = Provider("gemini", "GEMINI_API_KEY", "gemini", "url", "model", "gemini")
        second = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")
        quota_error = urllib.error.HTTPError(
            "https://example.test", 429, "quota", {}, None
        )
        with patch("ai_providers.ensure_configured"), patch(
            "ai_providers._configured_providers",
            return_value=[(first, "key-one"), (second, "key-two")],
        ), patch("ai_providers._call_provider", side_effect=[quota_error, {"summary": "ok"}]) as call:
            result, provider = generate_json("system", "user")
        self.assertEqual(result["summary"], "ok")
        self.assertEqual(provider, "groq")
        self.assertEqual(call.call_count, 2)

    def test_openai_compatible_response_extracts_json(self):
        provider = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")
        result = _response_text(provider, {
            "choices": [{"message": {"content": "```json\n{\"ok\": true}\n```"}}]
        })
        self.assertEqual(result, '{"ok": true}')

    def test_analysis_schema_validation(self):
        _validate_analysis(valid_analysis())

    def test_analysis_rejects_missing_recommendations(self):
        data = valid_analysis()
        data["picks"] = data["picks"][:2]
        with self.assertRaises(RuntimeError):
            _validate_analysis(data)

    def test_research_is_available_without_event_id_or_ai_key(self):
        result = collect_game_research({
            "Away Team": "Away", "Home Team": "Home", "Game Date (ET)": "Sunday",
            "Betting Line": "Unavailable",
        })
        self.assertIn("No ESPN event ID", result["research_text"])
        self.assertTrue(result["sources"])


if __name__ == "__main__":
    unittest.main()
