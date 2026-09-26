#!/usr/bin/env python3
"""
probe_addressing.py - CarPi's first active query on the Audi A3 (8V) OBD port.

The 8V's gateway (J533) keeps normal broadcast traffic off the OBD port, so a
passive candump only shows the gateway keep-alive frame (0x17F00010). To get
data we have to ASK for it. This script:

  1. Finds which addressing the car answers on (11-bit 0x7DF or 29-bit 0x18DB33F1)
  2. Asks every ECU that replies which standard OBD-II PIDs it supports
  3. Reads and decodes the live values CarPi cares about
  4. Saves everything (with raw hex) to ~/carpi/probes/probe-<time>.json

Read-only: it only sends standard OBD-II Mode 01 requests, same as any scan tool.

Usage (Pi plugged into the OBD port, ignition on or engine running):
    python3 probe_addressing.py            # one full probe
    python3 probe_addressing.py --watch    # live values every second, Ctrl+C to stop
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import can

REPLY_WINDOW = 0.25  # seconds to collect replies after each request (OBD allows 50 ms)

ADDRESSING = {
    "11-bit": dict(
        req_id=0x7DF, extended=False,
        is_reply=lambda aid: 0x7E8 <= aid <= 0x7EF,
        flt=dict(can_id=0x7E8, can_mask=0x7F8, extended=False),
    ),
    "29-bit": dict(
        req_id=0x18DB33F1, extended=True,
        is_reply=lambda aid: (aid & 0x1FFFFF00) == 0x18DAF100,
        flt=dict(can_id=0x18DAF100, can_mask=0x1FFFFF00, extended=True),
    ),
}

ECU_NAMES = {
    0x7E8: "engine", 0x7E9: "transmission",
    0x18DAF110: "engine", 0x18DAF118: "transmission",
}

# pid: (name, unit, decoder(data_bytes_after_pid) -> value)
PIDS = {
    0x04: ("engine_load", "%", lambda d: d[0] * 100 / 255),
    0x05: ("coolant_temp", "C", lambda d: d[0] - 40),
    0x06: ("stft_b1", "%", lambda d: (d[0] - 128) * 100 / 128),
    0x07: ("ltft_b1", "%", lambda d: (d[0] - 128) * 100 / 128),
    0x0B: ("map", "kPa", lambda d: d[0]),
    0x0C: ("rpm", "rpm", lambda d: (256 * d[0] + d[1]) / 4),
    0x0D: ("speed", "km/h", lambda d: d[0]),
    0x0E: ("timing_advance", "deg", lambda d: d[0] / 2 - 64),
    0x0F: ("intake_air_temp", "C", lambda d: d[0] - 40),
    0x10: ("maf", "g/s", lambda d: (256 * d[0] + d[1]) / 100),
    0x11: ("throttle", "%", lambda d: d[0] * 100 / 255),
    0x42: ("module_voltage", "V", lambda d: (256 * d[0] + d[1]) / 1000),
    0x46: ("ambient_temp", "C", lambda d: d[0] - 40),
    0x5C: ("oil_temp", "C", lambda d: d[0] - 40),
}
WATCH_PIDS = [0x0C, 0x05, 0x5C, 0x06, 0x07, 0x0E, 0x0B, 0x42]


def ecu_label(aid):
    name = ECU_NAMES.get(aid, "ECU")
    return f"{name} (0x{aid:X})"


def drain(bus):
    while bus.recv(timeout=0) is not None:
        pass


def query(bus, mode, service, pid, window=REPLY_WINDOW, stop_on=None):
    """Send one single-frame OBD request. Returns {reply_id: bytes | ("NRC", code) | ("MULTIFRAME", hex)}.
    If stop_on is a reply ID, return as soon as that ECU has answered."""
    cfg = ADDRESSING[mode]
    drain(bus)
    bus.send(can.Message(arbitration_id=cfg["req_id"], is_extended_id=cfg["extended"],
                         data=[0x02, service, pid, 0x55, 0x55, 0x55, 0x55, 0x55]),
             timeout=0.2)
    replies = {}
    deadline = time.monotonic() + window
    while (remaining := deadline - time.monotonic()) > 0:
        rx = bus.recv(timeout=remaining)
        if rx is None or not cfg["is_reply"](rx.arbitration_id) or not rx.data:
            continue
        d = bytes(rx.data)
        pci_type, length = d[0] >> 4, d[0] & 0x0F
        if pci_type != 0:  # multi-frame reply; Mode 01 never needs it
            replies[rx.arbitration_id] = ("MULTIFRAME", d.hex())
            continue
        payload = d[1:1 + length]
        if len(payload) >= 2 and payload[0] == service + 0x40 and payload[1] == pid:
            replies[rx.arbitration_id] = payload[2:]
        elif len(payload) >= 3 and payload[0] == 0x7F and payload[1] == service:
            replies[rx.arbitration_id] = ("NRC", payload[2])
        if stop_on is not None and stop_on in replies:
            break
    return replies


def supported_pids(bus, mode):
    """Walk the Mode 01 support bitmaps (PID 00, 20, 40...). Returns {reply_id: set(pids)}."""
    support = {}
    base = 0x00
    while base <= 0xC0:
        answered = False
        for aid, data in query(bus, mode, 0x01, base).items():
            if isinstance(data, tuple) or len(data) < 4:
                continue
            answered = True
            bits = int.from_bytes(data[:4], "big")
            pids = support.setdefault(aid, set())
            pids.update(base + i + 1 for i in range(32) if bits & (1 << (31 - i)))
        if not answered or not any((base + 0x20) in p for p in support.values()):
            break
        base += 0x20
    return support


def read_pids(bus, mode, ecu, pids):
    out = {}
    for pid in pids:
        name, unit, decode = PIDS[pid]
        data = query(bus, mode, 0x01, pid, stop_on=ecu).get(ecu)
        if data is None or isinstance(data, tuple):
            out[name] = {"value": None, "unit": unit, "raw": None if data is None else str(data)}
            continue
        try:
            value = round(decode(data), 2)
        except IndexError:
            value = None
        out[name] = {"value": value, "unit": unit, "raw": data.hex()}
    return out


def pick_engine(support):
    for aid in (0x7E8, 0x18DAF110):
        if aid in support:
            return aid
    return min(support)


def open_bus(args):
    try:
        return can.Bus(interface=args.interface, channel=args.channel,
                       can_filters=[c["flt"] for c in ADDRESSING.values()])
    except OSError as e:
        sys.exit(f"Can't open {args.channel}: {e}\nCheck: ip link show {args.channel}")


def probe(bus, args):
    result = {"time": datetime.now().isoformat(timespec="seconds"), "addressing": {}}
    working = None
    for mode, cfg in ADDRESSING.items():
        support = supported_pids(bus, mode)
        result["addressing"][mode] = {
            f"0x{aid:X}": sorted(f"{p:02X}" for p in pids) for aid, pids in support.items()
        }
        if not support:
            print(f"== {mode} (0x{cfg['req_id']:X}): no reply")
            continue
        print(f"== {mode} (0x{cfg['req_id']:X}): {len(support)} ECU(s) answered")
        for aid, pids in sorted(support.items()):
            print(f"   {ecu_label(aid)}: {len(pids)} PIDs -> {' '.join(f'{p:02X}' for p in sorted(pids))}")
        if working is None:
            working = (mode, support)

    if working is None:
        print("\nNo ECU answered on either addressing.")
        print("Check: Pi on the OBD port? Ignition ON? `ip -details -statistics link show can0` for errors.")
        return result

    mode, support = working
    ecu = pick_engine(support)
    wanted = [p for p in PIDS if p in support[ecu]]
    missing = [PIDS[p][0] for p in PIDS if p not in support[ecu]]
    print(f"\nLive values from {ecu_label(ecu)} via {mode}:")
    values = read_pids(bus, mode, ecu, wanted)
    for name, v in values.items():
        shown = "no reply" if v["value"] is None else f"{v['value']} {v['unit']}"
        print(f"   {name:<16} {shown:<14} raw={v['raw']}")
    if missing:
        print(f"   not offered as standard PIDs: {', '.join(missing)}")
    result.update(working_mode=mode, engine_ecu=f"0x{ecu:X}", values=values)

    outdir = Path(args.outdir).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / f"probe-{datetime.now():%Y-%m-%d_%H%M%S}.json"
    path.write_text(json.dumps(result, indent=2))
    print(f"\nSaved {path}")
    return result


def watch(bus, args):
    for mode in ADDRESSING:
        support = supported_pids(bus, mode)
        if support:
            break
    else:
        sys.exit("No ECU answered. Pi on the OBD port with ignition on?")
    ecu = pick_engine(support)
    pids = [p for p in WATCH_PIDS if p in support[ecu]]
    names = [PIDS[p][0] for p in pids]
    print(f"Watching {ecu_label(ecu)} via {mode}. Ctrl+C to stop.")
    print("time      " + "  ".join(f"{n:>14}" for n in names))
    try:
        while True:
            t0 = time.monotonic()
            vals = read_pids(bus, mode, ecu, pids)
            row = "  ".join(f"{'-' if v['value'] is None else v['value']:>14}" for v in vals.values())
            print(f"{datetime.now():%H:%M:%S}  {row}", flush=True)
            time.sleep(max(0, 1 - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        print()


def main():
    ap = argparse.ArgumentParser(description="Probe the car's OBD-II addressing and read live PIDs.")
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--interface", default="socketcan")
    ap.add_argument("--watch", action="store_true", help="print live values every second")
    ap.add_argument("--outdir", default="~/carpi/probes")
    args = ap.parse_args()
    bus = open_bus(args)
    try:
        watch(bus, args) if args.watch else probe(bus, args)
    except can.CanError as e:
        sys.exit(f"CAN send failed ({e}). Is the Pi on the OBD port with ignition on?")
    finally:
        bus.shutdown()


if __name__ == "__main__":
    main()