"""Provider-independent NFL matchup analysis with independent live research."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ai_providers import NoUsableAnalysisError, generate_json, generate_json_ensemble
from live_research import collect_game_research
from prop_averages import attach_per_game_averages

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
        if (upper.startswith("ODDS QUOTES RETRIEVED")
                or upper.startswith("THE ODDS API VERIFIED PLAYER-PROP QUOTES")
                or upper.startswith("THE ODDS API VERIFIED FULL-GAME SPORTSBOOK MARKETS")):
            priority = -1
        elif (upper.startswith("HEADER") or upper.startswith("INJURIES:")
                or upper.startswith("ODDS API MARKET DISCOVERY")
                or upper.startswith("THE ODDS API VERIFIED FULL-GAME")
                or upper.startswith("THE ODDS API VERIFIED PLAYER-PROP")
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
    priority_critical = [item for item in prioritized if item[0] < 0]
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
    prioritized = priority_critical + priority_zero + balanced_player_logs + remaining

    kept = []
    used = 0
    essential_omitted = False
    optional_omitted = False
    marker = "[Research truncated.]"
    budget = max(0, max_chars - len(marker) - 2)
    for priority, _, section in prioritized:
        separator = 2 if kept else 0
        if len(section) + separator > budget - used:
            if priority == -1:
                # Never discard the whole verified quote block. Retain as many
                # complete quote lines as fit, beginning with its heading.
                remaining_budget = max(0, budget - used - separator)
                compact_lines = []
                compact_length = 0
                for line in section.splitlines():
                    addition = len(line) + (1 if compact_lines else 0)
                    if compact_length + addition > remaining_budget:
                        break
                    compact_lines.append(line)
                    compact_length += addition
                if compact_lines:
                    compact_section = "\n".join(compact_lines)
                    kept.append(compact_section)
                    used += separator + len(compact_section)
                essential_omitted = True
            elif priority <= 2:
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


def _rank_quoted_player_markets(
    quotes: list[dict[str, Any]],
    usage: dict[str, dict[str, Any]],
    rosters: dict[str, list[str]],
    teams: tuple[str, str],
    limit: int = 12,
    injury_statuses: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Prioritize consequential offensive roles, not obscure alternate lines.

    Season-average/line gaps are ONLY a screening signal. One exact quote per
    player is displayed, preventing a reserve's alternate lines from crowding
    out leading QB/RB/WR/TE opportunities.
    """
    stats = {
        "player_pass_yds": "passingYards",
        "player_rush_yds": "rushingYards",
        "player_reception_yds": "receivingYards",
        "player_receptions": "receptions",
        "player_pass_attempts": "passingAttempts",
        "player_pass_completions": "completions",
        "player_rush_attempts": "rushingAttempts",
    }
    roster_team = {
        _normalize_name(name): team
        for team in teams for name in (rosters.get(team) or [])
        if isinstance(name, str)
    }
    unavailable = {
        _normalize_name(str(report.get("player") or ""))
        for report in (injury_statuses or [])
        if isinstance(report, dict) and _is_unavailable_status(
            report.get("status") or report.get("availability") or ""
        )
    }
    ranked: list[tuple[float, dict[str, Any]]] = []
    for q in quotes or []:
        if not isinstance(q, dict) or q.get("market_key") not in stats:
            continue
        player = str(q.get("player") or "").strip()
        key = _normalize_name(player)
        if key not in roster_team or key in unavailable:
            continue
        record = usage.get(key, {})
        season = record.get("season") if isinstance(record.get("season"), dict) else {}
        try:
            games = int(record.get("games_available") or 0)
            point = float(q["point"])
            average = _stat_number(season, stats[q["market_key"]]) / games
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            continue
        if games < 3 or point <= 0 or average <= 0 or stats[q["market_key"]] not in season:
            continue
        side = str(q.get("side") or "").casefold()
        if side not in {"over", "under"} or q.get("price") in (None, ""):
            continue
        if not all(q.get(field) for field in ("sportsbook", "line", "source_url", "retrieved_at")):
            continue
        position = str(record.get("position") or "").upper()
        carries = _stat_number(season, "rushingAttempts") / games
        targets = _stat_number(season, "receivingTargets") / games
        receptions = _stat_number(season, "receptions") / games
        rec_yards = _stat_number(season, "receivingYards") / games
        pass_attempts = _stat_number(season, "passingAttempts") / games
        # Current-season opportunities are a reproducible proxy for a key
        # offensive role. A famous name alone is not evidence of usage.
        if position == "QB":
            role_score = 3.0 + min(pass_attempts / 18.0, 2.0)
            if q["market_key"] in {"player_rush_yds", "player_rush_attempts"}:
                if carries < 3.0:
                    continue
            elif pass_attempts < 15.0:
                continue
        elif position == "RB":
            opportunities = carries + targets
            if opportunities < 7.0:
                continue
            role_score = 2.0 + min(opportunities / 9.0, 2.5)
        elif position in {"WR", "TE"}:
            if targets < 3.5 and receptions < 2.5 and rec_yards < 35:
                continue
            role_score = 2.0 + min(max(targets / 4.0, rec_yards / 45.0), 2.5)
            if q["market_key"] not in {"player_reception_yds", "player_receptions"}:
                continue
        else:
            continue
        difference = (average - point) / max(average, point)
        alignment = difference if side == "over" else -difference
        try:
            price = float(str(q["price"]).replace("+", ""))
        except (TypeError, ValueError):
            continue
        # Prefer standard -110-ish prices to extreme alternate lines.
        price_penalty = min(abs(price + 110) / 300.0, 1.0)
        score = (
            role_score + min(games, 12) / 20.0
            + max(-0.25, min(alignment, 0.35))
            - price_penalty
        )
        ranked.append((score, {
            "player": player, "team": roster_team[key],
            "position": position, "market_key": q["market_key"],
            "side": side.title(), "point": point, "price": q["price"],
            "sportsbook": q["sportsbook"],
            "season_average": round(average, 2), "games": games,
            "line": q["line"], "source_url": q["source_url"],
            "retrieved_at": q["retrieved_at"],
            "opportunities_per_game": round(
                pass_attempts if position == "QB" else
                carries + targets if position == "RB" else targets, 2
            ),
        }))
    ranked.sort(key=lambda item: item[0], reverse=True)
    selected = []
    used_players: set[str] = set()
    team_counts: dict[str, int] = {}
    per_team_limit = max(1, (limit + len(teams) - 1) // max(len(teams), 1))
    for _, candidate in ranked:
        key = _normalize_name(candidate["player"])
        team = candidate["team"]
        if key in used_players or team_counts.get(team, 0) >= per_team_limit:
            continue
        selected.append(candidate)
        used_players.add(key)
        team_counts[team] = team_counts.get(team, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def _build_unquoted_market_watchlist(
    player_summary: list[dict[str, Any]],
    injury_statuses: list[dict[str, Any]] | None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Surface evidence-backed markets to monitor without inventing lines or edges."""
    blocked = {
        _normalize_name(str(report.get("player") or ""))
        for report in (injury_statuses or [])
        if isinstance(report, dict) and _is_unavailable_status(report.get("status") or report.get("availability") or "")
    }
    options = {
        "QB": (("passing_yards_per_game", "passing_yards_recent", "passing yards"),),
        "RB": (("rushing_yards_per_game", "rushing_yards_recent", "rushing yards"),),
        "WR": (("receiving_yards_per_game", "receiving_yards_recent", "receiving yards"),),
        "TE": (("receiving_yards_per_game", "receiving_yards_recent", "receiving yards"),),
    }
    candidates = []
    seen = set()
    for row in player_summary:
        if not isinstance(row, dict):
            continue
        player = str(row.get("player") or "")
        key = _normalize_name(player)
        if not key or key in blocked or key in seen:
            continue
        if int(row.get("games") or 0) < 3:
            continue
        for season_key, recent_key, market in options.get(str(row.get("position") or ""), ()):
            season_avg, recent_avg = row.get(season_key), row.get(recent_key)
            if not isinstance(season_avg, (int, float)) or season_avg <= 0:
                continue
            if not isinstance(recent_avg, (int, float)):
                continue
            seen.add(key)
            candidates.append((season_avg, {
                "market_type": "Player Prop", "player_or_team": player,
                "bet_description": f"Monitor {market} market — line not posted",
                "market_line": "NOT VERIFIED — no sportsbook player-prop quote",
                "play_threshold": "Await exact sportsbook line",
                "estimated_hit_probability": None,
                "confidence": None,
                "statistical_rationale": (
                    f"ESPN season {season_avg:.1f}/game; last three {recent_avg:.1f}/game. "
                    "Compare with the posted line before selecting a side."
                ),
                "risk_factors": "No line, odds, edge or confirmed active status; not a bet",
                "data_quality": "Limited",
                "average_per_game": f"{market.title()}: {season_avg:.1f} season / {recent_avg:.1f} last 3",
            }))
            break
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in candidates[:limit]]


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
    # Summary and game script are narrative presentation, not proof of a bet.
    # Preserve otherwise usable structured selections if a model omits one.
    for field, fallback in (
        ("summary", "Matchup summary unavailable from this AI provider."),
        ("game_script", "Game-script narrative unavailable from this AI provider."),
    ):
        if not isinstance(data.get(field), str) or not data[field].strip():
            data[field] = fallback
            data.setdefault("data_gaps", [])
            if isinstance(data["data_gaps"], list):
                data["data_gaps"].append(
                    f"AI provider omitted {field}; no narrative was inferred."
                )
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

    if not isinstance(data.get("data_gaps"), list):
        data["data_gaps"] = []

    # Injury impacts are supplemental context. A provider occasionally emits
    # an unsupported direction label; discard only that row rather than
    # rejecting valid picks and the rest of the matchup analysis.
    if isinstance(injury_response, list):
        valid_injury_rows = []
        for impact in injury_response:
            if not isinstance(impact, dict):
                data["data_gaps"].append(
                    "Removed an injury/news impact because the response row was not an object."
                )
                continue
            direction = impact.get("impact_direction")
            if direction not in ("Positive", "Negative", "Mixed"):
                data["data_gaps"].append(
                    f"Removed injury/news impact for {impact.get('injured_player') or 'an unspecified player'}: "
                    f"unsupported impact direction {direction!r}; expected Positive, Negative, or Mixed."
                )
                continue
            valid_injury_rows.append(impact)
        data["injury_impact"] = valid_injury_rows
        injury_response = valid_injury_rows
    else:
        data["injury_impact"] = []
        data["data_gaps"].append(
            "Removed injury/news impacts because the response section was not a list."
        )
        injury_response = []

    # TD scorers are supplemental candidates, not the core betting analysis.
    # A malformed probability/confidence on one scorer must not erase otherwise
    # valid player props, game bets, or injury analysis. Drop only the bad row
    # and retain a visible data-gap explanation.
    valid_scorers = []
    for scorer in scorers:
        if not isinstance(scorer, dict):
            data.setdefault("data_gaps", []).append(
                "Removed a touchdown candidate because its response was not an object."
            )
            continue
        try:
            td_probability = float(scorer.get("estimated_td_probability", -1))
            td_confidence = int(scorer.get("confidence", 0))
        except (TypeError, ValueError):
            data.setdefault("data_gaps", []).append(
                f"Removed {scorer.get('player', 'a touchdown candidate')}: "
                "touchdown probability or confidence was not numeric."
            )
            continue
        if not 0 <= td_probability <= 100 or not 1 <= td_confidence <= 10:
            data.setdefault("data_gaps", []).append(
                f"Removed {scorer.get('player', 'a touchdown candidate')}: "
                f"invalid touchdown probability/confidence "
                f"({scorer.get('estimated_td_probability')!r}/"
                f"{scorer.get('confidence')!r}); expected probability 0–100 "
                "and confidence 1–10."
            )
            continue
        valid_scorers.append(scorer)
    data["touchdown_scorers"] = valid_scorers
    scorers = valid_scorers

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
    # Isolate invalid model picks rather than rejecting an entire provider's
    # otherwise usable analysis. Never coerce unsupported probabilities or
    # silently change a sportsbook market; invalid rows are removed.
    valid_picks = []
    for item in picks:
        reason = None
        if not isinstance(item, dict):
            reason = "pick was not a JSON object"
        else:
            try:
                probability = float(item.get("estimated_hit_probability", -1))
                confidence = int(item.get("confidence", 0))
                if not 0 <= probability <= 100 or not 1 <= confidence <= 10:
                    reason = "invalid pick probability or confidence"
            except (TypeError, ValueError, OverflowError):
                reason = "non-numeric pick probability or confidence"
            if reason is None and item.get("market_type") not in ("Player Prop", "Game Prop"):
                reason = "unsupported betting market type"
            if reason is None and item.get("data_quality") not in ("Strong", "Moderate", "Limited"):
                reason = "unsupported data-quality label"
            if reason is None:
                for field in ("market_type", "player_or_team", "bet_description",
                              "market_line", "play_threshold", "statistical_rationale",
                              "risk_factors"):
                    if not item.get(field):
                        reason = f"missing required pick field: {field}"
                        break
            if reason is None and rosters is not None and item.get("market_type") == "Player Prop":
                known_players = {
                    _normalize_name(player)
                    for roster in rosters.values()
                    for player in roster
                }
                if not known_players or _normalize_name(item.get("player_or_team", "")) not in known_players:
                    reason = "player not found on either current ESPN roster"
        if reason:
            player = item.get("player_or_team", "unknown") if isinstance(item, dict) else "unknown"
            data["data_gaps"].append(f"Removed model pick for {player}: {reason}.")
            continue
        valid_picks.append(item)
    data["picks"] = valid_picks
    picks = valid_picks

    # Do not accept a narrative-only response when all actionable and
    # supplemental evidence was discarded.
    if not any((
        picks, scorers, sneaky_response if isinstance(sneaky_response, list) else [],
        valid_injury_response, underdog_response,
    )):
        raise NoUsableAnalysisError(
            "AI response contains no usable matchup analysis: all picks, touchdown candidates, "
            "injury impacts, sneaky props, and underdog assessment are empty."
        )
    # Sneaky ideas are optional. Sanitize them row by row, just as primary
    # picks and TD scorers are sanitized above; never reject a provider's
    # valid main picks because one supplemental idea has malformed numbers.
    sneaky = data.get("sneaky_props", [])
    if not isinstance(sneaky, list):
        data["data_gaps"].append("Removed sneaky-prop section: expected a list.")
        sneaky = []
    valid_sneaky = []
    for item in sneaky[:8]:
        reason = None
        if not isinstance(item, dict):
            reason = "not an object"
        else:
            try:
                probability = float(item.get("estimated_hit_probability", -1))
                confidence = int(item.get("confidence", 0))
                if not 0 <= probability <= 100 or not 1 <= confidence <= 10:
                    reason = "invalid probability or confidence"
            except (TypeError, ValueError, OverflowError):
                reason = "non-numeric probability or confidence"
            if reason is None and item.get("market_type") not in ("Player Prop", "Game Prop"):
                reason = "unsupported market type"
            if reason is None and item.get("data_quality") not in ("Strong", "Moderate", "Limited"):
                reason = "unsupported data-quality label"
            if reason is None and not all(item.get(field) for field in (
                "player_or_team", "bet_description", "market_line", "play_threshold",
                "statistical_rationale", "risk_factors",
            )):
                reason = "missing required evidence"
            if reason is None and rosters is not None and item.get("market_type") == "Player Prop":
                known_players = {
                    _normalize_name(player) for roster in rosters.values() for player in roster
                }
                if _normalize_name(item.get("player_or_team", "")) not in known_players:
                    reason = "player not found on ESPN rosters"
        if reason:
            data["data_gaps"].append(
                f"Removed sneaky prop for {item.get('player_or_team', 'unknown') if isinstance(item, dict) else 'unknown'}: {reason}."
            )
        else:
            valid_sneaky.append(item)
    if len(sneaky) > 8:
        data["data_gaps"].append("Limited sneaky-prop ideas to the first eight rows.")
    data["sneaky_props"] = valid_sneaky
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
    if re.search(r"(first quarter|1st quarter|q1).*passing yards|passing yards.*(first quarter|1st quarter|q1)", text):
        return "player_pass_yds_q1"
    patterns = (
        (r"passing, rushing and receiving yards|passing plus rushing plus receiving yards", "player_pass_rush_reception_yds"),
        (r"passing plus rushing yards|pass(?:ing)? and rushing yards", "player_pass_rush_yds"),
        (r"rushing plus receiving yards|rush(?:ing)? and receiving yards", "player_rush_reception_yds"),
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


def _best_matching_quote(
    pick: dict[str, Any],
    quotes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select the best available price for the exact player, market, side and point."""
    matches = [
        quote for quote in quotes
        if isinstance(quote, dict)
        and _quote_matches_pick(pick, quote)
        and quote.get("line") not in (None, "")
        and quote.get("price") not in (None, "")
        and quote.get("sportsbook") and quote.get("source_url")
        and quote.get("retrieved_at")
    ]
    if not matches:
        return None

    def price_score(quote: dict[str, Any]) -> tuple[float, str]:
        try:
            price = float(str(quote.get("price") or "").replace("+", ""))
        except (TypeError, ValueError):
            price = float("-inf")
        return price, str(quote.get("retrieved_at") or "")

    return max(matches, key=price_score)


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
        return _best_matching_quote(pick, verified_quotes)

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
    """Require an exact sportsbook quote or clearly label an ESPN snapshot."""
    gaps = []
    totals = _game_market_total_points(research)
    sportsbook_quotes = research.get("verified_sportsbook_game_markets", [])
    sportsbook_quotes = sportsbook_quotes if isinstance(sportsbook_quotes, list) else []
    scoreboard_line = str(game.get("Betting Line") or "")
    spread_match = re.search(r"([A-Za-z]{2,3})\s*([+-]\d+(?:\.\d+)?)", scoreboard_line)
    scoreboard_team = _team_key(spread_match.group(1)) if spread_match else ""
    scoreboard_spread = float(spread_match.group(2)) if spread_match else None

    def same_point(value: Any, expected: float) -> bool:
        try:
            return abs(float(value) - expected) < 0.001
        except (TypeError, ValueError):
            return False

    def quoted_line(quote: dict[str, Any]) -> str:
        return (
            f"{quote.get('line')} ({quote.get('price')}) — "
            f"{quote.get('sportsbook')}"
        )

    def valid_quote(quote: dict[str, Any]) -> bool:
        return bool(
            quote.get("line") and quote.get("price") not in (None, "")
            and quote.get("sportsbook") and quote.get("source_url")
            and quote.get("retrieved_at")
        )

    retained = []
    for pick in data.get("picks", []):
        if pick.get("market_type") != "Game Prop":
            retained.append(pick)
            continue
        description = " ".join(str(pick.get(field) or "") for field in (
            "bet_description", "play_threshold", "market_line"
        ))
        normalized = description.casefold()
        identity_text = " ".join(str(pick.get(field) or "") for field in (
            "bet_description", "play_threshold"
        ))

        if re.search(r"\b(?:over|under)\b.*(?:total|points)", normalized):
            pick_team = _team_key(str(pick.get("player_or_team") or ""))
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
            side_match = re.search(r"\b(over|under)\b", normalized)
            side = side_match.group(1).casefold() if side_match else ""
            quote = next((
                q for q in sportsbook_quotes
                if isinstance(q, dict) and q.get("market_key") == "totals"
                and str(q.get("side") or "").casefold() == side
                and point is not None and same_point(q.get("point"), point)
                and valid_quote(q)
            ), None)
            if quote:
                pick["market_line"] = quoted_line(quote)
                pick["market_source"] = quote["source_url"]
                pick["market_retrieved_at"] = quote["retrieved_at"]
                retained.append(pick)
                continue
            if point is None or not any(abs(point - quote_total) < 0.001 for quote_total in totals):
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

        team_key = _team_key(str(pick.get("player_or_team") or ""))
        if "moneyline" in normalized or re.search(r"\b(?:to win|ml)\b", normalized):
            quote = next((
                q for q in sportsbook_quotes
                if isinstance(q, dict) and q.get("market_key") == "h2h"
                and _team_key(str(q.get("player") or "")) == team_key
                and valid_quote(q)
            ), None)
            if quote:
                pick["market_line"] = quoted_line(quote)
                pick["market_source"] = quote["source_url"]
                pick["market_retrieved_at"] = quote["retrieved_at"]
                retained.append(pick)
            else:
                gaps.append(
                    f"Removed unverified game moneyline {pick.get('bet_description')}: no matching sportsbook quote was supplied."
                )
            continue

        if "spread" in normalized or re.search(r"[+-]\s*\d+(?:\.\d+)?", normalized):
            pick_line_text = " ".join(str(pick.get(field) or "") for field in (
                "bet_description", "play_threshold"
            ))
            line_value_match = re.search(r"([+-]\d+(?:\.\d+)?)", pick_line_text)
            if line_value_match and team_key in {
                _team_key(team_names[0]), _team_key(team_names[1])
            }:
                point = float(line_value_match.group(1))
                quote = next((
                    q for q in sportsbook_quotes
                    if isinstance(q, dict) and q.get("market_key") == "spreads"
                    and _team_key(str(q.get("player") or "")) == team_key
                    and same_point(q.get("point"), point) and valid_quote(q)
                ), None)
                if quote:
                    pick["market_line"] = quoted_line(quote)
                    pick["market_source"] = quote["source_url"]
                    pick["market_retrieved_at"] = quote["retrieved_at"]
                    retained.append(pick)
                    continue
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
        "\nUNDERDOG JSON: "
        + template
        + " Use evidence-based numeric probabilities (0-100); upset must not exceed cover. "
        "do not copy example values. "
        "\n"
    )


def _merge_provider_analyses(
    provider_results: list[tuple[dict[str, Any], str]],
    verified_quotes: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Merge independent model opinions, rewarding agreement on the same exact market."""
    if not provider_results:
        raise RuntimeError("No AI provider returned a usable analysis.")

    def quote_for_pick(pick: dict[str, Any]) -> dict[str, Any] | None:
        if pick.get("market_type") != "Player Prop":
            return None
        return _best_matching_quote(pick, verified_quotes)

    verified_counts = {
        name: sum(1 for pick in result.get("picks", []) if quote_for_pick(pick))
        for result, name in provider_results
    }
    primary_result, primary_name = max(
        provider_results,
        key=lambda pair: (
            verified_counts.get(pair[1], 0),
            len(pair[0].get("picks", [])),
            sum(int(p.get("confidence", 0) or 0) for p in pair[0].get("picks", [])),
        ),
    )

    def median(values: list[float]) -> float:
        ordered = sorted(values)
        if not ordered:
            return 0.0
        middle = len(ordered) // 2
        return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2

    def merge_recommendations(field: str, limit: int, prefer_verified: bool = False) -> list[dict[str, Any]]:
        groups: dict[tuple[Any, ...], list[tuple[dict[str, Any], str, dict[str, Any] | None]]] = {}
        for result, provider_name in provider_results:
            for raw in result.get(field, []) or []:
                if not isinstance(raw, dict):
                    continue
                pick = dict(raw)
                quote = quote_for_pick(pick) if prefer_verified else None
                if quote:
                    key = (
                        "verified", _normalize_name(str(quote.get("player") or "")),
                        str(quote.get("market_key") or ""), str(quote.get("side") or "").casefold(),
                        str(quote.get("point") if quote.get("point") is not None else ""),
                    )
                else:
                    key = (
                        str(pick.get("market_type") or ""),
                        _normalize_name(str(pick.get("player_or_team") or "")),
                        _normalize_name(str(pick.get("bet_description") or "")),
                        _normalize_name(str(pick.get("play_threshold") or "")),
                    )
                groups.setdefault(key, []).append((pick, provider_name, quote))

        merged = []
        for entries in groups.values():
            entries.sort(
                key=lambda entry: (
                    int(entry[0].get("confidence", 0) or 0),
                    float(entry[0].get("estimated_hit_probability", 0) or 0),
                ),
                reverse=True,
            )
            item = dict(entries[0][0])
            probabilities = [
                float(entry[0].get("estimated_hit_probability", 0) or 0)
                for entry in entries
            ]
            confidences = [
                float(entry[0].get("confidence", 0) or 0)
                for entry in entries
            ]
            item["estimated_hit_probability"] = round(median(probabilities), 1)
            item["confidence"] = max(1, min(10, round(median(confidences))))
            providers = list(dict.fromkeys(entry[1] for entry in entries))
            item["model_votes"] = len(providers)
            item["model_providers"] = providers
            item["model_consensus"] = f"{len(providers)}/{len(provider_results)}"
            rationale = str(item.get("statistical_rationale") or "").strip()
            item["statistical_rationale"] = (
                rationale + f" Model agreement: {len(providers)}/{len(provider_results)}."
            ).strip()
            risks = list(dict.fromkeys(
                str(entry[0].get("risk_factors") or "").strip()
                for entry in entries if str(entry[0].get("risk_factors") or "").strip()
            ))
            if len(risks) > 1:
                item["risk_factors"] = " | ".join(risks[:2])
            quote = next((entry[2] for entry in entries if entry[2]), None)
            if quote:
                item["market_line"] = (
                    f"{quote['line']} ({quote['price']}) — {quote['sportsbook']}"
                )
                item["market_source"] = quote["source_url"]
                item["market_retrieved_at"] = quote["retrieved_at"]
                item["_verified_quote"] = True
            else:
                item["_verified_quote"] = False
            merged.append(item)

        merged.sort(key=lambda item: (
            bool(item.get("_verified_quote")) if prefer_verified else False,
            int(item.get("model_votes", 0)),
            int(item.get("confidence", 0)),
            float(item.get("estimated_hit_probability", 0)),
        ), reverse=True)
        for item in merged:
            item.pop("_verified_quote", None)
        return merged[:limit]

    data = dict(primary_result)
    # Preserve the gold-standard five-pick slate, with exact verified player props ranked first.
    data["picks"] = merge_recommendations("picks", 5, prefer_verified=True)
    data["sneaky_props"] = merge_recommendations("sneaky_props", 8, prefer_verified=True)

    scorer_groups: dict[tuple[str, str], list[tuple[dict[str, Any], str]]] = {}
    for result, provider_name in provider_results:
        for scorer in result.get("touchdown_scorers", []) or []:
            if isinstance(scorer, dict):
                key = (
                    _normalize_name(str(scorer.get("player") or "")),
                    _team_key(str(scorer.get("team") or "")),
                )
                scorer_groups.setdefault(key, []).append((dict(scorer), provider_name))
    scorers = []
    for entries in scorer_groups.values():
        entries.sort(key=lambda entry: int(entry[0].get("confidence", 0) or 0), reverse=True)
        scorer = dict(entries[0][0])
        scorer["estimated_td_probability"] = round(median([
            float(entry[0].get("estimated_td_probability", 0) or 0)
            for entry in entries
        ]), 1)
        scorer["confidence"] = max(1, min(10, round(median([
            float(entry[0].get("confidence", 0) or 0) for entry in entries
        ]))))
        scorer["model_votes"] = len({entry[1] for entry in entries})
        scorer["model_consensus"] = f"{scorer['model_votes']}/{len(provider_results)}"
        scorer["rationale"] = (
            str(scorer.get("rationale") or "").strip()
            + f" Model agreement: {scorer['model_consensus']}."
        ).strip()
        scorers.append(scorer)
    scorers.sort(key=lambda item: (
        int(item.get("model_votes", 0)), int(item.get("confidence", 0)),
        float(item.get("estimated_td_probability", 0)),
    ), reverse=True)
    data["touchdown_scorers"] = scorers[:5]

    impacts = []
    seen_impacts = set()
    gaps = []
    for result, _provider_name in provider_results:
        for impact in result.get("injury_impact", []) or []:
            if not isinstance(impact, dict):
                continue
            key = (
                _normalize_name(str(impact.get("injured_player") or "")),
                _normalize_name(str(impact.get("affected_player") or "")),
                str(impact.get("impact_type") or ""),
            )
            if key not in seen_impacts:
                seen_impacts.add(key)
                impacts.append(dict(impact))
        for gap in result.get("data_gaps", []) or []:
            if gap and str(gap) not in gaps:
                gaps.append(str(gap))
    data["injury_impact"] = impacts[:5]
    data["data_gaps"] = gaps[:6]
    data["ensemble_provider_names"] = [name for _result, name in provider_results]
    data["ensemble_primary_provider"] = primary_name
    return data, primary_name



def _rank_quoted_touchdown_markets(
    quotes: list[dict[str, Any]],
    usage: dict[str, dict[str, Any]],
    rosters: dict[str, list[str]],
    teams: tuple[str, str],
    injury_statuses: list[dict[str, Any]] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Verified anytime-TD offers for high-opportunity offensive players only."""
    team_by_player = {
        _normalize_name(player): team
        for team in teams for player in rosters.get(team, [])
    }
    unavailable = {
        _normalize_name(str(report.get("player") or ""))
        for report in (injury_statuses or [])
        if isinstance(report, dict) and _is_unavailable_status(
            report.get("status") or report.get("availability") or ""
        )
    }
    ranked = []
    for quote in quotes or []:
        if not isinstance(quote, dict) or quote.get("market_key") != "player_anytime_td":
            continue
        key = _normalize_name(str(quote.get("player") or ""))
        if key not in team_by_player or key in unavailable:
            continue
        if str(quote.get("side") or "").casefold() not in {"yes", "over"}:
            continue
        if quote.get("price") in (None, "") or not all(
            quote.get(field) for field in ("line", "sportsbook", "source_url", "retrieved_at")
        ):
            continue
        record = usage.get(key, {})
        season = record.get("season") if isinstance(record.get("season"), dict) else {}
        try:
            games = int(record.get("games_available") or 0)
        except (TypeError, ValueError):
            continue
        if games < 3:
            continue
        position = str(record.get("position") or "").upper()
        carries = _stat_number(season, "rushingAttempts") / games
        targets = _stat_number(season, "receivingTargets") / games
        rush_td = _stat_number(season, "rushingTouchdowns")
        rec_td = _stat_number(season, "receivingTouchdowns")
        if position == "QB":
            opportunities = carries
            eligible = carries >= 3 and rush_td >= 2
        elif position == "RB":
            opportunities = carries + targets
            eligible = opportunities >= 10
        elif position in {"WR", "TE"}:
            opportunities = targets
            eligible = targets >= 4 and (rec_td >= 1 or targets >= 7)
        else:
            continue
        if not eligible:
            continue
        try:
            price = float(str(quote["price"]).replace("+", ""))
        except (TypeError, ValueError):
            continue
        score = opportunities / 12 + (rush_td + rec_td) / games * 2 - min(abs(price + 110) / 500, 1)
        ranked.append((score, {
            "player": quote["player"], "team": team_by_player[key],
            "position": position, "games": games,
            "offensive_tds_per_game": round((rush_td + rec_td) / games, 2),
            "opportunities_per_game": round(opportunities, 2),
            "line": quote["line"], "price": quote["price"],
            "sportsbook": quote["sportsbook"], "source_url": quote["source_url"],
            "retrieved_at": quote["retrieved_at"],
        }))
    ranked.sort(key=lambda item: item[0], reverse=True)
    result = []
    used = set()
    for _score, item in ranked:
        key = _normalize_name(item["player"])
        if key in used:
            continue
        result.append(item)
        used.add(key)
        if len(result) >= limit:
            break
    return result


def _recover_verified_props_from_focused_review(
    data: dict[str, Any],
    quoted_shortlist: list[dict[str, Any]],
    research: dict[str, Any],
    usage: dict[str, dict[str, Any]],
    teams: tuple[str, str],
    provider_name: str,
) -> None:
    """Use a compact second review when full-game model output has no real props.

    The model is never allowed to introduce lines. Its selections must map to
    the exact independent sportsbook quotes in the focused candidate pools.
    An empty review remains a pass, not fabricated betting opportunities.
    """
    quotes = research.get("verified_player_prop_markets", [])
    if not isinstance(quotes, list):
        quotes = []
    already_verified = any(
        isinstance(pick, dict)
        and pick.get("market_type") == "Player Prop"
        and _best_matching_quote(pick, quotes)
        for pick in data.get("picks", [])
    )
    td_pool = _rank_quoted_touchdown_markets(
        quotes, usage, research.get("rosters") or {}, teams,
        injury_statuses=research.get("injury_statuses", []),
    )
    needs_td = not data.get("touchdown_scorers") and bool(td_pool)
    if (already_verified or not quoted_shortlist) and not needs_td:
        return
    if not quoted_shortlist and not td_pool:
        return
    # Unlike the full research prompt, this is bounded to key players and
    # their exact offers so the provider cannot lose them to truncation.
    context = []
    for item in quoted_shortlist:
        record = usage.get(_normalize_name(item["player"]), {})
        recent = record.get("recent") if isinstance(record.get("recent"), list) else []
        stat = {
            "player_pass_yds": "passingYards", "player_rush_yds": "rushingYards",
            "player_reception_yds": "receivingYards", "player_receptions": "receptions",
            "player_pass_attempts": "passingAttempts", "player_pass_completions": "completions",
            "player_rush_attempts": "rushingAttempts",
        }.get(item["market_key"])
        recent_values = [
            _stat_number(game["stats"], stat)
            for game in recent[:3]
            if isinstance(game, dict) and isinstance(game.get("stats"), dict)
            and stat in game["stats"]
        ]
        context.append({
            "quote": item,
            "recent_per_game": round(sum(recent_values) / len(recent_values), 2)
            if recent_values else None,
            "recent_games": len(recent_values),
        })
    injuries = [
        report for report in (research.get("injury_statuses") or [])
        if isinstance(report, dict)
        and _normalize_name(str(report.get("player") or "")) in {
            _normalize_name(item["player"]) for item in quoted_shortlist + td_pool
        }
    ][:12]
    request = json.dumps({
        "matchup": list(teams),
        "key_player_quotes": context if not already_verified else [],
        "verified_anytime_td_quotes": td_pool if needs_td else [],
        "injury_designations": injuries,
    }, ensure_ascii=False, separators=(",", ":"))
    system = (
        "You are an NFL player-prop analyst. Return a JSON object with exactly "
        "two arrays: picks and touchdown_scorers. Review supplied exact sportsbook "
        "offers for KEY high-usage players only. Select up to four player props and "
        "three anytime-TD candidates only if evidence supports them; an empty "
        "array is acceptable. Do not interpret an average above a line as proof "
        "of positive expected value. Consider sample size, recent production, "
        "injuries, volume, role and price. Never invent a player, line, price, "
        "sportsbook, injury, source or numerical stat. Each pick MUST use the "
        "exact player, market, Over/Under side and point of one key_player_quotes "
        "entry. Player picks need rank, market_type='Player Prop', player_or_team, "
        "bet_description ('Over 50.5 receiving yards' format), market_line, "
        "play_threshold, estimated_hit_probability (0-100), confidence (1-10), "
        "statistical_rationale, risk_factors, data_quality ('Strong', 'Moderate', "
        "'Limited'). Use actual season and recent numbers from the supplied data. "
        "TD candidates need rank, player, team, availability ('No injury "
        "designation found' unless explicitly documented), availability_evidence, "
        "estimated_td_probability (0-100), confidence (1-10), rationale and "
        "risk_factors. A TD candidate MUST match a verified_anytime_td_quotes entry. "
        "Never claim confirmed active status without actual game-day confirmation. "
        "Do not output anything besides JSON."
    )
    try:
        result, provider = generate_json(
            system, request,
            validator=lambda output: (
                None if isinstance(output.get("picks"), list)
                and isinstance(output.get("touchdown_scorers"), list)
                else (_ for _ in ()).throw(ValueError("Expected picks and touchdown_scorers arrays"))
            ),
            provider_filter=provider_name,
        )
    except Exception as exc:
        data.setdefault("data_gaps", []).append(
            "Focused sportsbook-prop model review unavailable: " + str(exc)[:180]
        )
        return
    valid_props = []
    pool_keys = {
        (_normalize_name(item["player"]), item["market_key"],
         item["side"].casefold(), item["point"])
        for item in quoted_shortlist
    }
    for pick in result.get("picks", [])[:4]:
        if not isinstance(pick, dict):
            continue
        quote = _best_matching_quote(pick, quotes)
        if quote is None:
            continue
        try:
            market_identity = (
                _normalize_name(str(quote["player"])), quote["market_key"],
                str(quote["side"]).casefold(), float(quote["point"]),
            )
            prob = float(pick["estimated_hit_probability"])
            confidence = int(pick["confidence"])
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
        if market_identity not in pool_keys or not (0 <= prob <= 100 and 1 <= confidence <= 10):
            continue
        if pick.get("data_quality") not in {"Strong", "Moderate", "Limited"}:
            continue
        if not all(pick.get(field) for field in (
            "player_or_team", "bet_description", "play_threshold",
            "statistical_rationale", "risk_factors",
        )):
            continue
        pick = dict(pick)
        pick["market_type"] = "Player Prop"
        pick["market_line"] = f"{quote['line']} ({quote['price']}) — {quote['sportsbook']}"
        pick["market_source"] = quote["source_url"]
        pick["market_retrieved_at"] = quote["retrieved_at"]
        valid_props.append(pick)
    valid_tds = []
    td_lookup = {_normalize_name(item["player"]): item for item in td_pool}
    for scorer in result.get("touchdown_scorers", [])[:3]:
        if not isinstance(scorer, dict):
            continue
        matched = td_lookup.get(_normalize_name(str(scorer.get("player") or "")))
        if matched is None or _team_key(str(scorer.get("team") or "")) != _team_key(matched["team"]):
            continue
        try:
            probability = float(scorer["estimated_td_probability"])
            confidence = int(scorer["confidence"])
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
        if not (0 <= probability <= 100 and 1 <= confidence <= 10):
            continue
        if not scorer.get("rationale") or not scorer.get("risk_factors"):
            continue
        scorer = dict(scorer)
        scorer["team"] = matched["team"]
        scorer["availability"] = "No injury designation found"
        scorer["availability_evidence"] = (
            "No confirmed game-day active status; check final inactive report."
        )
        scorer["market_line"] = (
            f"{matched['line']} ({matched['price']}) — {matched['sportsbook']}"
        )
        scorer["market_source"] = matched["source_url"]
        scorer["market_retrieved_at"] = matched["retrieved_at"]
        valid_tds.append(scorer)
    data.setdefault("picks", []).extend(valid_props)
    data.setdefault("touchdown_scorers", []).extend(valid_tds)
    data["focused_prop_review_provider"] = provider
    if not valid_props and not valid_tds:
        data.setdefault("data_gaps", []).append(
            "Focused model review did not identify an adequately supported exact sportsbook prop."
        )


def analyze_game(game: dict[str, Any], research: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect independent evidence, query every configured AI provider, and reconcile their picks."""
    research = research or collect_game_research(game)
    base = Path(__file__).resolve().parent / "agents"
    # Restore the fuller gold-standard analysis method and independent quality review.
    instructions = (
        (base / "NFL_MATCHUP_ANALYST.md").read_text(encoding="utf-8")
        + "\\n\\nQUALITY REVIEW CHECKLIST\\n"
        + (base / "PROBABILITY_REVIEW.md").read_text(encoding="utf-8")
    )
    matchup = f"{game['Away Team']} at {game['Home Team']}"
    team_names = (str(game["Away Team"]), str(game["Home Team"]))
    original_research = research.get("research_text", "No independent research was available.")
    # Compute and feed the same per-game averages that will be displayed in the
    # workbook, so they are part of prop selection rather than post-hoc decoration.
    player_usage = _research_player_usage(original_research)
    # The candidate selector can include stale ESPN associations. Only
    # research players explicitly present on the current matchup team roster.
    rosters = research.get("rosters") or {}
    raw_candidates = research.get("selected_player_candidates", {})
    vetted_candidates = {
        team: [
            candidate for candidate in candidates
            if isinstance(candidate, dict)
            and _normalize_name(str(candidate.get("name") or "")) in {
                _normalize_name(str(name)) for name in (rosters.get(team) or [])
            }
        ]
        for team, candidates in raw_candidates.items()
        if team in team_names and isinstance(candidates, list)
    } if isinstance(raw_candidates, dict) else {}
    player_research_summary = _build_player_research_summary(
        player_usage, vetted_candidates
    )
    if player_research_summary:
        original_research += (
            "\n\nSELECTED PLAYER PER-GAME AVERAGES (season / last three games): "
            + json.dumps(player_research_summary, ensure_ascii=False, separators=(",", ":"))
        )

    quoted_shortlist = _rank_quoted_player_markets(
        research.get("verified_player_prop_markets", []),
        player_usage, rosters, team_names,
        injury_statuses=research.get("injury_statuses", []),
    )
    quote_focus = (
        "\nEXACT VERIFIED SPORTSBOOK PROP CANDIDATES (prioritize these; "
        "evaluate, do not assume they are profitable):\n"
        + json.dumps(quoted_shortlist, ensure_ascii=False, separators=(",", ":"))
        if quoted_shortlist else ""
    )

    user_prefix = (
        f"Analyze this upcoming NFL matchup: {matchup}. Kickoff: {game.get('Game Date (ET)', 'Not supplied')}. "
        f"ESPN line: {game.get('Betting Line', 'Unavailable')}. "
        f"Status: {game.get('Game Status', 'Not supplied')}.\\n"
        "Use supplied research only; never invent stats, injuries, markets, prices, sources or dates. "
        "Return JSON only with summary, game_script, underdog_analysis, picks, sneaky_props, "
        "touchdown_scorers (up to 5), injury_impact and data_gaps. Include all schema fields. "
        f"Only these teams: {team_names[0]} and {team_names[1]}. "
        "The primary objective is to identify the strongest VERIFIED PLAYER PROPS from the "
        "supplied Odds API quotes. Evaluate the full quoted candidate pool before considering "
        "game-level bets. Target up to five verified player props when evidence supports them; "
        "do not fill the list with game bets merely to reach a count. Every selected player prop "
        "must match an exact supplied player, market, side, point, price and sportsbook quote. "
        "For Over/Under props, begin bet_description with the exact side and numeric line "
        "(for example, 'Over 50.5 rushing yards'); do not paraphrase or round the point. "
        "If no quoted prop has a defensible edge, say so explicitly; never invent or approximate "
        "a sportsbook line. Review the researched starters, usage leaders and named injury "
        "beneficiaries. Compare each exact threshold with season and last-three-game averages, "
        "opportunity volume and opponent matchup. Model-only leans remain separate and are never "
        "described as verified. After player props, evaluate full-game moneyline, spread and total "
        "quotes. "
        "Do not recommend unquoted 1Q/1H lines, invented team totals, or conflicting spreads; "
        "do not pair one team's moneyline and spread. "
        "For a numeric spread, attempt one assessment for the team receiving points, using its "
        "exact team and positive spread; separate cover and outright-upset probabilities. "
        "Monitor/Pass is valid; if the assessment cannot be supported, leave it empty and disclose "
        "the gap. Never default to the favorite. "
        "TDs require the player's own scoring role; passing TDs do not qualify a QB. "
        "Injury/news impacts: only material offensive absences, key defensive stars and verified "
        "role/QB news; investigate named replacements without assuming inherited production. "
        "Use 'Team unit' only for an elite defensive absence. Questionable players are conditional, "
        "confidence <=6, with explicit risk; Questionable TD risk_factors must start 'Questionable:'. "
        "Exclude doubtful, out and IR; missing status is not proof of active. Prioritize dated Adam "
        "Schefter ESPN reports. Return no more than five material injury/news impacts. "
        "summary <= 90 words; game_script <= 65; statistical_rationale <= 24 words; "
        "risk_factors <= 14 words; return no more than five material injury/news impacts; "
        "data_gaps <= 4 short items. No commentary outside JSON.\\n"
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
            # Reserve headroom for other requests inside Groq's rolling
            # 8k TPM window, including validation retries.
            input_token_budget = max(1000, 5000 - output_tokens - 500)
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
        # Put the structured exact quotes at the end so long ESPN research
        # cannot truncate the actionable candidates out of the prompt.
        focus = quote_focus
        if focus:
            bounded, was_truncated = _bounded_research_text(
                bounded, max(1000, research_budget - len(focus))
            )
            prompt_metadata[provider_name] = (
                was_truncated or prompt_metadata[provider_name][0],
                prompt_metadata[provider_name][1],
                len(bounded),
            )
        return (
            instructions,
            user_prefix + underdog_format
            + "\nINDEPENDENT RESEARCH AND SOURCE MATERIAL:\n" + bounded
            + focus,
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
    validator = lambda result: _validate_analysis(
        result, rosters=rosters, team_names=team_names,
        require_underdog=bool(spread_match),
        expected_underdog=expected_underdog,
        expected_spread=expected_spread,
    )
    provider_results, provider_failures, configured_provider_names = generate_json_ensemble(
        instructions,
        user_prefix,
        validator=validator,
        prompt_factory=prompts_for_provider,
    )
    data, primary_provider = _merge_provider_analyses(
        provider_results, research.get("verified_player_prop_markets", [])
    )
    _recover_verified_props_from_focused_review(
        data, quoted_shortlist, research, player_usage, team_names, primary_provider
    )
    used_provider_names = [name for _result, name in provider_results]
    provider_name = "ensemble: " + " + ".join(used_provider_names)
    used_prompt_metadata = [
        prompt_metadata[name] for name in used_provider_names if name in prompt_metadata
    ]
    research_truncated = any(item[0] for item in used_prompt_metadata)
    research_compacted = any(item[1] for item in used_prompt_metadata)
    prompt_research_characters = max((item[2] for item in used_prompt_metadata), default=0)
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
    attach_per_game_averages(data, player_usage, _expected_market_key)
    # When the sportsbook has not posted player lines, do not return a blank
    # sheet: show researched markets as explicitly non-bet watchlist entries.
    if not research.get("verified_player_prop_markets") and not data.get("model_only_picks"):
        data["model_only_picks"] = _build_unquoted_market_watchlist(
            player_research_summary, research.get("injury_statuses", [])
        )
        if data["model_only_picks"]:
            data.setdefault("data_gaps", []).append(
                "Sportsbook player props are not posted; the watchlist has no betting line, odds, side or estimated edge."
            )
    # Quotes are source facts, independent of whether a model can produce picks.
    # Keep these visible when a provider emits an empty or unusable slate.
    data["verified_market_candidates"] = [
        {
            "player_or_team": item["player"],
            "bet_description": f"{item['side']} {item['point']:g} "
                + {
                    "player_pass_yds": "passing yards",
                    "player_rush_yds": "rushing yards",
                    "player_reception_yds": "receiving yards",
                    "player_receptions": "receptions",
                    "player_pass_attempts": "passing attempts",
                    "player_pass_completions": "pass completions",
                    "player_rush_attempts": "rushing attempts",
                }.get(item["market_key"], item["market_key"]),
            "market_line": f"{item['line']} ({item['price']}) — {item['sportsbook']}",
            "market_retrieved_at": item.get("retrieved_at"),
            "market_source": item["source_url"],
            "average_per_game": f"{item['season_average']:.1f} over {item['games']} games",
            "play_threshold": "Research candidate — not a betting recommendation",
            "estimated_hit_probability": None,
            "confidence": None,
            "statistical_rationale": "Exact sportsbook quote; season average is context, not an estimated edge.",
            "risk_factors": "AI has not validated an edge; do not treat as a recommended bet",
            "data_quality": "Limited",
        }
        for item in quoted_shortlist
    ]
    data["player_research_summary"] = player_research_summary
    data["analysis_version"] = 14
    data["odds_api_status"] = research.get("odds_api_status", "not_configured")
    data["odds_api_usage"] = research.get("odds_api_usage", {})
    data["selected_player_candidates"] = research.get("selected_player_candidates", {})
    data["researched_player_evidence"] = player_usage
    data["sources"] = list(research.get("sources", []))
    data["independent_research"] = original_research
    data["ai_provider"] = provider_name
    data["ai_providers_used"] = used_provider_names
    data["ai_providers_configured"] = configured_provider_names
    data["ai_provider_failures"] = provider_failures
    data["ai_provider_consensus"] = len(used_provider_names)
    data["research_mode"] = "independent_espn_plus_multi_model_ai"
    data["research_truncated_for_prompt"] = research_truncated
    data["research_compacted_for_prompt"] = research_compacted
    data["research_prompt_characters"] = prompt_research_characters
    return data
