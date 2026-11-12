import io
import unittest
import urllib.error
from unittest.mock import patch

from ai_analysis import _validate_analysis
from ai_providers import Provider, _call_provider, _response_text, generate_json, reset_provider_run_state
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
        "injury_impact": [],
        "data_gaps": [],
    }


class ProviderFallbackTests(unittest.TestCase):
    def test_provider_skip_list_resets_for_new_run(self):
        skipped = {"gemini", "groq"}

        with patch("ai_providers._SKIP_PROVIDER_FOR_RUN", skipped):
            reset_provider_run_state()

        self.assertEqual(skipped, set())

    def test_falls_back_after_quota_error(self):
        first = Provider("gemini", "GEMINI_API_KEY", "gemini", "url", "model", "gemini")
        second = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")

        quota_error = urllib.error.HTTPError(
            "https://example.test", 429, "quota", {}, io.BytesIO(b"quota")
        )

        with patch(
            "ai_providers.ensure_configured",
            return_value=[(first, "key-one"), (second, "key-two")],
        ), patch(
            "ai_providers._SKIP_PROVIDER_FOR_RUN", set()
        ), patch(
            "ai_providers._call_provider",
            side_effect=[quota_error, {"summary": "ok"}],
        ) as call:
            result, provider = generate_json("system", "user")

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(provider, "groq")
        self.assertEqual(call.call_count, 2)

    def test_retries_temporary_429_without_retry_after(self):
        first = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")

        rate_error = urllib.error.HTTPError(
            "https://example.test", 429, "rate limit", {},
            io.BytesIO(b"rate limit")
        )

        with patch(
            "ai_providers.ensure_configured",
            return_value=[(first, "key-one")],
        ), patch(
            "ai_providers._SKIP_PROVIDER_FOR_RUN", set()
        ), patch(
            "ai_providers.time.sleep"
        ) as sleep, patch(
            "ai_providers._call_provider",
            side_effect=[rate_error, {"summary": "ok"}],
        ) as call:
            result, provider = generate_json("system", "user")

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(provider, "groq")
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_retries_server_error_before_fallback(self):
        first = Provider("gemini", "GEMINI_API_KEY", "gemini", "url", "model", "gemini")
        second = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")

        server_error = urllib.error.HTTPError(
            "https://example.test", 503, "unavailable", {}, io.BytesIO(b"server error")
        )

        with patch(
            "ai_providers.ensure_configured",
            return_value=[(first, "key-one"), (second, "key-two")],
        ), patch(
            "ai_providers._SKIP_PROVIDER_FOR_RUN", set()
        ), patch(
            "ai_providers.time.sleep"
        ) as sleep, patch(
            "ai_providers._call_provider",
            side_effect=[server_error, server_error, {"summary": "ok"}],
        ) as call:
            result, provider = generate_json("system", "user")

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(provider, "groq")
        self.assertEqual(call.call_count, 3)
        self.assertEqual(sleep.call_count, 1)

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


    def test_retries_connection_error_before_accepting_response(self):
        first = Provider("gemini", "GEMINI_API_KEY", "gemini", "url", "model", "gemini")
        second = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")

        with patch(
            "ai_providers.ensure_configured",
            return_value=[(first, "key-one"), (second, "key-two")],
        ), patch(
            "ai_providers._SKIP_PROVIDER_FOR_RUN", set()
        ), patch(
            "ai_providers.time.sleep"
        ) as sleep, patch(
            "ai_providers._call_provider",
            side_effect=[urllib.error.URLError("temporary connection failure"), {"summary": "ok"}],
        ) as call:
            result, provider = generate_json("system", "user")

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(provider, "gemini")
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_skipped_provider_stays_skipped_for_rest_of_run(self):
        first = Provider("gemini", "GEMINI_API_KEY", "gemini", "url", "model", "gemini")
        second = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")

        quota_error = urllib.error.HTTPError(
            "https://example.test", 429, "quota", {}, io.BytesIO(b"quota")
        )

        with patch(
            "ai_providers.ensure_configured",
            return_value=[(first, "key-one"), (second, "key-two")],
        ), patch(
            "ai_providers._SKIP_PROVIDER_FOR_RUN", set()
        ) as skipped, patch(
            "ai_providers._call_provider",
            side_effect=[
                quota_error,
                {"summary": "first game"},
                {"summary": "second game"},
            ],
        ) as call:
            first_result, first_provider = generate_json("system", "game one")
            self.assertIn("gemini", skipped)

            second_result, second_provider = generate_json("system", "game two")

        self.assertEqual(first_result["summary"], "first game")
        self.assertEqual(second_result["summary"], "second game")
        self.assertEqual(first_provider, "groq")
        self.assertEqual(second_provider, "groq")
        self.assertEqual(call.call_count, 3)
        self.assertEqual(
            [entry.args[0].name for entry in call.call_args_list],
            ["gemini", "groq", "groq"],
        )

    def test_falls_back_when_provider_response_fails_validation(self):
        first = Provider("gemini", "GEMINI_API_KEY", "gemini", "url", "model", "gemini")
        second = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")

        def validator(result):
            if result.get("valid") is not True:
                raise RuntimeError("Response failed schema validation.")

        with patch(
            "ai_providers.ensure_configured",
            return_value=[(first, "key-one"), (second, "key-two")],
        ), patch(
            "ai_providers._SKIP_PROVIDER_FOR_RUN", set()
        ), patch(
            "ai_providers._call_provider",
            side_effect=[
                {"valid": False},
                {"valid": True, "summary": "accepted"},
            ],
        ) as call:
            result, provider = generate_json("system", "user", validator=validator)

        self.assertEqual(result["summary"], "accepted")
        self.assertEqual(provider, "groq")
        self.assertEqual(call.call_count, 2)


    def test_groq_gpt_oss_uses_supported_reasoning_parameter(self):
        provider = Provider(
            "groq", "GROQ_API_KEY", "groq",
            "https://api.groq.com/openai/v1/chat/completions",
            "openai/gpt-oss-20b", "openai",
        )

        with patch(
            "ai_providers._request_json", return_value={}
        ) as request, patch(
            "ai_providers._response_text", return_value='{"ok": true}'
        ):
            result = _call_provider(provider, "test-key", "system", "user")

        payload = request.call_args.args[1]

        self.assertTrue(result["ok"])
        self.assertIs(payload["include_reasoning"], False)
        self.assertNotIn("reasoning_format", payload)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["max_completion_tokens"], 4096)
        self.assertEqual(payload["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
