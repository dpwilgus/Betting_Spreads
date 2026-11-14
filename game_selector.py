"""Native Tkinter selector for Sunday and non-Sunday NFL slates."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def select_games_gui(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose Sunday or non-Sunday games, then select individual matchups."""
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception as exc:
        raise RuntimeError(
            "The matchup-selection window requires Tkinter. Use --no-gui to analyze the full scoreboard."
        ) from exc

    root = tk.Tk()
    root.title("Betting Spreads — Choose NFL Games")
    root.geometry("590x650")
    root.minsize(500, 450)
    root.attributes("-topmost", True)
    mode = tk.StringVar(value="sunday")
    selected_vars: list[tuple[dict[str, Any], Any]] = []

    tk.Label(root, text="Choose the slate", font=("Arial", 15, "bold")).pack(
        anchor="w", padx=18, pady=(16, 8)
    )
    radio_row = tk.Frame(root)
    radio_row.pack(fill="x", padx=18)
    tk.Radiobutton(
        radio_row, text="Sunday slate", variable=mode, value="sunday",
        command=lambda: refresh_games()
    ).pack(side="left", padx=(0, 18))
    tk.Radiobutton(
        radio_row, text="Non-Sunday (Thu / Fri / Sat / Mon)", variable=mode,
        value="other", command=lambda: refresh_games()
    ).pack(side="left")

    tk.Label(
        root,
        text="All games start selected. Uncheck any matchups you do not want analyzed.",
        wraplength=540, justify="left",
    ).pack(anchor="w", padx=18, pady=(10, 8))

    controls = tk.Frame(root)
    controls.pack(fill="x", padx=18, pady=(0, 8))
    count_label = tk.Label(controls, text="")
    count_label.pack(side="left")
    list_frame = tk.Frame(root, bd=1, relief="solid")
    list_frame.pack(fill="both", expand=True, padx=18, pady=4)

    def refresh_games() -> None:
        nonlocal selected_vars
        for child in list_frame.winfo_children():
            child.destroy()
        selected_vars = []
        subset = []
        for game in games:
            try:
                kickoff = datetime.fromisoformat(
                    str(game.get("event_start_utc", "")).replace("Z", "+00:00")
                ).astimezone(ET)
                is_sunday = kickoff.weekday() == 6
            except ValueError:
                is_sunday = True
            if (mode.get() == "sunday" and is_sunday) or (mode.get() == "other" and not is_sunday):
                subset.append(game)
        if not subset:
            tk.Label(list_frame, text="No games found in this slate.").pack(
                anchor="w", padx=12, pady=12
            )
        for game in subset:
            label = (
                f"{game.get('Away Team')} at {game.get('Home Team')}\n"
                f"{game.get('Game Date (ET)', '')}"
            )
            value = tk.BooleanVar(value=True)
            tk.Checkbutton(
                list_frame, text=label, variable=value, anchor="w",
                justify="left", wraplength=500,
            ).pack(fill="x", anchor="w", padx=8, pady=4)
            selected_vars.append((game, value))
        count_label.config(text=f"{len(subset)} games in this slate")

    def set_all(value: bool) -> None:
        for _, variable in selected_vars:
            variable.set(value)

    tk.Button(controls, text="Select all", command=lambda: set_all(True)).pack(side="right", padx=4)
    tk.Button(controls, text="Select none", command=lambda: set_all(False)).pack(side="right", padx=4)

    result: dict[str, list[dict[str, Any]]] = {"games": []}

    def submit() -> None:
        chosen = [game for game, variable in selected_vars if variable.get()]
        if not chosen:
            messagebox.showwarning(
                "No games selected", "Select at least one matchup to continue.", parent=root
            )
            return
        result["games"] = chosen
        root.destroy()

    tk.Button(
        root, text="Analyze selected games", command=submit,
        font=("Arial", 11, "bold"), padx=16, pady=8,
    ).pack(pady=14)
    refresh_games()
    root.mainloop()
    if not result["games"]:
        raise RuntimeError("Game selection was cancelled.")
    return result["games"]
