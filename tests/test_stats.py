"""carpi_stats.py: drive analysis on the Pi. Standard library only, like the code it tests."""
import json
import math
import re
from pathlib import Path

import pytest

import carpi_stats as cs

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "drive-2026-09-25_203526.csv"
COLS = ["time", "elapsed_s", "rpm", "speed_kph", "pedal_pct", "throttle_pct", "load_pct", "timing_deg",
        "stft_pct", "ltft_pct", "lambda", "lambda_cmd", "coolant_c", "iat_c", "volts",
        "boost_act_kpa", "baro_kpa"]
# warm, closed-loop idle with a vacuum leak's +8 % total trim
WARM_IDLE = {"rpm": 800, "speed_kph": 0, "pedal_pct": 14.5, "throttle_pct": 3, "load_pct": 20,
             "timing_deg": 2, "stft_pct": 1, "ltft_pct": 7, "lambda": 1.0, "lambda_cmd": 1.0,
             "coolant_c": 90, "iat_c": 25, "volts": 14.2, "boost_act_kpa": 98, "baro_kpa": 98}


def csv_text(rows, cols=COLS, dt=0.13):
    """Rows (dicts) -> CSV text, one row every dt seconds; missing keys and None are blank cells."""
    out = [",".join(cols)]
    for i, r in enumerate(rows):
        r = {"time": "2026-11-03T07:00:00", "elapsed_s": round(i * dt, 3), **r}
        out.append(",".join("" if r.get(c) is None else str(r[c]) for c in cols))
    return "\n".join(out) + "\n"


def drive(rows, done=True, **kw):
    d = cs.Drive()
    d.feed(csv_text(rows, **kw))
    if done:
        d.finish()
    return d


def col(d, name):
    return list(d.data[name])


def test_blanks_and_garbled_values_take_the_last_value():
    d = drive([{"rpm": 800}, {"rpm": None}, {"rpm": "12x"}, {"rpm": 900}])
    assert col(d, "rpm") == [800, 800, 800, 900] and d.n == 4


def test_values_before_the_first_reading_are_nan():
    d = drive([{"rpm": 800}, {"rpm": 810, "coolant_c": 40}])
    assert math.isnan(d.data["coolant_c"][0]) and d.data["coolant_c"][1] == 40


def test_a_row_without_its_time_is_dropped_whole():
    text = csv_text([{"rpm": 800}, {"rpm": 900}, {"rpm": None}]).replace(",0.13,900,", ",,900,")
    d = cs.Drive()
    d.feed(text)
    d.finish()
    assert col(d, "rpm") == [800, 800]           # the 900 never counted, so the blank after it is 800


def test_power_cut_damage_is_ignored():
    d = cs.Drive()
    d.feed(csv_text([{"rpm": 800}, {"rpm": 810}]) + "2026-11-03T07:00:00,0.26,8")   # cut-off line
    d.feed("\0" * 512)                                                               # NUL padding
    d.finish()
    assert col(d, "rpm") == [800, 810]


def test_any_chunking_reads_the_same_rows():
    rows = [dict(WARM_IDLE, rpm=800 + i) for i in range(30)]
    whole = drive(rows)
    d = cs.Drive()
    for ch in csv_text(rows):                         # one character at a time
        d.feed(ch)
    d.finish()
    assert d.n == whole.n and col(d, "rpm") == col(whole, "rpm") and list(d.flags) == list(whole.flags)


def test_derived_values():
    d = drive([{"boost_act_kpa": 150, "stft_pct": 1.5, "ltft_pct": 7, "lambda": 1.02, "lambda_cmd": 1.0},
               {"boost_act_kpa": 150, "baro_kpa": 98, "rail_kpa": 5600}],
              cols=COLS + ["rail_kpa"])
    assert math.isnan(d.data["boost_psi"][0])           # no barometer reading yet (as the viewer)
    assert d.data["boost_psi"][1] == pytest.approx((150 - 98) / 6.895)
    assert col(d, "trim_total") == [8.5, 8.5]
    assert d.data["lambda_err"][0] == pytest.approx(0.02) and d.data["rail_bar"][1] == 56


