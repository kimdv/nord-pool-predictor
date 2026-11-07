What this repo does: It collects Danish electricity prices and weather/production data, trains price models, and serves forecasts through a dashboard and Home Assistant API. This audit assumes the documented local NAS deployment: one worker, a shared PostgreSQL database, limited container memory, and a few browser/API clients on a trusted network.

Audit date: 7 November 2026. Baseline: `ae712eb`.

## Plan and scope

Three research agents traced separate flows: API/database/worker/ingestion/tariffs; training/features/artifacts/scoring/experiments; and dashboard/proxy/browser state. The main agent checked deployment, CI, dependencies, and validation. Research used official upstream documentation and live tariff records. Confirmed fixes receive independent Ponytail Review and Review Agent reviews before publication, then Copilot review and CI before merge.

The audit covered the application modules, entry points, migrations, tests, scripts, Compose/Docker configuration, workflows, README, and recovery guide. It concentrated on data availability, time zones, price arithmetic, cancellation, shared process state, model promotion, and browser trust boundaries. It did not attempt a line-by-line proof of every test or third-party package.

## Must fix

1. **Another website can start or cancel jobs** (`web/app/api/backend/[...path]/route.ts`, `backend/src/nordpool_predictor/api/routers/jobs.py`)
   - **What this is:** Browser POST requests start training and ingestion or cancel running work.
   - **Problem:** Both endpoints accepted a foreign Origin. A browser form could submit a cancellation without reading the response; CORS does not prevent that submission.
   - **Fix:** Reject foreign browser origins at both entry points. Keep same-origin requests, explicitly configured backend dashboard origins, and command-line clients working.
   - **If we skip it:** Visiting another website can interrupt work or queue expensive jobs. This protection is separate from authentication; the documented trusted-network restriction still applies.

2. **Weather appears available before it arrives** (`backend/src/nordpool_predictor/ingestion/weather.py`)
   - **What this is:** Historical training uses the stored weather issuance timestamp to decide what information was available.
   - **Problem:** All responses received the rounded batch-start time. A response received at 13:22 could be used by a simulated forecast issued at 13:20.
   - **Fix:** Stamp each response after receipt, without rounding. Add a clock-controlled regression for sequential responses.
   - **If we skip it:** Historical evaluation can benefit from future information and overstate model quality. Old receipt times cannot be reconstructed from the existing rows.

3. **Current tariffs disappear from selectors** (`backend/src/nordpool_predictor/tariffs.py`)
   - **What this is:** The dashboard lists grid companies and their tariff codes from DataHub.
   - **Problem:** A six-month start-date filter excluded older tariffs that remain valid, and a 200-row code limit could omit choices. Live records included Elektrus code `6000114`, valid 17 April 2026 through 1 January 2027, outside the old date window.
   - **Fix:** Retrieve the history through the requested date without the row cap, filter by ValidFrom/ValidTo, and key selector caches by Copenhagen date. Reuse the existing breakdown validity rule.
   - **If we skip it:** Users cannot select some current tariffs and cannot obtain their complete electricity price.

4. **Single-area deployments show the wrong zone** (`web/components/dashboard-content.tsx`, `web/components/area-switcher.tsx`)
   - **What this is:** The dashboard chooses the bidding zone for prices and charts.
   - **Problem:** A DK2-only installation fell back to DK2 data while retaining DK1 labels and chart requests. A saved preference for a removed area caused the same mismatch.
   - **Fix:** Resolve the selected area against the configured list and use that resolved area for labels, requests, and selector choices.
   - **If we skip it:** Users can see one zone's price labelled as another zone, alongside failing charts.

5. **Database daylight-saving offsets erase training rows** (`backend/src/nordpool_predictor/ml/asof.py`, `backend/src/nordpool_predictor/ml/features.py`)
   - **What this is:** Pandas reads timestamped price and input rows from PostgreSQL into UTC-indexed training data.
   - **Problem:** With PostgreSQL set to Europe/Copenhagen, a query spanning the clock change returns mixed +01:00/+02:00 offsets. Default date parsing turns later rows into missing timestamps, so joins lose otherwise valid data.
   - **Fix:** Explicitly parse database timestamps as UTC. Exercise both clock changes with a Copenhagen database session so UTC-based CI also tests this boundary.
   - **If we skip it:** Training silently loses price and input history across seasonal clock changes on non-UTC database installations.

