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
GAME_MARKETS = ("h2h", "spreads", "totals")

# Pass 1: core player markets. Anytime TD is deliberately included.
PASS1_MARKETS = (
    "player_pass_yds",
    "player_pass_tds",
    "player_pass_attempts",
    "player_pass_completions",
    "player_rush_yds",
    "player_rush_attempts",
    "player_receptions",
    "player_reception_yds",
    "player_anytime_td",
)

# Pass 2: only targeted, evidence-supported markets. Defensive player props
# (sacks/tackles) are intentionally absent; those remain ESPN research inputs.
PASS2_TD_MARKETS = ("player_1st_td",)

PASS2_MARKETS = (
    "player_pass_interceptions",
    "player_rush_tds",
    "player_reception_tds",
    "player_reception_longest",
    "player_pass_longest_completion",
    "player_rush_longest",
    "player_pass_rush_yds",
    "player_rush_reception_yds",
    "player_pass_rush_reception_yds",
    "player_pass_yds_q1",
    "player_1st_td",
    "h2h_q1",
    "spreads_q1",
    "totals_q1",
    "h2h_h1",
    "spreads_h1",
    "totals_h1",
)

MARKET_LABELS = {
    "h2h": "full-game moneyline",
    "spreads": "full-game spread",
    "totals": "full-game total",
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
    "player_1st_td": "first touchdown scorer",
    "player_reception_longest": "longest reception",
    "player_pass_longest_completion": "longest pass completion",
    "player_rush_longest": "longest rush",
    "player_pass_rush_yds": "passing plus rushing yards",
    "player_rush_reception_yds": "rushing plus receiving yards",
    "player_pass_rush_reception_yds": "passing, rushing and receiving yards",
    "player_pass_yds_q1": "first quarter passing yards",
    "h2h_q1": "first quarter moneyline",
    "spreads_q1": "first quarter spread",
    "totals_q1": "first quarter total",
    "h2h_h1": "first half moneyline",
    "spreads_h1": "first half spread",
    "totals_h1": "first half total",
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


def _prompt_for_api_key_cli() -> bool:
    """Prompt visibly in Terminal when the application is running in CLI mode."""
    import getpass
    import sys

    if not sys.stdin or not sys.stdin.isatty():
        print(
            "The Odds API key is not configured. Set THE_ODDS_API_KEY or run "
            "the application interactively to configure it. Continuing without "
            "verified sportsbook quotes.",
            file=sys.stderr,
        )
        return False

    print(
        "The Odds API key is not configured. The key will be stored securely "
        "in your OS credential store, not in this project or workbook."
    )
    try:
        value = getpass.getpass(
            "Paste The Odds API key (press Enter to continue without odds): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nContinuing without verified sportsbook quotes.")
        return False

    if not value:
        print("Continuing without verified sportsbook quotes.")
        return False
    try:
        save_api_key(value)
    except Exception as exc:
        print(f"Could not save The Odds API key: {exc}", file=sys.stderr)
        return False
    print("The Odds API key was saved securely.")
    return True


def ensure_api_key_configured(use_gui: bool = True) -> bool:
    """Prompt for the Odds API key using a GUI or a visible Terminal prompt."""
    if get_api_key():
        return True
    if not use_gui:
        return _prompt_for_api_key_cli()

    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.title("Configure The Odds API")
        root.geometry("560x210")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        tk.Label(
            root,
            text=("Paste your The Odds API key. It will be stored in macOS Keychain "
                  "or Windows Credential Manager, not in this project or workbook. "
                  "Leave it blank to continue without verified sportsbook quotes."),
            wraplength=510, justify="left",
        ).pack(padx=18, pady=(18, 12), anchor="w")
        entry = tk.Entry(root, show="*", width=68)
        entry.pack(padx=18, fill="x")
        result = {"configured": False}

        def save() -> None:
            value = entry.get().strip()
            if not value:
                messagebox.showwarning("API key required", "Paste the key or choose Continue without odds.", parent=root)
                return
            try:
                save_api_key(value)
            except Exception as exc:
                messagebox.showerror("Could not save API key", str(exc), parent=root)
                return
            result["configured"] = True
            root.destroy()

        buttons = tk.Frame(root)
        buttons.pack(pady=18)
        tk.Button(buttons, text="Save key securely", command=save).pack(side="left", padx=6)
        tk.Button(buttons, text="Continue without odds", command=root.destroy).pack(side="left", padx=6)
        root.mainloop()
        return bool(result["configured"] or get_api_key())
    except Exception as exc:
        import sys
        print(
            f"Could not open the API-key window ({exc}). Falling back to Terminal.",
            file=sys.stderr,
        )
        return _prompt_for_api_key_cli()


def _cache_dir() -> Path:
    # Keep writable app data outside a packaged .app/.exe bundle.
    return Path.home() / ".betting_spreads" / "odds_cache"


def _quota_state_path() -> Path:
    return _cache_dir() / "quota_state.json"


def _known_remaining_credits() -> int | None:
    try:
        state = json.loads(_quota_state_path().read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(state.get("updated_at", ""))
        if datetime.now(timezone.utc) - updated > timedelta(hours=24):
            return None
        value = state.get("requests_remaining")
        return int(value) if value not in (None, "") else None
    except (OSError, ValueError, TypeError):
        return None


def _save_quota_state(headers: dict[str, str]) -> None:
    remaining = headers.get("x-requests-remaining")
    if remaining in (None, ""):
        return
    path = _quota_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "requests_remaining": remaining,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, indent=2), encoding="utf-8")


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


