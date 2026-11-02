"""
carpi_control.py - the phone <-> Pi protocol behind carpi_ble.py. Plain Python, no Bluetooth,
so it can be tested anywhere.

Transport: the phone writes UTF-8 JSON lines (each ending in "\\n") to the RX characteristic, in
chunks of any size; the Pi answers with one JSON line per request as notifications on TX, split
into pieces of notify_size(mtu) bytes and sent a few ms apart. Splitting on newlines makes the
framing independent of how BLE chunks the bytes.

Requests:  {"rid": 7, "cmd": "<name>", ...}       Replies: {"rid": 7, "ok": true, ...}
                                                            {"rid": 7, "ok": false, "error": "..."}
  hello                    catalog of values + everything in "state" (below)
  state                    presets, active preset id, tag, supported PIDs, logger status,
                           Pi system readings and the OBD/CAN link (see "status")
  status                   for the dashboard, polled every couple of seconds:
                             status  the logger's /run/carpi/status.json ("offline" if stale)
                             system  Pi CPU temp, load, clock, memory, SD space, uptime,
                                     under-voltage / throttling (carpi_system.py)
                             obd     can0 state, frames/s, seconds since the last frame and a
                                     verdict: checking | traffic | quiet | down | bus_off |
                                     no_interface
  live     cols (optional list)  latest reading of each value the logger is collecting, for
                           the Live tab (polled twice a second while it's open):
                             {"live": {"state", "drive", "values": {col: [value, age_s]}}}
                           state: logging | ended (drive just ended) | not_reading (the
                           logger's live.json is stale) | unknown (no file). Only columns in
                           cols are returned; the phone asks for the ones on screen.
  save     preset, activate (bool)   create or overwrite a saved preset
  delete   id
  activate id              the logger picks it up within a second (mid-drive: new file)
  tag      tag             one word for ~/carpi/tag.txt ("" clears it)

Settings (protocol 3; carpi_net.py and carpi_admin.py, allowed by pi-setup/50-carpi.rules):
  net                      Wi-Fi state, saved networks in priority order, addresses, hostname
  net_scan  rescan (bool)  nearby networks, strongest first (rescan starts a fresh scan; the
                           phone asks again a few seconds later for the results)
  net_add   ssid, password, connect   save a network at the lowest priority (password "" =
                           open). The password goes only to NetworkManager: never into a reply.
  net_forget uuid  /  net_priority order (uuids, most preferred first)  /  net_connect uuid
  power     action         "shutdown" | "reboot": replies first, acts POWER_DELAY_S later
  clock     now_ms         the phone's time; used only if the Pi isn't synced and is behind
  timezone  zone           the phone's time zone ("America/New_York"); the Pi switches to it, and
                           the logger uses it from the next drive (protocol 4)
  hello and state also report clock: {synced, source, zone}.

Uploads (protocol 5; carpi_sync.Uploads):
  upload    auto, publish  either or both (true/false): automatic uploads, and whether new drives
                           go on the public analyzer (~/carpi/upload.json)
  upload_now               start an upload now (even with automatic off): creates
                           /run/carpi/sync-now, which carpi-sync.path turns into a run
  status, state and hello also report sync: {set_up, auto, publish, state, waiting, error,
  last_success_age_s, last_attempt_age_s, next_try_s} (ages measured from boot on the Pi).
"""
import json
import time
from pathlib import Path

import carpi_net
import carpi_presets as presets

PROTOCOL = 5            # 2: the "live" request; 3: Wi-Fi, power and clock settings; 4: time zone; 5: uploads
POWER_DELAY_S = 2       # shut down / restart this long after replying, so the reply gets out
UNAVAILABLE = "not available on this Pi: update CarPi (git pull) and run  sudo ~/carpi/pi-setup/install.sh"
SERVICE_UUID = "6c2a0001-4f3b-4d1e-9a57-3c8e2b7d5f10"
RX_UUID = "6c2a0002-4f3b-4d1e-9a57-3c8e2b7d5f10"      # phone writes requests here
TX_UUID = "6c2a0003-4f3b-4d1e-9a57-3c8e2b7d5f10"      # Pi notifies replies here
MAX_LINE = 4096
NOTIFY_MAX = 244        # bytes per reply notification: fits one LE data packet (251 - 4 L2CAP - 3 ATT)
STATUS_STALE_S = 10.0
LIVE_STALE_S = 3.0      # the logger rewrites live.json every 0.5 s while it's reading the car


