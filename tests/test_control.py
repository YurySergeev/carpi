import json
import time

import carpi_control as cc
import carpi_presets as cp


def _ctl(tmp_path):
    return cc.Controller(cp.Store(tmp_path), tmp_path / "status.json")


def _ask(ctl, **msg):
    out = ctl.handle_line(json.dumps(msg))
    assert out.endswith(b"\n") and out.count(b"\n") == 1
    return json.loads(out)


def test_line_buffer_reassembles_any_chunking():
    data = b'{"rid":1,"cmd":"hello"}\n\n{"rid":2,"cmd":"status"}\n'
    for size in (1, 3, 20, 180, 1000):
        buf = cc.LineBuffer()
        lines = [line for chunk in cc.chunks(data, size) for line in buf.feed(chunk)]
        assert [json.loads(x)["rid"] for x in lines] == [1, 2]


def test_line_buffer_overflow_resets():
    buf = cc.LineBuffer(limit=10)
    assert buf.feed(b"x" * 11) == [""]
    assert buf.feed(b'{"a":1}\n') == ['{"a":1}']


def test_hello_has_catalog_and_state(tmp_path):
    r = _ask(_ctl(tmp_path), rid=1, cmd="hello")
    assert r["ok"] and r["rid"] == 1 and r["protocol"] == cc.PROTOCOL
    assert [c["id"] for c in r["catalog"]] == list(cp.CATALOG)
    assert r["active"] == "default" and r["tag"] == "" and r["supported"] is None
    assert r["status"] == {"state": "unknown"}
    assert len(json.dumps(r)) < cc.MAX_LINE * 2


def test_save_activate_delete_tag(tmp_path):
    ctl = _ctl(tmp_path)
    preset = {"name": "Trims", "weights": {"06": 10, "07": 10, "0C": 10}}
    r = _ask(ctl, rid=2, cmd="save", preset=preset, activate=True)
    assert r["ok"] and r["saved"] == "trims" and r["active"] == "trims"
    assert cp.read_active(tmp_path / "active_preset.json")["id"] == "trims"

    r = _ask(ctl, rid=3, cmd="delete", id="trims")
    assert not r["ok"] and "active" in r["error"] and r["rid"] == 3

    assert _ask(ctl, rid=4, cmd="activate", id="pulls")["active"] == "pulls"
    assert _ask(ctl, rid=5, cmd="delete", id="trims")["ok"]
    assert _ask(ctl, rid=6, cmd="tag", tag="post-pcv")["tag"] == "post-pcv"


def test_errors_are_replies_not_exceptions(tmp_path):
    ctl = _ctl(tmp_path)
    assert json.loads(ctl.handle_line("not json")) == {"ok": False, "error": "bad JSON", "rid": None}
    assert not _ask(ctl, rid=1, cmd="explode")["ok"]
    assert not _ask(ctl, rid=2, cmd="save", preset={"name": "x", "weights": {"0C": 99}})["ok"]
    assert not _ask(ctl, rid=3, cmd="activate", id="nope")["ok"]
    assert not json.loads(ctl.handle_line("[1, 2]"))["ok"]


def test_status_fresh_and_stale(tmp_path):
    ctl = _ctl(tmp_path)
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"state": "logging", "rows": 10, "updated": time.time()}))
    assert _ask(ctl, rid=1, cmd="status")["status"] == {"state": "logging", "rows": 10}
    path.write_text(json.dumps({"state": "logging", "updated": time.time() - 60}))
    assert _ask(ctl, rid=2, cmd="status")["status"] == {"state": "offline"}


def test_status_carries_system_and_obd(tmp_path):
    snap = {"system": {"cpu_temp_c": 48.2, "cpu_pct": 12.0}, "obd": {"verdict": "traffic", "rx_per_s": 2.0}}
    ctl = cc.Controller(cp.Store(tmp_path), tmp_path / "status.json", system=lambda: snap)
    r = _ask(ctl, rid=1, cmd="status")
    assert r["system"]["cpu_temp_c"] == 48.2 and r["obd"]["verdict"] == "traffic"
    assert r["status"] == {"state": "unknown"}
    h = _ask(ctl, rid=2, cmd="hello")
    assert h["obd"]["verdict"] == "traffic" and h["presets"]


def test_broken_sampler_still_replies(tmp_path):
    def boom():
        raise RuntimeError("no /proc")
    ctl = cc.Controller(cp.Store(tmp_path), tmp_path / "status.json", system=boom)
    r = _ask(ctl, rid=1, cmd="status")
    assert r["ok"] and "no /proc" in r["system"]["error"]


def test_status_reply_with_real_sampler_is_small(tmp_path):
    import carpi_system
    s = carpi_system.Sampler(root=tmp_path, disk=tmp_path, run=lambda cmd: None)
    ctl = cc.Controller(cp.Store(tmp_path), tmp_path / "status.json", system=s.snapshot)
    out = ctl.handle_line(json.dumps({"rid": 1, "cmd": "status"}))
    assert len(out) < 1500          # a handful of notifications at a typical phone MTU
