"""carpi_drives.py: the drive list, saved drives and the drive being recorded, for the phone
page's Analysis tab. Standard library only."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

import carpi_drives as cd

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "drive-2026-09-25_203526.csv"
HEADER = "time,elapsed_s,rpm,speed_kph,coolant_c,lambda_cmd,stft_pct,ltft_pct,load_pct\n"


def row(i, rpm=800):
    return f"2026-11-03T07:00:00,{i * 0.13:.3f},{rpm},0,90,1.0,1,7,20\n"


def save(logs, drive_id, rows=20, meta=None):
    path = logs / f"{drive_id}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "".join(row(i) for i in range(rows)))
    if meta is not None:
        (logs / f"{drive_id}.json").write_text(json.dumps(meta))
    return path


def test_drive_path_reads_drive_files_inside_logs_only(tmp_path):
    logs = tmp_path / "logs"
    save(logs, "2026-10-06/drive-171502")
    save(logs, "drive-2026-09-25_203526")
    (tmp_path / "secret.csv").write_text("x")
    assert cd.drive_path(logs, "2026-10-06/drive-171502").name == "drive-171502.csv"
    assert cd.drive_path(logs, "drive-2026-09-25_203526").is_file()
    for bad in ("../secret", "2026-10-06/../../secret", "/etc/passwd", "2026-10-06/drive-171502.csv",
                "2026-10-06/drive-999999", "", None, 7):
        with pytest.raises(ValueError, match="no such drive"):
            cd.drive_path(logs, bad)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need extra rights on Windows")
def test_a_symlink_out_of_logs_is_refused(tmp_path):
    logs = tmp_path / "logs"
    (logs / "2026-10-06").mkdir(parents=True)
    (tmp_path / "x.csv").write_text("x")
    (logs / "2026-10-06" / "drive-171502.csv").symlink_to(tmp_path / "x.csv")
    with pytest.raises(ValueError):
        cd.drive_path(logs, "2026-10-06/drive-171502")


def test_drive_names_match_the_upload_safety_check():
    sys.path.insert(0, str(ROOT / "deploy"))
    import check_drives
    for name in ("2026-10-06/drive-171502", "2026-10-06/drive-171502-2", "drive-2026-09-25_203526",
                 "2026-10-06/drive-17150", "drive-2026-09-25_203526-2", "2026-10-06/x"):
        ours = cd.drive_key(name) is not None
        theirs = bool(check_drives.DRIVE.fullmatch(f"{name}.csv") or check_drives.LEGACY.fullmatch(f"{name}.csv"))
        assert ours == theirs, name


def test_the_list_is_newest_first_15_at_a_time(tmp_path):
    logs = tmp_path / "logs"
    ids = [f"2026-10-{d:02d}/drive-0{h}0000" for d in range(1, 11) for h in (7, 8)]     # 20 drives
    for drive_id in ids + ["drive-2026-09-25_203526"]:
        save(logs, drive_id, rows=3)
    newest = sorted(ids, reverse=True)
    first = cd.list_drives(logs)
    assert [d["id"] for d in first["drives"]] == newest[:15] and first["more"]
    rest = cd.list_drives(logs, before=first["drives"][-1]["id"])
    assert [d["id"] for d in rest["drives"]] == newest[15:] + ["drive-2026-09-25_203526"]
    assert not rest["more"]
    with pytest.raises(ValueError, match="before"):
        cd.list_drives(logs, before="../x")


def test_list_entries_come_from_each_drives_json(tmp_path):
    logs = tmp_path / "logs"
    save(logs, "2026-10-06/drive-171502", meta={
        "tag": "post-pcv", "rows": 20, "duration_s": 2.5, "preset": {"id": "default", "name": "Full (default)"},
        "complete": False, "publish": False})
    save(logs, "2026-10-06/drive-181502")                                   # no .json yet
    r = cd.list_drives(logs, current="2026-10-06/drive-181502")
    a, b = r["drives"]
    assert r["current"] == "2026-10-06/drive-181502"
    assert b == {"id": "2026-10-06/drive-171502", "day": "2026-10-06", "time": "17:15", "duration_s": 2.5,
                 "rows": 20, "tag": "post-pcv", "preset": "Full (default)", "complete": False, "private": True}
    assert a["tag"] is None and a["complete"] and not a["private"] and a["time"] == "18:15"


def logging_now(status, drive_id):
    status.write_text(json.dumps({"state": "logging", "drive": f"{drive_id}.csv", "updated": time.time()}))


@pytest.fixture
def pi(tmp_path):
    logs, status = tmp_path / "logs", tmp_path / "status.json"
    logs.mkdir()
    return cd.Drives(logs=logs, status_file=status), logs, status


def test_no_drive_yet(pi):
    drives, _, _ = pi
    drives.step()
    assert drives.analysis("live") == {"id": None, "live": True, "recording": False, "state": "none"}
    assert drives.analysis_map("live", "rpm", "load_pct", "trim_total", [], "mean") == {"id": None, "state": "none"}


def test_the_drive_being_recorded_is_followed(pi):
    drives, logs, status = pi
    path = save(logs, "2026-11-03/drive-071502", rows=10)
    logging_now(status, "2026-11-03/drive-071502")
    drives.step()
    a = drives.analysis("live")
    assert (a["id"], a["live"], a["recording"], a["state"], a["rows"]) == ("2026-11-03/drive-071502", True, True, "ready", 10)
    assert a["updated_s"] >= 0 and "rpm" in a["columns"] and a["summary"]["trim_bands"]
    with path.open("a") as f:
        f.write(row(10) + row(11) + "2026-11-03T07:00:00,1.5")          # two rows and half a line
    drives.step()
    assert drives.analysis("live")["rows"] == 12
    assert drives.drives()["current"] == "2026-11-03/drive-071502"


def test_a_new_file_starts_fresh_and_a_stopped_logger_ends_the_drive(pi):
    drives, logs, status = pi
    save(logs, "2026-11-03/drive-071502", rows=10)
    logging_now(status, "2026-11-03/drive-071502")
    drives.step()
    save(logs, "2026-11-03/drive-072000", rows=4)
    logging_now(status, "2026-11-03/drive-072000")
    drives.step()
    a = drives.analysis("live")
    assert a["id"] == "2026-11-03/drive-072000" and a["rows"] == 4
    status.write_text(json.dumps({"state": "ended", "updated": time.time()}))
    drives.step()
    a = drives.analysis("live")
    assert a["id"] == "2026-11-03/drive-072000" and not a["recording"] and "updated_s" not in a
    assert drives.drives()["current"] is None


def test_a_saved_drive_loads_in_the_background(pi):
    drives, logs, _ = pi
    shutil.copy(SAMPLE, logs / SAMPLE.name)
    drive_id = SAMPLE.stem
    assert drives.analysis(drive_id) == {"id": drive_id, "live": False, "recording": False, "state": "loading"}
    drives.step()
    a = drives.analysis(drive_id)
    assert a["state"] == "ready" and not a["live"] and a["rows"] == 9944 and a["events_total"] >= len(a["events"])
    with pytest.raises(ValueError, match="no such drive"):
        drives.analysis("2026-01-01/drive-000000")


def test_maps_build_in_the_background(pi):
    drives, logs, status = pi
    save(logs, "2026-11-03/drive-071502", rows=30)
    logging_now(status, "2026-11-03/drive-071502")
    drives.step()
    args = ("live", "rpm", "load_pct", "trim_total", ["running", "warm", "closed_loop"], "mean")
    assert drives.analysis_map(*args) == {"id": "2026-11-03/drive-071502", "state": "loading"}
    drives.step()
    m = drives.analysis_map(*args)
    assert m["state"] == "ready" and m["cells"] and m["x"]["col"] == "rpm" and m["id"] == "2026-11-03/drive-071502"
    drives.analysis_map("live", "rpm", "load_pct", "oil_c", [], "mean")
    drives.step()
    assert drives.analysis_map("live", "rpm", "load_pct", "oil_c", [], "mean") == {
        "id": "2026-11-03/drive-071502", "state": "error", "error": "oil_c isn't in this drive"}
    for bad, msg in (((7, "load_pct", "x", [], "mean"), "x: a column"),
                     (("rpm", "load_pct", "x", ["fast"], "mean"), "filters"),
                     (("rpm", "load_pct", "x", "warm", "mean"), "filters"),
                     (("rpm", "load_pct", "x", [], "median"), "stat")):
        with pytest.raises(ValueError, match=msg):
            drives.analysis_map("live", *bad)


def test_the_worker_thread_runs_and_stops(pi):
    drives, logs, status = pi
    save(logs, "2026-11-03/drive-071502", rows=10)
    logging_now(status, "2026-11-03/drive-071502")
    drives.start()
    try:
        deadline = time.time() + 5
        while drives.analysis("live")["state"] != "ready" and time.time() < deadline:
            time.sleep(0.05)
        assert drives.analysis("live")["rows"] == 10
    finally:
        drives.stop()


def test_replies_stay_under_4_kb(pi, monkeypatch):
    import carpi_control as cc
    import carpi_presets as cp
    import carpi_stats
    drives, logs, _ = pi
    shutil.copy(SAMPLE, logs / SAMPLE.name)
    for i in range(30):
        save(logs, f"2026-10-{i % 28 + 1:02d}/drive-0{i % 9}1500", rows=3, meta={
            "tag": "a-long-tag-for-size", "rows": 123456, "duration_s": 12345.6,
            "preset": {"name": "Pulls (boost & timing)"}})
    longest = {"kind": "pull", "level": "info", "t0": 12345.6, "t1": 12399.9, "ongoing": False,
               "text": "6,500→6,800 rpm, peak 24.9 psi, min timing -12.5°, min λ 0.750 (shifts excluded)"}
    monkeypatch.setattr(carpi_stats.Drive, "events", lambda self: [dict(longest)] * 30)
    ctl = cc.Controller(cp.Store(logs.parent), logs.parent / "status.json", stats=drives)
    asks = [{"cmd": "drives"}, {"cmd": "analysis", "drive": SAMPLE.stem},
            {"cmd": "analysis_map", "drive": SAMPLE.stem, "x": "rpm", "y": "load_pct", "z": "lambda_err",
             "filters": ["running", "no_shift"], "stat": "mean"}]
    for msg in asks:                                                     # ask, let the worker run, ask again
        ctl.handle_line(json.dumps(dict(msg, rid=1)))
    drives.step()
    drives.step()
    for msg in asks:
        out = ctl.handle_line(json.dumps(dict(msg, rid=2)))
        assert json.loads(out)["ok"] and len(out) < 4096, (msg["cmd"], len(out))
