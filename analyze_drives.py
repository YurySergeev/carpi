#!/usr/bin/env python3
"""
analyze_drives.py - turn CarPi drive logs into reports.

Point it at drive CSVs (files, folders, wildcards, or a .txt list of paths). Every drive
it hasn't seen before gets analyzed; everything already analyzed is skipped.

    python analyze_drives.py                     # new drives in ~/carpi/logs (or ./logs)
    python analyze_drives.py C:\\carpi\\logs       # a folder
    python analyze_drives.py a.csv b.csv          # specific drives
    python analyze_drives.py "logs/drive-2026-10-*.csv"
    python analyze_drives.py new_drives.txt       # one path per line
    python analyze_drives.py --force              # re-analyze everything
    python analyze_drives.py --open               # open the index in a browser when done
    python analyze_drives.py x.csv --tag post-pcv # tag for drives that have no .json

Output (default ./reports, change with --out):
    index.html          every drive, tag comparison, idle-trim history chart
    drives.csv          one summary row per drive (for pandas / Excel)
    <drive>.html        full self-contained report per drive
    <drive>.json        the same numbers, machine-readable

Needs Python 3.9+, pandas, matplotlib:  pip install pandas matplotlib
"""
import argparse
import base64
import glob
import html
import io
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

VERSION = "1.0"
DEG, DEG_C, DEG_F = "\u00b0", " \u00b0C", " \u00b0F"  # kept out of f-string braces (Python < 3.12)

# ---------------------------------------------------------------- thresholds
# Everything the automatic checks compare against, in one place.
T = {
    "warm_coolant_c": 80,          # trims / idle checks only once the engine is warm
    "closed_loop": (0.98, 1.02),   # commanded lambda window = normal closed-loop fueling
    "idle_rpm": (600, 1000),
    "cruise": dict(kph_min=50, pedal=(15, 35), rpm=(1400, 2600)),
    "boost_psi": 3.0,              # "under boost" above this
    "pull_pedal_pct": 70,          # pedal >= this for >= pull_min_s = a full-throttle pull
    "pull_min_s": 1.5,
    "idle_vs_cruise_trim_pct": 5,  # idle total trim this much above cruise = vacuum-leak pattern
    "trim_abs_warn_pct": 10,
    "coolant_max_c": 110,
    "volts": (13.2, 14.8),
    "boost_idle_vs_baro_kpa": 3,
    "lambda_track": 0.02,
    "lean_under_boost": 1.08,      # actual lambda this lean while commanded ~1 under boost
    "rail_min_under_boost_bar": 100,
    "rail_max_bar": 210,
    "timing_dip_deg": -5,
    "min_rate_hz": 5,
    "max_gap_s": 1.0,
    "short_drive_min": 3,
}
BANDS = [(0, 1000, "Idle, under 1,000 rpm", "idle"),
         (1000, 1800, "1,000\u20131,800 rpm", "low"),
         (1800, 2500, "1,800\u20132,500 rpm", "cruise"),
         (2500, 8000, "Over 2,500 rpm", "high")]

# ---------------------------------------------------------------- chart style
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
MARKERS = ["o", "s", "D", "^", "v", "P", "X", "*"]
STATUS = {"pass": ("#0ca30c", "\u2713", "Pass"), "warn": ("#fab219", "!", "Check"),
          "crit": ("#d03b3b", "\u2715", "Problem"), "info": ("#52514e", "i", "Note"),
          "na": ("#898781", "\u2013", "No data")}

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": ["DejaVu Sans"], "font.size": 9.5, "text.color": INK,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "axes.titlecolor": INK,
    "axes.titlesize": 10.5, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False,
    "axes.spines.left": False, "lines.linewidth": 1.6, "lines.solid_capstyle": "round",
    "legend.frameon": False, "legend.fontsize": 9,
})


# ============================================================== input
def find_inputs(paths):
    """Expand files, folders, wildcards and .txt lists into drive CSV paths."""
    found = []

    def add(p):
        p = Path(p).expanduser()
        if p.is_dir():
            found.extend(sorted(p.glob("drive-*.csv")))
        elif p.suffix.lower() == ".txt" and p.is_file():
            for line in p.read_text().splitlines():
                if line.strip() and not line.strip().startswith("#"):
                    add(line.strip())
        elif p.suffix.lower() == ".csv" and p.is_file():
            found.append(p)
        elif any(ch in str(p) for ch in "*?["):
            for g in sorted(glob.glob(str(p))):
                add(g)
        else:
            print(f"  skipped (not found): {p}")

    for p in paths:
        add(p)
    seen, out = set(), []
    for p in found:
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def default_input():
    for p in (Path.home() / "carpi" / "logs", Path("logs"), Path(".")):
        if p.is_dir() and any(p.glob("drive-*.csv")):
            return [p]
    return []


def load_drive(csv_path, tag_override=None):
    """Read one drive (CSV + optional .json sidecar). Returns dict with raw df, filled df, meta, notes."""
    notes = []
    raw_bytes = csv_path.read_bytes()
    truncated = bool(raw_bytes) and not raw_bytes.endswith(b"\n")
    df = pd.read_csv(io.BytesIO(raw_bytes), on_bad_lines="skip")
    for c in df.columns:
        if c != "time":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["elapsed_s"]).sort_values("elapsed_s").reset_index(drop=True)
    if truncated:
        notes.append(("info", "Last row was cut off mid-write (power loss); it was dropped."))
        if len(df) and df.iloc[-1].isna().sum() > len(df.columns) / 2:
            df = df.iloc[:-1]

    meta = {}
    side = csv_path.with_suffix(".json")
    if side.exists():
        try:
            meta = json.loads(side.read_text())
        except json.JSONDecodeError:
            notes.append(("warn", "Metadata .json is unreadable; ignored."))
    tag = tag_override or meta.get("tag") or "untagged"

    s = df.ffill()  # slow PIDs are sampled round-robin; carry the last sample forward
    dt = s["elapsed_s"].diff().fillna(0).clip(lower=0, upper=1.0)
    s["dt"] = dt
    return dict(id=csv_path.stem, path=csv_path, raw=df, s=s, meta=meta, tag=tag,
                truncated=truncated, notes=notes)


