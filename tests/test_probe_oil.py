"""Engine.read_did (UDS 0x22) and probe_oil.py against a simulated engine ECU."""
import threading
import time
import uuid

import can
import pytest

import carpi_logger as cl
import probe_oil


class FakeUdsEcu(threading.Thread):
    """Answers 22 20 2F the way `reply` says, plus Mode 01 PIDs 00 and 05. Records every request."""

    def __init__(self, channel, reply):
        super().__init__(daemon=True)
        self.bus = can.Bus(interface="virtual", channel=channel)
        self.reply = reply          # "ok" | "nrc" | "pending" | "long" | "silent"
        self.seen = []
        self.pending = None
        self.stop = False

    def send(self, data):
        self.bus.send(can.Message(arbitration_id=cl.ENGINE_RX, is_extended_id=False,
                                  data=data + [0xAA] * (8 - len(data))))

    def run(self):
        while not self.stop:
            rx = self.bus.recv(0.02)
            if rx is None or rx.arbitration_id not in (cl.ENGINE_TX, cl.FUNCTIONAL_ID):
                continue
            d = list(rx.data)
            if d[0] == 0x30:                                   # flow control
                if self.pending:
                    self.send([0x21] + self.pending)
                    self.pending = None
                continue
            self.seen.append(d[:d[0] + 1])
            if d[:3] == [0x02, 0x01, 0x00]:
                self.send([6, 0x41, 0x00, 0x08, 0, 0, 0])      # supports PID 05 only
            elif d[:3] == [0x02, 0x01, 0x05]:
                self.send([3, 0x41, 0x05, 130])                # coolant 90 C
            elif d[:4] == [0x03, 0x22, 0x20, 0x2F]:
                if self.reply == "ok":
                    self.send([4, 0x62, 0x20, 0x2F, 0x7A])
                elif self.reply == "nrc":
                    self.send([3, 0x7F, 0x22, 0x31])            # requestOutOfRange
                elif self.reply == "pending":
                    self.send([3, 0x7F, 0x22, 0x78])            # response pending...
                    time.sleep(0.3)
                    self.send([4, 0x62, 0x20, 0x2F, 0x7B])      # ...then the answer
                elif self.reply == "long":                     # 10 data bytes: multi-frame
                    payload = [0x62, 0x20, 0x2F] + list(range(1, 11))
                    self.pending = payload[6:]
                    self.send([0x10, len(payload)] + payload[:6])
        self.bus.shutdown()


@pytest.fixture
def make(request):
    rigs = []

    def _make(reply):
        channel = f"uds-{uuid.uuid4().hex}"
        ecu = FakeUdsEcu(channel, reply)
        ecu.start()
        bus = can.Bus(interface="virtual", channel=channel)
        rigs.append((ecu, bus))
        return ecu, cl.Engine(bus)

    yield _make
    for ecu, bus in rigs:
        ecu.stop = True
        ecu.join(2)
        bus.shutdown()


def test_read_did_single_frame(make):
    ecu, engine = make("ok")
    assert engine.read_did(0x202F) == bytes([0x7A])
    assert ecu.seen == [[0x03, 0x22, 0x20, 0x2F]]          # exactly one request, nothing else


def test_read_did_negative_response(make):
    _, engine = make("nrc")
    with pytest.raises(cl.NegativeResponse) as e:
        engine.read_did(0x202F)
    assert e.value.code == 0x31 and "isn't supported" in str(e.value)


def test_read_did_waits_through_response_pending(make):
    _, engine = make("pending")
    assert engine.read_did(0x202F) == bytes([0x7B])


def test_read_did_multi_frame(make):
    _, engine = make("long")
    assert engine.read_did(0x202F) == bytes(range(1, 11))


def test_read_did_no_reply_is_none(make):
    _, engine = make("silent")
    assert engine.read_did(0x202F) is None


def test_mode01_requests_still_work(make):
    _, engine = make("ok")
    assert engine.request(0x05) == bytes([130])


def test_probe_refuses_identifiers_not_on_the_list(make):
    ecu, engine = make("ok")
    with pytest.raises(ValueError, match="allow-list"):
        probe_oil.read_oil(engine, 0x1234)
    time.sleep(0.1)
    assert ecu.seen == []                                    # refused before sending anything


def test_probe_reading(make):
    ecu, engine = make("ok")
    r = probe_oil.reading(engine, t0=time.monotonic() - 12.3)
    assert r["raw"] == "7a" and r["coolant_c"] == 90 and 12 <= r["elapsed_s"] < 13
    assert r["candidates"]["A - 40"] == 82                  # 0x7A = 122
    assert ecu.seen == [[0x03, 0x22, 0x20, 0x2F], [0x02, 0x01, 0x05]]


def test_probe_reports_a_refusal_in_words(make):
    _, engine = make("nrc")
    r = probe_oil.reading(engine, t0=time.monotonic())
    assert r["raw"] is None and r["error"].startswith("refused (0x31")


def test_probe_checks_the_logger_is_stopped():
    class Done:
        def __init__(self, out):
            self.stdout = out
    assert probe_oil.logger_running(run=lambda *a, **k: Done("active\n"))
    assert not probe_oil.logger_running(run=lambda *a, **k: Done("inactive\n"))

    def missing(*a, **k):
        raise FileNotFoundError("no systemctl")
    assert not probe_oil.logger_running(run=missing)        # e.g. testing on a PC
