"""Fetch, transform and validate Brazil dashboard indicators.

All data comes straight from primary sources:
  - Banco Central do Brasil (BCB): SGS time-series API and Focus survey (Olinda) API
  - IBGE: SIDRA API
  - Federal Reserve Board, via FRED (St. Louis Fed) for the US policy rate

Output: data/series.json (dashboard input) and data/summary.json (changes since
previous run, used by the email update). If a source fails or a series fails
validation, the previous good data for that series is kept and flagged stale.
"""

from __future__ import annotations

import io
import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SERIES_PATH = DATA_DIR / "series.json"
SUMMARY_PATH = DATA_DIR / "summary.json"

SAMPLE_START = date(2010, 1, 1)
FETCH_START = date(2009, 1, 1)  # one extra year so y/y and 12m transforms cover 2010
HEADERS = {"User-Agent": "brazil-dashboard (github.com/maikewsantos/brazil-dashboard)"}

SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
SIDRA_URL = "https://apisidra.ibge.gov.br/values/t/{table}/n1/all/v/{var}/p/all"
FOCUS_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/"
    "ExpectativasMercadoInflacao12Meses"
)
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

PROBLEMS: list[str] = []


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def get(url: str, params: dict | None = None, tries: int = 4) -> requests.Response:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=60)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:  # network hiccup
            last = repr(e)
        time.sleep(2 ** i * 3)
    raise RuntimeError(f"GET failed for {url} {params or ''}: {last}")


def fetch_sgs(code: int, daily: bool) -> pd.Series:
    """BCB SGS series. Daily series are requested in windows of < 10 years (BCB limit)."""
    end = date.today()
    windows = []
    if daily:
        start = FETCH_START
        while start <= end:
            stop = min(date(start.year + 9, 12, 31), end)
            windows.append((start, stop))
            start = stop + timedelta(days=1)
    else:
        windows.append((FETCH_START, end))
    frames = []
    for a, b in windows:
        r = get(
            SGS_URL.format(code=code),
            {"formato": "json", "dataInicial": a.strftime("%d/%m/%Y"), "dataFinal": b.strftime("%d/%m/%Y")},
        )
        frames.extend(r.json())
    if not frames:
        raise RuntimeError(f"SGS {code}: empty response")
    df = pd.DataFrame(frames)
    s = pd.Series(
        pd.to_numeric(df["valor"], errors="coerce").values,
        index=pd.to_datetime(df["data"], format="%d/%m/%Y"),
    )
    return s[~s.index.duplicated(keep="last")].sort_index().dropna()


def fetch_sidra(table: int, var: int) -> pd.Series:
    """IBGE SIDRA, national level. Period code (D3C) is YYYYMM for monthly tables."""
    rows = get(SIDRA_URL.format(table=table, var=var)).json()
    header, body = rows[0], rows[1:]
    period_key = next(k for k, v in header.items() if v.startswith(("Mês", "Trimestre")) and k.endswith("C"))
    out = {}
    for row in body:
        try:
            val = float(row["V"])
        except (TypeError, ValueError):
            continue  # "..." or "-" for unavailable
        p = row[period_key]
        out[pd.Timestamp(int(p[:4]), int(p[4:6]), 1)] = val
    if not out:
        raise RuntimeError(f"SIDRA {table}/{var}: no numeric observations")
    return pd.Series(out).sort_index()


def fetch_focus_ipca_12m() -> pd.Series:
    """Focus survey: median expected IPCA over the next 12 months (smoothed series)."""
    params = {
        "$filter": f"Indicador eq 'IPCA' and Suavizada eq 'S' and baseCalculo eq 0 and Data ge '{FETCH_START.isoformat()}'",
        "$select": "Data,Mediana",
        "$orderby": "Data",
        "$top": "20000",
        "$format": "json",
    }
    try:
        vals = get(FOCUS_URL, params, tries=2).json()["value"]
    except RuntimeError:
        # Fallback if the baseCalculo field is not accepted by this endpoint
        params["$filter"] = f"Indicador eq 'IPCA' and Suavizada eq 'S' and Data ge '{FETCH_START.isoformat()}'"
        vals = get(FOCUS_URL, params).json()["value"]
        print("[info] Focus: baseCalculo filter not accepted; used fallback filter")
    if not vals:
        raise RuntimeError("Focus: empty response")
    df = pd.DataFrame(vals)
    s = pd.Series(pd.to_numeric(df["Mediana"]).values, index=pd.to_datetime(df["Data"]))
    return s[~s.index.duplicated(keep="last")].sort_index()


