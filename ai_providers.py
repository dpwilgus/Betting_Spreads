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
from typing import Any

import certifi
import keyring

SERVICE_NAME = "Betting Spreads"
PROVIDER_ORDER = ("gemini", "groq", "openrouter", "openai")


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
    root.geometry("620x330")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    tk.Label(
        root,
        text=("Add one or more API keys. Gemini, Groq and OpenRouter offer free usage "
              "with limits. OpenAI is optional and usage is paid. Keys are stored in "
              "your computer's secure credential store."),
        wraplength=580, justify="left",
    ).pack(padx=18, pady=(16, 10), anchor="w")

    entries: dict[str, Any] = {}
    labels = [
        ("gemini", "Google Gemini API key (free tier)"),
        ("groq", "Groq API key (free tier)"),
        ("openrouter", "OpenRouter API key (free models)"),
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


def _configured_providers() -> list[tuple[Provider, str]]:
    configured = []
    order = os.environ.get("AI_PROVIDER_ORDER", ",".join(PROVIDER_ORDER))
    names = [part.strip().lower() for part in order.split(",") if part.strip()]
    for name in names:
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


def _call_provider(
    provider: Provider, key: str, system_prompt: str, user_prompt: str
) -> dict[str, Any]:
    model = os.environ.get(f"{provider.name.upper()}_MODEL", provider.default_model)
    if provider.style == "gemini":
        url = provider.endpoint.format(model=urllib.parse.quote(model, safe=""))
        url += "?key=" + urllib.parse.quote(key, safe="")
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
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
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {key}"}
        if provider.name == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/dpwilgus/Betting_Spreads"
            headers["X-Title"] = "Betting Spreads"
        result = _request_json(provider.endpoint, payload, headers)
    return json.loads(_response_text(provider, result))


def generate_json(system_prompt: str, user_prompt: str) -> tuple[dict[str, Any], str]:
    """Try configured providers in order; retry transient failures once, then fall back."""
    configured = _configured_providers()
    if not configured:
        _configure_keys()
        configured = _configured_providers()
    if not configured:
        raise RuntimeError("No usable AI API keys were found in environment variables or credential storage.")

    failures = []
    for provider, key in configured:
        for attempt in range(2):
            try:
                result = _call_provider(provider, key, system_prompt, user_prompt)
                if not isinstance(result, dict):
                    raise RuntimeError("The AI response was not a JSON object.")
                return result, provider.name
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:600]
                # 429 may be a short per-minute throttle or a depleted daily quota.
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                try:
                    delay = min(max(float(retry_after), 0), 3)
                except ValueError:
                    delay = 0
                transient = exc.code >= 500 or (exc.code == 429 and delay > 0)
                if transient and attempt == 0:
                    time.sleep(delay or 1)
                    continue
                failures.append(f"{provider.name}: HTTP {exc.code} {body}")
                break
            except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
                if attempt == 0:
                    time.sleep(1)
                    continue
                failures.append(f"{provider.name}: connection error: {exc}")
                break
            except (ValueError, KeyError, RuntimeError) as exc:
                failures.append(f"{provider.name}: {exc}")
                break
            except Exception as exc:
                failures.append(f"{provider.name}: unexpected error: {exc}")
                break

    details = "\n".join(failures)
    raise RuntimeError(
        "All configured AI providers failed. Check provider quotas, keys, and model availability. "
        "The independent NFL research step still runs before AI analysis.\n" + details
    )
