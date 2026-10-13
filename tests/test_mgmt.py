import struct

import carpi_control
import carpi_mgmt as m


def test_uuid_ad_is_little_endian():
    ad = m.uuid128_ad(carpi_control.SERVICE_UUID)
    assert len(ad) == 18 and ad[:2] == bytes([17, 0x07])
    assert ad[2:] == bytes.fromhex("6c2a00014f3b4d1e9a573c8e2b7d5f10")[::-1]


def test_add_advertising_packet():
    adv = m.uuid128_ad(carpi_control.SERVICE_UUID)
    opcode, params = m.add_advertising(1, m.CONNECTABLE | m.GENERAL_DISCOVERABLE | m.LOCAL_NAME, adv)
    pkt = m.packet(opcode, 0, params)
    assert struct.unpack_from("<HHH", pkt) == (0x003E, 0, 11 + 18)
    instance, flags, duration, timeout, adv_len, rsp_len = struct.unpack_from("<BIHHBB", pkt, 6)
    assert (instance, flags, duration, timeout, adv_len, rsp_len) == (1, 0x43, 0, 0, 18, 0)
    assert pkt[17:] == adv


def test_le_only_advertisement_fits():
    adv = m.flags_ad(m.AD_GENERAL_DISCOVERABLE | m.AD_NO_BREDR) + m.uuid128_ad(carpi_control.SERVICE_UUID)
    assert adv[:3] == bytes([2, 0x01, 0x06]) and len(adv) == 21 <= 31
    _, params = m.add_advertising(1, m.CONNECTABLE | m.LOCAL_NAME, adv)
    assert params[1] == 0x41 and params[1] & m.GENERAL_DISCOVERABLE == 0   # kernel not managing flags


def test_reply_status_matches_only_our_command():
    complete = struct.pack("<HHHHB", m.EV_CMD_COMPLETE, 0, 4, m.OP_ADD_ADVERTISING, 0x00) + b"\x01"
    status = struct.pack("<HHHHB", m.EV_CMD_STATUS, 0, 3, m.OP_ADD_ADVERTISING, 0x0D)
    other = struct.pack("<HHHHB", m.EV_CMD_COMPLETE, 0, 3, 0x0004, 0x00)
    assert m.reply_status(complete, m.OP_ADD_ADVERTISING, 0) == 0
    assert m.reply_status(status, m.OP_ADD_ADVERTISING, 0) == 0x0D
    assert m.reply_status(other, m.OP_ADD_ADVERTISING, 0) is None
    assert m.reply_status(complete, m.OP_ADD_ADVERTISING, 1) is None
    assert m.reply_status(b"\x01\x00", m.OP_ADD_ADVERTISING, 0) is None
