"""Compact independent NFL research collection; no AI provider required."""

from __future__ import annotations

import json
import re
from functools import lru_cache
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

import certifi

HEADERS = {"User-Agent": "Betting-Spreads/3.0 (independent NFL research)"}

SUMMARY_MAX_CHARS = 12000

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
        "interceptions", "passingYardsAllowed", "rushingYardsAllowed",
        "totalYardsAllowed", "yardsPerPlayAllowed", "pointsAllowed",
        "opponentPasserRating", "thirdDownPct", "redZonePct",
    },
    "defensiveInterceptions": {
        "interceptions", "interceptionTouchdowns",
    },
    "general": {
        "gamesPlayed", "fumblesAndLost",
    },
    "scoring": {
        "totalPoints", "totalTouchdowns", "pointsPerGame",
        "pointsFor", "pointsScored", "avgPointsFor",
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
                item = {
                    "name": stat.get("name"),
                    "value": stat.get("displayValue", stat.get("value")),
                }
                rank = stat.get("rankDisplayValue", stat.get("rank"))
                if rank not in (None, ""):
                    item["rank"] = rank
                selected.append(item)
        if selected:
            result["categories"][category_name] = selected

    # Derive a transparent team scoring average when ESPN supplies season points
    # and games played but omits a direct points-per-game field.
    general_stats = result["categories"].get("general", [])
    scoring_stats = result["categories"].get("scoring", [])
    games_played = next(
        (float(item["value"]) for item in general_stats
         if item.get("name") == "gamesPlayed" and item.get("value") not in (None, "")),
        0.0,
    )
    has_points_per_game = any(item.get("name") == "pointsPerGame" for item in scoring_stats)
    total_points = next(
        (float(item["value"]) for item in scoring_stats
         if item.get("name") == "totalPoints" and item.get("value") not in (None, "")),
        0.0,
    )
    if games_played > 0 and total_points > 0 and not has_points_per_game:
        result["categories"].setdefault("scoring", []).append({
            "name": "pointsPerGame",
            "value": round(total_points / games_played, 2),
        })
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
                "TIGHT END": "TE", "LINEBACKER": "LB", "DEFENSIVE END": "DE",
                "DEFENSIVE TACKLE": "DT", "CORNERBACK": "CB", "SAFETY": "S",
            }.get(position.strip().upper(), position.strip().upper())
            name = value.get("fullName") or value.get("displayName")
            athlete_id = value.get("id")
            placeholder_names = {
                "quarterback", "running back", "wide receiver", "tight end",
                "qb", "rb", "wr", "te", "receiver", "back", "linebacker",
                "defensive end", "defensive tackle", "cornerback", "safety",
            }
            if name and position and str(name).strip().casefold() not in placeholder_names:
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


