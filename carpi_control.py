"""
carpi_control.py - the phone <-> Pi protocol behind carpi_ble.py. Plain Python, no Bluetooth,
so it can be tested anywhere.

Transport: the phone writes UTF-8 JSON lines (each ending in "\\n") to the RX characteristic, in
chunks of any size; the Pi answers with one JSON line per request as notifications on TX, split
to fit the link's MTU. Splitting on newlines makes the framing independent of how BLE chunks
the bytes.

Requests:  {"rid": 7, "cmd": "<name>", ...}       Replies: {"rid": 7, "ok": true, ...}
                                                            {"rid": 7, "ok": false, "error": "..."}
  hello                    catalog of values + everything in "state" (below)
  state                    presets, active preset id, tag, supported PIDs, logger status
  status                   logger status only (polled every few seconds)
  save     preset, activate (bool)   create or overwrite a saved preset
  delete   id
  activate id              the logger picks it up within a second (mid-drive: new file)
  tag      tag             one word for ~/carpi/tag.txt ("" clears it)
"""
import json
import time
from pathlib import Path

import carpi_presets as presets

PROTOCOL = 1
SERVICE_UUID = "6c2a0001-4f3b-4d1e-9a57-3c8e2b7d5f10"
RX_UUID = "6c2a0002-4f3b-4d1e-9a57-3c8e2b7d5f10"      # phone writes requests here
TX_UUID = "6c2a0003-4f3b-4d1e-9a57-3c8e2b7d5f10"      # Pi notifies replies here
MAX_LINE = 4096
STATUS_STALE_S = 10.0


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


def catalog():
    return [{"id": pid, "label": label, "unit": unit, "group": group, "columns": cols,
             "viewer": pid in presets.VIEWER_PIDS}
            for pid, (label, unit, group, cols) in presets.CATALOG.items()]


class Controller:
    def __init__(self, store, status_path):
        self.store = store
        self.status_path = Path(status_path) if status_path else None

    def status(self):
        data = presets.read_json(self.status_path) if self.status_path else None
        if not isinstance(data, dict):
            return {"state": "unknown"}
        if time.time() - data.get("updated", 0) > STATUS_STALE_S:
            return {"state": "offline"}            # logger stopped or crashed
        data.pop("updated", None)
        return data

    def state(self):
        return {"presets": self.store.presets(), "active": self.store.active_id(),
                "tag": self.store.tag(), "supported": self.store.supported(),
                "status": self.status()}

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
            return {"status": self.status()}
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
        raise presets.PresetError(f"unknown command {cmd!r}")

    def handle_line(self, line):
        """One request line in, one encoded reply line out. Never raises."""
        rid = None
        try:
            msg = json.loads(line)
            rid = msg.get("rid") if isinstance(msg, dict) else None
            reply = dict(self.handle(msg), ok=True)
        except presets.PresetError as e:
            reply = {"ok": False, "error": str(e)}
        except ValueError:
            reply = {"ok": False, "error": "bad JSON"}
        except OSError as e:
            reply = {"ok": False, "error": f"Pi storage error: {e.strerror or e}"}
        reply["rid"] = rid
        return (json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
