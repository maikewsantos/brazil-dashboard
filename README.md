# Brazil Macro Dashboard

Twelve Brazilian macro indicators pulled directly from primary sources and refreshed automatically by GitHub Actions (weekdays, 11:17 and 21:17 UTC). The dashboard is published with GitHub Pages.

| Indicator | Source | Series |
|---|---|---|
| Nominal exchange rate (BRL per USD) | BCB SGS | 1 (PTAX selling) |
| Real effective exchange rate | BCB SGS | 11752, rebased to 2010 = 100 |
| BRL realized volatility | computed from SGS 1 | 21-day rolling, annualized |
| IPCA, 12-month change, vs. target | IBGE SIDRA | table 1737, variable 2265 |
| Inflation expectations, 12 months ahead | BCB Focus (Olinda) | median, smoothed |
| Selic target | BCB SGS | 432 |
| Ex-ante real policy rate | derived | (1+Selic)/(1+expected IPCA) - 1 |
| Carry: Selic minus Fed funds | BCB SGS 432; FRED | DFEDTARU, DFEDTARL (midpoint) |
| IBC-Br, y/y | BCB SGS | 24363 (not seasonally adjusted) |
| Unemployment | IBGE SIDRA | table 6381, variable 4099 |
| Current account and FDI, % GDP | BCB SGS | 23079, 22885, 24419 |
| Gross debt and primary balance, % GDP | BCB SGS | 13762, 5793 (sign flipped) |

## How it works

- `scripts/fetch.py` downloads every series, applies the transformations, validates each one (minimum observations, plausible ranges, maximum age of the latest observation) and writes `data/series.json` and `data/summary.json`.
- If a source or a check fails, the last good data for that indicator is kept and flagged **stale**, and the workflow run is marked failed so GitHub sends a failure email.
- `index.html` is a static page that reads `data/series.json`. It shows the time of the last successful refresh separately from each chart's latest observation date.

Run locally: `pip install -r requirements.txt && python scripts/fetch.py`, then serve the folder (`python -m http.server`).
