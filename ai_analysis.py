"""Provider-independent NFL matchup analysis with independent live research."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ai_providers import generate_json
from live_research import collect_game_research

MAX_RESEARCH_PROMPT_CHARS = 40000


def _compact_matchup_evidence(section: str) -> str:
    """Drop bulky recap trees while preserving venue, weather, markets and ATS evidence."""
    if not section.startswith("ESPN MATCHUP, PLAYER, INJURY, MARKET"):
        return section
    prefix, separator, payload_text = section.partition(":")
    if not separator:
        return section
    try:
        payload = json.loads(payload_text.strip())
    except (TypeError, ValueError):
        return section
    keep = ("retrieved_at_utc", "header", "game_info", "pickcenter_markets",
            "against_the_spread", "omitted_oversized_sections")
    compact = {key: payload[key] for key in keep if key in payload}
    return prefix + ": " + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _bounded_research_text(
    research_text: str, max_chars: int | None = None
) -> tuple[str, bool]:
    """Keep priority evidence within a provider-specific character budget.

    Resolve the module default at call time so tests and runtime configuration
    can safely override MAX_RESEARCH_PROMPT_CHARS after import.
    """
    text = str(research_text or "")
    if max_chars is None:
        max_chars = MAX_RESEARCH_PROMPT_CHARS
    max_chars = max(100, int(max_chars))
    if len(text) <= max_chars:
        return text, False

    sections = text.split("\n\n")
    prioritized = []
    for index, original in enumerate(sections):
        upper = original[:120].upper()
        if upper.startswith("CURRENT ESPN ACTIVE TEAM ROSTER"):
            continue
        section = _compact_matchup_evidence(original)
        upper = section[:120].upper()
        if (upper.startswith("HEADER") or upper.startswith("INJURIES:")
                or upper.startswith("CURRENT INJURY EVIDENCE")
                or upper.startswith("INDEPENDENT LIVE NFL RESEARCH")
                or upper.startswith("MATCHUP:") or upper.startswith("KICKOFF")
                or upper.startswith("SCOREBOARD LINE") or upper.startswith("RESEARCH RETRIEVED")
                or upper.startswith("MARKET DATA RULE")
                or upper.startswith("ESPN CURRENT PLAYER INJURIES")
                or upper.startswith("ESPN GAME SUMMARY INJURY DATA")
                or upper.startswith("ESPN RECENT TEAM NEWS")
                or upper.startswith("ADAM SCHEFTER PRIORITY NEWS")
                or upper.startswith("SELECTED PLAYER PER-GAME AVERAGES")):
            priority = 0
        elif (upper.startswith("PLAYER LOGS")
                or upper.startswith("ESPN CURRENT-SEASON PLAYER GAME LOGS")):
            priority = 1
        elif (upper.startswith("ESPN CURRENT-SEASON TEAM STATISTICS")
                or upper.startswith("TEAM STATS:")):
            priority = 2
        elif upper.startswith("ESPN MATCHUP, PLAYER, INJURY, MARKET"):
            priority = 3
        else:
            priority = 4
        prioritized.append((priority, index, section))

    prioritized.sort(key=lambda item: (item[0], item[1]))

    # Player logs are emitted one player per section. A simple stable sort lets
    # the first team's logs consume the entire provider budget before the second
    # team's candidates are considered. Interleave log sections by team so both
    # sides retain comparable evidence under tight free-tier TPM limits.
    priority_zero = [item for item in prioritized if item[0] == 0]
    player_logs = [item for item in prioritized if item[0] == 1]
    remaining = [item for item in prioritized if item[0] > 1]
    logs_by_team: dict[str, list[tuple[int, int, str]]] = {}
    for item in player_logs:
        match = re.match(
            r"ESPN CURRENT-SEASON PLAYER GAME LOGS — (.+?) \(",
            item[2],
        )
        team_key = _normalize_name(match.group(1)) if match else "__unassigned__"
        logs_by_team.setdefault(team_key, []).append(item)
    balanced_player_logs = []
    while any(logs_by_team.values()):
        for team_key in list(logs_by_team):
            if logs_by_team[team_key]:
                balanced_player_logs.append(logs_by_team[team_key].pop(0))
    prioritized = priority_zero + balanced_player_logs + remaining

    kept = []
    used = 0
    essential_omitted = False
    optional_omitted = False
    marker = "[Research truncated.]"
    budget = max(0, max_chars - len(marker) - 2)
    for priority, _, section in prioritized:
        separator = 2 if kept else 0
        if len(section) + separator > budget - used:
            if priority <= 2:
                essential_omitted = True
            else:
                optional_omitted = True
            continue
        kept.append(section)
        used += separator + len(section)

    result = "\n\n".join(kept)
    if essential_omitted:
        result += ("\n\n" if result else "") + marker
    elif optional_omitted:
        result += (
            "\n\n[Optional historical matchup recap compacted; player logs, injuries, "
            "news and current team statistics were retained.]"
        )
    return result[:max_chars], essential_omitted


ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "game_script": {"type": "string"},
        "underdog_analysis": {
            "type": "array", "minItems": 0, "maxItems": 1,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "underdog": {"type": "string"},
                    "spread": {"type": "string"},
                    "cover_probability": {"type": "number"},
                    "upset_probability": {"type": "number"},
                    "recommendation": {"type": "string", "enum": ["Bet underdog spread", "Upset moneyline", "Monitor", "Pass"]},
                    "rationale": {"type": "string"},
                    "risk_factors": {"type": "string"},
                    "evidence_quality": {"type": "string", "enum": ["Strong", "Moderate", "Limited"]},
                },
                "required": ["underdog", "spread", "cover_probability", "upset_probability",
                    "recommendation", "rationale", "risk_factors", "evidence_quality"],
            },
        },
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


# Canonical NFL team identity lets provider output use ESPN abbreviations.
_NFL_TEAM_ABBREVIATIONS = {
    "ari": "arizonacardinals", "atl": "atlantafalcons",
    "bal": "baltimoreravens", "buf": "buffalobills",
    "car": "carolinapanthers", "chi": "chicagobears",
    "cin": "cincinnatibengals", "cle": "clevelandbrowns",
    "dal": "dallascowboys", "den": "denverbroncos",
    "det": "detroitlions", "gb": "greenbaypackers",
    "hou": "houstontexans", "ind": "indianapoliscolts",
    "jax": "jacksonvillejaguars", "kc": "kansascitychiefs",
    "lv": "lasvegasraiders", "lac": "losangeleschargers",
    "lar": "losangelesrams", "mia": "miamidolphins",
    "min": "minnesotavikings", "ne": "newenglandpatriots",
    "no": "neworleanssaints", "nyg": "newyorkgiants",
    "nyj": "newyorkjets", "phi": "philadelphiaeagles",
    "pit": "pittsburghsteelers", "sf": "sanfrancisco49ers",
    "sea": "seattleseahawks", "tb": "tampabaybuccaneers",
    "ten": "tennesseetitans", "was": "washingtoncommanders",
}


def _team_key(value: str) -> str:
    normalized = _normalize_name(value)
    return _NFL_TEAM_ABBREVIATIONS.get(normalized, normalized)


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


def _build_player_research_summary(
    player_usage: dict[str, dict[str, Any]],
    selected_candidates: dict[str, list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Build season and recent per-game averages for every selected offensive player."""
    stat_fields = (
        ("passingYards", "passing_yards_per_game"),
        ("passingAttempts", "passing_attempts_per_game"),
        ("passingTouchdowns", "passing_tds_per_game"),
        ("rushingYards", "rushing_yards_per_game"),
        ("rushingAttempts", "rushing_attempts_per_game"),
        ("rushingTouchdowns", "rushing_tds_per_game"),
        ("receivingYards", "receiving_yards_per_game"),
        ("receptions", "receptions_per_game"),
        ("receivingTargets", "targets_per_game"),
        ("receivingTouchdowns", "receiving_tds_per_game"),
    )
    rows = []
    for team, candidates in (selected_candidates or {}).items():
        for candidate in candidates if isinstance(candidates, list) else []:
            if not isinstance(candidate, dict):
                continue
            position = str(candidate.get("position") or "").upper()
            if position not in {"QB", "RB", "WR", "TE"}:
                continue
            name = str(candidate.get("name") or "").strip()
            usage = player_usage.get(_normalize_name(name), {})
            season = usage.get("season") if isinstance(usage.get("season"), dict) else {}
            recent = usage.get("recent") if isinstance(usage.get("recent"), list) else []
            games = int(usage.get("games_available") or 0)
            row = {
                "team": team, "player": name, "position": position,
                "games": games, "research_reason": str(candidate.get("research_reason") or ""),
            }
            for stat_key, output_key in stat_fields:
                total = season.get(stat_key)
                recent_key = output_key.replace("_per_game", "_recent")
                if total in (None, "") or games <= 0:
                    row[output_key] = None
                    row[recent_key] = None
                    continue
                row[output_key] = round(_stat_number(season, stat_key) / games, 2)
                recent_values = [
                    _stat_number(item.get("stats", {}), stat_key)
                    for item in recent[:3]
                    if isinstance(item, dict) and isinstance(item.get("stats"), dict)
                    and stat_key in item["stats"]
                ]
                row[recent_key] = (
                    round(sum(recent_values) / len(recent_values), 2)
                    if recent_values else None
                )
            rush_td = row.get("rushing_tds_per_game")
            rec_td = row.get("receiving_tds_per_game")
            row["tds_scored_per_game"] = (
                round((rush_td or 0) + (rec_td or 0), 2)
                if rush_td is not None or rec_td is not None else None
            )
            recent_td_values = []
            for item in recent[:3]:
                stats = item.get("stats", {}) if isinstance(item, dict) else {}
                if isinstance(stats, dict) and (
                    "rushingTouchdowns" in stats or "receivingTouchdowns" in stats
                ):
                    recent_td_values.append(
                        _stat_number(stats, "rushingTouchdowns")
                        + _stat_number(stats, "receivingTouchdowns")
                    )
            row["tds_scored_recent"] = (
                round(sum(recent_td_values) / len(recent_td_values), 2)
                if recent_td_values else None
            )
            rows.append(row)
    return rows


