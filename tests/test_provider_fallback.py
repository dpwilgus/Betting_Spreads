import io
import unittest
import urllib.error
from unittest.mock import patch

from ai_analysis import _bounded_research_text, _validate_analysis, analyze_game
from ai_providers import (Provider, _call_provider, _response_text, generate_json,
                          get_provider_usage_stats, reset_provider_run_state)
from live_research import collect_game_research, _extract_roster_names


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
        self.assertEqual(sleep.call_count, 2)


    def test_groq_tpm_retry_honors_provider_wait_and_keeps_provider_available(self):
        first = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")
        rate_error = urllib.error.HTTPError(
            "https://example.test", 429, "rate limit", {},
            io.BytesIO(b"Rate limit reached. Please try again in 11.445s.")
        )
        skipped = set()
        with patch(
            "ai_providers.ensure_configured", return_value=[(first, "key-one")]
        ), patch(
            "ai_providers._SKIP_PROVIDER_FOR_RUN", skipped
        ), patch(
            "ai_providers.time.sleep"
        ) as sleep, patch(
            "ai_providers._call_provider",
            side_effect=[rate_error, {"summary": "recovered"}],
        ) as call:
            result, provider = generate_json("system", "user")
        self.assertEqual(result["summary"], "recovered")
        self.assertEqual(provider, "groq")
        sleep.assert_called_once_with(11.445)
        self.assertNotIn("groq", skipped)
        self.assertEqual(call.call_count, 2)

    def test_temporary_tpm_failure_does_not_disable_provider_for_next_game(self):
        first = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")
        rate_error = urllib.error.HTTPError(
            "https://example.test", 429, "rate limit", {},
            io.BytesIO(b"tokens per minute limit; try again in 45s")
        )
        skipped = set()
        with patch(
            "ai_providers.ensure_configured", return_value=[(first, "key-one")]
        ), patch(
            "ai_providers._SKIP_PROVIDER_FOR_RUN", skipped
        ), patch(
            "ai_providers.time.sleep"
        ), patch(
            "ai_providers._call_provider",
            side_effect=[rate_error, rate_error, rate_error, {"summary": "next game"}],
        ) as call:
            with self.assertRaises(RuntimeError):
                generate_json("system", "first game")
            self.assertNotIn("groq", skipped)
            result, provider = generate_json("system", "second game")
        self.assertEqual(result["summary"], "next game")
        self.assertEqual(provider, "groq")
        self.assertEqual(call.call_count, 4)

    def test_openai_compatible_response_extracts_json(self):
        provider = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")
        result = _response_text(provider, {
            "choices": [{"message": {"content": "```json\n{\"ok\": true}\n```"}}]
        })
        self.assertEqual(result, '{"ok": true}')

    def test_analysis_schema_validation(self):
        _validate_analysis(valid_analysis())

    def test_analysis_rejects_more_than_five_recommendations(self):
        data = valid_analysis()
        data["picks"].append(dict(data["picks"][0], rank=6))
        with self.assertRaises(RuntimeError):
            _validate_analysis(data)

    def test_analysis_allows_fewer_evidence_supported_recommendations(self):
        data = valid_analysis()
        data["picks"] = data["picks"][:2]
        data["touchdown_scorers"] = data["touchdown_scorers"][:1]
        _validate_analysis(data)

    def test_touchdown_scorers_must_belong_to_matchup_roster(self):
        data = valid_analysis()
        data["touchdown_scorers"][0]["player"] = "Eli Manning"
        data["touchdown_scorers"][0]["team"] = "New York Giants"
        with self.assertRaisesRegex(RuntimeError, "not found on the current ESPN roster"):
            _validate_analysis(
                data,
                rosters={"Washington Commanders": ["Jayden Daniels"],
                         "New York Giants": ["Daniel Jones"]},
                team_names=("Washington Commanders", "New York Giants"),
            )

    def test_touchdown_scorer_team_must_be_one_of_matchup_teams(self):
        data = valid_analysis()
        data["touchdown_scorers"][0]["team"] = "Dallas Cowboys"
        with self.assertRaisesRegex(RuntimeError, "outside this matchup"):
            _validate_analysis(
                data,
                rosters={"Team A": ["Player A"], "Team B": ["Player B"]},
                team_names=("Team A", "Team B"),
            )

    def test_player_prop_must_name_a_current_roster_player(self):
        data = valid_analysis()
        data["picks"][0]["player_or_team"] = "Eli Manning"
        with self.assertRaisesRegex(RuntimeError, "not found on either current ESPN roster"):
            _validate_analysis(
                data,
                rosters={"Team A": ["Player A"], "Team B": ["Player B"]},
                team_names=("Team A", "Team B"),
            )

    def test_roster_extractor_reads_nested_espn_athletes(self):
        names = _extract_roster_names({
            "athletes": [
                {"fullName": "Player One", "position": {"abbreviation": "QB"}},
                {"items": [{"displayName": "Player Two", "position": "WR"}]},
            ]
        })
        self.assertEqual(names, ["Player One", "Player Two"])

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
        self.assertEqual(payload["max_completion_tokens"], 1600)
        self.assertEqual(payload["response_format"], {"type": "json_object"})


    def test_research_prompt_truncates_only_between_complete_sections(self):
        research = "HEADER\n\nINJURIES: complete\n\nTEAM STATS: " + ("x" * 200)
        with patch("ai_analysis.MAX_RESEARCH_PROMPT_CHARS", 100):
            result, truncated = _bounded_research_text(research)

        self.assertTrue(truncated)
        self.assertIn("HEADER", result)
        self.assertIn("INJURIES: complete", result)
        self.assertNotIn("TEAM STATS:", result)
        self.assertIn("[Research truncated.]", result)
        self.assertLessEqual(len(result), 100)

    def test_short_research_prompt_is_not_changed(self):
        research = "HEADER\n\nINJURIES: complete"
        result, truncated = _bounded_research_text(research)
        self.assertEqual(result, research)
        self.assertFalse(truncated)

    def test_analysis_prompt_uses_compact_contract_and_keeps_research(self):
        game = {
            "Away Team": "Away", "Home Team": "Home",
            "Game Date (ET)": "Sunday", "Betting Line": "Unavailable",
        }
        research = {
            "research_text": "CURRENT INJURY EVIDENCE: Example player is questionable.",
            "sources": [{"title": "ESPN", "url": "https://example.test"}],
        }

        with patch(
            "ai_analysis.generate_json",
            return_value=(valid_analysis(), "gemini"),
        ) as generate:
            result = analyze_game(game, research=research)

        system_prompt, user_prompt = generate.call_args.args[:2]
        self.assertIn("up to five ranked betting opportunities", system_prompt)
        self.assertIn("CURRENT INJURY EVIDENCE", user_prompt)
        self.assertIn("statistical_rationale", user_prompt)
        self.assertIn("touchdown_scorers", user_prompt)
        self.assertNotIn('"additionalProperties"', user_prompt)
        self.assertLess(len(system_prompt) + len(user_prompt), 10000)
        self.assertEqual(result["ai_provider"], "gemini")
        self.assertEqual(result["sources"], research["sources"])

    def test_provider_usage_tracks_requests_and_reported_tokens(self):
        provider = Provider("groq", "GROQ_API_KEY", "groq", "url", "model", "openai")
        response = {
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45},
        }

        reset_provider_run_state()
        with patch("ai_providers._request_json", return_value=response):
            result = _call_provider(provider, "test-key", "system prompt", "user prompt")

        stats = get_provider_usage_stats()["groq"]
        self.assertTrue(result["ok"])
        self.assertEqual(stats["requests"], 1)
        self.assertEqual(stats["successful_responses"], 1)
        self.assertEqual(stats["prompt_characters"], len("system promptuser prompt"))
        self.assertEqual(stats["prompt_tokens"], 123)
        self.assertEqual(stats["completion_tokens"], 45)

    def test_provider_credentials_are_cached_for_one_run(self):
        provider = Provider("gemini", "GEMINI_API_KEY", "gemini", "url", "model", "gemini")
        configured = [(provider, "test-key")]

        with patch("ai_providers._CONFIGURED_PROVIDERS_CACHE", None), patch(
            "ai_providers._configured_providers", return_value=configured
        ) as load:
            first = __import__("ai_providers").ensure_configured()
            second = __import__("ai_providers").ensure_configured()

        self.assertEqual(first, configured)
        self.assertEqual(second, configured)
        load.assert_called_once()

    def test_reset_provider_run_state_clears_cached_credentials(self):
        provider = Provider("gemini", "GEMINI_API_KEY", "gemini", "url", "model", "gemini")
        configured = [(provider, "test-key")]

        with patch("ai_providers._CONFIGURED_PROVIDERS_CACHE", None), patch(
            "ai_providers._configured_providers", return_value=configured
        ) as load:
            module = __import__("ai_providers")
            module.ensure_configured()
            module.reset_provider_run_state()
            module.ensure_configured()

        self.assertEqual(load.call_count, 2)


if __name__ == "__main__":
    unittest.main()
