"""Multi-provider AI client with local credential storage and bounded fallback."""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

import certifi
import keyring

SERVICE_NAME = "Betting Spreads"
PROVIDER_ORDER = ("gemini", "groq", "openrouter")  # NVIDIA stays opt-in for local development.
_SKIP_PROVIDER_FOR_RUN: set[str] = set()
_CONFIGURED_PROVIDERS_CACHE: list[tuple["Provider", str]] | None = None
_PROVIDER_USAGE: dict[str, dict[str, int]] = {}


def reset_provider_run_state() -> None:
    """Allow all configured providers to be tried in a new analysis run."""
    global _CONFIGURED_PROVIDERS_CACHE
    _SKIP_PROVIDER_FOR_RUN.clear()
    _CONFIGURED_PROVIDERS_CACHE = None
    _PROVIDER_USAGE.clear()


@dataclass(frozen=True)
class Provider:
    name: str
    env_var: str
    account: str
    endpoint: str
    default_model: str
    style: str


PROVIDERS = {
    "gemini": Provider(
        "gemini", "GEMINI_API_KEY", "gemini_api_key",
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "gemini-3.8-flash", "gemini",
    ),
    "groq": Provider(
        "groq", "GROQ_API_KEY", "groq_api_key",
        "https://api.groq.com/openai/v1/chat/completions",
        "openai/gpt-oss-20b", "openai",
    ),
    "openrouter": Provider(
        "openrouter", "OPENROUTER_API_KEY", "openrouter_api_key",
        "https://openrouter.ai/api/v1/chat/completions",
        "openrouter/free", "openai",
    ),
    # Experimental NVIDIA-hosted NIM endpoint. Never included in the default order.
    "nvidia": Provider(
        "nvidia", "NVIDIA_API_KEY", "nvidia_api_key",
        "https://integrate.api.nvidia.com/v1/chat/completions",
        "nvidia/nemotron-3-ultra-550b-a55b", "openai",
    ),
    # Optional paid fallback. ChatGPT subscriptions do not include API credits.
    "openai": Provider(
        "openai", "OPENAI_API_KEY", "openai_api_key",
        "https://api.openai.com/v1/chat/completions",
        "gpt-5-mini", "openai",
    ),
}


def _saved_key(provider: Provider) -> str:
    value = os.environ.get(provider.env_var, "").strip()
    if value:
        return value
    try:
        return (keyring.get_password(SERVICE_NAME, provider.account) or "").strip()
    except Exception:
        return ""


def _save_key(provider: Provider, value: str) -> None:
    if value:
        keyring.set_password(SERVICE_NAME, provider.account, value.strip())


def _configure_keys() -> None:
    """Collect any combination of free provider keys; all fields are optional."""
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception as exc:
        raise RuntimeError(
            "No AI API keys are configured. Set GEMINI_API_KEY, GROQ_API_KEY, "
            "or OPENROUTER_API_KEY, then restart the app."
        ) from exc

    root = tk.Tk()
    root.title("Set up Betting Spreads AI")
    root.geometry("640x380")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    tk.Label(
        root,
        text=("Add one or more API keys. Gemini, Groq and OpenRouter offer free usage "
              "with limits. NVIDIA NIM is experimental and for development/testing only. "
              "OpenAI is optional and usage is paid. Keys are stored in your computer's "
              "secure credential store."),
        wraplength=580, justify="left",
    ).pack(padx=18, pady=(16, 10), anchor="w")

    entries: dict[str, Any] = {}
    labels = [
        ("gemini", "Google Gemini API key (free tier)"),
        ("groq", "Groq API key (free tier)"),
        ("openrouter", "OpenRouter API key (free models)"),
        ("nvidia", "NVIDIA NIM API key (experimental)"),
        ("openai", "OpenAI API key (optional, paid)"),
    ]
    for name, label in labels:
        row = tk.Frame(root)
        row.pack(fill="x", padx=18, pady=4)
        tk.Label(row, text=label, width=34, anchor="w").pack(side="left")
        entry = tk.Entry(row, show="*", width=43)
        entry.pack(side="left", fill="x", expand=True)
        entries[name] = entry

    result = {"saved": False}

    def save() -> None:
        supplied = 0
        try:
            for name, entry in entries.items():
                value = entry.get().strip()
                if value:
                    _save_key(PROVIDERS[name], value)
                    supplied += 1
        except Exception as exc:
            messagebox.showerror("Could not save API keys", str(exc), parent=root)
            return
        if not supplied:
            messagebox.showwarning(
                "No API keys entered",
                "Enter at least one key, or close this window and configure an API key later.",
                parent=root,
            )
            return
        result["saved"] = True
        root.destroy()

    tk.Button(root, text="Save API keys", command=save).pack(pady=15)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    if not result["saved"]:
        raise RuntimeError(
            "No AI API keys were saved. Add a Gemini, Groq or OpenRouter key to continue."
        )


