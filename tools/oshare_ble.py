#!/usr/bin/env python3
"""oshare_ble.py - one-stop BLE tool for OShare (ColorOS 互传) work on Linux.

Three modes:

  scan                     dump BLE advertisements (highlights OShare ones)
  adv                      advertise this machine as a share peer
  serve                    advertise + host the OShare GATT server, dump
                           everything the phone reads/writes

Typical session:

  python3 tools/oshare_ble.py scan -t 20          # see the phone's own advert
  python3 tools/oshare_ble.py serve --name LinuxBox
  # ... on the phone: 互传 -> 发送页 -> 其他设备 -> 点 "LinuxBox" ...

Runs unprivileged: it talks to bluetoothd over the system D-Bus, and the
default BlueZ policy allows any user to send to org.bluez. Only needs
python-gobject (already present on CachyOS via gobject-introspection).

Protocol notes live in docs/BLE.md.
"""

import argparse
import base64
import json
import os
import signal
import sys
import time

try:
    from gi.repository import Gio, GLib
except ImportError:  # pragma: no cover
    sys.exit("missing deps: sudo pacman -S python-gobject")

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
LE_ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
GATT_MANAGER = "org.bluez.GattManager1"
LE_ADV_IFACE = "org.bluez.LEAdvertisement1"
GATT_CHR_IFACE = "org.bluez.GattCharacteristic1"
GATT_SVC_IFACE = "org.bluez.GattService1"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

# Answer the phone's share request automatically with "ok".
AUTO_ACK = True

UUID_OS_MAIN = "00008881-0000-1000-8000-00805f9b34fb"   # advertised service uuid
UUID_SD_ADV = "00006666-0000-1000-8000-00805f9b34fb"    # adv payload
UUID_SD_RSP = "00006667-0000-1000-8000-00805f9b34fb"    # scan response payload
UUID_SVC_SHARE = "00009999-0000-1000-8000-00805f9b34fb"
UUID_SVC_COMMON = "00009955-0000-1000-8000-00805f9b34fb"

# characteristic uuid -> (flags, name)
#
# Mirrors the GATT server com.coloros.oshare itself builds (ble/impl/w.java
# G0(): service 0x9999) plus D0() (service 0x9955). The flags are the Android
# property bits: 2 read, 8 write, 16 notify.
CHARS_SHARE = {
    "00009998-0000-1000-8000-00805f9b34fb": (["notify"], "UPDATE"),
    "00009996-0000-1000-8000-00805f9b34fb": (["read", "write"], "WIFI_INFO"),
    "00009995-0000-1000-8000-00805f9b34fb": (["read", "write"], "SHARE_CMD"),
    "00009990-0000-1000-8000-00805f9b34fb": (["read", "write"], "P2P_CMD"),
    "00009989-0000-1000-8000-00805f9b34fb": (["read", "write", "notify"], "REQUEST_GO"),
    "00009986-0000-1000-8000-00805f9b34fb": (["read", "write"], "START_WPS_INFO"),
    "00009997-0000-1000-8000-00805f9b34fb": (["notify"], "LOG"),
    "00009985-0000-1000-8000-00805f9b34fb": (["read", "write", "notify"], "ACK_DIALOG"),
    "00009984-0000-1000-8000-00805f9b34fb": (["write", "notify"], "WPS_RESULT"),
    "00009981-0000-1000-8000-00805f9b34fb": (["read", "write"], "EXTRA_1"),
    "00009898-0000-1000-8000-00805f9b34fb": (["read", "write", "notify"], "IOS_1"),
    "00009897-0000-1000-8000-00805f9b34fb": (["read", "write"], "IOS_2"),
    "00009896-0000-1000-8000-00805f9b34fb": (["read", "write", "notify"], "IOS_3"),
    "00009894-0000-1000-8000-00805f9b34fb": (["read", "write", "notify"], "IOS_4"),
    "00009895-0000-1000-8000-00805f9b34fb": (["notify"], "IOS_TIMEOUT"),
}
CHARS_COMMON = {
    "00009954-0000-1000-8000-00805f9b34fb": (["read", "write"], "COMMON_1"),
    "00009953-0000-1000-8000-00805f9b34fb": (["read", "write"], "PORT_FREQUENCY"),
}

