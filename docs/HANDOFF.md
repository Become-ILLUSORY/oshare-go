# Handoff — OShare (互传) reverse engineering & Go port

State as of 2026-10-21. Everything below is measured on a real device and/or
read out of the decompiled APKs; claims are marked **(verified)** when they were
confirmed on the wire, **(code)** when they come from source, **(open)** when
still unproven.

---

## 1. Goal

A general Go implementation of ColorOS 互传 so that non-OPPO machines
(Linux desktop / NAS) can exchange files with an unmodified ColorOS/realme
phone, without OPPO's software.

## 2. Test environment

| item | value |
|---|---|
| phone | realme GT7 Pro (`RMX5010`), ColorOS 17 / Android 17 (SDK 37) |
| 互传 app | `com.coloros.oshare` 17.2.36 (matches the APK analysed) |
| O+ Connect app | `com.oplus.linker` 17.7.30 |
| interconnect | `AccessoryFramework.apk` 17.6.16 (`com.oplus.pantaconnect`) |
| root | KernelSU, `adb shell su -c …` works |
| other peer used for captures | a nearby Xiaomi phone (unmodified Mi Share / 互传联盟) |
| dev machine for BLE work | CachyOS laptop with a Bluetooth adapter |

APKs analysed with `apktool` + `jadx`; native libs with `rizin`; APK → source
recovered for the Windows client from its shipped source maps.

## 3. Repository

`https://github.com/Become-ILLUSORY/oshare-go` (public, MIT, CI green).

```
protocol/  frame codec            — done, byte-exact tests
transfer/  Push / Receive roles   — done, loopback integration tests
discovery/ mDNS (_oshare._tcp)    — done, packet tests
cmd/oshare CLI send/recv/discover — done
tools/     oshare_ble.py + ble_scan.sh + ble_adv.py — BLE experiment (untested)
docs/      BLE.md, PC-CLIENT.md, HANDOFF.md (this file)
```

`go test ./...` passes; `go vet` clean; 7 cross-build targets in CI.

## 4. The transfer protocol (verified)

Full description in [PROTOCOL.md](../PROTOCOL.md). Short form:

```
frame := magic:int32=1001  type:int32  length:int64  payload
  4096 command : cmd:int32, nameLen, name(utf8), flag      length = 12+nameLen
  16384 keepalive: value:int32                             length = 4
  8192 file    : bodyLen:int64(=28+nameLen), nameLen, name, fileType, size, flag
                 followed by `size` raw bytes              length = 44+nameLen
```

Commands: `0` keepalive, `1` announce, `2` allow, `3` reject, `4` timeout,
`5` over, `6` no space, `7` cancel, `8` cancel-ok, `9` thumbnail,
`10` short text, `11` client info, `12` progress, `21` server info, `22` log.
Multi-field payloads are joined with `&&_&&`. File flag `0x02|0x04` = directory,
`0x10` = source was a File, `0x20` = sender transcoded.

Source classes: `mb/f.java` encoder, `mb/d.java` decoder, `mb/i.java` file
header, `pb/a.java` payload join, `r9/g.java` (sender), `r9/b.java` (receiver).

## 5. The alliance path — why no OPPO key is needed (verified)

Two real phone↔Xiaomi transfers were captured with root `AF_PACKET` on `wlan0`
and `p2p0` (devices: OPPO→Xiaomi and Xiaomi→OPPO). Findings:

* Transport is **Wi-Fi Direct**, GO = whoever initiates: transfer 1 the phone
  was `192.168.49.1` (GO) and the Xiaomi `…49.171`; transfer 2 the roles swapped
  (Xiaomi `.1`, phone `.128`).
* The **sender is the TLS server**, the **receiver dials it** and opens two
  connections (control + data) to one dynamic high port (`50856`, `54377`).
* TLS 1.3 with a **self-signed `CN=localhost` certificate** (valid 2025→9999),
  no client cert, no OPPO CA, no account binding. The receiver does not
  validate it.
* Data channel is the response body of
  `GET https://<ip>:<port>/download?taskId=…` (zip stream, `Server: VS-HTTP`).
  Other endpoints: `/download?taskId=X&fileId=Y`, `/thumbnail?taskId=X`,
  `/bigmessage?taskId=X&messageId=Y`, `/versionNegotiation`.
