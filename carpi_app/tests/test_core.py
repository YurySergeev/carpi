"""Run from the CarPi folder:  python -m pytest carpi_app/tests -q"""
import json

import numpy as np
import pandas as pd

from carpi_app import events, filters
from carpi_app.channels import add_derived
from carpi_app.data import Store
from carpi_app.views.map3d import edges_for, table

HEADER = ("time,elapsed_s,clock_ok,rpm,map_kpa,boost_act_kpa,load_pct,timing_deg,stft_pct,lambda_cmd,lambda,"
          "pedal_pct,throttle_pct,speed_kph,coolant_c,iat_c,ltft_pct,volts,baro_kpa")


def frame(n=200, **cols):
    df = pd.DataFrame({"elapsed_s": np.arange(n) * 0.13, "rpm": 2000.0, "boost_act_kpa": 100.0, "baro_kpa": 98.0,
                       "timing_deg": 10.0, "lambda": 1.0, "lambda_cmd": 1.0, "load_pct": 50.0, "pedal_pct": 20.0,
                       "throttle_pct": 30.0, "speed_kph": 60.0, "coolant_c": 90.0, "stft_pct": 0.0,
                       "ltft_pct": 0.0, "volts": 14.2})
    for k, v in cols.items():
        df[k] = v
    return add_derived(df)


def test_spans_merge_and_min_length():
    df = frame(20)
    m = pd.Series([0, 1, 1, 0, 1, 1, 1, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0, 0], dtype=bool)
    assert len(events.spans(df, m)) == 3
    assert len(events.spans(df, m, merge_gap_s=0.3)) == 2          # first two runs are 0.26 s apart
    assert len(events.spans(df, m, min_rows=3)) == 1


def test_shift_is_not_reported_as_lean():
    # an upshift under boost: rpm falls 1,500 in one sample, timing cut, lambda spikes for 2 rows
    rpm = np.r_[np.linspace(3000, 6300, 100), np.linspace(4700, 5500, 100)]
    timing = np.full(200, 5.0)
    timing[100:102] = [-23.5, -4.5]
    lam = np.full(200, 0.99)
    lam[99:101] = [1.5, 1.28]
    df = frame(200, rpm=rpm, timing_deg=timing, boost_act_kpa=200.0, **{"lambda": lam})
    assert events.shift_mask(df).any()
    kinds = {e.kind for e in events.detect(df)}
    assert "shift" in kinds and "lean_boost" not in kinds


def test_real_lean_condition_is_reported():
    lam = np.full(200, 0.99)
    lam[50:60] = 1.15
    df = frame(200, boost_act_kpa=200.0, **{"lambda": lam})
    assert any(e.kind == "lean_boost" for e in events.detect(df))


def test_filters_and_query():
    df = frame(10, speed_kph=[0] * 5 + [60] * 5, rpm=[800] * 5 + [2000] * 5)
    m, err = filters.mask(df, ["idle"], None)
    assert m.sum() == 5 and err is None
    m, err = filters.mask(df, [], "rpm > 1500 and coolant_c >= 80")
    assert m.sum() == 5 and err is None
    m, err = filters.mask(df, [], "rpm >>> 3")  # bad syntax is reported, never raised
    assert m.all() and err and err.startswith("Query ignored")


def test_map_table_bins_and_min_rows():
    df = pd.DataFrame({"rpm": [900, 950, 1600, 1700, 1700, 2600], "load": [15, 18, 35, 38, 33, 70],
                       "timing": [1, 3, 10, 12, 11, 20]})
    ex, _ = edges_for(df["rpm"], 500)
    ey, _ = edges_for(df["load"], 10)
    Z, N = table(df, "rpm", "load", "timing", ex, ey, "mean", 2)
    assert np.nansum(N) == 6
    assert sorted(v for v in Z.flatten() if not np.isnan(v)) == [2.0, 11.0]   # single-row cells dropped


def test_store_dedupes_copies_and_reads_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr("carpi_app.config.CACHE_DIR", tmp_path / ".cache")
    rows = [HEADER] + [f"2026-10-01T12:00:{i:02d}.000,{i * 0.13:.2f},1,2000,40,99,30,10,0,1,1,20,30,50,90,30,2,14.2,98"
                       for i in range(50)]
    for d in ("logs/2026-10-01", "logs/copy"):
        p = tmp_path / d
        p.mkdir(parents=True)
        (p / "drive-120000.csv").write_text("\n".join(rows) + "\n")
        (p / "drive-120000.json").write_text(json.dumps({"tag": "post-pcv"}))
    s = Store([tmp_path / "logs"])
    assert len(s.drives) == 1
    info = next(iter(s.drives.values()))
    assert info.tag == "post-pcv" and info.start_ok and info.rows == 50
    df, total, err = s.frames(["missing"] + list(s.drives), ["warm"], None)
    assert total == 50 and len(df) == 50 and "boost_psi" in df
    did = next(iter(s.drives))
    assert list(df["drive"].cat.categories) == [did] and df["tag"].iloc[0] == "post-pcv"
    assert s.drive_at(["missing", did], df["order"].iloc[0]) == did


def test_app_builds_in_both_modes(tmp_path, monkeypatch):
    """Catches bad component props / duplicate outputs, which only fail when the layout is built."""
    import importlib
    from carpi_app import app as app_mod, config
    monkeypatch.setattr("carpi_app.config.CACHE_DIR", tmp_path / ".cache")
    for public in (False, True):
        monkeypatch.setattr(config, "PUBLIC", public)
        importlib.reload(app_mod)
        a = app_mod.create_app(Store([tmp_path]))
        assert a.layout is not None and len(a.callback_map) > 10


def test_examples_reference_real_controls():
    from carpi_app.examples import EXAMPLES
    from carpi_app.filters import FILTERS
    from carpi_app.views.guide import CONTROLS
    for e in EXAMPLES:
        assert e["tab"] in ("ts", "sc", "cmp", "map")
        assert set(e.get("filters", [])) <= set(FILTERS), e["id"]
        assert set(e["set"]) <= set(CONTROLS), e["id"]
