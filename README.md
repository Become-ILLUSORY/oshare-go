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
```

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

## Compatibility notes

* File transfer between two hosts running this library works out of the box
  (see `transfer/transfer_test.go`).
* Talking to a ColorOS/OPPO phone needs two extra steps that this repository
  cannot provide: the phone ignores unpaired peers on the PantaConnect socket,
  and the app only opens its `8959` file socket once the pairing/soft-AP
  service has handed it a device. The protocol above is what runs on that
  socket once it exists.
* The `port + 1` raw fast path is not implemented; the framed transfer
  channel (`type 8192`) is the one this library uses and the one the Android
  decoder always accepts.
* Path traversal is blocked: received names are cleaned and confined to the
  destination directory, existing files get a `-1`, `-2`, … suffix unless
  `Overwrite` is set.

## License

MIT
