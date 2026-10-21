# oshare-go

ColorOS / OPPO **互传 (OShare)** LAN file transfer, re-implemented in Go.
Pure standard library, no cgo, runs on Linux (amd64 / arm64 / armv7),
macOS and Windows.

The protocol was recovered from `com.coloros.oshare` **17.2.36** and
`com.oplus.linker` **17.7.30**; the wire format, command table and the
reverse-engineered details are documented in [PROTOCOL.md](PROTOCOL.md).

```
protocol/   frame codec: magic 1001, command / file / keepalive frames
transfer/   sender (Push) and receiver (Receive) roles, keepalive, sanitising
discovery/  mDNS (_oshare._tcp) responder and browser, no dependencies
cmd/oshare/ CLI: send, recv, discover
tools/      Linux <-> ColorOS phone: BLE advert, P2P GO/join, wss + /download
```

**Works with a real ColorOS phone** — see
[Talking to a real ColorOS / realme phone](#talking-to-a-real-coloros--realme-phone).

## Build

```sh
go build ./...
go test ./...
go build -trimpath -ldflags "-s -w" -o oshare ./cmd/oshare
```

Cross-compile for a NAS or a router:

```sh
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o oshare-arm64 ./cmd/oshare
CGO_ENABLED=0 GOOS=linux GOARCH=arm GOARM=7 go build -o oshare-armv7 ./cmd/oshare
```

## Use

Receive into `~/oshare` and announce yourself on the LAN:

```sh
oshare recv -dir ~/oshare -port 8959
```

Send to an address, or pick a discovered device by name:

```sh
oshare discover                       # list _oshare._tcp peers
oshare send -to 192.168.1.23:8959 ~/Pictures/*.jpg video.mp4
oshare send -peer "Pixel" -dir /sdcard/Download photo.jpg
oshare send -to 192.168.1.23:8959 -text "https://example.com"
```

`-yes` accepts transfers without asking; without it the CLI prompts on a TTY
and auto-accepts when stdin is not a terminal.

## Library

```go
conn, _ := net.Dial("tcp", "192.168.1.23:8959")
sess := transfer.NewSession(conn, transfer.Options{
    Device: transfer.Device{Name: "nas", ID: "0123456789abcdef"},
})
defer sess.Close()

res, err := transfer.Push(ctx, sess, transfer.PushRequest{
    Items:    []transfer.FileItem{{Path: "/srv/media/clip.mp4"}},
    DirHint:  "/sdcard/Download",
    Progress: func(sent, total int64) { /* … */ },
})
```

The receiver side:

```go
res, err := transfer.Receive(ctx, sess, transfer.ReceiveOptions{
    Dir: "/srv/incoming",
    Decide: func(ann protocol.Announce) (transfer.Decision, string) {
        if ann.TotalSize > freeSpace("/srv/incoming") {
            return transfer.NoSpace, ""
        }
        return transfer.Allow, ""
    },
})
```

## Talking to a real ColorOS / realme phone

**This works end to end** — verified on a realme GT7 Pro (ColorOS 17) on
2026-10-21: the phone lists the Linux box, the user taps it once, and the file
lands in `Documents/realme Share/`. No phone-side modification, no root on the
phone.

### Quick start (send a file to the phone)

```sh
sudo pacman -S bluez bluez-utils dnsmasq python-gobject iptables   # CachyOS
sudo systemctl enable --now bluetooth

sudo ./tools/oshare-go.sh -f ~/Pictures/photo.jpg
```

Then on the phone: 互传 → 发送 → pick a file → tap **LinuxBox**.

That one command does everything:

1. takes `wlan0` from NetworkManager and starts a private `wpa_supplicant`;
2. creates a Wi-Fi Direct **group owner** (`DIRECT-aa-fastCon`, random PSK)
   with `192.168.49.1/24` and a DHCP server;
3. opens a firewall hole for the group interface and stops the TUN proxy so
   the route to the phone is not hijacked;
4. serves `wss://192.168.49.1:<port>/websocket` + `GET /download` with a
   self-signed certificate;
5. advertises over BLE (`0x8881` + service data `0x6666`/`0x6667`);
6. when the phone writes its share request, connects back, reads `0x9954`
   and writes the port/SSID/PSK to `0x9953` — that is why the phone then
   joins *our* group and dials *our* port;
7. waits for the transfer and shuts everything down.

`Ctrl-C` restores Wi-Fi and the proxy.

### Receive a file from the phone

The phone is the sender and the group owner, so join its group first:

```sh
sudo ./tools/p2p_join.sh                 # reads the current PSK over adb
python3 tools/oshare_recv.py --host 192.168.49.1 --dir ./received
```

`--port` is optional; without it the script scans for a port that answers the
WebSocket upgrade (the phone only announces it in a BLE notification we cannot
subscribe to, see `docs/BLE.md`).

### Doing it by hand

Each step of the one-command script is usable on its own:

```sh
sudo ./tools/p2p_go.sh "DIRECT-aa-fastCon" "12345678" 50856   # GO + DHCP + TLS/WS
python3 tools/oshare_ble.py serve --name LinuxBox --no-ack \
        --share-port 50856 --share-ssid DIRECT-aa-fastCon --share-psk 12345678
python3 tools/oshare_ble.py scan -t 20                        # dump adverts

python3 tools/push_json.py --match 真我 --port 50856 \
        --ssid DIRECT-aa-fastCon --psk 12345678 --freq 2437   # push AP info only
python3 tools/oshare_ws.py --port 50856 --file ~/movie.mp4    # server only
```

The full protocol walk-through, the failed approaches and the evidence are in
[docs/TO-PHONE.md](docs/TO-PHONE.md); the BLE advertisement layout and the GATT
profile are in [docs/BLE.md](docs/BLE.md).

A step-by-step walkthrough (setup, every tool on its own, troubleshooting) is
in **[docs/USAGE.md](docs/USAGE.md)** (Chinese).

### Requirements and caveats

* Linux with a Wi-Fi adapter that supports `P2P-GO` (`iw list` →
  `Supported interface modes`) and BlueZ ≥ 5.65.
* The tools are Python 3 + `python-gobject`; no `python-dbus`.
* The BLE side talks to `bluetoothd` over the system bus, so it does **not**
  need root — only the P2P/DHCP part does.
* Changing the service layout requires clearing the phone's GATT cache
  (`adb shell su -c 'rm -f /data/misc/bluetooth/gatt_*'` +
  `svc bluetooth disable/enable`), otherwise it keeps the old attribute table.
* The phone's own save directory for received files is
  `Documents/realme Share/` (`Download/realme Share/` on older builds).

## Compatibility notes

* File transfer between two hosts running this library works out of the box
  (see `transfer/transfer_test.go`).
* Talking to a ColorOS/OPPO phone over the PantaConnect (account-paired) route
  does **not** work — the phone ignores unpaired peers there. The Wi-Fi
  Direct route above is the one that works, and it needs no OPPO credentials.
* The `port + 1` raw fast path is not implemented; the framed transfer
  channel (`type 8192`) is the one this library uses and the one the Android
  decoder always accepts.
* Path traversal is blocked: received names are cleaned and confined to the
  destination directory, existing files get a `-1`, `-2`, … suffix unless
  `Overwrite` is set.

## License

MIT
