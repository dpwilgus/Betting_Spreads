"""Provider-independent NFL matchup analysis with independent live research."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ai_providers import generate_json
from live_research import collect_game_research

MAX_RESEARCH_PROMPT_CHARS = 40000


def _bounded_research_text(research_text: str) -> tuple[str, bool]:
    """Prioritize player usage and matchup evidence over bulky roster/injury listings."""
    text = str(research_text or "")
    if len(text) <= MAX_RESEARCH_PROMPT_CHARS:
        return text, False

    sections = text.split("\n\n")
    prioritized = []
    for index, section in enumerate(sections):
        upper = section[:120].upper()
        # Structured rosters are independently validated in code; selected-player
        # game logs already provide the names the model should evaluate.
        if upper.startswith("CURRENT ESPN ACTIVE TEAM ROSTER"):
            continue
        if upper.startswith("INDEPENDENT LIVE NFL RESEARCH") or upper.startswith("MATCHUP:") \
                or upper.startswith("KICKOFF") or upper.startswith("SCOREBOARD LINE") \
                or upper.startswith("RESEARCH RETRIEVED") or upper.startswith("MARKET DATA RULE"):
            priority = 0
        elif upper.startswith("ESPN MATCHUP, PLAYER, INJURY, MARKET"):
            priority = 1
        elif upper.startswith("ESPN RECENT TEAM NEWS"):
            priority = 1
        elif upper.startswith("ESPN CURRENT-SEASON PLAYER GAME LOGS"):
            priority = 2
        elif upper.startswith("ESPN CURRENT PLAYER INJURIES") or upper.startswith("ESPN GAME SUMMARY INJURY DATA"):
            priority = 3
        elif upper.startswith("ESPN CURRENT-SEASON TEAM STATISTICS"):
            priority = 4
        else:
            priority = 6
        prioritized.append((priority, index, section))

    prioritized.sort(key=lambda item: (item[0], item[1]))
    kept: list[str] = []
    used = 0
    omitted = len(prioritized) != len(sections)
    marker = "[Research truncated.]"
    budget = max(0, MAX_RESEARCH_PROMPT_CHARS - len(marker) - 2)
    for _, _, section in prioritized:
        separator = 2 if kept else 0
        if len(section) + separator > budget - used:
            omitted = True
            continue
        kept.append(section)
        used += separator + len(section)

    if not omitted:
        return "\n\n".join(kept), False
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
            "type": "array", "minItems": 0,
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
        "sneaky_props": {
            "type": "array", "maxItems": 8,
            "items": {"type": "object", "additionalProperties": False,
                "properties": {
                    "rank": {"type": "integer"}, "market_type": {"type": "string", "enum": ["Player Prop", "Game Prop"]},
                    "player_or_team": {"type": "string"}, "bet_description": {"type": "string"},
                    "market_line": {"type": "string"}, "play_threshold": {"type": "string"},
                    "estimated_hit_probability": {"type": "number"}, "confidence": {"type": "integer"},
                    "statistical_rationale": {"type": "string"}, "risk_factors": {"type": "string"},
                    "data_quality": {"type": "string", "enum": ["Strong", "Moderate", "Limited"]}
                },
                "required": ["rank","market_type","player_or_team","bet_description","market_line","play_threshold",
                    "estimated_hit_probability","confidence","statistical_rationale","risk_factors","data_quality"]
            }
        },
        "touchdown_scorers": {
            "type": "array", "minItems": 0, "maxItems": 5,
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
            "type": "array", "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "impact_type": {"type": "string", "enum": ["Injury", "News", "Role Change", "Return"]},
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
                    "impact_type", "injured_player", "team", "availability",
                    "affected_player", "affected_team", "impact_direction",
                    "statistical_effect", "prop_market", "evidence",
                    "confidence", "conditions",
                ],
            },
        },
        "data_gaps": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
    },
    "required": [
        "summary", "game_script", "picks", "sneaky_props", "touchdown_scorers",
        "injury_impact", "data_gaps",
    ],
}


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _is_unavailable_status(value: str) -> bool:
    """Recognize explicit inactive designations without broad substring matches."""
    status = re.sub(r"[^a-z ]", " ", str(value).casefold())
    status = " ".join(status.split())
    return (
        status in {"out", "doubtful", "ir", "pup", "nfi"}
        or "injured reserve" in status
        or "physically unable to perform" in status
        or status.startswith("out ")
        or status.startswith("doubtful ")
    )


def _apply_independent_injury_statuses(
    data: dict[str, Any],
    injury_statuses: list[dict[str, Any]] | None,
) -> list[str]:
    """Remove unavailable player picks and TD candidates using independent ESPN reports."""
    unavailable: dict[str, str] = {}
    questionable: set[str] = set()
    for report in injury_statuses or []:
        if not isinstance(report, dict):
            continue
        player_key = _normalize_name(str(report.get("player", "")))
        status = str(report.get("status", "")).strip()
        if not player_key or not status:
            continue
        if _is_unavailable_status(status):
            unavailable[player_key] = status
        elif "questionable" in status.casefold():
            questionable.add(player_key)

    gaps: list[str] = []
    reported_players = set(unavailable) | set(questionable)
    for collection, name_field in (("picks", "player_or_team"), ("touchdown_scorers", "player")):
        retained = []
        for item in data.get(collection, []):
            if collection == "picks" and item.get("market_type") != "Player Prop":
                retained.append(item)
                continue
            player = str(item.get(name_field, ""))
            player_key = _normalize_name(player)
            status = unavailable.get(player_key)
            if status:
                gaps.append(
                    f"Removed {player} from {collection.replace('_', ' ')}: ESPN lists {status}."
                )
                continue
            if player_key in questionable:
                item["confidence"] = min(int(item.get("confidence", 6)), 6)
                risk = str(item.get("risk_factors", "")).strip()
                if "question" not in risk.casefold():
                    item["risk_factors"] = (
                        "Questionable: availability unconfirmed"
                        if not risk else "Questionable: availability unconfirmed; " + risk
                    )
            elif player_key not in reported_players:
                risk = str(item.get("risk_factors", "")).strip()
                if "availability" not in risk.casefold():
                    item["risk_factors"] = (
                        "Availability not independently verified"
                        if not risk else "Availability not independently verified; " + risk
                    )
                if collection == "touchdown_scorers":
                    item["availability"] = "Not independently verified"
                    item["availability_evidence"] = (
                        "No matching ESPN injury designation was found; active status is not confirmed."
                    )
            retained.append(item)
        data[collection] = retained
    return gaps



def _research_player_usage(research_text: str) -> dict[str, dict[str, Any]]:
    """Extract structured current-season player usage from independent ESPN log sections."""
    usage_by_player: dict[str, dict[str, Any]] = {}
    for line in str(research_text or "").splitlines():
        if not line.startswith("ESPN CURRENT-SEASON PLAYER GAME LOGS"):
            continue
        marker = line.find(": [")
        if marker < 0:
            continue
        try:
            records = json.loads(line[marker + 2:])
        except (TypeError, ValueError):
            continue
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            player = str(record.get("player") or "").strip()
            usage = record.get("usage_and_recent_stats")
            if not isinstance(usage, dict):
                # The active legacy collector stores the full normalized ESPN log.
                game_log = record.get("season_game_log")
                if isinstance(game_log, dict):
                    totals_by_category = game_log.get("season_totals", {})
                    totals = (
                        next(iter(totals_by_category.values()), {})
                        if isinstance(totals_by_category, dict) else {}
                    )
                    usage = {
                        "games_available": game_log.get("games_available", 0),
                        "season": totals if isinstance(totals, dict) else {},
                        "recent": game_log.get("recent_games", []),
                    }
            if player and isinstance(usage, dict):
                usage_by_player[_normalize_name(player)] = {
                    "player": player,
                    "position": str(record.get("position") or "").upper(),
                    "research_reason": str(record.get("research_reason") or ""),
                    **usage,
                }
    return usage_by_player


def _stat_number(stats: dict[str, Any], key: str) -> float:
    try:
        return float(str(stats.get(key, 0)).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _filter_player_props_by_usage(
    data: dict[str, Any],
    player_usage: dict[str, dict[str, Any]],
) -> list[str]:
    """Reject player props that materially contradict independent season/recent production."""
    stat_patterns = (
        (r"passing yards?", "passingYards"),
        (r"rushing yards?", "rushingYards"),
        (r"receiving yards?", "receivingYards"),
        (r"receptions?", "receptions"),
        (r"receiving targets?|targets?", "receivingTargets"),
        (r"rushing attempts?|carries", "rushingAttempts"),
        (r"passing attempts?", "passingAttempts"),
        (r"completions?", "completions"),
        (r"interceptions?", "interceptions"),
        (r"sacks?", "sacks"),
        (r"tackles for loss", "tacklesForLoss"),
        (r"quarterback hits?", "quarterbackHits"),
        (r"tackles?", "totalTackles"),
        (r"passes defended", "passesDefended"),
        (r"passing touchdowns?", "passingTouchdowns"),
        (r"rushing touchdowns?", "rushingTouchdowns"),
        (r"receiving touchdowns?", "receivingTouchdowns"),
        (r"forced fumbles?", "forcedFumbles"),
        (r"fumble recoveries?", "fumbleRecoveries"),
        (r"field goals? made", "fieldGoalsMade"),
        (r"extra points? made", "extraPointsMade"),
        (r"punting yards?", "puntingYards"),
        (r"punts?", "punts"),
        (r"longest reception", "longReception"),
    )
    retained = []
    gaps = []
    for pick in data.get("picks", []):
        if pick.get("market_type") != "Player Prop":
            retained.append(pick)
            continue
        player = str(pick.get("player_or_team") or "")
        usage = player_usage.get(_normalize_name(player))
        if not usage:
            gaps.append(f"Removed {player} player prop: no structured ESPN season game log was available.")
            continue
        description = str(pick.get("bet_description") or "").casefold()
        stat_key = next(
            (key for pattern, key in stat_patterns if re.search(pattern, description)),
            None,
        )
        threshold_text = str(pick.get("play_threshold") or pick.get("bet_description") or "")
        threshold_match = re.search(r"(-?\d+(?:\.\d+)?)", threshold_text)
        if not stat_key or not threshold_match:
            gaps.append(f"Removed {player} player prop: market statistic or numeric threshold could not be validated.")
            continue
        threshold = float(threshold_match.group(1))
        games = int(usage.get("games_available") or 0)
        season = usage.get("season") if isinstance(usage.get("season"), dict) else {}
        if games <= 0 or stat_key not in season:
            gaps.append(f"Removed {player} player prop: ESPN did not provide the required season statistic.")
            continue
        season_average = _stat_number(season, stat_key) / games
        recent = usage.get("recent") if isinstance(usage.get("recent"), list) else []
        recent_values = [
            _stat_number(game.get("stats", {}), stat_key)
            for game in recent[:5]
            if isinstance(game, dict) and isinstance(game.get("stats"), dict)
            and stat_key in game["stats"]
        ]
        recent_average = (
            sum(recent_values) / len(recent_values) if recent_values else season_average
        )
        if any(period in description for period in ("first quarter", "1st quarter", "first qtr", "1st qtr", "first-quarter", "1q", "q1", "first half", "1st half", "first-half", "1st-half", "1h", "h1")):
            retained.append(pick)
            continue
        is_over = bool(re.search(r"\bover\b", description))
        is_under = bool(re.search(r"\bunder\b", description))
        unsupported = (
            is_over and threshold > max(season_average, recent_average, 0.1) * 1.75
        ) or (
            is_under and threshold < min(season_average, recent_average) * 0.50
        )
        if unsupported:
            gaps.append(
                f"Removed {player} {stat_key} prop: {threshold:g} threshold materially conflicts "
                f"with ESPN season average {season_average:.1f} and recent average {recent_average:.1f}."
            )
            continue
        retained.append(pick)
    data["picks"] = retained
    return gaps


def _filter_td_scorers_by_usage(
    data: dict[str, Any],
    player_usage: dict[str, dict[str, Any]],
) -> list[str]:
    """Require a credible current-season scoring role; quarterbacks need rushing evidence."""
    ranked = []
    gaps = []
    for scorer in data.get("touchdown_scorers", []):
        player = str(scorer.get("player") or "")
        usage = player_usage.get(_normalize_name(player))
        if not usage:
            gaps.append(f"Removed {player} TD candidate: no structured ESPN season game log was available.")
            continue
        position = str(usage.get("position") or "").upper()
        season = usage.get("season") if isinstance(usage.get("season"), dict) else {}
        games = int(usage.get("games_available") or 0)
        if games <= 0:
            gaps.append(f"Removed {player} TD candidate: ESPN season game-log sample was unavailable.")
            continue
        rush_td = _stat_number(season, "rushingTouchdowns")
        receiving_td = _stat_number(season, "receivingTouchdowns")
        carries = _stat_number(season, "rushingAttempts")
        targets = _stat_number(season, "receivingTargets")
        total_td = rush_td + receiving_td
        recent = usage.get("recent") if isinstance(usage.get("recent"), list) else []
        recent_td = 0.0
        for game in recent[:5]:
            stats = game.get("stats", {}) if isinstance(game, dict) else {}
            if not isinstance(stats, dict):
                continue
            if position == "QB":
                recent_td += _stat_number(stats, "rushingTouchdowns")
            elif position == "RB":
                recent_td += (
                    _stat_number(stats, "rushingTouchdowns")
                    + _stat_number(stats, "receivingTouchdowns")
                )
            elif position in {"WR", "TE"}:
                recent_td += _stat_number(stats, "receivingTouchdowns")
        recent_rate = recent_td / max(1, len(recent[:5]))
        if position == "QB":
            carries_per_game = carries / games
            eligible = rush_td >= 3 or (rush_td >= 2 and carries_per_game >= 3.0)
            score = (rush_td / games) * 3.0 + carries_per_game * 0.12 + recent_rate
        elif position == "RB":
            opportunities_per_game = (carries + targets) / games
            eligible = (
                (total_td >= 1 and opportunities_per_game >= 4.0)
                or opportunities_per_game >= 12.0
            )
            score = (total_td / games) * 3.0 + opportunities_per_game * 0.08 + recent_rate
        elif position in {"WR", "TE"}:
            targets_per_game = targets / games
            eligible = (
                (receiving_td >= 1 and targets_per_game >= 2.5)
                or targets_per_game >= 6.0
            )
            score = (receiving_td / games) * 3.0 + targets_per_game * 0.12 + recent_rate
        else:
            eligible = False
            score = 0.0
        if not eligible:
            gaps.append(
                f"Removed {player} TD candidate: current ESPN {position or 'position'} usage/scoring "
                "does not support an anytime-TD recommendation."
            )
            continue
        ranked.append((score, scorer))
    ranked.sort(key=lambda item: item[0], reverse=True)
    data["touchdown_scorers"] = [scorer for _, scorer in ranked[:3]]
    for rank, scorer in enumerate(data["touchdown_scorers"], 1):
        scorer["rank"] = rank
    return gaps


def _validate_analysis(
    data: dict[str, Any],
    rosters: dict[str, list[str]] | None = None,
    team_names: tuple[str, str] | None = None,
) -> None:
    if not isinstance(data.get("summary"), str) or not isinstance(data.get("game_script"), str):
        raise RuntimeError("AI response is missing the matchup summary or game script.")
    picks = data.get("picks")
    scorers = data.get("touchdown_scorers")
    if not isinstance(picks, list):
        raise RuntimeError("AI response betting opportunities must be a list.")
    if not isinstance(scorers, list) or len(scorers) > 5:
        raise RuntimeError("AI response may contain at most five preliminary touchdown candidates.")
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
    sneaky = data.get("sneaky_props")
    if not isinstance(sneaky, list) or len(sneaky) > 8:
        raise RuntimeError("AI sneaky_props must be a list of at most eight ideas.")
    for item in sneaky:
        probability = float(item.get("estimated_hit_probability", -1))
        confidence = int(item.get("confidence", 0))
        if not 0 <= probability <= 100 or not 1 <= confidence <= 10:
            raise RuntimeError("AI returned an invalid sneaky-prop probability or confidence.")
        if item.get("market_type") not in ("Player Prop", "Game Prop"):
            raise RuntimeError("AI returned an unsupported sneaky-prop market type.")
        if not all(item.get(field) for field in (
            "player_or_team", "bet_description", "market_line", "play_threshold",
            "statistical_rationale", "risk_factors", "data_quality",
        )):
            raise RuntimeError("AI sneaky prop is missing required evidence fields.")
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
        if _is_unavailable_status(availability):
            raise RuntimeError("AI included an unavailable or doubtful touchdown candidate: " + str(item.get("player", "")))
        if "questionable" in availability and confidence > 6:
            raise RuntimeError("Questionable touchdown candidates must have confidence of 6/10 or lower.")
        if "questionable" in availability and "question" not in str(item.get("risk_factors", "")).casefold():
            existing_risk = str(item.get("risk_factors", "")).strip()
            item["risk_factors"] = (
                "Questionable: availability unconfirmed"
                if not existing_risk
                else "Questionable: availability unconfirmed; " + existing_risk
            )
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





def _filter_injury_impacts(
    data: dict[str, Any],
    injury_statuses: list[dict[str, Any]],
    rosters: dict[str, list[str]] | None,
    team_names: tuple[str, str],
    player_usage: dict[str, dict[str, Any]] | None = None,
    news_evidence_text: str | None = None,
) -> list[str]:
    """Keep impacts grounded in ESPN injury reports or the supplied team-news headlines."""
    reports = {
        (_normalize_name(str(item.get("team") or "")),
         _normalize_name(str(item.get("player") or ""))): item
        for item in injury_statuses
        if isinstance(item, dict) and item.get("team") and item.get("player")
    }
    roster_sets = {
        _normalize_name(team): {_normalize_name(player) for player in players}
        for team, players in (rosters or {}).items()
    }
    team_lookup = {_normalize_name(team): team for team in team_names}
    retained = []
    gaps = []
    for impact in data.get("injury_impact", []):
        injured_player = str(impact.get("injured_player") or "")
        injured_team = str(impact.get("team") or "")
        affected_player = str(impact.get("affected_player") or "")
        affected_team = str(impact.get("affected_team") or "")
        injured_player_key = _normalize_name(injured_player)
        affected_player_key = _normalize_name(affected_player)
        injured_team_key = _normalize_name(injured_team)
        affected_team_key = _normalize_name(affected_team)
        if injured_player_key and injured_player_key == affected_player_key:
            gaps.append(
                f"Removed injury impact for {affected_player}: an injured player cannot be listed as their own beneficiary."
            )
            continue
        unit_impact = affected_player_key in {"teamunit", "offensiveunit", "defensiveunit", "specialteamsunit"}
        if player_usage is not None and affected_player_key not in player_usage and not unit_impact:
            gaps.append(
                f"Removed injury impact for {affected_player}: no structured game-log evidence was collected for the proposed beneficiary."
            )
            continue
        impact_type = str(impact.get("impact_type") or "Injury")
        report = reports.get((injured_team_key, injured_player_key))
        if injured_team_key not in team_lookup:
            gaps.append(f"Removed impact for {affected_player}: subject team is outside this matchup.")
            continue
        if impact_type == "Injury":
            if report is None:
                gaps.append(
                    f"Removed injury impact for {affected_player}: injured player/team did not match an ESPN injury report."
                )
                continue
            if not _is_unavailable_status(str(report.get("status") or "")) and \
                    "questionable" not in str(report.get("status") or "").casefold():
                gaps.append(
                    f"Removed injury impact for {affected_player}: no meaningful injury designation was verified."
                )
                continue
        elif not impact.get("evidence") or (
            news_evidence_text is not None
            and _normalize_name(affected_player) not in _normalize_name(news_evidence_text)
            and _normalize_name(injured_player) not in _normalize_name(news_evidence_text)
        ):
            gaps.append(f"Removed news impact for {affected_player}: no matching ESPN team-news evidence was found.")
            continue
        if affected_team_key not in team_lookup or (
                not unit_impact
                and _normalize_name(affected_player) not in roster_sets.get(affected_team_key, set())
        ):
            gaps.append(
                f"Removed injury impact for {affected_player}: affected player was not found on the stated team's roster."
            )
            continue
        affected_report = reports.get((affected_team_key, _normalize_name(affected_player)))
        if affected_report and _is_unavailable_status(str(affected_report.get("status") or "")):
            gaps.append(
                f"Removed injury impact for {affected_player}: ESPN lists the affected player "
                f"{affected_report.get('status')}."
            )
            continue
        retained.append(impact)
    data["injury_impact"] = retained
    return gaps


def _separate_unverified_player_props(
    data: dict[str, Any],
    research: dict[str, Any],
) -> list[str]:
    """Move model-only thresholds out of actionable sportsbook recommendations."""
    verified_quotes = research.get("verified_player_prop_markets", [])
    actionable = []
    model_only = []
    gaps = []
    for pick in data.get("picks", []):
        if pick.get("market_type") != "Player Prop":
            actionable.append(pick)
            continue
        player_key = _normalize_name(str(pick.get("player_or_team") or ""))
        description = str(pick.get("bet_description") or "").casefold()
        matched = None
        for quote in (verified_quotes if isinstance(verified_quotes, list) else []):
            if not isinstance(quote, dict):
                continue
            quote_player = _normalize_name(str(quote.get("player") or ""))
            quote_market = str(quote.get("market") or "").casefold()
            market_match = any(
                token in description and token in quote_market
                for token in ("passing yards", "rushing yards", "receiving yards",
                              "receptions", "receiving targets", "rushing attempts",
                              "passing attempts", "completions", "interceptions",
                              "sacks", "tackles", "passes defended", "longest reception",
                              "passing touchdowns", "rushing touchdowns", "receiving touchdowns",
                              "tackles for loss", "quarterback hits", "forced fumbles",
                              "fumble recoveries", "field goals made", "extra points made", "punts")
            )
            if (
                quote_player == player_key and market_match
                and quote.get("line") not in (None, "")
                and quote.get("price") not in (None, "")
                and quote.get("sportsbook") and quote.get("source_url")
                and quote.get("retrieved_at")
            ):
                matched = quote
                break
        if matched:
            pick["market_line"] = (
                f"{matched['line']} ({matched['price']}) — {matched['sportsbook']}"
            )
            pick["market_source"] = matched["source_url"]
            pick["market_retrieved_at"] = matched["retrieved_at"]
            actionable.append(pick)
        else:
            pick["market_line"] = "NOT VERIFIED — MODEL THRESHOLD ONLY"
            model_only.append(pick)
            gaps.append(
                f"{pick.get('player_or_team')} {pick.get('bet_description')}: no verified sportsbook line/price; moved to model-only leans."
            )
    data["picks"] = actionable
    data["model_only_picks"] = model_only
    # Sneaky player-prop lines are not treated as quoted sportsbook markets.
    for pick in data.get("sneaky_props", []):
        if pick.get("market_type") == "Player Prop":
            pick["market_line"] = "NOT VERIFIED — MODEL THRESHOLD ONLY"
    return gaps

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
        "summary (string), game_script (string), picks (array), "
        "sneaky_props (array), touchdown_scorers (array of up to 5 preliminary candidates), injury_impact (array), "
        "data_gaps (array of strings). Each pick object must contain: rank (integer), "
        "market_type (Player Prop or Game Prop), player_or_team, bet_description, "
        "market_line, play_threshold, estimated_hit_probability (number 0-100), "
        "confidence (integer 1-10), statistical_rationale, risk_factors, "
        "data_quality (Strong, Moderate, or Limited). Only include evidence-supported picks. "
        f"Only use these matchup teams: {team_names[0]} and {team_names[1]}. "
        "Each Player Prop must name a player with a structured ESPN game log in the supplied research. "
        "Each touchdown_scorer team must exactly match one of the two matchup teams, and the player "
        "must have a structured ESPN game log for that team. "
        "Player props: target the strongest evidence-supported markets, not a fixed count; aim for at least "
        "three when supported. If no sportsbook line exists, label the idea model-only, not an actionable "
        "bet. Never invent a line. Keep game bets separate. Evaluate every researched offensive player and defensive starter/leader log. "
        "Expand across evidence-supported markets: passing/rushing/receiving attempts, completions, passing/rushing/receiving TDs, interceptions, receptions, targets, longest reception, sacks, tackles, tackles for loss, QB hits, passes defended, forced fumbles, fumble recoveries, kicker field goals/extra points, punter volume, and other researched stats. "
        "Evaluate full-game, first-quarter, first-half, team-total, spread, and total markets when quotes or matchup evidence support them. "
        "Never invent sportsbook availability or prices. Include alternate lines and ladders only when evidence supports a distinct high-upside angle. "
        "Separate best recommendations in picks from speculative high-upside Sneaky Props; do not duplicate exact markets. "
        "Never list the same team's moneyline and spread together; keep only the stronger option. A spread's hit probability cannot exceed that team's moneyline probability. "
        "TDs: compare every researched player's season TDs, carries/targets per game, and recent scoring. "
        "Passing TDs do not count for a QB's anytime TD; require at least one rushing TD or 3+ rush attempts/game. "
        "Never select a QB just for being the starter. For injury impacts, never list the injured player "
        "as their own affected_player. Include meaningful absences for offensive starters and high-impact defensive leaders (tackles, sacks, interceptions, pressure), plus verified news/role changes such as starting-QB changes even when the subject is not injured. Assess downstream beneficiaries including replacement TEs/WRs/RBs gaining snaps or targets. If no individual beneficiary is supported, use affected_player='Team unit' rather than omitting a material starter absence. For an injured QB/RB/WR/TE, assess the next available same-position "
        "candidates explicitly marked as potential replacements; for defensive injuries, assess the opposing "
        "team's researched offensive players. A replacement is a candidate, not an automatic positive impact. "
        "The matchup summary must compare both defenses using supplied passing/rushing efficiency, yards or "
        "points allowed, pressure, sacks, takeaways, and recent-game evidence where available. Explain the "
        "implications for key offensive players and state which defensive metrics are missing. Incorporate "
        "recent team-news headlines, especially QB changes, trades, injuries, and roster moves, with dates; "
        "distinguish confirmed transactions from speculation. "
        "Questionable players may "
        "remain only conditionally, with confidence no higher than 6/10 and explicit risk disclosure. "
        "For Questionable TD candidates, risk_factors must start 'Questionable:' "
        "(e.g. 'Questionable: active status unconfirmed'). "
        "Exclude doubtful, out, or injured-reserve players. Never infer active status from roster presence. "
        "If the roster does not support a player or evidence is inadequate, omit that candidate. Keep output extremely compact for free-model limits: "
        "summary <= 90 words; game_script <= 65 words; each pick statistical_rationale <= 24 words "
        "and risk_factors <= 14 words; each TD rationale <= 16 words and risk_factors <= 12 words; "
        "return every material injury/news impact supported by evidence (up to 12), each text field <= 16 words; "
        "data_gaps <= 4 short items. Use short phrases, not paragraphs. Do not repeat facts "
        "across fields or add commentary outside JSON. "
        "Each touchdown_scorer must "
        "contain: rank, player, team, estimated_td_probability (number 0-100), "
        "availability (current status), availability_evidence (latest dated practice/injury evidence), "
        "confidence (integer 1-10), concise rationale, concise risk_factors. Each injury_impact "
        "must contain: impact_type (Injury, News, Role Change, or Return), injured_player (subject player/news subject), team, availability, affected_player, "
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
    injury_gaps = _apply_independent_injury_statuses(
        data, research.get("injury_statuses", [])
    )
    sneaky_data = {"picks": data.get("sneaky_props", [])}
    sneaky_status_gaps = _apply_independent_injury_statuses(
        sneaky_data, research.get("injury_statuses", [])
    )
    data["sneaky_props"] = sneaky_data.get("picks", [])
    player_usage = _research_player_usage(research.get("research_text", ""))
    prop_gaps = _filter_player_props_by_usage(data, player_usage)
    sneaky_usage_data = {"picks": data.get("sneaky_props", [])}
    sneaky_usage_gaps = _filter_player_props_by_usage(sneaky_usage_data, player_usage)
    data["sneaky_props"] = sneaky_usage_data.get("picks", [])
    td_gaps = _filter_td_scorers_by_usage(data, player_usage)
    news_evidence_text = "\n".join(
        line for line in str(research.get("research_text", "")).splitlines()
        if "ESPN RECENT TEAM NEWS" in line
    )
    impact_gaps = _filter_injury_impacts(
        data, research.get("injury_statuses", []), research.get("rosters"),
        team_names, player_usage=player_usage, news_evidence_text=news_evidence_text
    )
    market_gaps = _separate_unverified_player_props(data, research)
    quality_gaps = injury_gaps + sneaky_status_gaps + prop_gaps + sneaky_usage_gaps + td_gaps + impact_gaps + market_gaps
    if quality_gaps:
        existing_gaps = data.setdefault("data_gaps", [])
        existing_gaps.extend(quality_gaps[:max(0, 6 - len(existing_gaps))])
    picks, removed_correlated = _deduplicate_correlated_game_picks(
        data.get("picks", []), team_names
    )
    data["picks"] = picks
    if removed_correlated:
        data.setdefault("data_gaps", []).append(
            "Overlapping same-team moneyline/spread recommendations were deduplicated; only the higher-confidence option was retained."
        )
    data["sneaky_props"] = data.get("sneaky_props", [])
    data["analysis_version"] = 6
    data["selected_player_candidates"] = research.get("selected_player_candidates", {})
    data["researched_player_evidence"] = player_usage
    data["sources"] = list(research.get("sources", []))
    data["independent_research"] = str(research.get("research_text", ""))
    data["ai_provider"] = provider_name
    data["research_mode"] = "independent_espn_plus_ai"
    data["research_truncated_for_prompt"] = research_truncated
    data["research_prompt_characters"] = len(prompt_research)
    return data