* Control channel is `wss://<ip>:<port>/websocket`, text frames
  `<flow>:<code>:<name>?<json>`, `flow ∈ ack|action`,
  `name ∈ versionNegotiation|sendRequest|status|statistic|wlanBandwidth|wlanChanged|logInstruction|extras|oliveSendProgress`.
* Received file landed in `/sdcard/Download/realme Share/` (10,106,590 B for
  10,090,372 B of TLS payload) — confirms the flow end to end.

URL builders: `eb/a.java`, `ra/b.java`; server: `fa/p.java` (HttpWlanServer),
`ib/c.java` (HttpP2pServer); router `y9/a.java`; file responses `z9/a.java`,
`z9/b.java`; messages `eb/*`.

**Conclusion:** the 互传联盟 leg needs no OPPO credentials. The certificate /
attestation material we found earlier belongs to the *account-paired PC/tablet*
channel (PantaConnect), which is a different, much harder path.

## 6. The BLE leg (code + partially verified)

Discovery is BLE only — during the iOS share page and the phone send page no
mDNS/UDP left the phone (verified with root `AF_PACKET`; only other vendors'
`_lyra-mdns` / `_companion-link` queries were visible).

Timeline from the vendor BT stack log (`/data/misc/bluetooth/logs/*.log`):

```
10:06:47 com.coloros.oshare StartScan filters=4
10:06:51 clientConnect -> 62:32:2A:CB:76:F9   (the Xiaomi)
10:06:51 com.heytap.accessory ×4 + oshare GATT servers connect
10:06:53 clientDisConnect                     (2 s exchange)
10:06:53 Wi-Fi Direct + TLS transfer begins
```

Advertisement the phone parses (`ble/impl/o.java`, built by `ble/impl/b.java`):

```
service uuid 0x8881 must be the *first* entry in the UUID list
service data 0x6666 (24B): [17][name 16B UTF-8][deviceId 6B][flags]
service data 0x6667 (20B): [flags][accountId 3B][accountName 16B]   (v>=17)
```

`flags` byte, decoded from `utils/x.java`:

```
( screenOn ? 1 : 2 ) | ( realme ? 16 : oneplus ? 8 : 4 ) | ( tablet ? 32 : fold ? 64 : 0 )
```

so realme → `0x11`, OPPO → `0x05`, OnePlus → `0x09`. A Linux peer should send
`0x05`. The scan-response byte always has bit0 set.

GATT (peer = server, phone = client):

| service | characteristics |
|---|---|
| `0x9999` | `0x9989` REQUEST_GO (notify, string ≥45 B), `0x9985` ACK_DIALOG (Base64), `0x9997` UPDATE, `0x9986` START_WPS_INFO (write), `0x9984`, `0x9895` |
| `0x9955` | `0x9953` PORT_FREQUENCY (write, JSON), `0x9954`, `0x9955` |
| `0xbb15` | DCP fallback |

Payload formats (code):

* `0x9986`: `<mac no colons>&<band>&11&<ssid>&<psk>&…` (`&` = `CARD_SPLIT`)
* `0x9953`: JSON with at least `port` (built by `za/c.java` `h()` / `i()`)

The GATT link is encrypted (bonded), so the payloads are **not** readable from
an HCI snoop without the LTK — the formats above come from the code, not from
decryption.

Vendor BT stack (native, `/system/lib64/libbluetooth_jni.so`, analysed with
rizin): `isOshare(RawAddress)` = `oplus_btif_is_comm_sharing_device(addr)` or
membership in the `persist.bluetooth.oshare` property list. It is only a
shortcut/whitelist — app-level advertisement parsing is what makes a device
appear in 「其他设备」.

## 7. Dead ends (documented so nobody repeats them)

* **Impersonating a paired PC/tablet** (PantaConnect): private keys live in
  AndroidKeyStore/TEE (`DeviceAttestationInfoRepositoryImpl.getKeyPair` →
  `KeyStore.getInstance("AndroidKeyStore").getKey(alias)`), peer attestation is
  verified through `com.oplus.ndsf.client` (TEE-backed). Copying
  `/data/user_de/0/com.heytap.accessory/databases/a_d` is useless. Dead end.
* **Windows client**: Electron 17.20.3, but all networking is native
  (`pantaChannelService.exe`, protobuf 3.11.4 + OPPO TLS chains); the JS only
  does IPC. The pairing QR (`getQrCodeUrl`) is a download URL, not a LAN token.
  See [PC-CLIENT.md](PC-CLIENT.md).
