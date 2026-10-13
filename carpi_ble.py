#!/usr/bin/env python3
"""
carpi_ble.py - Bluetooth LE service that lets the phone pick what the logger records.

Runs next to carpi-logger as its own systemd service. It never touches the CAN bus: all it does
is read and write small files in ~/carpi (presets, active_preset.json, tag.txt) and read the
logger's /run/carpi/status.json. The protocol is in carpi_control.py; the phone page is
web/config/.

BlueZ over D-Bus, standard Raspberry Pi OS packages only:
  sudo apt install -y python3-dbus python3-gi
  sudo usermod -aG bluetooth carpi        # D-Bus access to BlueZ without root

Security: both characteristics need an encrypted (paired) link, and the Pi only accepts new
pairings during a short window you open yourself over SSH:
  python3 ~/carpi/carpi_ble.py --pair      # 120 s to pair from the phone
Phones paired before keep working after the window closes.
"""
import argparse
import sys

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

import carpi_control
import carpi_presets

BLUEZ = "org.bluez"
OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
PROPERTIES = "org.freedesktop.DBus.Properties"
ADAPTER = "org.bluez.Adapter1"
GATT_MANAGER = "org.bluez.GattManager1"
AD_MANAGER = "org.bluez.LEAdvertisingManager1"
AGENT_MANAGER = "org.bluez.AgentManager1"
GATT_SERVICE = "org.bluez.GattService1"
GATT_CHRC = "org.bluez.GattCharacteristic1"
AD = "org.bluez.LEAdvertisement1"
AGENT = "org.bluez.Agent1"

ROOT = "/org/carpi"
DEFAULT_MTU = 23


def log(msg):
    print(msg, flush=True)


