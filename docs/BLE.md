# OShare / 互传联盟 BLE layer

Everything below comes from `com.coloros.oshare` 17.2.36 (`ble/impl/*`,
`k9/c.java`) plus the vendor Bluetooth stack logs of a real device
(`ble_oshare.cc`, `stack_app.cc` in `/data/misc/bluetooth/logs/`).

## Roles

The phone is always the BLE **central**: it scans, then connects as a GATT
**client** to the peer it wants to share with. The peer (another phone, or our
Linux box) must therefore be a GATT **server** (`peripheral`).

Timeline measured on a real OPPO ↔ Xiaomi transfer:

```
10:06:47  com.coloros.oshare  StartScan filters=4
10:06:51  clientConnect  -> 62:32:2A:CB:76:F9
10:06:51  com.heytap.accessory x4 + oshare GATT servers connected
10:06:53  clientDisConnect          (2 s handshake)
10:06:53  Wi-Fi Direct group forms, TLS transfer starts
```

The GATT link is encrypted (bonded), so the payloads cannot be read from an
HCI snoop without the LTK — the format below is taken from the code.

## Advertisement

The scanner (`com.oplus.oshare.ble.impl.o`) requires:

| AD field | content |
|---|---|
| Service UUIDs (16 bit) | **0x8881 must be the first entry** (`list.get(0).equals(k9.c.E)`) |
| Service Data `0x6666` | `[0]=17 (protocol version)` `[1..16]=deviceName (UTF-8, NUL padded)` `[17..22]=deviceId (6 bytes)` `[23]=capability bits` |
| Service Data `0x6667` | `[0]=capability bits` `[1..3]=accountId (3 bytes)` `[4..19]=accountName (16 bytes, protocol >= 17)` |

(There is a second, older pair of channels: service data `0x3333` + `0x3334`,
used by the pre-17 protocol.)

### capability bits (byte 23 of 0x6666, byte 0 of 0x6667)

`ble/impl/b.java` `q()` shows the advertiser ORs a device class and its
capabilities into one byte:

```
adv byte  = (x.p0() ? 1 : 2)                       // device class
          | 16 if x.o0() else 8 if x.O() else 4
          | 32 if x.u0() else 64 if x.X()

rsp byte  = 1                                      // always: new protocol
          | 2  if supports 5G
          | 4  if FastShareSupport.N()
          | 8 / 16 / 24 for band class 101 == 1 / 2 / 7
```

Decoded from `com/oplus/oshare/utils/x.java`:

| bit | meaning |
|---|---|
| 0 | screen on (1) / off (2) — `PowerManager.isInteractive()` |
| 2 | generic/OPPO brand (`x.O()` false and `x.o0()` false) |
| 3 | OnePlus (`Build.BRAND == OnePlus`) |
| 4 | realme (`Build.BRAND == Realme`) |
| 5 | tablet (`oplus.hardware.type.tablet`) |
| 6 | foldable |

So a realme phone advertises `0x11`, an OPPO phone `0x05`, a OnePlus `0x09`.
A Linux peer should send **`0x05`** (screen on, generic brand) — that is the
value `tools/ble_adv.py` defaults to.

The scanner decodes the same bits with `ble/impl/e0.java`
(`d() = bit3`, `g() = bit4`, `p() = bit5`, `k() = bit6`, `f() = bit1`,
`r() = bit0`). The exact combination that marks a "PC" is not documented —
`tools/ble_adv.py` lets you sweep `--adv-flags` to find one the phone accepts.

### vendor brand (`OShareVendorBrand`) — used in the JSON layer, not in BLE

| value | device |
|---|---|
| 10 | OPPO |
| 11 | realme |
| 20 | vivo |
| 30 | Xiaomi |
| 40 | other |
| 41–45 | OnePlus |
| 50 | Meizu |
| 60/66 | Nubia / Red Magic |
| 70 | Samsung |
| 80 | ZTE |
| 90 | "T" |
| 100 | Lenovo |
| 110 | Motorola |
| 120 | NIO |
| 160 | ROG |
| 161 | ASUS |
| 170 | Hisense |
| **800** | **Apple (iPhone/iPad)** |
| **801** | **Apple PC (Mac)** |
| **802** | **Windows PC** |