# ============================================================== helpers
def col(df, name):
    return df[name] if name in df.columns else pd.Series(np.nan, index=df.index)


def med(x):
    x = pd.Series(x).dropna()
    return float(x.median()) if len(x) else None


def rnd(v, n=1):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), n)


def c_to_f(c):
    return None if c is None else c * 1.8 + 32


def fmt(v, unit="", n=1, plus=False):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "\u2014"
    s = f"{v:+,.{n}f}" if plus else f"{v:,.{n}f}"
    return f"{s}{unit}"


def status_rank(st):
    return {"crit": 3, "warn": 2, "info": 1, "pass": 0, "na": 0}.get(st, 0)


# ============================================================== analysis
def analyze(d):
    raw, s = d["raw"], d["s"]
    meta = d["meta"]
    findings, quality = [], list(d["notes"])
    out = {"drive_id": d["id"], "source": str(d["path"]), "tag": d["tag"], "version": VERSION,
           "analyzed_at": datetime.now().isoformat(timespec="seconds")}

    # ---------- time base, clock, completeness
    el = s["elapsed_s"]
    duration = float(el.max()) if len(el) else 0.0
    rows = len(s)
    gaps = el.diff().dropna()
    rate = rows / duration if duration > 0 else 0
    out.update(rows=rows, duration_min=rnd(duration / 60, 1), rate_hz=rnd(rate, 1),
               max_gap_s=rnd(gaps.max() if len(gaps) else 0, 2))
    if rate and rate < T["min_rate_hz"]:
        quality.append(("warn", f"Low sample rate: {rate:.1f} rows/s (expected about 7\u20138)."))
    if len(gaps) and gaps.max() > T["max_gap_s"]:
        quality.append(("warn", f"Gap of {gaps.max():.1f} s between rows at {el[gaps.idxmax()]:.0f} s."))

    times = pd.to_datetime(raw["time"], errors="coerce") if "time" in raw.columns else pd.Series(dtype="datetime64[ns]")
    start, start_note = None, "unverified"
    if len(times) and times.notna().any():
        if "clock_ok" in raw.columns and (raw["clock_ok"] == 1).any():
            i = raw.index[raw["clock_ok"] == 1][0]
            start = times[i] - pd.Timedelta(seconds=float(raw["elapsed_s"][i]))
            start_note = "synced" if i == raw.index[0] else f"corrected (clock synced {raw['elapsed_s'][i]:.0f} s in)"
        else:
            jump = (times.diff().dt.total_seconds() - raw["elapsed_s"].diff()).abs()
            big = jump[jump > 2]
            if len(big):
                i = big.index[-1]
                start = times[i] - pd.Timedelta(seconds=float(raw["elapsed_s"][i]))
                start_note = f"corrected (clock jumped {jump[i]:.0f} s at {raw['elapsed_s'][i]:.0f} s)"
                quality.append(("info", f"Pi clock jumped {jump[i]:.0f} s at {raw['elapsed_s'][i]:.0f} s "
                                        f"(internet time sync). Start time corrected; elapsed_s unaffected."))
            else:
                start = times.dropna().iloc[0]
                start_note = "unverified (no clock_ok column, no sync jump seen)"
    out["start"] = start.isoformat(timespec="seconds") if start is not None else None
    out["start_note"] = start_note

    complete = meta.get("complete")
    out["complete"] = complete
    if not meta:
        quality.append(("info", "No .json metadata next to this CSV (tag and completeness unknown)."))
    elif complete is not True:
        quality.append(("warn", "Drive did not end cleanly (power was cut before the logger closed it)."))

    rpm = col(s, "rpm")
    running = rpm > 400
    engine_s = float(s["dt"][running].sum())
    rt = col(raw, "run_time_s").dropna()
    starts = int((rt.diff() < -5).sum()) + (1 if running.any() else 0)
    out.update(engine_min=rnd(engine_s / 60, 1), engine_starts=starts)
    if starts > 1:
        quality.append(("info", f"{starts} engine starts inside this log."))
    empty = [c for c in raw.columns if c not in ("time",) and raw[c].isna().all()]
    if empty:
        quality.append(("info", "Always blank (not reported by this ECU): " + ", ".join(empty) + "."))

    # ---------- derived channels
    baro = med(col(raw, "baro_kpa")) or 101.3
    s["psi"] = (col(s, "boost_act_kpa") - baro) / 6.895
    s["mph"] = col(s, "speed_kph") * 0.621371
    s["trim"] = col(s, "stft_pct") + col(s, "ltft_pct")
    warm = col(s, "coolant_c") >= T["warm_coolant_c"]
    lo, hi = T["closed_loop"]
    closed = col(s, "lambda_cmd").between(lo, hi)
    idle = (col(s, "speed_kph") == 0) & rpm.between(*T["idle_rpm"]) & warm
    cz = T["cruise"]
    cruise = ((col(s, "speed_kph") > cz["kph_min"]) & col(s, "pedal_pct").between(*cz["pedal"])
              & rpm.between(*cz["rpm"]) & closed & warm)
    boost = s["psi"] > T["boost_psi"]
    moving = col(s, "speed_kph") > 0

    # ---------- at a glance
    dist_mi = float((col(s, "speed_kph").fillna(0) * s["dt"]).sum() / 3600 * 0.621371)
    pk = s["psi"].idxmax() if s["psi"].notna().any() else None
    volts = col(raw, "volts")[running]
    out.update(
        baro_kpa=rnd(baro, 1), distance_mi=rnd(dist_mi, 1),
        avg_moving_mph=rnd(s["mph"][moving].mean() if moving.any() else None, 1),
        max_mph=rnd(s["mph"].max(), 1), idle_min=rnd(s["dt"][running & ~moving].sum() / 60, 1),
        max_rpm=rnd(rpm.max(), 0), idle_rpm=rnd(med(rpm[idle]), 0),
        peak_boost_psi=rnd(s["psi"].max(), 1),
        peak_boost_rpm=rnd(rpm[pk], 0) if pk is not None else None,
        peak_boost_pedal=rnd(col(s, "pedal_pct")[pk], 0) if pk is not None else None,
        time_over_3psi_s=rnd(s["dt"][boost].sum(), 0),
        coolant_start_c=rnd(col(raw, "coolant_c").dropna().iloc[0] if col(raw, "coolant_c").notna().any() else None, 0),
        coolant_max_c=rnd(col(raw, "coolant_c").max(), 0),
        coolant_end_c=rnd(col(raw, "coolant_c").dropna().iloc[-1] if col(raw, "coolant_c").notna().any() else None, 0),
        iat_min_c=rnd(col(raw, "iat_c").min(), 0), iat_max_c=rnd(col(raw, "iat_c").max(), 0),
        ambient_c=rnd(med(col(raw, "ambient_c")), 0),
        volts_median=rnd(med(volts), 2), volts_max=rnd(volts.max() if volts.notna().any() else None, 2),
        fuel_start_pct=rnd(col(raw, "fuel_pct").dropna().iloc[0] if col(raw, "fuel_pct").notna().any() else None, 1),
        fuel_end_pct=rnd(col(raw, "fuel_pct").dropna().iloc[-1] if col(raw, "fuel_pct").notna().any() else None, 1),
    )
    short = engine_s / 60 < T["short_drive_min"]
    out["short"] = short
    if short:
        findings.append(("info", "Short log (under 3 minutes of engine running): shown, but its cruise "
                                 "numbers are left out of tag comparisons."))

    # ---------- fuel trims by rpm band
    wc = warm & closed & running
    trims = []
    step = float(s["dt"][s["dt"] > 0].median()) if (s["dt"] > 0).any() else 0.13
    for a, b, label, key in BANDS:
        m = wc & (rpm > a) & (rpm <= b)
        row = dict(key=key, band=label, seconds=rnd(m.sum() * step, 0),
                   stft=rnd(med(col(s, "stft_pct")[m]), 1), ltft=rnd(med(col(s, "ltft_pct")[m]), 1),
                   total=rnd(med(s["trim"][m]), 1))
        trims.append(row)
        out[f"trim_{key}_total"] = row["total"]
    out["idle_stft"], out["idle_ltft"] = trims[0]["stft"], trims[0]["ltft"]
    ti, tc = trims[0]["total"], trims[2]["total"]
    if ti is not None and tc is not None and ti - tc > T["idle_vs_cruise_trim_pct"]:
        findings.append(("warn", f"Fuel trim is {ti:+.1f}% at idle but {tc:+.1f}% at cruise: the engine adds fuel "
                                 f"only at idle, the pattern of a small vacuum leak (PCV valve is the usual cause "
                                 f"on the EA888)."))
    for r in trims:
        if r["total"] is not None and abs(r["total"]) > T["trim_abs_warn_pct"]:
            findings.append(("warn", f"Total fuel trim {r['total']:+.1f}% in the {r['band']} band "
                                     f"(more than \u00b1{T['trim_abs_warn_pct']}%)."))

    # ---------- operating conditions
    def cond(name, m):
        if not m.any():
            return dict(name=name, rows=0)
        return dict(name=name, rows=int(m.sum()), rpm=rnd(med(rpm[m]), 0),
                    load=rnd(med(col(s, "load_pct")[m]), 0), map=rnd(med(col(s, "map_kpa")[m]), 0),
                    psi=rnd(med(s["psi"][m]), 1), lam=rnd(med(col(s, "lambda")[m]), 3),
                    lam_cmd=rnd(med(col(s, "lambda_cmd")[m]), 3), timing=rnd(med(col(s, "timing_deg")[m]), 1),
                    rail=rnd(med(col(s, "rail_kpa")[m]) / 100 if med(col(s, "rail_kpa")[m]) else None, 0),
                    iat=rnd(med(col(s, "iat_c")[m]), 0))
    conds = [cond("Warm idle", idle), cond("Steady cruise", cruise),
             cond(f"Under boost (over {T['boost_psi']:.0f} psi)", boost)]
    if pk is not None:
        conds.append(cond("Peak boost moment", s.index == pk))
    out["rail_idle_bar"] = conds[0].get("rail")
    out["rail_cruise_bar"] = conds[1].get("rail")
    out["rail_max_bar"] = rnd(col(s, "rail_kpa").max() / 100 if col(s, "rail_kpa").notna().any() else None, 0)
    out["timing_cruise_deg"] = conds[1].get("timing")
    out["lambda_err_cruise"] = (rnd(conds[1]["lam"] - conds[1]["lam_cmd"], 3)
                                if conds[1].get("lam") is not None and conds[1].get("lam_cmd") is not None else None)

    # ---------- full-throttle pulls
    wot = col(s, "pedal_pct") >= T["pull_pedal_pct"]
    grp = (wot != wot.shift()).cumsum()
    pulls = []
    for _, g in s[wot].groupby(grp[wot]):
        dur = float(g["elapsed_s"].iloc[-1] - g["elapsed_s"].iloc[0])
        if dur < T["pull_min_s"]:
            continue
        pulls.append(dict(start_s=rnd(g["elapsed_s"].iloc[0], 0), seconds=rnd(dur, 1),
                          rpm_from=rnd(g["rpm"].iloc[0], 0), rpm_to=rnd(g["rpm"].max(), 0),
                          mph_from=rnd(g["mph"].iloc[0], 0), peak_psi=rnd(g["psi"].max(), 1),
                          min_timing=rnd(col(g, "timing_deg").min(), 1), min_lambda=rnd(col(g, "lambda").min(), 3),
                          min_lambda_cmd=rnd(col(g, "lambda_cmd").min(), 3),
                          min_rail=rnd(col(g, "rail_kpa").min() / 100 if col(g, "rail_kpa").notna().any() else None, 0),
                          iat=rnd(col(g, "iat_c").max(), 0), idx=(int(g.index[0]), int(g.index[-1]))))
    out["pulls"] = len(pulls)

    # ---------- events
    rich = col(s, "lambda_cmd") < 0.95
    cut = col(s, "lambda_cmd") > 1.05
    rich_starts = s.index[rich & ~rich.shift(1, fill_value=False)]
    after_cut = sum(bool(cut.loc[max(0, i - 40):i - 1].any()) for i in rich_starts)
    out["fuel_cut_s"] = rnd(s["dt"][cut].sum(), 0)
    out["rich_events"], out["rich_after_cut"] = len(rich_starts), after_cut
    dips = boost & (col(s, "timing_deg") <= T["timing_dip_deg"]) & (s["psi"] > 5)
    out["timing_dip_s"] = rnd(s["dt"][dips].sum(), 1)
    if out["timing_dip_s"]:
        findings.append(("info", f"Timing at or below {T['timing_dip_deg']}\u00b0 for {out['timing_dip_s']} s while above "
                                 f"5 psi. Normal at low rpm and high load; OBD alone can't separate it from knock retard."))

    # ---------- sensor checks
    checks = []

    def check(name, ref, measured, ok):
        checks.append(dict(name=name, ref=ref, measured=measured, status="na" if ok is None else ("pass" if ok else "warn")))

    i0 = conds[0]
    bi = med(col(s, "boost_act_kpa")[idle])
    check("Boost sensor at idle", "Equals barometric pressure (\u00b13 kPa)",
          f"{fmt(bi, ' kPa')} vs {fmt(baro, ' kPa', 0)}", None if bi is None else abs(bi - baro) <= T["boost_idle_vs_baro_kpa"])
    c1 = conds[1]
    lam_ok = None if out["lambda_err_cruise"] is None else abs(out["lambda_err_cruise"]) <= T["lambda_track"]
    check("Air-fuel control", "Actual \u03bb within 0.02 of commanded at cruise",
          f"{fmt(c1.get('lam'), '', 3)} vs {fmt(c1.get('lam_cmd'), '', 3)}", lam_ok)
    rmax = out["rail_max_bar"]
    check("Fuel rail pressure", "Tens of bar at idle, \u2264 210 bar max",
          f"{fmt(i0.get('rail'), ' bar', 0)} idle, {fmt(c1.get('rail'), ' bar', 0)} cruise, {fmt(rmax, ' bar', 0)} max",
          None if rmax is None else (rmax <= T["rail_max_bar"] and (i0.get("rail") or 50) >= 30))
    ir = out["idle_rpm"]
    check("Warm idle speed", "About 700\u2013900 rpm", fmt(ir, " rpm", 0), None if ir is None else 700 <= ir <= 900)
    vm = out["volts_median"]
    check("Charging voltage", "13.2\u201314.8 V while running", f"{fmt(vm, ' V', 2)} median",
          None if vm is None else T["volts"][0] <= vm <= T["volts"][1])
    cm = out["coolant_max_c"]
    check("Coolant temperature", f"Peak under {T['coolant_max_c']} \u00b0C", fmt(cm, " \u00b0C", 0),
          None if cm is None else cm < T["coolant_max_c"])
    o2 = med(col(s, "o2s2_v")[warm & running])
    lt2 = med(col(s, "lt_o2s2_trim_pct"))
    check("Catalyst / downstream O2", "Steady 0.5\u20130.9 V, trim near 0%",
          f"{fmt(o2, ' V', 2)}, {fmt(lt2, '%', 1)} trim", None if o2 is None else 0.5 <= o2 <= 0.9 and abs(lt2 or 0) < 5)
    out["checks_failed"] = sum(c["status"] == "warn" for c in checks)
    for c in checks:
        if c["status"] == "warn":
            findings.append(("warn", f"{c['name']} out of range: {c['measured']} (expected: {c['ref'].lower()})."))

    # ---------- load-dependent problems
    lean = boost & (s["psi"] > 5) & (col(s, "lambda") > T["lean_under_boost"]) & (col(s, "lambda_cmd") <= 1.02)
    if s["dt"][lean].sum() > 0.5:
        findings.append(("crit", f"Lean under boost: \u03bb above {T['lean_under_boost']} while commanded \u2264 1.02 "
                                 f"for {s['dt'][lean].sum():.1f} s above 5 psi."))
    low_rail = (s["psi"] > 5) & (col(s, "rail_kpa") / 100 < T["rail_min_under_boost_bar"])
    if s["dt"][low_rail].sum() > 0.5:
        findings.append(("warn", f"Fuel rail pressure under {T['rail_min_under_boost_bar']} bar while above 5 psi "
                                 f"for {s['dt'][low_rail].sum():.1f} s (check the high-pressure fuel pump)."))
    if not pulls and not short:
        findings.append(("info", "No full-throttle pulls in this drive, so there's no data yet for "
                                 "tune comparisons at high load."))

    if not any(f[0] in ("warn", "crit") for f in findings):
        findings.insert(0, ("pass", "No problems found: every check with enough data passed."))
    worst = max((f[0] for f in findings), key=status_rank)
    out["status"] = worst if worst in ("warn", "crit") else "pass"
    out["warnings"] = sum(f[0] in ("warn", "crit") for f in findings)
    return dict(summary=out, findings=findings, quality=quality, checks=checks, trims=trims,
                conds=conds, pulls=pulls, s=s)


