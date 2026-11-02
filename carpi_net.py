"""
carpi_net.py - the Pi's Wi-Fi through NetworkManager, for the phone page's Settings tab.

Reads, scans, reorders, connects and forgets with `nmcli` (no secrets ever go on its command line).
Adding a network uses NetworkManager's D-Bus AddConnection, so the password only travels inside
that D-Bus message: it is never logged, put in an argument list, or returned to the phone.

The Bluetooth service runs as the user carpi; pi-setup/50-carpi.rules (polkit) lets it make these
changes without a password. Without that file NetworkManager refuses, and the phone is told to run
the setup.

Standard library only at import time (python3-dbus is imported when a network is added), so the
tests run anywhere. Every backend is injectable; see tests/test_net.py.
"""
import json
import re
import socket
import subprocess
import uuid as uuidlib

NMCLI_TIMEOUT_S = 15
TOP_PRIORITY = 10
UUID_OK = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SETUP_HINT = ("This Pi isn't set up for settings yet: in an SSH session run  "
              "sudo ~/carpi/pi-setup/install.sh")
WIFI = "802-11-wireless"


class NetError(Exception):
    """A problem worth showing on the phone, already phrased for it."""


def split_terse(line):
    """One line of `nmcli -t` output: fields split on ':', with '\\:' and '\\\\' as escapes."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def friendly(stderr):
    """An nmcli/NetworkManager error, shortened for a phone screen."""
    text = (stderr or "").strip()
    if re.search(r"not authorized|insufficient privileges|permission ?denied", text, re.I):
        return SETUP_HINT
    first = text.splitlines()[0] if text else "NetworkManager refused"
    if first.startswith("Error: "):
        first = first[len("Error: "):]
    return first[:160]


def validate(ssid, password):
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32:
        raise NetError("Network name: 1 to 32 characters")
    if not isinstance(password, str) or (password and not 8 <= len(password) <= 63):
        raise NetError("Password: 8 to 63 characters, or empty for an open network")


def check_uuid(value):
    if not isinstance(value, str) or not UUID_OK.match(value):
        raise NetError("Unknown network id")
    return value


def wifi_settings(ssid, password, priority):
    """The AddConnection settings for a WPA-PSK network, or an open one when password is ""."""
    settings = {
        "connection": {"id": ssid, "uuid": str(uuidlib.uuid4()), "type": WIFI, "autoconnect": True,
                       "autoconnect-priority": priority, "autoconnect-retries": 0},
        WIFI: {"ssid": ssid.encode("utf-8"), "mode": "infrastructure"},
        "ipv4": {"method": "auto"},
        "ipv6": {"method": "auto"},
    }
    if password:
        settings["802-11-wireless-security"] = {"key-mgmt": "wpa-psk", "psk": password}
    return settings


def parse_addresses(ip_json):
    """`ip -j -4 addr show` -> {"wlan0": ["192.168.1.40"]}, loopback and empty interfaces left out."""
    out = {}
    for iface in json.loads(ip_json or "[]"):
        name = iface.get("ifname")
        ips = [a["local"] for a in iface.get("addr_info", []) if a.get("local")]
        if name and name != "lo" and ips:
            out[name] = ips
    return out


# ---------- real backends (the Pi) ----------

def _run_nmcli(args):
    try:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=NMCLI_TIMEOUT_S)
    except FileNotFoundError:
        raise NetError("NetworkManager isn't available on this Pi") from None
    except subprocess.TimeoutExpired:
        raise NetError("NetworkManager didn't answer in time") from None
    if r.returncode:
        raise NetError(friendly(r.stderr or r.stdout))
    return r.stdout


def _ip_addresses():
    try:
        r = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=5)
        return parse_addresses(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _nm(interface, path):
    import dbus
    obj = dbus.SystemBus().get_object("org.freedesktop.NetworkManager", path)
    return dbus, dbus.Interface(obj, interface)


def _dbus_add(settings):
    dbus, settings_iface = _nm("org.freedesktop.NetworkManager.Settings",
                               "/org/freedesktop/NetworkManager/Settings")
    conv = dbus.Dictionary(signature="sa{sv}")
    for group, values in settings.items():
        conv[group] = dbus.Dictionary(
            {k: dbus.ByteArray(v) if isinstance(v, bytes) else v for k, v in values.items()},
            signature="sv")
    try:
        return str(settings_iface.AddConnection(conv))
    except dbus.exceptions.DBusException as e:
        raise NetError(friendly(f"{e.get_dbus_name()}: {e.get_dbus_message()}")) from None


def _dbus_activate(path):
    dbus, manager = _nm("org.freedesktop.NetworkManager", "/org/freedesktop/NetworkManager")
    try:
        manager.ActivateConnection(dbus.ObjectPath(path), dbus.ObjectPath("/"), dbus.ObjectPath("/"))
    except dbus.exceptions.DBusException as e:
        raise NetError(friendly(f"{e.get_dbus_name()}: {e.get_dbus_message()}")) from None


# ---------- the API the Bluetooth protocol uses ----------

class Net:
    def __init__(self, run=_run_nmcli, add_connection=_dbus_add, activate=_dbus_activate,
                 addresses=_ip_addresses, hostname=socket.gethostname):
        self.run = run
        self.add_connection = add_connection
        self.activate = activate
        self.addresses = addresses
        self.hostname = hostname

    def _lines(self, *args):
        return [split_terse(line) for line in self.run(list(args)).splitlines() if line.strip()]

    def _saved(self):
        out = []
        for row in self._lines("-t", "-f", "NAME,UUID,TYPE,AUTOCONNECT-PRIORITY,ACTIVE", "connection", "show"):
            if len(row) < 5 or row[2] != WIFI:
                continue
            name, uuid, _, prio, active = row[:5]
            ssid_rows = self._lines("-t", "-g", f"{WIFI}.ssid", "connection", "show", "uuid", uuid)
            ssid = ssid_rows[0][0] if ssid_rows else name
            out.append({"uuid": uuid, "name": name, "ssid": ssid,
                        "priority": int(prio) if prio.lstrip("-").isdigit() else 0, "active": active == "yes"})
        return sorted(out, key=lambda n: (-n["priority"], n["name"].lower()))

    def _visible(self):
        """Networks the Wi-Fi chip last saw: [(in_use, ssid, signal, secured)]."""
        rows = self._lines("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "--rescan", "no")
        return [(r[0].strip() == "*", r[1], int(r[2]) if r[2].isdigit() else 0, bool(r[3].strip()))
                for r in rows if len(r) >= 4]

    def state(self):
        addresses = self.addresses()
        wifi = {"state": "unavailable", "ssid": None, "signal": None, "ip": None}
        for row in self._lines("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"):
            if len(row) >= 3 and row[1] == "wifi":
                wifi["state"] = row[2]
                ips = addresses.get(row[0], [])
                wifi["ip"] = ips[0] if ips else None
                break
        for in_use, ssid, signal, _ in self._visible():
            if in_use:
                wifi["ssid"], wifi["signal"] = ssid, signal
        return {"wifi": wifi, "saved": self._saved(), "addresses": addresses, "hostname": self.hostname()}

    def scan(self, rescan=True):
        if rescan:
            try:
                self.run(["device", "wifi", "rescan"])      # returns at once; results come in seconds
            except NetError:
                pass                                       # e.g. a scan is already running
        saved = {n["ssid"] for n in self._saved()}
        best = {}
        for _, ssid, signal, secured in self._visible():
            if ssid and (ssid not in best or signal > best[ssid]["signal"]):
                best[ssid] = {"ssid": ssid, "signal": signal, "secured": secured, "saved": ssid in saved}
        return sorted(best.values(), key=lambda n: (-n["signal"], n["ssid"].lower()))

    def add(self, ssid, password, connect=False):
        validate(ssid, password)
        saved = self._saved()
        if any(n["ssid"] == ssid for n in saved):
            raise NetError(f'"{ssid}" is already saved. Forget it first to change its password.')
        priority = max(1, min(n["priority"] for n in saved) - 1) if saved else TOP_PRIORITY
        path = self.add_connection(wifi_settings(ssid, password, priority))
        if connect:
            self.activate(path)
        return {"added": ssid, "connecting": bool(connect)}

    def forget(self, uuid):
        self.run(["connection", "delete", "uuid", check_uuid(uuid)])

    def set_order(self, uuids):
        """First = most preferred. Priorities 10, 9, 8... (never below 1)."""
        uuids = [check_uuid(u) for u in uuids]
        for i, u in enumerate(uuids):
            self.run(["connection", "modify", "uuid", u, "connection.autoconnect-priority",
                      str(max(1, TOP_PRIORITY - i))])

    def connect(self, uuid):
        """Starts connecting and returns at once; the phone re-reads the state to see the result."""
        self.run(["--wait", "0", "connection", "up", "uuid", check_uuid(uuid)])