def test_columns_lists_only_columns_with_data():
    d = drive([{"rpm": 800, "boost_act_kpa": 100, "baro_kpa": 98, "oil_c": 60}], cols=COLS + ["oil_c"])
    assert d.columns() == ["rpm", "boost_act_kpa", "baro_kpa", "boost_psi", "oil_c"]


def test_a_file_that_isnt_a_drive_is_refused():
    with pytest.raises(ValueError, match="elapsed_s"):
        cs.Drive().feed("a,b\n1,2\n")


def flags_of(**changes):
    d = drive([dict(WARM_IDLE, **changes)])
    return {name for name, bit in cs.FILTERS.items() if name != "no_shift" and d.flags[0] & bit}


def test_filter_flags_match_the_analyzer():
    assert flags_of() == {"running", "warm", "closed_loop", "idle"}
    assert flags_of(speed_kph=60, rpm=2000, pedal_pct=25) == {"running", "warm", "closed_loop", "moving", "cruise"}
    assert "idle" not in flags_of(rpm=1100)
    assert "cruise" not in flags_of(speed_kph=60, rpm=2000, pedal_pct=40)
    assert "warm" not in flags_of(coolant_c=79.9)
    assert "closed_loop" not in flags_of(lambda_cmd=0.85)
    assert "boost" in flags_of(boost_act_kpa=98 + 3.1 * 6.895)
    assert "hard" in flags_of(pedal_pct=45, throttle_pct=75)
    assert "running" not in flags_of(rpm=400)


def test_a_dsg_shift_marks_the_rows_within_0_4_s():
    rows = ([dict(WARM_IDLE, rpm=3000, timing_deg=10)] * 10 + [dict(WARM_IDLE, rpm=2400, timing_deg=-15)]
            + [dict(WARM_IDLE, rpm=2450, timing_deg=8)] * 10)
    d = drive(rows, dt=0.15)                            # the shift: rows 9 and 10, at 1.35 and 1.5 s
    t = col(d, "elapsed_s")
    assert [t[i] for i in range(d.n) if d.flags[i] & cs.SHIFT] == [1.05, 1.2, 1.35, 1.5, 1.65, 1.8]


def test_rows_settle_half_a_second_after_they_arrive():
    d = drive([dict(WARM_IDLE)] * 20, done=False)       # 0 … 2.47 s: settled while t < 1.97
    assert d.settled == 16 and len(d.flags) == 16
    d.finish()
    assert d.settled == 20 and len(d.flags) == 20


def test_trim_by_rpm_band_counts_warm_closed_loop_rows_only():
    cruise = dict(WARM_IDLE, rpm=2000, speed_kph=80, stft_pct=-0.5, ltft_pct=0.5)
    cold = dict(WARM_IDLE, coolant_c=50, stft_pct=20)
    rich = dict(WARM_IDLE, lambda_cmd=0.85, stft_pct=-20)
    s = drive([dict(WARM_IDLE)] * 10 + [cruise] * 10 + [cold] * 10 + [rich] * 10, dt=1.0).summary()
    b = {x["label"]: x for x in s["trim_bands"]}
    assert b["Idle, under 1,000"]["total_pct"] == 8.0 and b["Idle, under 1,000"]["seconds"] == 9.0
    assert b["1,800–2,500"] == {"label": "1,800–2,500", "seconds": 10.0, "stft_pct": -0.5,
                                "ltft_pct": 0.5, "total_pct": 0.0}
    assert b["2,500 and up"]["seconds"] == 0.0 and b["2,500 and up"]["total_pct"] is None
    assert s["idle_rpm"] == 800 and s["rows"] == 40 and s["duration_s"] == 39.0


def test_distance_speed_and_peaks():
    rows = [dict(WARM_IDLE, speed_kph=36, rpm=2000) for _ in range(11)]       # 10 s at 36 km/h
    rows[5].update(boost_act_kpa=98 + 12 * 6.895, pedal_pct=70, timing_deg=-2)
    s = drive(rows, dt=1.0).summary()
    assert s["distance_km"] == 0.1 and s["max_speed_kph"] == 36
    assert s["peak_boost"] == {"psi": 12.0, "rpm": 2000, "pedal": 70.0}
    assert s["min_timing_boost"] == {"deg": -2.0, "rpm": 2000}
    assert s["max_coolant_c"] == 90 and s["max_iat_c"] == 25