def fetch_fred(series_id: str) -> pd.Series:
    r = get(FRED_URL, {"id": series_id, "cosd": FETCH_START.isoformat()})
    df = pd.read_csv(io.StringIO(r.text))
    date_col = df.columns[0]
    s = pd.Series(pd.to_numeric(df[series_id], errors="coerce").values, index=pd.to_datetime(df[date_col]))
    return s.dropna().sort_index()


# --------------------------------------------------------------------------- #
# Transformations
# --------------------------------------------------------------------------- #
def month_end(s: pd.Series) -> pd.Series:
    """Last available observation in each calendar month, indexed at month start."""
    m = s.resample("MS").last().dropna()
    return m


def yoy(s: pd.Series) -> pd.Series:
    """Percent change versus the same month a year earlier (monthly index)."""
    s = s.asfreq("MS")
    return ((s / s.shift(12) - 1) * 100).dropna()


def rebase(s: pd.Series, year: int) -> pd.Series:
    base = s[s.index.year == year].mean()
    return s / base * 100


def to_points(s: pd.Series, digits: int = 6) -> list:
    s = s[s.index >= pd.Timestamp(SAMPLE_START)].dropna()
    return [[d.strftime("%Y-%m-%d"), round(float(v), digits)] for d, v in s.items()]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate(sid: str, s: pd.Series, spec: dict) -> None:
    if s.empty:
        raise ValueError(f"{sid}: no observations")
    n = len(s[s.index >= pd.Timestamp(SAMPLE_START)])
    if n < spec["min_obs"]:
        raise ValueError(f"{sid}: only {n} observations since {SAMPLE_START}, expected >= {spec['min_obs']}")
    lo, hi = spec["bounds"]
    bad = s[(s < lo) | (s > hi)]
    if len(bad):
        raise ValueError(f"{sid}: {len(bad)} values outside plausible range [{lo}, {hi}], e.g. {bad.index[0].date()}={bad.iloc[0]}")
    age = (pd.Timestamp(date.today()) - s.index.max()).days
    if age > spec["max_age_days"]:
        raise ValueError(f"{sid}: latest observation {s.index.max().date()} is {age} days old (limit {spec['max_age_days']})")


