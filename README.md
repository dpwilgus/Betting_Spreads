# Betting Spreads

A one-click desktop app for Windows and macOS that retrieves the current NFL scoreboard week, independently collects ESPN game and injury information, then uses a configurable AI provider with automatic retries and fallback to analyze each matchup and export an Excel workbook.

## Downloads

Download the latest stable version from the repository's **Releases** page:

- **Windows:** `Betting_Spreads.exe`
- **macOS:** `Betting_Spreads-macOS.zip` (contains `Betting_Spreads.app`)

No Python installation or package setup is required for end users.

## AI providers and first-time setup

AI analysis is provider-independent. The app can use one or more API keys, tries providers in the configured order, retries transient failures, and falls back when a provider is unavailable, rate-limited, or out of quota.

### Free providers (recommended)

1. **Google Gemini API** — default model: `gemini-3.8-flash`. Free-tier token pricing is currently $0 for this model, subject to project/model rate limits. Google says free-tier content may be used to improve its products; do not submit private or sensitive information. See [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) and [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).
2. **GroqCloud** — default model: `openai/gpt-oss-20b`. The Free plan currently includes hosted models with per-model RPM, RPD, and token limits. Check your organization's live [rate limits](https://console.groq.com/docs/rate-limits).
3. **OpenRouter** — default route: `openrouter/free`, which selects an available free model. Free-model requests currently have a 20 RPM / 50 RPD limit for accounts below the $10 lifetime-credit threshold. See [free models](https://openrouter.ai/collections/free-models) and [pricing/limits](https://openrouter.ai/pricing).

Free tiers, model availability, rate limits, and provider terms can change. The app cannot guarantee that a provider's free quota will cover an entire NFL slate. Users should review each provider's terms and dashboard before use.

### Optional paid provider

**OpenAI API** is supported as an optional fallback. ChatGPT subscriptions do not include API usage; OpenAI API usage is billed separately. See [OpenAI API pricing](https://developers.openai.com/api/docs/pricing).

### Create your keys

- Gemini: [Google AI Studio API keys](https://aistudio.google.com/app/apikey)
- Groq: [GroqCloud API keys](https://console.groq.com/keys)
- OpenRouter: [OpenRouter API keys](https://openrouter.ai/settings/keys)
- OpenAI (optional): [OpenAI API keys](https://platform.openai.com/api-keys)

On first run, the app prompts for any combination of these keys. Leave providers you do not want to use blank. Keys are saved in the computer's secure credential store (macOS Keychain or Windows Credential Manager), not in the workbook or repository.

You can also configure environment variables before launching:
- `GEMINI_API_KEY`
- `GROQ_API_KEY`
- `OPENROUTER_API_KEY`
- `OPENAI_API_KEY` (optional; paid)

The default fallback order is Gemini → Groq → OpenRouter → OpenAI. Override it with `AI_PROVIDER_ORDER`, for example `GROQ,OPENROUTER,GEMINI`. Optional model overrides are `GEMINI_MODEL`, `GROQ_MODEL`, `OPENROUTER_MODEL`, and `OPENAI_MODEL`.

### NVIDIA NIM

NVIDIA NIM is not currently included as a production provider. NVIDIA's Developer Program NIM access is for prototyping, research, development, and testing; NVIDIA says serving real end users requires an NVIDIA AI Enterprise license. See [NVIDIA's NIM FAQ](https://docs.api.nvidia.com/nim/docs/product). We may add NIM back as a clearly labeled local-development option if useful.

## How to use

1. Download the file for your operating system from [Releases](../../releases).
2. Windows: double-click `Betting_Spreads.exe`.
3. macOS: unzip the download, then open `Betting_Spreads.app`. On first launch, macOS may require right-clicking the app and choosing **Open** because this app is not signed or notarized.
4. Enter one or more provider API keys on first run (Gemini, Groq, or OpenRouter are the recommended free options).
5. The app retrieves the current ESPN scoreboard week, researches each matchup, and creates or replaces `NFL_Weekly_Matchups.xlsx` beside the executable/app bundle.

## Workbook contents

- **Weekly Matchups:** kickoff in Eastern Time, away team, home team, ESPN betting line, and game status.
- **One tab per matchup:** matchup summary, expected game script, up to five researched player/game prop ideas, up to three touchdown scorer candidates, confidence ratings, model-estimated probabilities, data limitations, and source links.
- The redundant Spread and Sportsbook columns are intentionally omitted from the main schedule sheet.

## Research and probability limitations

- ESPN's public scoreboard endpoint supplies the schedule and available game line. It is unofficial and may change without notice.
- ESPN game summaries and available team injury endpoints are fetched independently of the AI provider. The selected AI provider analyzes this evidence; it does not automatically have web search. Current player-prop prices must be independently present in the evidence to be labeled verified.
- A market line is labeled **Not verified** when the research cannot confirm a live sportsbook offer. A model play threshold is an analytical target, not a claim that a sportsbook currently offers that number.
- Estimated hit probabilities and 1–10 confidence ratings are research estimates, not guarantees or independently audited forecasts. Confidence reflects the reliability of available data and the stability of the estimate; it is not the same as probability.
- The analyst is instructed not to invent stats or lines and not to pad the recommendations when evidence is weak. Sources and material data gaps are included in each game tab.
- API use is subject to each selected provider's free-tier limits or paid pricing. An internet connection is required. Provider fallback does not combine quotas and cannot guarantee uninterrupted free access.

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
2. Later, run `python main.py --reuse-cache` to fetch the current ESPN scoreboard and rebuild the workbook from those saved analyses without calling any AI API.
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