def _fetch_player_gamelog(athlete_id: str, season: int) -> dict[str, Any]:
    """Retry one transient player-log failure once; never disable other players."""
    try:
        return _player_gamelog(athlete_id, season)
    except urllib.error.HTTPError:
        # A 4xx/5xx response is explicit; do not amplify provider throttling.
        raise
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, ValueError):
        time.sleep(0.35)
        return _player_gamelog(athlete_id, season)


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
    injury_reports: list[dict[str, Any]] | None = None,
    team_name: str | None = None,
) -> list[dict[str, str]]:
    """Select core offensive contributors plus depth-chart replacements for injured teammates."""
    placeholder_names = {
        "quarterback", "running back", "wide receiver", "tight end",
        "qb", "rb", "wr", "te", "receiver", "back", "offense",
    }
    valid_players = [
        player for player in players
        if player.get("id")
        and str(player.get("name") or "").strip().casefold() not in placeholder_names
        and player.get("position", "").upper() in {"QB", "RB", "WR", "TE"}
    ]
    roster_by_id = {str(player["id"]): player for player in valid_players}
    depth_ids = _extract_depth_chart_ids(depth_chart or {})
    position_ids: dict[str, list[str]] = {}
    for position in ("QB", "RB", "WR", "TE"):
        # Preserve depth-chart IDs for locating injured players who may be absent
        # from the active roster, but count only named roster athletes toward limits.
        ordered = list(depth_ids.get(position, []))
        seen = set(ordered)
        ordered.extend(
            str(player["id"]) for player in valid_players
            if player.get("position", "").upper() == position
            and str(player["id"]) not in seen
        )
        position_ids[position] = ordered

    # Broad enough to cover the main passing/rushing options, without querying
    # every inactive/developmental roster player for every game in a full slate.
    limits = {"QB": 2, "RB": 3, "WR": 5, "TE": 2}
    selected: list[dict[str, str]] = []
    selected_by_id: dict[str, dict[str, str]] = {}

    def normalize(value: Any) -> str:
        return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())

    unavailable_names = {
        normalize(report.get("player"))
        for report in injury_reports or []
        if isinstance(report, dict)
        and report.get("player")
        and any(
            term in str(report.get("status") or "").casefold()
            for term in ("out", "doubtful", "injured reserve", "ir", "pup", "nfi")
        )
    }

    def add_player(athlete_id: str, reason: str) -> None:
        player = roster_by_id.get(str(athlete_id))
        if player is None or normalize(player.get("name")) in unavailable_names:
            return
        existing = selected_by_id.get(str(athlete_id))
        if existing is not None:
            prior = existing.get("research_reason", "")
            if reason not in prior:
                existing["research_reason"] = "; ".join(filter(None, (prior, reason)))
            return
        candidate = dict(player)
        candidate["research_reason"] = reason
        selected_by_id[str(athlete_id)] = candidate
        selected.append(candidate)

    def find_depth_chart_id(value: Any, wanted_name: str) -> str | None:
        if isinstance(value, dict):
            athlete = value.get("athlete")
            if isinstance(athlete, dict):
                athlete_name = athlete.get("displayName") or athlete.get("fullName")
                if normalize(athlete_name) == wanted_name and athlete.get("id"):
                    return str(athlete["id"])
            for child in value.values():
                found = find_depth_chart_id(child, wanted_name)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find_depth_chart_id(child, wanted_name)
                if found:
                    return found
        return None

    for position, limit in limits.items():
        added = 0
        for athlete_id in position_ids[position]:
            if athlete_id not in roster_by_id:
                continue
            add_player(athlete_id, "Core depth-chart/roster candidate")
            added += 1
            if added >= limit:
                break

    # Injury beneficiaries are added after the normal starter/usage pool so a
    # third-string replacement (e.g. Gavin Bartholomew behind T.J. Hockenson)
    # cannot be crowded out by a fixed position cap.
    for report in injury_reports or []:
        if not isinstance(report, dict):
            continue
        report_team = normalize(report.get("team"))
        if team_name and report_team and report_team != normalize(team_name):
            continue
        status = str(report.get("status") or "").casefold()
        if not (
            any(term in status for term in ("out", "doubtful", "injured reserve", "ir", "pup", "nfi"))
            or "questionable" in status
        ):
            continue
        position = str(report.get("position") or "").upper()
        if position not in position_ids:
            continue

        injured_name = normalize(report.get("player"))
        injured_id = next(
            (athlete_id for athlete_id, player in roster_by_id.items()
             if normalize(player.get("name")) == injured_name),
            None,
        )
        if injured_id is None:
            injured_id = find_depth_chart_id(depth_chart or {}, injured_name)
        ordered_ids = position_ids[position]
        if injured_id not in ordered_ids:
            # Do not infer replacements from generic roster order. The injured
            # athlete must be explicitly present on this team's depth chart.
            continue
        injured_index = ordered_ids.index(injured_id)
        if injured_index > 1:
            # Only an injured starter or first backup creates a next-man-up pool.
            # Lower-depth injuries must not create cascading speculative chains.
            continue

        # Consider the next two listed options, but add_player excludes anyone
        # independently reported unavailable (Out/IR/PUP/NFI/Doubtful).
        for athlete_id in ordered_ids[injured_index + 1:injured_index + 3]:
            add_player(
                athlete_id,
                f"Potential replacement for {report.get('player')} ({report.get('status')})",
            )

    return selected