def test_missing_columns_give_none_not_errors():
    d = drive([{"rpm": 800}] * 5, cols=["time", "elapsed_s", "rpm"])
    s = d.summary()
    assert s["idle_rpm"] is None and s["peak_boost"] is None and s["max_speed_kph"] is None
    assert s["trim_bands"][0]["total_pct"] is None and d.events() == [] and s["rows"] == 5


def test_low_voltage_needs_a_minute_and_bridges_short_gaps():
    low, ok = dict(WARM_IDLE, volts=12.0), dict(WARM_IDLE)
    evs = drive([low] * 40 + [ok] * 5 + [low] * 30 + [ok] * 20, dt=1.0).events()
    assert evs == [{"kind": "low_volts", "level": "warn", "t0": 0.0, "t1": 74.0,
                    "text": "min 12.00 V for 1.2 min", "ongoing": False}]
    assert drive([low] * 50 + [ok] * 20, dt=1.0).events() == []          # under a minute


def test_lean_under_boost_needs_3_rows():
    boost = dict(WARM_IDLE, rpm=3000, speed_kph=60, boost_act_kpa=98 + 8 * 6.895, **{"lambda": 1.10})
    idle = [dict(WARM_IDLE)] * 5
    assert drive(idle + [boost] * 2 + idle).events() == []
    (e,) = drive(idle + [boost] * 3 + idle).events()
    assert (e["kind"], e["level"], e["text"]) == ("lean_boost", "crit", "λ up to 1.100 (cmd 1.000)")


def test_hard_acceleration_text_matches_the_analyzer():
    pull = [dict(WARM_IDLE, rpm=2000 + 200 * i, speed_kph=50 + i, pedal_pct=60, throttle_pct=90,
                 boost_act_kpa=98 + (5 + i) * 6.895, timing_deg=6 - i / 2, **{"lambda": 0.86})
            for i in range(10)]
    evs = [e for e in drive([dict(WARM_IDLE)] * 5 + pull + [dict(WARM_IDLE)] * 10).events()
           if e["kind"] == "pull"]
    assert [e["text"] for e in evs] == [
        "2,000→3,800 rpm, peak 14.0 psi, min timing 1.5°, min λ 0.860 (shifts excluded)"]


def test_an_event_still_going_shows_as_ongoing():
    d = drive([dict(WARM_IDLE, volts=12.0)] * 70, done=False, dt=1.0)
    (e,) = d.events()
    assert e["ongoing"] and e["t1"] == 68.0
    d.finish()
    (e,) = d.events()
    assert not e["ongoing"] and e["t1"] == 69.0


TRIM = ["running", "warm", "closed_loop"]


def test_a_map_bins_cells_like_the_analyzer():
    rows = ([dict(WARM_IDLE, load_pct=25)] * 6                                  # +8 % at 800 rpm, 25 %
            + [dict(WARM_IDLE, rpm=2100, load_pct=41, stft_pct=0, ltft_pct=1)] * 5
            + [dict(WARM_IDLE, rpm=2100, load_pct=45, stft_pct=2, ltft_pct=1)] * 5)
    g = drive(rows).grid("rpm", "load_pct", "trim_total", TRIM)
    v = g.view("mean")
    assert v["x"] == {"col": "rpm", "step": 500, "start": 500} and v["y"] == {"col": "load_pct", "step": 10, "start": 20}
    assert (v["nx"], v["ny"], v["rows"]) == (4, 3, 16)
    assert v["cells"] == [[0, 0, 8.0, 6], [3, 2, 2.0, 10]]
    assert g.view("max")["cells"][1][2] == 3.0 and g.view("min")["cells"][1][2] == 1.0
    assert g.view("rows")["cells"][1][2] == 10


def test_map_filters():
    d = drive([dict(WARM_IDLE, coolant_c=40)] * 6 + [dict(WARM_IDLE)] * 6)
    assert d.grid("rpm", "load_pct", "trim_total", ["warm"]).view()["rows"] == 6
    assert d.grid("rpm", "load_pct", "trim_total").view()["rows"] == 12
    shift = ([dict(WARM_IDLE, rpm=3000, timing_deg=10)] * 10 + [dict(WARM_IDLE, rpm=2400, timing_deg=-15)]
             + [dict(WARM_IDLE, rpm=2450, timing_deg=8)] * 10)
    assert drive(shift, dt=0.15).grid("rpm", "load_pct", "timing_deg", ["no_shift"]).view()["rows"] == 15


