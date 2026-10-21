#!/usr/bin/env python3
"""Connect to the phone and subscribe to the notifications it sends us.

In the "phone sends, we receive" direction the phone is the GATT *server* and
the GO: it announces the group on 0x9989 (REQUEST_GO) and the AP credentials
on 0x9985 (BLE_ACK_DIALOG, Base64 JSON). Neither has a standard 0x2902 in the
phone's tree - OShare attaches its own descriptors (0x9988 for 0x9989, none for
0x9985) - so this tries, in order:

  1. StartNotify on the characteristic,
  2. write 0x2902 / the custom descriptor to 0x0001, then StartNotify again,
  3. plain GATT client notify registration via AcquireNotify.

Everything that changes on a subscribed characteristic is printed, so a
notification shows up as a `[notify]` line with the raw bytes.

usage: watch_notify.py [--match ADDR] [--seconds 120]
"""
import argparse
import time

from gi.repository import Gio, GLib

BUS = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
CHR_IFACE = "org.bluez.GattCharacteristic1"
DESC_IFACE = "org.bluez.GattDescriptor1"
SHARE_SVC = "00009999-0000-1000-8000-00805f9b34fb"

WATCH = {
    "00009989-0000-1000-8000-00805f9b34fb": "REQUEST_GO",
    "00009985-0000-1000-8000-00805f9b34fb": "ACK_DIALOG",
    "00009997-0000-1000-8000-00805f9b34fb": "UPDATE",
    "00009984-0000-1000-8000-00805f9b34fb": "WPS_RESULT",
}

ap = argparse.ArgumentParser()
ap.add_argument("--match", default=None)
ap.add_argument("--seconds", type=int, default=120)
ap.add_argument("--read-wifi", action="store_true",
                help="read the phone's 0x9996 once connected")
args = ap.parse_args()


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def objects():
    r = BUS.call_sync("org.bluez", "/", OM_IFACE, "GetManagedObjects", None,
                      GLib.VariantType.new("(a{oa{sa{sv}}})"),
                      Gio.DBusCallFlags.NONE, -1, None)
    return r.unpack()[0]


def prop(path, iface, name):
    r = BUS.call_sync("org.bluez", path, PROPS_IFACE, "Get",
                      GLib.Variant("(ss)", (iface, name)),
                      GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, -1, None)
    return r.unpack()[0]


ADAPTER = "/org/bluez/hci0"


def discover(seconds=10):
    """Run an LE scan so the phone (random address) shows up in the tree."""
    try:
        BUS.call_sync("org.bluez", ADAPTER, "org.bluez.Adapter1", "SetDiscoveryFilter",
                      GLib.Variant("(a{sv})", ({
                          "Transport": GLib.Variant("s", "le"),
                          "DuplicateData": GLib.Variant("b", True),
                      },)), None, Gio.DBusCallFlags.NONE, 5000, None)
    except GLib.Error:
        pass
    try:
        BUS.call_sync("org.bluez", ADAPTER, "org.bluez.Adapter1", "StartDiscovery",
                      None, None, Gio.DBusCallFlags.NONE, 5000, None)
    except GLib.Error as e:
        log("discovery:", e.message)
        return
    time.sleep(seconds)
    try:
        BUS.call_sync("org.bluez", ADAPTER, "org.bluez.Adapter1", "StopDiscovery",
                      None, None, Gio.DBusCallFlags.NONE, 5000, None)
    except GLib.Error:
        pass


def find_phone(want=None):
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
        share = any("6666" in u or "6667" in u or "88881" in u or "8881" in u
                    for u in uuids)
        best = (share, path, addr, alias)
        if share:
            break
    if not best:
        return None, None, None
    return best[1], best[2], best[3]


def tree(dev):
    """characteristics and descriptors belonging to dev, grouped by service."""
    chars, descs, svcs = {}, {}, {}
    for p, ifaces in objects().items():
        if not p.startswith(dev):
            continue
        s = ifaces.get("org.bluez.GattService1")
        if s:
            svcs[p] = str(s.get("UUID", "")).lower()
        c = ifaces.get(CHR_IFACE)
        if c:
            chars[p] = (str(c.get("UUID", "")).lower(), str(c.get("Service", "")),
                        [str(f) for f in (c.get("Flags") or [])])
        d = ifaces.get(DESC_IFACE)
        if d:
            descs[p] = (str(d.get("UUID", "")).lower(), str(d.get("Characteristic", "")))
    return chars, descs, svcs