def _load_fresh_cache(key: str, max_age_minutes: int = 5) -> dict[str, Any] | None:
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
    """Normalize player props and full-game sportsbook markets."""
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
                side = str(outcome.get("name") or "").strip()
                description = str(outcome.get("description") or "").strip()
                point = outcome.get("point")
                price = outcome.get("price")
                if price in (None, ""):
                    continue

                if market_key in GAME_MARKETS:
                    if market_key == "totals":
                        player = "Game Total"
                        if side.casefold() not in {"over", "under"} or point in (None, ""):
                            continue
                        line = f"{side} {point}"
                    else:
                        player = side
                        if not player:
                            continue
                        if market_key == "spreads":
                            if point in (None, ""):
                                continue
                            try:
                                line = f"{player} {float(point):+g}"
                            except (TypeError, ValueError):
                                continue
                        else:
                            line = player
                    quote_type = "game"
                else:
                    player = description
                    # Some yes/no scorer feeds put the player in name and Yes/No in description.
                    if market_key in {"player_anytime_td", "player_tds", "player_1st_td", "player_last_td"}:
                        if player.casefold() in {"yes", "no", "over", "under"} and side.casefold() not in {"yes", "no", "over", "under"}:
                            player, side = side, player
                        elif not player and side.casefold() not in {"yes", "no", "over", "under"}:
                            player, side = side, "Yes"
                    if not player:
                        continue
                    line = (
                        f"{side} {point}" if point not in (None, "") else
                        side if side else "Yes"
                    )
                    quote_type = "player"

                quotes.append({
                    "player": player,
                    "market": label,
                    "market_key": market_key,
                    "market_type": quote_type,
                    "side": side,
                    "line": line,
                    "point": point,
                    "price": f"{int(price):+d}" if isinstance(price, (int, float)) else str(price),
                    "sportsbook": book_name,
                    "sportsbook_key": book_key,
                    "source_url": "https://the-odds-api.com/",
                    "event_id": event_id,
                    "retrieved_at": retrieved_at,
                    "market_updated_at": updated,
                })
    return quotes


def _available_market_keys(payload: Any) -> set[str]:
    """Extract currently listed market keys from the free event-markets endpoint."""
    if not isinstance(payload, dict):
        return set()
    bookmakers = payload.get("bookmakers") or []
    if not isinstance(bookmakers, list):
        return set()
    return {
        str(market.get("key") or "")
        for bookmaker in bookmakers if isinstance(bookmaker, dict)
        for market in (bookmaker.get("markets") or [])
        if isinstance(market, dict) and market.get("key")
    }