# --------------------------------------------------------------------------- #
# Indicator definitions (metadata shown on the dashboard)
# --------------------------------------------------------------------------- #
BCB = "Banco Central do Brasil (SGS)"
SPECS: dict[str, dict] = {
    "ptax": dict(
        title="Nominal exchange rate", group="Exchange rate", units="BRL per USD", frequency="Daily (business days)",
        source=f"{BCB}, series 1: US dollar selling rate (PTAX)",
        convention="Quoted as BRL per 1 USD. A rise means the real DEPRECIATES against the dollar.",
        transformation="None (level as published).",
        min_obs=3000, bounds=(1.0, 15.0), max_age_days=7, digits=4),
    "reer": dict(
        title="Real effective exchange rate", group="Exchange rate", units="Index, 2010 average = 100", frequency="Monthly",
        source=f"{BCB}, series 11752: real effective exchange rate index (IPCA-deflated, broad trade-weighted basket)",
        convention="Direction checked against PTAX in each run; see note on chart.",
        transformation="Rebased from BCB's original base (June 1994 = 100) to 2010 average = 100. Rebasing changes the level only, not percentage changes.",
        min_obs=150, bounds=(20, 400), max_age_days=130, digits=3),
    "ipca": dict(
        title="Inflation vs. target", group="Inflation & monetary policy", units="% (12-month change)", frequency="Monthly",
        source="IBGE, SIDRA table 1737, variable 2265: IPCA accumulated over 12 months",
        convention="Target band: 3.0% centre, tolerance ±1.5 pp (continuous target since Jan 2025, CMN Resolution 5,141/2024). Before 2025 the target was set per calendar year; the band drawn for earlier years shows the target in force that year.",
        transformation="None (IBGE publishes the 12-month change).",
        min_obs=150, bounds=(-5, 30), max_age_days=75, digits=2),
    "focus": dict(
        title="Inflation expectations", group="Inflation & monetary policy", units="% (expected IPCA, next 12 months)", frequency="Weekly (last reading of each week)",
        source="BCB Focus market survey (Olinda API): median of expected IPCA for the next 12 months, smoothed series",
        convention="Median across survey respondents.",
        transformation="BCB publishes daily; the dashboard keeps the last reading of each week.",
        min_obs=600, bounds=(0, 20), max_age_days=14, digits=2),
    "selic": dict(
        title="Policy rate (Selic target)", group="Inflation & monetary policy", units="% per year", frequency="Changes at Copom meetings (shown daily)",
        source=f"{BCB}, series 432: Selic target set by Copom",
        convention="Annualized, 252 business-day basis (BCB convention).",
        transformation="None.",
        min_obs=3000, bounds=(1, 30), max_age_days=7, digits=2),
    "real_rate": dict(
        title="Ex-ante real policy rate", group="Inflation & monetary policy", units="% per year", frequency="Monthly (end of month)",
        source="Derived from BCB series 432 (Selic target) and BCB Focus 12-month IPCA expectations",
        convention="Positive = policy rate above expected inflation (restrictive territory rises with it).",
        transformation="Exact Fisher relation: (1 + Selic) / (1 + expected 12m IPCA) - 1, using end-of-month values.",
        min_obs=150, bounds=(-10, 20), max_age_days=45, digits=3),
    "carry": dict(
        title="Carry: Selic minus Fed funds", group="Inflation & monetary policy", units="Percentage points", frequency="Daily (business days)",
        source=f"{BCB}, series 432; Federal Reserve Board via FRED: federal funds target range (DFEDTARU, DFEDTARL)",
        convention="Positive = BRL short-term rates above USD rates (the return to borrowing USD to hold BRL, before exchange-rate moves).",
        transformation="Selic target minus the midpoint of the Fed funds target range. The US rate is carried forward to Brazilian business days.",
        min_obs=3000, bounds=(-5, 30), max_age_days=7, digits=3),
    "vol": dict(
        title="BRL realized volatility", group="Exchange rate", units="% annualized", frequency="Daily (business days)",
        source="Computed from BCB series 1 (PTAX)",
        convention="Higher = larger day-to-day swings in BRL/USD.",
        transformation="Standard deviation of daily log changes in PTAX over a rolling 21-business-day (about 1 month) window, times the square root of 252, in percent.",
        min_obs=3000, bounds=(0, 150), max_age_days=7, digits=2),
    "ibcbr": dict(
        title="Economic activity (IBC-Br)", group="Activity", units="% change vs. same month a year earlier", frequency="Monthly",
        source=f"{BCB}, series 24363: IBC-Br, not seasonally adjusted",
        convention="Monthly proxy for GDP published by BCB.",
        transformation="Year-over-year % change of the index. The non-seasonally-adjusted index is used because y/y comparisons already remove seasonality.",
        min_obs=150, bounds=(-25, 25), max_age_days=140, digits=2),
    "unemp": dict(
        title="Unemployment rate", group="Activity", units="% of labour force", frequency="Monthly (rolling quarter)",
        source="IBGE, SIDRA table 6381, variable 4099: PNAD Contínua unemployment rate",
        convention="Each point is the 3-month moving quarter ending in that month (IBGE definition). Not seasonally adjusted.",
        transformation="None. Series starts in 2012 (survey start).",
        min_obs=100, bounds=(2, 25), max_age_days=100, digits=1),
    "ca": dict(
        title="Current account and FDI", group="External & fiscal", units="% of GDP, 12-month sum", frequency="Monthly",
        source=f"{BCB}: series 23079 (current account, 12m, % GDP), 22885 (direct investment liabilities, monthly, USD mn), 24419 (current account, 12m, USD mn)",
        convention="Current account: negative = deficit. FDI: inflows of direct investment into Brazil (net incurrence of liabilities).",
        transformation="FDI % GDP = 12-month sum of series 22885 divided by 12-month GDP in USD, where GDP is implied by BCB's own ratio (series 24419 / series 23079 x 100), so both lines share BCB's denominator.",
        min_obs=150, bounds=(-15, 15), max_age_days=100, digits=2),
    "fiscal": dict(
        title="Fiscal: gross debt and primary balance", group="External & fiscal", units="% of GDP", frequency="Monthly",
        source=f"{BCB}: series 13762 (general government gross debt, % GDP, 2008 methodology) and 5793 (consolidated public sector primary result, 12m, % GDP)",
        convention="Primary balance: positive = surplus. Shown as two stacked panels with their own scales (no dual axis).",
        transformation="BCB publishes the primary result as a borrowing requirement (positive = deficit); its sign is flipped so a surplus is positive.",
        min_obs=150, bounds=(-15, 120), max_age_days=100, digits=2),
}


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build_all() -> tuple[dict[str, dict], dict[str, str]]:
    """Returns {sid: {"lines": {name: Series}}} for series that built, and {sid: error}."""
    built, errors = {}, {}
    raw: dict[str, pd.Series] = {}

    def grab(key, fn, *args):
        if key not in raw:
            raw[key] = fn(*args)
        return raw[key]

    def attempt(sid, fn):
        try:
            lines = fn()
            for name, s in lines.items():
                validate(f"{sid}/{name}", s, SPECS[sid])
            built[sid] = lines
            print(f"[ok]   {sid}: " + "; ".join(f"{n} last {s.index.max().date()} = {s.iloc[-1]:.4f}" for n, s in lines.items()))
        except Exception as e:  # noqa: BLE001
            errors[sid] = f"{type(e).__name__}: {e}"
            print(f"[FAIL] {sid}: {errors[sid]}")

    attempt("ptax", lambda: {"BRL per USD": grab("sgs1", fetch_sgs, 1, True)})

    def reer():
        s = grab("sgs11752", fetch_sgs, 11752, False)
        s.index = s.index.to_period("M").to_timestamp()
        return {"REER (2010 = 100)": rebase(s, 2010)}
    attempt("reer", reer)

    attempt("ipca", lambda: {"IPCA, 12-month change": grab("sidra1737", fetch_sidra, 1737, 2265)})

    def focus():
        d = grab("focus", fetch_focus_ipca_12m)
        wk = d.groupby(d.index.to_period("W-FRI")).tail(1)  # last survey reading in each week, at its real date
        return {"Expected IPCA, next 12m (median)": wk}
    attempt("focus", focus)

    attempt("selic", lambda: {"Selic target": grab("sgs432", fetch_sgs, 432, True)})

    def real_rate():
        sel = month_end(grab("sgs432", fetch_sgs, 432, True))
        exp = month_end(grab("focus", fetch_focus_ipca_12m))
        df = pd.concat([sel, exp], axis=1, keys=["i", "pi"]).dropna()
        return {"Ex-ante real Selic": ((1 + df.i / 100) / (1 + df.pi / 100) - 1) * 100}
    attempt("real_rate", real_rate)

    def carry():
        sel = grab("sgs432", fetch_sgs, 432, True)
        up, lo = fetch_fred("DFEDTARU"), fetch_fred("DFEDTARL")
        mid = ((up + lo) / 2).dropna()
        mid = mid.reindex(mid.index.union(sel.index)).ffill().reindex(sel.index)
        return {"Selic minus Fed funds (midpoint)": (sel - mid).dropna()}
    attempt("carry", carry)

    def vol():
        p = grab("sgs1", fetch_sgs, 1, True)
        r = np.log(p).diff()
        return {"21-day realized vol": (r.rolling(21).std() * math.sqrt(252) * 100).dropna()}
    attempt("vol", vol)

    def ibcbr():
        s = grab("sgs24363", fetch_sgs, 24363, False)
        s.index = s.index.to_period("M").to_timestamp()
        return {"IBC-Br, y/y": yoy(s)}
    attempt("ibcbr", ibcbr)

    attempt("unemp", lambda: {"Unemployment rate": grab("sidra6381", fetch_sidra, 6381, 4099)})

    def ca():
        ca_pct = fetch_sgs(23079, False)
        ca_usd = fetch_sgs(24419, False)
        fdi = fetch_sgs(22885, False)
        for s in (ca_pct, ca_usd, fdi):
            s.index = s.index.to_period("M").to_timestamp()
        gdp_usd = (ca_usd / (ca_pct / 100)).replace([np.inf, -np.inf], np.nan)
        fdi12 = fdi.asfreq("MS").rolling(12).sum()
        fdi_pct = (fdi12 / gdp_usd * 100).dropna()
        return {"Current account": ca_pct, "FDI inflows": fdi_pct}
    attempt("ca", ca)

    def fiscal():
        debt = fetch_sgs(13762, False)
        prim = -fetch_sgs(5793, False)
        for s in (debt, prim):
            s.index = s.index.to_period("M").to_timestamp()
        return {"Gross general government debt": debt, "Primary balance (12m)": prim}
    attempt("fiscal", fiscal)

    # Direction check for the REER: correlation of monthly changes with PTAX
    if "reer" in built and "ptax" in built:
        reer_m = list(built["reer"].values())[0]
        ptax_m = month_end(list(built["ptax"].values())[0])
        ptax_m.index = ptax_m.index.to_period("M").to_timestamp()
        both = pd.concat([reer_m.pct_change(), ptax_m.pct_change()], axis=1).dropna()
        corr = both.corr().iloc[0, 1]
        up_dep = corr > 0
        SPECS["reer"]["convention"] = (
            ("A rise means the real DEPRECIATES in real terms (Brazil becomes cheaper relative to trading partners)."
             if up_dep else
             "A rise means the real APPRECIATES in real terms (Brazil becomes more expensive relative to trading partners).")
            + f" Verified each run from the correlation of monthly changes with PTAX (BRL/USD): {corr:.2f}."
        )
        print(f"[info] REER vs PTAX monthly-change correlation = {corr:.3f} -> up = {'depreciation' if up_dep else 'appreciation'}")

    return built, errors


