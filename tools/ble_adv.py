#!/usr/bin/env python3
"""Advertise a Linux box as an OShare ("互传") peer over BLE.

Milestone 1: make the phone list us under 其他设备 in 互传.

Requires BlueZ (CachyOS: `sudo pacman -S bluez bluez-utils python-dbus`)
and an adapter that supports LE peripheral mode (`hciconfig`/`bluetoothctl list`).

Usage:
    sudo systemctl start bluetooth
    python3 tools/ble_adv.py --name LinuxBox --brand 802
    # ... open 互传 on the phone, look at 其他设备 ...
    Ctrl-C to stop

The advertising payload mirrors what the ColorOS app expects
(see docs/BLE.md):

  service uuid 0x8881 (must be first)
  service data 0x6666 : [ver][16B name][6B deviceId][brand]
  service data 0x6667 : [brand][3B accountId][16B accountName]
"""

import argparse
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
ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"
ADAPTER_IFACE = "org.bluez.Adapter1"
LE_ADV_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"

UUID_OS_MAIN = "00008881-0000-1000-8000-00805f9b34fb"
UUID_SD_ADV = "00006666-0000-1000-8000-00805f9b34fb"
UUID_SD_RSP = "00006667-0000-1000-8000-00805f9b34fb"




def pad(b: bytes, n: int) -> bytes:
    return b[:n] + b"\x00" * max(0, n - len(b))


def build_service_data(name: str, device_id: bytes, adv_flags: int,
                       rsp_flags: int = 1,
                       account_id: bytes = b"\x00\x00\x00",
                       account_name: str = "") -> dict:
    """Build the two OShare service-data blobs (see docs/BLE.md)."""
    adv = bytes([17]) + pad(name.encode(), 16) + pad(device_id, 6) + bytes([adv_flags & 0xFF])
    rsp = bytes([rsp_flags & 0xFF]) + pad(account_id, 3) + pad(account_name.encode(), 16)
    return {UUID_SD_ADV: adv, UUID_SD_RSP: rsp}


class Advertisement(dbus.service.Object):
    PATH_BASE = "/org/oshare/adv"

    def __init__(self, bus, index, name, service_data):
        self.path = f"{self.PATH_BASE}{index}"
        self.bus = bus
        self.name = name
        self.service_data = service_data
        super().__init__(bus, self.path)

    def get_properties(self):
        props = {
            "Type": dbus.String("peripheral"),
            "ServiceUUIDs": dbus.Array([UUID_OS_MAIN], signature="s"),
            "LocalName": dbus.String(self.name),
            "IncludeTxPower": dbus.Boolean(False),
        }
        if self.service_data:
            props["ServiceData"] = dbus.Dictionary(
                {dbus.String(k): dbus.Array(dbus.ByteArray(v), signature="y")
                 for k, v in self.service_data.items()},
                signature="sv",
            )
        return {ADVERTISEMENT_IFACE: props}

    @dbus.service.method(ADVERTISEMENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        print("[adv] released", flush=True)

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        return self.get_properties()[interface][prop]

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="", out_signature="a{sv}")
    def GetAll(self, interface):
        return self.get_properties()[interface]


def find_adapter(bus, want=None):
    om = dbus.Interface(bus.get_object(BLUEZ, "/"), "org.freedesktop.DBus.ObjectManager")
    for path, ifaces in om.GetManagedObjects().items():
        if ADAPTER_IFACE not in ifaces:
            continue
        if want and not path.endswith("/" + want):
            continue
        return path, ifaces
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="LinuxBox", help="device name (16 bytes max)")
    ap.add_argument("--adv-flags", default="0x02",
                    help="capability byte in the 0x6666 payload (bitmask, default 0x02)")
    ap.add_argument("--rsp-flags", default="0x01",
                    help="capability byte in the 0x6667 payload (bitmask, default 0x01)")
    ap.add_argument("--instance", default=None, help="override instance name")
    ap.add_argument("--adapter", default=None, help="e.g. hci0")
    ap.add_argument("--account", default="", help="account name (16 bytes max)")
    ap.add_argument("--dry-run", action="store_true", help="print payloads and exit")
    args = ap.parse_args()

    adv_flags = int(args.adv_flags, 0)
    rsp_flags = int(args.rsp_flags, 0)

    device_id = os.urandom(6)
    sd = build_service_data(args.name, device_id, adv_flags,
                            rsp_flags=rsp_flags, account_name=args.account)
    if args.dry_run:
        for k, v in sd.items():
            print(k, v.hex())
        return

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    path, ifaces = find_adapter(bus, args.adapter)
    if path is None:
        sys.exit("no bluetooth adapter found (is bluetoothd running?)")
    print(f"[adv] adapter {path}", flush=True)

    adapter = dbus.Interface(bus.get_object(BLUEZ, path), ADAPTER_IFACE)
    try:
        adapter.SetDiscoveryFilter({"Transport": dbus.String("le")})
    except dbus.DBusException:
        pass
    try:
        adapter.SetPowered(True)
    except dbus.DBusException as e:
        print("[adv] set powered:", e)

    adv_mgr = dbus.Interface(bus.get_object(BLUEZ, path), LE_ADV_MANAGER_IFACE)
    adv = Advertisement(bus, 0, args.name[:16], sd)
    try:
        adv_mgr.RegisterAdvertisement(adv.path, {})
    except dbus.DBusException as e:
        sys.exit(f"RegisterAdvertisement failed: {e}")

    print(f"[adv] advertising as {args.name!r} advFlags=0x{adv_flags:02x} rspFlags=0x{rsp_flags:02x} id={device_id.hex()}", flush=True)
    print("[adv] now open 互传 on the phone and look at 其他设备", flush=True)

    loop = GLib.MainLoop()
    signal.signal(signal.SIGINT, lambda *_: loop.quit())
    signal.signal(signal.SIGTERM, lambda *_: loop.quit())
    try:
        loop.run()
    finally:
        try:
            adv_mgr.UnregisterAdvertisement(adv.path)
        except dbus.DBusException:
            pass
        print("[adv] stopped", flush=True)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