XML_ADV = """
<node>
  <interface name="org.bluez.LEAdvertisement1">
    <method name="Release"/>
    <property name="Type" type="s" access="read"/>
    <property name="ServiceUUIDs" type="as" access="read"/>
    <property name="LocalName" type="s" access="read"/>
    <property name="ServiceData" type="a{sv}" access="read"/>
    <property name="ScanResponseServiceData" type="a{sv}" access="read"/>
    <property name="ManufacturerData" type="a{qv}" access="read"/>
    <property name="IncludeTxPower" type="b" access="read"/>
  </interface>
</node>
"""

XML_SVC = """
<node>
  <interface name="org.bluez.GattService1">
    <property name="UUID" type="s" access="read"/>
    <property name="Primary" type="b" access="read"/>
    <property name="Characteristics" type="ao" access="read"/>
    <property name="Includes" type="ao" access="read"/>
  </interface>
</node>
"""

XML_CHR = """
<node>
  <interface name="org.bluez.GattCharacteristic1">
    <method name="ReadValue">
      <arg name="options" type="a{sv}" direction="in"/>
      <arg name="value" type="ay" direction="out"/>
    </method>
    <method name="WriteValue">
      <arg name="value" type="ay" direction="in"/>
      <arg name="options" type="a{sv}" direction="in"/>
    </method>
    <method name="StartNotify"/>
    <method name="StopNotify"/>
    <property name="UUID" type="s" access="read"/>
    <property name="Service" type="o" access="read"/>
    <property name="Flags" type="as" access="read"/>
    <property name="Value" type="ay" access="read"/>
    <property name="Notifying" type="b" access="read"/>
  </interface>
</node>
"""

XML_APP = """
<node>
  <interface name="org.freedesktop.DBus.ObjectManager">
    <method name="GetManagedObjects">
      <arg name="objects" type="a{oa{sa{sv}}}" direction="out"/>
    </method>
  </interface>
</node>
"""


def _wifi_info(state=0, mac="4c6978757a42", key=""):
    """The 0x9996 payload, built the way ble/impl/w.java za.c.e() builds it."""
    return json.dumps({"state": state, "mac": mac, "key": key}).encode()


# Values handed out before the phone writes anything. The phone (as the BLE
# client) reads 0x9996 right after service discovery; leaving it empty makes it
# report "peer busy" and give up.
INITIAL_VALUES = {
    "WIFI_INFO": _wifi_info(),
    "SHARE_CMD": json.dumps({"state": 0}).encode(),
    "UPDATE": json.dumps({"state": 0}).encode(),
}


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def _signal_add():
    """GLib.unix_signal_add is deprecated in newer PyGObject builds."""
    try:
        from gi.repository import GLibUnix
        return GLibUnix.signal_add
    except ImportError:
        return GLib.unix_signal_add


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


# --------------------------------------------------------------------------
# D-Bus plumbing
# --------------------------------------------------------------------------
class Bus:
    def __init__(self):
        self.conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        log(f"[dbus] connected as {self.conn.get_unique_name()}")
        self.loop = GLib.MainLoop()
        self._objects = {}
        self._pending = []

    def call(self, path, iface, method, args=None, on_done=None, timeout=15000):
        """Async call; the GLib loop must be running for the reply to arrive."""
        self.conn.call(BLUEZ, path, iface, method, args, None,
                       Gio.DBusCallFlags.NONE, timeout, None,
                       lambda conn, res: self._finish(method, conn, res, on_done))

    def _finish(self, method, conn, res, on_done):
        try:
            reply = conn.call_finish(res)
            err = None
        except GLib.Error as e:
            reply, err = None, e
        if on_done:
            on_done(reply, err)
        else:
            if err:
                log(f"[dbus] {method} failed: {err.message}")
            else:
                log(f"[dbus] {method} ok")

    def register(self, path, xml):
        info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
        obj = _Object(self, path, info)
        self._objects[path] = obj
        return obj

    def stop(self):
        self.loop.quit()

    def run(self, seconds=None):
        if seconds:
            GLib.timeout_add_seconds(seconds, lambda: (self.stop(), False)[1])
        add = _signal_add()
        for sig in (signal.SIGINT, signal.SIGTERM):
            add(GLib.PRIORITY_HIGH, sig, lambda *_: (self.stop(), False)[1])
        self.loop.run()


