#!/usr/bin/env python3
"""
carpi_logger.py - CarPi drive logger for the Audi A3 8V (EA888 Gen3).

Runs forever as a systemd service and writes one CSV per drive.

  WAIT    Listen only. Never transmits while the car's CAN bus is asleep, so the
          Pi can't keep the car awake and drain the battery.
  DETECT  Bus traffic seen (e.g. the gateway keep-alive 0x17F00010) -> ask the
          engine ECU if it's on. Limited number of tries per wake-up.
  LOG     Engine answered -> read its supported PIDs, open a new CSV, poll the
          fast PIDs every loop and one slow PID per loop (round-robin).
  END     Engine silent for --end-after seconds (ignition off) -> close the file.

Output (default ~/carpi/logs/), one folder per day, e.g. logs/2026-09-26/:
  drive-HHMMSS.csv    one row per poll loop; slow columns are blank on rows where
                      they weren't sampled (pandas: df.ffill())
  drive-HHMMSS.json   metadata: tag, supported PIDs, rows, duration. Refreshed every 30 s;
                      "complete": false means power was cut before the drive ended cleanly.
  The day and time come from the Pi's clock, which has no RTC: until NTP syncs they
  can be off (see clock_ok).
  clock_ok column     1 once the Pi's clock has synced from the internet. Rows with 0 may have
                      wrong wall-clock times; elapsed_s is always right.
Tag drives for before/after comparisons by putting one word in ~/carpi/tag.txt
(e.g. "pre-pcv"). The tag is copied into each new drive's metadata.

Read-only: standard OBD-II Mode 01 requests only, same as any scan tool.
"""
import argparse
import csv
import json
import os
import signal
import time
from datetime import datetime
from pathlib import Path

import can

FUNCTIONAL_ID = 0x7DF   # "any OBD ECU"
ENGINE_TX = 0x7E0       # physical request ID for the engine ECU (also where flow control goes)
ENGINE_RX = 0x7E8       # engine ECU replies
KEEPALIVE_ID = 0x17F00010

REPLY_TIMEOUT = 0.10    # engine normally answers in 10-30 ms
DETECT_EVERY = 3.0      # seconds between "are you on?" checks while the bus is awake
DETECT_TRIES = 40       # per wake-up (~2 min), then stay silent until the bus sleeps and wakes
QUIET_RESET = 10.0      # this many seconds of bus silence = car asleep -> re-arm detection
FSYNC_EVERY = 5.0
META_EVERY = 30.0      # refresh the .json so a power cut loses at most this much metadata
CLOCK_SYNCED = Path("/run/systemd/timesync/synchronized")  # exists once NTP has set the clock
STATUS_EVERY = 15.0


def u16(d, i):
    return d[i] * 256 + d[i + 1]


# pid: [(column, decoder(data_after_pid) -> value or None), ...]
PIDS = {
    0x04: [("load_pct", lambda d: d[0] * 100 / 255)],
    0x05: [("coolant_c", lambda d: d[0] - 40)],
    0x06: [("stft_pct", lambda d: (d[0] - 128) * 100 / 128)],
    0x07: [("ltft_pct", lambda d: (d[0] - 128) * 100 / 128)],
    0x0B: [("map_kpa", lambda d: d[0])],
    0x0C: [("rpm", lambda d: u16(d, 0) / 4)],
    0x0D: [("speed_kph", lambda d: d[0])],
    0x0E: [("timing_deg", lambda d: d[0] / 2 - 64)],
    0x0F: [("iat_c", lambda d: d[0] - 40)],
    0x11: [("throttle_pct", lambda d: d[0] * 100 / 255)],
    0x15: [("o2s2_v", lambda d: d[0] / 200),
           ("o2s2_trim_pct", lambda d: None if d[1] == 0xFF else (d[1] - 128) * 100 / 128)],
    0x1F: [("run_time_s", lambda d: u16(d, 0))],
    0x23: [("rail_kpa", lambda d: u16(d, 0) * 10)],
    0x2F: [("fuel_pct", lambda d: d[0] * 100 / 255)],
    0x33: [("baro_kpa", lambda d: d[0])],
    0x34: [("lambda", lambda d: u16(d, 0) * 2 / 65536),
           ("o2s1_ma", lambda d: u16(d, 2) / 256 - 128)],
    0x3C: [("cat_temp_c", lambda d: u16(d, 0) / 10 - 40)],
    0x42: [("volts", lambda d: u16(d, 0) / 1000)],
    0x43: [("abs_load_pct", lambda d: u16(d, 0) * 100 / 255)],
    0x44: [("lambda_cmd", lambda d: u16(d, 0) * 2 / 65536)],
    0x46: [("ambient_c", lambda d: d[0] - 40)],
    0x49: [("pedal_pct", lambda d: d[0] * 100 / 255)],
    0x56: [("lt_o2s2_trim_pct", lambda d: (d[0] - 128) * 100 / 128)],
    # Boost pressure control: multi-frame reply. Absolute kPa; boost psi = (kPa - baro) / 6.895
    0x70: [("boost_cmd_kpa", lambda d: u16(d, 1) / 32 if d[0] & 0x01 else None),
           ("boost_act_kpa", lambda d: u16(d, 3) / 32 if d[0] & 0x02 else None)],
}
FAST = [0x0C, 0x0B, 0x70, 0x04, 0x0E, 0x06, 0x44, 0x34, 0x23, 0x49, 0x11, 0x0D]
SLOW = [0x05, 0x0F, 0x07, 0x42, 0x33, 0x46, 0x2F, 0x3C, 0x15, 0x56, 0x43, 0x1F]


