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


def test_notify_size_caps_at_one_radio_packet():
    assert cc.notify_size(23) == 20            # default ATT MTU
    assert cc.notify_size(185) == 182          # iOS-sized MTU: fits under the cap
    assert cc.notify_size(517) == cc.NOTIFY_MAX == 244   # Android's 517: capped
    assert cc.notify_size(5) == 20
    reply = b"x" * 3985 + b"\n"
    pieces = cc.chunks(reply, cc.notify_size(517))
    assert len(pieces) == 17 and b"".join(pieces) == reply and max(map(len, pieces)) == 244


# ---------- live values for the Live tab ----------

def _write_live(path, age_of_file=0.0, **values):
    path.write_text(json.dumps({"state": "logging", "drive": "2026-10-30/drive-081502.csv",
                                "updated": time.time() - age_of_file,
                                "values": {k: list(v) for k, v in values.items()}}))


def test_protocol_has_the_live_tab(tmp_path):
    assert cc.PROTOCOL >= 2                      # 2 added "live"
    assert _ask(_ctl(tmp_path), rid=1, cmd="hello")["protocol"] == cc.PROTOCOL


def test_live_filters_columns_and_ages_values(tmp_path):
    _write_live(tmp_path / "live.json", age_of_file=1.0, rpm=(812.0, 0.1), coolant_c=(46, 1.4),
                volts=(14.1, 0.6))
    r = _ask(_ctl(tmp_path), rid=1, cmd="live", cols=["rpm", "coolant_c", "oil_c"])
    live = r["live"]
    assert r["ok"] and live["state"] == "logging" and live["drive"].endswith(".csv")
    assert set(live["values"]) == {"rpm", "coolant_c"}             # oil_c isn't being read: just absent
    value, age = live["values"]["rpm"]
    assert value == 812.0 and 1.0 <= age < 1.5                      # 0.1 s old when written, file 1 s old
    assert 2.3 <= live["values"]["coolant_c"][1] < 3.0


def test_live_without_cols_returns_everything(tmp_path):
    _write_live(tmp_path / "live.json", rpm=(812.0, 0.1), coolant_c=(46, 1.4))
    assert set(_ask(_ctl(tmp_path), rid=1, cmd="live")["live"]["values"]) == {"rpm", "coolant_c"}


def test_live_stale_or_missing_file(tmp_path):
    ctl = _ctl(tmp_path)
    assert _ask(ctl, rid=1, cmd="live")["live"] == {"state": "unknown", "values": {}}
    _write_live(tmp_path / "live.json", age_of_file=5.0, rpm=(812.0, 0.1))
    assert _ask(ctl, rid=2, cmd="live")["live"] == {"state": "not_reading", "values": {}}


def test_live_after_drive_end(tmp_path):
    (tmp_path / "live.json").write_text(json.dumps({"state": "ended", "drive": None,
                                                    "updated": time.time(), "values": {}}))
    assert _ask(_ctl(tmp_path), rid=1, cmd="live")["live"] == {"state": "ended", "values": {}}


def test_live_path_defaults_next_to_status_and_can_be_set(tmp_path):
    assert _ctl(tmp_path).live_path == tmp_path / "live.json"
    other = tmp_path / "elsewhere.json"
    assert cc.Controller(cp.Store(tmp_path), None, live_path=other).live_path == other
    assert cc.Controller(cp.Store(tmp_path), None).live_path is None


def test_live_reply_is_small(tmp_path):
    cols = ["rpm", "speed_kph", "boost_act_kpa", "baro_kpa", "coolant_c", "stft_pct", "ltft_pct", "run_time_s"]
    _write_live(tmp_path / "live.json", **{c: (1234.567, 0.13) for c in cols})
    out = _ctl(tmp_path).handle_line(json.dumps({"rid": 1, "cmd": "live", "cols": cols}))
    assert len(out) < 600          # 3 notifications at most, even at a small MTU


# ---------- settings: Wi-Fi, power, clock (protocol 3) ----------

import carpi_net  # noqa: E402

PASSWORD = "hunter22"
HOME = "11111111-2222-3333-4444-555555555555"


