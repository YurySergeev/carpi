"""carpi_stats.py (on the Pi, no pandas) against the viewer's analysis on the sample drive: the
phone's Analysis tab and the public viewer must show the same numbers."""
import sys
from pathlib import Path

import pytest

import carpi_analysis                                  # analysis/ is on the path (conftest.py)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import carpi_stats  # noqa: E402

SAMPLE = ROOT / "data" / "drive-2026-09-25_203526.csv"


@pytest.fixture(scope="module")
def both():
    d = carpi_stats.Drive()
    d.feed(SAMPLE.read_text(encoding="utf-8"))
    d.finish()
    return d.summary(), carpi_analysis.load_drive(SAMPLE)


def test_trim_bands_match_the_viewer(both):
    s, df = both
    for mine, theirs in zip(s["trim_bands"], carpi_analysis.trim_bands(df)):
        assert mine["label"] == theirs["label"]
        assert mine["seconds"] == pytest.approx(theirs["seconds"], abs=0.05)
        for key in ("stft_pct", "ltft_pct", "total_pct"):
            if theirs[key] is None:
                assert mine[key] is None
            else:
                assert mine[key] == pytest.approx(theirs[key], abs=0.006), (mine["label"], key)


def test_headline_numbers_match_the_viewer(both):
    s, df = both
    v = carpi_analysis.summarize(df)
    assert s["rows"] == v["samples"]
    assert s["duration_s"] == pytest.approx(v["duration_s"], abs=0.05)
    assert s["distance_km"] * 0.621371 == pytest.approx(v["distance_mi"], abs=0.01)
    assert s["max_speed_kph"] * 0.621371 == pytest.approx(v["max_speed_mph"], abs=0.06)
    assert s["idle_rpm"] == pytest.approx(v["idle_rpm"], abs=0.5)
    assert s["peak_boost"]["psi"] == pytest.approx(v["peak_boost_psi"], abs=0.006)
    assert s["peak_boost"]["rpm"] == pytest.approx(v["peak_boost_rpm"], abs=0.5)
