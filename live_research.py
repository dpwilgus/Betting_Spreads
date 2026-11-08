"""Independent NFL research collection; no AI key or AI provider required."""
from __future__ import annotations

import json
import ssl
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
    selected = {key: data[key] for key in (
        "header", "gameInfo", "weather", "odds", "pickcenter",
        "boxscore", "injuries", "leaders", "predictor", "rosters"
    ) if key in data}
    return json.dumps(selected or data, ensure_ascii=False, separators=(",", ":"))[:18000]


def collect_game_research(game: dict[str, Any]) -> dict[str, Any]:
    event_id = str(game.get("event_id", "")).strip()
    sources = [{"title": "ESPN NFL scoreboard", "url": "https://www.espn.com/nfl/scoreboard"}]
    sections = [
        "INDEPENDENT LIVE NFL RESEARCH",
        f"Matchup: {game.get('Away Team', 'Away')} at {game.get('Home Team', 'Home')}",
        f"Kickoff (ET): {game.get('Game Date (ET)', 'Unknown')}",
        f"Scoreboard line: {game.get('Betting Line', 'Unavailable')}",
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
            sections.extend(["ESPN GAME SUMMARY (retrieved independently of AI):", _compact_summary(data)])
            sources.append({"title": "ESPN game summary data", "url": api_url})
            teams = {}
            for competition in data.get("header", {}).get("competitions", []):
                for competitor in competition.get("competitors", []):
                    team = competitor.get("team") or {}
                    if team.get("id"):
                        teams[str(team["id"])] = team.get("displayName", "Team")
            for team_id, team_name in teams.items():
                injury_url = (
                    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                    f"teams/{team_id}/injuries"
                )
                try:
                    injury_data = _get_json(injury_url, timeout=15)
                    sections.append(
                        f"ESPN INJURY REPORT — {team_name}: "
                        + json.dumps(injury_data, ensure_ascii=False, separators=(",", ":"))[:4500]
                    )
                    sources.append({"title": f"ESPN injury report — {team_name}", "url": injury_url})
                except Exception as exc:
                    sections.append(f"Injury endpoint unavailable for {team_name}: {exc}")
        except Exception as exc:
            sections.append(f"ESPN game-summary request failed: {exc}")

    sources.append({
        "title": "nflverse open NFL play-by-play and player statistics datasets",
        "url": "https://github.com/nflverse/nflverse-data",
    })
    sections.append(
        "nflverse is linked as an open historical data resource only; its datasets were not downloaded "
        "in this request. Do not claim its current-week data was queried."
    )
    seen, unique = set(), []
    for source in sources:
        if source["url"] not in seen:
            seen.add(source["url"])
            unique.append(source)
    return {"research_text": "\n\n".join(sections), "sources": unique}