class FakeNet:
    def __init__(self):
        self.calls = []

    def state(self):
        return {"wifi": {"state": "connected", "ssid": "HomeNet", "signal": 72, "ip": "192.168.1.40"},
                "saved": [{"uuid": HOME, "name": "HomeNet", "ssid": "HomeNet", "priority": 10, "active": True}],
                "addresses": {"wlan0": ["192.168.1.40"]}, "hostname": "carpi"}

    def scan(self, rescan=True):
        self.calls.append(("scan", rescan))
        return [{"ssid": "Cafe Guest", "signal": 40, "secured": False, "saved": False}]

    def add(self, ssid, password, connect=False):
        self.calls.append(("add", ssid, connect))       # the password is deliberately not recorded
        if ssid == "HomeNet":
            raise carpi_net.NetError('"HomeNet" is already saved. Forget it first to change its password.')
        return {"added": ssid, "connecting": connect}

    def forget(self, uuid):
        self.calls.append(("forget", uuid))

    def set_order(self, uuids):
        self.calls.append(("order", list(uuids)))

    def connect(self, uuid):
        self.calls.append(("connect", uuid))


class FakeAdmin:
    def __init__(self):
        self.ran = []

    def clock_status(self):
        return {"synced": False, "source": "none", "pi_time_ms": 1, "zone": "UTC"}

    def set_clock(self, now_ms):
        return {"synced": False, "source": "phone", "pi_time_ms": now_ms, "set": True, "reason": "set from the phone"}

    def set_zone(self, zone):
        if zone == "Mars/Olympus":
            raise ValueError("unknown time zone: Mars/Olympus")
        return {"zone": zone, "set": True, "reason": "set from the phone"}

    def power(self, action):
        if action not in ("shutdown", "reboot"):
            raise ValueError('power: "shutdown" or "reboot"')
        return lambda: self.ran.append(action)


def _settings_ctl(tmp_path):
    net, admin = FakeNet(), FakeAdmin()
    return cc.Controller(cp.Store(tmp_path), tmp_path / "status.json", net=net, admin=admin), net, admin


def test_protocol_4_and_clock_in_hello(tmp_path):
    ctl, _, _ = _settings_ctl(tmp_path)
    h = _ask(ctl, rid=1, cmd="hello")
    assert h["protocol"] == cc.PROTOCOL >= 4
    assert h["clock"] == {"synced": False, "source": "none", "zone": "UTC"}
    assert _ask(ctl, rid=2, cmd="state")["clock"]["source"] == "none"


def test_net_requests(tmp_path):
    ctl, net, _ = _settings_ctl(tmp_path)
    assert _ask(ctl, rid=1, cmd="net")["wifi"]["ssid"] == "HomeNet"
    assert _ask(ctl, rid=2, cmd="net_scan", rescan=False)["nearby"][0]["ssid"] == "Cafe Guest"
    r = _ask(ctl, rid=3, cmd="net_add", ssid="Cafe Guest", password="", connect=True)
    assert r["ok"] and r["added"] == "Cafe Guest" and r["connecting"] and r["saved"]   # state comes back too
    assert _ask(ctl, rid=4, cmd="net_priority", order=[HOME])["ok"]
    assert _ask(ctl, rid=5, cmd="net_connect", uuid=HOME)["wifi"]["ssid"] == "HomeNet"
    assert _ask(ctl, rid=6, cmd="net_forget", uuid=HOME)["ok"]
    assert net.calls == [("scan", False), ("add", "Cafe Guest", True), ("order", [HOME]),
                         ("connect", HOME), ("forget", HOME)]


def test_net_add_reply_never_contains_the_password(tmp_path):
    ctl, _, _ = _settings_ctl(tmp_path)
    out = ctl.handle_line(json.dumps({"rid": 1, "cmd": "net_add", "ssid": "Cafe WPA", "password": PASSWORD}))
    assert PASSWORD.encode() not in out and json.loads(out)["ok"]


def test_net_errors_are_replies(tmp_path):
    ctl, _, _ = _settings_ctl(tmp_path)
    r = _ask(ctl, rid=1, cmd="net_add", ssid="HomeNet", password=PASSWORD)
    assert not r["ok"] and "already saved" in r["error"] and PASSWORD not in json.dumps(r)
    assert not _ask(ctl, rid=2, cmd="net_priority", order="not a list")["ok"]
    assert not _ask(ctl, rid=3, cmd="net_add", ssid=5, password=None)["ok"]


def test_power_runs_only_after_the_reply(tmp_path):
    ctl, _, admin = _settings_ctl(tmp_path)
    r = _ask(ctl, rid=1, cmd="power", action="shutdown")
    assert r == {"ok": True, "power": "shutdown", "in_s": 2, "rid": 1}
    assert admin.ran == []                       # nothing happens while the reply is being sent
    after = ctl.take_after()
    assert ctl.take_after() is None              # handed out once
    after()
    assert admin.ran == ["shutdown"]
    assert not _ask(ctl, rid=2, cmd="power", action="format")["ok"] and ctl.take_after() is None


def test_clock_request(tmp_path):
    ctl, _, _ = _settings_ctl(tmp_path)
    r = _ask(ctl, rid=1, cmd="clock", now_ms=1_793_000_600_000)
    assert r["clock"]["set"] and r["clock"]["source"] == "phone"


