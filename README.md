# Betting Spreads

A simple double-click desktop app that retrieves the current NFL scoreboard week from ESPN and exports matchups and available betting spreads to an Excel workbook.

## Downloads

Download the latest version from the repository's **Releases** page:

- **Windows:** `Betting_Spreads.exe`
- **macOS:** `Betting_Spreads-macOS.zip` (contains `Betting_Spreads.app`)

No Python installation or package setup is required for end users.

## How to use

1. Download the file for your operating system from [Releases](../../releases).
2. Windows: double-click `Betting_Spreads.exe`. A small console window displays the result.
3. macOS: unzip the download, then open `Betting_Spreads.app`. On first launch, macOS may require right-clicking the app and choosing **Open** because this app is not signed or notarized.
4. The app retrieves the current NFL scoreboard week and creates or replaces `NFL_Weekly_Matchups.xlsx` in the same folder as the executable (Windows) or beside the app bundle (macOS).

The workbook includes kickoff time in Eastern Time, away and home teams, the available betting line and spread, sportsbook/provider when supplied, and game status.

## Data and odds limitations

- Schedule and odds are retrieved at run time from ESPN's public scoreboard endpoint. This is an unofficial endpoint and may change without notice.
- Betting odds are included only when ESPN returns them. Some games may show **Odds unavailable**.
- The first odds entry returned by ESPN is used; it is not necessarily the best price or a particular sportsbook's latest line.
- An internet connection is required.
- This tool is for informational purposes and does not provide betting advice.

## Build locally (developers)

Python 3.10 or newer is recommended.

```bash
python -m pip install -r requirements.txt
python main.py
```

To create a standalone executable on your own operating system:

```bash
pyinstaller --noconfirm --clean --onefile --console --name Betting_Spreads main.py
```

For macOS, use `--windowed` instead to build a double-clickable app bundle:

```bash
pyinstaller --noconfirm --clean --windowed --name Betting_Spreads main.py
```

## Publishing a release

Push a version tag (for example, `v1.0.0`) to trigger GitHub Actions. It builds the Windows and macOS downloads and attaches both to a GitHub Release. You can also run the workflow manually from the Actions tab to test builds; manual runs create build artifacts but do not publish a release.

```bash
git tag v1.0.0
git push origin v1.0.0
```