def _apply_independent_injury_statuses(
    data: dict[str, Any],
    injury_statuses: list[dict[str, Any]] | None,
) -> list[str]:
    """Remove unavailable player picks and TD candidates using independent ESPN reports."""
    unavailable: dict[str, str] = {}
    questionable: set[str] = set()
    explicitly_available: dict[str, str] = {}
    other_statuses: dict[str, str] = {}
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
        elif status.casefold() in {"active", "available"}:
            explicitly_available[player_key] = status
        elif status:
            other_statuses[player_key] = status

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
                item["availability"] = "Questionable"
                item["availability_evidence"] = (
                    "ESPN's injury feed lists Questionable; final game-day active status is pending."
                )
                item["confidence"] = min(int(item.get("confidence", 6)), 6)
                risk = str(item.get("risk_factors", "")).strip()
                if "question" not in risk.casefold():
                    item["risk_factors"] = (
                        "Questionable: availability unconfirmed"
                        if not risk else "Questionable: availability unconfirmed; " + risk
                    )
            elif player_key in explicitly_available:
                item["availability"] = "Available (ESPN injury feed)"
                item["availability_evidence"] = (
                    f"ESPN's injury feed explicitly lists {explicitly_available[player_key]}."
                )
            elif player_key in other_statuses:
                item["availability"] = f"ESPN status: {other_statuses[player_key]}"
                item["availability_evidence"] = (
                    f"ESPN's injury feed lists {other_statuses[player_key]}; "
                    "final game-day active status may still be pending."
                )
            else:
                # ESPN's Core injury endpoint contains non-active designations, not
                # a complete game-day inactive list. No matching record means
                # "no designation found", never a claim that the player is active.
                item["availability"] = "No injury designation found"
                item["availability_evidence"] = (
                    "No non-active designation appeared in ESPN's injury feed; "
                    "final game-day active status is not confirmed."
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
        (r"field goals? attempted", "fieldGoalsAttempted"),
        (r"field goals? made", "fieldGoalsMade"),
        (r"extra points? attempted", "extraPointsAttempted"),
        (r"extra points? made", "extraPointsMade"),
        (r"punting yards?", "puntingYards"),
        (r"longest punt", "longPunt"),
        (r"punts?", "punts"),
        (r"longest pass", "longestPass"),
        (r"longest rush", "longRushing"),
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
        if any(term in description for term in (
            "anytime touchdown", "anytime td", "touchdown scorer",
            "first touchdown", "first td", "first-half touchdown",
            "first half touchdown", "1st half touchdown", "first half td",
            "1st half td", "1h td",
        )):
            season = usage.get("season") if isinstance(usage.get("season"), dict) else {}
            games = int(usage.get("games_available") or 0)
            position = str(usage.get("position") or "").upper()
            carries = _stat_number(season, "rushingAttempts")
            targets = _stat_number(season, "receivingTargets")
            rush_tds = _stat_number(season, "rushingTouchdowns")
            receiving_tds = _stat_number(season, "receivingTouchdowns")
            eligible = games > 0 and (
                (position == "QB" and (rush_tds >= 1 or carries / max(games, 1) >= 3))
                or (position == "RB" and (carries + targets) / max(games, 1) >= 4)
                or (position in {"WR", "TE"} and targets / max(games, 1) >= 2.5)
            )
            if eligible:
                retained.append(pick)
            else:
                gaps.append(
                    f"Removed {player} anytime-TD prop: current-season role evidence was insufficient."
                )
            continue
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
            pick["confidence"] = min(int(pick.get("confidence", 5)), 5)
            pick["data_quality"] = "Limited"
            risk = str(pick.get("risk_factors") or "").strip()
            if "period split" not in risk.casefold():
                pick["risk_factors"] = (
                    "No ESPN period splits; full-game stats are context only"
                    if not risk else risk + "; no ESPN period splits"
                )
            gaps.append(
                f"{player} period-specific prop: ESPN quarter/half splits were unavailable; confidence capped at 5/10."
            )
            retained.append(pick)
            continue
        is_over = bool(re.search(r"\bover\b", description))
        is_under = bool(re.search(r"\bunder\b", description))
        # Model thresholds must stay close to demonstrated production. The
        # previous 1.75x/0.50x guard let inflated yardage targets through.
        unsupported = (
            is_over and threshold > max(season_average, recent_average, 0.1) * 1.10
        ) or (
            is_under and threshold < min(season_average, recent_average) * 0.70
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


def _sanitize_underdog_analysis(
    data: dict[str, Any],
    team_names: tuple[str, str] | None,
    expected_underdog: str | None = None,
    expected_spread: float | None = None,
) -> list[str]:
    """Normalize or discard malformed underdog output without losing matchup analysis."""
    rows = data.get("underdog_analysis", [])
    gaps = data.get("data_gaps")
    if not isinstance(gaps, list):
        gaps = []
        data["data_gaps"] = gaps
    if not isinstance(rows, list):
        rows = []
    accepted = []
    rejection_reasons: list[str] = []
    valid_teams = {_team_key(team) for team in team_names} if team_names else None
    recommendations = {
        "bet underdog spread": "Bet underdog spread", "underdog spread": "Bet underdog spread",
        "bet the spread": "Bet underdog spread", "upset moneyline": "Upset moneyline",
        "underdog moneyline": "Upset moneyline", "moneyline upset": "Upset moneyline",
        "monitor": "Monitor", "watch": "Monitor", "pass": "Pass", "no bet": "Pass",
    }
    qualities = {"strong": "Strong", "high": "Strong", "moderate": "Moderate",
                 "medium": "Moderate", "limited": "Limited", "low": "Limited"}
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate = dict(row)
        candidate["underdog"] = str(candidate.get("underdog") or "").strip()
        candidate["spread"] = str(candidate.get("spread") or "").strip()
        if not candidate["underdog"] or not candidate["spread"]:
            rejection_reasons.append("missing underdog team or spread")
            continue
        spread_match = re.search(r"[+-]?\d+(?:\.\d+)?", candidate["spread"])
        if not spread_match or float(spread_match.group(0)) <= 0:
            rejection_reasons.append(f"invalid underdog spread value: {candidate['spread']!r}")
            continue
        if expected_spread is not None and abs(float(spread_match.group(0))) != expected_spread:
            rejection_reasons.append(
                f"spread mismatch: model supplied {candidate['spread']!r}; expected +{expected_spread:g}"
            )
            continue
        if valid_teams is not None and _team_key(candidate["underdog"]) not in valid_teams:
            rejection_reasons.append(f"team is not in matchup: {candidate['underdog']!r}")
            continue
        if expected_underdog and _team_key(candidate["underdog"]) != _team_key(expected_underdog):
            rejection_reasons.append(
                f"wrong underdog: model supplied {candidate['underdog']!r}; expected {expected_underdog!r}"
            )
            continue
        try:
            cover = float(candidate.get("cover_probability"))
            upset = float(candidate.get("upset_probability"))
        except (TypeError, ValueError):
            rejection_reasons.append("cover_probability or upset_probability is missing/non-numeric")
            continue
        if not (0 <= cover <= 100 and 0 <= upset <= cover):
            rejection_reasons.append(
                f"invalid probabilities: cover={candidate.get('cover_probability')!r}, "
                f"upset={candidate.get('upset_probability')!r}"
            )
            continue
        recommendation = str(candidate.get("recommendation") or "").strip().casefold()
        quality = str(candidate.get("evidence_quality") or "").strip().casefold()
        if recommendation not in recommendations or quality not in qualities:
            rejection_reasons.append(
                f"invalid recommendation/evidence_quality: {candidate.get('recommendation')!r}/"
                f"{candidate.get('evidence_quality')!r}"
            )
            continue
        if not str(candidate.get("rationale") or "").strip() or not str(candidate.get("risk_factors") or "").strip():
            rejection_reasons.append("rationale or risk_factors is missing")
            continue
        candidate.update({
            "cover_probability": cover, "upset_probability": upset,
            "recommendation": recommendations[recommendation],
            "evidence_quality": qualities[quality],
        })
        accepted.append(candidate)
        break
    data["underdog_analysis"] = accepted
    if not accepted:
        warning = "Underdog assessment unavailable: model output was missing, incomplete, or invalid."
        if rejection_reasons:
            warning += " Rejection detail: " + "; ".join(rejection_reasons[:3]) + "."
        if warning not in gaps:
            gaps.append(warning)
        return rejection_reasons
    return []


def _validate_analysis(
    data: dict[str, Any],
    rosters: dict[str, list[str]] | None = None,
    team_names: tuple[str, str] | None = None,
    require_underdog: bool = False,
    expected_underdog: str | None = None,
    expected_spread: float | None = None,
) -> None:
    # Keep require_underdog accepted for existing callers, but treat this
    # supplemental section as optional so its omission cannot erase valid picks.
    data.setdefault("underdog_analysis", [])
    if not isinstance(data.get("summary"), str) or not isinstance(data.get("game_script"), str):
        raise RuntimeError("AI response is missing the matchup summary or game script.")
    # Underdog analysis is valuable, but it is a supplemental section. A
    # malformed or omitted underdog row must never discard otherwise usable
    # matchup analysis; the sanitizer records a visible data-gap warning.
    _sanitize_underdog_analysis(
        data, team_names, expected_underdog=expected_underdog,
        expected_spread=expected_spread,
    )
    picks = data.get("picks")
    scorers = data.get("touchdown_scorers")
    if not isinstance(picks, list):
        raise RuntimeError("AI response betting opportunities must be a list.")
    if not isinstance(scorers, list) or len(scorers) > 5:
        raise RuntimeError("AI response may contain at most five preliminary touchdown candidates.")
    # These are optional output sections. Treat omitted sections as empty rather
    # than rejecting otherwise valid analyses that contain picks or TD candidates.
    sneaky_response = data.get("sneaky_props", [])
    injury_response = data.get("injury_impact", [])
    underdog_response = data.get("underdog_analysis", [])
    # An invalid raw underdog row is not usable analysis: only count it
    # after _sanitize_underdog_analysis has retained a valid assessment.
    # Likewise, count injury impacts only when both teams belong to this
    # matchup and the proposed beneficiary is on the stated roster (or is
    # an explicitly permitted unit-level impact).
    valid_injury_response = False
    if isinstance(injury_response, list):
        valid_team_keys = {_team_key(team) for team in team_names} if team_names else None
        roster_lookup = {
            _team_key(team): {_normalize_name(player) for player in players}
            for team, players in (rosters or {}).items()
        }
        for impact in injury_response:
            if not isinstance(impact, dict):
                continue
            subject_team = _team_key(impact.get("team", ""))
            beneficiary_team = _team_key(impact.get("affected_team", ""))
            beneficiary = _normalize_name(impact.get("affected_player", ""))
            unit_impact = beneficiary in {"teamunit", "offensiveunit", "defensiveunit", "specialteamsunit"}
            if not subject_team or not beneficiary_team or not beneficiary:
                continue
            if valid_team_keys is not None and (
                subject_team not in valid_team_keys or beneficiary_team not in valid_team_keys
            ):
                continue
            if rosters is not None and not unit_impact and beneficiary not in roster_lookup.get(beneficiary_team, set()):
                continue
            valid_injury_response = True
            break
    if not any((
        picks, scorers, sneaky_response if isinstance(sneaky_response, list) else [],
        valid_injury_response, underdog_response,
    )):
        raise RuntimeError(
            "AI response contains no usable matchup analysis: all picks, touchdown candidates, "
            "injury impacts, sneaky props, and underdog assessment are empty."
        )
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
    sneaky = data.setdefault("sneaky_props", [])
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
            normalized_team = _team_key(item.get("team", ""))
            matching_team = next(
                (name for name in team_names if _team_key(name) == normalized_team),
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
                     if _team_key(name) == _team_key(matching_team)),
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





def _is_key_defensive_absence(
    impact: dict[str, Any],
    player_usage: dict[str, dict[str, Any]],
) -> bool:
    """Return True only for a clearly high-impact defensive player, not any starter."""
    player = str(impact.get("injured_player") or "")
    usage = player_usage.get(_normalize_name(player), {})
    position = str(usage.get("position") or "").upper()
    if position not in {"LB", "ILB", "OLB", "DE", "EDGE", "DT", "NT", "CB", "S", "FS", "SS"}:
        return False
    narrative = " ".join(
        str(impact.get(field) or "")
        for field in ("statistical_effect", "evidence", "injured_player")
    ).casefold()
    if re.search(r"\b(?:all[- ]pro|pro bowl|team captain|defensive player of the year)\b", narrative):
        return True
    season = usage.get("season") if isinstance(usage.get("season"), dict) else {}
    return any((
        _stat_number(season, "sacks") >= 5.0,
        _stat_number(season, "totalTackles") >= 65,
        _stat_number(season, "interceptions") >= 3,
        _stat_number(season, "passesDefended") >= 8,
        _stat_number(season, "tacklesForLoss") >= 8,
        _stat_number(season, "quarterbackHits") >= 10,
    ))


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
        (_team_key(str(item.get("team") or "")),
         _normalize_name(str(item.get("player") or ""))): item
        for item in injury_statuses
        if isinstance(item, dict) and item.get("team") and item.get("player")
    }
    roster_sets = {
        _team_key(team): {_normalize_name(player) for player in players}
        for team, players in (rosters or {}).items()
    }
    team_lookup = {_team_key(team): team for team in team_names}
    retained = []
    gaps = []
    for impact in data.get("injury_impact", []):
        injured_player = str(impact.get("injured_player") or "")
        injured_team = str(impact.get("team") or "")
        affected_player = str(impact.get("affected_player") or "")
        affected_team = str(impact.get("affected_team") or "")
        injured_player_key = _normalize_name(injured_player)
        affected_player_key = _normalize_name(affected_player)
        injured_team_key = _team_key(injured_team)
        affected_team_key = _team_key(affected_team)
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
        if unit_impact and not _is_key_defensive_absence(impact, player_usage or {}):
            gaps.append(
                f"Removed team-unit impact for {injured_player}: only key defensive stars "
                "may remain as unit-level injury entries."
            )
            continue
        retained.append(impact)

    retained.sort(
        key=lambda item: (
            _is_key_defensive_absence(item, player_usage or {}),
            int(item.get("confidence", 0) or 0),
            _normalize_name(str(item.get("affected_player") or "")) not in {
                "teamunit", "offensiveunit", "defensiveunit", "specialteamsunit"
            },
        ),
        reverse=True,
    )
    if len(retained) > 5:
        gaps.append(
            f"Condensed injury/news impacts from {len(retained)} to the five most material entries."
        )
        retained = retained[:5]
    data["injury_impact"] = retained
    return gaps


def _expected_market_key(description: str) -> str | None:
    text = description.casefold()
    if any(term in text for term in ("first touchdown", "first td")):
        return "player_1st_td"
    if any(term in text for term in ("anytime touchdown", "anytime td", "touchdown scorer")):
        return "player_anytime_td"
    patterns = (
        (r"passing touchdowns?", "player_pass_tds"),
        (r"passing yards?", "player_pass_yds"),
        (r"passing attempts?", "player_pass_attempts"),
        (r"completions?", "player_pass_completions"),
        (r"interceptions?", "player_pass_interceptions"),
        (r"rushing touchdowns?", "player_rush_tds"),
        (r"rushing yards?", "player_rush_yds"),
        (r"rushing attempts?|carries", "player_rush_attempts"),
        (r"receiving touchdowns?", "player_reception_tds"),
        (r"receiving yards?", "player_reception_yds"),
        (r"receptions?", "player_receptions"),
        (r"longest reception", "player_reception_longest"),
        (r"longest rush", "player_rush_longest"),
        (r"longest pass|longest completion", "player_pass_longest_completion"),
    )
    return next((key for pattern, key in patterns if re.search(pattern, text)), None)


def _quote_matches_pick(pick: dict[str, Any], quote: dict[str, Any]) -> bool:
    """Require the same player, exact market key, direction and numeric threshold."""
    if _normalize_name(str(pick.get("player_or_team") or "")) != _normalize_name(str(quote.get("player") or "")):
        return False
    expected_key = _expected_market_key(str(pick.get("bet_description") or ""))
    if not expected_key or str(quote.get("market_key") or "") != expected_key:
        return False

    description = " ".join((
        str(pick.get("bet_description") or ""),
        str(pick.get("play_threshold") or ""),
    ))
    quote_side = str(quote.get("side") or "").strip().casefold()
    if expected_key in {"player_anytime_td", "player_1st_td"}:
        return quote_side in {"yes", "over"} or str(quote.get("line") or "").strip().casefold() == "yes"

    pick_match = re.search(r"\b(over|under)\s*(-?\d+(?:\.\d+)?)", description, re.I)
    quote_point = quote.get("point")
    if quote_point in (None, ""):
        quote_match = re.search(r"\b(over|under)\s*(-?\d+(?:\.\d+)?)", str(quote.get("line") or ""), re.I)
        if quote_match:
            quote_side = quote_match.group(1).casefold()
            quote_point = quote_match.group(2)
    if not pick_match or quote_point in (None, "") or quote_side not in {"over", "under"}:
        return False
    try:
        same_point = abs(float(pick_match.group(2)) - float(quote_point)) < 0.0001
    except (TypeError, ValueError):
        return False
    return pick_match.group(1).casefold() == quote_side and same_point


def _separate_unverified_player_props(
    data: dict[str, Any],
    research: dict[str, Any],
) -> list[str]:
    """Keep only exact sportsbook-matched player props in actionable/sneaky sections."""
    verified_quotes = research.get("verified_player_prop_markets", [])
    verified_quotes = verified_quotes if isinstance(verified_quotes, list) else []
    gaps = []
    model_only = []

    def quote_has_same_market_different_point(pick: dict[str, Any], quote: dict[str, Any]) -> bool:
        if _normalize_name(str(pick.get("player_or_team") or "")) != _normalize_name(str(quote.get("player") or "")):
            return False
        expected_key = _expected_market_key(str(pick.get("bet_description") or ""))
        if not expected_key or str(quote.get("market_key") or "") != expected_key:
            return False
        if expected_key in {"player_anytime_td", "player_1st_td"}:
            return False
        description = " ".join((
            str(pick.get("bet_description") or ""),
            str(pick.get("play_threshold") or ""),
        ))
        pick_match = re.search(r"\b(over|under)\s*(-?\d+(?:\.\d+)?)", description, re.I)
        quote_point = quote.get("point")
        if quote_point in (None, ""):
            quote_match = re.search(r"\b(over|under)\s*(-?\d+(?:\.\d+)?)", str(quote.get("line") or ""), re.I)
            if quote_match:
                quote_point = quote_match.group(2)
        if not pick_match or quote_point in (None, ""):
            return False
        try:
            return abs(float(pick_match.group(2)) - float(quote_point)) >= 0.0001
        except (TypeError, ValueError):
            return False


    def match_quote(pick: dict[str, Any]) -> dict[str, Any] | None:
        return next((
            quote for quote in verified_quotes
            if isinstance(quote, dict)
            and _quote_matches_pick(pick, quote)
            and quote.get("line") not in (None, "")
            and quote.get("price") not in (None, "")
            and quote.get("sportsbook") and quote.get("source_url")
            and quote.get("retrieved_at")
        ), None)

    def mismatched_line_quote(pick: dict[str, Any]) -> dict[str, Any] | None:
        return next((
            quote for quote in verified_quotes
            if isinstance(quote, dict)
            and quote_has_same_market_different_point(pick, quote)
            and quote.get("line") not in (None, "")
            and quote.get("price") not in (None, "")
            and quote.get("sportsbook") and quote.get("source_url")
            and quote.get("retrieved_at")
            and not _quote_matches_pick(pick, quote)
        ), None)

    actionable = []
    for pick in data.get("picks", []):
        if pick.get("market_type") != "Player Prop":
            actionable.append(pick)
            continue
        matched = match_quote(pick)
        if matched:
            pick["market_line"] = (
                f"{matched['line']} ({matched['price']}) — {matched['sportsbook']}"
            )
            pick["market_source"] = matched["source_url"]
            pick["market_retrieved_at"] = matched["retrieved_at"]
            actionable.append(pick)
        else:
            mismatched = mismatched_line_quote(pick)
            if mismatched:
                quoted_point = mismatched.get("point")
                quoted_side = str(mismatched.get("side") or "").strip()
                quoted_line = (
                    f"{quoted_point} ({quoted_side})"
                    if quoted_point not in (None, "") and quoted_side
                    else mismatched.get("line")
                )
                gaps.append(
                    f"Removed {pick.get('player_or_team')} {pick.get('bet_description')}: the sportsbook offers {quoted_line} at {mismatched.get('sportsbook')}, not the model's threshold; the line was not silently changed."
                )
                continue
            pick["market_line"] = "NOT VERIFIED — MODEL THRESHOLD ONLY"
            model_only.append(pick)
            gaps.append(
                f"{pick.get('player_or_team')} {pick.get('bet_description')}: exact player market, side and line were not verified; moved to model-only leans."
            )
    data["picks"] = actionable
    data["model_only_picks"] = model_only

    retained_sneaky = []
    for pick in data.get("sneaky_props", []):
        if pick.get("market_type") != "Player Prop":
            retained_sneaky.append(pick)
            continue
        matched = match_quote(pick)
        if not matched:
            gaps.append(
                f"Removed unverified sneaky player prop for {pick.get('player_or_team')}: "
                "no exact sportsbook market, side, line and price were found."
            )
            continue
        pick["market_line"] = (
            f"{matched['line']} ({matched['price']}) — {matched['sportsbook']}"
        )
        pick["market_source"] = matched["source_url"]
        pick["market_retrieved_at"] = matched["retrieved_at"]
        retained_sneaky.append(pick)
    data["sneaky_props"] = retained_sneaky
    return gaps


def _move_sneaky_game_picks(data: dict[str, Any]) -> bool:
    """Move 1Q/1H and other game-line ideas out of Sneaky Props into game-level bets."""
    sneaky = data.get("sneaky_props", [])
    if not isinstance(sneaky, list):
        return False
    moved = [item for item in sneaky if isinstance(item, dict) and item.get("market_type") == "Game Prop"]
    if not moved:
        return False
    data.setdefault("picks", []).extend(moved)
    data["sneaky_props"] = [
        item for item in sneaky
        if not isinstance(item, dict) or item.get("market_type") != "Game Prop"
    ]
    return True


def _game_market_total_points(research: dict[str, Any]) -> set[float]:
    """Read full-game total points only from timestamped ESPN pickcenter quotes."""
    totals: set[float] = set()
    for market in research.get("verified_game_markets", []) or []:
        if not isinstance(market, dict):
            continue
        total = market.get("total")
        if isinstance(total, dict):
            for side_data in total.values():
                if not isinstance(side_data, dict):
                    continue
                for phase in ("open", "close"):
                    quote = side_data.get(phase)
                    if isinstance(quote, dict) and quote.get("line") not in (None, ""):
                        try:
                            totals.add(float(quote["line"]))
                        except (TypeError, ValueError):
                            pass
        displayed = market.get("displayed_total")
        if displayed not in (None, ""):
            try:
                totals.add(float(displayed))
            except (TypeError, ValueError):
                pass
    return totals


def _filter_game_level_picks(
    data: dict[str, Any],
    game: dict[str, Any],
    research: dict[str, Any],
    team_names: tuple[str, str],
) -> list[str]:
    """Reject game bets that do not match a supplied, independently sourced market."""
    gaps = []
    totals = _game_market_total_points(research)
    scoreboard_line = str(game.get("Betting Line") or "")
    spread_match = re.search(r"([A-Za-z]{2,3})\s*([+-]\d+(?:\.\d+)?)", scoreboard_line)
    scoreboard_team = _team_key(spread_match.group(1)) if spread_match else ""
    scoreboard_spread = float(spread_match.group(2)) if spread_match else None
    retained = []
    for pick in data.get("picks", []):
        if pick.get("market_type") != "Game Prop":
            retained.append(pick)
            continue
        description = " ".join(str(pick.get(field) or "") for field in (
            "bet_description", "play_threshold", "market_line"
        ))
        normalized = description.casefold()
        if re.search(r"\b(?:over|under)\b.*(?:total|points)", normalized):
            pick_team = _team_key(str(pick.get("player_or_team") or ""))
            identity_text = " ".join(str(pick.get(field) or "") for field in (
                "bet_description", "play_threshold"
            ))
            normalized_identity = _normalize_name(identity_text)
            def mentions_team(team: str) -> bool:
                if any(
                    _normalize_name(term) in normalized_identity
                    for term in (team, str(team).split()[-1])
                ):
                    return True
                return any(
                    re.search(r"\b" + abbreviation + r"\b", identity_text, re.IGNORECASE)
                    for abbreviation, canonical in _NFL_TEAM_ABBREVIATIONS.items()
                    if canonical == _team_key(team)
                )
            # A single team reference denotes a team total; both teams named
            # together can still describe a combined game total.
            team_mentioned = sum(1 for team in team_names if mentions_team(team)) == 1
            if "team total" in normalized or team_mentioned or pick_team in {
                _team_key(team_names[0]), _team_key(team_names[1])
            }:

                gaps.append(
                    f"Removed ambiguous team total {pick.get('bet_description')}: the selected team requires an exact team-total quote, not a full-game total."
                )
                continue
            point_match = re.search(r"\b(?:over|under)\s*(-?\d+(?:\.\d+)?)", normalized)
            point = float(point_match.group(1)) if point_match else None
            if point is None or not any(abs(point - quote) < 0.001 for quote in totals):
                gaps.append(
                    f"Removed game total {pick.get('bet_description')}: no matching ESPN pickcenter total quote was supplied."
                )
                continue
            pick["market_line"] = f"ESPN pickcenter total {point:g} (timestamped quote)"
            retained.append(pick)
            continue
        if re.search(r"\b(?:1st|first)[- ]?(?:half|quarter|qtr)|\b(?:1h|1q)\b", normalized):
            pick["market_line"] = "NOT VERIFIED — PERIOD MARKET QUOTE UNAVAILABLE"
            pick["data_quality"] = "Limited"
            try:
                pick["confidence"] = min(int(pick.get("confidence", 5)), 5)
            except (TypeError, ValueError):
                pick["confidence"] = 5
            risk = str(pick.get("risk_factors") or "").strip()
            if "period market" not in risk.casefold():
                pick["risk_factors"] = (
                    "No verified 1Q/1H sportsbook quote"
                    if not risk else risk + "; no verified 1Q/1H sportsbook quote"
                )
            retained.append(pick)
            gaps.append(
                f"Retained {pick.get('bet_description')} in game-level bets only as a Limited model idea; no 1Q/1H sportsbook quote was supplied."
            )
            continue
        if "spread" in normalized or re.search(r"[+-]\s*\d+(?:\.\d+)?", normalized):
            team_key = _team_key(str(pick.get("player_or_team") or ""))
            pick_line_text = " ".join(str(pick.get(field) or "") for field in (
                "bet_description", "play_threshold"
            ))
            line_value_match = re.search(r"([+-]\d+(?:\.\d+)?)", pick_line_text)
            if scoreboard_spread is None or not scoreboard_team or team_key not in {
                _team_key(team_names[0]), _team_key(team_names[1])
            }:
                gaps.append(f"Removed unverified game spread {pick.get('bet_description')}: scoreboard spread could not be matched.")
                continue
            expected = scoreboard_spread if team_key == scoreboard_team else -scoreboard_spread
            if not line_value_match or (
                abs(abs(float(line_value_match.group(1))) - abs(expected)) > 0.001
                or float(line_value_match.group(1)) * expected < 0
            ):
                gaps.append(f"Removed game spread {pick.get('bet_description')}: threshold conflicts with ESPN scoreboard spread.")
                continue
            pick["market_line"] = scoreboard_line + " (ESPN scoreboard)"
            retained.append(pick)
            continue
        gaps.append(
            f"Removed game-level pick {pick.get('bet_description')}: no independently verified matching market was supplied."
        )
    data["picks"] = retained
    return gaps


def _remove_contradictory_period_game_picks(
    data: dict[str, Any],
    team_names: tuple[str, str],
) -> list[str]:
    """Keep one consistent spread side across full-game and period game picks."""
    picks = data.get("picks", [])

    def spread_side(item: dict[str, Any]) -> bool:
        text = " ".join(str(item.get(field) or "") for field in (
            "bet_description", "play_threshold"
        )).casefold()
        if "moneyline" in text or re.search(r"\bml\b", text):
            return False
        return "spread" in text or bool(re.search(r"[+-]\s*\d+(?:\.\d+)?", text))

    def is_period(item: dict[str, Any]) -> bool:
        text = " ".join(str(item.get(field) or "") for field in (
            "bet_description", "play_threshold"
        )).casefold()
        return bool(re.search(r"\b(?:1st|first)[- ]?(?:half|quarter|qtr)|\b(?:1h|1q)\b", text))

    spread_picks = [
        item for item in picks
        if item.get("market_type") == "Game Prop" and spread_side(item)
        and _team_key(str(item.get("player_or_team") or "")) in {
            _team_key(team_names[0]), _team_key(team_names[1])
        }
    ]
    if not spread_picks:
        return []
    full_game_picks = [item for item in spread_picks if not is_period(item)]
    anchors = full_game_picks or spread_picks
    anchor = max(
        anchors,
        key=lambda item: (
            int(item.get("confidence", 0) or 0),
            float(item.get("estimated_hit_probability", 0) or 0),
        ),
    )
    anchor_team = _team_key(str(anchor.get("player_or_team") or ""))
    gaps = []
    retained = []
    for item in picks:
        is_spread = item.get("market_type") == "Game Prop" and spread_side(item)
        team_key = _team_key(str(item.get("player_or_team") or ""))
        if is_spread and team_key in {
            _team_key(team_names[0]), _team_key(team_names[1])
        } and team_key != anchor_team:
            gaps.append(
                f"Removed contradictory spread {item.get('bet_description')}: it backs the opposite team from the highest-confidence retained spread side."
            )
            continue
        retained.append(item)
    data["picks"] = retained
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


def _underdog_format_instruction(
    expected_underdog: str | None,
    expected_spread: float | None,
) -> str:
    """Give the model a concrete, matchup-specific underdog JSON shape."""
    if not expected_underdog or expected_spread is None:
        return (
            "\nUNDERDOG FORMAT: If a numeric ESPN spread is available in the supplied "
            "line, return one underdog assessment; otherwise use an empty array.\n"
        )
    spread = f"+{expected_spread:g}"
    template = (
        '[{"underdog":"' + expected_underdog + '","spread":"' + spread
        + '","cover_probability":NUMBER,"upset_probability":NUMBER,'
        + '"recommendation":"Monitor","rationale":"SHORT EVIDENCE-BASED REASON",'
        + '"risk_factors":"SHORT RISK","evidence_quality":"Moderate"}]'
    )
    return (
        "\nMATCHUP-SPECIFIC UNDERDOG FORMAT (replace placeholders; this is a structure example): "
        + template
        + " The probabilities must be numeric 0-100, with upset_probability no greater "
        "than cover_probability. Estimate them from supplied evidence; do not copy example "
        "values or force a wager.\n"
    )


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
    original_research = research.get("research_text", "No independent research was available.")
    # Compute and feed the same per-game averages that will be displayed in the
    # workbook, so they are part of prop selection rather than post-hoc decoration.
    player_usage = _research_player_usage(original_research)
    player_research_summary = _build_player_research_summary(
        player_usage, research.get("selected_player_candidates", {})
    )
    if player_research_summary:
        original_research += (
            "\n\nSELECTED PLAYER PER-GAME AVERAGES (season / last three games): "
            + json.dumps(player_research_summary, ensure_ascii=False, separators=(",", ":"))
        )

    user_prefix = (
        f"Analyze this upcoming NFL matchup: {matchup}.\\n"
        f"Kickoff: {game.get('Game Date (ET)', 'Not supplied')}. "
        f"ESPN line: {game.get('Betting Line', 'Unavailable')}. "
        f"Status: {game.get('Game Status', 'Not supplied')}.\\n\\n"
        "Use supplied research as the factual basis. Do not invent or claim unprovided "
        "stats, injuries, lines, prices, sources, dates, browsing or verification. "
        "Return JSON only: summary, game_script, underdog_analysis, picks, sneaky_props, "
        "touchdown_scorers (up to 5), injury_impact, data_gaps. "
        "Pick fields: rank, market_type (Player Prop/Game Prop), player_or_team, "
        "bet_description, market_line, play_threshold, estimated_hit_probability (0-100), "
        "confidence (1-10), statistical_rationale, risk_factors, data_quality "
        "(Strong/Moderate/Limited). Include every schema field; use empty arrays when appropriate. "
        "For every spread, return exactly one assessment for the team receiving points, "
        "with separate cover_probability and outright upset_probability, recommendation, "
        "rationale, risk_factors and evidence_quality. Monitor/Pass is valid; never force a bet. "
        "The underdog_analysis value must be a one-item array with all eight fields; "
        "use the exact underdog team and positive points-received spread shown below. "
        "Do not omit this section or return an empty array when a numeric spread is supplied. "
        f"Only use these matchup teams: {team_names[0]} and {team_names[1]}. "
        "Prioritize evidence-backed player props. Compare each threshold with season and "
        "last-three-game averages; reject unrealistic thresholds. Target five verified props "
        "only when exact player, market, side, line and price are supplied; otherwise label "
        "model-only and do not present as an actionable sportsbook pick. "
        "Assess core passing, rushing, receiving and anytime-TD markets, plus supported "
        "full-game/1Q/1H game lines and researched sneaky/first-TD opportunities. "
        "Never invent alternate lines or totals; game/team totals require an exact quote. "
        "Put 1Q/1H game lines in picks, not Sneaky Props; never oppose a full-game spread "
        "with a period spread. Do not pair a team's moneyline and spread. "
        "For player props, use researched season and recent production; omit unsupported "
        "period props. TDs require scoring/opportunity evidence; passing TDs do not qualify "
        "a QB for anytime TD (require a rushing TD profile). "
        "Injury/news impacts: only material offensive absences, key defensive stars and "
        "verified role/QB news. Research downstream replacements; never assume inherited "
        "production or make the injured player their own beneficiary. 'Team unit' is allowed "
        "only for an elite key defensive star, not ordinary starters or offensive-line absences. "
        "Questionable players are conditional only, confidence <=6, with explicit risk. "
        "For Questionable TD candidates, risk_factors must start 'Questionable:' "
        "(e.g. 'Questionable: active status unconfirmed'). Exclude doubtful, out and IR; "
        "absence of a report does not establish active status. "
        "Prioritize dated Adam Schefter ESPN reports for selected players and matchup teams. "
        "Keep output compact: summary <= 90 words; game_script <= 65 words; each pick "
        "statistical_rationale <= 24 words and risk_factors <= 14 words; each TD rationale "
        "<= 16 words and risk_factors <= 12 words; return no more than five material "
        "injury/news impacts, each text field <= 16 words; data_gaps <= 4 short items. "
        "Use short phrases, avoid repetition and add no commentary outside JSON.\\n"
    )

    prompt_metadata: dict[str, tuple[bool, bool, int]] = {}

    def prompts_for_provider(provider_name: str) -> tuple[str, str]:
        if provider_name == "groq":
            # Leave 500 TPM tokens as a safety margin for estimation variance.
            # Keep total prompt + completion demand below the 8k TPM ceiling
            # with a larger safety margin for other requests in the rolling window.
            output_tokens = max(
                256, min(int(os.environ.get("GROQ_MAX_COMPLETION_TOKENS", "1600")), 1600)
            )
            input_token_budget = max(1000, 6500 - output_tokens - 500)
            combined_budget = int(input_token_budget * 3.5)
        else:
            combined_budget = len(instructions) + len(user_prefix) + MAX_RESEARCH_PROMPT_CHARS
        research_budget = max(
            1000, combined_budget - len(instructions) - len(user_prefix) - 48
        )
        bounded, was_truncated = _bounded_research_text(original_research, research_budget)
        prompt_metadata[provider_name] = (
            was_truncated,
            len(bounded) < len(str(original_research or "")) and not was_truncated,
            len(bounded),
        )
        underdog_format = _underdog_format_instruction(
            expected_underdog, expected_spread
        )
        return (
            instructions,
            user_prefix + underdog_format
            + "\nINDEPENDENT RESEARCH AND SOURCE MATERIAL:\n" + bounded,
        )

    rosters = research.get("rosters")
    spread_match = re.search(
        r"([A-Za-z]{2,3})\s*([+-]\d+(?:\.\d+)?)",
        str(game.get("Betting Line") or ""),
    )
    expected_underdog = None
    expected_spread = None
    if spread_match:
        spread_team = _team_key(spread_match.group(1))
        spread_value = float(spread_match.group(2))
        expected_spread = abs(spread_value)
        favorite_team = spread_team if spread_value < 0 else (
            _team_key(team_names[0]) if spread_team == _team_key(team_names[1])
            else _team_key(team_names[1])
        )
        expected_underdog = next(
            (team for team in team_names if _team_key(team) != favorite_team),
            None,
        )
    data, provider_name = generate_json(
        instructions,
        user_prefix,
        validator=lambda result: _validate_analysis(
            result, rosters=rosters, team_names=team_names,
            require_underdog=bool(spread_match),
            expected_underdog=expected_underdog,
            expected_spread=expected_spread,
        ),
        prompt_factory=prompts_for_provider,
    )
    research_truncated, research_compacted, prompt_research_characters = prompt_metadata.get(
        provider_name, (False, False, 0)
    )
    injury_gaps = _apply_independent_injury_statuses(
        data, research.get("injury_statuses", [])
    )
    sneaky_data = {"picks": data.get("sneaky_props", [])}
    sneaky_status_gaps = _apply_independent_injury_statuses(
        sneaky_data, research.get("injury_statuses", [])
    )
    data["sneaky_props"] = sneaky_data.get("picks", [])
    prop_gaps = _filter_player_props_by_usage(data, player_usage)
    sneaky_usage_data = {"picks": data.get("sneaky_props", [])}
    sneaky_usage_gaps = _filter_player_props_by_usage(sneaky_usage_data, player_usage)
    data["sneaky_props"] = sneaky_usage_data.get("picks", [])
    td_gaps = _filter_td_scorers_by_usage(data, player_usage)
    news_evidence_text = "\n".join(
        line for line in str(research.get("research_text", "")).splitlines()
        if "ESPN RECENT TEAM NEWS" in line or "ADAM SCHEFTER PRIORITY NEWS" in line
    )
    impact_gaps = _filter_injury_impacts(
        data, research.get("injury_statuses", []), research.get("rosters"),
        team_names, player_usage=player_usage, news_evidence_text=news_evidence_text
    )
    market_gaps = _separate_unverified_player_props(data, research)
    moved_sneaky_games = _move_sneaky_game_picks(data)
    game_market_gaps = _filter_game_level_picks(data, game, research, team_names)
    contradiction_gaps = _remove_contradictory_period_game_picks(data, team_names)
    quality_gaps = (
        injury_gaps + sneaky_status_gaps + prop_gaps + sneaky_usage_gaps
        + td_gaps + impact_gaps + market_gaps + game_market_gaps + contradiction_gaps
    )
    if moved_sneaky_games:
        quality_gaps.append("Moved 1Q/1H game-line ideas from Sneaky Props into the game-level bets section.")
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
    top_market_keys = {
        (_normalize_name(str(item.get("player_or_team") or "")),
         _normalize_name(str(item.get("bet_description") or "")))
        for item in data.get("picks", [])
    }
    data["sneaky_props"] = [
        item for item in data.get("sneaky_props", [])
        if (_normalize_name(str(item.get("player_or_team") or "")),
            _normalize_name(str(item.get("bet_description") or ""))) not in top_market_keys
    ]
    data["player_research_summary"] = player_research_summary
    data["analysis_version"] = 12
    data["odds_api_status"] = research.get("odds_api_status", "not_configured")
    data["odds_api_usage"] = research.get("odds_api_usage", {})
    data["selected_player_candidates"] = research.get("selected_player_candidates", {})
    data["researched_player_evidence"] = player_usage
    data["sources"] = list(research.get("sources", []))
    data["independent_research"] = original_research
    data["ai_provider"] = provider_name
    data["research_mode"] = "independent_espn_plus_ai"
    data["research_truncated_for_prompt"] = research_truncated
    data["research_compacted_for_prompt"] = research_compacted
    data["research_prompt_characters"] = prompt_research_characters
    return data