def ensure_configured() -> list[tuple[Provider, str]]:
    """Return providers with credentials cached for the current analysis run."""
    global _CONFIGURED_PROVIDERS_CACHE
    if _CONFIGURED_PROVIDERS_CACHE is not None:
        return list(_CONFIGURED_PROVIDERS_CACHE)

    configured = _configured_providers()
    if not configured:
        _configure_keys()
        configured = _configured_providers()
    if not configured:
        raise RuntimeError(
            "No AI API keys are configured. Set GEMINI_API_KEY, GROQ_API_KEY, "
            "or OPENROUTER_API_KEY, then restart the app."
        )
    _CONFIGURED_PROVIDERS_CACHE = list(configured)
    return list(_CONFIGURED_PROVIDERS_CACHE)

def _configured_providers() -> list[tuple[Provider, str]]:
    configured = []
    order = os.environ.get("AI_PROVIDER_ORDER", ",".join(PROVIDER_ORDER))
    names = [part.strip().lower() for part in order.split(",") if part.strip()]
    for name in names:
        if name == "openai" and os.environ.get("AI_ALLOW_PAID", "").strip().lower() not in {"1", "true", "yes"}:
            continue
        provider = PROVIDERS.get(name)
        if provider:
            key = _saved_key(provider)
            if key:
                configured.append((provider, key))
    return configured


def _request_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json",
                 "User-Agent": "Betting-Spreads/3.0"},
        method="POST",
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=180, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def _response_text(provider: Provider, result: dict[str, Any]) -> str:
    if provider.style == "gemini":
        candidates = result.get("candidates") or []
        if not candidates:
            raise RuntimeError("Gemini returned no candidates.")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts)
    else:
        choices = result.get("choices") or []
        if not choices:
            raise RuntimeError(f"{provider.name} returned no choices.")
        text = choices[0].get("message", {}).get("content", "")
        if isinstance(text, list):
            text = "".join(item.get("text", "") for item in text if isinstance(item, dict))
    text = str(text).strip()
    text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text, flags=re.I)
    if not text:
        raise RuntimeError(f"{provider.name} returned an empty response.")
    return text



def get_provider_usage_stats() -> dict[str, dict[str, int]]:
    """Return a copy of per-run provider request and token usage counters."""
    return {name: dict(values) for name, values in _PROVIDER_USAGE.items()}


def _record_provider_request(provider: Provider, system_prompt: str, user_prompt: str) -> None:
    usage = _PROVIDER_USAGE.setdefault(provider.name, {
        "requests": 0,
        "successful_responses": 0,
        "prompt_characters": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    })
    usage["requests"] += 1
    usage["prompt_characters"] += len(system_prompt) + len(user_prompt)


def _record_provider_response(provider: Provider, result: dict[str, Any]) -> None:
    usage = _PROVIDER_USAGE.setdefault(provider.name, {
        "requests": 0,
        "successful_responses": 0,
        "prompt_characters": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    })
    usage["successful_responses"] += 1
    metadata = result.get("usage") or result.get("usageMetadata") or {}
    prompt_tokens = metadata.get("prompt_tokens", metadata.get("promptTokenCount", 0))
    completion_tokens = metadata.get(
        "completion_tokens", metadata.get("candidatesTokenCount", 0)
    )
    if isinstance(prompt_tokens, (int, float)):
        usage["prompt_tokens"] += int(prompt_tokens)
    if isinstance(completion_tokens, (int, float)):
        usage["completion_tokens"] += int(completion_tokens)

