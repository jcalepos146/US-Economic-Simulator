# MACROSCOPE Phase B — Official Data Synchronization

Phase B upgrades the generated economic-calendar snapshot into a combined release-calendar and official-observation feed. GitHub Actions uses the repository's `FRED_API_KEY`; GitHub Pages visitors receive only the generated JSON.

## Replace these repository files

```text
index.html
scripts/update_calendar.py
data/economic-calendar.json
.github/workflows/static.yml
```

The checked-in JSON is an empty schema placeholder. Run the workflow once after committing the files to populate it.

## Data flow

```text
FRED release calendar + FRED series observations
                    ↓
       scripts/update_calendar.py
                    ↓
       data/economic-calendar.json
                    ↓
        static GitHub Pages website
```

No FRED key is placed in `index.html` or sent to the browser.

## Mapped official series

| MACROSCOPE field | FRED series | Behavior |
|---|---|---|
| Real GDP growth | `A191RL1Q225SBEA` | Output anchor |
| Headline CPI, YoY | `CPIAUCSL` | Output anchor |
| Core CPI, YoY | `CPILFESL` | Displayed observation |
| Unemployment | `UNRATE` | Direct slider input |
| Labor-force participation | `CIVPART` | Direct slider input |
| Monthly payroll change | `PAYEMS` | Displayed observation |
| Effective federal funds rate | `DFF` | Direct slider input |
| Interest on reserve balances | `IORB` | Direct slider input |
| Two-year Treasury yield | `DGS2` | Output anchor |
| Ten-year Treasury yield | `DGS10` | Output anchor |
| Thirty-year mortgage rate | `MORTGAGE30US` | Output anchor |
| Housing starts | `HOUST` | Direct slider input, converted to millions |
| WTI crude oil | `DCOILWTICO` | Direct slider input |
| PCE inflation, YoY | `PCEPI` | Displayed observation |

## How synchronization works

### Direct input mappings

Compatible observations set model sliders directly:

- Unemployment
- Labor-force participation
- Effective federal funds rate
- Interest on reserve balances
- Housing starts
- WTI oil

The sliders display their FRED series, observation date, and provenance. Moving a synchronized slider changes its label to **User Override** while preserving the official value as its reset point.

### Output anchors

Observed GDP, CPI, two-year and ten-year Treasury yields, and the mortgage rate are converted into transparent additive calibration offsets. The structural model still determines how scenarios move away from those observed values.

Calibration is sequential:

1. CPI
2. Treasury yields
3. Mortgage rate
4. GDP after yield, spillover, and wealth-effect feedback

The GDP waterfall identifies the residual as **Official Data Calibration**.

### New snapshot rebasing

When a refreshed JSON snapshot arrives while official synchronization is active:

1. MACROSCOPE replaces the previous official baseline.
2. User changes are measured as deviations from the old baseline.
3. Those deviations are reapplied to the new baseline.
4. Output anchors are recalculated.

Example: if official unemployment moves from 4.3% to 4.5% while the user has added a +0.7 percentage-point labor shock, the scenario moves from 5.0% to 5.2%, not back to 4.5%.

## Official Data page

The new **Official Data** section displays:

- Current observation
- Previous observation period
- Period-over-period change
- Revision versus FRED's initial-release value, when available
- Observation date
- Frequency and units
- Freshness or stale fallback status
- Direct-input or output-anchor mapping

## Calendar enrichment

The most recent released occurrence of mapped releases can display associated values. For example:

- Employment Situation → unemployment, participation, payroll change
- CPI → headline and core CPI
- GDP → real GDP growth
- New Residential Construction → housing starts

Future releases continue to display as scheduled events.

## Partial-update resilience

Calendar requests retain the longer timeout and retry policy. Individual official-series requests use shorter retry limits so one unavailable series does not consume the entire workflow runtime.

When a series fails:

- The updater uses that series from the previous JSON snapshot when available.
- It marks the observation as a fallback.
- Other series continue updating.
- The overall official-data status becomes `partial`.

If the entire release-calendar call fails but the previous JSON has events, the prior calendar is retained while the series updater continues.

## Running the workflow

1. Commit the replacement files to `main`.
2. Confirm the repository secret is named `FRED_API_KEY`.
3. Open **Actions**.
4. Select **Update economic calendar and deploy Pages**.
5. Select **Run workflow**.
6. Open the deployed site after the run completes.

The log now reports both calendar-event count and official-series count.

## JSON schema additions

The generated file uses top-level `schemaVersion: 2` and adds:

```json
{
  "officialData": {
    "status": "complete",
    "seriesCount": 14,
    "baseline": {
      "inputs": {},
      "outputs": {}
    },
    "observations": {}
  }
}
```

Each observation includes its series ID, current value, previous period, optional initial-release value and revision, mapping target, observation date, units, and freshness metadata.

## Important interpretation

- “Previous” means the preceding observation period, not analyst consensus.
- “Revision” means current value minus FRED's initial-release value when the endpoint returns one.
- Output anchoring does not make the model statistically estimated; it aligns the structural scenario model with the latest observed state.
- The recession-risk figure remains a deterministic model score.
