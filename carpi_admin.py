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

Drive names and times are in the Pi's local time, so the phone page also sends its time zone and
set_zone() switches the Pi to it through systemd-timedated (`timedatectl set-timezone`), which the
same polkit rule allows. The name is checked first: Area/City characters only, and it must exist
in /usr/share/zoneinfo. The logger picks the new zone up at the start of its next drive.
"""
import math
import re
import subprocess
import time
from pathlib import Path

import carpi_net

SYNCED = Path("/run/systemd/timesync/synchronized")    # exists once NTP has set the clock
MARKER = Path("/run/carpi/clock_from_phone")
ZONEINFO = Path("/usr/share/zoneinfo")
ZONE_NAME = re.compile(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*")     # America/New_York, Etc/GMT+5
MIN_AHEAD_S = 2.0
MAX_AHEAD_S = 366 * 86400
POWER = {"shutdown": ["systemctl", "poweroff"], "reboot": ["systemctl", "reboot"]}


def _clock_settime(t):
    time.clock_settime(time.CLOCK_REALTIME, t)


class Admin:
    def __init__(self, run=subprocess.run, now=time.time, set_time=_clock_settime,
                 synced=SYNCED, marker=MARKER, zoneinfo=ZONEINFO):
        self.run = run
        self.now = now
        self.set_time = set_time
        self.synced = Path(synced)
        self.marker = Path(marker)
        self.zoneinfo = Path(zoneinfo)

    def clock_status(self):
        synced = self.synced.exists()
        source = "ntp" if synced else "phone" if self.marker.exists() else "none"
        return {"synced": synced, "source": source, "pi_time_ms": int(self.now() * 1000),
                "zone": self.time_zone()}

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

    def time_zone(self):
        """The Pi's time zone ("America/New_York"), or None if timedatectl can't say."""
        try:
            r = self.run(["timedatectl", "show", "-p", "Timezone", "--value"],
                         capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode:
            return None
        return (r.stdout or "").strip() or None

    def set_zone(self, zone):
        """Switch the Pi to the phone's time zone. Raises ValueError for a bad or unknown name."""
        if not isinstance(zone, str) or len(zone) > 64 or not ZONE_NAME.fullmatch(zone):
            raise ValueError("zone: a time zone name like America/New_York")
        if not (self.zoneinfo / zone).is_file():
            raise ValueError(f"unknown time zone: {zone}")
        if zone == self.time_zone():
            return {"zone": zone, "set": False, "reason": "already this time zone"}
        try:
            r = self.run(["timedatectl", "--no-ask-password", "set-timezone", zone],
                         capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            raise ValueError(f"couldn't run timedatectl: {e}") from None
        if r.returncode:
            raise ValueError(carpi_net.friendly(r.stderr))
        return {"zone": zone, "set": True, "reason": "set from the phone"}

    def power(self, action):
        """The shutdown/restart to run once the reply is out. Raises ValueError for anything else."""
        if not isinstance(action, str) or action not in POWER:
            raise ValueError('power: "shutdown" or "reboot"')
        cmd = POWER[action]
        return lambda: self.run(cmd, timeout=30)