def test_a_wide_map_merges_bins_to_fit_12_across():
    rows = [dict(WARM_IDLE, rpm=500 + 250 * i) for i in range(25) for _ in range(5)]   # 500…6500 rpm
    v = drive(rows).grid("rpm", "load_pct", "trim_total").view()
    assert v["x"]["step"] == 1000 and v["nx"] <= 12 and v["rows"] == 125


def test_cells_under_5_rows_are_left_out_and_dont_widen_the_map():
    v = drive([dict(WARM_IDLE)] * 6 + [dict(WARM_IDLE, rpm=6000)] * 2).grid("rpm", "load_pct", "trim_total").view()
    assert v["nx"] == 1 and v["x"]["step"] == 500 and v["rows"] == 8


def test_map_errors():
    d = drive([dict(WARM_IDLE)])
    with pytest.raises(ValueError, match="oil_c isn't in this drive"):
        d.grid("rpm", "load_pct", "oil_c")
    with pytest.raises(ValueError, match="elapsed_s isn't in this drive"):
        d.grid("elapsed_s", "load_pct", "rpm")
    with pytest.raises(ValueError, match="unknown filter"):
        d.grid("rpm", "load_pct", "trim_total", ["fast"])
    with pytest.raises(ValueError, match="stat"):
        d.grid("rpm", "load_pct", "trim_total").view("median")


def test_only_the_newest_6_maps_are_kept():
    d = drive([dict(WARM_IDLE)] * 6)
    for z in ("rpm", "speed_kph", "pedal_pct", "throttle_pct", "load_pct", "timing_deg", "stft_pct"):
        d.grid("rpm", "load_pct", z)
    assert len(d._grids) == 6


def test_a_map_keeps_up_with_a_growing_drive():
    text = csv_text([dict(WARM_IDLE)] * 40)
    half = text.index("\n", len(text) // 2) + 1
    d = cs.Drive()
    d.feed(text[:half])
    g = d.grid("rpm", "load_pct", "trim_total")
    assert 0 < g.view()["rows"] < 40
    d.feed(text[half:])
    d.finish()
    assert g.view()["rows"] == 40


def test_a_full_map_is_small_enough_to_send():
    rows = [dict(WARM_IDLE, rpm=500 * (1 + i % 12) + 123.456, load_pct=10 * (i // 12) + 3.21,
                 stft_pct=-1.23456 * i) for i in range(144) for _ in range(5)]
    v = drive(rows).grid("rpm", "load_pct", "trim_total").view()
    assert (v["nx"], v["ny"]) == (12, 12) and len(json.dumps(v, separators=(",", ":"))) < 3000


def analysis_of(d):
    maps = [d.grid("rpm", "load_pct", "trim_total", TRIM).view("mean"),
            d.grid("rpm", "pedal_pct", "boost_psi", ["running", "no_shift"]).view("max")]
    return d.summary(), d.events(), maps


def test_feeding_the_sample_in_pieces_gives_the_same_analysis():
    text = SAMPLE.read_text(encoding="utf-8")
    whole = cs.Drive()
    whole.feed(text)
    whole.finish()
    for size in (97, 4096):
        d = cs.Drive()
        for i in range(0, len(text), size):
            d.feed(text[i:i + size])
        d.finish()
        assert analysis_of(d) == analysis_of(whole)


def test_the_page_knows_every_column_and_event():
    """analysis.js keeps its own labels (replies stay small); they must match COLUMNS and DETECTORS."""
    js = (ROOT / "web" / "config" / "analysis.js").read_text(encoding="utf-8")
    cols = {m[1]: (m[2], m[3], float(m[4])) for m in
            re.finditer(r'^\s*(\w+): \["([^"]*)", "([^"]*)", ([\d.]+)\],?$', js, re.M)}
    assert cols == {c: (label, unit, float(s)) for c, (label, unit, s) in cs.COLUMNS.items()}
    labels = dict(re.findall(r'^\s*(\w+): "([^"]+)",? // event$', js, re.M))
    assert labels == {kind: label for kind, label, *_ in cs.DETECTORS}
