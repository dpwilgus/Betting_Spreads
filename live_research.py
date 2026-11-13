"""Compact independent NFL research collection; no AI provider required."""

from __future__ import annotations

import json
from functools import lru_cache
import ssl
import urllib.request
from datetime import datetime, timezone
from typing import Any

import certifi

HEADERS = {"User-Agent": "Betting-Spreads/3.0 (independent NFL research)"}

SUMMARY_MAX_CHARS = 12000
PLAYER_GAMELOG_FAILURE_LIMIT = 4
_player_gamelog_failures = 0
_player_gamelogs_disabled = False

ESPN_SUMMARY = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
    "summary?event={event_id}"
)

# Only retain fields that help explain matchup, player, injury, or betting context.
TREE_KEYS = {
    "id", "name", "displayName", "shortDisplayName", "abbreviation",
    "fullName", "position", "jersey", "team", "athlete", "player",
    "leaders", "leader", "injuries", "injury", "injuryStatus",
    "status", "type", "description", "details", "date", "event",
    "events", "competitions", "competitors", "opponent", "homeAway",
    "score", "result", "gameResult", "winner", "records", "summary",
    "shortName", "displayValue", "value", "statistics", "stat",
    "category", "items", "games", "results", "season", "week",
    "state", "completed", "startDate", "endDate",
}

STAT_FIELDS = {
    "passing": {
        "completionPct", "passingYardsPerGame", "passingTouchdowns",
        "interceptionPct", "interceptions", "yardsPerPassAttempt",
    },
    "rushing": {
        "rushingYardsPerGame", "yardsPerRushAttempt", "rushingTouchdowns",
        "rushingAttempts",
    },
    "receiving": {
        "receivingYardsPerGame", "receptions", "receivingTargets",
        "receivingTouchdowns",
    },
    "miscellaneous": {
        "firstDowns", "thirdDownConvPct",
    },
    "defensive": {
        "sacks", "passesDefended", "tacklesForLoss", "quarterbackHits",
    },
    "defensiveInterceptions": {
        "interceptions", "interceptionTouchdowns",
    },
    "general": {
        "gamesPlayed", "fumblesAndLost",
    },
    "scoring": {
        "totalPoints", "totalTouchdowns",
    },
    "kicking": {
        "fieldGoalsMade", "fieldGoalPct",
    },
}


def _get_json(url: str, timeout: int = 25) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=HEADERS)
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def _compact_tree(value: Any, depth: int = 0) -> Any:
    """Prune ESPN records to useful fields and limit nested list sizes."""
    if depth > 5:
        return None
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key not in TREE_KEYS:
                continue
            compacted = _compact_tree(item, depth + 1)
            if compacted not in (None, {}, []):
                result[key] = compacted
        return result
    if isinstance(value, list):
        return [
            item for item in (
                _compact_tree(entry, depth + 1) for entry in value[:8]
            )
            if item not in (None, {}, [])
        ]
    if isinstance(value, str):
        return value[:220]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:220]


INJURY_KEYS = {
    "name", "displayName", "fullName", "shortDisplayName",
    "abbreviation", "position", "team", "athlete", "player",
    "status", "injuryStatus", "type", "description", "details",
    "date", "startDate", "endDate", "displayValue", "value",
}


def _compact_injuries(value: Any, depth: int = 0) -> Any:
    """Keep injury-relevant fields while dropping IDs and redundant metadata."""
    if depth > 5:
        return None
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key not in INJURY_KEYS:
                continue
            compacted = _compact_injuries(item, depth + 1)
            if compacted not in (None, {}, []):
                result[key] = compacted
        return result
    if isinstance(value, list):
        return [
            item for item in (
                _compact_injuries(entry, depth + 1) for entry in value[:20]
            )
            if item not in (None, {}, [])
        ]
    if isinstance(value, str):
        return value[:120]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:120]



@lru_cache(maxsize=32)
def _core_team_injuries(team_id: str) -> list[dict[str, Any]]:
    """Fetch current non-active ESPN injury records, including injured reserve."""
    base = (
        "https://sports.core.api.espn.com/v2/sports/football/"
        f"leagues/nfl/teams/{team_id}/injuries?limit=100"
    )
    result = _get_json(base, timeout=15)
    injuries = []
    seen_players = set()

    # ESPN returns newest reports first. Bound detail lookups per team while searching beyond the newest handful of
    # records; important longer-term designations can otherwise be missed.
    # The game-summary injury feed is retained separately as supplemental evidence.
    for item in result.get("items", [])[:50]:
        ref = item.get("$ref")
        if not ref:
            continue

        try:
            record = _get_json(ref, timeout=15)
            status = str(record.get("status") or "").strip()
            if not status or status.casefold() == "active":
                continue

            athlete_ref = (record.get("athlete") or {}).get("$ref")
            if not athlete_ref:
                continue

            athlete = _get_json(athlete_ref, timeout=15)
            name = athlete.get("displayName") or athlete.get("fullName")
            if not name or name in seen_players:
                continue

            position = (athlete.get("position") or {}).get("abbreviation", "")
            details = record.get("details") or {}
            if isinstance(details, dict):
                details = {
                    key: details[key]
                    for key in ("type", "location", "detail", "side", "returnDate")
                    if details.get(key) is not None
                }
            else:
                details = {}

            comment = str(record.get("shortComment") or "")[:180]
            practice_status_conflict = (
                status.casefold() != "active"
                and "full participant" in comment.casefold()
            )

            injury_entry = {
                "player": name,
                "position": position,
                "status": status,
                "report_date": record.get("date"),
                "comment": comment,
                "injury_details": details,
                "practice_status_conflict": practice_status_conflict,
            }

            injuries.append(injury_entry)
            seen_players.add(name)

        except Exception:
            # One malformed or unavailable record should not discard the team feed.
            continue

    return injuries


