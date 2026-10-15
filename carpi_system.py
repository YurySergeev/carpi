"""
carpi_system.py - Pi health and OBD/CAN link readings for the phone dashboard.

carpi_ble.py calls Sampler.sample() every couple of seconds on its main loop and puts
Sampler.snapshot() into every status reply. Everything here only READS: /proc, /sys, and
`ip -details -json link show can0`. It never opens a CAN socket, so it can't put a frame on the
bus, and it keeps working when the logger is stopped.

How "is it plugged into OBD?" is answered: the Pi only has power while the ignition is on, and
with the ignition on the car's gateway sends a keep-alive frame about twice a second. So frames
arriving on can0 (the kernel's rx_packets counter going up) means the cable is in and the car is
awake; no frames for a few seconds means the cable is out, or the car is asleep (bench power).

Standard library only. Every reading is best effort: anything missing comes back as None rather
than an exception, so a Pi without, say, the firmware throttle file still gets a dashboard.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

TRAFFIC_WINDOW_S = 5.0      # a frame this recent = bus awake (keep-alive is ~2 per second)

# Bits of the firmware's get_throttled value (same as `vcgencmd get_throttled`)
THROTTLE_BITS = {
    0: "under-voltage", 1: "clock capped", 2: "throttled", 3: "soft temperature limit",
}
THROTTLE_PAST_SHIFT = 16    # bits 16-19: the same four, "has happened since boot"


def _read(path):
    try:
        return Path(path).read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def _num(text, scale=1.0, base=10):
    """Decimal text -> float / scale; hex text (base=16, "0x" optional) -> int."""
    try:
        return int(text, base) if base != 10 else float(text) / scale
    except (TypeError, ValueError):
        return None


def _round(x, n=1):
    return None if x is None else round(x, n)


def throttle_flags(value):
    """get_throttled bits -> {"now": [...], "since_boot": [...]} (labels), or None."""
    if value is None:
        return None
    now = [label for bit, label in THROTTLE_BITS.items() if value & (1 << bit)]
    past = [label for bit, label in THROTTLE_BITS.items() if value & (1 << (bit + THROTTLE_PAST_SHIFT))]
    return {"now": now, "since_boot": past}


def cpu_times(stat_text):
    """(busy, total) jiffies from the aggregate "cpu" line of /proc/stat, or None."""
    if not stat_text:
        return None
    for line in stat_text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu":
            vals = [int(v) for v in parts[1:9]]          # user nice system idle iowait irq softirq steal
            idle = vals[3] + vals[4]
            total = sum(vals)
            return total - idle, total
    return None


def meminfo(text):
    """MemTotal and MemAvailable in kB from /proc/meminfo, or (None, None)."""
    found = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            found[key] = _num(rest.split()[0]) if rest.split() else None
    return found.get("MemTotal"), found.get("MemAvailable")


def can_link_info(text):
    """Controller state and error counters from `ip -details -json link show <if>` output."""
    try:
        link = json.loads(text)[0]
    except (TypeError, ValueError, IndexError, KeyError):
        return {}
    data = (link.get("linkinfo") or {}).get("info_data") or {}
    berr = data.get("berr_counter") or {}
    return {"controller": data.get("state"), "bitrate": (data.get("bittiming") or {}).get("bitrate"),
            "tx_errors": berr.get("tx"), "rx_errors": berr.get("rx")}


def obd_verdict(present, up, controller, last_frame_s):
    """One word the page turns into a sentence (see carpi_control.py)."""
    if not present:
        return "no_interface"           # CAN HAT not detected (overlay / SPI)
    if not up:
        return "down"                   # can0.service didn't bring it up
    if controller == "BUS-OFF":
        return "bus_off"                # wiring or bitrate problem
    if last_frame_s is not None and last_frame_s <= TRAFFIC_WINDOW_S:
        return "traffic"                # plugged in, car awake
    return "quiet"                      # unplugged, or the car is asleep


class Sampler:
    """Keeps the latest readings. sample() returns True so it can be a GLib timeout callback."""

    def __init__(self, root="/", can="can0", disk="~/carpi", clock=time.monotonic, run=None):
        self.root = Path(root)
        self.can = can
        self.disk = Path(disk).expanduser()
        self.clock = clock
        self.run = run or self._run
        self._ip = shutil.which("ip") or "/usr/sbin/ip"
        self._prev_cpu = None
        self._prev_rx = None            # (rx_packets, when)
        self._last_frame = None         # monotonic time rx_packets last went up
        self._sampled = None
        self.system = {}
        self.obd = {}

    def _p(self, rel):
        return self.root / rel

    @staticmethod
    def _run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=1.5).stdout
        except (OSError, subprocess.SubprocessError):
            return None

    # ---- system ----
    def _throttled(self):
        raw = _read(self._p("sys/devices/platform/soc/soc:firmware/get_throttled"))
        if raw is not None:
            return _num(raw, base=16)
        if shutil.which("vcgencmd") and self.root == Path("/"):
            out = self.run(["vcgencmd", "get_throttled"]) or ""     # "throttled=0x50000"
            return _num(out.partition("=")[2].strip(), base=16)
        return None

    def _sample_system(self):
        cpu = cpu_times(_read(self._p("proc/stat")))
        cpu_pct = None
        if cpu and self._prev_cpu:
            busy, total = cpu[0] - self._prev_cpu[0], cpu[1] - self._prev_cpu[1]
            cpu_pct = 100.0 * busy / total if total > 0 else None
        if cpu:
            self._prev_cpu = cpu
        total_kb, avail_kb = meminfo(_read(self._p("proc/meminfo")))
        load = (_read(self._p("proc/loadavg")) or "").split()
        uptime = (_read(self._p("proc/uptime")) or "").split()
        freq = "sys/devices/system/cpu/cpu0/cpufreq/"
        disk_free = disk_total = None
        try:
            st = os.statvfs(self.disk if self.disk.exists() else self.root)
            disk_free, disk_total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
        except OSError:
            pass
        self.system = {
            "cpu_temp_c": _round(_num(_read(self._p("sys/class/thermal/thermal_zone0/temp")), 1000)),
            "cpu_pct": _round(cpu_pct),
            "cpu_mhz": _round(_num(_read(self._p(freq + "scaling_cur_freq")), 1000), 0),
            "cpu_max_mhz": _round(_num(_read(self._p(freq + "cpuinfo_max_freq")), 1000), 0),
            "cores": os.cpu_count() if self.root == Path("/") else None,
            "load_1m": _round(_num(load[0]) if load else None, 2),
            "mem_pct": _round(100.0 * (1 - avail_kb / total_kb)) if total_kb and avail_kb is not None else None,
            "mem_total_mb": _round(total_kb / 1024, 0) if total_kb else None,
            "disk_free_gb": _round(disk_free / 1e9, 2) if disk_free is not None else None,
            "disk_pct": _round(100.0 * (1 - disk_free / disk_total)) if disk_total else None,
            "uptime_s": _round(_num(uptime[0]) if uptime else None, 0),
            "throttle": throttle_flags(self._throttled()),
        }

    # ---- CAN / OBD ----
    def _sample_obd(self, now):
        net = self._p(f"sys/class/net/{self.can}")
        present = net.is_dir()
        flags = _num(_read(net / "flags"), base=16) if present else None     # "0x40081"
        up = bool(flags is not None and flags & 0x1)                     # IFF_UP
        rx = _num(_read(net / "statistics" / "rx_packets")) if present else None
        rx_per_s = None
        first = self._prev_rx is None           # one reading can't show a change yet
        if rx is not None:
            if self._prev_rx is not None:
                prev, when = self._prev_rx
                if rx > prev:
                    self._last_frame = now
                if rx >= prev and now > when:
                    rx_per_s = (rx - prev) / (now - when)
            self._prev_rx = (rx, now)
        info = can_link_info(self.run([self._ip, "-details", "-json", "link", "show", self.can])) if present else {}
        last = None if self._last_frame is None else now - self._last_frame
        self.obd = {
            "iface": self.can, "present": present, "up": up,
            "controller": info.get("controller"), "bitrate": info.get("bitrate"),
            "tx_errors": info.get("tx_errors"), "rx_errors": info.get("rx_errors"),
            "rx_per_s": _round(rx_per_s), "last_frame_s": _round(last),
            "verdict": ("checking" if first and present and up and info.get("controller") != "BUS-OFF"
                        else obd_verdict(present, up, info.get("controller"), last)),
        }

    def sample(self):
        now = self.clock()
        try:
            self._sample_system()
        except Exception as e:          # a dashboard reading must never take the BLE service down
            self.system = {"error": f"{type(e).__name__}: {e}"}
        try:
            self._sample_obd(now)
        except Exception as e:
            self.obd = {"verdict": "unknown", "error": f"{type(e).__name__}: {e}"}
        self._sampled = now
        return True

    def snapshot(self):
        """{"system": {...}, "obd": {...}} as of the last sample; samples first if never run."""
        if self._sampled is None:
            self.sample()
        age = self.clock() - self._sampled
        return {"system": dict(self.system, age_s=_round(age)), "obd": dict(self.obd)}