* **iOS IPA**: App Store needs a signed-in Apple ID; third-party mirrors do not
  carry `com.heytap.oshare` (v1.4.9, CN only). Even with the IPA, FairPlay
  encrypts `__TEXT`. The iOS protocol was recovered from the Android side
  instead (the phone is the iOS-mode server).

## 8. Evidence / captures

Two pcaps from the real transfers (attack vector: root `AF_PACKET` on `p2p0`):

* `cap_p2p.pcap` — OPPO → Xiaomi, 64 MB, phone = `.1` (GO, TLS server),
  Xiaomi = `.171`, data port 50856
* `cap_p2p2.pcap` — Xiaomi → OPPO, 10 MB, Xiaomi = `.1` (GO), phone = `.128`,
  data port 54377, file saved as `base.apk`

They are attached to the GitHub release
[`captures-2026-10-21`](https://github.com/Become-ILLUSORY/oshare-go/releases/tag/captures-2026-10-21)
(`cap.pcap` plus `cap_p2p.pcap` split into `p2p.part-*` because the upload path
resets around 60 MB):

```sh
cat p2p.part-aa p2p.part-ab p2p.part-ac p2p.part-ad p2p.part-ae > cap_p2p.pcap
sha256sum cap_p2p.pcap   # a8bc73c6d519e797d62bec43bf36274d2225eb90c03eaa53925426b7c732192b
```

Helper scripts used on the device are not committed; re-create them if needed:

* raw pcap writer — Go, `AF_PACKET` on an interface, writes classic pcap
* mDNS dumper — parses `_lyra` / `_adb` records, needed to identify which side of
  a Wi-Fi Direct group is the phone

## 9. Open tasks (in order)

1. **Run the BLE experiment on the CachyOS laptop.**
   ```sh
   sudo pacman -S bluez bluez-utils python-dbus python-gobject
   sudo systemctl enable --now bluetooth
   sudo python3 tools/oshare_ble.py scan -t 20     # reference adverts
   sudo python3 tools/oshare_ble.py serve --name LinuxBox
   ```
   Expected: the phone's 互传 → 发送页 → 其他设备 lists `LinuxBox`; tapping it
   should produce `write START_WPS_INFO` and `write PORT_FREQUENCY` on the
   laptop. These carry the Wi-Fi Direct SSID/PSK and the TLS port.
   If it does not appear, sweep `--adv-flags` (0x02/0x04/0x06/0x11) and compare
   with the phone's own advert from `scan`.

2. **Parse the credentials and join the group** (new Go code, `p2p/`):
   `wpa_cli p2p_connect` / `p2p_group_add`+`p2p_join`, or NetworkManager, then
   confirm the assigned address.

3. **Drive the transfer**: implement the receiver side against the phone's TLS
   server — `Transfer-Encoding: chunked`, `/versionNegotiation` then
   `/download?taskId=…`. A self-signed server cert must be accepted
   (`InsecureSkipVerify`). Reuse `transfer/` for framing once a socket exists.

4. **Sending to the phone** (reverse direction): the phone becomes the TLS
   client, so our side must host `wss://…/websocket` + `/download`. The control
   message sequence is in `ib/b.java` (receiver) and `fa/p.java` (server).

5. **Nice-to-have**: replace the Python BLE tool with a Go implementation
   (`go-bluetooth` or direct D-Bus) once the flow is proven, and move it under
   `ble/`.

## 10. Practical notes / traps

* In this sandbox, `pkill -f <pattern>` kills the caller's own shell (PRoot
  process appears in `/proc`); use `kill -9 $(pidof name)`.
* Multicast (mDNS, 239.255.255.250) is only received by **root** processes on
  Android; unprivileged apps need a `MulticastLock`.
* The phone only opens its OShare port when a transfer session exists; there is
  nothing listening in idle state.
* `adb shell` is `shell` uid; root via `su -c`. ADB over Wi-Fi is already
  authorised on the test device — `adb shell` from the sandbox works.
* Decompiled trees used during the investigation: `/tmp/src2` (oshare),
  `/tmp/src1` (linker), `/tmp/srcacc` (AccessoryFramework) — regenerate with
  `jadx -d out --no-res <apk>` if they are gone.