def _compact_header(data: dict[str, Any]) -> dict[str, Any]:
    header = data.get("header", {})
    result = {
        key: header[key]
        for key in ("id", "season", "week")
        if key in header
    }
    competitions = []
    for competition in header.get("competitions", [])[:1]:
        item = {
            key: competition[key]
            for key in ("date", "neutralSite")
            if key in competition
        }
        status = competition.get("status", {})
        if status:
            item["status"] = _compact_tree(status)
        item["competitors"] = []
        for competitor in competition.get("competitors", []):
            team = competitor.get("team", {})
            item["competitors"].append({
                "team": {
                    key: team[key]
                    for key in ("id", "displayName", "abbreviation")
                    if key in team
                },
                **{
                    key: competitor[key]
                    for key in ("homeAway", "score", "winner")
                    if key in competitor
                },
            })
        competitions.append(item)
    if competitions:
        result["competitions"] = competitions
    return result


def _compact_game_info(data: dict[str, Any]) -> dict[str, Any]:
    info = data.get("gameInfo", {})
    result = {}
    venue = info.get("venue")
    weather = info.get("weather")
    if venue:
        result["venue"] = _compact_tree(venue)
    if weather:
        result["weather"] = _compact_tree(weather)
    return result


def _compact_pickcenter(data: dict[str, Any], retrieved_at: str) -> list[dict[str, Any]]:
    def compact_market(market: Any) -> dict[str, Any]:
        result = {}
        if not isinstance(market, dict):
            return result
        for side, side_data in market.items():
            if not isinstance(side_data, dict):
                continue
            side_result = {}
            for phase in ("open", "close"):
                quote = side_data.get(phase)
                if not isinstance(quote, dict):
                    continue
                values = {
                    key: quote[key]
                    for key in ("line", "odds")
                    if quote.get(key) is not None
                }
                if values:
                    side_result[phase] = values
            if side_result:
                result[side] = side_result
        return result

    quotes = []
    for quote in data.get("pickcenter", [])[:3]:
        provider = quote.get("provider") or {}
        item = {
            "provider": provider.get("name", "Unknown"),
            "retrieved_at_utc": retrieved_at,
        }

        for source_key, output_key in (
            ("pointSpread", "spread"),
            ("total", "total"),
            ("moneyline", "moneyline"),
        ):
            compacted = compact_market(quote.get(source_key))
            if compacted:
                item[output_key] = compacted

        # Keep ESPN's displayed snapshot as a fallback when open/close
        # breakdowns are unavailable.
        fallback = {
            "displayed_spread": quote.get("spread"),
            "displayed_total": quote.get("overUnder"),
            "displayed_over_odds": quote.get("overOdds"),
            "displayed_under_odds": quote.get("underOdds"),
        }
        item.update({
            key: value for key, value in fallback.items()
            if value is not None
        })
        quotes.append(item)
    return quotes


def _compact_team_stats(data: dict[str, Any]) -> dict[str, Any]:
    root = data.get("results", {}).get("stats", {})
    result = {
        "team": root.get("displayName") or root.get("name"),
        "categories": {},
    }
    for category in root.get("categories", []):
        category_name = category.get("name", "")
        allowed = STAT_FIELDS.get(category_name)
        if not allowed:
            continue
        selected = []
        for stat in category.get("stats", []):
            if stat.get("name") in allowed:
                selected.append({
                    "name": stat.get("name"),
                    "value": stat.get("displayValue", stat.get("value")),
                })
        if selected:
            result["categories"][category_name] = selected
    return result


