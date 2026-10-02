"""Row filters shared by every view, plus a free-form pandas query.

Add one with the decorator; it appears in the sidebar automatically:

    @row_filter("hot", "Intake air above 50 °C")
    def _(df):
        return df["iat_c"] > 50
"""
import pandas as pd

from .safe_query import QueryError, evaluate

FILTERS = {}   # key -> (label, fn)


def row_filter(key, label):
    def deco(fn):
        FILTERS[key] = (label, fn)
        return fn
    return deco


def _c(df, n):
    return df[n] if n in df.columns else pd.Series(float("nan"), index=df.index)


@row_filter("running", "Engine running")
def _(df):
    return _c(df, "rpm") > 400


@row_filter("warm", "Warm engine (coolant ≥ 80 °C)")
def _(df):
    return _c(df, "coolant_c") >= 80


@row_filter("closed_loop", "Closed loop (λ cmd 0.98–1.02)")
def _(df):
    return _c(df, "lambda_cmd").between(0.98, 1.02)


@row_filter("idle", "Idle (stopped, 600–1000 rpm)")
def _(df):
    return (_c(df, "speed_kph") == 0) & _c(df, "rpm").between(600, 1000)


@row_filter("moving", "Moving")
def _(df):
    return _c(df, "speed_kph") > 0


@row_filter("cruise", "Steady cruise (>50 km/h, light pedal)")
def _(df):
    return ((_c(df, "speed_kph") > 50) & _c(df, "pedal_pct").between(15, 35)
            & _c(df, "rpm").between(1400, 2600))


@row_filter("boost", "Under boost (> 3 psi)")
def _(df):
    return _c(df, "boost_psi") > 3


@row_filter("hard", "Hard throttle (pedal ≥ 45%, throttle ≥ 75%)")
def _(df):
    from .events import HARD_PEDAL, HARD_THROTTLE
    return (_c(df, "pedal_pct") >= HARD_PEDAL) & (_c(df, "throttle_pct") >= HARD_THROTTLE)


@row_filter("no_shift", "Exclude DSG shift moments")
def _(df):
    from .events import shift_mask
    return ~shift_mask(df)


def mask(df, keys=(), query=None):
    """Boolean mask for the given filter keys AND the query. Returns (mask, error)."""
    m = pd.Series(True, index=df.index)
    for k in keys or ():
        if k in FILTERS:
            m &= FILTERS[k][1](df).fillna(False).astype(bool)
    err = None
    if (query or "").strip():
        try:
            m &= evaluate(df, query)
        except QueryError as ex:
            err = f"Query ignored: {ex}"
    return m, err
