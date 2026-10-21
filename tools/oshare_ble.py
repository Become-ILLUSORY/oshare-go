#!/usr/bin/env python3
"""oshare_ble.py - one-stop BLE tool for OShare (ColorOS 互传) work on Linux.

Three modes:

  scan                     dump BLE advertisements (highlights OShare ones)
  adv                      advertise this machine as a share peer
  serve                    advertise + host the OShare GATT server, dump
                           everything the phone reads/writes

Typical session:

  sudo python3 tools/oshare_ble.py scan           # see the phone's own advert
  sudo python3 tools/oshare_ble.py serve          # let the phone find us
  # ... on the phone: 互传 -> 发送页 -> 其他设备 -> 点 "LinuxBox" ...

Dependencies (CachyOS):
  sudo pacman -S bluez bluez-utils python-dbus python-gobject

Protocol notes live in docs/BLE.md.
"""

import argparse
import json
import os
import signal
import sys
import time

try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib
except ImportError:  # pragma: no cover
    sys.exit("missing deps: sudo pacman -S python-dbus python-gobject")

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
LE_ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
GATT_MANAGER = "org.bluez.GattManager1"
LE_ADV_IFACE = "org.bluez.LEAdvertisement1"

UUID_OS_MAIN = "00008881-0000-1000-8000-00805f9b34fb"   # advertised service uuid
UUID_SD_ADV = "00006666-0000-1000-8000-00805f9b34fb"    # adv payload
UUID_SD_RSP = "00006667-0000-1000-8000-00805f9b34fb"    # scan response payload
UUID_SVC_SHARE = "00009999-0000-1000-8000-00805f9b34fb"
UUID_SVC_COMMON = "00009955-0000-1000-8000-00805f9b34fb"

# characteristic uuid -> (flags, name)
CHARS_SHARE = {
    "00009989-0000-1000-8000-00805f9b34fb": (["read", "notify"], "REQUEST_GO"),
    "00009985-0000-1000-8000-00805f9b34fb": (["read", "write", "notify"], "ACK_DIALOG"),
    "00009997-0000-1000-8000-00805f9b34fb": (["read", "notify"], "UPDATE"),
    "00009986-0000-1000-8000-00805f9b34fb": (["read", "write", "write-without-response"], "START_WPS_INFO"),
    "00009984-0000-1000-8000-00805f9b34fb": (["read", "notify"], "LOG"),
    "00009895-0000-1000-8000-00805f9b34fb": (["read", "write", "notify"], "IOS_TIMEOUT"),
}
CHARS_COMMON = {
    "00009953-0000-1000-8000-00805f9b34fb": (["read", "write", "write-without-response"], "PORT_FREQUENCY"),
    "00009954-0000-1000-8000-00805f9b34fb": (["read", "notify"], "COMMON_1"),
    "00009955-0000-1000-8000-00805f9b34fb": (["read", "notify"], "COMMON_2"),
}


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def hexdump(b: bytes, width: int = 32) -> str:
    if not b:
        return ""
    out = []
    for i in range(0, len(b), width):
        chunk = b[i:i + width]
        out.append("    " + chunk.hex(" ") + "   " + "".join(
            chr(c) if 32 <= c < 127 else "." for c in chunk))
    return "\n".join(out)


def pad(b: bytes, n: int) -> bytes:
    return b[:n] + b"\x00" * max(0, n - len(b))


def build_service_data(name: str, device_id: bytes, adv_flags: int,
                       rsp_flags: int = 1, account_id: bytes = b"\x00\x00\x00",
                       account_name: str = "") -> dict:
    """0x6666 = [17][name 16][id 6][flags]; 0x6667 = [flags][acc 3][name 16]."""
    adv = bytes([17]) + pad(name.encode(), 16) + pad(device_id, 6) + bytes([adv_flags & 0xFF])
    rsp = bytes([rsp_flags & 0xFF]) + pad(account_id, 3) + pad(account_name.encode(), 16)
    return {UUID_SD_ADV: adv, UUID_SD_RSP: rsp}


