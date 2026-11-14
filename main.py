#!/usr/bin/env python3
"""Download NFL matchups and generate AI-assisted, research-backed betting analysis."""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import certifi
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

from ai_analysis import analyze_game
from live_research import collect_game_research, reset_research_cache
from ai_providers import ensure_configured, get_provider_usage_stats, reset_provider_run_state

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
HEADERS = {"User-Agent": "Betting-Spreads/2.0 (NFL matchup analysis exporter)"}
ET = ZoneInfo("America/New_York")


def application_directory() -> Path:
    """Return the folder beside the executable (or source script)."""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        if sys.platform == "darwin" and ".app" in str(executable):
            for parent in executable.parents:
                if parent.suffix == ".app":
                    return parent.parent
        return executable.parent
    return Path(__file__).resolve().parent


def fetch_scoreboard() -> dict:
    request = urllib.request.Request(ESPN_URL, headers=HEADERS)
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
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
        home = next((team for team in competitors if team.get("homeAway") == "home"), None)
        away = next((team for team in competitors if team.get("homeAway") == "away"), None)
        if not home or not away:
            continue

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
        details = market.get("details", "")
        spread = market.get("spread", "")
        if not details:
            details = str(spread) if spread != "" else "Odds unavailable"

        games.append({
            "event_id": str(event.get("id", "")),
            "away_team_id": str(away.get("team", {}).get("id", "")),
            "home_team_id": str(home.get("team", {}).get("id", "")),
            "Game Date (ET)": kickoff_text,
            "Away Team": away.get("team", {}).get("displayName", "Away team"),
            "Home Team": home.get("team", {}).get("displayName", "Home team"),
            "Betting Line": details,
            "Game Status": event.get("status", {}).get("type", {}).get("description", ""),
        })

    if not games:
        raise RuntimeError("The ESPN response contained no usable NFL matchups.")
    return games, season, week



def game_is_no_longer_pregame(game: dict) -> bool:
    """Avoid generating pregame bets for games that are live or already finished."""
    status = str(game.get("Game Status", "")).lower()
    return any(marker in status for marker in (
        "final", "completed", "in progress", "halftime", "quarter", "overtime"
    ))

def _sheet_title(game: dict, used: set[str]) -> str:
    raw = f"{game['Away Team']} @ {game['Home Team']}"
    cleaned = re.sub(r"[\\/*?:\[\]]", "", raw).strip()[:31] or "Matchup"
    candidate = cleaned
    index = 2
    while candidate in used:
        suffix = f" ({index})"
        candidate = cleaned[:31 - len(suffix)] + suffix
        index += 1
    used.add(candidate)
    return candidate