class LineBuffer:
    """Reassembles newline-terminated messages from arbitrary chunks."""

    def __init__(self, limit=MAX_LINE):
        self.limit = limit
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        lines = []
        while (i := self.buf.find(b"\n")) >= 0:
            line, self.buf = bytes(self.buf[:i]), self.buf[i + 1:]
            if line.strip():
                lines.append(line.decode("utf-8", errors="replace"))
        if len(self.buf) > self.limit:            # no newline in sight: drop it, don't grow forever
            self.buf.clear()
            lines.append("")                       # becomes an error reply
        return lines


def chunks(data, size):
    return [data[i:i + size] for i in range(0, len(data), size)] or [b""]


def notify_size(mtu):
    """Bytes per notification for a link with this ATT MTU: MTU - 3, capped at NOTIFY_MAX,
    never under the 20 every link allows."""
    return max(20, min(int(mtu) - 3, NOTIFY_MAX))


def catalog():
    return [{"id": pid, "label": label, "unit": unit, "group": group, "columns": cols,
             "viewer": pid in presets.VIEWER_PIDS}
            for pid, (label, unit, group, cols) in presets.CATALOG.items()]


class Controller:
    def __init__(self, store, status_path, system=None, live_path=None, net=None, admin=None, uploads=None):
        """system: a callable returning {"system": {...}, "obd": {...}} (carpi_system.Sampler
        .snapshot), or None where there's no Pi to read (tests). live_path defaults to
        live.json next to the status file. net / admin / uploads: carpi_net.Net,
        carpi_admin.Admin and carpi_sync.Uploads, or None where they aren't available."""
        self.store = store
        self.status_path = Path(status_path) if status_path else None
        self.system = system
        if live_path is None and self.status_path is not None:
            live_path = self.status_path.with_name("live.json")
        self.live_path = Path(live_path) if live_path else None
        self.net = net
        self.admin = admin
        self.uploads = uploads
        self._after = None

    def take_after(self):
        """An action to run once the reply has been sent (shut down / restart), handed out once."""
        after, self._after = self._after, None
        return after

    @staticmethod
    def _need(backend):
        if backend is None:
            raise presets.PresetError(UNAVAILABLE)
        return backend

    def live_values(self, cols=None):
        """The logger's latest readings, with each value's age brought up to now."""
        data = presets.read_json(self.live_path) if self.live_path else None
        if not isinstance(data, dict) or not isinstance(data.get("values"), dict):
            return {"state": "unknown", "values": {}}
        file_age = time.time() - data.get("updated", 0)
        if file_age > LIVE_STALE_S:
            return {"state": "not_reading", "values": {}}    # logger stopped, crashed or idle
        wanted = set(cols) if isinstance(cols, list) else None
        values = {c: [v[0], round(v[1] + max(file_age, 0), 2)]
                  for c, v in data["values"].items()
                  if (wanted is None or c in wanted) and isinstance(v, list) and len(v) == 2}
        out = {"state": data.get("state", "unknown"), "values": values}
        if data.get("drive"):
            out["drive"] = data["drive"]
        return out

    def status(self):
        data = presets.read_json(self.status_path) if self.status_path else None
        if not isinstance(data, dict):
            return {"state": "unknown"}
        if time.time() - data.get("updated", 0) > STATUS_STALE_S:
            return {"state": "offline"}            # logger stopped or crashed
        data.pop("updated", None)
        return data

    def live(self):
        """Logger status plus the Pi's own readings: everything the dashboard shows."""
        out = {"status": self.status()}
        if self.system is not None:
            try:
                out.update(self.system())
            except Exception as e:             # readings are best effort; the reply still goes out
                out["system"] = {"error": f"{type(e).__name__}: {e}"}
        if self.uploads is not None:
            try:
                out["sync"] = self.uploads.status()
            except Exception as e:
                out["sync"] = {"error": f"{type(e).__name__}: {e}"}
        return out

    def state(self):
        out = dict(self.live(), presets=self.store.presets(), active=self.store.active_id(),
                   tag=self.store.tag(), supported=self.store.supported())
        if self.admin is not None:
            clock = self.admin.clock_status()
            out["clock"] = {"synced": clock["synced"], "source": clock["source"], "zone": clock.get("zone")}
        return out

    def handle(self, msg):
        if not isinstance(msg, dict):
            raise presets.PresetError("request must be a JSON object")
        cmd = msg.get("cmd")
        if cmd == "hello":
            return dict(self.state(), protocol=PROTOCOL, catalog=catalog(),
                        max_weight=presets.MAX_WEIGHT)
        if cmd == "state":
            return self.state()
        if cmd == "status":
            return self.live()
        if cmd == "live":
            return {"live": self.live_values(msg.get("cols"))}
        if cmd == "save":
            saved = self.store.save(msg.get("preset"))
            if msg.get("activate"):
                self.store.activate(saved["id"])
            return dict(self.state(), saved=saved["id"])
        if cmd == "delete":
            self.store.delete(str(msg.get("id", "")))
            return self.state()
        if cmd == "activate":
            self.store.activate(str(msg.get("id", "")))
            return self.state()
        if cmd == "tag":
            self.store.set_tag(msg.get("tag", ""))
            return self.state()
        if cmd in ("net", "net_scan", "net_add", "net_forget", "net_priority", "net_connect"):
            return self._network(cmd, msg, self._need(self.net))
        if cmd == "clock":
            return {"clock": self._need(self.admin).set_clock(msg.get("now_ms"))}
        if cmd == "timezone":
            return {"timezone": self._need(self.admin).set_zone(msg.get("zone"))}
        if cmd == "upload":
            changes = {k: msg[k] for k in ("auto", "publish") if k in msg}
            return {"sync": self._need(self.uploads).change(**changes)}
        if cmd == "upload_now":
            status = self._need(self.uploads).start_now()
            return {"sync": {k: v for k, v in status.items() if k != "started"}, "started": True}
        if cmd == "power":
            action = msg.get("action")
            self._after = self._need(self.admin).power(action)      # raises for anything unknown
            return {"power": action, "in_s": POWER_DELAY_S}
        raise presets.PresetError(f"unknown command {cmd!r}")

    @staticmethod
    def _network(cmd, msg, net):
        if cmd == "net_scan":
            return {"nearby": net.scan(bool(msg.get("rescan", True)))}
        if cmd == "net_add":
            carpi_net.validate(msg.get("ssid"), msg.get("password", ""))     # checked here too, before any backend
            added = net.add(msg.get("ssid"), msg.get("password", ""), bool(msg.get("connect")))
            return dict(net.state(), **added)
        if cmd == "net_forget":
            net.forget(msg.get("uuid"))
        elif cmd == "net_priority":
            if not isinstance(msg.get("order"), list):
                raise carpi_net.NetError("order: a list of network ids, most preferred first")
            net.set_order(msg["order"])
        elif cmd == "net_connect":
            net.connect(msg.get("uuid"))
            return dict(net.state(), connecting=True)
        return net.state()

    def handle_line(self, line):
        """One request line in, one encoded reply line out. Never raises."""
        rid = None
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"ok": False, "error": "bad JSON"}
        else:
            rid = msg.get("rid") if isinstance(msg, dict) else None
            try:
                reply = dict(self.handle(msg), ok=True)
            except (presets.PresetError, carpi_net.NetError, ValueError) as e:
                reply = {"ok": False, "error": str(e)}
            except OSError as e:
                reply = {"ok": False, "error": f"Pi storage error: {e.strerror or e}"}
        reply["rid"] = rid
        return (json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
