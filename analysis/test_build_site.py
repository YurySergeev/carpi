"""Tests for the site build: manifest validation and the JSON it writes."""
import json
import re
import shutil
from pathlib import Path

import pytest

import build_site as bs
from test_analysis import BASELINE, REPO

ENTRY = """
[[drive]]
id = "{id}"
file = "{file}"
title = "Baseline"
date = 2026-09-25
tag = "baseline"
"""


def _manifest(tmp_path, *entries):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    shutil.copy(BASELINE, data / BASELINE.name)
    path = data / "drives.toml"
    path.write_text("".join(entries), encoding="utf-8")
    return path


PRE_PCV = ["2026-09-25-baseline", "2026-09-26-cold-start", "2026-09-26-first-pull"]


def test_manifest_loads_real_file():
    drives = bs.load_manifest(REPO / "data" / "drives.toml")
    assert [d["id"] for d in drives] == PRE_PCV
    assert drives[0]["date"] == "2026-09-25"
    assert {d["group"] for d in drives} == {"Before PCV repair"}
    assert all(d["csv"].exists() for d in drives)   # day-folder paths resolve


def test_build_defaults_missing_group_to_empty(tmp_path):
    out = tmp_path / "_site"
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    bs.build(_manifest(tmp_path, ENTRY.format(id="a", file=BASELINE.name)), web, out)
    site = json.loads((out / "data" / "drives.json").read_text(encoding="utf-8"))
    assert site["drives"][0]["group"] == ""


def test_manifest_rejects_duplicate_ids(tmp_path):
    e = ENTRY.format(id="a", file=BASELINE.name)
    with pytest.raises(bs.BuildError, match="duplicate id 'a'"):
        bs.load_manifest(_manifest(tmp_path, e, e))


def test_manifest_rejects_missing_csv(tmp_path):
    with pytest.raises(bs.BuildError, match="nope.csv"):
        bs.load_manifest(_manifest(tmp_path, ENTRY.format(id="a", file="nope.csv")))


def test_manifest_rejects_missing_keys(tmp_path):
    path = _manifest(tmp_path, '[[drive]]\nid = "a"\n')
    with pytest.raises(bs.BuildError, match="missing file, title, date"):
        bs.load_manifest(path)


def test_manifest_rejects_unsafe_id(tmp_path):
    with pytest.raises(bs.BuildError, match="id"):
        bs.load_manifest(_manifest(tmp_path, ENTRY.format(id="../x", file=BASELINE.name)))


def test_build_writes_index_drive_json_and_web(tmp_path):
    out = tmp_path / "_site"
    bs.build(REPO / "data" / "drives.toml", REPO / "web", out)
    site = json.loads((out / "data" / "drives.json").read_text(encoding="utf-8"))
    assert site["upcoming"] == "After PCV repair"
    index = site["drives"]
    assert [d["id"] for d in index] == PRE_PCV
    assert [d["group"] for d in index] == ["Before PCV repair"] * 3
    assert index[0]["summary"]["samples"] == 9944
    assert index[1]["summary"]["peak_boost_psi"] < index[2]["summary"]["peak_boost_psi"]
    assert len(index[0]["checks"]) == 7
    for drive_id in PRE_PCV:
        assert (out / "data" / f"{drive_id}.json").exists()
    drive = json.loads((out / "data" / "2026-09-25-baseline.json").read_text(encoding="utf-8"))
    assert set(drive) == {"trace", "bands"}
    assert set(drive["bands"]) == {"steady", "all"}
    assert (out / "index.html").exists()


CLOCK_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")   # also inside "T23:13:49"
WALL_CLOCK_KEYS = {"time", "start", "end"}


def wall_clock_leaks(text):
    """Clock times ('23:13') or wall-clock keys anywhere in a JSON document."""
    leaks = CLOCK_TIME.findall(text)

    def walk(node):
        if isinstance(node, dict):
            leaks.extend(k for k in node if k in WALL_CLOCK_KEYS)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads(text))
    return leaks


def test_leak_check_catches_a_leaked_start_time():
    assert wall_clock_leaks('{"id": "x", "start": "2026-09-25T23:13:49"}') == ["23:13", "start"]


def test_build_output_is_strict_json_without_wall_clock(tmp_path):
    out = tmp_path / "_site"
    bs.build(REPO / "data" / "drives.toml", REPO / "web", out)
    files = list((out / "data").glob("*.json"))
    assert len(files) == 4   # drives.json + one per published drive
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "NaN" not in text and "Infinity" not in text
        assert wall_clock_leaks(text) == [], path.name   # when the car was driven must not leak


# ---------- the output folder is deleted and rebuilt, so guard what it can point at ----------

def _site_sources(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return _manifest(tmp_path, ENTRY.format(id="a", file=BASELINE.name)), web


@pytest.mark.parametrize("target", ["web", ".", "web/nested"])
def test_build_refuses_out_dir_overlapping_sources(tmp_path, target):
    manifest, web = _site_sources(tmp_path)
    with pytest.raises(bs.BuildError, match="--out"):
        bs.build(manifest, web, tmp_path / target)
    assert (web / "index.html").exists()
    assert manifest.exists()


def test_build_refuses_to_delete_a_folder_it_did_not_build(tmp_path):
    manifest, web = _site_sources(tmp_path)
    other = tmp_path / "Documents"
    other.mkdir()
    (other / "thesis.docx").write_text("important", encoding="utf-8")
    with pytest.raises(bs.BuildError, match="not a previous build"):
        bs.build(manifest, web, other)
    assert (other / "thesis.docx").exists()


def test_build_replaces_its_own_previous_output(tmp_path):
    manifest, web = _site_sources(tmp_path)
    out = tmp_path / "_site"
    bs.build(manifest, web, out)
    (out / "stale.txt").write_text("old", encoding="utf-8")
    bs.build(manifest, web, out)
    assert not (out / "stale.txt").exists()
    assert (out / "index.html").exists()


# ---------- dates must be real calendar dates (the page formats them) ----------

@pytest.mark.parametrize("value", ['"Sept 26"', '"2026-09-26"', "2026-09-26T23:13:49"])
def test_manifest_rejects_dates_that_are_not_plain_toml_dates(tmp_path, value):
    entry = ENTRY.format(id="a", file=BASELINE.name).replace("date = 2026-09-25", f"date = {value}")
    with pytest.raises(bs.BuildError, match="date"):
        bs.load_manifest(_manifest(tmp_path, entry))
