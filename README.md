# MACROSCOPE

**MACROSCOPE v4.4** is a static, terminal-style U.S. economic outlook simulator and release-aware nowcasting dashboard. It runs on GitHub Pages, updates through GitHub Actions, and uses generated JSON snapshots so no API key is exposed to site visitors.

## Live site

🌐 **[Open MACROSCOPE](https://YOUR-GITHUB-USERNAME.github.io/YOUR-REPOSITORY/)**

Replace `YOUR-GITHUB-USERNAME` and `YOUR-REPOSITORY` with the values from your GitHub Pages URL after deployment.

> MACROSCOPE is an educational and analytical model. Its forecasts, risk scores, and shock effects are not official forecasts, investment advice, or causal estimates.

## Current capabilities

### Terminal overview

- Bloomberg-inspired startup sequence and information-dense interface
- Live-baseline dashboard with GDP, inflation, unemployment, recession risk, and Treasury yields
- Economic-regime classification
- What Changed attribution
- Upcoming-release panel
- GDP driver waterfall
- Persistent macroeconomic risk monitor

### Scenario model

- More than 70 economic, policy, market, institutional, and geopolitical inputs
- One-year, three-year, and ten-year horizons
- Monetary, fiscal, innovation, bond, trade, housing, labor, energy, infrastructure, state, political-economy, and geopolitical modules
- Taylor Rule mode
- Country presets and comparison
- Scenario saving, sharing, JSON export, CSV export, and undo/redo

### Phase A — structured shock engine

- Adjustable shock magnitude
- Temporary, persistent, and structural shock classes
- Onset, lag, peak, duration, and decay
- State-dependent interactions
- Diminishing overlap weights to reduce double counting
- Shock-attribution inspector
- Structured custom-shock builder

### Phase B — official-data synchronization

- Official FRED observations are downloaded by GitHub Actions
- Compatible observations update the U.S. live baseline
- GDP, CPI, Treasury yields, and mortgage rates act as transparent output anchors
- User scenario deviations are preserved when the official baseline updates
- Current, previous, revision, freshness, and provenance information are displayed

### Phase C — release-surprise engine

Phase C compares a released value with MACROSCOPE's pre-release forecast and converts the standardized surprise into a temporary, decaying information shock.

```text
Official observation
        ↓
MACROSCOPE expected value
        ↓
Actual − expected
        ↓
Normalize by rolling forecast error
        ↓
Release-specific transmission channels
        ↓
GDP, inflation, rates, confidence, markets, and recession risk respond
```

The first Phase C release families are:

- Employment Situation
- Consumer Price Index
- Gross Domestic Product
- Retail Sales
- Personal Income and Outlays

The release engine includes:

- Transparent weighted three-period forecasts
- Historical one-step backtests
- Mean absolute error and root mean squared error
- Directional accuracy
- Standardized surprises measured in forecast-error standard deviations
- Versioned release IDs
- Revision replacement rather than duplicate application
- Decay through the Phase A timing system
- Supersession weights so newer releases gradually replace older information
- A Release Surprises page with expected, actual, raw surprise, standardized surprise, and model effects
- A toggle to compare the model with release signals enabled or disabled

## Forecast methodology

The initial forecasting method is deliberately simple and auditable:

```text
Expected next value =
55% × most recent prior value
+ 30% × second prior value
+ 15% × third prior value
```

For a newly released observation:

```text
Raw surprise = Actual − Expected

Standardized surprise =
Raw surprise ÷ rolling historical RMSE
```

The standardized surprise is capped at ±3.0 standard deviations to reduce the effect of extreme data errors and unstable early samples.

Transmission coefficients are defined explicitly in `SURPRISE_SERIES_SPECS` inside `scripts/update_calendar.py`. They are expert-prior scenario coefficients, not estimated causal relationships. Each generated JSON shock exposes:

- The forecast
- The actual value
- Forecast error scale
- Standardized surprise
- Confidence
- Duration and decay
- Every target and coefficient

## Official series

The updater retrieves a core set of official series, including:

- Real GDP growth
- Headline CPI
- Core CPI
- Unemployment
- Labor-force participation
- Nonfarm payroll change
- Effective federal funds rate
- Interest on reserve balances
- Two-year Treasury yield
- Ten-year Treasury yield
- Thirty-year mortgage rate
- Housing starts
- WTI crude oil
- PCE inflation
- Advance retail sales
- Real personal-consumption growth

Series availability may vary. A failed individual series can fall back to the previous valid snapshot without preventing the rest of the deployment.

## Repository structure

```text
.
├── .github/
│   └── workflows/
│       └── static.yml
├── data/
│   └── economic-calendar.json
├── scripts/
│   └── update_calendar.py
├── index.html
└── README.md
```

Keep these paths at the repository root. Do not place them inside an extra folder such as `macroscope_phaseC_update/`.

## Installation

### 1. Replace the files together

Phase C changes the frontend, updater, workflow validation, and JSON schema. Replace these files in one commit:

```text
index.html
scripts/update_calendar.py
data/economic-calendar.json
.github/workflows/static.yml
README.md
```

Suggested commit message:

```text
Implement Phase C release surprise engine
```

### 2. Create a free FRED API key

Request a key from the official FRED API site:

<https://fred.stlouisfed.org/docs/api/api_key.html>

### 3. Store the key as a GitHub Actions secret

In the repository:

```text
Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

Name it exactly:

```text
FRED_API_KEY
```

Never place the key in `index.html`, committed JavaScript, or the public JSON file.

### 4. Configure Pages

```text
Settings
→ Pages
→ Build and deployment
→ Source
→ GitHub Actions
```

Use only one Pages deployment workflow. The included `static.yml` both generates the data and deploys the site.

### 5. Run the first update manually

```text
Actions
→ Update economic calendar and deploy Pages
→ Run workflow
→ main
→ Run workflow
```

The workflow will:

1. Validate the repository structure.
2. Set up Python.
3. Download the FRED release calendar.
4. Download mapped official series.
5. Calculate forecasts and historical forecast errors.
6. Generate versioned release surprises and decaying shock channels.
7. Write `data/economic-calendar.json` using schema version 3.
8. Upload and deploy the GitHub Pages artifact.

## Automatic updates

The included workflow runs:

- On pushes to `main`
- When manually triggered
- Once daily
- Several additional times on weekdays

GitHub Actions schedules are not guaranteed to run at the exact scheduled minute. The site displays the snapshot generation time so visitors can judge freshness.

## Running locally

The updater uses only the Python standard library.

### macOS or Linux

```bash
export FRED_API_KEY="your_fred_api_key"
python3 scripts/update_calendar.py
```

### PowerShell

```powershell
$env:FRED_API_KEY="your_fred_api_key"
python scripts/update_calendar.py
```

Then serve the repository through a local HTTP server rather than opening `index.html` directly:

```bash
python3 -m http.server 8000
```

Open:

<http://localhost:8000>

## Generated JSON structure

The browser reads:

```text
data/economic-calendar.json
```

The top-level objects are:

```json
{
  "events": [],
  "officialData": {},
  "surpriseEngine": {}
}
```

### `events`

Contains historical and upcoming release dates, official observations attached to the latest released occurrence, model forecasts attached to the next occurrence, and release-surprise summaries.

### `officialData`

Contains the latest observation, previous period, initial-release comparison, revision, age, stale status, source, and model mapping for each series.

### `surpriseEngine`

Contains:

- Active and decaying release shocks
- Stable observation-based shock IDs
- Version IDs for revision handling
- Forecasts and standardized surprises
- Release-specific transmission channels
- Model confidence and timing parameters
- Forecast performance by series

## Revision handling

A release shock has a stable ID based on its series and observation period:

```text
release-unemployment-2026-07-01
```

Its version ID changes if the actual value or revision changes. The updater replaces the existing version rather than adding the release again. The JSON also records the incremental revision effects.

## Data freshness and fallback behavior

The updater:

- Splits the calendar into smaller date windows
- Retries timeouts, HTTP 429 responses, and temporary server errors
- Uses exponential backoff
- Writes the JSON atomically
- Preserves a previous valid calendar after a temporary calendar failure
- Preserves individual prior observations when one series is unavailable
- Does not replace a valid snapshot with an empty result

## Troubleshooting

### `FRED_API_KEY is missing or empty`

Confirm the repository secret is named exactly `FRED_API_KEY` under Actions secrets.

### `scripts/update_calendar.py is missing`

The script must be located at the repository-root path:

```text
scripts/update_calendar.py
```

### The workflow still says “Deploy static content to Pages”

Replace the old default Pages workflow with the included combined `.github/workflows/static.yml`. Keep only one workflow that deploys Pages.

### The workflow reports a timeout

Run it again. The updater retries and preserves valid prior data when possible. The first successful run cannot use a fallback because no prior generated snapshot exists.

### Official data load, but release signals are empty

Confirm the generated JSON reports:

```json
"schemaVersion": 3
```

and includes a nonempty `surpriseEngine.shocks` array. Some signals may be absent until the mapped series have enough observations for a forecast and backtest.

### Metrics do not appear to move

Open **Release Surprises** and confirm **Release signals: ON**. Signals also decay over time and may be very small if the actual release was close to the model forecast.

### The page shows old information

Check `generatedAt` in `data/economic-calendar.json`, inspect the workflow logs, and hard-refresh the site with `Ctrl+Shift+R` or `Cmd+Shift+R`.

## Security model

GitHub Pages is static and cannot safely hide private API keys in frontend code.

The safe architecture is:

```text
FRED API
   ↓
GitHub Actions runner using encrypted FRED_API_KEY
   ↓
Public generated JSON snapshot
   ↓
GitHub Pages browser application
```

The browser never receives the FRED key.

## Attribution

This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.

Release dates originate with statistical agencies and may change. FRED calendar dates do not guarantee that a value will be available on FRED at the exact listed time.

## License

Add the license appropriate for your repository. Confirm that any redistributed third-party datasets, fonts, libraries, or content permit your intended use.