class _Object:
    """A D-Bus object with a fixed property table plus optional method hooks."""

    def __init__(self, bus, path, info):
        self.bus = bus
        self.path = path
        self.info = info
        self.props = {}
        self.methods = {}
        bus.conn.register_object(
            path, info, self._on_method, self._on_get, None)

    def _on_method(self, conn, sender, path, iface, method, params, inv):
        fn = self.methods.get(method)
        if fn is None:
            inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
            return
        try:
            out = fn(params.unpack())
        except Exception as e:  # keep bluetoothd from hanging on our bugs
            inv.return_dbus_error("org.bluez.Error.Failed", str(e))
            return
        if out is None:
            inv.return_value(None)
        else:
            inv.return_value(out)

    def _on_get(self, conn, sender, path, iface, prop):
        v = self.props.get(prop)
        if v is None:
            return None
        return v() if callable(v) else v


# --------------------------------------------------------------------------
# advertising
# --------------------------------------------------------------------------
class Advertisement(_Object):
    PATH = "/org/oshare/adv0"

    def __init__(self, bus, name, service_data):
        super().__init__(bus, self.PATH, Gio.DBusNodeInfo.new_for_xml(XML_ADV).interfaces[0])
        self.name = name
        self.service_data = service_data
        self.methods["Release"] = self._release
        # 0x6666 is 24 bytes and 0x6667 another 20, so both cannot ride in one
        # legacy advert (31 B). Put 0x6667 in the advert together with the
        # service UUID list and 0x6666 into the scan response, which is what
        # BlueZ's ScanResponseServiceData is for. No LocalName/Discoverable
        # here: BlueZ would add a Flags AD and push ADV past 31 bytes, and a
        # packet that long is sent as extended advertising, which the phone's
        # legacy scanner never reports.
        self.props = {
            "Type": GLib.Variant("s", "peripheral"),
            "ServiceUUIDs": GLib.Variant("as", [UUID_SD_RSP, UUID_SD_ADV]),
            "ServiceData": GLib.Variant("a{sv}", {
                UUID_SD_RSP: GLib.Variant("ay", service_data[UUID_SD_RSP])}),
            "ScanResponseServiceData": GLib.Variant("a{sv}", {
                UUID_SD_ADV: GLib.Variant("ay", service_data[UUID_SD_ADV])}),
            "ManufacturerData": GLib.Variant("a{qv}", {}),
            "IncludeTxPower": GLib.Variant("b", False),
        }

    def _release(self, params):
        log("[adv] released by bluez")


