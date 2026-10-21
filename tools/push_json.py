#!/usr/bin/env python3
"""Push our AP/port JSON to the phone over 0x9953 (write-only, no notifications).

ble/impl/w.java: onCharacteristicWriteRequest gates the common-share branch on
transferType == 2, which is set by *reading* 0x9954 first:

    onCharacteristicReadRequest(0x9954) -> info.c(true); info.d(2)
    onCharacteristicWriteRequest(0x9953) -> accumulate until valid JSON ->
        OShareServer.ServerCallback.d(json) -> gb.a.d().f(port)  (phone dials us)

usage: push_json.py [--match ADDR] [--port 50856] [--ssid S] [--psk P]
"""
import argparse
import json
import time

from gi.repository import Gio, GLib

BUS = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
CHR = "org.bluez.GattCharacteristic1"
SHARE_SVC = "00009999-0000-1000-8000-00805f9b34fb"
OTHER_BAND = "00009954-0000-1000-8000-00805f9b34fb"   # k9.c.K, sets transferType=2
COMMON_SHARE = "00009953-0000-1000-8000-00805f9b34fb"  # k9.c.L, takes the JSON

ap = argparse.ArgumentParser()
ap.add_argument("--match", default=None)
ap.add_argument("--port", type=int, default=50856)
ap.add_argument("--freq", type=int, default=2437)
ap.add_argument("--mac", default="4cd577f02f01")
ap.add_argument("--ssid", default="DIRECT-aa-fastCon")
ap.add_argument("--psk", default="")
ap.add_argument("--key", default="")
args = ap.parse_args()


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def objects():
    r = BUS.call_sync("org.bluez", "/", "org.freedesktop.DBus.ObjectManager",
                      "GetManagedObjects", None,
                      GLib.VariantType.new("(a{oa{sa{sv}}})"),
                      Gio.DBusCallFlags.NONE, -1, None)
    return r.unpack()[0]


def find_peer(want=None):
    """The phone may hide its UUID list after a reconnect, so fall back to a
    device that is either already connected or whose name we were told."""
    best = None
    for path, ifaces in objects().items():
        d = ifaces.get("org.bluez.Device1")
        if not d:
            continue
        addr = str(d.get("Address", ""))
        alias = str(d.get("Alias", d.get("Name", "")) or "")
        if want and want.lower() not in (addr + " " + alias).lower():
            continue
        uuids = [str(u).lower() for u in (d.get("UUIDs") or [])]
        if any("6666" in u or "8881" in u for u in uuids):
            return path, addr, alias
        if best is None:
            best = (path, addr, alias)
    return best if best else (None, None, None)


def char_of(dev, uuid):
    svc = {}
    hit = []
    for p, ifaces in objects().items():
        if not p.startswith(dev):
            continue
        s = ifaces.get("org.bluez.GattService1")
        if s:
            svc[p] = str(s.get("UUID", "")).lower()
        c = ifaces.get(CHR)
        if c and str(c.get("UUID", "")).lower() == uuid:
            hit.append((p, svc.get(p.rsplit("/", 1)[0], "")))
    hit.sort(key=lambda h: h[1] != SHARE_SVC)
    return hit[0][0] if hit else None


def read(path):
    r = BUS.call_sync("org.bluez", path, CHR, "ReadValue",
                      GLib.Variant("(a{sv})", ({},)),
                      GLib.VariantType.new("(ay)"),
                      Gio.DBusCallFlags.NONE, 15000, None)
    return bytes(r.unpack()[0])


def write(path, data):
    BUS.call_sync("org.bluez", path, CHR, "WriteValue",
                  GLib.Variant("(aya{sv})", (data, {})), None,
                  Gio.DBusCallFlags.NONE, 20000, None)


def main():
    dev, addr, alias = find_peer(args.match)
    if not dev:
        log("peer not found; open 互传 on the phone")
        return
    log(f"peer {alias} {addr}")
    try:
        BUS.call_sync("org.bluez", dev, "org.bluez.Device1", "Connect", None, None,
                      Gio.DBusCallFlags.NONE, 25000, None)
        log("connected")
    except GLib.Error as e:
        log("connect:", e.message)
    for _ in range(40):
        try:
            r = BUS.call_sync("org.bluez", dev, "org.freedesktop.DBus.Properties",
                              "Get", GLib.Variant("(ss)", ("org.bluez.Device1",
                                                           "ServicesResolved")),
                              GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, -1, None)
            if r.unpack()[0]:
                break
        except GLib.Error:
            pass
        time.sleep(0.3)

    band = char_of(dev, OTHER_BAND)
    share = char_of(dev, COMMON_SHARE)
    log(f"0x9954 {band}")
    log(f"0x9953 {share}")
    if not band or not share:
        log("the phone does not expose the common-share service in this state")
        return

    log("reading 0x9954 (sets transferType=2 on the phone)")
    try:
        val = read(band)
        log(f"0x9954 -> {len(val)}B {val.hex()}")
    except GLib.Error as e:
        log("read failed:", e.message)

    payload = json.dumps({
        "port": args.port,
        "freq": args.freq,
        "mac": args.mac,
        "ssid": args.ssid,
        "psk": args.psk,
        "key": args.key,
    }, separators=(",", ":")).encode()
    log(f"writing 0x9953: {payload.decode()}")
    try:
        write(share, payload)
        log("write accepted")
    except GLib.Error as e:
        log("write failed:", e.message)


main()