def _select_defensive_research_players(
    players: list[dict[str, str]],
    depth_chart: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Select published defensive starters at high-impact positions for individual stat research."""
    roster = {str(player.get("id")): player for player in players if player.get("id")}
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    priority = {"LB": 2, "ILB": 1, "OLB": 1, "DE": 1, "EDGE": 1,
                "DT": 1, "NT": 1, "CB": 1, "S": 1, "FS": 1, "SS": 1}
    charts = depth_chart.get("depthCharts", []) if isinstance(depth_chart, dict) else []
    for chart in charts:
        positions = chart.get("positions", {}) if isinstance(chart, dict) else {}
        for position_data in positions.values():
            if not isinstance(position_data, dict):
                continue
            position_obj = position_data.get("position") or {}
            pos = str(position_obj.get("abbreviation", "") if isinstance(position_obj, dict) else position_obj).upper()
            limit = priority.get(pos, 0)
            if not limit:
                continue
            athletes = sorted(
                position_data.get("athletes", []),
                key=lambda item: int(item.get("rank", 999)) if str(item.get("rank", "")).isdigit() else 999,
            )
            added = 0
            for item in athletes:
                athlete = item.get("athlete") or {}
                athlete_id = str(athlete.get("id") or "")
                player = roster.get(athlete_id)
                if not player or athlete_id in seen:
                    continue
                candidate = dict(player)
                candidate["research_reason"] = f"Defensive depth-chart starter/leader candidate ({pos})"
                selected.append(candidate)
                seen.add(athlete_id)
                added += 1
                if added >= limit:
                    break
    return selected



def _select_special_teams_research_players(
    players: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Include the active roster kicker and punter for their player-specific markets."""
    selected = []
    for position in ("K", "P"):
        player = next((item for item in players if item.get("position", "").upper() == position and item.get("id")), None)
        if player:
            candidate = dict(player)
            candidate["research_reason"] = f"Special-teams starter candidate ({position})"
            selected.append(candidate)
    return selected
def _compact_player_gamelog(
    gamelog: dict[str, Any],
    position: str,
    recent_limit: int = 3,
) -> dict[str, Any]:
    """Keep only role-relevant volume/production stats and recent game evidence."""
    stat_keys = {
        "QB": (
            "passingAttempts", "completions", "passingYards", "passingTouchdowns",
            "interceptions", "rushingAttempts", "rushingYards", "rushingTouchdowns",
            "longestPass", "sacksTaken",
        ),
        "DEF": (
            "totalTackles", "soloTackles", "assistedTackles", "sacks",
            "interceptions", "passesDefended", "tacklesForLoss", "quarterbackHits",
            "forcedFumbles", "fumbleRecoveries",
        ),
        "K": ("fieldGoalsMade", "fieldGoalsAttempted", "fieldGoalPct",
              "extraPointsMade", "extraPointsAttempted"),
        "P": ("punts", "puntingYards", "netPuntingYards", "longPunt"),
        "RB": (
            "rushingAttempts", "rushingYards", "rushingTouchdowns",
            "receivingTargets", "receptions", "receivingYards", "receivingTouchdowns",
            "longReception",
        ),
        "WR": (
            "receivingTargets", "receptions", "receivingYards", "receivingTouchdowns",
            "rushingAttempts", "rushingYards", "longReception",
        ),
        "TE": (
            "receivingTargets", "receptions", "receivingYards", "receivingTouchdowns",
            "longReception",
        ),
    }.get(position.upper(), (
        "totalTackles", "soloTackles", "assistedTackles", "sacks",
        "interceptions", "passesDefended", "tacklesForLoss", "quarterbackHits",
        "forcedFumbles", "fumbleRecoveries",
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

@lru_cache(maxsize=1)
def _adam_schefter_news() -> list[dict[str, Any]]:
    """Fetch Adam Schefter's ESPN contributor feed and normalize article records."""
    url = (
        "https://site.web.api.espn.com/apis/v2/flex?"
        "region=us&lang=en&contentorigin=espn&contributor=adam-schefter&"
        "limit=100&pubkey=contributor-page"
    )
    payload = _get_json(url, timeout=20)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            headline = value.get("headline") or value.get("title")
            published = value.get("published") or value.get("lastModified") or value.get("date")
            links = value.get("links") or {}
            web_link = links.get("web") if isinstance(links, dict) else {}
            href = web_link.get("href") if isinstance(web_link, dict) else ""
            href = href or value.get("url") or value.get("href") or ""
            if headline and published and href:
                key = str(href)
                if key not in seen:
                    seen.add(key)
                    found.append({
                        "headline": str(headline)[:220],
                        "description": str(value.get("description") or value.get("summary") or "")[:420],
                        "byline": "Adam Schefter",
                        "published": str(published),
                        "url": key,
                    })
            for child in value.values():
                if isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    found.sort(key=lambda item: str(item.get("published") or ""), reverse=True)
    return found[:100]


def _confirmed_starting_qb_news(
    player_name: str,
    articles: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find dated team-news evidence explicitly saying this QB will start."""
    for article in articles:
        if not isinstance(article, dict):
            continue
        headline = str(article.get("headline") or article.get("title") or "")
        description = str(article.get("description") or article.get("summary") or "")
        text = f"{headline} {description}"
        if not re.search(re.escape(player_name), text, flags=re.I):
            continue
        if re.search(
            r"\b(?:will start|to start|named (?:the )?starter|named .*starting quarterback|starting quarterback|starting QB|start Sunday|start this week)\b",
            text,
            flags=re.I,
        ):
            published = str(article.get("published") or article.get("lastModified") or "")
            if published:
                return {
                    "headline": headline,
                    "published": published,
                    "url": str(article.get("url") or ""),
                    "description": description,
                }
    return None


@lru_cache(maxsize=32)
def _team_news(team_id: str) -> dict[str, Any]:
    """Fetch the latest ESPN team news headlines for roster and quarterback developments."""
    url = (
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
        f"teams/{team_id}/news"
    )
    payload = _get_json(url, timeout=15)
    articles = payload.get("articles") or payload.get("news") or []
    compact = []
    for article in articles[:10]:
        if not isinstance(article, dict):
            continue
        headline = article.get("headline") or article.get("title")
        if not headline:
            continue
        links = article.get("links") or {}
        web_link = links.get("web") or {}
        href = web_link.get("href") if isinstance(web_link, dict) else ""
        compact.append({
            "headline": str(headline)[:220],
            "description": str(article.get("description") or article.get("summary") or "")[:420],
            "byline": str(article.get("byline") or article.get("author") or "")[:120],
            "published": article.get("published") or article.get("lastModified"),
            "url": href,
        })
    return {"articles": compact}



def reset_research_cache() -> None:
    """Clear per-slate ESPN caches so a later run never reuses stale data."""
    _team_statistics.cache_clear()
    _team_news.cache_clear()
    _team_roster_players.cache_clear()
    _team_depth_chart.cache_clear()
    _player_gamelog.cache_clear()
    _core_team_injuries.cache_clear()

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
            verified_game_markets = _compact_pickcenter(data, retrieved_at)
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
                    roster_quarterbacks_by_team[team_name] = [
                        str(player.get("name") or "")
                        for player in roster_players
                        if str(player.get("position") or "").upper() == "QB"
                        and player.get("name")
                    ]
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
                            gamelog_url = (
                                "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/"
                                f"athletes/{player['id']}/gamelog?season={datetime.now(timezone.utc).year}&seasontype=2"
                            )
                            try:
                                gamelog = _fetch_player_gamelog(
                                    player["id"], datetime.now(timezone.utc).year
                                )
                                player_records.append({
                                    "player": player["name"],
                                    "position": player["position"],
                                    "research_reason": player.get("research_reason", ""),
                                    "usage_and_recent_stats": _compact_player_gamelog(
                                        gamelog, player["position"]
                                    ),
                                })
                                sources.append({
                                    "title": f"ESPN player game log — {player['name']}",
                                    "url": gamelog_url,
                                })
                            except Exception as exc:
                                player_records.append({
                                    "player": player["name"],
                                    "position": player["position"],
                                    "research_reason": player.get("research_reason", ""),
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

            sections.extend(news_sections)

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

    # Player usage is essential for prop/TD evaluation. Keep it ahead of injury
    # context so prompt trimming cannot silently remove one team's game logs.
    sections.extend(player_stat_sections)
    sections.extend(injury_sections)
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
    _team_statistics.cache_clear()
    _team_news.cache_clear()
    _team_roster_players.cache_clear()
    _team_depth_chart.cache_clear()
    _player_gamelog.cache_clear()
    _core_team_injuries.cache_clear()
    _adam_schefter_news.cache_clear()

def _compact_injury_prompt_reports(
    reports: list[dict[str, Any]],
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Keep concise, newest injury designations for the AI while retaining full statuses separately."""
    ordered = sorted(
        (item for item in reports if isinstance(item, dict)),
        key=lambda item: str(item.get("report_date") or ""),
        reverse=True,
    )
    compact = []
    for report in ordered[:limit]:
        details = report.get("injury_details") or {}
        compact.append({
            "player": report.get("player", ""),
            "position": report.get("position", ""),
            "status": report.get("status", ""),
            "report_date": report.get("report_date", ""),
            "injury": details.get("type", "") if isinstance(details, dict) else "",
            "comment": str(report.get("comment") or "")[:90],
        })
    return compact


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
    news_sections: list[str] = []
    injury_statuses: list[dict[str, Any]] = []
    rosters: dict[str, list[str]] = {}
    selected_player_candidates: dict[str, list[dict[str, str]]] = {}
    news_articles_by_team: dict[str, list[dict[str, Any]]] = {}
    roster_quarterbacks_by_team: dict[str, list[str]] = {}
    confirmed_starting_qbs: dict[str, str] = {}
    verified_game_markets: list[dict[str, Any]] = []

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
            verified_game_markets = _compact_pickcenter(data, retrieved_at)
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

            team_injuries_by_id: dict[str, list[dict[str, Any]]] = {}
            # Injury reports are fetched independently of roster availability;
            # an incomplete roster response must never suppress injury evidence.
            for team_id, team_name in teams.items():
                core_url = (
                    "https://sports.core.api.espn.com/v2/sports/football/"
                    f"leagues/nfl/teams/{team_id}/injuries?limit=100"
                )
                try:
                    team_injuries = _core_team_injuries(team_id)
                    team_injuries_by_id[team_id] = team_injuries
                    for report in team_injuries:
                        if isinstance(report, dict) and report.get("player") and report.get("status"):
                            injury_statuses.append({
                                "player": str(report["player"]),
                                "position": str(report.get("position") or ""),
                                "team": str(team_name),
                                "status": str(report["status"]),
                                "report_date": str(report.get("report_date") or ""),
                                "comment": str(report.get("comment") or ""),
                            })
                    if team_injuries:
                        injury_sections.append(
                            f"ESPN CURRENT PLAYER INJURIES — {team_name}: "
                            + json.dumps(
                                _compact_injury_prompt_reports(team_injuries),
                                ensure_ascii=False, separators=(",", ":"),
                            )
                        )
                except Exception as exc:
                    team_injuries_by_id[team_id] = []
                    sections.append(
                        f"ESPN Core API injury data unavailable for {team_name}: "
                        f"{type(exc).__name__}"
                    )
                sources.append({
                    "title": f"ESPN Core API player injuries — {team_name}",
                    "url": core_url,
                })

            for team_id, team_name in teams.items():
                try:
                    roster_players = list(_team_roster_players(team_id))
                    roster_names = [player["name"] for player in roster_players]
                    roster_quarterbacks_by_team[team_name] = [
                        str(player.get("name") or "")
                        for player in roster_players
                        if str(player.get("position") or "").upper() == "QB"
                        and player.get("name")
                    ]
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
                        team_injuries = team_injuries_by_id.get(team_id, [])
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
                                roster_players, depth_chart, team_injuries, team_name
                            )
                            defensive_players = _select_defensive_research_players(
                                roster_players, depth_chart
                            )
                            selected_ids = {str(player.get("id")) for player in selected_players}
                            selected_players.extend(
                                player for player in defensive_players
                                if str(player.get("id")) not in selected_ids
                            )
                            selection_basis = (
                                "offensive depth-chart/roster leaders, injury replacements, "
                                "and defensive starters at high-impact positions"
                            )
                        except Exception as exc:
                            selected_players = _select_research_players(
                                roster_players, injury_reports=team_injuries, team_name=team_name
                            )
                            selection_basis = (
                                f"roster-order offensive fallback; depth chart unavailable "
                                f"({type(exc).__name__}); defensive starters not selected"
                            )
                        selected_player_candidates[team_name] = [
                            {
                                "name": player["name"],
                                "position": player["position"],
                                "research_reason": player.get("research_reason", ""),
                            }
                            for player in selected_players
                        ]
                        player_records = []
                        for player in selected_players:
                            gamelog_url = (
                                "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/"
                                f"athletes/{player['id']}/gamelog?season={datetime.now(timezone.utc).year}&seasontype=2"
                            )
                            try:
                                gamelog = _fetch_player_gamelog(
                                    player["id"], datetime.now(timezone.utc).year
                                )
                                player_records.append({
                                    "player": player["name"],
                                    "position": player["position"],
                                    "research_reason": player.get("research_reason", ""),
                                    "usage_and_recent_stats": _compact_player_gamelog(
                                        gamelog, player["position"]
                                    ),
                                })
                                sources.append({
                                    "title": f"ESPN player game log — {player['name']}",
                                    "url": gamelog_url,
                                })
                            except Exception as exc:
                                player_records.append({
                                    "player": player["name"],
                                    "position": player["position"],
                                    "research_reason": player.get("research_reason", ""),
                                    "game_log_unavailable": type(exc).__name__,
                                })
                        if player_records:
                            # Keep each player's evidence as its own prompt section.
                            # This prevents one oversized team block from causing all
                            # players for that team to be dropped during prompt trimming.
                            for record in player_records:
                                player_stat_sections.append(
                                    f"ESPN CURRENT-SEASON PLAYER GAME LOGS — {team_name} "
                                    f"(selected by {selection_basis}; retrieved {retrieved_at}; regular season): "
                                    + json.dumps([record], ensure_ascii=False, separators=(",", ":"))
                                )
                    else:
                        sections.append(f"ESPN roster returned no player names for {team_name}.")
                except Exception as exc:
                    sections.append(
                        f"ESPN roster unavailable for {team_name}: {type(exc).__name__}"
                    )

            for team_id, team_name in teams.items():
                news_url = (
                    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                    f"teams/{team_id}/news"
                )
                try:
                    team_news = _team_news(team_id)
                    articles = team_news.get("articles", [])
                    news_articles_by_team[team_name] = articles if isinstance(articles, list) else []
                    if articles:
                        news_sections.append(
                            f"ESPN RECENT TEAM NEWS — {team_name}: "
                            + json.dumps(articles, ensure_ascii=False, separators=(",", ":"))
                        )
                        for article in articles:
                            if isinstance(article, dict) and article.get("url"):
                                sources.append({
                                    "title": str(article.get("headline") or "ESPN team news"),
                                    "url": str(article["url"]),
                                })
                    sources.append({
                        "title": f"ESPN recent team news — {team_name}",
                        "url": news_url,
                    })
                except Exception as exc:
                    sections.append(
                        f"ESPN recent team news unavailable for {team_name}: {type(exc).__name__}"
                    )

            # Adam Schefter's contributor feed is checked separately from team feeds.
            # His reporting is prioritized for selected players and the two matchup teams.
            schefter_feed_url = (
                "https://site.web.api.espn.com/apis/v2/flex?"
                "region=us&lang=en&contentorigin=espn&contributor=adam-schefter&"
                "limit=100&pubkey=contributor-page"
            )
            sources.append({"title": "Adam Schefter ESPN contributor feed", "url": schefter_feed_url})
            try:
                selected_names = {
                    str(candidate.get("name") or "").strip()
                    for candidates in selected_player_candidates.values()
                    for candidate in candidates
                    if isinstance(candidate, dict) and candidate.get("name")
                }
                player_terms = selected_names | {
                    name.split()[-1]
                    for name in selected_names if len(name.split()) > 1
                }
                team_terms = {
                    str(team_name).casefold()
                    for team_name in teams.values()
                } | {
                    str(team_name).split()[-1].casefold()
                    for team_name in teams.values() if str(team_name).split()
                } | {
                    str(team_name).split()[0].casefold()
                    for team_name in teams.values()
                    if len(str(team_name).split()) > 1
                    and str(team_name).split()[0].casefold() not in {"los", "new"}
                }
                schefter_articles = _adam_schefter_news()
                relevant_schefter = []
                for article in schefter_articles:
                    article_text = (
                        str(article.get("headline") or "") + " "
                        + str(article.get("description") or "")
                    ).casefold()
                    if any(name.casefold() in article_text for name in player_terms) or any(
                        term and re.search(r"\b" + re.escape(term) + r"\b", article_text)
                        for term in team_terms
                    ):
                        relevant_schefter.append(article)
                for article in relevant_schefter[:12]:
                    news_sections.append(
                        "ADAM SCHEFTER PRIORITY NEWS — "
                        + json.dumps(article, ensure_ascii=False, separators=(",", ":"))
                    )
                    sources.append({
                        "title": "Adam Schefter — " + str(article.get("headline") or "NFL report"),
                        "url": str(article.get("url") or ""),
                    })
                if not relevant_schefter:
                    sections.append(
                        "Adam Schefter contributor feed checked; no recent article matched the selected matchup players or teams."
                    )
            except Exception as exc:
                sections.append(f"Adam Schefter contributor feed unavailable: {type(exc).__name__}")

            # A newer, dated team announcement that a QB will start supersedes
            # an older injury-feed designation; preserve both sources in research.
            # Check every rostered quarterback, not only selected research
            # candidates: an older Out designation can exclude the QB from that
            # candidate list, which must not prevent newer starter news overriding it.
            for team_name, quarterbacks in roster_quarterbacks_by_team.items():
                for player_name in quarterbacks:
                    confirmation = _confirmed_starting_qb_news(
                        player_name, news_articles_by_team.get(team_name, [])
                    )
                    if not confirmation:
                        continue
                    matching_reports = [
                        item for item in injury_statuses
                        if item.get("team") == team_name
                        and str(item.get("player", "")).casefold() == player_name.casefold()
                    ]
                    if not matching_reports:
                        continue
                    news_date = confirmation["published"][:10]
                    superseded_reports = [
                        item for item in matching_reports
                        if str(item.get("report_date") or "")[:10]
                        and news_date >= str(item.get("report_date") or "")[:10]
                    ]
                    if not superseded_reports:
                        continue
                    # A feed can contain duplicate entries for the same player.
                    # Remove every dated entry superseded by the newer confirmation,
                    # rather than leaving a stale duplicate active downstream.
                    superseded_ids = {id(item) for item in superseded_reports}
                    injury_statuses[:] = [
                        item for item in injury_statuses if id(item) not in superseded_ids
                    ]
                    confirmed_starting_qbs[team_name] = player_name
                    latest_report = max(
                        superseded_reports,
                        key=lambda item: str(item.get("report_date") or ""),
                    )
                    news_sections.append(
                        f"CONFIRMED STARTING QB ROLE — {team_name}: {player_name} "
                        f"named to start in dated team news ({confirmation['published']}): "
                        f"{confirmation['headline']}. This supersedes the older "
                        f"{latest_report.get('status')} injury-feed entry dated "
                        f"{latest_report.get('report_date')}."
                    )

            if confirmed_starting_qbs:
                confirmed_names = {
                    player_name.casefold() for player_name in confirmed_starting_qbs.values()
                }
                for team_name, player_name in confirmed_starting_qbs.items():
                    for candidate in selected_player_candidates.get(team_name, []):
                        if f"potential replacement for {player_name}".casefold() in str(
                            candidate.get("research_reason") or ""
                        ).casefold():
                            candidate["research_reason"] = (
                                "Depth-chart candidate; older starter-out report superseded by dated team news"
                            )
                updated_player_sections = []
                for section in player_stat_sections:
                    prefix, separator, payload_text = section.partition(": ")
                    if not separator:
                        updated_player_sections.append(section)
                        continue
                    try:
                        records = json.loads(payload_text)
                    except (TypeError, ValueError):
                        updated_player_sections.append(section)
                        continue
                    if isinstance(records, list):
                        for record in records:
                            reason = str(record.get("research_reason") or "")
                            if any(
                                f"potential replacement for {name}".casefold() in reason.casefold()
                                for name in confirmed_starting_qbs.values()
                            ):
                                record["research_reason"] = (
                                    "Depth-chart candidate; older starter-out report superseded by dated team news"
                                )
                        updated_player_sections.append(
                            prefix + ": " + json.dumps(
                                records, ensure_ascii=False, separators=(",", ":")
                            )
                        )
                    else:
                        updated_player_sections.append(section)
                player_stat_sections = updated_player_sections

                filtered_injury_sections = []
                for section in injury_sections:
                    prefix, separator, payload_text = section.partition(": ")
                    if not separator:
                        filtered_injury_sections.append(section)
                        continue
                    try:
                        reports = json.loads(payload_text)
                    except (TypeError, ValueError):
                        filtered_injury_sections.append(section)
                        continue
                    if isinstance(reports, list):
                        reports = [
                            item for item in reports
                            if str(item.get("player") or "").casefold()
                            not in confirmed_names
                        ]
                        if reports:
                            filtered_injury_sections.append(
                                prefix + ": " + json.dumps(
                                    reports, ensure_ascii=False, separators=(",", ":")
                                )
                            )
                    else:
                        filtered_injury_sections.append(section)
                injury_sections = filtered_injury_sections

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
            # Keep game-summary injuries as supplemental evidence even when
            # the Core API returns injuries for only one of the two teams.
            summary_injuries = _compact_injuries(data.get("injuries", []))
            if confirmed_starting_qbs:
                confirmed_names = {
                    player_name.casefold() for player_name in confirmed_starting_qbs.values()
                }
                summary_injuries = [
                    item for item in summary_injuries
                    if not any(
                        player_name in json.dumps(item, ensure_ascii=False).casefold()
                        for player_name in confirmed_names
                    )
                ]
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

    # Player usage is essential for prop/TD evaluation. Keep it ahead of injury
    # context so prompt trimming cannot silently remove one team's game logs.
    sections.extend(player_stat_sections)
    sections.extend(injury_sections)
    sections.extend(news_sections)
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
        "selected_player_candidates": selected_player_candidates,
        "verified_game_markets": verified_game_markets,
    }