def main() -> int:
    DATA_DIR.mkdir(exist_ok=True)
    prev = json.loads(SERIES_PATH.read_text()) if SERIES_PATH.exists() else {"series": {}}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    built, errors = build_all()

    out_series = {}
    for sid, spec in SPECS.items():
        meta = {k: spec[k] for k in ("title", "group", "units", "frequency", "source", "convention", "transformation")}
        if sid in built:
            lines = {name: to_points(s, spec["digits"]) for name, s in built[sid].items()}
            latest = max(pts[-1][0] for pts in lines.values())
            out_series[sid] = {**meta, "lines": lines, "latest_observation": latest,
                               "status": "ok", "last_success_utc": now, "error": None}
        elif sid in prev["series"]:
            old = prev["series"][sid]
            out_series[sid] = {**old, **meta, "status": "stale", "error": errors.get(sid)}
        else:
            out_series[sid] = {**meta, "lines": {}, "latest_observation": None,
                               "status": "missing", "last_success_utc": None, "error": errors.get(sid)}

    all_ok = not errors
    payload = {
        "run_utc": now,
        "last_successful_refresh_utc": now if all_ok else prev.get("last_successful_refresh_utc"),
        "run_status": "ok" if all_ok else f"partial: {len(errors)} of {len(SPECS)} indicators failed",
        "sample_start": SAMPLE_START.isoformat(),
        "series": out_series,
    }
    SERIES_PATH.write_text(json.dumps(payload, separators=(",", ":")))

    # Summary of changes vs previous run (for the email)
    changes = []
    for sid, cur in out_series.items():
        old = prev["series"].get(sid)
        for name, pts in cur.get("lines", {}).items():
            if not pts:
                continue
            old_pts = (old or {}).get("lines", {}).get(name) or []
            entry = {"indicator": cur["title"], "line": name, "units": cur["units"],
                     "latest_date": pts[-1][0], "latest_value": pts[-1][1], "status": cur["status"]}
            if old_pts:
                entry["previous_date"], entry["previous_value"] = old_pts[-1]
                entry["new_observation"] = pts[-1][0] != old_pts[-1][0]
            changes.append(entry)
    SUMMARY_PATH.write_text(json.dumps({"run_utc": now, "run_status": payload["run_status"],
                                        "last_successful_refresh_utc": payload["last_successful_refresh_utc"],
                                        "errors": errors, "indicators": changes}, indent=1))

    print(f"\nRun status: {payload['run_status']}")
    # Non-zero exit on any failure: the workflow still publishes the kept data,
    # then marks the run failed so GitHub sends its own failure email.
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
