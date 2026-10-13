"""The logger against a simulated engine ECU on a python-can virtual bus."""
import csv
import json
import threading
import time
import uuid
from argparse import Namespace

import can
import pytest

import carpi_logger as cl
import carpi_presets as cp

SUPPORTED = {int(k, 16) for k in cp.CATALOG}
# PID -> data bytes after the PID (decoded values are only sanity-checked)
VALUES = {0x0C: [0x0C, 0x80], 0x05: [130], 0x06: [0x88], 0x07: [0x84], 0x15: [0x80, 0xFF],
          0x34: [0x80, 0x00, 0x80, 0x00], 0x44: [0x80, 0x00], 0x23: [0x00, 0x39],
          0x42: [0x36, 0xB0], 0x3C: [0x1F, 0x40], 0x43: [0x00, 0x30], 0x1F: [0x01, 0x00]}
BOOST = [0x03, 0x0C, 0x40, 0x0C, 0x40, 0, 0, 0, 0]       # 98 kPa commanded and actual


class FakeEcu(threading.Thread):
    """Answers Mode 01 on 0x7E8 (multi-frame for 0x70) and sends the gateway keep-alive."""

    def __init__(self, channel):
        super().__init__(daemon=True)
        self.bus = can.Bus(interface="virtual", channel=channel)
        self.supported = set(SUPPORTED)
        self.engine_on = True
        self.stop = False
        self.pending = None

    def send(self, data):
        self.bus.send(can.Message(arbitration_id=cl.ENGINE_RX, is_extended_id=False,
                                  data=data + [0xAA] * (8 - len(data))))

    def answer(self, pid):
        if pid % 0x20 == 0:                                       # supported-PID bitmap
            bits = 0
            for p in range(pid + 1, pid + 0x21):
                more = p == pid + 0x20 and any(q > p for q in self.supported)
                if p in self.supported or more:
                    bits |= 1 << (31 - (p - pid - 1))
            return self.send([6, 0x41, pid] + list(bits.to_bytes(4, "big")))
        if pid not in self.supported:
            return
        if pid == 0x70:
            payload = [0x41, 0x70] + BOOST
            self.pending = payload[6:]
            return self.send([0x10, len(payload)] + payload[:6])
        data = VALUES.get(pid, [0x40])
        self.send([2 + len(data), 0x41, pid] + data)

    def run(self):
        next_keepalive = 0.0
        while not self.stop:
            if time.monotonic() > next_keepalive:
                self.bus.send(can.Message(arbitration_id=cl.KEEPALIVE_ID, is_extended_id=True,
                                          data=[0x20, 0x10, 0, 0, 0, 0, 0, 0]))
                next_keepalive = time.monotonic() + 0.5
            rx = self.bus.recv(0.02)
            if rx is None or not self.engine_on or rx.arbitration_id not in (cl.ENGINE_TX, cl.FUNCTIONAL_ID):
                continue
            d = list(rx.data)
            if d[0] == 0x30 and self.pending is not None:          # flow control -> rest of 0x70
                self.send([0x21] + self.pending)
                self.pending = None
            elif d[0] == 0x02 and d[1] == 0x01:
                self.answer(d[2])
        self.bus.shutdown()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "PRESET_CHECK_EVERY", 0.2)
    monkeypatch.setattr(cl, "STATUS_FILE_EVERY", 0.2)
    channel = f"carpi-{uuid.uuid4().hex}"
    args = Namespace(channel=channel, interface="virtual", logdir=str(tmp_path / "logs"),
                     tag_file=str(tmp_path / "tag.txt"),
                     preset_file=str(tmp_path / "active_preset.json"),
                     supported_file=str(tmp_path / "supported.json"),
                     status_file=str(tmp_path / "status.json"), end_after=1.0, hz=50, once=True)
    ecu = FakeEcu(channel)
    ecu.start()
    logger = threading.Thread(target=cl.run, args=(args,), daemon=True)
    yield Namespace(args=args, ecu=ecu, logger=logger, store=cp.Store(tmp_path), tmp=tmp_path)
    ecu.stop = True
    ecu.join(2)


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _status(rig):
    try:
        return json.loads((rig.tmp / "status.json").read_text())
    except (OSError, ValueError):
        return {}