# --------------------------------------------------------------------------
# GATT server
# --------------------------------------------------------------------------
class Characteristic(_Object):
    def __init__(self, bus, index, uuid, flags, name, service_path):
        super().__init__(bus, f"/org/oshare/gatt/char{index}",
                         Gio.DBusNodeInfo.new_for_xml(XML_CHR).interfaces[0])
        self.uuid = uuid
        self.flags = flags
        self.name = name
        self.service_path = service_path
        self.value = INITIAL_VALUES.get(name, b"")
        self.notifying = False
        self.pending_writes = {}  # offset -> bytes, for long (prepared) writes
        self.methods = {
            "ReadValue": self._read,
            "WriteValue": self._write,
            "StartNotify": self._start_notify,
            "StopNotify": self._stop_notify,
        }
        self.props = {
            "UUID": GLib.Variant("s", uuid),
            "Service": GLib.Variant("o", service_path),
            "Flags": GLib.Variant("as", flags),
            "Value": lambda: GLib.Variant("ay", self.value),
            "Notifying": lambda: GLib.Variant("b", self.notifying),
        }

    def _read(self, params):
        # A read with an offset is a long read: return the tail only.
        options = params[0] if params else {}
        offset = int(options.get("offset", 0)) if isinstance(options, dict) else 0
        data = self.value[offset:]
        log(f"[gatt] read  {self.name} @{offset} -> {len(data)}B {data.hex()}")
        return GLib.Variant("(ay)", (data,))

    def _write(self, params):
        data = bytes(params[0])
        options = params[1] if len(params) > 1 else {}
        offset = int(options.get("offset", 0)) if isinstance(options, dict) else 0
        if offset:
            # Long write: bluez sends the value in pieces with increasing
            # offsets, and asks us to keep the merged result.
            self.pending_writes[offset] = data
            merged = b"".join(self.pending_writes[k] for k in sorted(self.pending_writes))
            log(f"[gatt] write {self.name} @{offset} ({len(data)}B, merged {len(merged)}B)")
            if "prepare-authorize" in options:
                return GLib.Variant("(ay)", (merged,))
            data = merged
        self.value = data
        log(f"[gatt] write {self.name} ({len(data)} bytes)")
        print(hexdump(data), flush=True)
        try:
            text = data.decode("utf-8")
            if text.isprintable():
                log(f"[gatt]   text: {text!r}")
                parse_payload(self.name, text, data)
        except UnicodeDecodeError:
            pass
        dev_path = None
        if isinstance(options, dict):
            dev_path = options.get("device")
        if dev_path and AUTO_ACK and self.name in ("ACK_DIALOG", "WPS_RESULT", "SHARE_CMD"):
            log(f"[ack] peer device {dev_path}, scheduling ACK")
            ack_peer(self.bus, str(dev_path))

        if False and self.name == "ACK_DIALOG" and AUTO_ACK:
            # The phone wrote its share request and is now waiting for the
            # receiver's verdict on 0x9985: plain "ok" / "no" / "time",
            # Base64-encoded (ble/impl/w.java i1()).
            GLib.timeout_add(500, self._ack, b"ok")
        return None

    def _ack(self, verdict: bytes):
        self.notify(base64.b64encode(verdict))
        return False

    def _start_notify(self, params):
        self.notifying = True
        log(f"[gatt] notify on  {self.name}")
        return None

    def _stop_notify(self, params):
        self.notifying = False
        log(f"[gatt] notify off {self.name}")
        return None

    def notify(self, data: bytes):
        # Emit regardless of our own subscription bookkeeping: bluez drops the
        # value for devices that have not enabled the CCC descriptor, and the
        # phone enables notifications without us ever seeing a StartNotify.
        self.value = data
        self.bus.conn.emit_signal(
            None, self.path, PROPS_IFACE, "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (
                GATT_CHR_IFACE,
                {"Value": GLib.Variant("ay", data)},
                [])))
        log(f"[gatt] notified {self.name} {data.hex()}"
            + ("" if self.notifying else " (no CCCD subscription seen)"))
        return True


class Service(_Object):
    def __init__(self, bus, path, uuid, chars):
        super().__init__(bus, path, Gio.DBusNodeInfo.new_for_xml(XML_SVC).interfaces[0])
        self.uuid = uuid
        self.chars = chars
        self.props = {
            "UUID": GLib.Variant("s", uuid),
            "Primary": GLib.Variant("b", True),
            "Characteristics": GLib.Variant("ao", [c.path for c in chars]),
            "Includes": GLib.Variant("ao", []),
        }


class Application(_Object):
    PATH = "/org/oshare/gatt"

    def __init__(self, bus, services):
        super().__init__(bus, self.PATH, Gio.DBusNodeInfo.new_for_xml(XML_APP).interfaces[0])
        self.services = services
        self.methods = {"GetManagedObjects": self._managed_objects}

    def _managed_objects(self, params):
        out = {}
        for svc in self.services:
            out[svc.path] = {GATT_SVC_IFACE: _props_to_variants(svc.props)}
            for ch in svc.chars:
                out[ch.path] = {GATT_CHR_IFACE: _props_to_variants(ch.props)}
        return GLib.Variant("(a{oa{sa{sv}}})", (out,))