## Should fix

6. **Weather stops before the forecast horizon ends** (`backend/src/nordpool_predictor/ingestion/weather.py`)
   - **What this is:** Price prediction needs weather inputs for an eight-day horizon.
   - **Problem:** Open-Meteo defaults to seven calendar days starting at midnight. An afternoon issuance therefore runs out of weather well before the price forecast ends.
   - **Fix:** Request ten calendar days, covering eight days from late-day issuance with a buffer. Explicitly retain the historical km/h wind units so existing models keep their input scale.
   - **If we skip it:** The end of the price forecast loses weather inputs even when the upstream service can provide them.

7. **Development tooling includes an advisory-affected matcher** (`web/package.json`, `web/package-lock.json`)
   - **What this is:** ESLint and TypeScript tooling use minimatch to match file patterns.
   - **Problem:** The lockfile resolved minimatch 10.2.6, covered by GHSA-g3hv-75wg-m249 for excessive work on crafted patterns. Nine npm alerts trace back to this single development dependency; this is not a demonstrated runtime dashboard exploit.
   - **Fix:** Override minimatch to the patched 11.0.5 release and validate the actual lint/build workflows. Remove the override when direct dependencies support a patched release themselves.
   - **If we skip it:** The documented denial-of-service issue remains in development tooling that processes matching patterns.

8. **Model tuning searches an inactive setting** (`backend/src/nordpool_predictor/ml/train.py`)
   - **What this is:** Optuna searches LightGBM settings during retraining.
   - **Problem:** It sampled `subsample`, but the default `subsample_freq=0` disables row sampling. This search dimension did not affect fitting.
   - **Fix:** Delete the unused suggestion. Keep saved experiment parameters readable and reproducible.
   - **If we skip it:** Tuning explores a meaningless dimension and records misleading sampling settings.

9. **Two replaced components remain in the tree** (`web/components/forecast-chart.tsx`, `web/components/forecast-info-bar.tsx`)
   - **What this is:** Older forecast chart and status displays were replaced by the current price chart and job status.
   - **Problem:** Whole-tree searches found definitions but no imports, dynamic references, configuration references, or tests using either component.
   - **Fix:** Delete both files, removing 163 lines without replacement.
   - **If we skip it:** Future maintenance retains duplicate display code that users never see.

## Research and boundaries

- [Next.js security guidance](https://nextjs.org/blog/security-nextjs-server-components-actions): custom Route Handlers require their own protection against cross-site request forgery (unwanted browser commands).
- [Open-Meteo forecast documentation](https://open-meteo.com/en/docs): seven-day default, extended forecast windows, and default km/h wind units. Changing old wind values or the model feature scale needs a separate versioned migration; this fix preserves compatibility despite the misleading `_ms` column name.
- [DataHub tariff metadata](https://api.energidataservice.dk/meta/dataset/DatahubPricelist) and [Energi Data Service API guide](https://www.energidataservice.dk/guides/api-guides): date validity, response fields, and request limits. A suspected dynamic-date syntax issue was excluded after the live API accepted it.
- [LightGBM regressor documentation](https://lightgbm.readthedocs.io/en/stable/pythonapi/lightgbm.LGBMRegressor.html): row sampling requires an enabled sampling frequency.
- [Pandas datetime documentation](https://pandas.pydata.org/docs/reference/api/pandas.to_datetime.html): mixed daylight-saving offsets require explicit UTC normalization.
- [Matcher security advisory](https://github.com/advisories/GHSA-g3hv-75wg-m249): affected versions and patched release.

The existing recovery guide explicitly requires trusted-network access; adding public authentication was outside this deployment's stated scope. Existing frozen experiments and production models remain intact. Improving predictive accuracy still requires the documented prospective holdout and target-NAS resource approval; unit tests cannot establish those results.

Verdict: Fix the browser command boundary, weather availability, tariff discovery, area selection, and timestamp parsing first; all nine findings have corresponding changes in this audit branch.

Lean: -164 lines of obsolete components and inactive tuning configuration; no direct dependencies removed.

Not checked: Live NAS deployment, restore execution, target-hardware memory/queue measurements, and prospective model accuracy. Validation results and review status are recorded in the accompanying pull request.
