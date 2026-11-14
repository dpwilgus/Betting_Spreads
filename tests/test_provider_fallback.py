import io
import unittest
import urllib.error
from unittest.mock import patch

from ai_analysis import (_apply_independent_injury_statuses, _bounded_research_text,
                         _filter_injury_impacts, _filter_player_props_by_usage, _filter_td_scorers_by_usage,
                         _research_player_usage, _separate_unverified_player_props,
                         _normalize_name, _validate_analysis, analyze_game)
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
        "availability": "Active",
        "availability_evidence": "Latest team injury report lists no designation.",
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
            side_effect=[server_error, server_error, server_error, {"summary": "ok"}],
        ) as call:
            result, provider = generate_json("system", "user")

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(provider, "groq")
        self.assertEqual(call.call_count, 4)
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

    def test_nvidia_is_last_in_default_provider_order(self):
        from ai_providers import PROVIDER_ORDER
        self.assertEqual(PROVIDER_ORDER, ("gemini", "groq", "openrouter", "nvidia"))

    def test_nvidia_is_optional_and_configured_only_with_a_saved_key(self):
        from ai_providers import _configured_providers
        with patch.dict("os.environ", {"AI_PROVIDER_ORDER": "gemini,groq,openrouter,nvidia"}, clear=False), patch(
            "ai_providers._saved_key",
            side_effect=lambda provider: "test-key" if provider.name != "nvidia" else "",
        ):
            providers = _configured_providers()
        self.assertEqual(
            [provider.name for provider, _ in providers],
            ["gemini", "groq", "openrouter"],
        )

        with patch.dict("os.environ", {"AI_PROVIDER_ORDER": "gemini,groq,openrouter,nvidia"}, clear=False), patch(
            "ai_providers._saved_key", return_value="test-key"
        ):
            providers = _configured_providers()
        self.assertEqual(
            [provider.name for provider, _ in providers],
            ["gemini", "groq", "openrouter", "nvidia"],
        )

        with patch.dict("os.environ", {"AI_PROVIDER_ORDER": "nvidia"}, clear=False), patch(
            "ai_providers._saved_key", return_value="test-key"
        ):
            providers = _configured_providers()
        self.assertEqual([provider.name for provider, _ in providers], ["nvidia"])

    def test_nvidia_request_uses_hosted_nim_compatible_payload(self):
        provider = Provider(
            "nvidia", "NVIDIA_API_KEY", "nvidia_api_key",
            "https://integrate.api.nvidia.com/v1/chat/completions",
            "nvidia/nemotron-3-ultra-550b-a55b", "openai",
        )
        response = {
            "choices": [{"message": {"content": '{"summary":"ok"}'}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8},
        }
        with patch("ai_providers._request_json", return_value=response) as request, patch.dict(
            "os.environ", {
                "NVIDIA_MODEL": "nvidia/nemotron-3.5-lightning-30b-a3b",
                "NVIDIA_MAX_TOKENS": "1800",
            }
        ):
            result = _call_provider(provider, "nvidia-test-key", "system", "user")
        args = request.call_args
        self.assertEqual(args.args[0], provider.endpoint)
        self.assertEqual(args.args[2]["Authorization"], "Bearer nvidia-test-key")
        payload = args.args[1]
        self.assertEqual(payload["model"], "nvidia/nemotron-3.5-lightning-30b-a3b")
        self.assertEqual(payload["max_tokens"], 1800)
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("response_format", payload)
        self.assertEqual(result, {"summary": "ok"})

    def test_analysis_schema_validation(self):
        _validate_analysis(valid_analysis())

    def test_analysis_allows_more_than_five_recommendations(self):
        data = valid_analysis()
        data["picks"].extend(
            dict(data["picks"][0], rank=rank)
            for rank in range(6, 9)
        )
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
                rosters={"Washington Commanders": ["Jayden Daniels", "Player A"],
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
                {"valid": False},
                {"valid": True, "summary": "accepted"},
            ],
        ) as call:
            result, provider = generate_json("system", "user", validator=validator)

        self.assertEqual(result["summary"], "accepted")
        self.assertEqual(provider, "groq")
        self.assertEqual(call.call_count, 3)


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

    def test_research_truncation_skips_oversized_section_and_keeps_later_evidence(self):
        research = (
            "HEADER"
            + "\n\nINJURIES: " + ("x" * 120)
            + "\n\nPLAYER LOGS: compact evidence"
        )
        with patch("ai_analysis.MAX_RESEARCH_PROMPT_CHARS", 100):
            result, truncated = _bounded_research_text(research)

        self.assertTrue(truncated)
        self.assertIn("HEADER", result)
        self.assertNotIn("INJURIES:", result)
        self.assertIn("PLAYER LOGS: compact evidence", result)
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
        self.assertIn("Target five verified player props", system_prompt)
        self.assertIn("model-only", system_prompt)
        self.assertIn("CURRENT INJURY EVIDENCE", user_prompt)
        self.assertIn("statistical_rationale", user_prompt)
        self.assertIn("touchdown_scorers", user_prompt)
        self.assertIn("summary <= 90 words", user_prompt)
        self.assertIn("statistical_rationale <= 24 words", user_prompt)
        self.assertIn("up to 3 meaningful injury_impact entries", user_prompt)
        self.assertIn("risk_factors must start 'Questionable:'", user_prompt)
        self.assertIn("'Questionable:'", user_prompt)
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


    def test_player_props_are_rejected_when_threshold_conflicts_with_espn_stats(self):
        research = """ESPN CURRENT-SEASON PLAYER GAME LOGS — Team (selected by ESPN depth chart; retrieved 2026-11-13): [{"player":"Josh Jacobs","position":"RB","season_game_log":{"games_available":9,"season_totals":{"Regular Season Stats":{"rushingYards":"80","rushingAttempts":"36"}},"recent_games":[{"stats":{"rushingYards":"3"}},{"stats":{"rushingYards":"-1"}}]}}]"""
        usage = _research_player_usage(research)
        data = {"picks":[{
            "market_type":"Player Prop","player_or_team":"Josh Jacobs",
            "bet_description":"Rushing Yards Over 65.5",
            "play_threshold":"65.5 yards",
        }]}
        gaps = _filter_player_props_by_usage(data, usage)
        self.assertEqual(data["picks"], [])
        self.assertTrue(any("materially conflicts" in gap for gap in gaps))

    def test_td_filter_rejects_quarterback_without_rushing_td_profile(self):
        usage = {
            "jordanlove": {
                "player":"Jordan Love","position":"QB","games_available":9,
                "season":{"rushingAttempts":12,"rushingTouchdowns":2},
                "recent":[]
            },
            "justinjefferson": {
                "player":"Justin Jefferson","position":"WR","games_available":9,
                "season":{"receivingTargets":65,"receivingTouchdowns":1},
                "recent":[]
            },
        }
        data = {"touchdown_scorers":[
            {"player":"Jordan Love","estimated_td_probability":30},
            {"player":"Justin Jefferson","estimated_td_probability":25},
        ]}
        gaps = _filter_td_scorers_by_usage(data, usage)
        self.assertEqual([p["player"] for p in data["touchdown_scorers"]], ["Justin Jefferson"])
        self.assertEqual(data["touchdown_scorers"][0]["rank"], 1)
        self.assertTrue(any("Jordan Love" in gap for gap in gaps))

    def test_missing_injury_record_never_gets_reported_as_active(self):
        data = {
            "picks":[{
                "market_type":"Player Prop","player_or_team":"Player A",
                "risk_factors":"No current risks",
            }],
            "touchdown_scorers":[{
                "player":"Player A","availability":"Active",
                "availability_evidence":"No injury report today",
                "confidence":7,"risk_factors":"",
            }],
        }
        _apply_independent_injury_statuses(data, [])
        self.assertEqual(data["touchdown_scorers"][0]["availability"], "Not independently verified")
        self.assertIn("not independently verified", data["picks"][0]["risk_factors"].casefold())


    def test_model_only_player_props_are_not_actionable_sportsbook_picks(self):
        data = {"picks":[{
            "market_type":"Player Prop","player_or_team":"Jordan Love",
            "bet_description":"Passing Yards Over","market_line":"Not verified",
            "play_threshold":"255.5",
        }]}
        gaps = _separate_unverified_player_props(data, {"verified_player_prop_markets":[]})
        self.assertEqual(data["picks"], [])
        self.assertEqual(len(data["model_only_picks"]), 1)
        self.assertIn("NOT VERIFIED", data["model_only_picks"][0]["market_line"])
        self.assertTrue(any("no verified sportsbook line" in gap for gap in gaps))

    def test_player_prop_with_independent_sportsbook_quote_remains_actionable(self):
        data = {"picks":[{
            "market_type":"Player Prop","player_or_team":"Jordan Love",
            "bet_description":"Passing Yards Over","market_line":"Not verified",
            "play_threshold":"255.5",
        }]}
        quote = {
            "player":"Jordan Love","market":"Passing Yards","line":"250.5",
            "price":"-110","sportsbook":"ExampleBook",
            "source_url":"https://sportsbook.example/market",
            "retrieved_at":"2026-11-14T00:00:00Z",
        }
        _separate_unverified_player_props(data, {"verified_player_prop_markets":[quote]})
        self.assertEqual(len(data["picks"]), 1)
        self.assertEqual(data["picks"][0]["market_line"], "250.5 (-110) — ExampleBook")
        self.assertEqual(data["model_only_picks"], [])

    def test_injury_impact_drops_affected_player_who_is_out(self):
        data = {"injury_impact":[{
            "injured_player":"Blake Cashman","team":"Minnesota Vikings",
            "affected_player":"Josh Jacobs","affected_team":"Green Bay Packers",
            "impact_direction":"Positive",
        }]}
        statuses = [
            {"player":"Blake Cashman","team":"Minnesota Vikings","status":"Out","position":"LB"},
            {"player":"Josh Jacobs","team":"Green Bay Packers","status":"Out","position":"RB"},
        ]
        rosters = {
            "Minnesota Vikings":["Blake Cashman"],
            "Green Bay Packers":["Josh Jacobs"],
        }
        gaps = _filter_injury_impacts(
            data,statuses,rosters,("Minnesota Vikings","Green Bay Packers")
        )
        self.assertEqual(data["injury_impact"], [])
        self.assertTrue(any("Josh Jacobs" in gap and "Out" in gap for gap in gaps))

    def test_injury_impact_keeps_cross_team_matchup_effect_for_available_player(self):
        data = {"injury_impact":[{
            "injured_player":"Blake Cashman","team":"Minnesota Vikings",
            "affected_player":"Matthew Golden","affected_team":"Green Bay Packers",
            "impact_direction":"Positive",
        }]}
        statuses = [
            {"player":"Blake Cashman","team":"Minnesota Vikings","status":"Out","position":"LB"},
        ]
        rosters = {
            "Minnesota Vikings":["Blake Cashman"],
            "Green Bay Packers":["Matthew Golden"],
        }
        _filter_injury_impacts(
            data,statuses,rosters,("Minnesota Vikings","Green Bay Packers")
        )
        self.assertEqual(len(data["injury_impact"]), 1)


    def test_td_filter_keeps_only_three_highest_scoring_skill_candidates(self):
        usage = {}
        scorers = []
        for index, (player, touchdowns, targets) in enumerate([
            ("Receiver One", 5, 80),
            ("Receiver Two", 4, 75),
            ("Receiver Three", 3, 70),
            ("Receiver Four", 1, 50),
        ]):
            key = _normalize_name(player)
            usage[key] = {
                "player":player,"position":"WR","games_available":9,
                "season":{"receivingTouchdowns":touchdowns,"receivingTargets":targets},
                "recent":[]
            }
            scorers.append({"player":player,"estimated_td_probability":20+index})
        data = {"touchdown_scorers":scorers}
        _filter_td_scorers_by_usage(data, usage)
        self.assertEqual(len(data["touchdown_scorers"]), 3)
        self.assertEqual(
            [item["player"] for item in data["touchdown_scorers"]],
            ["Receiver One","Receiver Two","Receiver Three"],
        )


    def test_high_volume_receiver_can_qualify_as_td_candidate_without_prior_td(self):
        data = {"touchdown_scorers":[{"player":"Justin Jefferson","estimated_td_probability":24}]}
        usage = {
            "justinjefferson":{
                "player":"Justin Jefferson","position":"WR","games_available":9,
                "season":{"receivingTargets":90,"receivingTouchdowns":0},
                "recent":[{"stats":{"receivingTargets":10,"receivingTouchdowns":0}}]
            }
        }
        _filter_td_scorers_by_usage(data,usage)
        self.assertEqual([item["player"] for item in data["touchdown_scorers"]],["Justin Jefferson"])


if __name__ == "__main__":
    unittest.main()
