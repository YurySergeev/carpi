"""carpi_stats.py against the PC analyzer (carpi_app) on the sample drive: row filters, event
detectors, a map and the column table. Local only: needs the analyzer's requirements.
Run from the CarPi folder:  python -m pytest carpi_app/tests -q"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import carpi_stats  # noqa: E402
from carpi_app import channels, events, filters  # noqa: E402
from carpi_app.views import map3d  # noqa: E402

SAMPLE = ROOT / "data" / "drive-2026-09-25_203526.csv"


@pytest.fixture(scope="module")
def drive():
    d = carpi_stats.Drive()
    d.feed(SAMPLE.read_text(encoding="utf-8"))
    d.finish()
    return d


def frame(d):
    """The same rows as a DataFrame, so the analyzer's code sees exactly carpi_stats' inputs."""
    return pd.DataFrame({c: np.array(a) for c, a in d.data.items()})


def test_row_filters_match_the_analyzer(drive):
    df = frame(drive)
    assert set(carpi_stats.FILTERS) == set(filters.FILTERS)
    for key, bit in carpi_stats.FILTERS.items():
        theirs = filters.FILTERS[key][1](df).fillna(False).astype(bool).to_numpy()
        mine = np.array([bool(f & bit) for f in drive.flags])
        assert ((~mine if key == "no_shift" else mine) == theirs).all(), key


def test_events_match_the_analyzer(drive):
    kinds = [spec[0] for spec in carpi_stats.DETECTORS]
    theirs = sorted((e.kind, round(e.start_s, 1), round(e.end_s, 1), e.detail, e.severity)
                    for e in events.detect(frame(drive), kinds))
    mine = sorted((e["kind"], e["t0"], e["t1"], e["text"], e["level"]) for e in drive.events())
    assert mine == theirs


def test_fuel_trim_map_matches_the_analyzer(drive):
    df = frame(drive)
    keys = ["running", "warm", "closed_loop"]
    m, _ = filters.mask(df, keys)
    v = drive.grid("rpm", "load_pct", "trim_total", keys).view("mean", max_bins=1000)
    ex = v["x"]["start"] + 500 * np.arange(v["nx"] + 1)
    ey = v["y"]["start"] + 10 * np.arange(v["ny"] + 1)
    Z, N = map3d.table(df[m], "rpm", "load_pct", "trim_total", ex, ey, "mean", 5)
    theirs = {(ix, iy): (Z[iy, ix], N[iy, ix]) for iy in range(Z.shape[0]) for ix in range(Z.shape[1])
              if not np.isnan(Z[iy, ix])}
    mine = {(c[0], c[1]): (c[2], c[3]) for c in v["cells"]}
    assert mine.keys() == theirs.keys()
    for k, (val, n) in mine.items():
        assert n == theirs[k][1] and val == pytest.approx(theirs[k][0], rel=1e-3)


def test_column_table_matches_the_analyzer():
    for c, meta in channels.CHANNELS.items():
        assert carpi_stats.COLUMNS[c] == meta
    for c in carpi_stats.DERIVED:
        d = channels.DERIVED[c]
        assert carpi_stats.COLUMNS[c] == (d["label"], d["unit"], d["step"])