def get_adapter(bus, want=None):
    om = dbus.Interface(bus.get_object(BLUEZ, "/"),
                        "org.freedesktop.DBus.ObjectManager")
    for path, ifaces in om.GetManagedObjects().items():
        if ADAPTER_IFACE in ifaces and (not want or path.endswith("/" + want)):
            return path, ifaces
    return None, None


# --------------------------------------------------------------------------
# advertising
# --------------------------------------------------------------------------
class Advertisement(dbus.service.Object):
    PATH = "/org/oshare/adv0"

    def __init__(self, bus, name, service_data):
        self.name = name
        self.service_data = service_data
        super().__init__(bus, self.PATH)

    def props(self):
        p = {
            "Type": dbus.String("peripheral"),
            "ServiceUUIDs": dbus.Array([UUID_OS_MAIN], signature="s"),
            "LocalName": dbus.String(self.name),
            "IncludeTxPower": dbus.Boolean(False),
        }
        if self.service_data:
            p["ServiceData"] = dbus.Dictionary(
                {dbus.String(k): dbus.Array(dbus.ByteArray(v), signature="y")
                 for k, v in self.service_data.items()}, signature="sv")
        return {LE_ADV_IFACE: p}

    @dbus.service.method(LE_ADV_IFACE, in_signature="", out_signature="")
    def Release(self):
        log("[adv] released by bluez")

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self.props()[iface][prop]

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="", out_signature="a{sv}")
    def GetAll(self, iface):
        return self.props()[iface]


# --------------------------------------------------------------------------
# GATT server
# --------------------------------------------------------------------------
class Characteristic(dbus.service.Object):
    def __init__(self, bus, index, uuid, flags, name, service_path, on_write=None):
        self.path = f"/org/oshare/gatt/char{index}"
        self.uuid = uuid
        self.flags = flags
        self.name = name
        self.service_path = service_path
        self.value = b""
        self.notifying = False
        self.on_write = on_write
        super().__init__(bus, self.path)

    def props(self):
        p = {
            "UUID": dbus.String(self.uuid),
            "Service": dbus.ObjectPath(self.service_path),
            "Flags": dbus.Array(self.flags, signature="s"),
        }
        if self.notifying:
            p["Notifying"] = dbus.Boolean(True)
        return {"org.bluez.GattCharacteristic1": p}

    @dbus.service.method("org.bluez.GattCharacteristic1",
                         in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        log(f"[gatt] read  {self.name} -> {self.value.hex()}")
        return dbus.Array(self.value, signature="y")

    @dbus.service.method("org.bluez.GattCharacteristic1",
                         in_signature="aya{sv}", out_signature="")
    def WriteValue(self, value, options):
        data = bytes(value)
        self.value = data
        log(f"[gatt] write {self.name} ({len(data)} bytes)")
        print(hexdump(data), flush=True)
        try:
            text = data.decode("utf-8")
            if text.isprintable():
                log(f"[gatt]   text: {text!r}")
                parse_payload(self.name, text)
        except UnicodeDecodeError:
            pass
        if self.on_write:
            self.on_write(self.name, data)

    @dbus.service.method("org.bluez.GattCharacteristic1",
                         in_signature="a{sv}", out_signature="")
    def StartNotify(self, options):
        self.notifying = True
        log(f"[gatt] notify on  {self.name}")

    @dbus.service.method("org.bluez.GattCharacteristic1",
                         in_signature="a{sv}", out_signature="")
    def StopNotify(self, options):
        self.notifying = False
        log(f"[gatt] notify off {self.name}")

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self.props()[iface][prop]

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="", out_signature="a{sv}")
    def GetAll(self, iface):
        return self.props()[iface]

    @dbus.service.signal("org.freedesktop.DBus.Properties", signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass

    def notify(self, data: bytes):
        if not self.notifying:
            log(f"[gatt] {self.name} not subscribed, cannot notify")
            return False
        self.value = data
        self.PropertiesChanged(
            "org.bluez.GattCharacteristic1",
            {"Value": dbus.Array(dbus.ByteArray(data), signature="y")}, [])
        log(f"[gatt] notified {self.name} {data.hex()}")
        return True


class Service(dbus.service.Object):
    def __init__(self, bus, path, uuid, chars):
        self.path = path
        self.uuid = uuid
        self.chars = chars
        super().__init__(bus, path)

    def props(self):
        return {"org.bluez.GattService1": {
            "UUID": dbus.String(self.uuid),
            "Primary": dbus.Boolean(True),
            "Characteristics": dbus.Array(
                [dbus.ObjectPath(c.path) for c in self.chars], signature="o"),
        }}

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self.props()[iface][prop]

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="", out_signature="a{sv}")
    def GetAll(self, iface):
        return self.props()[iface]


