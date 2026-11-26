"""Render the dashboard charts as email-sized PNGs (static versions of the web charts).

Usage: python scripts/charts.py OUTDIR
Reads data/series.json. Charts show the last EMAIL_YEARS years.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SERIES = json.loads((ROOT / "data" / "series.json").read_text())["series"]
EMAIL_YEARS = 5

INK, INK2, GRID, AXIS, REF = "#191919", "#666666", "#e7e7e7", "#c2c2c2", "#8a8a8a"
C1, C2, BAND = "#257de0", "#f47230", "#00000012"
DIG = {"ptax": 4, "reer": 1, "vol": 1, "unemp": 1, "ibcbr": 2}
ZERO = {"real_rate", "carry", "ibcbr", "ca"}
TARGETS = {2017: (4.5, 1.5), 2018: (4.5, 1.5), 2019: (4.25, 1.5), 2020: (4.0, 1.5), 2021: (3.75, 1.5),
           2022: (3.5, 1.5), 2023: (3.25, 1.5), 2024: (3.0, 1.5)}

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": AXIS, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2, "axes.spines.top": False, "axes.spines.right": False,
    "axes.spines.left": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.axisbelow": True, "figure.facecolor": "white", "axes.facecolor": "white",
})


def to_series(pts) -> pd.Series:
    return pd.Series([v for _, v in pts], index=pd.to_datetime([d for d, _ in pts]))


def fmt(v, sid):
    d = DIG.get(sid, 2)
    return f"{0.0 if abs(v) < 0.5 * 10 ** -d else v:,.{d}f}"


def style_axis(ax, start, end):
    ax.set_xlim(start, end)
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.tick_params(length=0, pad=4)
    ax.spines["bottom"].set_color(AXIS)


def draw(sid: str, out: Path) -> Path | None:
    s = SERIES.get(sid)
    if not s or not s.get("lines"):
        return None
    end = pd.Timestamp(date.today())
    start = end - pd.DateOffset(years=EMAIL_YEARS)
    names = list(s["lines"])
    panels = [[n] for n in names] if sid == "fiscal" else [names]
    fig, axes = plt.subplots(len(panels), 1, figsize=(6.4, 2.5 * len(panels) if len(panels) > 1 else 2.9), dpi=200, squeeze=False)
    for ax, group in zip(axes[:, 0], panels):
        for i, name in enumerate(group):
            ser = to_series(s["lines"][name])
            ser = ser[ser.index >= start]
            if ser.empty:
                continue
            col = C1 if (i == 0 or sid == "fiscal") else C2
            if sid == "selic":
                ax.step(ser.index, ser.values, where="post", color=col, lw=1.8)
            else:
                ax.plot(ser.index, ser.values, color=col, lw=1.8, solid_joinstyle="round")
            ax.plot(ser.index[-1], ser.values[-1], "o", ms=5, color=col, mec="white", mew=1.5, zorder=5)
            lbl = fmt(ser.values[-1], sid) + (f"  {name}" if len(group) > 1 else "")
            ax.annotate(lbl, (ser.index[-1], ser.values[-1]), xytext=(6, 0), textcoords="offset points",
                        va="center", fontsize=8.5, color=INK, fontweight="bold", annotation_clip=False)
        if sid == "ipca":
            for y in range(start.year, end.year + 1):
                c, tol = TARGETS.get(y, (3.0, 1.5)) if y >= 2017 else (4.5, 2.0)
                a, b = max(start, pd.Timestamp(y, 1, 1)), min(end, pd.Timestamp(y + 1, 1, 1))
                ax.fill_between([a, b], c - tol, c + tol, color=BAND, lw=0)
                ax.plot([a, b], [c, c], color=REF, ls=(0, (4, 3)), lw=1.2)
        if sid in ZERO or (sid == "fiscal" and group[0].startswith("Primary")):
            lo, hi = ax.get_ylim()
            if lo < 0 < hi:
                ax.axhline(0, color=REF, lw=0.9)
        style_axis(ax, start, end)
        if len(panels) > 1:
            ax.set_title(group[0], loc="left", fontsize=8.5, color=INK2, pad=4)
    fig.tight_layout(pad=0.6, rect=(0, 0, 0.93, 1))
    path = out / f"{sid}.png"
    fig.savefig(path, facecolor="white")
    plt.close(fig)
    return path


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for sid in SERIES:
        p = draw(sid, out)
        print(f"{sid}: {p}")


if __name__ == "__main__":
    main()