# ============================================================== charts
def png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def chart_trace(s):
    m = s["elapsed_s"] / 60
    fig, axes = plt.subplots(3, 1, figsize=(9, 6.2), sharex=True, gridspec_kw=dict(hspace=0.45))
    for ax, (y, title, c) in zip(axes, [(s["mph"], "Speed (mph)", SERIES[0]), (col(s, "rpm"), "Engine speed (rpm)", SERIES[1]),
                                        (s["psi"].clip(lower=0), "Boost (psi)", SERIES[2])]):
        ax.plot(m, y, color=c, linewidth=1.3)
        ax.set_title(title)
        ax.set_ylim(bottom=0)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
        if y.notna().any():
            i = y.idxmax()
            ax.plot(m[i], y[i], "o", color=c, markersize=5, markeredgecolor=SURFACE, markeredgewidth=1.5)
            ax.annotate(f"{y[i]:,.0f}" if y[i] >= 100 else f"{y[i]:.1f}", (m[i], y[i]), xytext=(6, 2),
                        textcoords="offset points", color=INK, fontsize=9)
    axes[-1].set_xlabel("Minutes since logging started")
    return png(fig)


def chart_temps(raw):
    fig, ax = plt.subplots(figsize=(9, 2.8))
    m = raw["elapsed_s"] / 60
    for (name, label), c in zip([("coolant_c", "Coolant"), ("iat_c", "Intake air"), ("ambient_c", "Ambient")], SERIES):
        y = col(raw, name)
        ok = y.notna()
        if ok.any():
            ax.plot(m[ok], y[ok], color=c, label=label, linewidth=1.6)
            ax.annotate(f"{label} {y[ok].iloc[-1]:.0f}", (m[ok].iloc[-1], y[ok].iloc[-1]), xytext=(6, -3),
                        textcoords="offset points", color=INK2, fontsize=8.5)
    ax.set_ylabel("\u00b0C")
    ax.set_xlabel("Minutes since logging started")
    ax.legend(loc="upper left", ncol=3, bbox_to_anchor=(0, -0.22))
    return png(fig)