def on_value_changed(conn, sender, path, iface, signal, params):
    iname, changed, _inv = params.unpack()
    if iname != CHR_IFACE or "Value" not in changed:
        return
    raw = bytes(changed["Value"])
    log(f"[notify] {path.rsplit('/', 1)[-1]} {len(raw)}B {raw.hex()}")
    try:
        text = raw.decode()
        if text.isprintable():
            log(f"[notify]   text {text!r}")
            import base64
            try:
                dec = base64.b64decode(text, validate=True).decode()
                log(f"[notify]   base64 -> {dec!r}")
            except Exception:
                pass
    except UnicodeDecodeError:
        pass


def start_notify(path):
    try:
        BUS.call_sync("org.bluez", path, CHR_IFACE, "StartNotify", None, None,
                      Gio.DBusCallFlags.NONE, 8000, None)
        log(f"[sub] StartNotify ok    {path.rsplit('/', 1)[-1]}")
        return True
    except GLib.Error as e:
        log(f"[sub] StartNotify failed {path.rsplit('/', 1)[-1]}: {e.message}")
        return False


def write_desc(path, value=b"\x01\x00"):
    try:
        BUS.call_sync("org.bluez", path, DESC_IFACE, "WriteValue",
                      GLib.Variant("(aya{sv})", (value, {})), None,
                      Gio.DBusCallFlags.NONE, 8000, None)
        log(f"[sub] wrote {value.hex()} to {path.rsplit('/', 1)[-1]}")
        return True
    except GLib.Error as e:
        log(f"[sub] desc write failed {path.rsplit('/', 1)[-1]}: {e.message}")
        return False


def main():
    dev, addr, alias = find_phone(args.match)
    if not dev:
        log("scanning for the peer ...")
        discover(12)
        dev, addr, alias = find_phone(args.match)
    if not dev:
        log("no peer found; open 互传 on the phone first")
        return
    log(f"peer {alias} {addr} at {dev}")
    try:
        BUS.call_sync("org.bluez", dev, "org.bluez.Device1", "Connect", None, None,
                      Gio.DBusCallFlags.NONE, 25000, None)
        log("connected")
    except GLib.Error as e:
        log("connect:", e.message)
    for _ in range(40):
        try:
            if prop(dev, "org.bluez.Device1", "ServicesResolved"):
                break
        except GLib.Error:
            pass
        time.sleep(0.3)

    chars, descs, svcs = tree(dev)
    log(f"peer exposes {len(chars)} characteristics in {len(svcs)} services")

    BUS.signal_subscribe("org.bluez", PROPS_IFACE, "PropertiesChanged", None,
                              None, Gio.DBusSignalFlags.NONE, on_value_changed)

    for path, (uuid, svc_path, flags) in sorted(chars.items()):
        name = WATCH.get(uuid)
        if not name:
            continue
        svc_uuid = svcs.get(svc_path, "?")
        if svc_uuid != SHARE_SVC:
            log(f"[sub] skip {name} in {svc_uuid} (not the share service)")
            continue
        log(f"[sub] {name} {uuid} flags={flags}")
        if start_notify(path):
            continue
        # Try the descriptors the phone attached to this characteristic; OShare
        # uses its own UUIDs there instead of 0x2902.
        for dpath, (duuid, dchr) in descs.items():
            if dchr == path:
                log(f"[sub]   descriptor {duuid}")
                write_desc(dpath)
        start_notify(path)

    if args.read_wifi:
        for path, (uuid, svc_path, flags) in sorted(chars.items()):
            if uuid.startswith("00009996") and svcs.get(svc_path) == SHARE_SVC:
                try:
                    r = BUS.call_sync("org.bluez", path, CHR_IFACE, "ReadValue",
                                      GLib.Variant("(a{sv})", ({},)),
                                      GLib.VariantType.new("(ay)"), Gio.DBusCallFlags.NONE,
                                      15000, None)
                    log("0x9996 ->", bytes(r.unpack()[0]).decode(errors="replace"))
                except GLib.Error as e:
                    log("0x9996 read failed:", e.message)

    log(f"watching for {args.seconds}s — tap the phone's 发送 page now")
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(args.seconds, lambda: (loop.quit(), False)[1])
    loop.run()
    log("done")


main()
