"""Provider-independent NFL matchup analysis with independent live research."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ai_providers import generate_json
from live_research import collect_game_research

MAX_RESEARCH_PROMPT_CHARS = 16000


def _bounded_research_text(research_text: str) -> tuple[str, bool]:
    """Keep complete, priority-ordered research sections within the prompt budget."""
    text = str(research_text or "")
    if len(text) <= MAX_RESEARCH_PROMPT_CHARS:
        return text, False

    kept: list[str] = []
    used = 0
    for section in text.split("\n\n"):
        separator = 2 if kept else 0
        remaining = MAX_RESEARCH_PROMPT_CHARS - used - separator
        if len(section) > remaining:
            break
        kept.append(section)
        used += separator + len(section)

    marker = "[Research truncated.]"
    while kept and used + 2 + len(marker) > MAX_RESEARCH_PROMPT_CHARS:
        removed = kept.pop()
        used -= len(removed) + (2 if kept else 0)
    if not kept:
        return marker[:MAX_RESEARCH_PROMPT_CHARS], True
    return "\n\n".join(kept) + "\n\n" + marker, True
ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "game_script": {"type": "string"},
        "picks": {
            "type": "array", "minItems": 0, "maxItems": 5,
            "items": {
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
                    "data_quality": {"type": "string", "enum": ["Strong", "Moderate", "Limited"]},
                },
                "required": [
                    "rank", "market_type", "player_or_team", "bet_description", "market_line",
                    "play_threshold", "estimated_hit_probability", "confidence",
                    "statistical_rationale", "risk_factors", "data_quality",
                ],
            },
        },
        "touchdown_scorers": {
            "type": "array", "minItems": 0, "maxItems": 3,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "rank": {"type": "integer"},
                    "player": {"type": "string"},
                    "team": {"type": "string"},
                    "availability": {"type": "string"},
                    "availability_evidence": {"type": "string"},
                    "estimated_td_probability": {"type": "number"},
                    "confidence": {"type": "integer"},
                    "rationale": {"type": "string"},
                    "risk_factors": {"type": "string"},
                },
                "required": [
                    "rank", "player", "team", "availability", "availability_evidence",
                    "estimated_td_probability", "confidence", "rationale", "risk_factors",
                ],
            },
        },
        "injury_impact": {
            "type": "array", "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "injured_player": {"type": "string"},
                    "team": {"type": "string"},
                    "availability": {"type": "string"},
                    "affected_player": {"type": "string"},
                    "affected_team": {"type": "string"},
                    "impact_direction": {
                        "type": "string",
                        "enum": ["Positive", "Negative", "Mixed"],
                    },
                    "statistical_effect": {"type": "string"},
                    "prop_market": {"type": "string"},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "integer"},
                    "conditions": {"type": "string"},
                },
                "required": [
                    "injured_player", "team", "availability",
                    "affected_player", "affected_team", "impact_direction",
                    "statistical_effect", "prop_market", "evidence",
                    "confidence", "conditions",
                ],
            },
        },
        "data_gaps": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
    },
    "required": [
        "summary", "game_script", "picks", "touchdown_scorers",
        "injury_impact", "data_gaps",
    ],
}


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _validate_analysis(
    data: dict[str, Any],
    rosters: dict[str, list[str]] | None = None,
    team_names: tuple[str, str] | None = None,
) -> None:
    if not isinstance(data.get("summary"), str) or not isinstance(data.get("game_script"), str):
        raise RuntimeError("AI response is missing the matchup summary or game script.")
    picks = data.get("picks")
    scorers = data.get("touchdown_scorers")
    if not isinstance(picks, list) or len(picks) > 5:
        raise RuntimeError("AI response may contain at most five betting opportunities.")
    if not isinstance(scorers, list) or len(scorers) > 3:
        raise RuntimeError("AI response may contain at most three touchdown candidates.")
    for item in picks:
        probability = float(item.get("estimated_hit_probability", -1))
        confidence = int(item.get("confidence", 0))
        if not 0 <= probability <= 100 or not 1 <= confidence <= 10:
            raise RuntimeError("AI returned an invalid pick probability or confidence.")
        if item.get("market_type") not in ("Player Prop", "Game Prop"):
            raise RuntimeError("AI returned an unsupported betting market type.")
        if item.get("data_quality") not in ("Strong", "Moderate", "Limited"):
            raise RuntimeError("AI returned an unsupported data-quality label.")
        for field in ("market_type", "player_or_team", "bet_description",
                      "market_line", "play_threshold", "statistical_rationale", "risk_factors"):
            if not item.get(field):
                raise RuntimeError(f"AI response is missing a required pick field: {field}")
        if rosters is not None and item.get("market_type") == "Player Prop":
            known_players = {
                _normalize_name(player)
                for roster in rosters.values()
                for player in roster
            }
            if not known_players or _normalize_name(item.get("player_or_team", "")) not in known_players:
                raise RuntimeError(
                    "AI suggested a player prop for someone not found on either current ESPN roster: "
                    + str(item.get("player_or_team", ""))
                )
    for item in scorers:
        probability = float(item.get("estimated_td_probability", -1))
        confidence = int(item.get("confidence", 0))
        if not 0 <= probability <= 100 or not 1 <= confidence <= 10:
            raise RuntimeError("AI returned an invalid touchdown probability or confidence.")
        availability = str(item.get("availability", "")).casefold()
        if team_names is not None:
            normalized_team = _normalize_name(item.get("team", ""))
            matching_team = next(
                (name for name in team_names if _normalize_name(name) == normalized_team),
                None,
            )
            if matching_team is None:
                raise RuntimeError(
                    "AI assigned a touchdown candidate to a team outside this matchup: "
                    + str(item.get("team", ""))
                )
            if rosters is not None:
                roster = next(
                    (players for name, players in rosters.items()
                     if _normalize_name(name) == _normalize_name(matching_team)),
                    [],
                )
                roster_names = {_normalize_name(player) for player in roster}
                if not roster_names or _normalize_name(item.get("player", "")) not in roster_names:
                    raise RuntimeError(
                        "AI suggested a touchdown scorer not found on the current ESPN roster: "
                        + str(item.get("player", ""))
                    )
        # Validate matchup and roster identity before status details so callers
        # receive the most actionable error when multiple fields are invalid.
        if not availability or not item.get("availability_evidence"):
            raise RuntimeError("AI touchdown candidate is missing current availability evidence.")
        if any(term in availability for term in ("out", "doubtful", "injured reserve", "ir")):
            raise RuntimeError("AI included an unavailable or doubtful touchdown candidate: " + str(item.get("player", "")))
        if "questionable" in availability and confidence > 6:
            raise RuntimeError("Questionable touchdown candidates must have confidence of 6/10 or lower.")
        if "questionable" in availability and "question" not in str(item.get("risk_factors", "")).casefold():
            raise RuntimeError("Questionable touchdown candidates must disclose the availability risk.")
    injury_impacts = data.get("injury_impact")
    if not isinstance(injury_impacts, list):
        raise RuntimeError("AI injury_impact must be a list.")

    for item in injury_impacts:
        if item.get("impact_direction") not in ("Positive", "Negative", "Mixed"):
            raise RuntimeError("AI returned an unsupported injury-impact direction.")
        confidence = int(item.get("confidence", 0))
        if not 1 <= confidence <= 10:
            raise RuntimeError("AI returned an invalid injury-impact confidence.")
        for field in (
            "injured_player", "team", "availability", "affected_player",
            "affected_team", "statistical_effect", "prop_market",
            "evidence", "conditions",
        ):
            if not item.get(field):
                raise RuntimeError(
                    f"AI injury impact is missing a required field: {field}"
                )

    if not isinstance(data.get("data_gaps", []), list):
        raise RuntimeError("AI data_gaps must be a list.")




def _deduplicate_correlated_game_picks(
    picks: list[dict[str, Any]],
    team_names: tuple[str, str],
) -> tuple[list[dict[str, Any]], bool]:
    """Keep only one same-team game pick when moneyline and spread overlap."""
    retained = list(picks)
    removed_any = False

    def team_key(value: str) -> str | None:
        normalized = _normalize_name(value)
        for team in team_names:
            team_normalized = _normalize_name(team)
            if normalized and (normalized in team_normalized or team_normalized in normalized):
                return team_normalized
        return None

    def market_kind(pick: dict[str, Any]) -> str | None:
        text = " ".join(str(pick.get(field, "")) for field in (
            "bet_description", "market_line", "play_threshold"
        )).casefold()
        if re.search(r"money\s*line|\bml\b", text):
            return "moneyline"
        if re.search(r"\bspread\b|[+-]\s*\d+(?:\.\d+)?", text):
            return "spread"
        return None

    for team in team_names:
        candidates = [
            pick for pick in retained
            if pick.get("market_type") == "Game Prop"
            and team_key(str(pick.get("player_or_team", ""))) == _normalize_name(team)
            and market_kind(pick) in {"moneyline", "spread"}
        ]
        kinds = {market_kind(pick) for pick in candidates}
        if {"moneyline", "spread"}.issubset(kinds):
            # Confidence reflects evidence reliability. If tied, keep the higher
            # probability option; never list both overlapping outcomes as separate edges.
            winner = max(
                candidates,
                key=lambda pick: (
                    int(pick.get("confidence", 0)),
                    float(pick.get("estimated_hit_probability", 0)),
                    {"Strong": 3, "Moderate": 2, "Limited": 1}.get(pick.get("data_quality"), 0),
                ),
            )
            retained = [
                pick for pick in retained
                if pick not in candidates or pick is winner
            ]
            removed_any = True

    retained.sort(key=lambda pick: (
        int(pick.get("rank", 999)),
        -int(pick.get("confidence", 0)),
        -float(pick.get("estimated_hit_probability", 0)),
    ))
    for rank, pick in enumerate(retained, 1):
        pick["rank"] = rank
    return retained, removed_any


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
    team_names = (str(game["Away Team"]), str(game["Home Team"]))
    prompt_research, research_truncated = _bounded_research_text(
        research.get("research_text", "No independent research was available.")
    )

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
        "Return one JSON object only, with exactly these top-level keys: "
        "summary (string), game_script (string), picks (array of up to 5), "
        "touchdown_scorers (array of up to 3), injury_impact (array), "
        "data_gaps (array of strings). Each pick object must contain: rank (integer), "
        "market_type (Player Prop or Game Prop), player_or_team, bet_description, "
        "market_line, play_threshold, estimated_hit_probability (number 0-100), "
        "confidence (integer 1-10), statistical_rationale, risk_factors, "
        "data_quality (Strong, Moderate, or Limited). Only include evidence-supported picks. "
        f"Only use these matchup teams: {team_names[0]} and {team_names[1]}. "
        "Each Player Prop must name a player exactly as listed on one of the supplied current ESPN rosters. "
        "Each touchdown_scorer team must exactly match one of the two matchup teams, and the player "
        "must exactly match that team's supplied roster. "
        "Aim for 4-5 strong picks when evidence supports them; do not stop at 2-3 merely to be concise. "
        "Target a balanced shortlist with 2-3 player props when current player logs support them, "
        "plus the strongest game props. Explicitly assess every researched QB, RB, WR and TE log, "
        "including secondary options and players identified as beneficiaries in injury_impact. "
        "For each candidate compare recent games with season rates, role/opportunity evidence, "
        "and opponent weakness; do not require a verified sportsbook prop line to assess a model "
        "threshold, but label the line Not verified. Do not omit a strong player prop solely because "
        "the game markets are easier to discuss. "
        "Never list both a team's moneyline and that same team's spread; choose only the stronger "
        "single recommendation. A spread covering a team win is a subset of its moneyline outcome, "
        "so its estimated hit probability cannot exceed the moneyline probability. "
        "If confidence is higher for one overlapping market, retain that one rather than duplicating. "
        "For TD candidates, check the latest injury status and practice comment for each player. "
        "Include required availability and availability_evidence fields. Questionable players may "
        "remain only conditionally, with confidence no higher than 6/10 and explicit risk disclosure; "
        "exclude doubtful, out, or injured-reserve players. Never infer active status from roster presence. "
        "If the roster does not support a player or evidence is inadequate, omit that candidate; "
        "shorter arrays are acceptable. Keep the JSON compact: summary <= 70 words; game_script "
        "<= 90 words; each pick statistical_rationale <= 35 words and risk_factors <= 20 words; "
        "each TD rationale <= 25 words and risk_factors <= 15 words; return at most 3 meaningful "
        "injury_impact entries, with each text field <= 18 words; data_gaps <= 6 short items. "
        "Use concise phrases, avoid repeating research, and do not add commentary outside JSON. "
        "Each touchdown_scorer must "
        "contain: rank, player, team, estimated_td_probability (number 0-100), "
        "availability (current status), availability_evidence (latest dated practice/injury evidence), "
        "confidence (integer 1-10), concise rationale, concise risk_factors. Each injury_impact "
        "must contain: injured_player, team, availability, affected_player, "
        "affected_team, impact_direction (Positive, Negative, or Mixed), "
        "statistical_effect, prop_market, evidence, confidence (integer 1-10), "
        "conditions. Include every listed field; use empty arrays when appropriate.\n"
        "\nINDEPENDENT RESEARCH AND SOURCE MATERIAL:\n"
        + prompt_research
    )

    rosters = research.get("rosters")
    data, provider_name = generate_json(
        instructions,
        user_input,
        validator=lambda result: _validate_analysis(
            result, rosters=rosters, team_names=team_names
        ),
    )
    picks, removed_correlated = _deduplicate_correlated_game_picks(
        data.get("picks", []), team_names
    )
    data["picks"] = picks
    if removed_correlated:
        data.setdefault("data_gaps", []).append(
            "Overlapping same-team moneyline/spread recommendations were deduplicated; only the higher-confidence option was retained."
        )
    data["sources"] = list(research.get("sources", []))
    data["independent_research"] = str(research.get("research_text", ""))
    data["ai_provider"] = provider_name
    data["research_mode"] = "independent_espn_plus_ai"
    data["research_truncated_for_prompt"] = research_truncated
    data["research_prompt_characters"] = len(prompt_research)
    return data