def log(msg):
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


class Engine:
    """Talks to the engine ECU over SocketCAN, with minimal ISO-TP for multi-frame replies."""

    def __init__(self, bus):
        self.bus = bus
        self.req_id = ENGINE_TX
        self.last_activity = 0.0

    def recv(self, timeout):
        rx = self.bus.recv(timeout=timeout)
        if rx is not None:
            self.last_activity = time.monotonic()
        return rx

    def drain(self):
        while self.recv(0) is not None:
            pass

    def _send(self, arb_id, data):
        try:
            self.bus.send(can.Message(arbitration_id=arb_id, is_extended_id=False,
                                      data=data + [0x55] * (8 - len(data))), timeout=0.05)
            return True
        except can.CanError:
            return False

    def request(self, pid, service=0x01):
        """One Mode 01 request. Returns the data bytes after the PID, or None."""
        self.drain()
        if not self._send(self.req_id, [0x02, service, pid]):
            return None
        deadline = time.monotonic() + REPLY_TIMEOUT
        buf, expected, seq = None, 0, 1
        while (remaining := deadline - time.monotonic()) > 0:
            rx = self.recv(remaining)
            if rx is None:
                break
            if rx.arbitration_id != ENGINE_RX or rx.is_extended_id or not rx.data:
                continue
            d = bytes(rx.data)
            kind = d[0] >> 4
            if kind == 0:                                    # single frame
                return self._payload(d[1:1 + (d[0] & 0x0F)], service, pid)
            if kind == 1:                                    # first frame -> send flow control
                expected = ((d[0] & 0x0F) << 8) | d[1]
                buf, seq = bytearray(d[2:8]), 1
                self._send(ENGINE_TX, [0x30, 0x00, 0x05])    # block size 0, 5 ms between frames
                deadline = time.monotonic() + REPLY_TIMEOUT
            elif kind == 2 and buf is not None:              # consecutive frame
                if (d[0] & 0x0F) != (seq & 0x0F):
                    return None
                seq += 1
                buf += d[1:8]
                if len(buf) >= expected:
                    return self._payload(bytes(buf[:expected]), service, pid)
                deadline = time.monotonic() + REPLY_TIMEOUT
        return None

    @staticmethod
    def _payload(p, service, pid):
        if len(p) >= 2 and p[0] == service + 0x40 and p[1] == pid:
            return p[2:]
        return None

    def detect(self):
        """Is the engine ECU answering? Tries physical addressing first, then functional."""
        for req_id in (ENGINE_TX, FUNCTIONAL_ID):
            self.req_id = req_id
            data = self.request(0x00)
            if data is not None and len(data) >= 4:
                return True
        self.req_id = ENGINE_TX
        return False

    def supported(self):
        pids, base = set(), 0x00
        while base <= 0xC0:
            data = self.request(base)
            if data is None or len(data) < 4:
                break
            bits = int.from_bytes(data[:4], "big")
            pids.update(base + i + 1 for i in range(32) if bits & (1 << (31 - i)))
            if (base + 0x20) not in pids:
                break
            base += 0x20
        return pids


def read_tag(path):
    try:
        return path.read_text().strip().split()[0]
    except (OSError, IndexError):
        return ""


