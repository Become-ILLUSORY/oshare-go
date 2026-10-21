# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

A clean-room Go re-implementation of **ColorOS / OPPO 互传 (OShare)** LAN file
transfer, reverse engineered from:

* `com.coloros.oshare` **17.2.36** (the 互传 app)
* `com.oplus.linker` **17.7.30** (O+ Connect)
* `AccessoryFramework.apk` **17.6.16** (`com.oplus.pantaconnect` — the pairing /
  interconnect stack, 5566 decompiled classes)
* `O+ Connect 17.20.3` for Windows (Electron + native `pantaChannelService.exe`)

Read [docs/HANDOFF.md](docs/HANDOFF.md) first — it has the full state of the
investigation, the evidence, and the open tasks. Protocol details live in
[PROTOCOL.md](PROTOCOL.md), the PC client in
[docs/PC-CLIENT.md](docs/PC-CLIENT.md), the BLE layer in
[docs/BLE.md](docs/BLE.md).

## Layout

```
protocol/     frame codec (magic 1001, types 4096/8192/16384), command table
transfer/     sender (Push) / receiver (Receive), keepalive, path sanitising
discovery/    dependency-free mDNS responder + browser for _oshare._tcp
cmd/oshare/   CLI: send / recv / discover
tools/        BlueZ helpers for the BLE experiment (Python)
docs/         BLE.md, PC-CLIENT.md, HANDOFF.md
```

## Commands

```sh
go test ./...                      # unit + integration (loopback TCP)
go vet ./... && gofmt -l .
go build -o oshare ./cmd/oshare
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build ./cmd/oshare   # phone/NAS
```

CI (`.github/workflows/ci.yml`) runs gofmt/vet/`go test -race` on Go 1.22 and
1.23 and cross-builds seven targets; tags `v*` publish a release.

## Conventions

* Pure standard library in Go — no external modules. Keep it that way unless
  there is a strong reason.
* Wire format is big-endian, verified against `mb/f.java` (encoder) and
  `mb/d.java` (decoder); `protocol/frame_test.go` holds byte-exact fixtures.
  Do not "tidy" the lengths: command/keepalive frames carry the payload length
  while file frames carry the **whole frame** length.
* Multi-field command payloads are joined with the literal `&&_&&`.
* Commits use `Become-ILLUSORY <becomeillusory@gmail.com>`.

## Message for the next agent

**Read [docs/TO-CLAUDE.md](docs/TO-CLAUDE.md) first.** It contains the state of
the BLE bring-up: the phone already discovers the Linux peer and reads/writes
our characteristics; the blocker is the ACK, which is a *write* of `0x01` to the
phone's `0x9985` (not a notification — ColorOS never writes a CCCD, so BlueZ
will never emit `PropertiesChanged`). `tools/oshare_ble.py` now does that from
`ack_peer()`, including connection retries.

## Current focus

The transfer protocol is done and tested. The remaining work is making a
**Linux box discoverable by an unmodified ColorOS phone**:

1. `tools/oshare_ble.py serve` advertises as a share peer and hosts the GATT
   server — needs a real run on a laptop with a BLE adapter (CachyOS).
2. Confirm the phone lists the peer and writes `START_WPS_INFO` /
   `PORT_FREQUENCY`; those carry the Wi-Fi Direct credentials + port.
3. Join the group and drive `transfer/`'s TLS client against
   `https://<phone>:<port>/download?taskId=…`.

See "Open tasks" at the end of `docs/HANDOFF.md` for the exact checklist.