def chart_boost_rpm(s):
    m = (s["rpm"] > 600) & s["psi"].notna()
    if m.sum() < 20:
        return None
    fig, ax = plt.subplots(figsize=(9, 3.2))
    ax.scatter(s["rpm"][m], s["psi"][m].clip(lower=-1), s=5, color=SERIES[0], alpha=0.35, linewidths=0)
    ax.axhline(0, color=AXIS, linewidth=1)
    ax.set_title("Boost by engine speed (psi, every sample)")
    ax.set_xlabel("Engine speed (rpm)")
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    return png(fig)


def chart_pulls(s, pulls):
    if not pulls:
        return None
    fig, axes = plt.subplots(1, 3, figsize=(9, 2.8), gridspec_kw=dict(wspace=0.35))
    for k, p in enumerate(pulls[:8]):
        a, b = p["idx"]
        g = s.loc[a:b]
        lab = f"Pull {k + 1} ({p['start_s']:.0f} s)"
        for ax, y in zip(axes, [g["psi"], col(g, "timing_deg"), col(g, "lambda")]):
            ax.plot(g["rpm"], y, color=SERIES[k], label=lab, marker=MARKERS[k], markersize=3, markevery=4, linewidth=1.4)
    for ax, t in zip(axes, ["Boost (psi)", "Timing (\u00b0)", "Lambda (\u03bb)"]):
        ax.set_title(t)
        ax.set_xlabel("rpm")
    axes[0].legend(loc="upper left", bbox_to_anchor=(0, -0.3), ncol=min(4, len(pulls)))
    return png(fig)


