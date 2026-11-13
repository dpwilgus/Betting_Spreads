import unittest
from unittest.mock import patch

from ai_providers import (
    IncompleteResponseError,
    Provider,
    _call_provider,
    generate_json,
)


class NvidiaTruncationTests(unittest.TestCase):
    def setUp(self):
        self.provider = Provider(
            "nvidia", "NVIDIA_API_KEY", "nvidia_api_key",
            "https://integrate.api.nvidia.com/v1/chat/completions",
            "nvidia/nemotron-3-ultra-550b-a55b", "openai",
        )

    def test_explicit_length_finish_reason_is_reported_before_json_parse(self):
        response = {
            "choices": [{
                "message": {"content": '{"summary":"cut off'},
                "finish_reason": "length",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 777},
        }
        with patch("ai_providers._request_json", return_value=response), patch.dict(
            "os.environ", {"NVIDIA_MAX_TOKENS": "777"}
        ):
            with self.assertRaisesRegex(
                IncompleteResponseError, "finish_reason=length.*output limit=777"
            ):
                _call_provider(self.provider, "test-key", "system", "user")

    def test_token_cap_is_detected_when_finish_reason_is_missing(self):
        response = {
            "choices": [{"message": {"content": '{"summary":"cut off'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 777},
        }
        with patch("ai_providers._request_json", return_value=response), patch.dict(
            "os.environ", {"NVIDIA_MAX_TOKENS": "777"}
        ):
            with self.assertRaisesRegex(
                IncompleteResponseError, "finish_reason=token_limit_reached"
            ):
                _call_provider(self.provider, "test-key", "system", "user")

    def test_explicit_truncation_does_not_repeat_the_same_provider_request(self):
        with patch(
            "ai_providers.ensure_configured",
            return_value=[(self.provider, "test-key")],
        ), patch(
            "ai_providers._SKIP_PROVIDER_FOR_RUN", set()
        ), patch(
            "ai_providers._call_provider",
            side_effect=IncompleteResponseError("NVIDIA response was truncated"),
        ) as call:
            with self.assertRaisesRegex(RuntimeError, "NVIDIA response was truncated"):
                generate_json("system", "user")
        call.assert_called_once()

    def test_nvidia_default_output_limit_is_5000(self):
        response = {
            "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        with patch("ai_providers._request_json", return_value=response) as request, patch.dict(
            "os.environ", {}, clear=True
        ):
            _call_provider(self.provider, "test-key", "system", "user")
        self.assertEqual(request.call_args.args[1]["max_tokens"], 5000)


if __name__ == "__main__":
    unittest.main()
