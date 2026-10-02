"""Event detectors. Each one scans a drive and returns Events the app can jump to.

Add a detector:

    @detector("hot_iat", "Hot intake air")
    def _(df):
        m = df["iat_c"] > 60
        return [Event("hot_iat", a, b, f"IAT up to {df['iat_c'][i:j+1].max():.0f} °C")
                for a, b, i, j in spans(df, m, min_s=5)]
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

DETECTORS = {}   # kind -> (label, fn)


@dataclass
class Event:
    kind: str
    start_s: float
    end_s: float
    detail: str
    severity: str = "info"   # info / warn / crit


def detector(kind, label):
    def deco(fn):
        DETECTORS[kind] = (label, fn)
        return fn
    return deco


def _c(df, n):
    return df[n] if n in df.columns else pd.Series(np.nan, index=df.index)


def spans(df, m, min_s=0.0, min_rows=1, merge_gap_s=0.0):
    """Contiguous True runs of mask m -> [(start_s, end_s, i0, i1)] (row positions, inclusive)."""
    m = np.asarray(pd.Series(m).fillna(False), dtype=bool)
    t = df["elapsed_s"].to_numpy()
    if not m.any():
        return []
    edges = np.diff(np.concatenate(([0], m.astype(np.int8), [0])))
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0] - 1
    runs = []
    for a, b in zip(starts, ends):
        if runs and t[a] - t[runs[-1][1]] <= merge_gap_s:
            runs[-1][1] = b
        else:
            runs.append([a, b])
    return [(float(t[a]), float(t[b]), int(a), int(b)) for a, b in runs
            if (t[b] - t[a]) >= min_s and (b - a + 1) >= min_rows]


def shift_mask(df, pad_s=0.4):
    """DSG upshift under load: rpm falls >300 in one sample while the ECU pulls timing for torque
    reduction. Lambda and timing look alarming for ~0.3 s here; that's normal."""
    rpm = _c(df, "rpm")
    drop = rpm.diff() < -300
    cut = (_c(df, "timing_deg") <= -10) | (_c(df, "timing_deg").shift(-1) <= -10)
    core = (drop & cut) | (drop.shift(-1, fill_value=False) & cut)
    if not core.any():
        return pd.Series(False, index=df.index)
    t = df["elapsed_s"].to_numpy()
    out = np.zeros(len(df), dtype=bool)
    for i in np.where(core.to_numpy())[0]:
        out |= (t >= t[i] - pad_s) & (t <= t[i] + pad_s)
    return pd.Series(out, index=df.index)


def _rng(s, i, j):
    return s.iloc[i:j + 1]


# This car's pedal PID reads ~14.5% at rest and only ~60% at the floor, so "hard" is pedal >= 45%.
HARD_PEDAL, HARD_THROTTLE = 45, 75


@detector("pull", "Hard acceleration")
def _(df):
    m = (_c(df, "pedal_pct") >= HARD_PEDAL) & (_c(df, "throttle_pct") >= HARD_THROTTLE) & (_c(df, "rpm") > 1500)
    ok = ~shift_mask(df)
    out = []
    for a, b, i, j in spans(df, m, min_s=1.0, merge_gap_s=0.8):
        rpm, psi = _rng(_c(df, "rpm"), i, j), _rng(_c(df, "boost_psi"), i, j)
        tim = _rng(_c(df, "timing_deg").where(ok), i, j)
        lam = _rng(_c(df, "lambda").where(ok), i, j)
        out.append(Event("pull", a, b, f"{rpm.iloc[0]:,.0f}→{rpm.max():,.0f} rpm, peak {psi.max():.1f} psi, "
                                        f"min timing {tim.min():.1f}°, min λ {lam.min():.3f} (shifts excluded)"))
    return out


@detector("shift", "DSG shift under load")
def _(df):
    m = shift_mask(df, pad_s=0.15) & (_c(df, "boost_psi") > 3)
    rpm = _c(df, "rpm")
    return [Event("shift", a, b, f"{_rng(rpm, i, j).max():,.0f}→{_rng(rpm, i, j).min():,.0f} rpm")
            for a, b, i, j in spans(df, m)]


@detector("timing_dip", "Timing ≤ -5° under boost")
def _(df):
    m = (_c(df, "timing_deg") <= -5) & (_c(df, "boost_psi") > 5) & ~shift_mask(df)
    out = []
    for a, b, i, j in spans(df, m, min_rows=2, merge_gap_s=0.5):
        out.append(Event("timing_dip", a, b, f"min {_rng(_c(df, 'timing_deg'), i, j).min():.1f}° at "
                                              f"{_rng(_c(df, 'rpm'), i, j).median():,.0f} rpm, "
                                              f"{_rng(_c(df, 'boost_psi'), i, j).max():.1f} psi", "warn"))
    return out


@detector("lean_boost", "Lean under boost")
def _(df):
    m = ((_c(df, "lambda") > 1.08) & (_c(df, "lambda_cmd") <= 1.02) & (_c(df, "boost_psi") > 5)
         & ~shift_mask(df))
    return [Event("lean_boost", a, b, f"λ up to {_rng(_c(df, 'lambda'), i, j).max():.3f} "
                                      f"(cmd {_rng(_c(df, 'lambda_cmd'), i, j).median():.3f})", "crit")
            for a, b, i, j in spans(df, m, min_rows=3)]


@detector("low_volts", "Voltage below 12.4 V")
def _(df):
    m = (_c(df, "volts") < 12.4) & (_c(df, "rpm") > 400)
    return [Event("low_volts", a, b, f"min {_rng(_c(df, 'volts'), i, j).min():.2f} V for {(b - a) / 60:.1f} min",
                  "warn") for a, b, i, j in spans(df, m, min_s=60, merge_gap_s=10)]


@detector("high_trim", "Fuel trim beyond ±12%")
def _(df):
    tt = _c(df, "trim_total")
    m = (tt.abs() > 12) & (_c(df, "coolant_c") >= 80) & _c(df, "lambda_cmd").between(0.98, 1.02)
    return [Event("high_trim", a, b, f"{_rng(tt, i, j).median():+.1f}% median", "warn")
            for a, b, i, j in spans(df, m, min_s=10, merge_gap_s=3)]


@detector("long_idle", "Idle longer than 3 min")
def _(df):
    m = (_c(df, "speed_kph") == 0) & _c(df, "rpm").between(500, 1100)
    return [Event("long_idle", a, b, f"{(b - a) / 60:.1f} min, trim {_rng(_c(df, 'trim_total'), i, j).median():+.1f}%")
            for a, b, i, j in spans(df, m, min_s=180, merge_gap_s=5)]


@detector("peak_boost", "Peak boost")
def _(df):
    psi = _c(df, "boost_psi")
    if not psi.notna().any():
        return []
    k = int(np.nanargmax(psi.to_numpy()))
    t = float(df["elapsed_s"].iloc[k])
    return [Event("peak_boost", t - 2, t + 2, f"{psi.iloc[k]:.1f} psi at {_c(df, 'rpm').iloc[k]:,.0f} rpm")]


def detect(df, kinds=None):
    out = []
    for kind, (_, fn) in DETECTORS.items():
        if kinds and kind not in kinds:
            continue
        try:
            out += fn(df)
        except Exception as ex:   # one broken detector shouldn't hide the others
            out.append(Event(kind, 0, 0, f"detector failed: {type(ex).__name__}: {ex}", "warn"))
    return sorted(out, key=lambda e: e.start_s)