def chart_history(df):
    d = df[df["trim_idle_total"].notna()].copy()
    if d.empty:
        return None
    d = d.sort_values(["start", "drive_id"], na_position="last").reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(9, 3.2))
    tags = list(dict.fromkeys(d["tag"]))
    for k, t in enumerate(tags):
        g = d[d["tag"] == t]
        ax.scatter(g.index, g["trim_idle_total"], s=42, color=SERIES[k % 8], marker=MARKERS[k % 8],
                   edgecolors=SURFACE, linewidths=1.5, label=t, zorder=3)
    ax.axhline(0, color=AXIS, linewidth=1)
    ax.axhline(T["idle_vs_cruise_trim_pct"], color=MUTED, linewidth=0.8)
    ax.annotate(f"+{T['idle_vs_cruise_trim_pct']}%", (d.index.max(), T["idle_vs_cruise_trim_pct"]), xytext=(6, -3),
                textcoords="offset points", color=MUTED, fontsize=8.5)
    ax.set_ylabel("Idle fuel trim, total (%)")
    ax.set_xticks(d.index)
    ax.set_xticklabels([str(x)[5:10] if isinstance(x, str) else "?" for x in d["start"]], rotation=0, fontsize=8)
    ax.set_xlabel("Drive (start date)")
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.2), ncol=min(4, len(tags)))
    return png(fig)


