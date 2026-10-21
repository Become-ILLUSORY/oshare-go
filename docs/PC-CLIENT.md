# The Windows client (O+ Connect 17.20.3) — what it is and what it tells us

Installer analysed: `OppoConnectSetup_17.20.3_domestic_261015182742_a7d984b.exe`
(360 MB NSIS, `O+ Connect` / `O+互联`, appBundleId `com.oplus.devicespace`).

```
O+Connect.exe                     Electron shell
resources/app.asar                renderer + bundled main process (127 MB -> 1089 files)
daemon/pantaChannelService.exe    PantaConnect channel service (transport, 3.4 MB, TLS certs)
daemon/SLDesktopAgent.exe         desktop agent
daemon/devicespace.exe            logger / crash service
daemon/oplus_remote_service.exe   remote control
daemon/adb.exe                    bundled adb
app_process/phoneCast.exe         cast
app_process/screenshare.exe       screen share
childprocess/ChildProcess.exe     sandboxed renderer
```

## Source maps

`js/*.js.map` ships `sourcesContent`, so the original Vue sources are recoverable:
2422 files under `OplusConnect/src/{main,render,common,shared}`.

## Where the network lives

The JavaScript layer **never opens a socket** — no `createServer`, no
`new WebSocket`, no `http` server anywhere in the bundle. Everything is an
`ipcManager.invoke(...)` into the native daemons, which do the actual
PantaConnect work (`pantaChannelService.exe` imports the OPPO TLS chain and
logs `deviceId / localHostName / remoteHostName`).

## Cross-file RPC (phone filesystem browsing)

The renderer calls the phone's file service through IPC with this envelope:

```json
{ "api": "file.root",
  "header": { "clientVersion": "1.0.0", "apilevel": "1" },
  "body": { } }
```

Response shape: `response.result.code == 200`, `result.data.result[]`,
`result.data.total`.

Known APIs (`src/render/views/crossFile/service`):

| api | purpose |
|---|---|
| `file.root` | storage roots / menu |
| `file.category.query` | paginated list of a category |
| `file.path.list` | list a directory |
| `file.select.path` | folder picker helper |
| `file.recently.list` | recent files |
| `album.list` | photo albums |
| `album.set` | album management |
| `app.permissions` | permission probe |
| `sync.file.state` | sync state |

Transfer itself is not an HTTP upload: `download`/`upload` tasks are driven by
IPC task ids (`taskId`, `fileId`, `state`, `progress`) and the bytes are moved by
the native channel, not by JS.

## Pairing

* Same OPPO account + same WLAN (or the phone's hotspot) → auto connect.
* Otherwise the PC shows a QR (`getQrCodeUrl`) that the phone scans; the QR is a
  **download/help URL**, not a LAN token — no address, port or secret is encoded
  in it.

## Consequence

The PC client is not a LAN server a third-party program can impersonate without
the OPPO interconnect stack: transport = native PantaConnect channel with TLS
certificates, device pairing and account binding. Everything above it (the
`file.*` RPCs, and the `&&_&&` command protocol documented in
[PROTOCOL.md](../PROTOCOL.md)) is straightforward once such a channel exists,
but the channel itself is the wall.

Practical options for a Linux host:

1. **oshare-go peer-to-peer** (this repository) — full protocol between hosts
   that run it, no OPPO software involved.
2. **adb bridge** — the phone's own ADB over Wi-Fi moves files at LAN speed and
   needs no pairing beyond the developer-options handshake.
3. **Reimplement the interconnect** — needs OPPO account auth, device
   certificates (`oplus_prod_cert_chain`) and the SRP handshake in
   `libomes_srp_si.so`; out of scope for a port.
