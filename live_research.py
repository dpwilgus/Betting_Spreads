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

    # ESPN returns newest reports first. Bound detail lookups per team so a
    # large historical injury feed cannot trigger dozens of slow HTTP requests.
    # The game-summary injury feed is retained separately as supplemental evidence.
    for item in result.get("items", [])[:12]:
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

            comment = str(record.get("shortComment") or "")
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

    return json.dumps(selected, ensure_ascii=False, separators=(",", ":"))



@lru_cache(maxsize=32)
def _team_statistics(team_id: str) -> dict[str, Any]:
    """Fetch and compact team statistics once per team during a slate run."""
    endpoint_url = (
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
        f"teams/{team_id}/statistics"
    )
    return _compact_team_stats(_get_json(endpoint_url, timeout=15))

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
                endpoint_name = "statistics"
                endpoint_url = (
                    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                    f"teams/{team_id}/{endpoint_name}"
                )
                try:
                    compact_team_data = _team_statistics(team_id)
                    sections.append(
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
                        sections.append(
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
                sections.append(
                    "ESPN GAME SUMMARY INJURY DATA: "
                    + json.dumps(
                        summary_injuries,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )

        except Exception as exc:
            sections.append(f"ESPN game-summary request failed: {exc}")

    sections.append(
        "MARKET DATA RULE: Pickcenter quotes are ESPN-displayed sportsbook snapshots. "
        "Use the named provider and retrieval timestamp. Do not describe them as guaranteed "
        "current prices. If no pickcenter quote is supplied, market prices are unverified. "
        "No player-prop market is claimed unless an actual prop line and price appear in the evidence."
    )

    seen, unique = set(), []
    for source in sources:
        if source["url"] not in seen:
            seen.add(source["url"])
            unique.append(source)

    return {
        "research_text": "\n\n".join(sections),
        "sources": unique,
    }
