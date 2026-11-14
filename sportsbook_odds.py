"""The Odds API client: secure key storage, quota-aware caching, and NFL quote parsing.

No browser automation or sportsbook scraping is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import certifi
import keyring

SERVICE_NAME = "Betting Spreads"
ACCOUNT_NAME = "the_odds_api_key"
BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEY = "americanfootball_nfl"

# Pass 1: core player markets. Anytime TD is deliberately included.
PASS1_MARKETS = (
    "player_pass_yds",
    "player_pass_tds",
    "player_pass_attempts",
    "player_pass_completions",
    "player_pass_interceptions",
    "player_rush_yds",
    "player_rush_attempts",
    "player_receptions",
    "player_reception_yds",
    "player_reception_tds",
    "player_rush_tds",
    "player_anytime_td",
)

# Pass 2: only targeted, evidence-supported markets. Defensive player props
# (sacks/tackles) are intentionally absent; those remain ESPN research inputs.
PASS2_MARKETS = (
    "player_reception_longest",
    "player_pass_longest_completion",
    "player_rush_longest",
    "player_pass_rush_yds",
    "player_rush_reception_yds",
    "player_pass_rush_reception_yds",
    "player_pass_yds_q1",
    "player_pass_yds_h1",
    "player_reception_yds_q1",
    "player_reception_yds_h1",
    "player_rush_yds_q1",
    "player_rush_yds_h1",
    "player_receptions_q1",
    "player_receptions_h1",
)

MARKET_LABELS = {
    "player_pass_yds": "passing yards",
    "player_pass_tds": "passing touchdowns",
    "player_pass_attempts": "passing attempts",
    "player_pass_completions": "completions",
    "player_pass_interceptions": "passing interceptions",
    "player_rush_yds": "rushing yards",
    "player_rush_attempts": "rushing attempts",
    "player_receptions": "receptions",
    "player_reception_yds": "receiving yards",
    "player_reception_tds": "receiving touchdowns",
    "player_rush_tds": "rushing touchdowns",
    "player_anytime_td": "anytime touchdown",
    "player_reception_longest": "longest reception",
    "player_pass_longest_completion": "longest pass completion",
    "player_rush_longest": "longest rush",
    "player_pass_rush_yds": "passing plus rushing yards",
    "player_rush_reception_yds": "rushing plus receiving yards",
    "player_pass_rush_reception_yds": "passing, rushing and receiving yards",
    "player_pass_yds_q1": "first quarter passing yards",
    "player_pass_yds_h1": "first half passing yards",
    "player_reception_yds_q1": "first quarter receiving yards",
    "player_reception_yds_h1": "first half receiving yards",
    "player_rush_yds_q1": "first quarter rushing yards",
    "player_rush_yds_h1": "first half rushing yards",
    "player_receptions_q1": "first quarter receptions",
    "player_receptions_h1": "first half receptions",
}


def get_api_key() -> str:
    value = os.environ.get("THE_ODDS_API_KEY", "").strip()
    if value:
        return value
    try:
        return (keyring.get_password(SERVICE_NAME, ACCOUNT_NAME) or "").strip()
    except Exception:
        return ""


def save_api_key(value: str) -> None:
    value = str(value or "").strip()
    if not value:
        raise ValueError("The Odds API key cannot be blank.")
    keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, value)


def _cache_dir() -> Path:
    return Path(__file__).resolve().parent / "odds_cache"


def _cache_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return _cache_dir() / f"{digest}.json"


def _request_json(path: str, params: dict[str, str] | None = None) -> tuple[Any, dict[str, str]]:
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("The Odds API key is not configured.")
    query = urllib.parse.urlencode(params or {})
    url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
    request = urllib.request.Request(
        url,
        headers={
            "x-api-key": api_key,
            "User-Agent": "Betting-Spreads/1.0 (documented odds API client)",
            "Accept": "application/json",
        },
    )
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            body = json.loads(response.read().decode("utf-8"))
            headers = {k.lower(): v for k, v in response.headers.items()}
            return body, headers
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        if exc.code == 429:
            raise RuntimeError(
                "The Odds API quota/rate limit was reached (HTTP 429). No further odds requests were attempted."
            ) from exc
        if exc.code in (401, 403):
            raise RuntimeError(
                f"The Odds API rejected the key or plan permissions (HTTP {exc.code}): {detail}"
            ) from exc
        raise RuntimeError(f"The Odds API request failed (HTTP {exc.code}): {detail}") from exc


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _event_cache_key(event_id: str, markets: tuple[str, ...]) -> str:
    return f"{event_id}|us|american|{','.join(markets)}"


def _load_fresh_cache(key: str, max_age_minutes: int = 20) -> dict[str, Any] | None:
    path = _cache_path(key)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        saved = datetime.fromisoformat(record["cached_at"])
        if datetime.now(timezone.utc) - saved <= timedelta(minutes=max_age_minutes):
            return record
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def _save_cache(key: str, record: dict[str, Any]) -> None:
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_event(game: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any] | None:
    away = _norm(game.get("Away Team", ""))
    home = _norm(game.get("Home Team", ""))
    try:
        target = datetime.fromisoformat(str(game.get("event_start_utc", "")).replace("Z", "+00:00"))
    except ValueError:
        target = None
    candidates = []
    for event in events:
        if _norm(event.get("away_team", "")) != away or _norm(event.get("home_team", "")) != home:
            continue
        try:
            kickoff = datetime.fromisoformat(str(event.get("commence_time", "")).replace("Z", "+00:00"))
            delta = abs((kickoff - target).total_seconds()) if target else 0
        except ValueError:
            delta = 0
        if delta <= 8 * 3600:
            candidates.append((delta, event))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _parse_quotes(event_data: dict[str, Any], retrieved_at: str) -> list[dict[str, Any]]:
    quotes: list[dict[str, Any]] = []
    event_id = str(event_data.get("id") or "")
    for bookmaker in event_data.get("bookmakers", []) or []:
        book_name = str(bookmaker.get("title") or bookmaker.get("key") or "Unknown sportsbook")
        book_key = str(bookmaker.get("key") or "")
        for market in bookmaker.get("markets", []) or []:
            market_key = str(market.get("key") or "")
            label = MARKET_LABELS.get(market_key, market_key.replace("_", " "))
            updated = str(market.get("last_update") or retrieved_at)
            for outcome in market.get("outcomes", []) or []:
                player = str(outcome.get("description") or "").strip()
                side = str(outcome.get("name") or "").strip()
                # Some yes/no scorer feeds put the player in name and Yes/No in description.
                if market_key in {"player_anytime_td", "player_tds", "player_1st_td", "player_last_td"}:
                    if not player and side.casefold() not in {"yes", "no", "over", "under"}:
                        player, side = side, "Yes"
                if not player:
                    continue
                point = outcome.get("point")
                price = outcome.get("price")
                if price in (None, ""):
                    continue
                line = (
                    f"{side} {point}" if point not in (None, "") else
                    side if side else "Yes"
                )
                quotes.append({
                    "player": player,
                    "market": label,
                    "market_key": market_key,
                    "side": side,
                    "line": line,
                    "point": point,
                    "price": f"{int(price):+d}" if isinstance(price, (int, float)) else str(price),
                    "sportsbook": book_name,
                    "sportsbook_key": book_key,
                    "source_url": f"https://the-odds-api.com/",
                    "event_id": event_id,
                    "retrieved_at": retrieved_at,
                    "market_updated_at": updated,
                })
    return quotes


def fetch_event_quotes(
    game: dict[str, Any],
    markets: tuple[str, ...] = PASS1_MARKETS,
    refresh: bool = False,
) -> dict[str, Any]:
    """Fetch/cache quotes for one matchup. Events lookup is free; odds cost is market-based."""
    if not get_api_key():
        return {"quotes": [], "status": "no_api_key", "usage": {}}
    cache_key = _event_cache_key(str(game.get("event_id") or ""), markets)
    cached = None if refresh else _load_fresh_cache(cache_key)
    if cached:
        return {**cached, "status": "cached"}

    events, event_headers = _request_json(
        f"/sports/{SPORT_KEY}/events",
        {"dateFormat": "iso"},
    )
    event = _find_event(game, events if isinstance(events, list) else [])
    if not event:
        return {
            "quotes": [], "status": "event_not_matched",
            "message": "No matching The Odds API event was found for this ESPN matchup.",
            "usage": event_headers,
        }

    event_id = str(event.get("id") or "")
    params = {
        "regions": "us",
        "markets": ",".join(markets),
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    payload, headers = _request_json(
        f"/sports/{SPORT_KEY}/events/{event_id}/odds",
        params,
    )
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record = {
        "quotes": _parse_quotes(payload if isinstance(payload, dict) else {}, retrieved_at),
        "status": "fetched",
        "event_id": event_id,
        "retrieved_at": retrieved_at,
        "usage": {
            "requests_used": headers.get("x-requests-used"),
            "requests_remaining": headers.get("x-requests-remaining"),
            "requests_last": headers.get("x-requests-last"),
        },
    }
    _save_cache(cache_key, record)
    return record


def summarize_quotes(quotes: list[dict[str, Any]]) -> str:
    if not quotes:
        return "No live player-prop quotes were returned by The Odds API for the requested markets."
    lines = ["THE ODDS API VERIFIED PLAYER-PROP QUOTES (US region; American odds):"]
    for quote in quotes:
        lines.append(
            f"{quote.get('player')} | {quote.get('market')} | {quote.get('line')} "
            f"| {quote.get('price')} | {quote.get('sportsbook')} "
            f"| market updated {quote.get('market_updated_at')} "
            f"| retrieved {quote.get('retrieved_at')}"
        )
    return "\n".join(lines)
