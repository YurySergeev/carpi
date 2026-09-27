#!/usr/bin/env python3
"""
carpi_analysis.py - summarize one CarPi drive CSV.

Used by build_site.py for the drive viewer, and on its own as a quick report:
    python analysis/carpi_analysis.py data/drive-2026-09-25_203526.csv

The wall-clock `time` column is never used: it can be wrong until NTP syncs, and
the published site shouldn't reveal when the car was driven. Everything runs on
`elapsed_s`.
"""
import io
import math
import sys
from pathlib import Path

import pandas as pd

REQUIRED = ["elapsed_s", "rpm", "speed_kph", "stft_pct", "ltft_pct", "coolant_c",
            "lambda_cmd", "boost_act_kpa", "baro_kpa"]
KPH_TO_MPH = 0.621371
KPA_PER_PSI = 6.895
RUNNING_RPM = 400
BANDS = [("Idle, under 1,000", 0, 1000), ("1,000–1,800", 1000, 1800),
         ("1,800–2,500", 1800, 2500), ("2,500 and up", 2500, math.inf)]
# trace column -> decimals
TRACE_SIGNALS = {
    "speed_mph": 1, "rpm": 0, "boost_psi": 2, "trim_pct": 1, "load_pct": 1,
    "timing_deg": 1, "lambda": 3, "coolant_c": 0, "rail_bar": 1, "pedal_pct": 1,
}


class DriveError(ValueError):
    """The CSV can't be analyzed (missing file, columns or rows)."""


# ---------- loading and masks ----------

def load_drive(path):
    path = Path(path)
    if not path.exists():
        raise DriveError(f"{path}: file not found")
    # A power cut can leave NUL padding and a half-written last line; a cut-off
    # number ("1295.3" -> "12") would parse as a wrong value, so drop that line.
    raw = path.read_bytes().rstrip(b"\0")
    if not raw.endswith(b"\n"):
        raw = raw[: raw.rfind(b"\n") + 1]
    if not raw.strip():
        raise DriveError(f"{path.name}: no data")
    df = pd.read_csv(io.BytesIO(raw))
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise DriveError(f"{path.name}: missing column(s) {', '.join(missing)}")
    for col in df.columns.drop("time", errors="ignore"):   # a garbled value -> NaN, not a text column
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[df["elapsed_s"].notna()]
    if df.empty:
        raise DriveError(f"{path.name}: no rows")
    return df.ffill().reset_index(drop=True)  # slow PIDs are blank between samples


def _dt(df):
    return df["elapsed_s"].diff().fillna(0)


def _running(df):
    return df["rpm"] > RUNNING_RPM


def _steady(df):
    """Warm engine in closed loop: the conditions fuel trims are meaningful in."""
    return _running(df) & (df["coolant_c"] >= 80) & df["lambda_cmd"].between(0.98, 1.02)


def _warm_idle(df):
    return _steady(df) & (df["rpm"] < 1000) & (df["speed_kph"] == 0)


def _boost_psi(df):
    return (df["boost_act_kpa"] - df["baro_kpa"]) / KPA_PER_PSI


def _num(x):
    """NaN/None -> None, numpy scalars -> plain float, so results are JSON-safe."""
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _median(df, col, mask):
    if col not in df.columns or not mask.any():
        return None
    return _num(df.loc[mask, col].median())


# ---------- analysis ----------

def trim_bands(df, steady=True):
    """Fuel trim by rpm band. Total is the median of per-row STFT + LTFT."""
    base = _steady(df) if steady else _running(df)
    dt = _dt(df)
    total = df["stft_pct"] + df["ltft_pct"]
    out = []
    for label, lo, hi in BANDS:
        m = base & (df["rpm"] >= lo) & (df["rpm"] < hi)
        out.append({
            "label": label,
            "seconds": round(float(dt[m].sum()), 1),
            "stft_pct": _median(df, "stft_pct", m),
            "ltft_pct": _median(df, "ltft_pct", m),
            "total_pct": _num(total[m].median()) if m.any() else None,
        })
    return out


def summarize(df, steady_bands=None):
    """Headline numbers. Pass trim_bands(df, steady=True) if already computed."""
    if steady_bands is None:
        steady_bands = trim_bands(df, steady=True)
    dt = _dt(df)
    speed = df["speed_kph"]
    moving = speed > 0
    boost = _boost_psi(df)
    peak = boost.idxmax() if boost.notna().any() else None
    running = _running(df)
    return {
        "samples": int(len(df)),
        "duration_s": round(float(df["elapsed_s"].iloc[-1]), 1),
        "engine_start_s": _num(df.loc[running, "elapsed_s"].iloc[0]) if running.any() else None,
        "distance_mi": round(float((speed * dt).sum() / 3600 * KPH_TO_MPH), 2),
        "max_speed_mph": round(float(speed.max() * KPH_TO_MPH), 1),
        "avg_moving_mph": round(float(speed[moving].mean() * KPH_TO_MPH), 1) if moving.any() else None,
        "peak_boost_psi": round(float(boost[peak]), 2) if peak is not None else None,
        "peak_boost_rpm": _num(df.at[peak, "rpm"]) if peak is not None else None,
        "peak_boost_pedal_pct": _num(df.at[peak, "pedal_pct"]) if peak is not None and "pedal_pct" in df else None,
        "idle_rpm": _median(df, "rpm", _warm_idle(df)),
        "max_rpm": _num(df["rpm"].max()),
        "idle_trim_pct": steady_bands[0]["total_pct"],
    }


def _check(name, reference, measured, ok):
    return {"name": name, "reference": reference, "measured": measured, "pass": ok}


