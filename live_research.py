"""Independent NFL research collection; does not require an AI provider or API key."""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

import certifi

HEADERS = {"User-Agent": "Betting-Spreads/3.0 (independent NFL research)"}
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={event_id}"


def _get_json(url: str, timeout: int = 25) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=HEADERS)
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def _compact_summary(data: dict[str, Any]) -> str:
    """Keep matchup-relevant live fields while avoiding enormous raw API payloads."""
    selected = {}
    for key in ("header", "gameInfo", "weather", "odds", "pickcenter",
                "boxscore", "injuries", "leaders", "predictor", "rosters"):
        if key in data:
            selected[key] = data[key]
    text = json.dumps(selected or data, ensure_ascii=False, separators=(",", ":"))
    return text[:18000]


def collect_game_research(game: dict[str, Any]) -> dict[str, Any]:
    """Fetch current game-level ESPN details independently of the AI provider."""
    event_id = str(game.get("event_id", "")).strip()
    sources = []
    sections = [
        "INDEPENDENT LIVE NFL RESEARCH",
        f"Matchup: {game.get('Away Team', 'Away')} at {game.get('Home Team', 'Home')}",
        f"Kickoff (ET): {game.get('Game Date (ET', 'Unknown')}",
        f"Scoreboard line: {game.get('Betting Line', 'Unavailable')}",
    ]
    scoreboard_url = "https://www.espn.com/nfl/scoreboard"
    sources.append({"title": "ESPN NFL scoreboard", "url": scoreboard_url})

    if event_id:
        api_url = ESPN_SUMMARY.format(event_id=event_id)
        game_url = f"https://www.espn.com/nfl/game/_/gameId/{event_id}"
        try:
            data = _get_json(api_url)
            sections.append("ESPN LIVE GAME SUMMARY (retrieved independently of AI):")
            sections.append(_compact_summary(data))
            sources.insert(0, {"title": "ESPN matchup game page", "url": game_url})
            sources.append({"title": "ESPN game summary data", "url": api_url})
            for competition in (data.get("header", {}).get("competitions") or []):
                for competitor in competition.get("competitors") or []:
                    team = competitor.get("team") or {}
                    team_id = team.get("id")
                    if team_id:
                        injury_url = (
                            f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                            f"teams/{team_id}/injuries"
                        )
                        try:
                            injury_data = _get_json(injury_url, timeout=15)
                            sections.append(
                                f"ESPN CURRENT INJURY REPORT — {team.get('displayName', 'Team')}: "
                                + json.dumps(injury_data, ensure_ascii=False, separators=(",", ":"))[:4500]
                            )
                            sources.append({
                                "title": f"ESPN injury report — {team.get('displayName', 'team')}",
                                "url": injury_url,
                            })
                        except Exception as exc:
                            sections.append(
                                f"Injury endpoint unavailable for {team.get('displayName', 'team')}: {exc}"
                            )
        except Exception as exc:
            sections.append(f"ESPN game-summary request failed: {exc}")
            sources.append({"title": "ESPN NFL scoreboard fallback", "url": scoreboard_url})
    else:
        sections.append("No ESPN event ID was supplied; only scoreboard matchup information is available.")

    # Public dataset landing page is provided as a transparent historical-statistics source.
    sources.append({
        "title": "nflverse open NFL play-by-play and player statistics datasets",
        "url": "https://github.com/nflverse/nflverse-data",
    })
    sections.append(
        "Historical data reference: nflverse publishes open NFL play-by-play and player-statistics "
        "datasets. The landing page above is a source link, not a claim that its files were downloaded "
        "during this run."
    )
    return {
        "research_text": "\n\n".join(sections),
        "sources": _deduplicate_sources(sources),
    }


def _deduplicate_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    result = []
    for source in sources:
        url = source.get("url")
        if url and url not in seen:
            seen.add(url)
            result.append(source)
    return result
