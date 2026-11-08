"""Provider-independent NFL matchup analysis with independent live research."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai_providers import generate_json
from live_research import collect_game_research

ANALYSIS_SCHEMA: dict[str, Any] = {
    "summary": "string",
    "game_script": "string",
    "picks": [{
        "rank": "integer", "market_type": "Player Prop or Game Prop",
        "player_or_team": "string", "bet_description": "string",
        "market_line": "string", "play_threshold": "string",
        "estimated_hit_probability": "number 0-100", "confidence": "integer 1-10",
        "statistical_rationale": "string", "risk_factors": "string",
        "data_quality": "Strong, Moderate, or Limited",
    }],
    "touchdown_scorers": [{
        "rank": "integer", "player": "string", "team": "string",
        "estimated_td_probability": "number 0-100", "confidence": "integer 1-10",
        "rationale": "string", "risk_factors": "string",
    }],
    "data_gaps": ["string"],
}


def _validate_analysis(data: dict[str, Any]) -> None:
    if not isinstance(data.get("summary"), str) or not isinstance(data.get("game_script"), str):
        raise RuntimeError("AI response is missing the matchup summary or game script.")
    picks = data.get("picks")
    scorers = data.get("touchdown_scorers")
    if not isinstance(picks, list) or len(picks) != 5:
        raise RuntimeError("AI response must contain exactly five betting opportunities.")
    if not isinstance(scorers, list) or len(scorers) != 3:
        raise RuntimeError("AI response must contain exactly three touchdown candidates.")
    for item in picks:
        probability = float(item.get("estimated_hit_probability", -1))
        confidence = int(item.get("confidence", 0))
        if not 0 <= probability <= 100 or not 1 <= confidence <= 10:
            raise RuntimeError("AI returned an invalid pick probability or confidence.")
        for field in ("market_type", "player_or_team", "bet_description",
                      "market_line", "play_threshold", "statistical_rationale", "risk_factors"):
            if not item.get(field):
                raise RuntimeError(f"AI response is missing a required pick field: {field}")
    for item in scorers:
        probability = float(item.get("estimated_td_probability", -1))
        confidence = int(item.get("confidence", 0))
        if not 0 <= probability <= 100 or not 1 <= confidence <= 10:
            raise RuntimeError("AI returned an invalid touchdown probability or confidence.")
    if not isinstance(data.get("data_gaps", []), list):
        raise RuntimeError("AI data_gaps must be a list.")


def analyze_game(game: dict[str, Any], research: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect current data without AI, then use the first healthy configured AI provider."""
    research = research or collect_game_research(game)
    base = Path(__file__).resolve().parent / "agents"
    instructions = (
        (base / "NFL_MATCHUP_ANALYST.md").read_text(encoding="utf-8")
        + "\n\nQUALITY REVIEW CHECKLIST\n"
        + (base / "PROBABILITY_REVIEW.md").read_text(encoding="utf-8")
    )
    matchup = f"{game['Away Team']} at {game['Home Team']}"
    user_input = (
        f"Analyze this upcoming NFL matchup: {matchup}.\n"
        f"Kickoff: {game.get('Game Date (ET)', 'Not supplied')}.\n"
        f"ESPN game line: {game.get('Betting Line', 'Unavailable')}.\n"
        f"Game status: {game.get('Game Status', 'Not supplied')}.\n\n"
        "Use the independently collected research below as source material. Distinguish current "
        "facts from historical references. You may supplement it with your own knowledge, but do "
        "not claim to have browsed or verified anything that is not in the supplied evidence. "
        "If a current prop line is not in the evidence, label it Not verified. Never fabricate "
        "injuries, player statistics, market prices, sources, or source timestamps.\n\n"
        "Return JSON matching this structure exactly (no Markdown):\n"
        + json.dumps(ANALYSIS_SCHEMA, ensure_ascii=False)
        + "\n\nINDEPENDENT RESEARCH AND SOURCE MATERIAL:\n"
        + str(research.get("research_text", "No independent research was available."))[:24000]
    )

    data, provider_name = generate_json(instructions, user_input)
    _validate_analysis(data)
    data["sources"] = list(research.get("sources", []))
    data["independent_research"] = str(research.get("research_text", ""))
    data["ai_provider"] = provider_name
    data["research_mode"] = "independent_espn_plus_ai"
    return data
