"""
carpi_admin.py - shut down / restart, and the clock set from the phone, for the Settings tab.

Power goes through systemd-logind (`systemctl poweroff` / `reboot` as the user carpi), which
pi-setup/50-carpi.rules (polkit) allows without a password. power() only returns the action:
carpi_ble.py runs it a couple of seconds after the reply has reached the phone.

The Pi has no battery-backed clock, so after a power cut it starts with a stale time until it
syncs over the internet. When it hasn't synced, the phone page sends its own time; set_clock()
uses it only if it's later than the Pi's (a restored clock is always behind) and less than a year
later. That needs CAP_SYS_TIME, which systemd/carpi-ble.service grants. A marker in /run/carpi
tells the logger the clock is now trustworthy (clock_ok).
"""
import math
import subprocess
import time
from pathlib import Path

SYNCED = Path("/run/systemd/timesync/synchronized")    # exists once NTP has set the clock
MARKER = Path("/run/carpi/clock_from_phone")
MIN_AHEAD_S = 2.0
MAX_AHEAD_S = 366 * 86400
POWER = {"shutdown": ["systemctl", "poweroff"], "reboot": ["systemctl", "reboot"]}


def _clock_settime(t):
    time.clock_settime(time.CLOCK_REALTIME, t)


class Admin:
    def __init__(self, run=subprocess.run, now=time.time, set_time=_clock_settime,
                 synced=SYNCED, marker=MARKER):
        self.run = run
        self.now = now
        self.set_time = set_time
        self.synced = Path(synced)
        self.marker = Path(marker)

    def clock_status(self):
        synced = self.synced.exists()
        source = "ntp" if synced else "phone" if self.marker.exists() else "none"
        return {"synced": synced, "source": source, "pi_time_ms": int(self.now() * 1000)}

    def set_clock(self, now_ms):
        if isinstance(now_ms, bool) or not isinstance(now_ms, (int, float)) or not math.isfinite(now_ms) or now_ms <= 0:
            raise ValueError("now_ms: the phone's time in milliseconds")
        phone, pi = now_ms / 1000, self.now()
        reason = None
        if self.synced.exists():
            reason = "already synced from the internet"
        elif phone <= pi + MIN_AHEAD_S:
            reason = "the Pi's clock is already ahead of the phone"
        elif phone - pi > MAX_AHEAD_S:
            reason = "the phone's time looks wrong (over a year ahead)"
        if reason:
            return dict(self.clock_status(), set=False, reason=reason)
        try:
            self.set_time(phone)
        except PermissionError:
            raise ValueError("The Bluetooth service can't set the clock: in an SSH session run  "
                             "sudo ~/carpi/pi-setup/install.sh") from None
        try:
            self.marker.write_text(f"{phone:.0f}\n")
        except OSError:
            pass                                  # no /run/carpi (logger not running): nothing to tell it
        return dict(self.clock_status(), set=True, reason="set from the phone")

    def power(self, action):
        """The shutdown/restart to run once the reply is out. Raises ValueError for anything else."""
        if not isinstance(action, str) or action not in POWER:
            raise ValueError('power: "shutdown" or "reboot"')
        cmd = POWER[action]
        return lambda: self.run(cmd, timeout=30)
