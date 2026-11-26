"""Build the email update from data/series.json and data/summary.json.

Writes notify.md (body) and notify_title.txt, and prints SEND=1 or SEND=0.
Weekly summary: Monday morning run (before 15:00 UTC), or WEEKLY=1.
Otherwise an event update is sent only when something material happened.
All numbers come from the data files; nothing is estimated.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SERIES = json.loads((ROOT / "data" / "series.json").read_text())
SUMMARY = json.loads((ROOT / "data" / "summary.json").read_text())
CHI = ZoneInfo("America/Chicago")
URL = "https://maikewsantos.github.io/brazil-dashboard/"
DIG = {"ptax": 4, "reer": 1, "vol": 1, "unemp": 1, "fiscal": 2}

now_utc = datetime.now(timezone.utc)
weekly = os.environ.get("WEEKLY") == "1" or (now_utc.weekday() == 0 and now_utc.hour < 15)


def fmt(v: float, sid: str) -> str:
    d = DIG.get(sid, 2)
    if abs(v) < 0.5 * 10 ** -d:
        v = 0.0
    return f"{v:,.{d}f}"


def monthly(s: dict) -> bool:
    return s["frequency"].startswith("Monthly")


def fdate(iso: str, s: dict) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%b %Y") if monthly(s) else d.strftime("%d %b %Y").lstrip("0")


def ts(iso: str | None) -> str:
    if not iso:
        return "never"
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return d.astimezone(CHI).strftime("%a %d %b %Y, %I:%M %p Chicago").replace(" 0", " ")


def value_on_or_before(pts: list, iso: str):
    best = None
    for d, v in pts:
        if d <= iso:
            best = (d, v)
        else:
            break
    return best


prev = {(e["indicator"], e["line"]): e for e in SUMMARY["indicators"]}
order = ["ptax", "reer", "vol", "ipca", "focus", "selic", "real_rate", "carry", "ibcbr", "unemp", "ca", "fiscal"]

rows, events = [], []
for sid in order:
    s = SERIES["series"][sid]
    if s["status"] != "ok":
        events.append(f"**{s['title']}** is {s['status']}: {s.get('error') or 'no detail'}. Last good data kept.")
    for name, pts in s.get("lines", {}).items():
        if not pts:
            continue
        ld, lv = pts[-1]
        label = s["title"] if len(s["lines"]) == 1 else f"{s['title']}: {name}"
        if monthly(s):
            ref = pts[-2] if len(pts) > 1 else None
            cmp_txt = f"{lv - ref[1]:+.2f} vs {fdate(ref[0], s)}" if ref else "n/a"
        else:
            ref = (pts[-2] if len(pts) > 1 else None) if sid == "focus" else \
                value_on_or_before(pts, (date.fromisoformat(ld) - timedelta(days=7)).isoformat())
            cmp_txt = f"{lv - ref[1]:+.{DIG.get(sid, 2)}f} vs {fdate(ref[0], s)}" if ref else "n/a"
            if sid == "ptax" and ref:
                cmp_txt += f" ({(lv / ref[1] - 1) * 100:+.2f}%)"
        rows.append(f"| {label} | {fmt(lv, sid)} | {s['units']} | {fdate(ld, s)} | {cmp_txt} |")

        p = prev.get((s["title"], name), {})
        is_new = p.get("new_observation", False)
        pv = p.get("previous_value")
        if sid == "ipca" and is_new:
            events.append(f"New IPCA print: {fmt(lv, sid)}% over 12 months ({fdate(ld, s)}), previous {fmt(pv, sid)}%. Target band 1.5% to 4.5%.")
        if sid == "selic" and pv is not None and lv != pv:
            events.append(f"Selic target changed: {fmt(pv, sid)}% to {fmt(lv, sid)}% (effective {fdate(ld, s)}).")
        if sid == "ptax" and len(pts) > 1 and is_new:
            dd = (lv / pts[-2][1] - 1) * 100
            if abs(dd) > 2:
                events.append(f"BRL/USD PTAX moved {dd:+.2f}% day on day to {fmt(lv, sid)} ({fdate(ld, s)}).")
        if sid == "focus" and is_new and ref and abs(lv - ref[1]) >= 0.10:
            events.append(f"Focus 12-month IPCA expectation {fmt(lv, sid)}%, {lv - ref[1]:+.2f} pp vs a week earlier.")

if SERIES["run_status"] != "ok":
    events.insert(0, f"Run status: {SERIES['run_status']}.")
lsr = SERIES.get("last_successful_refresh_utc")
if lsr and now_utc - datetime.fromisoformat(lsr.replace("Z", "+00:00")) > timedelta(hours=30):
    events.insert(0, f"No fully successful refresh since {ts(lsr)}.")

send = weekly or bool(events)
today = now_utc.astimezone(CHI).strftime("%d %b %Y").lstrip("0")
title = f"Brazil dashboard: weekly summary, {today}" if weekly else f"Brazil dashboard: update, {today}"

body = [f"@maikewsantos {'Weekly summary' if weekly else 'Event update'} for the Brazil Macro Dashboard.", ""]
if events:
    body += ["**What changed**", ""] + [f"- {e}" for e in events] + [""]
if weekly:
    body += ["## Key numbers", "", "Change is in each series' own units (percentage points for rates and % series): vs about 7 days earlier for daily series, vs the previous weekly reading for Focus, and vs the prior month for monthly series.", "",
             "| Indicator | Latest | Units | Observation | Change |", "|---|---:|---|---|---|"] + rows + [""]
# Charts (static versions of the dashboard charts), rendered by scripts/charts.py into email/<stamp>/
stamp = now_utc.strftime("%Y-%m-%d-%H%M")
img_base = f"https://raw.githubusercontent.com/maikewsantos/brazil-dashboard/main/email/{stamp}"
groups = [("Exchange rate", ["ptax", "reer", "vol"]), ("Inflation and monetary policy", ["ipca", "focus", "selic", "real_rate", "carry"]),
          ("Activity", ["ibcbr", "unemp"]), ("External and fiscal", ["ca", "fiscal"])]
body += ["---", "", "## Charts (last 5 years)", ""]
for gname, ids in groups:
    body += [f"### {gname}", ""]
    for sid in ids:
        s_ = SERIES["series"][sid]
        if not s_.get("lines"):
            continue
        latest = "; ".join(f"{n if len(s_['lines']) > 1 else 'Latest'} {fmt(p_[-1][1], sid)} ({fdate(p_[-1][0], s_)})"
                           for n, p_ in s_["lines"].items() if p_)
        stale = "" if s_["status"] == "ok" else f" **{s_['status'].upper()}: last good data shown.**"
        body += [f"**{s_['title']}**: {s_['units']}, {s_['frequency'].lower()}. {latest}.{stale}", "",
                 f'<img src="{img_base}/{sid}.png" width="600" alt="{s_["title"]} chart">', "",
                 f"<sub>Source: {s_['source']}. {s_['convention']} Transformation: {s_['transformation']}</sub>", ""]
(ROOT / "notify_stamp.txt").write_text(stamp)

body += [
    f"Last successful refresh: {ts(lsr)}. " + "Each indicator has its own latest observation date" + (" (shown above)" if weekly else "") + ", which lags the refresh because agencies publish with delays.",
    "",
    "BRL/USD is quoted as BRL per 1 USD: a rise means the real depreciated.",
    "",
    f"Dashboard: {URL}",
]
(ROOT / "notify.md").write_text("\n".join(body))
(ROOT / "notify_title.txt").write_text(title)
print(f"SEND={'1' if send else '0'}")