def _style_header(cell, fill="D9EAF7"):
    cell.font = Font(bold=True, color="17365D")
    cell.fill = PatternFill("solid", fgColor=fill)
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _write_matchup_sheet(workbook: Workbook, game: dict, analysis: dict, used: set[str]) -> None:
    sheet = workbook.create_sheet(_sheet_title(game, used))
    sheet.sheet_view.showGridLines = False
    navy = PatternFill("solid", fgColor="17365D")
    pale = PatternFill("solid", fgColor="D9EAF7")
    light = PatternFill("solid", fgColor="EEF3F8")
    white_bold = Font(bold=True, color="FFFFFF")
    thin_gray = Side(style="thin", color="D9E1F2")

    sheet.merge_cells("A1:J1")
    sheet["A1"] = f"{game['Away Team']} at {game['Home Team']} — AI Betting Analysis"
    sheet["A1"].font = Font(bold=True, size=15, color="FFFFFF")
    sheet["A1"].fill = navy
    sheet["A1"].alignment = Alignment(horizontal="center")
    sheet.row_dimensions[1].height = 28

    sheet.merge_cells("A2:J2")
    sheet["A2"] = (
        f"Kickoff: {game.get('Game Date (ET)', 'Unknown')} | "
        f"ESPN game line: {game.get('Betting Line', 'Unavailable')}"
    )
    sheet["A2"].font = Font(italic=True, color="17365D")
    sheet["A2"].alignment = Alignment(wrap_text=True)

    sheet.merge_cells("A3:J3")
    if analysis.get("cache_reused"):
        sheet["A3"] = (
            "CACHED ANALYSIS — research has not been refreshed for current injuries, "
            "player availability, weather, or sportsbook markets."
        )
        sheet["A3"].font = Font(italic=True, color="9C6500")
    else:
        provider = analysis.get("ai_provider", "Unknown provider")
        research_truncated = analysis.get("research_truncated_for_prompt")
        research_chars = analysis.get("research_prompt_characters")
        if research_truncated is True:
            prompt_status = f"TRUNCATED ({research_chars:,} chars)" if isinstance(research_chars, int) else "TRUNCATED"
        elif research_truncated is False:
            prompt_status = f"complete ({research_chars:,} chars)" if isinstance(research_chars, int) else "complete"
        else:
            prompt_status = "not recorded"
        sheet["A3"] = (
            f"AI provider: {provider} | Research: {analysis.get('research_mode', 'Not recorded')} "
            f"| AI research prompt: {prompt_status} | Live sources are listed at the bottom of this sheet."
        )
        sheet["A3"].font = Font(italic=True, color="9C6500" if research_truncated is True else "526579")
    sheet["A3"].alignment = Alignment(wrap_text=True)

    sheet["A4"] = "MATCHUP SUMMARY"
    sheet["A4"].font = white_bold
    sheet["A4"].fill = navy
    sheet.merge_cells("A4:J4")
    sheet.merge_cells("A5:J6")
    sheet["A5"] = analysis.get("summary", "No summary returned.")
    sheet["A5"].alignment = Alignment(wrap_text=True, vertical="top")
    sheet.row_dimensions[5].height = 38
    sheet.row_dimensions[6].height = 28

    sheet["A8"] = "EXPECTED GAME SCRIPT"
    sheet["A8"].font = white_bold
    sheet["A8"].fill = navy
    sheet.merge_cells("A8:J8")
    sheet.merge_cells("A9:J10")
    sheet["A9"] = analysis.get("game_script", "")
    sheet["A9"].alignment = Alignment(wrap_text=True, vertical="top")
    sheet.row_dimensions[9].height = 40
    sheet.row_dimensions[10].height = 25

    headers = [
        "Rank", "Player / Team", "Recommended Bet", "Verified Market Line",
        "Model Play Threshold", "Estimated Hit Probability", "Confidence (1–10)",
        "Statistical Rationale", "Risk Factors", "Data Quality",
    ]
    picks = analysis.get("picks", [])
    player_props = [pick for pick in picks if pick.get("market_type") == "Player Prop"]
    game_picks = [pick for pick in picks if pick.get("market_type") == "Game Prop"]

    def write_pick_section(start_row, title, items, empty_message):
        sheet.cell(start_row, 1, title)
        sheet.cell(start_row, 1).font = white_bold
        sheet.cell(start_row, 1).fill = navy
        sheet.merge_cells(start_row=start_row, start_column=1, end_row=start_row, end_column=10)
        for col, value in enumerate(headers, 1):
            _style_header(sheet.cell(start_row + 1, col, value))
        if not items:
            sheet.merge_cells(start_row=start_row + 2, start_column=1, end_row=start_row + 2, end_column=10)
            sheet.cell(start_row + 2, 1, empty_message)
            sheet.cell(start_row + 2, 1).alignment = Alignment(wrap_text=True)
            return start_row + 4
        for offset, pick in enumerate(items):
            row_num = start_row + 2 + offset
            values = [
                offset + 1, pick.get("player_or_team", ""),
                pick.get("bet_description", ""), pick.get("market_line", "Not verified"),
                pick.get("play_threshold", ""),
                f"{pick.get('estimated_hit_probability', 0):.1f}%",
                pick.get("confidence", ""), pick.get("statistical_rationale", ""),
                pick.get("risk_factors", ""), pick.get("data_quality", ""),
            ]
            for col, value in enumerate(values, 1):
                cell = sheet.cell(row=row_num, column=col, value=value)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                cell.border = Border(bottom=thin_gray)
                if row_num % 2 == 0:
                    cell.fill = light
            sheet.row_dimensions[row_num].height = 82
        return start_row + len(items) + 3

    next_pick_row = write_pick_section(
        12, "VERIFIED PLAYER PROPS (SPORTSBOOK LINE REQUIRED)", player_props,
        "No verified sportsbook player-prop lines were available for this matchup."
    )
    model_only_props = analysis.get("model_only_picks", [])
    next_pick_row = write_pick_section(
        next_pick_row,
        "MODEL-ONLY PROP LEANS (NOT VERIFIED SPORTSBOOK BETS)",
        model_only_props,
        "No model-only player-prop leans met the evidence threshold."
    )
    next_pick_row = write_pick_section(
        next_pick_row, "GAME-LEVEL BETS (FULL GAME / 1ST HALF / 1ST QUARTER)", game_picks,
        "No game-level bets met the evidence threshold for this matchup."
    )
    td_start = max(16, next_pick_row + 1)
    sheet.cell(td_start, 1, "ANYTIME TOUCHDOWN SCORERS")
    sheet.cell(td_start, 1).font = white_bold
    sheet.cell(td_start, 1).fill = navy
    sheet.merge_cells(start_row=td_start, start_column=1, end_row=td_start, end_column=9)
    td_headers = ["Rank", "Player", "Team", "Availability", "Availability Evidence", "Estimated TD Probability", "Confidence (1–10)", "Rationale", "Risk Factors"]
    for col, value in enumerate(td_headers, 1):
        _style_header(sheet.cell(td_start + 1, col, value))
    scorers = analysis.get("touchdown_scorers", [])
    if not scorers:
        sheet.merge_cells(start_row=td_start + 2, start_column=1, end_row=td_start + 2, end_column=9)
        sheet.cell(td_start + 2, 1, "No touchdown scorer met the evidence threshold.")
    for row_num, scorer in enumerate(scorers, td_start + 2):
        values = [
            scorer.get("rank", row_num - td_start - 1), scorer.get("player", ""),
            scorer.get("team", ""), scorer.get("availability", "Not verified"),
            scorer.get("availability_evidence", "No current evidence supplied"),
            f"{scorer.get('estimated_td_probability', 0):.1f}%",
            scorer.get("confidence", ""), scorer.get("rationale", ""),
            scorer.get("risk_factors", ""),
        ]
        for col, value in enumerate(values, 1):
            cell = sheet.cell(row=row_num, column=col, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border = Border(bottom=thin_gray)
            if row_num % 2 == 0:
                cell.fill = light
        sheet.row_dimensions[row_num].height = 65

    injury_start = td_start + max(len(scorers), 1) + 4
    sheet.cell(injury_start, 1, "INJURY / NEWS IMPACT ANALYSIS")
    sheet.cell(injury_start, 1).font = white_bold
    sheet.cell(injury_start, 1).fill = navy
    sheet.merge_cells(
        start_row=injury_start, start_column=1,
        end_row=injury_start, end_column=10,
    )

    injury_headers = [
        "Impact Type / Subject", "Team", "Status / Development", "Affected Player",
        "Affected Team", "Impact", "Statistical Effect", "Prop Market",
        "Evidence / Confidence", "Conditions",
    ]
    for col, value in enumerate(injury_headers, 1):
        _style_header(sheet.cell(injury_start + 1, col, value))

    impacts = analysis.get("injury_impact", [])
    if not impacts:
        sheet.merge_cells(
            start_row=injury_start + 2, start_column=1,
            end_row=injury_start + 2, end_column=10,
        )
        sheet.cell(
            injury_start + 2, 1,
            "No meaningful, evidence-supported injury or news impacts identified.",
        )
        sheet.cell(injury_start + 2, 1).alignment = Alignment(wrap_text=True)

    for row_num, impact in enumerate(impacts, injury_start + 2):
        values = [
            f"{impact.get('impact_type', 'Injury')} — {impact.get('injured_player', '')}",
            impact.get("team", ""),
            impact.get("availability", ""),
            impact.get("affected_player", ""),
            impact.get("affected_team", ""),
            impact.get("impact_direction", ""),
            impact.get("statistical_effect", ""),
            impact.get("prop_market", ""),
            (
                f"{impact.get('evidence', '')} "
                f"(Confidence: {impact.get('confidence', '')}/10)"
            ),
            impact.get("conditions", ""),
        ]
        for col, value in enumerate(values, 1):
            cell = sheet.cell(row=row_num, column=col, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border = Border(bottom=thin_gray)
            if row_num % 2 == 0:
                cell.fill = light
        sheet.row_dimensions[row_num].height = 78

    # The full research pool remains internal; surface only selected high-upside ideas.
    sneaky_start = injury_start + max(len(impacts), 1) + 4
    sneaky_props = analysis.get("sneaky_props", [])
    sneaky_props = sneaky_props if isinstance(sneaky_props, list) else []
    sneaky_headers = ["Rank", "Player / Team", "Sneaky Prop / Ladder", "Market Line",
        "Model Threshold", "Hit Probability", "Confidence", "Why It Has Upside",
        "Risk Factors", "Data Quality"]
    sheet.cell(sneaky_start, 1, "SNEAKY PROPS & LADDER OPPORTUNITIES")
    sheet.cell(sneaky_start, 1).font = white_bold
    sheet.cell(sneaky_start, 1).fill = navy
    sheet.merge_cells(start_row=sneaky_start, start_column=1, end_row=sneaky_start, end_column=10)
    for col, value in enumerate(sneaky_headers, 1):
        _style_header(sheet.cell(sneaky_start + 1, col, value))
    if not sneaky_props:
        sheet.merge_cells(start_row=sneaky_start + 2, start_column=1, end_row=sneaky_start + 2, end_column=10)
        sheet.cell(sneaky_start + 2, 1, "No additional high-upside prop or ladder angle met the evidence threshold.")
    for offset, pick in enumerate(sneaky_props):
        row_num = sneaky_start + 2 + offset
        values = [offset + 1, pick.get("player_or_team", ""), pick.get("bet_description", ""),
            pick.get("market_line", "NOT VERIFIED — MODEL IDEA ONLY"), pick.get("play_threshold", ""),
            f"{pick.get('estimated_hit_probability', 0):.1f}%", pick.get("confidence", ""),
            pick.get("statistical_rationale", ""), pick.get("risk_factors", ""), pick.get("data_quality", "")]
        for col, value in enumerate(values, 1):
            cell = sheet.cell(row=row_num, column=col, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.border = Border(bottom=thin_gray)
            if row_num % 2 == 0:
                cell.fill = light
        sheet.row_dimensions[row_num].height = 78

    gap_row = sneaky_start + max(len(sneaky_props), 1) + 4
    sheet.cell(gap_row, 1, "DATA LIMITATIONS")
    sheet.cell(gap_row, 1).font = white_bold
    sheet.cell(gap_row, 1).fill = navy
    sheet.merge_cells(start_row=gap_row, start_column=1, end_row=gap_row, end_column=10)
    gaps = analysis.get("data_gaps", [])
    sheet.merge_cells(start_row=gap_row + 1, start_column=1, end_row=gap_row + 2, end_column=10)
    sheet.cell(gap_row + 1, 1, "\n".join(f"• {gap}" for gap in gaps) if gaps else "No material data gaps were reported.")
    sheet.cell(gap_row + 1, 1).alignment = Alignment(wrap_text=True, vertical="top")

    research_row = gap_row + 4
    sheet.cell(research_row, 1, "INDEPENDENT LIVE RESEARCH")
    sheet.cell(research_row, 1).font = white_bold
    sheet.cell(research_row, 1).fill = navy
    sheet.merge_cells(start_row=research_row, start_column=1, end_row=research_row, end_column=10)
    sheet.merge_cells(start_row=research_row + 1, start_column=1, end_row=research_row + 3, end_column=10)
    sheet.cell(
        research_row + 1, 1,
        analysis.get("independent_research", "No independent research text was retained.")
    )
    sheet.cell(research_row + 1, 1).alignment = Alignment(wrap_text=True, vertical="top")
    sheet.row_dimensions[research_row + 1].height = 180

    source_row = research_row + 5
    sheet.cell(source_row, 1, "RESEARCH SOURCES")
    sheet.cell(source_row, 1).font = white_bold
    sheet.cell(source_row, 1).fill = navy
    sheet.merge_cells(start_row=source_row, start_column=1, end_row=source_row, end_column=10)
    sources = analysis.get("sources", [])
    if not sources:
        sheet.cell(source_row + 1, 1, "No source URLs were returned by the research tool.")
    for offset, source in enumerate(sources, 1):
        cell = sheet.cell(source_row + offset, 1, source.get("title", source.get("url", "")))
        cell.hyperlink = source.get("url")
        cell.style = "Hyperlink"

    widths = [8, 15, 22, 30, 22, 28, 18, 16, 58, 42]
    for col, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(col)].width = width
    sheet.freeze_panes = "A14"


def write_workbook(games: list[dict], analyses: list[dict], season: object, week: object) -> Path:
    output = application_directory() / "NFL_Weekly_Matchups.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Weekly Matchups"
    sheet.sheet_view.showGridLines = False
    sheet.append(["NFL Weekly Matchups"])
    sheet.append([
        f"Season: {season}", f"Week: {week}",
        f"Updated: {datetime.now(ET).strftime('%Y-%m-%d %I:%M %p ET')}",
    ])
    sheet.append([])
    headers = ["Game Date (ET)", "Away Team", "Home Team", "Betting Line", "Game Status"]
    sheet.append(headers)
    for game in games:
        sheet.append([game.get(header, "") for header in headers])

    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    sheet["A1"].font = Font(bold=True, size=16, color="FFFFFF")
    sheet["A1"].fill = PatternFill("solid", fgColor="17365D")
    sheet["A1"].alignment = Alignment(horizontal="center")
    for cell in sheet[2]:
        cell.font = Font(bold=True, color="17365D")
    for cell in sheet[4]:
        _style_header(cell)
    for row in sheet.iter_rows(min_row=5):
        for cell in row:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
    for col, width in enumerate([27, 24, 24, 24, 22], 1):
        sheet.column_dimensions[get_column_letter(col)].width = width
    sheet.freeze_panes = "A5"
    sheet.auto_filter.ref = f"A4:E{sheet.max_row}"

    used = {sheet.title}
    for game, analysis in zip(games, analyses):
        _write_matchup_sheet(workbook, game, analysis, used)

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



class ProgressWindow:
    """Small macOS progress window so the one-click app does not appear idle."""

    def __init__(self, total: int):
        self.root = None
        self.label = None
        self.bar = None
        if sys.platform != "darwin" or not getattr(sys, "frozen", False):
            return
        try:
            import tkinter as tk
            from tkinter import ttk
            self.root = tk.Tk()
            self.root.title("Betting Spreads")
            self.root.geometry("440x105")
            self.root.resizable(False, False)
            self.label = tk.Label(
                self.root, text=f"Preparing research for {total} matchups...",
                anchor="w", padx=14, pady=12
            )
            self.label.pack(fill="x")
            self.bar = ttk.Progressbar(self.root, maximum=total, mode="determinate")
            self.bar.pack(fill="x", padx=14, pady=(0, 14))
            self.root.update()
        except Exception:
            self.root = None

    def update(self, index: int, total: int, matchup: str) -> None:
        if self.root is not None:
            try:
                self.label.config(text=f"Researching {index} of {total}: {matchup}")
                self.bar["value"] = index - 1
                self.root.update_idletasks()
                self.root.update()
            except Exception:
                self.close()

    def close(self) -> None:
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:
                pass
            self.root = None

def _analysis_cache_path(game: dict) -> Path:
    """Stable local cache key for a specific matchup and kickoff."""
    identity = "|".join((
        str(game.get("Away Team", "")),
        str(game.get("Home Team", "")),
        str(game.get("Game Date (ET)", "")),
    ))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return application_directory() / "analysis_cache" / f"{digest}.json"


def _save_cached_analysis(game: dict, analysis: dict) -> None:
    path = _analysis_cache_path(game)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_cached_analysis(game: dict) -> dict:
    path = _analysis_cache_path(game)
    if not path.is_file():
        raise FileNotFoundError(
            f"No saved AI analysis exists for {game['Away Team']} at {game['Home Team']}. "
            "Run a normal update once to generate and cache it."
        )
    analysis = json.loads(path.read_text(encoding="utf-8"))
    gaps = list(analysis.get("data_gaps", []))
    gaps.append(
        "CACHED ANALYSIS: This research was reused without an AI API call. "
        "Player availability, injuries, weather, usage, and sportsbook markets have not been refreshed."
    )
    analysis["data_gaps"] = gaps
    analysis["cache_reused"] = True
    return analysis



def filter_requested_matchup(games: list[dict], arguments: list[str]) -> list[dict]:
    """Optionally limit an update to one named matchup for focused testing."""
    if "--matchup" not in arguments:
        return games
    index = arguments.index("--matchup")
    if index + 1 >= len(arguments) or arguments[index + 1].startswith("--"):
        raise ValueError('Use --matchup "Away Team at Home Team" to select one game.')
    requested = arguments[index + 1].strip().casefold()
    requested_sides = [
        side.strip() for side in requested.split(" at ", maxsplit=1)
    ]

    def team_matches(requested_team: str, full_team: str) -> bool:
        """Allow a nickname or city portion as well as the full team name."""
        normalized_team = full_team.casefold().strip()
        return bool(requested_team) and (
            requested_team in normalized_team or normalized_team in requested_team
        )

    matches = []
    for game in games:
        away = str(game.get("Away Team", "")).casefold()
        home = str(game.get("Home Team", "")).casefold()
        if len(requested_sides) == 2:
            is_match = (
                team_matches(requested_sides[0], away)
                and team_matches(requested_sides[1], home)
            )
        else:
            matchup = f"{away} at {home}"
            is_match = requested in matchup
        if is_match:
            matches.append(game)
    if not matches:
        available = ", ".join(
            f"{game['Away Team']} at {game['Home Team']}" for game in games
        )
        raise ValueError(
            f"No matchup matched {arguments[index + 1]!r}. Available games: {available}"
        )
    return matches


def main() -> int:
    reset_provider_run_state()
    reset_research_cache()
    log_path = application_directory() / "Betting_Spreads.log"
    try:
        reuse_cache = "--reuse-cache" in sys.argv[1:]
        refresh_incomplete = "--refresh-incomplete" in sys.argv[1:]
        data = fetch_scoreboard()
        games, season, week = extract_games(data)
        games = filter_requested_matchup(games, sys.argv[1:])
        if not reuse_cache:
            ensure_configured()
        analyses = []
        failures = []
        progress_window = ProgressWindow(len(games))
        try:
            for index, game in enumerate(games, 1):
                matchup = f"{game['Away Team']} at {game['Home Team']}"
                progress_window.update(index, len(games), matchup)
                if game_is_no_longer_pregame(game):
                    analyses.append({
                        "summary": "This game is already live or completed. Pregame recommendations were intentionally skipped.",
                        "game_script": "",
                        "picks": [],
                        "touchdown_scorers": [],
                        "data_gaps": ["No pregame betting analysis is generated after kickoff."],
                        "sources": [],
                    })
                    continue
                if sys.stdout is not None:
                    print(f"Researching {index} of {len(games)}: {matchup}", flush=True)
                research = None
                try:
                    if reuse_cache:
                        analysis = _load_cached_analysis(game)
                    elif refresh_incomplete:
                        cache_path = _analysis_cache_path(game)
                        cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else None
                        is_complete = (
                            cached is not None
                            and isinstance(cached.get("picks"), list)
                            and isinstance(cached.get("touchdown_scorers"), list)
                            and len(cached["touchdown_scorers"]) <= 3
                            and cached.get("analysis_version") == 5
                            and bool(cached.get("ai_provider"))
                        )
                        if is_complete:
                            analysis = _load_cached_analysis(game)
                        else:
                            research = collect_game_research(game)
                            analysis = analyze_game(game, research)
                            _save_cached_analysis(game, analysis)
                    else:
                        research = collect_game_research(game)
                        analysis = analyze_game(game, research)
                        _save_cached_analysis(game, analysis)
                    analyses.append(analysis)
                except Exception as exc:
                    failures.append(f"{matchup}: {exc}")
                    analyses.append({
                        "summary": (
                            "AI analysis was unavailable. Independent NFL research was collected "
                            "where possible; see the research evidence and source links below."
                            if research else
                            "AI research could not be completed and no independent research was available."
                        ),
                        "game_script": "No AI-generated game script is available for this matchup.",
                        "picks": [],
                        "touchdown_scorers": [],
                        "data_gaps": [str(exc)],
                        "sources": list(research.get("sources", [])) if research else [],
                        "independent_research": research.get("research_text", "") if research else "",
                        "ai_provider": "Unavailable",
                        "research_mode": "independent_research_only" if research else "unavailable",
                    })
        finally:
            progress_window.close()

        output = write_workbook(games, analyses, season, week)
        success_count = len(games) - len(failures)
        message = (
            f"NFL Season {season}, Week {week}\n"
            f"{len(games)} matchups exported.\n"
            f"AI analysis completed for {success_count} of {len(games)} games.\n"
            + ("Cached analysis reused; no AI API calls were made.\n" if reuse_cache else
               "Only missing/incomplete cache entries were refreshed; complete entries were reused.\n" if refresh_incomplete else
               "Fresh AI research completed with provider fallback; results were saved to the local cache.\n")
            + f"\nSaved to:\n{output}"
        )
        provider_usage = get_provider_usage_stats()
        usage_lines = []
        for provider_name, stats in sorted(provider_usage.items()):
            usage_line = (
                f"{provider_name}: {stats['requests']} request(s), "
                f"{stats['prompt_characters']:,} prompt characters"
            )
            if stats["prompt_tokens"] or stats["completion_tokens"]:
                usage_line += (
                    f", {stats['prompt_tokens']:,} input tokens, "
                    f"{stats['completion_tokens']:,} output tokens"
                )
            usage_lines.append(usage_line)
        usage_report = "\n".join(usage_lines) or "No AI provider requests were made."
        message += f"\n\nProvider API usage:\n{usage_report}"
        if failures:
            message += f"\n\n{len(failures)} matchup analyses failed. See Betting_Spreads.log."
        log_path.write_text(message + ("\n\n" + "\n".join(failures) if failures else "") + "\n", encoding="utf-8")
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