def _call_provider(
    provider: Provider, key: str, system_prompt: str, user_prompt: str
) -> dict[str, Any]:
    model = os.environ.get(f"{provider.name.upper()}_MODEL", provider.default_model)
    _record_provider_request(provider, system_prompt, user_prompt)
    if provider.style == "gemini":
        url = provider.endpoint.format(model=urllib.parse.quote(model, safe=""))
        url += "?key=" + urllib.parse.quote(key, safe="")
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2, "maxOutputTokens": 3500},
        }
        result = _request_json(url, payload, {})
    else:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        if provider.name == "nvidia":
            # NVIDIA's hosted Nemotron endpoint uses max_tokens. Its API does
            # not guarantee JSON mode, so rely on the prompt and our validator.
            payload["max_tokens"] = int(os.environ.get("NVIDIA_MAX_TOKENS", "2600"))
        else:
            payload["max_completion_tokens"] = int(
                os.environ.get("GROQ_MAX_COMPLETION_TOKENS", "1600")
                if provider.name == "groq" else "1400"
            )
            payload["response_format"] = {"type": "json_object"}
        if provider.name == "groq":
            if model.startswith("openai/gpt-oss"):
                payload["include_reasoning"] = False
                payload["reasoning_effort"] = "low"
            else:
                payload["reasoning_format"] = "hidden"

        headers = {"Authorization": f"Bearer {key}"}
        if provider.name == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/dpwilgus/Betting_Spreads"
            headers["X-Title"] = "Betting Spreads"
        try:
            result = _request_json(provider.endpoint, payload, headers)
        except urllib.error.HTTPError as exc:
            # Some free OpenRouter model routes do not support JSON mode.
            if provider.name != "openrouter" or exc.code != 400:
                raise
            payload.pop("response_format", None)
            _record_provider_request(provider, system_prompt, user_prompt)
            result = _request_json(provider.endpoint, payload, headers)
    _record_provider_response(provider, result)
    return json.loads(_response_text(provider, result))


def generate_json(
    system_prompt: str, user_prompt: str,
    validator: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], str]:
    """Try configured providers in order; retry transient failures with bounded waits, then fall back."""
    configured = [
        item for item in ensure_configured()
        if item[0].name not in _SKIP_PROVIDER_FOR_RUN
    ]
    if not configured:
        raise RuntimeError("No configured AI providers remain available for this run. Check API keys and quotas.")

    failures = []
    for provider, key in configured:
        for attempt in range(3):
            try:
                result = _call_provider(provider, key, system_prompt, user_prompt)
                if not isinstance(result, dict):
                    raise RuntimeError("The AI response was not a JSON object.")
                if validator is not None:
                    validator(result)
                return result, provider.name
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:600]
                # 429 may be a short per-minute throttle or a depleted daily quota.
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                wait_match = re.search(
                    r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*s",
                    body,
                    flags=re.I,
                )
                try:
                    delay = float(retry_after) if retry_after else (
                        float(wait_match.group(1)) if wait_match else 1.0
                    )
                except ValueError:
                    delay = 1.0
                quota_exhausted = exc.code == 429 and any(
                    marker in body.lower()
                    for marker in ("quota", "insufficient_quota", "resource_exhausted",
                                   "daily limit", "billing")
                )
                transient = exc.code >= 500 or (exc.code == 429 and not quota_exhausted)
                if transient and attempt < 2:
                    # Respect provider-supplied retry windows instead of retrying too early.
                    time.sleep(min(max(delay, 0.5), 60))
                    continue
                failures.append(f"{provider.name}: HTTP {exc.code} {body}")
                # Temporary TPM/5xx failures apply to this request, not the whole slate.
                if quota_exhausted or exc.code in (401, 403, 404):
                    _SKIP_PROVIDER_FOR_RUN.add(provider.name)
                break
            except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
                if attempt == 0:
                    time.sleep(1)
                    continue
                failures.append(f"{provider.name}: connection error: {exc}")
                break
            except (ValueError, KeyError, RuntimeError) as exc:
                # Invalid model output is not evidence that the provider is offline.
                # Retry once for this matchup, then allow the next matchup to try again.
                if attempt == 0:
                    time.sleep(1)
                    continue
                failures.append(f"{provider.name}: {exc}")
                break
            except Exception as exc:
                failures.append(f"{provider.name}: unexpected error: {exc}")
                _SKIP_PROVIDER_FOR_RUN.add(provider.name)
                break

    details = "\n".join(failures)
    raise RuntimeError(
        "All configured AI providers failed. Check provider quotas, keys, and model availability. "
        "The independent NFL research step still runs before AI analysis.\n" + details
    )