def sensor_checks(df):
    """Each decoded signal against a physical or expected reference.
    "pass" is None when the drive lacks the data to run the check."""
    idle, steady, running = _warm_idle(df), _steady(df), _running(df)
    cruise = steady & (df["rpm"] > 1500) & (df["speed_kph"] > 40)
    checks = []

    boost, baro = _median(df, "boost_act_kpa", idle), _median(df, "baro_kpa", idle)
    checks.append(_check(
        "Boost decoding", "At idle, boost pressure equals barometric pressure",
        f"{boost:.1f} kPa vs {baro:.0f} kPa" if None not in (boost, baro) else "no warm idle",
        abs(boost - baro) <= 2 if None not in (boost, baro) else None))

    lam, cmd = _median(df, "lambda", cruise), _median(df, "lambda_cmd", cruise)
    checks.append(_check(
        "Air-fuel control", "Actual λ tracks commanded λ at cruise",
        f"{lam:.3f} vs {cmd:.3f}" if None not in (lam, cmd) else "no cruise data",
        abs(lam - cmd) <= 0.02 if None not in (lam, cmd) else None))

    rail_idle = _median(df, "rail_kpa", idle)
    rail_max = _num(df["rail_kpa"].max()) if "rail_kpa" in df else None
    ok = None if None in (rail_idle, rail_max) else (30 <= rail_idle / 100 <= 100 and rail_max / 100 <= 220)
    checks.append(_check(
        "Fuel rail pressure", "Direct injection: tens of bar at idle, about 200 bar max",
        f"{rail_idle / 100:.0f} bar idle, {rail_max / 100:.0f} bar max" if ok is not None else "no data", ok))

    rpm = _median(df, "rpm", idle)
    checks.append(_check(
        "RPM decoding", "Warm idle around 800 rpm",
        f"{rpm:.0f} rpm" if rpm is not None else "no warm idle",
        650 <= rpm <= 950 if rpm is not None else None))

    volts = _median(df, "volts", running)
    checks.append(_check(
        "Charging", "Alternator 13.5–14.8 V while running",
        f"{volts:.1f} V median" if volts is not None else "no data",
        13.5 <= volts <= 14.8 if volts is not None else None))

    coolant = _num(df["coolant_c"].max())
    checks.append(_check(
        "Coolant", "Thermostat holds about 95–105 °C at light load",
        f"{coolant:.0f} °C peak" if coolant is not None else "no data",
        85 <= coolant <= 110 if coolant is not None else None))

    o2, o2trim = _median(df, "o2s2_v", steady), _median(df, "lt_o2s2_trim_pct", steady)
    ok = None if None in (o2, o2trim) else (0.6 <= o2 <= 0.9 and abs(o2trim) <= 3)
    checks.append(_check(
        "Catalyst", "Steady downstream O2 voltage, trim near zero",
        f"{o2:.2f} V, {o2trim:+.1f}% trim" if ok is not None else "no data", ok))
    return checks


def trace(df):
    """One-second medians of the signals the viewer plots, as JSON-safe columns."""
    src = pd.DataFrame({"t": df["elapsed_s"].astype(int)})
    src["speed_mph"] = df["speed_kph"] * KPH_TO_MPH
    src["rpm"] = df["rpm"]
    src["boost_psi"] = _boost_psi(df).clip(lower=0)
    src["trim_pct"] = (df["stft_pct"] + df["ltft_pct"]).where(_running(df))
    src["rail_bar"] = df["rail_kpa"] / 100 if "rail_kpa" in df else float("nan")
    for col in ("load_pct", "timing_deg", "lambda", "coolant_c", "pedal_pct"):
        src[col] = df[col] if col in df else float("nan")
    binned = src.groupby("t").median()
    out = {"t": [int(t) for t in binned.index]}
    for col, decimals in TRACE_SIGNALS.items():
        out[col] = [None if (v := _num(x)) is None else round(v, decimals) for x in binned[col]]
    return out


def analyze(path):
    df = load_drive(path)
    steady = trim_bands(df, steady=True)
    return {
        "summary": summarize(df, steady_bands=steady),
        "checks": sensor_checks(df),
        "bands": {"steady": steady, "all": trim_bands(df, steady=False)},
        "trace": trace(df),
    }


# ---------- CLI ----------

def _fmt(v, spec):
    return "–" if v is None else format(v, spec)


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: carpi_analysis.py <drive.csv>")
    sys.stdout.reconfigure(encoding="utf-8")   # λ and °C on a Windows console
    try:
        result = analyze(argv[1])
    except DriveError as e:
        sys.exit(str(e))
    s = result["summary"]
    print(f"{Path(argv[1]).name}: {s['duration_s'] / 60:.1f} min, {s['samples']:,} rows, "
          f"{s['distance_mi']:.1f} mi, {s['max_speed_mph']:.0f} mph max")
    print(f"peak boost {_fmt(s['peak_boost_psi'], '.1f')} psi at {_fmt(s['peak_boost_rpm'], '.0f')} rpm, "
          f"warm idle {_fmt(s['idle_rpm'], '.0f')} rpm\n")
    print(f"{'Fuel trim (steady state)':<26}{'time':>7}{'short':>8}{'long':>8}{'total':>8}")
    for b in result["bands"]["steady"]:
        print(f"{b['label']:<26}{b['seconds']:>6.0f}s{_fmt(b['stft_pct'], '+8.1f')}"
              f"{_fmt(b['ltft_pct'], '+8.1f')}{_fmt(b['total_pct'], '+8.1f')}")
    print()
    for c in result["checks"]:
        mark = {True: "pass", False: "FAIL", None: "skip"}[c["pass"]]
        print(f"[{mark}] {c['name']:<20} {c['measured']:<26} ({c['reference']})")


if __name__ == "__main__":
    main(sys.argv)
