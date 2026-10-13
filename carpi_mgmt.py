"""
carpi_mgmt.py - add or remove an LE advertisement through the Linux kernel's Bluetooth management
socket (the channel btmgmt uses), for when BlueZ's D-Bus advertising API is refused.

Standard library only. Talking to the control channel needs CAP_NET_ADMIN; without it the kernel
answers "Permission Denied". Packet layouts: Documentation in BlueZ, doc/mgmt-api.txt.
"""
import ctypes
import os
import select
import socket
import struct
import time
import uuid

HCI_DEV_NONE = 0xFFFF
HCI_CHANNEL_CONTROL = 3
OP_ADD_ADVERTISING = 0x003E
OP_REMOVE_ADVERTISING = 0x003F
EV_CMD_COMPLETE = 0x0001
EV_CMD_STATUS = 0x0002

# Add Advertising flags
CONNECTABLE = 1 << 0
GENERAL_DISCOVERABLE = 1 << 1      # the kernel writes the AD flags field
LOCAL_NAME = 1 << 6                # the kernel puts the adapter name in the scan response

# AD "Flags" field bits, for when we write that field ourselves
AD_GENERAL_DISCOVERABLE = 0x02
AD_NO_BREDR = 0x04                 # "LE only": the phone pairs over LE instead of classic Bluetooth

STATUS = {0x00: "Success", 0x01: "Unknown Command", 0x03: "Failed", 0x0A: "Busy",
          0x0B: "Rejected", 0x0C: "Not Supported", 0x0D: "Invalid Parameters",
          0x0F: "Not Powered", 0x11: "Invalid Index", 0x14: "Permission Denied"}


class MgmtError(RuntimeError):
    """The kernel refused a command, or couldn't be reached."""


class _SockaddrHci(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("dev", ctypes.c_ushort), ("channel", ctypes.c_ushort)]


def flags_ad(bits):
    """AD structure "Flags" (type 0x01). Only valid when the kernel isn't managing the flags,
    i.e. without GENERAL_DISCOVERABLE in the Add Advertising flags."""
    return bytes([2, 0x01, bits])


def uuid128_ad(uuid_str):
    """AD structure "Complete List of 128-bit Service UUIDs" (type 0x07), UUID little-endian."""
    return bytes([17, 0x07]) + uuid.UUID(uuid_str).bytes[::-1]


def add_advertising(instance, flags, adv_data, scan_rsp=b""):
    """(opcode, params) for Add Advertising: no duration or timeout, i.e. until removed."""
    params = struct.pack("<BIHHBB", instance, flags, 0, 0, len(adv_data), len(scan_rsp))
    return OP_ADD_ADVERTISING, params + adv_data + scan_rsp


def remove_advertising(instance):
    return OP_REMOVE_ADVERTISING, struct.pack("<B", instance)


def packet(opcode, index, params):
    return struct.pack("<HHH", opcode, index, len(params)) + params


def reply_status(pkt, opcode, index):
    """Status byte if pkt is the Command Complete / Command Status for our command, else None."""
    if len(pkt) < 9:
        return None
    event, idx, _ = struct.unpack_from("<HHH", pkt)
    op, status = struct.unpack_from("<HB", pkt, 6)
    if event in (EV_CMD_COMPLETE, EV_CMD_STATUS) and idx == index and op == opcode:
        return status
    return None


def send(index, command, timeout=3.0):
    """Send one (opcode, params) command to adapter hci<index>; raise MgmtError unless it succeeds."""
    opcode, params = command
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    try:
        # Python's bind() can't pick an HCI channel, so bind through libc
        addr = _SockaddrHci(socket.AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_CONTROL)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.bind(s.fileno(), ctypes.byref(addr), ctypes.sizeof(addr)) != 0:
            err = ctypes.get_errno()
            raise MgmtError(f"can't open the Bluetooth management socket: {os.strerror(err)}")
        s.send(packet(opcode, index, params))
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            if not select.select([s], [], [], left)[0]:
                break
            status = reply_status(s.recv(1024), opcode, index)   # other events are skipped
            if status is None:
                continue
            if status:
                raise MgmtError(f"{STATUS.get(status, 'error')} (0x{status:02x})")
            return
        raise MgmtError("no reply from the kernel")
    finally:
        s.close()