def fetch_event_quotes(
    game: dict[str, Any],
    markets: tuple[str, ...] = PASS1_MARKETS,
    refresh: bool = False,
) -> dict[str, Any]:
    """Discover listed markets first, then fetch only available quotes for one matchup."""
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
    discovery_error = ""
    discovery_headers: dict[str, str] = {}
    try:
        available_payload, discovery_headers = _request_json(
            f"/sports/{SPORT_KEY}/events/{event_id}/markets",
            {"regions": "us", "dateFormat": "iso"},
        )
        _save_quota_state(discovery_headers)
        available = _available_market_keys(available_payload)
    except Exception as exc:
        # Preserve the original request path if market discovery is temporarily
        # unavailable, but make the fallback explicit in the returned status.
        available = set(markets)
        discovery_error = f"{type(exc).__name__}: {exc}"

    requested = tuple(market for market in markets if market in available)
    if not requested:
        remaining = _known_remaining_credits()
        return {
            "quotes": [], "status": "no_requested_markets_listed",
            "event_id": event_id, "available_markets": sorted(available),
            "requested_markets": [],
            "message": "The event-markets endpoint listed no requested player/game markets.",
            "usage": {"requests_remaining": remaining},
            "market_discovery_error": discovery_error,
        }

    remaining = _known_remaining_credits()
    # Keep a 25-credit monthly safety buffer; event-market discovery itself costs 1 credit.
    if remaining is not None and remaining < len(requested) + 25:
        return {
            "quotes": [], "status": "quota_reserve_reached",
            "message": f"Skipped odds refresh to preserve the 25-credit reserve ({remaining} credits reported remaining).",
            "event_id": event_id, "available_markets": sorted(available),
            "requested_markets": list(requested),
            "usage": {"requests_remaining": remaining},
            "market_discovery_error": discovery_error,
        }

    params = {
        "regions": "us",
        "markets": ",".join(requested),
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    payload, headers = _request_json(
        f"/sports/{SPORT_KEY}/events/{event_id}/odds",
        params,
    )
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_quota_state(headers)
    quotes = _parse_quotes(payload if isinstance(payload, dict) else {}, retrieved_at)
    quote_counts: dict[str, int] = {}
    for quote in quotes:
        key = str(quote.get("market_key") or "")
        quote_counts[key] = quote_counts.get(key, 0) + 1
    record = {
        "cached_at": retrieved_at,
        "quotes": quotes,
        "status": "fetched",
        "event_id": event_id,
        "retrieved_at": retrieved_at,
        "available_markets": sorted(available),
        "requested_markets": list(requested),
        "quote_counts_by_market": quote_counts,
        "market_discovery_error": discovery_error,
        "usage": {
            "requests_used": headers.get("x-requests-used"),
            "requests_remaining": headers.get("x-requests-remaining"),
            "requests_last": headers.get("x-requests-last"),
        },
    }
    _save_cache(cache_key, record)
    return record


def research_supports_first_td(research_text: str) -> bool:
    """Return true only when collected player logs show a credible scoring-role candidate."""
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
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            log = record.get("season_game_log")
            if not isinstance(log, dict):
                continue
            try:
                games = int(log.get("games_available") or 0)
            except (TypeError, ValueError):
                continue
            totals_by_category = log.get("season_totals", {})
            totals = next(iter(totals_by_category.values()), {}) if isinstance(totals_by_category, dict) else {}
            if games <= 0 or not isinstance(totals, dict):
                continue
            try:
                rushing_tds = float(totals.get("rushingTouchdowns", 0) or 0)
                receiving_tds = float(totals.get("receivingTouchdowns", 0) or 0)
                carries = float(totals.get("rushingAttempts", 0) or 0)
                targets = float(totals.get("receivingTargets", 0) or 0)
            except (TypeError, ValueError):
                continue
            position = str(record.get("position") or "").upper()
            if position == "RB" and carries / games >= 7 and rushing_tds >= 2:
                return True
            if position in {"WR", "TE"} and targets / games >= 5 and receiving_tds >= 2:
                return True
            if position == "QB" and carries / games >= 3 and rushing_tds >= 1:
                return True
    return False


def summarize_quotes(quotes: list[dict[str, Any]]) -> str:
    """Render exact sportsbook quotes with market type, book and update timestamps."""
    if not quotes:
        return "No sportsbook quotes were returned for the requested markets."
    game_quotes = [q for q in quotes if q.get("market_type") == "game"]
    player_quotes = [q for q in quotes if q.get("market_type") != "game"]
    lines = []
    if game_quotes:
        lines.append("THE ODDS API VERIFIED FULL-GAME SPORTSBOOK MARKETS:")
        for quote in game_quotes:
            lines.append(
                f"{quote.get('market')} | {quote.get('player')} | {quote.get('line')} "
                f"| {quote.get('price')} | {quote.get('sportsbook')} "
                f"| updated {quote.get('market_updated_at')} "
                f"| retrieved {quote.get('retrieved_at')}"
            )
    if player_quotes:
        lines.append("THE ODDS API VERIFIED PLAYER-PROP QUOTES (US region; American odds):")
        for quote in player_quotes:
            lines.append(
                f"{quote.get('player')} | {quote.get('market')} | {quote.get('line')} "
                f"| {quote.get('price')} | {quote.get('sportsbook')} "
                f"| updated {quote.get('market_updated_at')} "
                f"| retrieved {quote.get('retrieved_at')}"
            )
    return "\n".join(lines)