class Application(dbus.service.Object):
    def __init__(self, bus, services):
        self.path = "/org/oshare/gatt"
        self.services = services
        super().__init__(bus, self.path)

    @dbus.service.method("org.freedesktop.DBus.ObjectManager",
                         out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        out = {}
        for svc in self.services:
            out[dbus.ObjectPath(svc.path)] = svc.props()
            for ch in svc.chars:
                out[dbus.ObjectPath(ch.path)] = ch.props()
        return out


def parse_payload(name, text):
    """Best-effort decode of the two payloads the phone writes."""
    if name == "START_WPS_INFO":
        parts = text.split("&")
        log(f"[wps] fields ({len(parts)}): {parts}")
        if len(parts) >= 5:
            log(f"[wps] mac={parts[0]} band={parts[1]} type={parts[2]}")
            log(f"[wps] ssid={parts[3]!r} psk={parts[4]!r}")
    elif name == "PORT_FREQUENCY":
        try:
            obj = json.loads(text)
            log(f"[port] {obj}")
        except json.JSONDecodeError:
            pass


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------
def mode_scan(bus, adapter_path, seconds, watch):
    adapter = dbus.Interface(bus.get_object(BLUEZ, adapter_path), ADAPTER_IFACE)
    om = dbus.Interface(bus.get_object(BLUEZ, "/"),
                        "org.freedesktop.DBus.ObjectManager")
    seen = {}

    def dump(path, props):
        addr = str(props.get("Address", "?"))
        name = str(props.get("Name", "")) or str(props.get("Alias", ""))
        sd = props.get("ServiceData") or {}
        rssi = props.get("RSSI", "")
        uuids = [str(u) for u in props.get("UUIDs", [])]
        interesting = any("6666" in u or "6667" in u or "8881" in u for u in uuids) or sd
        key = (addr, name, tuple(sorted(uuids)), rssi)
        if key in seen:
            return
        seen[key] = True
        mark = "  <== OSHARE?" if interesting else ""
        log(f"{addr} rssi={rssi} name={name!r} uuid={uuids}{mark}")
        for uuid, val in sd.items():
            raw = bytes(val)
            print(f"    sd {uuid}: {raw.hex()}", flush=True)
        md = props.get("ManufacturerData") or {}
        for k, v in md.items():
            print(f"    mfr 0x{int(k):04x}: {bytes(v).hex()}", flush=True)

    for path, ifaces in om.GetManagedObjects().items():
        if "org.bluez.Device1" in ifaces:
            dump(path, ifaces["org.bluez.Device1"])
    if watch:
        bus.add_signal_receiver(
            lambda path, ifaces: [dump(p, v) for i, v in ifaces.items()
                                  if i == "org.bluez.Device1"],
            dbus_interface="org.freedesktop.DBus.ObjectManager",
            signal_name="InterfacesAdded")
        bus.add_signal_receiver(
            lambda path, iface, props, inv: dump(path, props) if iface == "org.bluez.Device1" else None,
            dbus_interface="org.freedesktop.DBus.Properties",
            signal_name="PropertiesChanged")

    log(f"[scan] discovering for {seconds}s ...")
    try:
        adapter.SetDiscoveryFilter({"Transport": dbus.String("le"),
                                    "DuplicateData": dbus.Boolean(True)})
    except dbus.DBusException:
        pass
    adapter.StartDiscovery()

    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(seconds, loop.quit)
    signal.signal(signal.SIGINT, lambda *_: loop.quit())
    loop.run()
    try:
        adapter.StopDiscovery()
    except dbus.DBusException:
        pass
    log("[scan] done")


def mode_serve(bus, adapter_path, name, adv_flags, rsp_flags, account, with_gatt=True):
    adapter = dbus.Interface(bus.get_object(BLUEZ, adapter_path), ADAPTER_IFACE)
    try:
        adapter.SetPowered(True)
    except dbus.DBusException as e:
        log("[adv] SetPowered:", e)

    device_id = os.urandom(6)
    sd = build_service_data(name, device_id, adv_flags, rsp_flags, account_name=account)

    # GATT application
    svc0_path = "/org/oshare/gatt/svc0"
    svc1_path = "/org/oshare/gatt/svc1"
    idx = 0
    share_chars = []
    for uuid, (flags, cname) in CHARS_SHARE.items():
        share_chars.append(Characteristic(bus, idx, uuid, flags, cname, svc0_path))
        idx += 1
    common_chars = []
    for uuid, (flags, cname) in CHARS_COMMON.items():
        common_chars.append(Characteristic(bus, idx, uuid, flags, cname, svc1_path))
        idx += 1

    svc_share = Service(bus, svc0_path, UUID_SVC_SHARE, share_chars)
    svc_common = Service(bus, svc1_path, UUID_SVC_COMMON, common_chars)
    app = Application(bus, [svc_share, svc_common])

    gatt_mgr = dbus.Interface(bus.get_object(BLUEZ, adapter_path), GATT_MANAGER)
    if with_gatt:
        try:
            gatt_mgr.RegisterApplication(app.path, {})
            log("[gatt] application registered")
        except dbus.DBusException as e:
            log("[gatt] RegisterApplication failed:", e)

    adv = Advertisement(bus, name[:16], sd)
    adv_mgr = dbus.Interface(bus.get_object(BLUEZ, adapter_path), LE_ADV_MANAGER)
    try:
        adv_mgr.RegisterAdvertisement(adv.PATH, {})
        log(f"[adv] advertising name={name!r} advFlags=0x{adv_flags:02x} "
            f"rspFlags=0x{rsp_flags:02x} id={device_id.hex()}")
    except dbus.DBusException as e:
        log("[adv] RegisterAdvertisement failed:", e)
        log("      (adapter may not support LE peripheral mode)")

    log("[run] open 互传 on the phone, go to 其他设备, tap this device")
    log("[run] Ctrl-C to stop")

    loop = GLib.MainLoop()
    signal.signal(signal.SIGINT, lambda *_: loop.quit())
    signal.signal(signal.SIGTERM, lambda *_: loop.quit())
    try:
        loop.run()
    finally:
        try:
            adv_mgr.UnregisterAdvertisement(adv.PATH)
        except Exception:
            pass
        if with_gatt:
            try:
                gatt_mgr.UnregisterApplication(app.path)
            except Exception:
                pass
        log("[run] stopped")


def main():
    ap = argparse.ArgumentParser(description="OShare BLE helper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="dump BLE advertisements")
    s.add_argument("-t", "--time", type=int, default=20)
    s.add_argument("--no-watch", action="store_true")

    a = sub.add_parser("adv", help="advertise only")
    a.add_argument("--name", default="LinuxBox")
    a.add_argument("--adv-flags", default="0x05")
    a.add_argument("--rsp-flags", default="0x01")
    a.add_argument("--account", default="")

    v = sub.add_parser("serve", help="advertise + GATT server")
    v.add_argument("--name", default="LinuxBox")
    v.add_argument("--adv-flags", default="0x05")
    v.add_argument("--rsp-flags", default="0x01")
    v.add_argument("--account", default="")

    for p in (s, a, v):
        p.add_argument("--adapter", default=None, help="e.g. hci0")

    args = ap.parse_args()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    path, _ = get_adapter(bus, args.adapter)
    if path is None:
        sys.exit("no bluetooth adapter (is bluetoothd running?)")
    log(f"[init] adapter {path}")

    if args.cmd == "scan":
        mode_scan(bus, path, args.time, not args.no_watch)
    elif args.cmd == "adv":
        mode_serve(bus, path, args.name, int(args.adv_flags, 0),
                   int(args.rsp_flags, 0), args.account, with_gatt=False)
    else:
        mode_serve(bus, path, args.name, int(args.adv_flags, 0),
                   int(args.rsp_flags, 0), args.account)


if __name__ == "__main__":
    main()
