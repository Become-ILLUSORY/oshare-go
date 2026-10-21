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

1. Advertise service UUID `0x8881` first + service data `0x6666`/`0x6667` with
   `advBrandType = 802`.
2. Host a GATT server with service `0x9999` (and `0x9955`) exposing the
   characteristics above; the phone will connect, subscribe to `0x9989`/
   `0x9985`/`0x9997` and write to `0x9986`/`0x9953`.
3. Parse the written Wi-Fi credentials, join the Wi-Fi Direct group the phone
   creates, then run the TLS client from `transfer/` against
   `https://<phone>:<port>/download?taskId=…`.

`tools/ble_scan.py` and `tools/ble_peer.py` in this repository implement the
first steps on BlueZ (Linux).
