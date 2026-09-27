"""Tests for the drive analysis. The baseline numbers come from docs/baseline-2026-09-25.md."""
import math
from pathlib import Path

import pandas as pd
import pytest

import carpi_analysis as ca

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "data" / "drive-2026-09-25_203526.csv"


@pytest.fixture(scope="module")
def baseline():
    return ca.load_drive(BASELINE)


# ---------- real baseline drive ----------

def test_baseline_drops_truncated_last_row(baseline):
    assert len(baseline) == 9944


def test_baseline_summary_matches_report(baseline):
    s = ca.summarize(baseline)
    assert s["samples"] == 9944
    assert s["duration_s"] == pytest.approx(1295, abs=1)
    assert s["distance_mi"] == pytest.approx(11.8, abs=0.05)
    assert s["max_speed_mph"] == pytest.approx(65, abs=0.5)
    assert s["peak_boost_psi"] == pytest.approx(9.7, abs=0.05)
    assert s["peak_boost_rpm"] == pytest.approx(1987, abs=1)
    assert s["idle_rpm"] == pytest.approx(799, abs=1)
    assert s["max_rpm"] == pytest.approx(3222, abs=1)
    assert s["idle_trim_pct"] == pytest.approx(8.6, abs=0.05)


def test_baseline_steady_state_trim_bands_match_report(baseline):
    bands = ca.trim_bands(baseline, steady=True)
    assert [b["label"] for b in bands] == ["Idle, under 1,000", "1,000–1,800", "1,800–2,500", "2,500 and up"]
    assert [round(b["total_pct"], 1) for b in bands] == [8.6, 0.8, 0.8, 0.0]
    assert [round(b["stft_pct"], 1) for b in bands] == [3.9, -1.6, 0.0, -1.6]
    assert [round(b["ltft_pct"], 1) for b in bands] == [4.7, 3.1, 0.8, 1.6]
    assert bands[0]["seconds"] == pytest.approx(110, abs=2)


def test_all_running_bands_include_more_time(baseline):
    steady = ca.trim_bands(baseline, steady=True)
    everything = ca.trim_bands(baseline, steady=False)
    assert all(a["seconds"] >= s["seconds"] for a, s in zip(everything, steady))


def test_baseline_all_sensor_checks_pass(baseline):
    checks = ca.sensor_checks(baseline)
    assert len(checks) == 7
    failed = [c["name"] for c in checks if c["pass"] is not True]
    assert failed == []
    assert all(c["reference"] and c["measured"] for c in checks)


def test_baseline_trace_is_one_second_bins_without_wall_clock(baseline):
    tr = ca.trace(baseline)
    assert "time" not in tr
    assert 1250 <= len(tr["t"]) <= 1300
    assert all(len(col) == len(tr["t"]) for col in tr.values())
    assert set(ca.TRACE_SIGNALS) <= set(tr)
    # engine starts about 62 s in: trim must be null before that, set after
    assert tr["trim_pct"][10] is None
    assert tr["trim_pct"][300] is not None


def test_trace_values_are_json_safe(baseline):
    tr = ca.trace(baseline)
    for col in tr.values():
        for v in col:
            assert v is None or (isinstance(v, (int, float)) and math.isfinite(v))


# ---------- edge cases on small synthetic drives ----------

def _frame(rows):
    cols = ["elapsed_s", "rpm", "speed_kph", "stft_pct", "ltft_pct", "coolant_c",
            "lambda_cmd", "boost_act_kpa", "baro_kpa"]
    return pd.DataFrame(rows, columns=cols)


def _write(tmp_path, df):
    path = tmp_path / "drive.csv"
    df.to_csv(path, index=False)
    return path


def test_missing_required_column_raises(tmp_path):
    df = _frame([[0.0, 800, 0, 1, 1, 90, 1.0, 98, 98]]).drop(columns=["ltft_pct"])
    with pytest.raises(ca.DriveError, match="ltft_pct"):
        ca.load_drive(_write(tmp_path, df))


def test_empty_band_reports_none(tmp_path):
    rows = [[i * 0.1, 800, 0, 2.0, 3.0, 90, 1.0, 98, 98] for i in range(50)]
    df = ca.load_drive(_write(tmp_path, _frame(rows)))
    bands = ca.trim_bands(df, steady=True)
    assert bands[0]["total_pct"] == pytest.approx(5.0)
    assert bands[3] == {"label": "2,500 and up", "seconds": 0.0,
                        "stft_pct": None, "ltft_pct": None, "total_pct": None}


def test_check_without_inputs_is_not_run(tmp_path):
    rows = [[i * 0.1, 800, 0, 2.0, 3.0, 90, 1.0, 98, 98] for i in range(50)]
    df = ca.load_drive(_write(tmp_path, _frame(rows)))
    by_name = {c["name"]: c for c in ca.sensor_checks(df)}
    assert by_name["Charging"]["pass"] is None          # no volts column
    assert by_name["RPM decoding"]["pass"] is True       # 800 rpm warm idle


# ---------- power-cut damage at the end of a CSV ----------

HEADER = "time,elapsed_s,rpm,speed_kph,stft_pct,ltft_pct,coolant_c,lambda_cmd,boost_act_kpa,baro_kpa\n"
GOOD = "".join(f"t,{i * 0.1:.1f},800,36,2,3,90,1.0,98,98\n" for i in range(50))   # last elapsed 4.9


def _raw(tmp_path, text):
    path = tmp_path / "drive.csv"
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    return path


def test_partial_last_line_is_dropped(tmp_path):
    # power cut mid-line: elapsed_s "1295.3" was only written as "12"
    df = ca.load_drive(_raw(tmp_path, HEADER + GOOD + "t,12"))
    assert len(df) == 50
    s = ca.summarize(df)
    assert s["duration_s"] == pytest.approx(4.9)
    assert s["distance_mi"] > 0


def test_nul_padding_after_partial_line_is_dropped(tmp_path):
    # the filesystem kept the block but not the data: a value glued to NUL bytes
    df = ca.load_drive(_raw(tmp_path, (HEADER + GOOD + "t,5.0,80").encode() + b"\0" * 2000))
    assert len(df) == 50
    assert df["rpm"].dtype.kind in "if"   # numeric, not text


def test_garbage_value_does_not_turn_a_column_into_text(tmp_path):
    df = ca.load_drive(_raw(tmp_path, HEADER + GOOD + "t,5.0,8#0,36,2,3,90,1.0,98,98\n"))
    assert df["rpm"].dtype.kind == "f"
    assert ca.summarize(df)["max_rpm"] == 800
