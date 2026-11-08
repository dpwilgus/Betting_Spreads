# Betting Spreads

A one-click desktop app for Windows and macOS that retrieves the current NFL scoreboard week from ESPN, researches each matchup with the OpenAI API, and exports an Excel workbook with a separate analysis tab for every game.

## Downloads

Download the latest stable version from the repository's **Releases** page:

- **Windows:** `Betting_Spreads.exe`
- **macOS:** `Betting_Spreads-macOS.zip` (contains `Betting_Spreads.app`)

No Python installation or package setup is required for end users.

## First-time AI setup

The AI matchup research feature requires an OpenAI API key and API billing. ChatGPT subscriptions do not include API usage.

1. Create an API key at [OpenAI API keys](https://platform.openai.com/api-keys).
2. Run Betting Spreads.
3. On first launch, paste your API key into the setup prompt.
4. The key is saved in your computer's secure credential store (macOS Keychain or Windows Credential Manager). It is not written into the workbook or repository.

The app uses the OpenAI Responses API with web search to research each matchup. API usage and web-search tool charges are billed to the API account associated with the key. Review current [API pricing](https://developers.openai.com/api/docs/pricing) before use. The number of API research calls generally corresponds to the number of games on the current scoreboard.

To replace a saved key, remove the `Betting Spreads` / `openai_api_key` credential from macOS Keychain Access or Windows Credential Manager, then run the app again. An `OPENAI_API_KEY` environment variable can also be used instead of the saved key.

## How to use

1. Download the file for your operating system from [Releases](../../releases).
2. Windows: double-click `Betting_Spreads.exe`.
3. macOS: unzip the download, then open `Betting_Spreads.app`. On first launch, macOS may require right-clicking the app and choosing **Open** because this app is not signed or notarized.
4. Enter your OpenAI API key on first run.
5. The app retrieves the current ESPN scoreboard week, researches each matchup, and creates or replaces `NFL_Weekly_Matchups.xlsx` beside the executable/app bundle.

## Workbook contents

- **Weekly Matchups:** kickoff in Eastern Time, away team, home team, ESPN betting line, and game status.
- **One tab per matchup:** matchup summary, expected game script, up to five researched player/game prop ideas, up to three touchdown scorer candidates, confidence ratings, model-estimated probabilities, data limitations, and source links.
- The redundant Spread and Sportsbook columns are intentionally omitted from the main schedule sheet.

## Research and probability limitations

- ESPN's public scoreboard endpoint supplies the schedule and available game line. It is unofficial and may change without notice.
- OpenAI web search is used to research current statistics, player usage, injuries, matchup tendencies, weather, and publicly available prop markets.
- A market line is labeled **Not verified** when the research cannot confirm a live sportsbook offer. A model play threshold is an analytical target, not a claim that a sportsbook currently offers that number.
- Estimated hit probabilities and 1–10 confidence ratings are research estimates, not guarantees or independently audited forecasts. Confidence reflects the reliability of available data and the stability of the estimate; it is not the same as probability.
- The analyst is instructed not to invent stats or lines and not to pad the recommendations when evidence is weak. Sources and material data gaps are included in each game tab.
- API use is subject to OpenAI's usage-based pricing. An internet connection is required.

This tool is for research and informational purposes only. It does not guarantee outcomes or constitute financial advice.

## Build locally (developers)

Python 3.10 or newer is recommended.

```bash
python -m pip install -r requirements.txt
python main.py
```

To create a standalone executable, include the agent instruction resources and certificate/keyring dependencies:

Windows:
```bash
pyinstaller --noconfirm --clean --onefile --console --collect-all tzdata --collect-data certifi --collect-all keyring --collect-data agents --name Betting_Spreads main.py
```

macOS:
```bash
pyinstaller --noconfirm --clean --windowed --collect-data certifi --collect-all keyring --collect-data agents --name Betting_Spreads main.py
```

## Local development: reuse saved AI analysis

For local testing only, the Python script supports a cache-only workbook refresh:

1. Run a normal update once. Each successful matchup analysis is saved as JSON in the local `analysis_cache/` folder.
2. Later, run `python main.py --reuse-cache` to fetch the current ESPN scoreboard and rebuild the workbook from those saved analyses without calling the OpenAI API.
3. Cached analyses are clearly marked in the workbook because injuries, player availability, weather, usage, and sportsbook markets are not refreshed.

Use `python main.py --refresh-incomplete` to reuse cached analyses that already contain exactly five prop ideas and three touchdown candidates, while making API calls only for missing or incomplete cache entries. This is useful when importing older analyses or after changing the required output structure. Games that are already live or completed are still skipped.

The first run for a new slate may require API usage for games that have no cache entry. Existing workbooks created before caching do not automatically contain the structured cache. These options are intended for developers and are not exposed in the packaged end-user app.

```bash
python main.py --reuse-cache
python main.py --refresh-incomplete
```

## Publishing a release

Push a version tag (for example, `v2.0.0`) to trigger GitHub Actions. It builds the Windows and macOS downloads and attaches both to a GitHub Release. You can also run the workflow manually from the Actions tab to test builds; manual runs create build artifacts but do not publish a release.

```bash
git tag v2.0.0
git push origin v2.0.0
```