def write_meta(path, meta):
    """Write-then-rename so a power cut never leaves a half-written .json."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    os.replace(tmp, path)


def new_drive_stem(daydir, started):
    """drive-HHMMSS, or drive-HHMMSS-2, -3... if that name is taken. Without an RTC the clock
    can repeat after a power-cut reboot, and a repeated name must never overwrite a drive."""
    base = f"drive-{started:%H%M%S}"
    stem, n = daydir / base, 1
    while stem.with_suffix(".csv").exists() or stem.with_suffix(".json").exists():
        n += 1
        stem = daydir / f"{base}-{n}"
    return stem


def log_drive(engine, args):
    started = datetime.now()
    supported = engine.supported()
    fast = [p for p in FAST if p in supported]
    slow = [p for p in SLOW if p in supported]
    columns = ["time", "elapsed_s", "clock_ok"] + [c for p in fast + slow for c, _ in PIDS[p]]

    daydir = Path(args.logdir).expanduser() / f"{started:%Y-%m-%d}"
    daydir.mkdir(parents=True, exist_ok=True)
    stem = new_drive_stem(daydir, started)
    csv_path, meta_path = stem.with_suffix(".csv"), stem.with_suffix(".json")
    meta = {
        "start": started.isoformat(timespec="seconds"),
        "tag": read_tag(Path(args.tag_file).expanduser()),
        "request_id": f"0x{engine.req_id:X}",
        "supported_pids": sorted(f"{p:02X}" for p in supported),
        "fast_pids": [f"{p:02X}" for p in fast],
        "slow_pids": [f"{p:02X}" for p in slow],
    }
    write_meta(meta_path, meta)
    log(f"drive started -> {daydir.name}/{csv_path.name} (tag '{meta['tag']}', "
        f"{len(fast)} fast + {len(slow)} slow PIDs via 0x{engine.req_id:X})")

    rows, i = 0, 0
    t0 = time.monotonic()
    last_reply = last_sync = last_status = last_meta = t0
    latest = {}
    with open(csv_path, "x", newline="", buffering=1) as f:   # "x": never truncate an existing file
        writer = csv.DictWriter(f, fieldnames=columns, restval="")
        writer.writeheader()
        try:
            while True:
                loop_start = time.monotonic()
                loop_pids = fast + ([slow[i % len(slow)]] if slow else [])
                i += 1
                row = {}
                for pid in loop_pids:
                    data = engine.request(pid)
                    if data is None:
                        continue
                    for col, decode in PIDS[pid]:
                        try:
                            value = decode(data)
                        except IndexError:
                            value = None
                        if value is not None:
                            row[col] = round(value, 3)
                now = time.monotonic()
                if row:
                    row["time"] = datetime.now().isoformat(timespec="milliseconds")
                    row["elapsed_s"] = round(now - t0, 3)
                    row["clock_ok"] = int(CLOCK_SYNCED.exists())
                    writer.writerow(row)
                    rows += 1
                    last_reply = now
                    latest.update(row)
                elif now - last_reply > args.end_after:
                    break
                else:
                    time.sleep(0.2)
                if now - last_sync > FSYNC_EVERY:
                    os.fsync(f.fileno())
                    last_sync = now
                if now - last_meta > META_EVERY:
                    meta.update(rows=rows, duration_s=round(now - t0, 1), complete=False)
                    write_meta(meta_path, meta)
                    last_meta = now
                time.sleep(max(0.0, 1 / args.hz - (time.monotonic() - loop_start)))
                if now - last_status > STATUS_EVERY:
                    log(f"  {rows} rows | rpm {latest.get('rpm', '-')} | coolant {latest.get('coolant_c', '-')} C"
                        f" | stft {latest.get('stft_pct', '-')}% ltft {latest.get('ltft_pct', '-')}%"
                        f" | boost {latest.get('boost_act_kpa', '-')} kPa")
                    last_status = now
        finally:
            os.fsync(f.fileno())
            duration = round(time.monotonic() - t0, 1)
            if rows == 0:
                csv_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                log("engine answered once but no data followed; nothing saved")
            else:
                meta.update(end=datetime.now().isoformat(timespec="seconds"),
                            rows=rows, duration_s=duration, complete=True)
                write_meta(meta_path, meta)
                log(f"drive ended: {rows} rows, {duration} s -> {daydir.name}/{csv_path.name}")


def run(args):
    bus = can.Bus(interface=args.interface, channel=args.channel, can_filters=[
        dict(can_id=ENGINE_RX, can_mask=0x7FF, extended=False),
        dict(can_id=KEEPALIVE_ID, can_mask=0x1FFFFFFF, extended=True),
    ])
    engine = Engine(bus)
    tries_left = DETECT_TRIES
    was_quiet = True
    drives = 0
    log(f"CarPi logger up on {args.channel}; waiting for the car")
    try:
        while True:
            rx = engine.recv(1.0)
            quiet_for = time.monotonic() - engine.last_activity
            if rx is None:
                if quiet_for > QUIET_RESET and not was_quiet:
                    log("bus asleep; detection re-armed")
                    was_quiet, tries_left = True, DETECT_TRIES
                continue
            if was_quiet:
                log("bus awake")
                was_quiet = False
            if tries_left <= 0:
                continue                      # car awake but ignition off: stay silent
            tries_left -= 1
            if engine.detect():
                log_drive(engine, args)
                drives += 1
                if args.once:
                    return
                tries_left = DETECT_TRIES     # catch a quick restart after ignition off
            else:
                time.sleep(DETECT_EVERY)
    finally:
        bus.shutdown()


def main():
    ap = argparse.ArgumentParser(description="CarPi OBD-II drive logger")
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--interface", default="socketcan")
    ap.add_argument("--logdir", default="~/carpi/logs")
    ap.add_argument("--tag-file", default="~/carpi/tag.txt")
    ap.add_argument("--end-after", type=float, default=8.0,
                    help="seconds of engine silence that end a drive")
    ap.add_argument("--hz", type=float, default=10.0, help="max rows per second")
    ap.add_argument("--once", action="store_true", help="exit after one drive (testing)")
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, _stop)   # systemd stop/shutdown -> close the CSV cleanly
    run(args)


def _stop(signum, frame):
    raise SystemExit(0)


if __name__ == "__main__":
    main()