# ============================================================== html
CSS = """
:root{color-scheme:light}
body{margin:0;background:#f9f9f7;color:#0b0b0b;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px}
.sub{color:#52514e;margin:0 0 16px}
section{background:#fcfcfb;border:1px solid rgba(11,11,11,.10);border-radius:10px;padding:16px 18px;margin:14px 0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #e1e0d9;vertical-align:top}
th{color:#52514e;font-weight:600}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-flex;align-items:center;gap:6px;font-weight:600;white-space:nowrap}
.dot{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;color:#fff;font-size:12px;font-weight:700}
ul.f{list-style:none;padding:0;margin:0}ul.f li{display:flex;gap:10px;padding:6px 0;border-bottom:1px solid #e1e0d9}
ul.f li:last-child{border-bottom:0}
img{max-width:100%;height:auto;display:block}
.chips span{display:inline-block;background:#f0efec;border-radius:999px;padding:2px 10px;margin:0 6px 6px 0;font-size:13px}
a{color:#1c5cab}.muted{color:#898781;font-size:13px}
@media (max-width:640px){table{font-size:13px}th,td{padding:5px 4px}}
.scroll{overflow-x:auto}
"""


def e(x):
    return html.escape("" if x is None else str(x))


def badge(st, label=None):
    color, icon, word = STATUS[st]
    ink = "#0b0b0b" if st == "warn" else "#fff"  # dark icon on the light warning fill
    return (f'<span class="badge"><span class="dot" style="background:{color};color:{ink}">{icon}</span>'
            f'{e(label or word)}</span>')


def page(title, body):
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{e(title)}</title><style>{CSS}</style></head>"
            f"<body><main>{body}<p class='muted'>Generated by analyze_drives.py v{VERSION} on "
            f"{datetime.now():%Y-%m-%d %H:%M}.</p></main></body></html>")


def table(headers, rows, numeric=()):
    th = "".join(f"<th class='{'n' if i in numeric else ''}'>{e(h)}</th>" for i, h in enumerate(headers))
    trs = "".join("<tr>" + "".join(f"<td class='{'n' if i in numeric else ''}'>{c}</td>" for i, c in enumerate(r))
                  + "</tr>" for r in rows)
    return f"<div class='scroll'><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>"


def img(b64, alt):
    return f"<img alt='{e(alt)}' src='data:image/png;base64,{b64}'>" if b64 else ""