def _compact_summary(data: dict[str, Any], retrieved_at: str) -> str:
    selected: dict[str, Any] = {
        "retrieved_at_utc": retrieved_at,
        "header": _compact_header(data),
        "game_info": _compact_game_info(data),
        "pickcenter_markets": _compact_pickcenter(data, retrieved_at),
        "against_the_spread": _compact_tree(data.get("againstTheSpread", [])),
        "recent_games": _compact_tree(data.get("lastFiveGames", [])),
    }
    selected = {
        key: value for key, value in selected.items()
        if value not in (None, {}, [])
    }

    # A pregame boxscore is generally empty or not useful. Include it only
    # after the event has started or finished.
    state = ""
    competitions = data.get("header", {}).get("competitions", [])
    if competitions:
        state = (
            competitions[0].get("status", {})
            .get("type", {}).get("state", "")
        )
    if state in {"in", "post"} and data.get("boxscore"):
        selected["boxscore"] = _compact_tree(data["boxscore"])

    # Keep high-value matchup/market context while bounding bulky historical
    # trees. Injury feeds and team statistics are appended as separate sections,
    # so they are not sacrificed when recent-game payloads are unusually large.
    omitted = []
    def serialize() -> str:
        return json.dumps(selected, ensure_ascii=False, separators=(",", ":"))

    if len(serialize()) > SUMMARY_MAX_CHARS and "recent_games" in selected:
        selected.pop("recent_games")
        omitted.append("recent_games")
    if len(serialize()) > 12000 and "against_the_spread" in selected:
        selected.pop("against_the_spread")
        omitted.append("against_the_spread")
    if len(serialize()) > 12000 and "boxscore" in selected:
        selected.pop("boxscore")
        omitted.append("boxscore")
    if omitted:
        selected["omitted_oversized_sections"] = omitted

    return serialize()



def _extract_roster_players(payload: Any) -> list[dict[str, str]]:
    """Extract named roster athletes with ESPN IDs and position abbreviations."""
    players: list[dict[str, str]] = []
    seen: set[str] = set()

    def walk(value: Any, inherited_position: str = "") -> None:
        if isinstance(value, dict):
            raw_position = value.get("position", inherited_position)
            if isinstance(raw_position, dict):
                position = str(raw_position.get("abbreviation") or raw_position.get("name") or "")
            else:
                position = str(raw_position or "")
            position = {
                "QUARTERBACK": "QB",
                "RUNNING BACK": "RB",
                "WIDE RECEIVER": "WR",
                "TIGHT END": "TE",
            }.get(position.strip().upper(), position.strip().upper())
            name = value.get("fullName") or value.get("displayName")
            athlete_id = value.get("id")
            if name and position:
                key = str(athlete_id or str(name).strip().casefold())
                if key not in seen:
                    seen.add(key)
                    players.append({
                        "id": str(athlete_id or ""),
                        "name": str(name).strip(),
                        "position": position.upper(),
                    })
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child, position)
        elif isinstance(value, list):
            for child in value:
                walk(child, inherited_position)

    walk(payload)
    return players


def _extract_roster_names(payload: Any) -> list[str]:
    """Extract roster names even when a partial roster response omits athlete IDs."""
    names: list[str] = []
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            name = value.get("fullName") or value.get("displayName")
            if name and isinstance(value.get("position"), (dict, str)):
                cleaned = str(name).strip()
                if cleaned and cleaned.casefold() not in seen:
                    seen.add(cleaned.casefold())
                    names.append(cleaned)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return names


@lru_cache(maxsize=32)
def _team_roster_players(team_id: str) -> tuple[dict[str, str], ...]:
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
        f"teams/{team_id}/roster"
    )
    return tuple(_extract_roster_players(_get_json(url, timeout=15)))


@lru_cache(maxsize=32)
def _team_depth_chart(team_id: str) -> dict[str, Any]:
    """Fetch the team's published depth chart once per run."""
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
        f"teams/{team_id}/depthcharts"
    )
    return _get_json(url, timeout=15)


def _extract_depth_chart_ids(payload: Any) -> dict[str, list[str]]:
    """Return ESPN depth-chart athlete IDs ordered by listed rank."""
    result: dict[str, list[tuple[int, str]]] = {}
    charts = payload.get("depthCharts", []) if isinstance(payload, dict) else []
    for chart in charts:
        positions = chart.get("positions", {}) if isinstance(chart, dict) else {}
        for position_data in positions.values():
            if not isinstance(position_data, dict):
                continue
            position = position_data.get("position") or {}
            abbreviation = (
                position.get("abbreviation", "")
                if isinstance(position, dict) else ""
            )
            abbreviation = str(abbreviation).upper()
            if abbreviation not in {"QB", "RB", "WR", "TE"}:
                continue
            for item in position_data.get("athletes", []):
                athlete = item.get("athlete") or {}
                athlete_id = str(athlete.get("id") or "")
                if athlete_id:
                    try:
                        rank = int(item.get("rank", 999))
                    except (TypeError, ValueError):
                        rank = 999
                    result.setdefault(abbreviation, []).append((rank, athlete_id))
    return {
        position: [athlete_id for _, athlete_id in sorted(entries)]
        for position, entries in result.items()
    }


