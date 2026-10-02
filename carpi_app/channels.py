"""Channel names, units and derived channels.

Add a derived channel by writing a function and decorating it:

    @derived("map_psi", "Manifold pressure", "psi", default_step=1)
    def _(df):
        return df["map_kpa"] * 0.145038

It then shows up in every channel dropdown in the app.
"""
import numpy as np
import pandas as pd

# column -> (label, unit, default bin step for maps)
CHANNELS = {
    "rpm": ("Engine speed", "rpm", 500),
    "speed_kph": ("Vehicle speed", "km/h", 10),
    "pedal_pct": ("Accelerator pedal", "%", 10),
    "throttle_pct": ("Throttle plate", "%", 10),
    "load_pct": ("Calculated load", "%", 10),
    "abs_load_pct": ("Absolute load", "%", 10),
    "map_kpa": ("Manifold pressure", "kPa", 20),
    "boost_act_kpa": ("Charge pressure (abs)", "kPa", 20),
    "boost_cmd_kpa": ("Charge pressure cmd (abs)", "kPa", 20),
    "timing_deg": ("Ignition timing", "°", 2),
    "lambda": ("Lambda actual", "", 0.02),
    "lambda_cmd": ("Lambda commanded", "", 0.02),
    "stft_pct": ("Short-term fuel trim", "%", 2),
    "ltft_pct": ("Long-term fuel trim", "%", 2),
    "o2s1_ma": ("Upstream O2 current", "mA", 0.1),
    "o2s2_v": ("Downstream O2", "V", 0.1),
    "o2s2_trim_pct": ("Downstream O2 trim", "%", 2),
    "lt_o2s2_trim_pct": ("Downstream O2 LT trim", "%", 2),
    "rail_kpa": ("Fuel rail pressure", "kPa", 2000),
    "coolant_c": ("Coolant temp", "°C", 5),
    "iat_c": ("Intake air temp", "°C", 5),
    "ambient_c": ("Ambient temp", "°C", 5),
    "cat_temp_c": ("Catalyst temp", "°C", 50),
    "volts": ("Module voltage", "V", 0.25),
    "baro_kpa": ("Barometric pressure", "kPa", 1),
    "fuel_pct": ("Fuel level", "%", 10),
    "run_time_s": ("Engine run time", "s", 60),
}

DERIVED = {}   # name -> dict(fn, label, unit, step)


def derived(name, label, unit, default_step=None):
    def deco(fn):
        DERIVED[name] = dict(fn=fn, label=label, unit=unit, step=default_step)
        return fn
    return deco


def _c(df, name):
    return df[name] if name in df.columns else pd.Series(np.nan, index=df.index)


@derived("t_min", "Time into drive", "min", 5)
def _(df):
    return df["elapsed_s"] / 60.0


@derived("boost_psi", "Boost (gauge)", "psi", 2)
def _(df):
    baro = _c(df, "baro_kpa").median()
    baro = 101.3 if pd.isna(baro) else baro
    return (_c(df, "boost_act_kpa") - baro) / 6.895


@derived("trim_total", "Total fuel trim", "%", 2)
def _(df):
    return _c(df, "stft_pct") + _c(df, "ltft_pct")


@derived("lambda_err", "Lambda error (act - cmd)", "", 0.01)
def _(df):
    return _c(df, "lambda") - _c(df, "lambda_cmd")


@derived("rail_bar", "Fuel rail pressure", "bar", 20)
def _(df):
    return _c(df, "rail_kpa") / 100.0


@derived("mph", "Vehicle speed", "mph", 10)
def _(df):
    return _c(df, "speed_kph") * 0.621371


@derived("rpm_per_kph", "RPM per km/h (gear)", "", 5)
def _(df):
    spd = _c(df, "speed_kph")
    return (_c(df, "rpm") / spd).where(spd > 8)


@derived("accel_g", "Longitudinal accel", "g", 0.05)
def _(df):
    v = (_c(df, "speed_kph") / 3.6).rolling(7, center=True, min_periods=3).mean()
    dt = df["elapsed_s"].diff().rolling(7, center=True, min_periods=3).mean()
    return (v.diff() / dt / 9.81).clip(-1.5, 1.5)


def add_derived(df):
    for name, d in DERIVED.items():
        try:
            df[name] = d["fn"](df).astype("float32")
        except Exception:          # a broken formula should never take the app down
            df[name] = np.float32("nan")
    return df


def meta(ch):
    if ch in DERIVED:
        d = DERIVED[ch]
        return d["label"], d["unit"], d["step"]
    return CHANNELS.get(ch, (ch, "", None))


def label(ch, unit=True):
    name, u, _ = meta(ch)
    return f"{name} ({u})" if unit and u else name


def step(ch):
    return meta(ch)[2]


def options(columns):
    """Dropdown options for the columns that exist (and aren't empty), derived first."""
    out = [{"label": f"{label(c)}  ·  {c}", "value": c} for c in DERIVED if c in columns]
    out += [{"label": f"{label(c)}  ·  {c}", "value": c} for c in CHANNELS if c in columns]
    out += [{"label": c, "value": c} for c in columns
            if c not in CHANNELS and c not in DERIVED and c not in ("elapsed_s", "clock_ok", "time")]
    return out