def render_drive(a, charts):
    S = a["summary"]
    when = S["start"].replace("T", " ") if S["start"] else "unknown time"
    head = (f"<h1>Drive {e(when)}</h1><p class='sub'>{badge(S['status'])} &nbsp; tag <b>{e(S['tag'])}</b> \u00b7 "
            f"{fmt(S['distance_mi'], ' mi')} \u00b7 {fmt(S['duration_min'], ' min')} \u00b7 "
            f"<a href='index.html'>all drives</a></p>")
    order = {"crit": 0, "warn": 1, "pass": 2, "info": 3}
    findings = "".join(f"<li>{badge(st)}<span>{e(t)}</span></li>" for st, t in sorted(a["findings"], key=lambda f: order.get(f[0], 4)))
    glance = table(["Measure", "Value"], [
        ["File", f"<code>{e(S['drive_id'])}.csv</code>, tag <code>{e(S['tag'])}</code>"],
        ["Start", f"{e(when)} <span class='muted'>({e(S['start_note'])})</span>"],
        ["Logged", f"{fmt(S['duration_min'], ' min')} ({fmt(S['engine_min'], ' min')} engine running), "
                   f"{S['rows']:,} rows at {fmt(S['rate_hz'], ' rows/s')}"],
        ["Distance", f"{fmt(S['distance_mi'], ' mi')} ({fmt(S['distance_mi'] and S['distance_mi'] / 0.621371, ' km')})"],
        ["Speed", f"{fmt(S['avg_moving_mph'], ' mph', 0)} average while moving, {fmt(S['max_mph'], ' mph', 0)} max; "
                  f"{fmt(S['idle_min'], ' min')} stopped with the engine running"],
        ["Engine speed", f"{fmt(S['idle_rpm'], ' rpm', 0)} warm idle, {fmt(S['max_rpm'], ' rpm', 0)} max"],
        ["Boost", f"{fmt(S['peak_boost_psi'], ' psi')} max ({fmt(S['peak_boost_rpm'], ' rpm', 0)}, "
                  f"{fmt(S['peak_boost_pedal'], '% pedal', 0)}); {fmt(S['time_over_3psi_s'], ' s', 0)} above 3 psi; "
                  f"{S['pulls']} full-throttle pull(s)"],
        ["Coolant", f"{fmt(S['coolant_start_c'], DEG_C, 0)} \u2192 {fmt(S['coolant_max_c'], DEG_C, 0)} peak "
                    f"({fmt(c_to_f(S['coolant_max_c']), DEG_F, 0)})"],
        ["Intake air / ambient", f"{fmt(S['iat_min_c'], '', 0)}\u2013{fmt(S['iat_max_c'], DEG_C, 0)} intake; "
                                 f"{fmt(S['ambient_c'], DEG_C, 0)} ({fmt(c_to_f(S['ambient_c']), DEG_F, 0)}) ambient"],
        ["Charging", f"{fmt(S['volts_median'], ' V', 2)} median, {fmt(S['volts_max'], ' V', 2)} max while running"],
        ["Fuel level", f"{fmt(S['fuel_start_pct'], '%')} \u2192 {fmt(S['fuel_end_pct'], '%')}"],
    ])
    checks = table(["Check", "Reference", "Measured", "Result"],
                   [[e(c["name"]), e(c["ref"]), e(c["measured"]), badge(c["status"])] for c in a["checks"]])
    trims = table(["RPM band", "Time", "Short-term", "Long-term", "Total"],
                  [[e(r["band"]), fmt(r["seconds"], " s", 0), fmt(r["stft"], "%", 1, True), fmt(r["ltft"], "%", 1, True),
                    f"<b>{fmt(r['total'], '%', 1, True)}</b>"] for r in a["trims"]], numeric=(1, 2, 3, 4))
    conds = table(["Condition", "Rows", "RPM", "Load", "MAP", "Boost", "\u03bb act / cmd", "Timing", "Rail", "IAT"],
                  [[e(c["name"]), f"{c['rows']:,}"] + ([fmt(c.get("rpm"), "", 0), fmt(c.get("load"), "%", 0),
                    fmt(c.get("map"), " kPa", 0), fmt(c.get("psi"), " psi", 1),
                    f"{fmt(c.get('lam'), '', 3)} / {fmt(c.get('lam_cmd'), '', 3)}", fmt(c.get("timing"), "\u00b0", 1),
                    fmt(c.get("rail"), " bar", 0), fmt(c.get("iat"), " \u00b0C", 0)] if c["rows"] else ["\u2014"] * 8)
                   for c in a["conds"]], numeric=(1, 2, 3, 4, 5, 7, 8, 9))
    pulls = ""
    if a["pulls"]:
        pulls = ("<section><h2>Full-throttle pulls</h2>" + table(
            ["#", "At", "Length", "RPM", "From", "Peak boost", "Min timing", "Min \u03bb (cmd)", "Min rail", "IAT"],
            [[str(k + 1), fmt(p["start_s"], " s", 0), fmt(p["seconds"], " s"), f"{fmt(p['rpm_from'], '', 0)}\u2192{fmt(p['rpm_to'], '', 0)}",
              fmt(p["mph_from"], " mph", 0), fmt(p["peak_psi"], " psi"), fmt(p["min_timing"], "\u00b0"),
              f"{fmt(p['min_lambda'], '', 3)} ({fmt(p['min_lambda_cmd'], '', 3)})", fmt(p["min_rail"], " bar", 0),
              fmt(p["iat"], " \u00b0C", 0)] for k, p in enumerate(a["pulls"])], numeric=(1, 2, 5, 6, 8, 9))
            + img(charts.get("pulls"), "Boost, timing and lambda against rpm for each pull") + "</section>")
    quality = "".join(f"<li>{badge(st)}<span>{e(t)}</span></li>" for st, t in a["quality"]) or \
        f"<li>{badge('pass')}<span>No data-quality issues.</span></li>"
    S2 = S
    events = (f"Deceleration fuel cut: {fmt(S2['fuel_cut_s'], ' s', 0)}. Commanded-rich events: {S2['rich_events']} "
              f"({S2['rich_after_cut']} right after a fuel cut: the ECU refilling the catalyst).")
    body = (head
            + f"<section><h2 style='margin-top:0'>Findings</h2><ul class='f'>{findings}</ul></section>"
            + f"<section><h2 style='margin-top:0'>At a glance</h2>{glance}</section>"
            + f"<section><h2 style='margin-top:0'>Drive trace</h2>{img(charts.get('trace'), 'Speed, rpm and boost over the drive')}</section>"
            + f"<section><h2 style='margin-top:0'>Sensor checks</h2>{checks}</section>"
            + f"<section><h2 style='margin-top:0'>Fuel trims</h2><p class='muted'>Medians over rows with coolant \u2265 "
              f"{T['warm_coolant_c']} \u00b0C and commanded \u03bb {T['closed_loop'][0]}\u2013{T['closed_loop'][1]}. "
              f"Positive = the ECU is adding fuel.</p>{trims}</section>"
            + f"<section><h2 style='margin-top:0'>Boost, fueling and timing</h2><p class='muted'>Medians per condition. "
              f"Boost is gauge pressure (charge pressure minus {fmt(S['baro_kpa'], ' kPa')} barometric).</p>{conds}"
              f"<p>{e(events)}</p>{img(charts.get('boost_rpm'), 'Boost against engine speed')}</section>"
            + pulls
            + f"<section><h2 style='margin-top:0'>Temperatures</h2>{img(charts.get('temps'), 'Coolant, intake and ambient temperature over the drive')}</section>"
            + f"<section><h2 style='margin-top:0'>Data quality</h2><ul class='f'>{quality}</ul></section>")
    return page(f"Drive {when}", body)


def render_index(df, hist_png):
    d = df.sort_values(["start", "drive_id"], ascending=False, na_position="last")
    rows = [[f"<a href='{e(r.drive_id)}.html'>{e(str(r.start).replace('T', ' ')[:16] if isinstance(r.start, str) else r.drive_id)}</a>",
             e(r.tag), badge(r.status), fmt(r.duration_min, " min"), fmt(r.distance_mi, " mi"),
             fmt(r.trim_idle_total, "%", 1, True), fmt(r.trim_cruise_total, "%", 1, True), fmt(r.peak_boost_psi, " psi"),
             fmt(r.coolant_max_c, " \u00b0C", 0), str(int(r.pulls)) if pd.notna(r.pulls) else "\u2014"]
            for r in d.itertuples()]
    drives = table(["Start", "Tag", "Status", "Length", "Distance", "Idle trim", "Cruise trim", "Peak boost",
                    "Coolant max", "Pulls"], rows, numeric=(3, 4, 5, 6, 7, 8, 9))
    full = df[~df["short"].astype(bool)]
    comp = []
    for t in dict.fromkeys(df["tag"]):
        g, gf = df[df["tag"] == t], full[full["tag"] == t]
        comp.append([f"<b>{e(t)}</b>", str(len(g)), fmt(g["duration_min"].sum(), " min", 0), fmt(g["distance_mi"].sum(), " mi"),
                     fmt(med(g["trim_idle_total"]), "%", 1, True), fmt(med(gf["trim_cruise_total"]), "%", 1, True),
                     fmt(gf["peak_boost_psi"].max() if len(gf) else None, " psi"), fmt(med(gf["rail_cruise_bar"]), " bar", 0),
                     fmt(med(gf["timing_cruise_deg"]), "\u00b0"), fmt(g["coolant_max_c"].max(), " \u00b0C", 0)])
    comp_t = table(["Tag", "Drives", "Time", "Distance", "Idle trim", "Cruise trim", "Peak boost", "Cruise rail",
                    "Cruise timing", "Coolant max"], comp, numeric=tuple(range(1, 10)))
    body = (f"<h1>CarPi drives</h1><p class='sub'>{len(df)} drives \u00b7 {fmt(df['distance_mi'].sum(), ' mi')} \u00b7 "
            f"{fmt(df['duration_min'].sum() / 60, ' h')} logged</p>"
            + f"<section><h2 style='margin-top:0'>Compare by tag</h2><p class='muted'>Idle trim uses every drive; "
              f"cruise and load columns leave out short logs. Values are medians across drives.</p>{comp_t}</section>"
            + (f"<section><h2 style='margin-top:0'>Idle fuel trim over time</h2>{img(hist_png, 'Idle fuel trim per drive, by tag')}"
               f"<p class='muted'>The line at +{T['idle_vs_cruise_trim_pct']}% is where an idle-only correction starts "
               f"to point at a vacuum leak. After a PCV fix, new drives should sit closer to 0.</p></section>" if hist_png else "")
            + f"<section><h2 style='margin-top:0'>All drives</h2>{drives}</section>")
    return page("CarPi drives", body)


