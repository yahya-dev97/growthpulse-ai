# GrowthPulse AI dashboard (synthetic demo)

A static, dependency-free dashboard for GrowthPulse AI. It has four screens:
- **Performance Overview**;
- **AI Insights & Experiments**;
- **Accounts & Sync**;
- **Reports**.

It runs entirely locally. No database, credentials or advertising APIs are involved.

## Run it

From the repository root:

```bash
python3 -m http.server 8765 --directory frontend
```

Then open <http://127.0.0.1:8765/>. A server is needed because the page loads its data with `fetch`, which a `file://` URL doesn't allow.

**Deep links**, useful for screenshots:
- `#overview`, `#insights`, `#accounts` or `#reports` choose the screen;
- `?client=examplescan-uk`, `exampleclinic-ie` or `examplescan-eu` chooses the client;
- `?account=<external account ID>` limits the scope to one account.

For example, <http://127.0.0.1:8765/?client=examplescan-eu#overview> shows the client with three currencies. The theme follows your OS setting. The sun button toggles it and remembers the choice.

## Where the numbers come from

Every figure is computed, not typed in. `tools/build_dashboard_data.py` runs the real backend code on the deterministic synthetic dataset:
- the synthetic connectors fetch each account;
- `analyse_client` builds each client's report and account breakdown;
- daily series follow the same rules as the analytics, with delivery deduplicated per campaign-day and conversions only ever grouped within one conversion basis.

The legacy client's report is the committed sample report, `feb1e176…`. The script refuses to write anything if it isn't.

Two sections are **illustrative**. They're flagged in the data and labelled on screen:
- **Sync status.** No database is connected, so watermarks come from the dataset itself.
- **The experiment workflow.** Drafts are deterministic templates derived from the findings, not AI output. Promoting and approving them only changes page state, which resets on reload. Every draft requires human approval, and nothing can change a campaign.

To regenerate the data after a backend change:

```bash
uv run python frontend/tools/build_dashboard_data.py
```

## Test it

```bash
uv run pytest tests/test_dashboard_data.py   # data integrity (also runs in the normal suite)
node --test frontend/tests/dashboard.test.mjs  # UI smoke tests in headless Chrome
```

- **Data tests** check that:
  - the committed file is exactly what the pipeline produces now;
  - the legacy report ID is `feb1e176…`;
  - daily series add up to the report totals per currency and conversion basis;
  - findings stay within their client;
  - the illustrative sections are flagged.
- **UI tests** need Python 3 and Google Chrome, and skip if Chrome is missing. Set `CHROME_PATH` if it's in a non-standard place. They check that:
  - all four screens render;
  - navigation and deep links work;
  - the multi-currency client shows one portfolio per currency;
  - account scope works;
  - the demo workflow is labelled.

## Portfolio screenshots

`showcase.html` is a separate, fixed 1920 × 1080 canvas in dark mode. It reuses the dashboard's chart code, design tokens and data file. It has two layouts:
- `showcase.html#performance`: performance intelligence;
- `showcase.html#findings`: analytical findings to experiment drafts.

The four dashboard screens are unchanged.

To regenerate both PNGs into `portfolio/screenshots/`:

```bash
node frontend/tools/export_showcase.mjs
```

The exporter works in four steps:
1. It pins the viewport to exactly 1920 × 1080 through the Chrome DevTools Protocol.
2. It checks that the layout reports no overflow (`data-fits="true"`). If it does report overflow, the exporter **refuses to save**.
3. It captures the screen.
4. It checks each PNG's real dimensions.

**Provenance.** Every value comes from `data/dashboard_data.json`, which the pipeline generates. The showcase selects and formats values but never computes new ones. `node --test frontend/tests/showcase.test.mjs` checks that:
- the key values (spend, clicks, each conversion basis, the three findings) match the pipeline's output;
- every disclosure is present;
- no unsupported AI claim appears.

The findings shown are the strongest high-severity change for each of three detection rules, among those with a draft. The zero-conversion rule is stated by its own evidence (the run of days with spend and no conversions), not by a percentage.

## Structure

| Path | Role |
|---|---|
| `index.html`, `styles.css` | Page shell and design tokens: a validated chart palette, with light and dark modes chosen separately |
| `js/app.js` | State, screens and the one data adapter (`loadDashboardData`) |
| `js/charts.js` | SVG line charts with a crosshair, bar charts and sparklines. Every chart has a table view |
| `data/dashboard_data.json` | Generated. Don't edit by hand |
| `tools/build_dashboard_data.py` | Generates the data from the synthetic pipeline |

**Connecting to the backend later.** Replace `loadDashboardData()` with an adapter that returns the same shape from the Phase C read-only views, through the `growthpulse_dashboard` role on a server. The screens don't need to change.
