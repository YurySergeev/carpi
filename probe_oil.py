#!/usr/bin/env python3
"""
probe_oil.py - one-time check of the engine-oil-temperature reading before the logger uses it.

This A3's engine doesn't offer the standard OBD-II oil-temperature PID (0x5C), so oil temperature
needs a VW-specific read: UDS ReadDataByIdentifier (service 0x22). The candidate identifier is
0x202F, which hasn't been confirmed on this car. This script reads it so the number can be
compared with the oil temperature on the dash.

What it sends to the car, and nothing else:
  01 00     is the engine answering? (standard OBD-II)
  22 20 2F  read identifier 0x202F: read-only, in the engine's current diagnostic session.
            No session change, no security access, no other identifiers.
  01 05     coolant temperature (standard OBD-II), printed alongside for comparison

Run it in an SSH session on the Pi, with the logger stopped (one talker on the CAN bus):
    sudo systemctl stop carpi-logger
    python3 ~/carpi/probe_oil.py --watch       # a reading every 2 s; Ctrl+C to stop
    sudo systemctl start carpi-logger

Best done from a cold start: note what the dash shows for oil temperature, together with the
elapsed seconds on screen, at a few points as the engine warms (cold, about 60 C, fully warm).
Every reading is also saved to ~/carpi/probes/oil-check-<time>.csv for comparing afterwards.
"""
import argparse
import csv
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import can

import carpi_logger as cl

# The only identifiers this script will ever request. Add one only after it's been researched.
ALLOWED = {0x202F: "engine oil temperature (candidate, unconfirmed on this car)"}
DEFAULT_DID = 0x202F
WATCH_EVERY = 2.0

# Common ways a VW identifier encodes a temperature (A, B = first data bytes). Which one, if any,
# tracks the dash is what this check finds out; the raw bytes are saved too, for any other scaling.
CANDIDATES = [
    ("a", "A - 40", 1, lambda d: d[0] - 40),
    ("b", "A x 0.75 - 48", 1, lambda d: d[0] * 0.75 - 48),
    ("c", "(256A+B)/10 - 273.1", 2, lambda d: (d[0] * 256 + d[1]) / 10 - 273.1),
    ("d", "(256A+B)/10", 2, lambda d: (d[0] * 256 + d[1]) / 10),
]


def read_oil(engine, did=DEFAULT_DID):
    """Data bytes for an allowed identifier, None on no reply; NegativeResponse if refused."""
    if did not in ALLOWED:
        allowed = ", ".join(f"0x{d:04X}" for d in ALLOWED)
        raise ValueError(f"0x{did:04X} isn't on the allow-list ({allowed})")
    return engine.read_did(did)


def candidates(data):
    return {label: round(fn(data), 1) for _, label, need, fn in CANDIDATES if len(data) >= need}


def reading(engine, t0, did=DEFAULT_DID):
    """One reading: the identifier's raw bytes and candidate conversions, plus coolant."""
    r = {"elapsed_s": round(time.monotonic() - t0, 1), "raw": None, "candidates": {}, "error": None}
    try:
        data = read_oil(engine, did)
    except cl.NegativeResponse as e:
        r["error"] = f"refused ({e})"
    else:
        if data is None:
            r["error"] = "no reply"
        else:
            r["raw"], r["candidates"] = data.hex(), candidates(data)
    coolant = engine.request(0x05)
    r["coolant_c"] = coolant[0] - 40 if coolant else None
    return r


def logger_running(run=subprocess.run):
    """True if the carpi-logger service is active (False where there's no systemd, e.g. a PC)."""
    try:
        out = run(["systemctl", "is-active", "carpi-logger"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() == "active"


def show(r):
    cells = [f"{r['elapsed_s']:>6.0f} s", f"{r['raw'] or '-':<10}"]
    for _, label, _, _ in CANDIDATES:
        v = r["candidates"].get(label)
        cells.append(f"{'-' if v is None else v:>7}")
    cool = "-" if r["coolant_c"] is None else f"{r['coolant_c']} °C"
    cells.append(f"{cool:>8}")
    line = "  ".join(cells)
    print(f"{line}  {r['error']}" if r["error"] else line, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="One-time check of the oil-temperature identifier 0x202F (read-only).")
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--interface", default="socketcan")
    ap.add_argument("--watch", action="store_true", help=f"read every {WATCH_EVERY:.0f} s until Ctrl+C")
    ap.add_argument("--outdir", default="~/carpi/probes")
    args = ap.parse_args(argv)

    if logger_running():
        sys.exit("carpi-logger is running. Stop it first, so only one program talks to the car:\n"
                 "  sudo systemctl stop carpi-logger")
    try:
        bus = can.Bus(interface=args.interface, channel=args.channel,
                      can_filters=[dict(can_id=cl.ENGINE_RX, can_mask=0x7FF, extended=False)])
    except (OSError, can.CanError) as e:
        sys.exit(f"Can't open {args.channel}: {e}\nCheck: ip link show {args.channel}")

    engine = cl.Engine(bus)
    outdir = Path(args.outdir).expanduser()
    path = outdir / f"oil-check-{datetime.now():%Y-%m-%d_%H%M%S}.csv"
    try:
        if not engine.detect():
            sys.exit("The engine didn't answer. Is the ignition on (or the engine running), "
                     "and the Pi on the OBD-II port?")
        outdir.mkdir(parents=True, exist_ok=True)
        print(f"Reading 0x{DEFAULT_DID:04X}: {ALLOWED[DEFAULT_DID]}")
        print("Candidate conversions to compare with the dash: "
              + "   ".join(f"{key} = {label}" for key, label, _, _ in CANDIDATES))
        print("elapsed   raw" + " " * 7 + "".join(f"{key:>9}" for key, _, _, _ in CANDIDATES) + "   coolant")
        t0 = time.monotonic()
        with open(path, "w", newline="") as f:
            out = csv.writer(f)
            out.writerow(["elapsed_s", "raw_hex"] + [label for _, label, _, _ in CANDIDATES]
                         + ["coolant_c", "error"])
            while True:
                started = time.monotonic()
                r = reading(engine, t0)
                show(r)
                out.writerow([r["elapsed_s"], r["raw"] or ""]
                             + [r["candidates"].get(label, "") for _, label, _, _ in CANDIDATES]
                             + [r["coolant_c"] if r["coolant_c"] is not None else "", r["error"] or ""])
                f.flush()
                if r["error"] and r["error"].startswith("refused"):
                    print("\nThe engine refused this identifier. CarPi doesn't switch diagnostic sessions "
                          "or unlock anything,\nso oil temperature waits until a VW tool (VCDS) confirms "
                          "the right identifier.")
                    break
                if not args.watch:
                    break
                time.sleep(max(0.0, WATCH_EVERY - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print()
    finally:
        bus.shutdown()
    if path.exists():
        print(f"Saved {path}")


if __name__ == "__main__":
    main()
