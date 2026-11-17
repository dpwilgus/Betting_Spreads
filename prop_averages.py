"""Stat-specific per-game averages used in player-prop and TD workbook rows."""
from __future__ import annotations

from typing import Any, Callable
import re


STAT_MAP = {
    "player_pass_yds": (("passingYards",), "Passing yards"),
    "player_pass_tds": (("passingTouchdowns",), "Passing TDs"),
    "player_pass_attempts": (("passingAttempts",), "Pass attempts"),
    "player_pass_completions": (("completions",), "Completions"),
    "player_pass_interceptions": (("interceptions",), "Interceptions"),
    "player_rush_yds": (("rushingYards",), "Rushing yards"),
    "player_rush_attempts": (("rushingAttempts",), "Carries"),
    "player_rush_tds": (("rushingTouchdowns",), "Rushing TDs"),
    "player_receptions": (("receptions",), "Receptions"),
    "player_reception_yds": (("receivingYards",), "Receiving yards"),
    "player_reception_tds": (("receivingTouchdowns",), "Receiving TDs"),
    "player_reception_longest": (("longReception",), "Longest reception"),
    "player_pass_longest_completion": (("longestPass",), "Longest completion"),
    "player_rush_longest": (("longRushing",), "Longest rush"),
    "player_anytime_td": (("rushingTouchdowns", "receivingTouchdowns"), "Offensive TDs"),
    "player_1st_td": (("rushingTouchdowns", "receivingTouchdowns"), "Offensive TDs"),
    "player_pass_rush_yds": (("passingYards", "rushingYards"), "Pass + rush yards"),
    "player_rush_reception_yds": (("rushingYards", "receivingYards"), "Rush + receiving yards"),
    "player_pass_rush_reception_yds": (("passingYards", "rushingYards", "receivingYards"), "Total yards"),
}


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _number(stats: dict[str, Any], key: str) -> float:
    try:
        return float(str(stats.get(key, 0)).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def format_per_game_average(
    player: str,
    market_key: str,
    player_usage: dict[str, dict[str, Any]],
) -> str:
    usage = player_usage.get(_normalize(player))
    if not isinstance(usage, dict):
        return "Not available"
    season = usage.get("season") if isinstance(usage.get("season"), dict) else {}
    recent = usage.get("recent") if isinstance(usage.get("recent"), list) else []
    try:
        games = int(usage.get("games_available") or 0)
    except (TypeError, ValueError):
        games = 0
    definition = STAT_MAP.get(market_key)
    if games <= 0 or not definition:
        return "Not available"
    stat_keys, label = definition
    if not any(key in season for key in stat_keys):
        return "Not available"
    season_average = sum(_number(season, key) for key in stat_keys) / games
    recent_values = []
    for game in recent[:3]:
        stats = game.get("stats", {}) if isinstance(game, dict) else {}
        if isinstance(stats, dict) and any(key in stats for key in stat_keys):
            recent_values.append(sum(_number(stats, key) for key in stat_keys))
    if recent_values:
        recent_average = sum(recent_values) / len(recent_values)
        return f"{label}: {season_average:.1f} season / {recent_average:.1f} last {len(recent_values)}"
    return f"{label}: {season_average:.1f} season / recent n/a"


def attach_per_game_averages(
    data: dict[str, Any],
    player_usage: dict[str, dict[str, Any]],
    market_key_for_description: Callable[[str], str | None],
) -> None:
    for collection in ("picks", "model_only_picks", "sneaky_props"):
        for pick in data.get(collection, []) or []:
            if not isinstance(pick, dict) or pick.get("market_type") != "Player Prop":
                continue
            market_key = market_key_for_description(str(pick.get("bet_description") or ""))
            pick["average_per_game"] = format_per_game_average(
                str(pick.get("player_or_team") or ""), market_key or "", player_usage
            )
    for scorer in data.get("touchdown_scorers", []) or []:
        if isinstance(scorer, dict):
            scorer["average_per_game"] = format_per_game_average(
                str(scorer.get("player") or ""), "player_anytime_td", player_usage
            )