def _normalize_player_gamelog(payload: dict[str, Any], recent_limit: int = 5) -> dict[str, Any]:
    """Join ESPN's split stat arrays to event metadata and emit concise player evidence."""
    names = payload.get("names", [])
    labels = payload.get("labels", [])
    display_names = payload.get("displayNames", [])
    definitions = []
    for index, name in enumerate(names):
        definitions.append({
            "name": str(name),
            "label": str(labels[index]) if index < len(labels) else str(name),
            "display": str(display_names[index]) if index < len(display_names) else str(name),
        })

    event_metadata = payload.get("events", {})
    if not isinstance(event_metadata, dict):
        event_metadata = {}
    season_totals = {}
    recent_games = []
    season_types = payload.get("seasonTypes", [])
    for season_type in season_types if isinstance(season_types, list) else []:
        for category in season_type.get("categories", []):
            if not isinstance(category, dict):
                continue
            totals = category.get("totals", [])
            if totals:
                season_totals[category.get("displayName", "Season totals")] = {
                    definitions[i]["name"]: totals[i]
                    for i in range(min(len(totals), len(definitions)))
                }
            for event_stats in category.get("events", []):
                if not isinstance(event_stats, dict):
                    continue
                event_id = str(event_stats.get("eventId", ""))
                metadata = event_metadata.get(event_id, {})
                values = event_stats.get("stats", [])
                stats = {
                    definitions[i]["name"]: values[i]
                    for i in range(min(len(values), len(definitions)))
                }
                opponent = metadata.get("opponent") or {}
                recent_games.append({
                    "week": metadata.get("week"),
                    "date": metadata.get("gameDate"),
                    "opponent": opponent.get("displayName"),
                    "home_away": metadata.get("atVs"),
                    "result": metadata.get("gameResult"),
                    "score": metadata.get("score"),
                    "stats": stats,
                })
    recent_games.sort(key=lambda game: str(game.get("date") or ""), reverse=True)
    if recent_games or season_totals:
        return {
            "stat_definitions": definitions,
            "season_totals": season_totals,
            "recent_games": recent_games[:recent_limit],
            "games_available": len(recent_games),
        }

    # Retain compatibility with incomplete/unexpected ESPN payloads, but keep bounded.
    return _compact_player_data(payload)


@lru_cache(maxsize=128)
def _player_gamelog(athlete_id: str, season: int) -> dict[str, Any]:
    """Fetch one player's current-season game log; shared across matchup sheets."""
    url = (
        "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/"
        f"athletes/{athlete_id}/gamelog?season={season}&seasontype=2"
    )
    return _normalize_player_gamelog(_get_json(url, timeout=15))


def _compact_player_data(value: Any, depth: int = 0) -> Any:
    """Bound player game-log payloads while retaining labels, dates, opponents and stats."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:45]:
            if key.casefold() in {"links", "images", "videos", "news", "headlines", "notes"}:
                continue
            compacted = _compact_player_data(item, depth + 1)
            if compacted not in (None, {}, []):
                result[str(key)[:60]] = compacted
        return result
    if isinstance(value, list):
        return [
            item for item in (
                _compact_player_data(entry, depth + 1) for entry in value[:6]
            )
            if item not in (None, {}, [])
        ]
    if isinstance(value, str):
        return value[:140]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:140]


def _select_research_players(
    players: list[dict[str, str]],
    depth_chart: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Research primary and secondary offensive contributors, not just one starter."""
    roster_by_id = {player.get("id"): player for player in players if player.get("id")}
    depth_ids = _extract_depth_chart_ids(depth_chart or {})
    # Secondary backs and receivers can lead their team in actual usage or scoring.
    # Use depth-chart order first, then fill any missing slots from the current roster.
    limits = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
    selected: list[dict[str, str]] = []
    selected_ids: set[str] = set()
    for position, limit in limits.items():
        added = 0
        for athlete_id in depth_ids.get(position, []):
            player = roster_by_id.get(athlete_id)
            if player is not None and athlete_id not in selected_ids:
                selected.append(player)
                selected_ids.add(athlete_id)
                added += 1
                if added >= limit:
                    break
        if added < limit:
            for player in players:
                athlete_id = str(player.get("id") or "")
                if (
                    player.get("position", "").upper() == position
                    and athlete_id
                    and athlete_id not in selected_ids
                ):
                    selected.append(player)
                    selected_ids.add(athlete_id)
                    added += 1
                    if added >= limit:
                        break
    return selected

def _compact_player_gamelog(
    gamelog: dict[str, Any],
    position: str,
    recent_limit: int = 3,
) -> dict[str, Any]:
    """Keep only role-relevant volume/production stats and recent game evidence."""
    stat_keys = {
        "QB": (
            "passingAttempts", "passingYards", "passingTouchdowns", "interceptions",
            "rushingAttempts", "rushingYards", "rushingTouchdowns",
        ),
        "RB": (
            "rushingAttempts", "rushingYards", "rushingTouchdowns",
            "receivingTargets", "receptions", "receivingYards", "receivingTouchdowns",
        ),
        "WR": (
            "receivingTargets", "receptions", "receivingYards", "receivingTouchdowns",
            "rushingAttempts", "rushingYards",
        ),
        "TE": (
            "receivingTargets", "receptions", "receivingYards", "receivingTouchdowns",
        ),
    }.get(position.upper(), (
        "rushingAttempts", "rushingYards", "rushingTouchdowns",
        "receivingTargets", "receptions", "receivingYards", "receivingTouchdowns",
    ))

    totals_by_category = gamelog.get("season_totals", {})
    totals = next(iter(totals_by_category.values()), {}) if isinstance(totals_by_category, dict) else {}
    usage = {key: totals.get(key) for key in stat_keys if key in totals}
    def as_number(value: Any) -> float:
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return 0.0

    opportunities = as_number(totals.get("passingAttempts")) if position.upper() == "QB" else (
        as_number(totals.get("rushingAttempts")) + as_number(totals.get("receivingTargets"))
    )
    games_available = int(gamelog.get("games_available") or 0)
    if not games_available:
        status = "no_game_log"
    elif opportunities >= 20:
        status = "established_volume"
    elif opportunities > 0:
        status = "some_recorded_usage"
    else:
        status = "no_recorded_usage"

    recent = []
    for game in gamelog.get("recent_games", [])[:recent_limit]:
        stats = game.get("stats", {})
        recent.append({
            key: game.get(key)
            for key in ("week", "date", "opponent", "home_away", "result", "score")
            if game.get(key) is not None
        } | {
            "stats": {key: stats[key] for key in stat_keys if key in stats}
        })
    return {
        "games_available": games_available,
        "usage_status": status,
        "season": usage,
        "recent": recent,
    }