# ============================================================== driver
FLAT_KEYS = ["drive_id", "tag", "start", "start_note", "status", "warnings", "short", "complete", "duration_min",
             "engine_min", "engine_starts", "rows", "rate_hz", "max_gap_s", "distance_mi", "avg_moving_mph", "max_mph",
             "idle_min", "idle_rpm", "max_rpm", "peak_boost_psi", "peak_boost_rpm", "time_over_3psi_s", "pulls",
             "trim_idle_total", "trim_low_total", "trim_cruise_total", "trim_high_total", "idle_stft", "idle_ltft",
             "lambda_err_cruise", "rail_idle_bar", "rail_cruise_bar", "rail_max_bar", "timing_cruise_deg",
             "timing_dip_s", "coolant_start_c", "coolant_max_c", "iat_max_c", "ambient_c", "volts_median",
             "fuel_start_pct", "fuel_end_pct", "fuel_cut_s", "rich_events", "baro_kpa", "source", "source_mtime",
             "source_size", "version"]


def main():
    ap = argparse.ArgumentParser(description="Analyze CarPi drive logs into HTML reports.")
    ap.add_argument("inputs", nargs="*", help="drive CSVs, folders, wildcards, or .txt lists of paths")
    ap.add_argument("--out", default="reports", help="report folder (default: ./reports)")
    ap.add_argument("--force", action="store_true", help="re-analyze drives already in the index")
    ap.add_argument("--tag", help="tag for drives without a .json sidecar")
    ap.add_argument("--open", action="store_true", help="open index.html when done")
    args = ap.parse_args()

    inputs = find_inputs(args.inputs or default_input())
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    index_csv = out / "drives.csv"
    index = pd.read_csv(index_csv) if index_csv.exists() else pd.DataFrame(columns=FLAT_KEYS)

    if not inputs and index.empty:
        sys.exit("No drive CSVs found. Pass files or a folder, e.g.  python analyze_drives.py logs/")

    done = 0
    for p in inputs:
        st = p.stat()
        prev = index[index["drive_id"] == p.stem]
        if (not args.force and len(prev) and abs(float(prev["source_mtime"].iloc[0]) - st.st_mtime) < 1e-3
                and int(prev["source_size"].iloc[0]) == st.st_size and str(prev["version"].iloc[0]) == VERSION):
            print(f"  up to date: {p.name}")
            continue
        try:
            d = load_drive(p, args.tag)
            if len(d["s"]) < 10:
                print(f"  skipped (fewer than 10 rows): {p.name}")
                continue
            a = analyze(d)
        except Exception as ex:  # keep going: one bad file shouldn't stop the batch
            print(f"  FAILED {p.name}: {type(ex).__name__}: {ex}")
            continue
        S = a["summary"]
        S.update(source_mtime=st.st_mtime, source_size=st.st_size)
        charts = dict(trace=chart_trace(a["s"]), temps=chart_temps(d["raw"]), boost_rpm=chart_boost_rpm(a["s"]),
                      pulls=chart_pulls(a["s"], a["pulls"]))
        (out / f"{p.stem}.html").write_text(render_drive(a, charts), encoding="utf-8")
        payload = {k: a[k] for k in ("summary", "findings", "quality", "checks", "trims", "conds")}
        payload["pulls"] = [{k: v for k, v in pl.items() if k != "idx"} for pl in a["pulls"]]
        (out / f"{p.stem}.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        row = pd.DataFrame([{k: S.get(k) for k in FLAT_KEYS}])
        index = pd.concat([index[index["drive_id"] != p.stem], row], ignore_index=True)
        done += 1
        top = next((t for st_, t in a["findings"] if st_ in ("crit", "warn")), "no problems found")
        print(f"  analyzed {p.name}: {S['tag']}, {S['duration_min']} min, {S['distance_mi']} mi, "
              f"{STATUS[S['status']][2].lower()} \u2014 {top[:90]}")

    index = index.sort_values(["start", "drive_id"], na_position="last").reset_index(drop=True)
    index.to_csv(index_csv, index=False)
    for c in ("short",):
        index[c] = index[c].map(lambda v: str(v).lower() == "true" if not isinstance(v, bool) else v)
    (out / "index.html").write_text(render_index(index, chart_history(index)), encoding="utf-8")
    print(f"\n{done} drive(s) analyzed, {len(index)} in the index \u2192 {(out / 'index.html').resolve()}")
    if args.open:
        webbrowser.open((out / "index.html").resolve().as_uri())


if __name__ == "__main__":
    main()
