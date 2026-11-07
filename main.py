#!/usr/bin/env python3
"""Download the current NFL week's schedule and available betting spreads to Excel."""

from __future__ import annotations

import json
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ESPN_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)
HEADERS = {"User-Agent": "Betting-Spreads/1.0 (weekly schedule exporter)"}
ET = ZoneInfo("America/New_York")


def application_directory() -> Path:
    """Return the folder beside the executable (or source script)."""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        # A windowed macOS PyInstaller app lives inside:
        # AppName.app/Contents/MacOS/AppName
        if sys.platform == "darwin" and ".app" in str(executable):
            for parent in executable.parents:
                if parent.suffix == ".app":
                    return parent.parent
        return executable.parent
    return Path(__file__).resolve().parent


def fetch_scoreboard() -> dict:
    request = urllib.request.Request(ESPN_URL, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not retrieve NFL data from ESPN: {exc}") from exc


def extract_games(data: dict) -> tuple[list[dict], object, object]:
    events = data.get("events", [])
    if not events:
        raise RuntimeError(
            "ESPN returned no games for the current scoreboard week. "
            "Try again later or check the ESPN scoreboard."
        )

    week = data.get("week", {}).get("number", "Unknown")
    season = data.get("season", {}).get("year", datetime.now(ET).year)
    games = []

    for event in events:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competition = competitions[0]
        competitors = competition.get("competitors") or []

        home = next((t for t in competitors if t.get("homeAway") == "home"), None)
        away = next((t for t in competitors if t.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        home_team = home.get("team", {}).get("displayName", "Home team")
        away_team = away.get("team", {}).get("displayName", "Away team")
        kickoff = event.get("date", "")
        try:
            kickoff_dt = datetime.fromisoformat(kickoff.replace("Z", "+00:00"))
            kickoff_text = kickoff_dt.astimezone(ET).strftime("%a %b %d, %Y %I:%M %p ET")
        except (ValueError, AttributeError):
            kickoff_text = kickoff

        odds = competition.get("odds") or event.get("odds") or []
        if isinstance(odds, dict):
            odds = [odds]
        market = odds[0] if odds else {}

        spread = market.get("spread", "")
        details = market.get("details", "")
        provider = market.get("provider") or {}
        sportsbook = provider.get("name", "") if isinstance(provider, dict) else str(provider)

        if not details:
            details = f"{spread}" if spread != "" else "Odds unavailable"

        games.append({
            "Game Date (ET)": kickoff_text,
            "Away Team": away_team,
            "Home Team": home_team,
            "Betting Line": details,
            "Spread": spread if spread != "" else "Unavailable",
            "Sportsbook": sportsbook or "Not provided",
            "Game Status": event.get("status", {}).get("type", {}).get("description", ""),
        })

    if not games:
        raise RuntimeError("The ESPN response contained no usable NFL matchups.")

    return games, season, week


def write_workbook(games: list[dict], season: object, week: object) -> Path:
    output = application_directory() / "NFL_Weekly_Matchups.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Weekly Matchups"

    sheet.append(["NFL Weekly Matchups"])
    sheet.append([f"Season: {season}", f"Week: {week}",
                  f"Updated: {datetime.now(ET).strftime('%Y-%m-%d %I:%M %p ET')}"])
    sheet.append([])
    headers = list(games[0].keys())
    sheet.append(headers)

    for game in games:
        sheet.append([game.get(header, "") for header in headers])

    title_fill = PatternFill("solid", fgColor="17365D")
    header_fill = PatternFill("solid", fgColor="D9EAF7")
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    sheet["A1"].font = Font(bold=True, size=16, color="FFFFFF")
    sheet["A1"].fill = title_fill
    sheet["A1"].alignment = Alignment(horizontal="center")

    for cell in sheet[2]:
        cell.font = Font(bold=True, color="17365D")
    for cell in sheet[4]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for row in sheet.iter_rows(min_row=5):
        for cell in row:
            cell.alignment = Alignment(vertical="center")

    for column in sheet.columns:
        values = [len(str(cell.value or "")) for cell in column]
        width = min(max(max(values, default=10) + 3, 12), 38)
        sheet.column_dimensions[get_column_letter(column[0].column)].width = width

    sheet.freeze_panes = "A5"
    sheet.auto_filter.ref = f"A4:{get_column_letter(len(headers))}{sheet.max_row}"
    workbook.save(output)
    return output


def show_macos_alert(title: str, message: str) -> None:
    """Show a native macOS completion/error dialog for the double-click app."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return
    import subprocess
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display dialog "{safe_message}" with title "{safe_title}" buttons {{"OK"}} default button "OK"'
    subprocess.run(["osascript", "-e", script], check=False)


def main() -> int:
    log_path = application_directory() / "Betting_Spreads.log"
    try:
        data = fetch_scoreboard()
        games, season, week = extract_games(data)
        output = write_workbook(games, season, week)
        message = (
            f"NFL Season {season}, Week {week}\n"
            f"{len(games)} matchups exported.\n\n"
            f"Saved to:\n{output}"
        )
        log_path.write_text(message + "\n", encoding="utf-8")
        if sys.stdout is not None:
            print(message)
        show_macos_alert("Betting Spreads updated", message)
        return 0
    except Exception as exc:
        details = f"Update failed: {exc}\n\n{traceback.format_exc()}"
        log_path.write_text(details, encoding="utf-8")
        if sys.stderr is not None:
            print(details, file=sys.stderr)
        show_macos_alert("Betting Spreads — update failed", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
