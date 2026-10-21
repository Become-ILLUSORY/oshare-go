#!/usr/bin/env python3
"""Send an ATT Handle Value Notification over a raw HCI socket.

BlueZ refuses to emit notifications unless the peer wrote the CCC descriptor,
and the ColorOS client never does (setCharacteristicNotification is local only).
The link is unencrypted, so we can put the ATT PDU on the wire ourselves.

usage: raw_notify.py HANDLE HEX          # HANDLE = 0x04d1 style attribute handle
       raw_notify.py --list              # list connection handles seen
"""
import os
import struct
import sys
import time

AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCI_CHANNEL_RAW = 0
HCI_DEV = 0

H4_ACL = 0x02
ATT_HANDLE_VALUE_NTF = 0x1B
ATT_CID = 0x0004


def open_hci():
    s = socket_socket()
    s.bind((HCI_DEV,))
    return s


def socket_socket():
    import socket
    return socket.socket(AF_BLUETOOTH, socket.SOCK_RAW, BTPROTO_HCI)


def read_events(s, seconds):
    """Drain HCI events; return the LE connection complete handle if seen."""
    import socket
    s.settimeout(0.5)
    end = time.time() + seconds
    handle = None
    while time.time() < end:
        try:
            pkt = s.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not pkt or pkt[0] != 0x04 or len(pkt) < 4:
            continue
        evt = pkt[1]
        if evt == 0x3E and pkt[2] == 0x01 and len(pkt) >= 19:
            status = pkt[3]
            h = struct.unpack("<H", pkt[4:6])[0]
            atype = pkt[6]
            addr = pkt[7:13][::-1].hex(":")
            print(f"LE conn complete status={status} handle=0x{h:04x} "
                  f"addr_type={atype} addr={addr}", flush=True)
            if status == 0:
                handle = h
        elif evt == 0x05 and len(pkt) >= 7:  # Disconnection Complete
            h = struct.unpack("<H", pkt[4:6])[0]
            print(f"disconnected handle=0x{h:04x} reason={pkt[6]}", flush=True)
    return handle


def send_notification(s, handle, attr_handle, value):
    """ACL packet -> L2CAP(CID 4) -> ATT Handle Value Notification."""
    att = struct.pack("<BH", ATT_HANDLE_VALUE_NTF, attr_handle) + value
    l2cap = struct.pack("<HH", len(att), ATT_CID) + att
    acl_hdr = struct.pack("<H", (0b10 << 12) | (handle & 0x0FFF)) + \
              struct.pack("<H", len(l2cap))
    pkt = bytes([H4_ACL]) + acl_hdr + l2cap
    s.send(pkt)
    print(f"sent notification handle=0x{attr_handle:04x} len={len(value)} "
          f"({value.hex()})", flush=True)


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--list":
        s = open_hci()
        print("listening 15s for connection events (connect the phone now)")
        read_events(s, 15)
        return
    if len(sys.argv) < 3:
        print(__doc__)
        return
    attr = int(sys.argv[1], 0)
    value = bytes.fromhex(sys.argv[2])
    s = open_hci()
    print("waiting up to 30s for a connection ...", flush=True)
    handle = read_events(s, 30)
    if handle is None:
        print("no connection seen; is the phone connected?")
        return
    send_notification(s, handle, attr, value)
    time.sleep(0.3)


main()
