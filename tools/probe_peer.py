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
ap.add_argument("--match", default=None, help="address or name fragment of the peer")
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


SHARE_SVC = "00009999-0000-1000-8000-00805f9b34fb"


def find_phone(want=None):
    """Pick the peer: an explicit address, else the one exposing service 0x9999."""
    cands = []
    for path, ifaces in objects().items():
        if "org.bluez.Device1" not in ifaces:
            continue
        props = ifaces["org.bluez.Device1"]
        addr = str(props.get("Address", ""))
        alias = str(props.get("Alias", props.get("Name", "")))
        if want and want.lower() not in (addr + " " + alias).lower():
            continue
        uuids = [str(u).lower() for u in (props.get("UUIDs") or [])]
        has_svc = any(SHARE_SVC in u for u in uuids) or any(
            "00006666" in u or "00008881" in u for u in uuids)
        if not has_svc:
            pass
        if not has_svc:
            for p2, i2 in objects().items():
                if p2.startswith(path) and "org.bluez.GattService1" in i2:
                    if SHARE_SVC in str(i2["org.bluez.GattService1"].get("UUID", "")):
                        has_svc = True
                        break
        cands.append((has_svc, path, addr, alias))
    if not cands:
        return None, None, None
    cands.sort(key=lambda c: (not c[0], c[2]))
    _, path, addr, alias = cands[0]
    return path, addr, alias


def char_in(dev_path, uuid):
    """Exact match, preferring the characteristic inside service 0x9999."""
    ui = uuid.lower()
    found = []
    svc = {}
    for p, ifaces in objects().items():
        if not p.startswith(dev_path):
            continue
        s1 = ifaces.get("org.bluez.GattService1")
        if s1:
            svc[p] = str(s1.get("UUID", "")).lower()
        c = ifaces.get("org.bluez.GattCharacteristic1")
        if c and str(c.get("UUID", "")).lower() == ui:
            found.append(p)
    found.sort(key=lambda p: not p.startswith(dev_path + "/service00c2"))
    for p in found:
        s2 = svc.get(p.rsplit("/", 1)[0], "")
        print(f"  candidate {p} in service {s2 or '?'}")
    return found[0] if found else None


def main():
    dev, addr, name = find_phone(args.match)
    if not dev:
        print("no peer found in BlueZ's object tree (pass --match ADDR); scan first")
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

    wifi = char_in(dev, WIFI_UUID)
    print("0x9996:", wifi)
    if wifi:
        try:
            r = BUS.call_sync("org.bluez", wifi, "org.bluez.GattCharacteristic1",
                              "ReadValue", GLib.Variant("(a{sv})", ({},)),
                              GLib.VariantType.new("(ay)"), Gio.DBusCallFlags.NONE, 15000, None)
            print("0x9996 read ->", bytes(r.unpack()[0]).hex())
        except GLib.Error as e:
            print("0x9996 read failed:", e.message)

    target = char_in(dev, args.uuid)
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