These numbers travel in the `extras`/`deviceInfo` JSON, not in the BLE
advertisement; the advertisement only carries the capability byte above.

## GATT profile

The phone looks up two services on the peer:

| service | purpose |
|---|---|
| `00009999-0000-1000-8000-00805f9b34fb` | main share service |
| `00009955-0000-1000-8000-00805f9b34fb` | "common share" (port / frequency) |
| `0000bb15-…` | DCP fallback (PantaConnect SDK) |

Characteristics used by the phone (`k9/c.java`):

| uuid | direction | meaning |
|---|---|---|
| `0x9989` | peer → phone (notify) | **REQUEST_GO**: a string, accumulated until `len >= 45`; carries the peer's request/wifi info |
| `0x9985` | peer → phone (notify) | **BLE_ACK_DIALOG**: Base64 text, the user's allow/deny result |
| `0x9997` | peer → phone (notify) | progress / update (`UPDATE_CHARACTERISTIC_UUID`) |
| `0x9986` | phone → peer (write) | **START_WITH_WPS_INFO**: MAC + `&`-separated fields (freq, type 11, SSID, PSK, …) |
| `0x9953` | phone → peer (write) | **PORT_FREQUENCY**: JSON `{"port":N,"ip":…}` (built by `za.c.h/i`) |
| `0x9984` | notify | log instruction text |
| `0x9895` | notify | iOS timeout marker |

`WidgetCodeToSeedlingCardConvertor.CARD_SPLIT` used as separator is the
literal `"&"`.

The 0x9986 payload is assembled in `j.j1()` as:

```
<mac without ':'> + "&" + <freqClass> + "&" + 11 + "&" + <ssid> + "&" + <psk> + …
```

(fields stay plain unless the pair is bound, in which case
`b0.o().e(...)` encrypts them with the per-pair key).

The 0x9953 payload is a JSON document containing at least `port` and the
device id; when the connection is paired the JSON is additionally signed
(`{"info":…,"salt":…,"sign":…}`).

## Consequence for a Linux peer

1. Advertise service UUID `0x8881` **plus the service-data UUIDs** and split
   the two payloads across ADV_IND and SCAN_RSP (see "Legacy packet budget").
2. Host a GATT server with service `0x9999` (and `0x9955`) exposing the
   characteristics above; the phone will connect, read `0x9996`, subscribe to
   `0x9985`/`0x9989` and write its share request to `0x9985`.
3. Parse the written Wi-Fi credentials, join the Wi-Fi Direct group the phone
   creates, then run the TLS client from `transfer/` against
   `https://<phone>:<port>/download?taskId=…`.

`tools/oshare_ble.py` implements steps 1 and 2 on BlueZ (Linux).

## Legacy packet budget (verified 2026-10-21)

BlueZ switches an advertisement to **extended advertising** as soon as the
ADV_IND payload exceeds 31 bytes (`src/advertising.c`: `bt_ad_length() >
BT_AD_MAX_DATA_LEN` forces `MGMT_ADV_FLAG_SEC_1M`). The ColorOS scanner uses
`SCAN_MODE_LOW_LATENCY` with `LEGACY = true`, so it **never reports** extended
PDUs: the phone's Bluetooth stack does receive them (it logs
`btm_ble_process_adv_addr`), but nothing reaches `ScanRecord`, so
`BleScannerImpl` logs only `byteData is null`.

Two further traps found on the way:

* `0x6666` (24 B) + `0x6667` (20 B) cannot share one legacy packet. BlueZ's
  `ScanResponseServiceData` property puts one of them into the scan response;
  Android merges ADV_IND + SCAN_RSP back into a single `ScanRecord`, which is
  exactly what `o.q()` expects.
* A `Discoverable`/`LocalName` property makes BlueZ append a Flags AD (3 B)
  and a name AD on top, pushing the packet over the limit again.

Layout that the phone accepts (`tools/oshare_ble.py serve`):