class NotSupported(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.NotSupported"


class InvalidArgs(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


class Rejected(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


# ---------- GATT objects (the shape BlueZ's example-gatt-server uses) ----------

class Application(dbus.service.Object):
    def __init__(self, bus):
        self.path = ROOT
        self.services = []
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(OBJECT_MANAGER, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        out = {}
        for s in self.services:
            out[s.get_path()] = s.get_properties()
            for c in s.characteristics:
                out[c.get_path()] = c.get_properties()
        return out


class Service(dbus.service.Object):
    def __init__(self, bus, index, uuid):
        self.path = f"{ROOT}/service{index}"
        self.uuid = uuid
        self.characteristics = []
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {GATT_SERVICE: {
            "UUID": self.uuid, "Primary": True,
            "Characteristics": dbus.Array([c.get_path() for c in self.characteristics],
                                          signature="o")}}

    @dbus.service.method(PROPERTIES, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_SERVICE:
            raise InvalidArgs()
        return self.get_properties()[GATT_SERVICE]


class Characteristic(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, service):
        self.path = f"{service.path}/char{index}"
        self.uuid = uuid
        self.flags = flags
        self.service = service
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {GATT_CHRC: {"Service": self.service.get_path(), "UUID": self.uuid,
                            "Flags": dbus.Array(self.flags, signature="s")}}

    @dbus.service.method(PROPERTIES, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != GATT_CHRC:
            raise InvalidArgs()
        return self.get_properties()[GATT_CHRC]

    @dbus.service.method(GATT_CHRC, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        raise NotSupported()

    @dbus.service.method(GATT_CHRC, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        raise NotSupported()

    @dbus.service.method(GATT_CHRC)
    def StartNotify(self):
        raise NotSupported()

    @dbus.service.method(GATT_CHRC)
    def StopNotify(self):
        raise NotSupported()

    @dbus.service.signal(PROPERTIES, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass


class TxCharacteristic(Characteristic):
    """Pi -> phone: reply lines as notifications, split to the link's MTU."""

    def __init__(self, bus, index, service):
        super().__init__(bus, index, carpi_control.TX_UUID, ["encrypt-notify"], service)
        self.notifying = False

    @dbus.service.method(GATT_CHRC)
    def StartNotify(self):
        self.notifying = True

    @dbus.service.method(GATT_CHRC)
    def StopNotify(self):
        self.notifying = False

    def send(self, data, mtu):
        if not self.notifying:
            return False
        for chunk in carpi_control.chunks(data, max(20, mtu - 3)):
            self.PropertiesChanged(GATT_CHRC, {"Value": dbus.Array(chunk, signature="y")}, [])
        return False                      # GLib.idle_add: run once


class RxCharacteristic(Characteristic):
    """Phone -> Pi: request lines, in chunks of any size (long writes arrive with an offset)."""

    def __init__(self, bus, index, service, controller, tx):
        super().__init__(bus, index, carpi_control.RX_UUID, ["encrypt-write"], service)
        self.controller = controller
        self.tx = tx
        self.buffers = {}                 # one per phone, so two phones can't garble each other

    @dbus.service.method(GATT_CHRC, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        device = str(options.get("device", ""))
        mtu = int(options.get("mtu", DEFAULT_MTU))
        buf = self.buffers.setdefault(device, carpi_control.LineBuffer())
        for line in buf.feed(bytes(value)):
            reply = self.controller.handle_line(line)
            GLib.idle_add(self.tx.send, reply, mtu)   # after WriteValue has returned


def ad_variants(name):
    """Advertisement layouts to try, simplest-that-still-shows-the-name first. The phone finds
    CarPi by service UUID, so the name is a nicety. Asking for "Discoverable" made the Pi 4's
    kernel reject the advertisement ("Invalid Parameters (0x0d)"), so the flags are left to it."""
    return [
        ("name in advertisement", {"LocalName": dbus.String(name)}),
        ("name in scan response", {"Includes": dbus.Array(["local-name"], signature="s")}),
        ("service UUID only", {}),
    ]


class Advertisement(dbus.service.Object):
    def __init__(self, bus, index, extra):
        self.path = f"{ROOT}/advertisement{index}"
        self.extra = extra
        super().__init__(bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {AD: dict({"Type": "peripheral",
                          "ServiceUUIDs": dbus.Array([carpi_control.SERVICE_UUID], signature="s")},
                         **self.extra)}

    @dbus.service.method(PROPERTIES, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != AD:
            raise InvalidArgs()
        return self.get_properties()[AD]

    @dbus.service.method(AD)
    def Release(self):
        log("advertisement released")


class Agent(dbus.service.Object):
    """Accepts "Just Works" pairing. That's only possible while the adapter is pairable, which
    is off except during a --pair window, so this doesn't let strangers in."""

    def __init__(self, bus):
        self.path = f"{ROOT}/agent"
        super().__init__(bus, self.path)

    @dbus.service.method(AGENT)
    def Release(self):
        pass

    @dbus.service.method(AGENT, in_signature="os")
    def AuthorizeService(self, device, uuid):
        pass

    @dbus.service.method(AGENT, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        raise Rejected("no PIN entry on CarPi")

    @dbus.service.method(AGENT, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        raise Rejected("no passkey entry on CarPi")

    @dbus.service.method(AGENT, in_signature="ouq")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(AGENT, in_signature="os")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method(AGENT, in_signature="ou")
    def RequestConfirmation(self, device, passkey):
        log(f"pairing with {device}")

    @dbus.service.method(AGENT, in_signature="o")
    def RequestAuthorization(self, device):
        log(f"pairing with {device}")

    @dbus.service.method(AGENT)
    def Cancel(self):
        pass


# ---------- setup ----------

def find_adapter(bus):
    """The first adapter that can host GATT services and advertise (hci0 on the Pi 4)."""
    om = dbus.Interface(bus.get_object(BLUEZ, "/"), OBJECT_MANAGER)
    for path, ifaces in om.GetManagedObjects().items():
        if GATT_MANAGER in ifaces and AD_MANAGER in ifaces:
            return path
    return None


def set_prop(bus, path, name, value):
    props = dbus.Interface(bus.get_object(BLUEZ, path), PROPERTIES)
    props.Set(ADAPTER, name, value)


def get_prop(bus, path, name):
    return dbus.Interface(bus.get_object(BLUEZ, path), PROPERTIES).Get(ADAPTER, name)


ADAPTER_HINT = ("check 'rfkill list', 'bluetoothctl show' and "
                "'journalctl -u bluetooth -b --no-pager | tail -20'")


def step(what, fn, *fn_args, fatal=True, hint=ADAPTER_HINT):
    """Run one setup call; on a BlueZ error say which step failed instead of a bare traceback.
    Fatal steps exit (systemd retries in 5 s); the others just log a warning."""
    try:
        fn(*fn_args)
        return True
    except dbus.exceptions.DBusException as e:
        msg = f"{what} failed: {e.get_dbus_name()}: {e.get_dbus_message()}"
        if hint:
            msg += f"\n  {hint}"
        if fatal:
            sys.exit(msg)
        log(f"warning: {msg}")
        return False


def open_pairing_window(bus, adapter, seconds):
    step("setting PairableTimeout", set_prop, bus, adapter, "PairableTimeout", dbus.UInt32(seconds))
    step("making the adapter pairable", set_prop, bus, adapter, "Pairable", dbus.Boolean(True))
    print(f"CarPi is pairable for {seconds} s. On the phone, open the config page, tap Connect,\n"
          f"pick CarPi and accept the pairing prompt.")


def main():
    ap = argparse.ArgumentParser(description="CarPi Bluetooth config service")
    ap.add_argument("--home", default="~/carpi", help="folder with presets/ and tag.txt")
    ap.add_argument("--status-file", default="/run/carpi/status.json")
    ap.add_argument("--name", default="CarPi", help="advertised Bluetooth name")
    ap.add_argument("--pair", type=int, nargs="?", const=120, metavar="SECONDS",
                    help="open a pairing window (default 120 s) and exit")
    args = ap.parse_args()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    adapter = find_adapter(bus)
    if adapter is None:
        sys.exit("no Bluetooth LE adapter found (is bluetooth.service running? try "
                 "'rfkill unblock bluetooth')")
    if args.pair:
        open_pairing_window(bus, adapter, args.pair)
        return

    log(f"using adapter {adapter}")
    if not get_prop(bus, adapter, "Powered"):
        step("powering on the adapter", set_prop, bus, adapter, "Powered", dbus.Boolean(True))
    if get_prop(bus, adapter, "Pairable"):    # the pairing gate: must be closed outside --pair
        step("turning pairing off", set_prop, bus, adapter, "Pairable", dbus.Boolean(False))
    if get_prop(bus, adapter, "Alias") != args.name:
        step(f"renaming the adapter to {args.name}", set_prop, bus, adapter, "Alias",
             dbus.String(args.name), fatal=False, hint=None)

    store = carpi_presets.Store(args.home)
    controller = carpi_control.Controller(store, args.status_file)
    app = Application(bus)
    service = Service(bus, 0, carpi_control.SERVICE_UUID)
    tx = TxCharacteristic(bus, 1, service)
    rx = RxCharacteristic(bus, 0, service, controller, tx)
    service.characteristics = [rx, tx]
    app.services.append(service)
    agent = Agent(bus)

    loop = GLib.MainLoop()

    def failed(what):
        def cb(error):
            log(f"{what} failed: {error}")
            loop.quit()                    # systemd restarts us
        return cb

    agents = dbus.Interface(bus.get_object(BLUEZ, "/org/bluez"), AGENT_MANAGER)
    pair_hint = "already-paired phones still work; new pairings may not"
    if step("registering the pairing agent", agents.RegisterAgent, agent.path, "NoInputNoOutput",
            fatal=False, hint=pair_hint):
        step("making it the default agent", agents.RequestDefaultAgent, agent.path,
             fatal=False, hint=pair_hint)
    adapter_obj = bus.get_object(BLUEZ, adapter)
    dbus.Interface(adapter_obj, GATT_MANAGER).RegisterApplication(
        app.get_path(), {}, reply_handler=lambda: log("GATT service registered"),
        error_handler=failed("RegisterApplication"))
    ad_manager = dbus.Interface(adapter_obj, AD_MANAGER)
    variants = ad_variants(args.name)
    ads = []                               # keep the D-Bus objects alive

    def advertise(i):
        label, extra = variants[i]
        ads.append(Advertisement(bus, i, extra))

        def refused(error):
            log(f"advertisement ({label}) refused: {error}")
            if i + 1 < len(variants):
                advertise(i + 1)
            else:
                failed("RegisterAdvertisement")(error)
        ad_manager.RegisterAdvertisement(
            ads[-1].get_path(), {}, reply_handler=lambda: log(f"advertising as {args.name} ({label})"),
            error_handler=refused)

    advertise(0)

    def bluez_owner(owner):
        if not owner:                      # bluetoothd restarted: our registrations are gone
            log("bluetoothd went away")
            loop.quit()
    bus.watch_name_owner(BLUEZ, bluez_owner)
    try:
        loop.run()
    finally:
        try:
            agents.UnregisterAgent(agent.path)
        except dbus.exceptions.DBusException:
            pass
    sys.exit(1)                            # only reached on failure; systemd restarts us


if __name__ == "__main__":
    main()