def _props_to_variants(props):
    return {k: (v() if callable(v) else v) for k, v in props.items()}


def parse_payload(name, text, raw):
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
ACK_CHAR_UUID = "00009985-0000-1000-8000-00805f9b34fb"


def ack_peer(bus, dev_path, payload=b"\x01", delay_ms=300, retries=5):
    """Write the allow-ACK to the *phone's* 0x9985 characteristic.

    The phone is a GATT client towards us, but its own server exposes the same
    0x9999 service. `ble/impl/w.java` (onCharacteristicWriteRequest) treats a
    write to 0x9985 as the receiver's verdict: first byte 1 = allow, anything
    else = reject. BlueZ will not emit our notification until the phone writes
    the CCCD (it never does), so we ACK by writing to the phone instead.
    """

    def find_and_write(attempt=0):
        def on_objects(reply, err):
            chars = []
            if not err:
                for path, ifaces in reply.unpack()[0].items():
                    if "org.bluez.GattCharacteristic1" not in ifaces:
                        continue
                    props = ifaces["org.bluez.GattCharacteristic1"]
                    if str(props.get("UUID", "")).lower() == ACK_CHAR_UUID:
                        chars.append(str(path))
            if not chars:
                if attempt < retries:
                    log(f"[ack] 0x9985 not visible yet (attempt {attempt + 1}), retrying")
                    GLib.timeout_add(700, lambda: (find_and_write(attempt + 1), False)[1])
                else:
                    log("[ack] gave up: 0x9985 not in the peer's GATT tree")
                return
            for path in chars:
                log(f"[ack] writing {payload.hex()} to {path}")

                def done(reply2, err2):
                    if err2:
                        log("[ack] WriteValue failed:", err2.message)
                    else:
                        log("[ack] peer acknowledged (write ok)")

                bus.call(path, "org.bluez.GattCharacteristic1", "WriteValue",
                         GLib.Variant("(aya{sv})", (payload, {})), done, timeout=15)

        bus.call("/", "org.freedesktop.DBus.ObjectManager",
                 "GetManagedObjects", None, on_objects, timeout=10)

    def after_connect(reply, err):
        if err:
            log("[ack] connect failed:", err.message)
            return
        log("[ack] connected to peer, scanning its GATT tree for 0x9985")
        GLib.timeout_add(400, lambda: (find_and_write(), False)[1])

    def kick():
        bus.call(dev_path, "org.bluez.Device1", "Connect", None, after_connect,
                 timeout=25)

    GLib.timeout_add(delay_ms, lambda: (kick(), False)[1])