```
ADV_IND : Flags | UUID16 list [6666, 6667] | ServiceData 0x6667        ~31 B
SCAN_RSP:                                   ServiceData 0x6666        ~27 B
```

`ServiceUUIDs` matters twice: the scanner's `ScanFilter` is built from
`00008881`, `00006667`, `00003333`, `00003331` (dumpsys `LE Scanner`), but
`ScanRecord.getServiceUuids()` is what makes the app treat the peer as a
share target at all — service data alone shows up in `ScanRecord` but not in
the device list.

## GATT session as observed

After service discovery the phone (as the GATT client) runs:

```
read   0x9996 (WIFI_INFO)   -> expects {"state":0,"mac":"<12 hex>","key":""}
write  0x9985 (ACK_DIALOG)  -> 66-byte share request, '&'-separated:
                               66&1&11&<device name>&...&<mime>&<size>&<contact hash>
notify 0x9985               -> receiver's verdict: Base64("ok" | "no" | "time")
```

The share request is `w.e.y()`'s format: `count`, `type`, `version`, device
name, …, mime, size, and the sender's contact hash. `0x9985` is written with
`setCharacteristicNotification()` immediately before the write, i.e. the phone
expects the answer on the same characteristic.

Open problem (2026-10-21): the phone calls
`setCharacteristicNotification(0x9985, true)` but the GATT exploration on the
phone (`bta_gattc_display_explore_record`) lists **no 0x2902 descriptor** for
the `0x9999` service — only the GAP service shows one. Without a CCC write
BlueZ refuses to emit the notification, so the phone times out with
"对方未确认". BlueZ does create the CCCD on `RegisterApplication`
(`database_add_ccc()`), so the descriptor is either not advertised because
the service was registered with an explicit handle, or the phone reuses a
stale cache from `/data/misc/bluetooth/gatt_cache_*`.

## ACK semantics (why the phone reports 「发送超时，对方未确认」)

From `ble/impl/w.java` (the phone's own GATT **server**):

```java
// onCharacteristicWriteRequest, uuid == 0x9985
byte b = value[0];
if (b == 1) o(address, value);     // receiver allowed
else        k(address, char, off); // rejected / cancelled
```

So the verdict travels as a **write** of `0x01` to the peer's `0x9985`, not as a
notification. The notification variant (`sendBleAckResult`, Base64 text) is what
the phone's server sends to *its* peer, but BlueZ will only forward a
`PropertiesChanged` after a client writes the CCCD — and the ColorOS client
never writes a CCCD (its `onDescriptorWrite` is empty and it only calls
`setCharacteristicNotification` locally).

`tools/oshare_ble.py` therefore ACKs by switching roles: when a write arrives it
connects to the phone as a GATT client and writes `0x01` to the phone's
`0x9985` (`ack_peer()`), which is exactly what `w.java` expects.

### Android GATT cache

Android caches the peer's attribute database per device address. After changing
our service layout the phone keeps the old (incomplete) view — that is the usual
reason a newly added characteristic/CCCD does not show up. Between runs either

```sh
adb shell su -c 'svc bluetooth disable; sleep 2; svc bluetooth enable'
```

or change the advertised address / unpair. The phone-side log (below) tells you
which UUID it is looking at.

### Custom descriptors on the phone's server (`w.G0()`)

The phone does **not** use the standard `0x2902`; it attaches its own
descriptors to the notifiable characteristics:

| characteristic | descriptor |
|---|---|
| `0x9998` | `0x9994` |
| `0x9996` | `0x9993` |
| `0x9995` | `0x9992` |
| `0x9990` | `0x9991` |
| `0x9989` | `0x9988` |
| `0x9986` | `0x9987` |

Mirroring them on our side is harmless and may satisfy clients that probe for
them.

### Watching the phone while testing

```sh
adb logcat -s BleServer:V BleClient:V BleServer_iPhone:V OShare_:V
```

`BleServer onCharacteristicWriteRequest …` / `BLE_ACK_DIALOG flag:` /
`request create GroupOwner` show exactly which step the phone is waiting on.