def test_timezone_request(tmp_path):
    ctl, _, _ = _settings_ctl(tmp_path)
    r = _ask(ctl, rid=1, cmd="timezone", zone="America/New_York")
    assert r["ok"] and r["timezone"] == {"zone": "America/New_York", "set": True, "reason": "set from the phone"}
    bad = _ask(ctl, rid=2, cmd="timezone", zone="Mars/Olympus")
    assert not bad["ok"] and "unknown time zone" in bad["error"]


def test_settings_unavailable_without_backends(tmp_path):
    ctl = _ctl(tmp_path)
    for cmd in ("net", "net_scan", "power", "clock", "timezone"):
        r = _ask(ctl, rid=1, cmd=cmd, action="reboot", now_ms=1, zone="UTC")
        assert not r["ok"] and "not available" in r["error"]
    assert "clock" not in _ask(ctl, rid=2, cmd="hello")


def test_settings_replies_stay_small(tmp_path):
    ctl, _, _ = _settings_ctl(tmp_path)
    for msg in ({"cmd": "net"}, {"cmd": "net_scan"}, {"cmd": "hello"}):
        assert len(ctl.handle_line(json.dumps(dict(msg, rid=1)))) < 4096


# ---- uploads (protocol 5; carpi_sync.Uploads on temp files) ----

import carpi_sync  # noqa: E402


def _upload_ctl(tmp_path, set_up=True):
    if set_up:
        (tmp_path / "logs" / ".git").mkdir(parents=True)
    uploads = carpi_sync.Uploads(settings=tmp_path / "upload.json", logs=tmp_path / "logs",
                                 sync_file=tmp_path / "sync.json", trigger=tmp_path / "sync-now",
                                 up=lambda: 500.0)
    return cc.Controller(cp.Store(tmp_path), tmp_path / "status.json", uploads=uploads)


def test_protocol_5_and_sync_everywhere(tmp_path):
    ctl = _upload_ctl(tmp_path)
    h = _ask(ctl, rid=1, cmd="hello")
    assert cc.PROTOCOL == 5 and h["protocol"] == 5
    assert h["sync"]["set_up"] and h["sync"]["auto"] and h["sync"]["publish"]
    assert "sync" in _ask(ctl, rid=2, cmd="status") and "sync" in _ask(ctl, rid=3, cmd="state")


def test_upload_settings(tmp_path):
    ctl = _upload_ctl(tmp_path)
    r = _ask(ctl, rid=1, cmd="upload", publish=False)
    assert r["ok"] and r["sync"]["publish"] is False and r["sync"]["auto"] is True
    r = _ask(ctl, rid=2, cmd="upload", auto=False, publish=True)
    assert r["sync"]["auto"] is False and r["sync"]["publish"] is True and r["sync"]["next_try_s"] is None
    assert json.loads((tmp_path / "upload.json").read_text()) == {"auto": False, "publish": True}
    for bad in ({}, {"auto": "off"}, {"publish": 0}):
        r = _ask(ctl, rid=3, cmd="upload", **bad)
        assert not r["ok"] and "true or false" in r["error"]
    assert json.loads((tmp_path / "upload.json").read_text()) == {"auto": False, "publish": True}


def test_upload_now(tmp_path):
    r = _ask(_upload_ctl(tmp_path, set_up=False), rid=1, cmd="upload_now")
    assert not r["ok"] and "set up" in r["error"] and not (tmp_path / "sync-now").exists()
    r = _ask(_upload_ctl(tmp_path), rid=2, cmd="upload_now")
    assert r["ok"] and r["started"] and r["sync"]["state"] == "uploading"
    assert (tmp_path / "sync-now").exists()


def test_uploads_unavailable_without_the_backend(tmp_path):
    ctl = _ctl(tmp_path)
    for cmd in ("upload", "upload_now"):
        r = _ask(ctl, rid=1, cmd=cmd, auto=True)
        assert not r["ok"] and "not available" in r["error"]
    assert "sync" not in _ask(ctl, rid=2, cmd="hello")


def test_upload_replies_stay_small(tmp_path):
    ctl = _upload_ctl(tmp_path)
    (tmp_path / "sync.json").write_text(json.dumps({"state": "failed", "error": "x" * 200, "waiting": 12,
                                                     "attempt_up": 400.0, "success_up": 100.0}))
    for msg in ({"cmd": "hello"}, {"cmd": "status"}, {"cmd": "upload", "auto": True}):
        assert len(ctl.handle_line(json.dumps(dict(msg, rid=1)))) < 4096
