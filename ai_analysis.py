"""OpenAI-powered NFL matchup research and betting analysis."""
from __future__ import annotations
import json, os, ssl, urllib.error, urllib.request
from pathlib import Path
from typing import Any
import certifi, keyring
from tkinter import Tk, simpledialog

API_URL = "https://api.openai.com/v1/responses"
MODEL = "gpt-6-astra"
SERVICE_NAME = "Betting Spreads"
ACCOUNT_NAME = "openai_api_key"

def _resource_path(relative: str) -> Path:
    import sys
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative

def get_api_key() -> str:
    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        stored = keyring.get_password(SERVICE_NAME, ACCOUNT_NAME)
    except Exception:
        stored = None
    if stored:
        return stored.strip()
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        key = simpledialog.askstring(
            "Set up Betting Spreads AI",
            "Enter your OpenAI API key. It will be saved securely in your computer's credential store.",
            show="*", parent=root
        )
        if not key or not key.strip():
            raise RuntimeError("An OpenAI API key is required to generate matchup analysis.")
        key = key.strip()
        try:
            keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, key)
        except Exception as exc:
            raise RuntimeError(
                "Could not securely save your API key in macOS Keychain or Windows Credential Manager. "
                f"Details: {exc}"
            ) from exc
        return key
    finally:
        root.destroy()

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "game_script": {"type": "string"},
        "picks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "rank": {"type": "integer"},
                "market_type": {"type": "string", "enum": ["Player Prop", "Game Prop"]},
                "player_or_team": {"type": "string"},
                "bet_description": {"type": "string"},
                "market_line": {"type": "string"},
                "play_threshold": {"type": "string"},
                "estimated_hit_probability": {"type": "number"},
                "confidence": {"type": "integer"},
                "statistical_rationale": {"type": "string"},
                "risk_factors": {"type": "string"},
                "data_quality": {"type": "string", "enum": ["Strong", "Moderate", "Limited"]}
            },
            "required": ["rank", "market_type", "player_or_team", "bet_description", "market_line",
                         "play_threshold", "estimated_hit_probability", "confidence",
                         "statistical_rationale", "risk_factors", "data_quality"]
        }},
        "touchdown_scorers": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "rank": {"type": "integer"}, "player": {"type": "string"}, "team": {"type": "string"},
                "estimated_td_probability": {"type": "number"}, "confidence": {"type": "integer"},
                "rationale": {"type": "string"}, "risk_factors": {"type": "string"}
            },
            "required": ["rank", "player", "team", "estimated_td_probability", "confidence",
                         "rationale", "risk_factors"]
        }},
        "data_gaps": {"type": "array", "items": {"type": "string"}}
    },
    "required": ["summary", "game_script", "picks", "touchdown_scorers", "data_gaps"]
}

def _extract_output_text(response: dict[str, Any]) -> str:
    chunks = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                chunks.append(content.get("text", ""))
            elif content.get("type") == "refusal":
                raise RuntimeError(f"AI analysis was refused: {content.get('refusal', 'No details')}")
    if not chunks:
        raise RuntimeError("OpenAI returned no structured analysis text.")
    return "".join(chunks)

def analyze_game(game: dict[str, Any], api_key: str) -> dict[str, Any]:
    instructions = (
        _resource_path("agents/NFL_MATCHUP_ANALYST.md").read_text(encoding="utf-8")
        + "\n\nQUALITY REVIEW CHECKLIST\n"
        + _resource_path("agents/PROBABILITY_REVIEW.md").read_text(encoding="utf-8")
    )
    matchup = f"{game['Away Team']} at {game['Home Team']}"
    user_input = (
        f"Analyze this upcoming NFL matchup: {matchup}.\n"
        f"Kickoff: {game.get('Game Date (ET)', 'Not supplied')}.\n"
        f"ESPN game line: {game.get('Betting Line', 'Unavailable')}.\n"
        f"Game status: {game.get('Game Status', 'Not supplied')}.\n\n"
        "Research current season statistics, recent player game logs and usage, injuries/availability, "
        "defensive matchup tendencies, and weather when relevant. Research current player/game prop "
        "market lines only where publicly verifiable. Return the requested JSON schema. "
        "Do not assume an unverified market line is available."
    )
    payload = {
        "model": MODEL, "reasoning": {"effort": "low"},
        "tools": [{"type": "web_search"}], "tool_choice": "auto",
        "include": ["web_search_call.action.sources"],
        "text": {"format": {"type": "json_schema", "name": "nfl_matchup_analysis",
                            "strict": True, "schema": ANALYSIS_SCHEMA}},
        "input": [{"role": "system", "content": instructions},
                  {"role": "user", "content": user_input}]
    }
    request = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "User-Agent": "Betting-Spreads/2.0"}, method="POST"
    )
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(request, timeout=240, context=context) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise RuntimeError(
                "OpenAI rejected the API key. Check that it is valid and API billing is enabled. "
                "To replace it, remove the 'Betting Spreads' credential from Keychain Access "
                "(macOS) or Windows Credential Manager and run the app again."
            ) from exc
        raise RuntimeError(f"OpenAI API error ({exc.code}): {body[:1200]}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not connect to OpenAI: {exc}") from exc
    analysis = json.loads(_extract_output_text(result))
    sources = []
    for item in result.get("output", []):
        if item.get("type") == "web_search_call":
            action = item.get("action", {})
            source_items = action.get("sources", []) or action.get("search", {}).get("sources", [])
            for source in source_items:
                url = source.get("url")
                if url and all(existing["url"] != url for existing in sources):
                    sources.append({"title": source.get("title") or url, "url": url})
    if len(analysis.get("picks", [])) > 5 or len(analysis.get("touchdown_scorers", [])) > 3:
        raise RuntimeError("The AI returned more recommendations than the workbook supports.")
    for pick in analysis.get("picks", []):
        probability = float(pick.get("estimated_hit_probability", -1))
        confidence = int(pick.get("confidence", 0))
        if not 0 <= probability <= 100 or not 1 <= confidence <= 10:
            raise RuntimeError("The AI returned an invalid probability or confidence score.")
    for scorer in analysis.get("touchdown_scorers", []):
        probability = float(scorer.get("estimated_td_probability", -1))
        confidence = int(scorer.get("confidence", 0))
        if not 0 <= probability <= 100 or not 1 <= confidence <= 10:
            raise RuntimeError("The AI returned an invalid touchdown probability or confidence score.")
    analysis["sources"] = sources
    return analysis