@lru_cache(maxsize=32)
def _team_statistics(team_id: str) -> dict[str, Any]:
    """Fetch and compact team statistics once per team during a slate run."""
    endpoint_url = (
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
        f"teams/{team_id}/statistics"
    )
    return _compact_team_stats(_get_json(endpoint_url, timeout=15))


def reset_research_cache() -> None:
    """Clear per-slate ESPN caches so a later run never reuses stale data."""
    global _player_gamelog_failures, _player_gamelogs_disabled
    _team_statistics.cache_clear()
    _team_roster_players.cache_clear()
    _team_depth_chart.cache_clear()
    _player_gamelog.cache_clear()
    _core_team_injuries.cache_clear()
    _player_gamelog_failures = 0
    _player_gamelogs_disabled = False

def collect_game_research(game: dict[str, Any]) -> dict[str, Any]:
    event_id = str(game.get("event_id", "")).strip()
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sources = [{
        "title": "ESPN NFL scoreboard",
        "url": "https://www.espn.com/nfl/scoreboard",
    }]
    sections = [
        "INDEPENDENT LIVE NFL RESEARCH",
        f"Matchup: {game.get('Away Team', 'Away')} at {game.get('Home Team', 'Home')}",
        f"Kickoff (ET): {game.get('Game Date (ET)', 'Unknown')}",
        f"Scoreboard line: {game.get('Betting Line', 'Unavailable')}",
        f"Research retrieved (UTC): {retrieved_at}",
    ]

    sections.append(
        "MARKET DATA RULE: Pickcenter quotes are ESPN-displayed sportsbook snapshots. "
        "Use the named provider and retrieval timestamp. Do not describe them as guaranteed "
        "current prices. If no pickcenter quote is supplied, market prices are unverified. "
        "No player-prop market is claimed unless an actual prop line and price appear in the evidence."
    )

    team_stat_sections: list[str] = []
    player_stat_sections: list[str] = []
    injury_sections: list[str] = []
    injury_statuses: list[dict[str, Any]] = []
    rosters: dict[str, list[str]] = {}

    if not event_id:
        sections.append("No ESPN event ID was supplied; only scoreboard context is available.")
    else:
        api_url = ESPN_SUMMARY.format(event_id=event_id)
        sources.insert(0, {
            "title": "ESPN matchup game page",
            "url": f"https://www.espn.com/nfl/game/_/gameId/{event_id}",
        })
        try:
            data = _get_json(api_url)
            compact = _compact_summary(data, retrieved_at)
            sections.extend([
                "ESPN MATCHUP, PLAYER, INJURY, MARKET AND RECENT-GAME DATA:",
                compact,
            ])
            sources.append({"title": "ESPN game summary data", "url": api_url})

            teams = {
                str(game.get("away_team_id", "")): game.get("Away Team", "Away team"),
                str(game.get("home_team_id", "")): game.get("Home Team", "Home team"),
            }
            teams.pop("", None)
            for competition in data.get("header", {}).get("competitions", []):
                for competitor in competition.get("competitors", []):
                    team = competitor.get("team") or {}
                    if team.get("id"):
                        teams[str(team["id"])] = team.get("displayName", "Team")

            for team_id, team_name in teams.items():
                try:
                    roster_players = list(_team_roster_players(team_id))
                    roster_names = [player["name"] for player in roster_players]
                    if roster_names:
                        rosters[team_name] = roster_names
                        roster_url = (
                            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                            f"teams/{team_id}/roster"
                        )
                        sources.append({
                            "title": f"ESPN current roster — {team_name}",
                            "url": roster_url,
                        })
                        sections.append(
                            f"CURRENT ESPN ACTIVE TEAM ROSTER — {team_name}: "
                            + ", ".join(roster_names)
                        )
                        try:
                            depth_chart = _team_depth_chart(team_id)
                            depth_chart_url = (
                                "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                                f"teams/{team_id}/depthcharts"
                            )
                            sources.append({
                                "title": f"ESPN offensive depth chart — {team_name}",
                                "url": depth_chart_url,
                            })
                            selected_players = _select_research_players(
                                roster_players, depth_chart
                            )
                            selection_basis = "ESPN depth-chart candidates; usage status assessed from game logs"
                        except Exception as exc:
                            selected_players = _select_research_players(roster_players)
                            selection_basis = (
                                f"roster-order fallback candidates; depth chart unavailable "
                                f"({type(exc).__name__})"
                            )
                        player_records = []
                        for player in selected_players:
                            global _player_gamelog_failures, _player_gamelogs_disabled
                            if _player_gamelogs_disabled:
                                player_records.append({
                                    "player": player["name"],
                                    "position": player["position"],
                                    "game_log_unavailable": "skipped after repeated ESPN player-log errors",
                                })
                                continue
                            gamelog_url = (
                                "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/"
                                f"athletes/{player['id']}/gamelog?season={datetime.now(timezone.utc).year}&seasontype=2"
                            )
                            try:
                                gamelog = _player_gamelog(
                                    player["id"], datetime.now(timezone.utc).year
                                )
                                player_records.append({
                                    "player": player["name"],
                                    "position": player["position"],
                                    "usage_and_recent_stats": _compact_player_gamelog(gamelog, player["position"]),
                                })
                                _player_gamelog_failures = 0
                                sources.append({
                                    "title": f"ESPN player game log — {player['name']}",
                                    "url": gamelog_url,
                                })
                            except Exception as exc:
                                _player_gamelog_failures += 1
                                if _player_gamelog_failures >= PLAYER_GAMELOG_FAILURE_LIMIT:
                                    _player_gamelogs_disabled = True
                                player_records.append({
                                    "player": player["name"],
                                    "position": player["position"],
                                    "game_log_unavailable": type(exc).__name__,
                                })
                        if player_records:
                            player_stat_sections.append(
                                f"ESPN CURRENT-SEASON PLAYER GAME LOGS — {team_name} "
                                f"(selected by {selection_basis}; retrieved {retrieved_at}; regular season): "
                                + json.dumps(
                                    player_records,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                            )
                    else:
                        sections.append(f"ESPN roster returned no player names for {team_name}.")
                except Exception as exc:
                    sections.append(
                        f"ESPN roster unavailable for {team_name}: {type(exc).__name__}"
                    )

            for team_id, team_name in teams.items():
                endpoint_name = "statistics"
                endpoint_url = (
                    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                    f"teams/{team_id}/{endpoint_name}"
                )
                try:
                    compact_team_data = _team_statistics(team_id)
                    team_stat_sections.append(
                        f"ESPN CURRENT-SEASON TEAM STATISTICS — {team_name}: "
                        + json.dumps(
                            compact_team_data,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    sources.append({
                        "title": f"ESPN {endpoint_name} — {team_name}",
                        "url": endpoint_url,
                    })
                except Exception as exc:
                    sections.append(
                        f"ESPN {endpoint_name} endpoint unavailable for {team_name}: {exc}"
                    )
            core_injuries_found = False
            # Core API includes current reports and older injured-reserve records.
            for team_id, team_name in teams.items():
                core_url = (
                    "https://sports.core.api.espn.com/v2/sports/football/"
                    f"leagues/nfl/teams/{team_id}/injuries?limit=100"
                )
                try:
                    team_injuries = _core_team_injuries(team_id)
                    if team_injuries:
                        core_injuries_found = True
                        for report in team_injuries:
                            if isinstance(report, dict) and report.get("player") and report.get("status"):
                                injury_statuses.append({
                                    "player": str(report["player"]),
                                    "team": str(team_name),
                                    "status": str(report["status"]),
                                    "report_date": str(report.get("report_date") or ""),
                                    "comment": str(report.get("comment") or ""),
                                })
                        injury_sections.append(
                            f"ESPN CURRENT PLAYER INJURIES — {team_name}: "
                            + json.dumps(
                                team_injuries,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        )
                    sources.append({
                        "title": f"ESPN Core API player injuries — {team_name}",
                        "url": core_url,
                    })
                except Exception as exc:
                    sections.append(
                        f"ESPN Core API injury data unavailable for {team_name}: "
                        f"{type(exc).__name__}"
                    )
            # Keep game-summary injuries as supplemental evidence even when
            # the Core API returns injuries for only one of the two teams.
            summary_injuries = _compact_injuries(data.get("injuries", []))
            if summary_injuries:
                injury_sections.append(
                    "ESPN GAME SUMMARY INJURY DATA: "
                    + json.dumps(
                        summary_injuries,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )

        except Exception as exc:
            sections.append(f"ESPN game-summary request failed: {exc}")

    # Injury evidence is more time-sensitive than season aggregates. Place it
    # before team statistics because the AI prompt has a hard research-text ceiling.
    sections.extend(injury_sections)
    sections.extend(player_stat_sections)
    sections.extend(team_stat_sections)

    seen, unique = set(), []
    for source in sources:
        if source["url"] not in seen:
            seen.add(source["url"])
            unique.append(source)

    return {
        "research_text": "\n\n".join(sections),
        "sources": unique,
        "rosters": rosters,
        "injury_statuses": injury_statuses,
    }

    """Fetch one player's current-season game log; shared across matchup sheets."""
    url = (
        "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/"
        f"athletes/{athlete_id}/gamelog?season={season}&seasontype=2"
    )
    return _compact_player_data(_get_json(url, timeout=15))


def _compact_player_data(value: Any, depth: int = 0) -> Any:
    """Bound player game-log payloads while retaining labels, dates, opponents and stats."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:45]:
            if key.casefold() in {"links", "images", "videos", "news", "headlines", "notes"}:
                continue
            compacted = _compact_player_data(item, depth + 1)
            if compacted not in (None, {}, []):
                result[str(key)[:60]] = compacted
        return result
    if isinstance(value, list):
        return [
            item for item in (
                _compact_player_data(entry, depth + 1) for entry in value[:6]
            )
            if item not in (None, {}, [])
        ]
    if isinstance(value, str):
        return value[:140]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:140]


@lru_cache(maxsize=32)
def _team_statistics(team_id: str) -> dict[str, Any]:
    """Fetch and compact team statistics once per team during a slate run."""
    endpoint_url = (
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
        f"teams/{team_id}/statistics"
    )
    return _compact_team_stats(_get_json(endpoint_url, timeout=15))


def reset_research_cache() -> None:
    """Clear per-slate ESPN caches so a later run never reuses stale data."""
    global _player_gamelog_failures, _player_gamelogs_disabled
    _team_statistics.cache_clear()
    _team_roster_players.cache_clear()
    _team_depth_chart.cache_clear()
    _player_gamelog.cache_clear()
    _core_team_injuries.cache_clear()
    _player_gamelog_failures = 0
    _player_gamelogs_disabled = False

def collect_game_research(game: dict[str, Any]) -> dict[str, Any]:
    event_id = str(game.get("event_id", "")).strip()
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sources = [{
        "title": "ESPN NFL scoreboard",
        "url": "https://www.espn.com/nfl/scoreboard",
    }]
    sections = [
        "INDEPENDENT LIVE NFL RESEARCH",
        f"Matchup: {game.get('Away Team', 'Away')} at {game.get('Home Team', 'Home')}",
        f"Kickoff (ET): {game.get('Game Date (ET)', 'Unknown')}",
        f"Scoreboard line: {game.get('Betting Line', 'Unavailable')}",
        f"Research retrieved (UTC): {retrieved_at}",
    ]

    sections.append(
        "MARKET DATA RULE: Pickcenter quotes are ESPN-displayed sportsbook snapshots. "
        "Use the named provider and retrieval timestamp. Do not describe them as guaranteed "
        "current prices. If no pickcenter quote is supplied, market prices are unverified. "
        "No player-prop market is claimed unless an actual prop line and price appear in the evidence."
    )

    team_stat_sections: list[str] = []
    player_stat_sections: list[str] = []
    injury_sections: list[str] = []
    injury_statuses: list[dict[str, Any]] = []
    rosters: dict[str, list[str]] = {}

    if not event_id:
        sections.append("No ESPN event ID was supplied; only scoreboard context is available.")
    else:
        api_url = ESPN_SUMMARY.format(event_id=event_id)
        sources.insert(0, {
            "title": "ESPN matchup game page",
            "url": f"https://www.espn.com/nfl/game/_/gameId/{event_id}",
        })
        try:
            data = _get_json(api_url)
            compact = _compact_summary(data, retrieved_at)
            sections.extend([
                "ESPN MATCHUP, PLAYER, INJURY, MARKET AND RECENT-GAME DATA:",
                compact,
            ])
            sources.append({"title": "ESPN game summary data", "url": api_url})

            teams = {
                str(game.get("away_team_id", "")): game.get("Away Team", "Away team"),
                str(game.get("home_team_id", "")): game.get("Home Team", "Home team"),
            }
            teams.pop("", None)
            for competition in data.get("header", {}).get("competitions", []):
                for competitor in competition.get("competitors", []):
                    team = competitor.get("team") or {}
                    if team.get("id"):
                        teams[str(team["id"])] = team.get("displayName", "Team")

            for team_id, team_name in teams.items():
                try:
                    roster_players = list(_team_roster_players(team_id))
                    roster_names = [player["name"] for player in roster_players]
                    if roster_names:
                        rosters[team_name] = roster_names
                        roster_url = (
                            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                            f"teams/{team_id}/roster"
                        )
                        sources.append({
                            "title": f"ESPN current roster — {team_name}",
                            "url": roster_url,
                        })
                        sections.append(
                            f"CURRENT ESPN ACTIVE TEAM ROSTER — {team_name}: "
                            + ", ".join(roster_names)
                        )
                        try:
                            depth_chart = _team_depth_chart(team_id)
                            depth_chart_url = (
                                "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                                f"teams/{team_id}/depthcharts"
                            )
                            sources.append({
                                "title": f"ESPN offensive depth chart — {team_name}",
                                "url": depth_chart_url,
                            })
                            selected_players = _select_research_players(
                                roster_players, depth_chart
                            )
                            selection_basis = "ESPN depth-chart No. 1 at position"
                        except Exception as exc:
                            selected_players = _select_research_players(roster_players)
                            selection_basis = (
                                f"roster-order fallback; depth chart unavailable "
                                f"({type(exc).__name__})"
                            )
                        player_records = []
                        for player in selected_players:
                            global _player_gamelog_failures, _player_gamelogs_disabled
                            if _player_gamelogs_disabled:
                                player_records.append({
                                    "player": player["name"],
                                    "position": player["position"],
                                    "game_log_unavailable": "skipped after repeated ESPN player-log errors",
                                })
                                continue
                            gamelog_url = (
                                "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/"
                                f"athletes/{player['id']}/gamelog?season={datetime.now(timezone.utc).year}&seasontype=2"
                            )
                            try:
                                gamelog = _player_gamelog(
                                    player["id"], datetime.now(timezone.utc).year
                                )
                                player_records.append({
                                    "player": player["name"],
                                    "position": player["position"],
                                    "season_game_log": gamelog,
                                })
                                _player_gamelog_failures = 0
                                sources.append({
                                    "title": f"ESPN player game log — {player['name']}",
                                    "url": gamelog_url,
                                })
                            except Exception as exc:
                                _player_gamelog_failures += 1
                                if _player_gamelog_failures >= PLAYER_GAMELOG_FAILURE_LIMIT:
                                    _player_gamelogs_disabled = True
                                player_records.append({
                                    "player": player["name"],
                                    "position": player["position"],
                                    "game_log_unavailable": type(exc).__name__,
                                })
                        if player_records:
                            player_stat_sections.append(
                                f"ESPN CURRENT-SEASON PLAYER GAME LOGS — {team_name} "
                                f"(selected by {selection_basis}; retrieved {retrieved_at}; regular season): "
                                + json.dumps(
                                    player_records,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                            )
                    else:
                        sections.append(f"ESPN roster returned no player names for {team_name}.")
                except Exception as exc:
                    sections.append(
                        f"ESPN roster unavailable for {team_name}: {type(exc).__name__}"
                    )

            for team_id, team_name in teams.items():
                endpoint_name = "statistics"
                endpoint_url = (
                    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                    f"teams/{team_id}/{endpoint_name}"
                )
                try:
                    compact_team_data = _team_statistics(team_id)
                    team_stat_sections.append(
                        f"ESPN CURRENT-SEASON TEAM STATISTICS — {team_name}: "
                        + json.dumps(
                            compact_team_data,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
                    sources.append({
                        "title": f"ESPN {endpoint_name} — {team_name}",
                        "url": endpoint_url,
                    })
                except Exception as exc:
                    sections.append(
                        f"ESPN {endpoint_name} endpoint unavailable for {team_name}: {exc}"
                    )
            core_injuries_found = False
            # Core API includes current reports and older injured-reserve records.
            for team_id, team_name in teams.items():
                core_url = (
                    "https://sports.core.api.espn.com/v2/sports/football/"
                    f"leagues/nfl/teams/{team_id}/injuries?limit=100"
                )
                try:
                    team_injuries = _core_team_injuries(team_id)
                    if team_injuries:
                        core_injuries_found = True
                        for report in team_injuries:
                            if isinstance(report, dict) and report.get("player") and report.get("status"):
                                injury_statuses.append({
                                    "player": str(report["player"]),
                                    "team": str(team_name),
                                    "status": str(report["status"]),
                                    "report_date": str(report.get("report_date") or ""),
                                    "comment": str(report.get("comment") or ""),
                                })
                        injury_sections.append(
                            f"ESPN CURRENT PLAYER INJURIES — {team_name}: "
                            + json.dumps(
                                team_injuries,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                        )
                    sources.append({
                        "title": f"ESPN Core API player injuries — {team_name}",
                        "url": core_url,
                    })
                except Exception as exc:
                    sections.append(
                        f"ESPN Core API injury data unavailable for {team_name}: "
                        f"{type(exc).__name__}"
                    )
            # Keep game-summary injuries as supplemental evidence even when
            # the Core API returns injuries for only one of the two teams.
            summary_injuries = _compact_injuries(data.get("injuries", []))
            if summary_injuries:
                injury_sections.append(
                    "ESPN GAME SUMMARY INJURY DATA: "
                    + json.dumps(
                        summary_injuries,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )

        except Exception as exc:
            sections.append(f"ESPN game-summary request failed: {exc}")

    # Injury evidence is more time-sensitive than season aggregates. Place it
    # before team statistics because the AI prompt has a hard research-text ceiling.
    sections.extend(injury_sections)
    sections.extend(player_stat_sections)
    sections.extend(team_stat_sections)

    seen, unique = set(), []
    for source in sources:
        if source["url"] not in seen:
            seen.add(source["url"])
            unique.append(source)

    return {
        "research_text": "\n\n".join(sections),
        "sources": unique,
        "rosters": rosters,
        "injury_statuses": injury_statuses,
    }
