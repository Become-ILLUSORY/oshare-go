#!/usr/bin/env python3
"""Connect to the phone as a GATT client, read 0x9996 first, then write 0x9985.

The phone's onCharacteristicWriteRequest bails out unless the *same* device has
already read its 0x9996 (that call sets mDeviceInfoMap[addr], hasRead=true and
transferType=1). Writing 0x9985 without that read is answered with nothing at
all, which is the ATT timeout we saw.

usage: probe_peer.py [--write-hex 01] [--watch]
"""
import argparse
import time
from gi.repository import Gio, GLib

BUS = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
OM = "/"
WIFI_UUID = "00009996-0000-1000-8000-00805f9b34fb"
ACK_UUID = "00009985-0000-1000-8000-00805f9b34fb"
REQ_UUID = "00009989-0000-1000-8000-00805f9b34fb"

ap = argparse.ArgumentParser()
ap.add_argument("--write-hex", default="01")
ap.add_argument("--uuid", default=ACK_UUID)
ap.add_argument("--watch", action="store_true")
args = ap.parse_args()


def objects():
    r = BUS.call_sync("org.bluez", OM, "org.freedesktop.DBus.ObjectManager",
                      "GetManagedObjects", None,
                      GLib.VariantType.new("(a{oa{sa{sv}}})"),
                      Gio.DBusCallFlags.NONE, -1, None)
    return r.unpack()[0]


def prop(path, iface, name):
    r = BUS.call_sync("org.bluez", path, "org.freedesktop.DBus.Properties", "Get",
                      GLib.Variant("(ss)", (iface, name)),
                      GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, -1, None)
    return r.unpack()[0]


def find_phone():
    """The most recently seen OShare peer: it advertises 0x9999 and is connectable."""
    for path, ifaces in objects().items():
        if "org.bluez.Device1" not in ifaces:
            continue
        props = ifaces["org.bluez.Device1"]
        uuids = [str(u) for u in (props.get("UUIDs") or [])]
        if "00009999-0000-1000-8000-00805f9b34fb" in uuids:
            return path, str(props.get("Address")), str(props.get("Alias", ""))
    return None, None, None


def char_in(path, uuid_frag):
    for p, ifaces in sorted(objects().items()):
        if not p.startswith(path):
            continue
        c = ifaces.get("org.bluez.GattCharacteristic1")
        if c and uuid_frag in str(c.get("UUID", "")).lower():
            return p
    return None


def main():
    dev, addr, name = find_phone()
    if not dev:
        print("no OShare peer in BlueZ's object tree; scan first")
        return
    print(f"peer {name} {addr} at {dev}")
    t0 = time.time()
    try:
        BUS.call_sync("org.bluez", dev, "org.bluez.Device1", "Connect", None, None,
                      Gio.DBusCallFlags.NONE, 25000, None)
        print(f"connected in {time.time() - t0:.1f}s")
    except GLib.Error as e:
        print("connect failed:", e.message)

    resolved = False
    for _ in range(40):
        try:
            if prop(dev, "org.bluez.Device1", "ServicesResolved"):
                resolved = True
                break
        except GLib.Error:
            pass
        time.sleep(0.3)
    print("services resolved:", resolved)

    wifi = char_in(dev, "9996")
    print("0x9996:", wifi)
    if wifi:
        try:
            r = BUS.call_sync("org.bluez", wifi, "org.bluez.GattCharacteristic1",
                              "ReadValue", GLib.Variant("(a{sv})", ({},)),
                              GLib.VariantType.new("(ay)"), Gio.DBusCallFlags.NONE, 15000, None)
            print("0x9996 read ->", bytes(r.unpack()[0]).hex())
        except GLib.Error as e:
            print("0x9996 read failed:", e.message)

    target = char_in(dev, args.uuid[-12:-8])
    print(f"{args.uuid}:", target)
    if target:
        payload = bytes.fromhex(args.write_hex)
        try:
            BUS.call_sync("org.bluez", target, "org.bluez.GattCharacteristic1",
                          "WriteValue", GLib.Variant("(aya{sv})", (payload, {})),
                          None, Gio.DBusCallFlags.NONE, 15000, None)
            print("write ok:", payload.hex())
        except GLib.Error as e:
            print("write failed:", e.message)

    if args.watch:
        print("watching for 60s (Ctrl-C to stop)")
        time.sleep(60)


main()