def _drives(rig):
    out = []
    for path in sorted((rig.tmp / "logs").glob("*/*.csv")):
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        meta = json.loads(path.with_suffix(".json").read_text())
        out.append((path, rows, meta))
    return sorted(out, key=lambda d: "follows" in d[2])   # a rollover's second file "follows" the first


def _finish(rig):
    rig.ecu.engine_on = False                     # ignition off
    rig.logger.join(15)
    assert not rig.logger.is_alive()


def test_default_preset_drive(rig):
    rig.store.set_tag("baseline")
    rig.logger.start()
    assert _wait(lambda: _status(rig).get("rows", 0) >= 30)
    assert _status(rig)["preset"] == {"id": "default", "name": "Full (default)"}
    _finish(rig)

    [(path, rows, meta)] = _drives(rig)
    assert meta["complete"] and meta["ended_by"] == "engine_off" and meta["tag"] == "baseline"
    assert meta["preset"]["id"] == "default" and "follows" not in meta
    assert len(meta["fast_pids"]) == 12 and len(meta["slow_pids"]) == 12
    rows = rows[:-1]                     # the last row can be cut short by the ignition going off
    header = list(rows[0])
    assert header[:4] == ["time", "elapsed_s", "clock_ok", "rpm"]
    assert all(float(r["rpm"]) == 800 for r in rows)                       # 0x0C80 / 4
    assert all(float(r["boost_act_kpa"]) == 98 for r in rows)              # multi-frame worked
    slow_cols = ["coolant_c", "iat_c", "ltft_pct", "volts", "baro_kpa", "ambient_c",
                 "fuel_pct", "cat_temp_c", "o2s2_v", "lt_o2s2_trim_pct", "abs_load_pct",
                 "run_time_s"]
    assert all(sum(bool(r[c]) for c in slow_cols) == 1 for r in rows)    # one slow value per row
    assert {"0C", "70"} <= set(json.loads((rig.tmp / "supported.json").read_text())["pids"])


def test_preset_change_mid_drive_starts_new_file(rig):
    rig.logger.start()
    assert _wait(lambda: _status(rig).get("rows", 0) >= 20)
    rig.store.activate("pulls")
    assert _wait(lambda: _status(rig).get("preset", {}).get("id") == "pulls")
    assert _wait(lambda: _status(rig).get("rows", 0) >= 20)
    _finish(rig)

    (p1, rows1, meta1), (p2, rows2, meta2) = _drives(rig)
    assert meta1["ended_by"] == "preset_change" and meta1["complete"]
    assert meta2["follows"] == f"{p1.parent.name}/{p1.name}"
    assert meta2["preset"]["id"] == "pulls" and meta2["ended_by"] == "engine_off"
    pulls_cols = [c for pid in cp.BUILTIN["pulls"]["weights"] for c in cp.CATALOG[pid][3]]
    assert list(rows2[0])[3:] == pulls_cols
    assert "volts" in rows1[0] and "volts" not in rows2[0]


def test_bad_preset_file_means_default_and_no_flapping(rig):
    (rig.tmp / "active_preset.json").write_text("{ not json")
    rig.logger.start()
    assert _wait(lambda: _status(rig).get("rows", 0) >= 20)
    time.sleep(0.6)                               # several preset checks
    _finish(rig)
    [(_, _, meta)] = _drives(rig)
    assert meta["preset"]["id"] == "default" and meta["ended_by"] == "engine_off"


def test_unsupported_values_fall_back_to_default(rig):
    rig.store.save({"name": "Ghost", "weights": {"0E": 5}})
    rig.store.activate("ghost")
    rig.ecu.supported.discard(0x0E)
    rig.logger.start()
    assert _wait(lambda: _status(rig).get("rows", 0) >= 10)
    time.sleep(0.6)
    _finish(rig)
    [(_, rows, meta)] = _drives(rig)
    assert meta["preset"]["id"] == "default" and "timing_deg" not in rows[0]