def find_adapter(bus, want=None):
    reply = bus.conn.call_sync(
        BLUEZ, "/", OM_IFACE, "GetManagedObjects", None,
        GLib.VariantType.new("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 10000, None)
    for path, ifaces in reply.unpack()[0].items():
        if ADAPTER_IFACE in ifaces and (not want or path.endswith("/" + want)):
            return path
    return None


def mode_scan(bus, adapter_path, seconds, watch):
    seen = {}

    def dump(path, props):
        addr = str(props.get("Address", "?"))
        name = str(props.get("Name", "") or props.get("Alias", "") or "")
        sd = props.get("ServiceData") or {}
        rssi = props.get("RSSI", "")
        uuids = [str(u) for u in (props.get("UUIDs") or [])]
        interesting = any("6666" in u or "6667" in u or "8881" in u for u in uuids) or any(
            "6666" in k or "6667" in k or "8881" in k for k in sd)
        key = (addr, name, tuple(sorted(uuids)), rssi)
        if key in seen:
            return
        seen[key] = True
        mark = "  <== OSHARE?" if interesting else ""
        log(f"{addr} rssi={rssi} name={name!r} uuid={uuids}{mark}")
        for uuid, val in sd.items():
            print(f"    sd {uuid}: {bytes(val).hex()}", flush=True)
        md = props.get("ManufacturerData") or {}
        for k, v in md.items():
            print(f"    mfr 0x{int(k):04x}: {bytes(v).hex()}", flush=True)

    def _on_interfaces_added(params):
        path, ifaces = params.unpack()
        for iname, props in ifaces.items():
            if iname == "org.bluez.Device1":
                dump(path, props)

    def _on_props_changed(path, params):
        iname, changed, _invalidated = params.unpack()
        if iname == "org.bluez.Device1":
            dump(path, changed)

    if watch:
        bus.conn.signal_subscribe(
            BLUEZ, OM_IFACE, "InterfacesAdded", "/", None,
            Gio.DBusSignalFlags.NONE,
            lambda conn, sender, path, iface, signame, params:
                _on_interfaces_added(params))
        bus.conn.signal_subscribe(
            BLUEZ, PROPS_IFACE, "PropertiesChanged", "/", None,
            Gio.DBusSignalFlags.NONE,
            lambda conn, sender, path, iface, signame, params:
                _on_props_changed(path, params))

    # Devices already cached from earlier scans.
    reply = bus.conn.call_sync(
        BLUEZ, "/", OM_IFACE, "GetManagedObjects", None,
        GLib.VariantType.new("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 10000, None)
    for path, ifaces in reply.unpack()[0].items():
        if "org.bluez.Device1" in ifaces:
            dump(path, ifaces["org.bluez.Device1"])

    log(f"[scan] discovering for {seconds}s ...")
    bus.call(adapter_path, ADAPTER_IFACE, "SetDiscoveryFilter",
             GLib.Variant("(a{sv})", ({
                 "Transport": GLib.Variant("s", "le"),
                 "DuplicateData": GLib.Variant("b", True),
             },)))
    bus.call(adapter_path, ADAPTER_IFACE, "StartDiscovery")
    bus.run(seconds)
    bus.call(adapter_path, ADAPTER_IFACE, "StopDiscovery")
    log("[scan] done")


def mode_serve(bus, adapter_path, name, adv_flags, rsp_flags, account, with_gatt=True):
    device_id = os.urandom(6)
    sd = build_service_data(name, device_id, adv_flags, rsp_flags, account_name=account)

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

    if with_gatt:
        bus.call(adapter_path, GATT_MANAGER, "RegisterApplication",
                 GLib.Variant("(oa{sv})", (app.path, {})),
                 lambda reply, err: log("[gatt] application failed: " + err.message)
                 if err else log("[gatt] application registered"))

    adv = Advertisement(bus, name, sd)

    def adv_done(reply, err):
        if err:
            log("[adv] RegisterAdvertisement failed:", err.message)
            log("      (adapter may not support peripheral mode, or another "
                "advertisement is active)")
        else:
            log(f"[adv] advertising name={name!r} advFlags=0x{adv_flags:02x} "
                f"rspFlags=0x{rsp_flags:02x} id={device_id.hex()}")

    bus.call(adapter_path, LE_ADV_MANAGER, "RegisterAdvertisement",
             GLib.Variant("(oa{sv})", (adv.PATH, {})), adv_done)

    log("[run] open 互传 on the phone, go to 其他设备, tap this device")
    log("[run] Ctrl-C to stop")
    try:
        bus.run()
    finally:
        bus.call(adapter_path, LE_ADV_MANAGER, "UnregisterAdvertisement",
                 GLib.Variant("(o)", (adv.PATH,)),
                 lambda reply, err: None if err else log("[adv] unregistered"))
        if with_gatt:
            bus.call(adapter_path, GATT_MANAGER, "UnregisterApplication",
                     GLib.Variant("(o)", (app.path,)),
                     lambda reply, err: None if err else log("[gatt] unregistered"))
        # Let the unregister replies drain.
        GLib.timeout_add_seconds(1, lambda: (bus.stop(), False)[1])
        bus.loop.run()
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
    v.add_argument("--no-ack", action="store_true",
                   help="do not auto-write the 0x01 verdict to the phone")

    for p in (s, a, v):
        p.add_argument("--adapter", default=None, help="e.g. hci0")

    args = ap.parse_args()

    global AUTO_ACK
    if getattr(args, "no_ack", False):
        AUTO_ACK = False

    bus = Bus()
    path = find_adapter(bus, args.adapter)
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
