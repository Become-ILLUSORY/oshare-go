# OShare (ColorOS 互传) LAN protocol

Everything below was recovered by decompiling **`com.coloros.oshare` 17.2.36**
(`/tmp/src2`, jadx) and cross-checked against **`com.oplus.linker` 17.7.30**.
Class names in brackets are the decompiled sources the fact comes from.

Status legend: **verified** = read directly out of the encoder/decoder,
**inferred** = semantics reconstructed from the surrounding UI/handler code.

---

## 1. Layers

```
+-------------------------------------------------------------+
| app UI (Send/Receive dialogs, seeding cards)                 |
+-------------------------------------------------------------+
| control protocol  - big endian binary frames over TCP        |  mb/* (MINA)
|   magic 1001, command/file/keepalive frames                  |
+-------------------------------------------------------------+
| transport                                                     |
|   Android: PantaConnect LAN connection service,              |  pantaconnect/*
|   i.e. a system-service socket (auth + crypto live there)     |
|   Plain Wi-Fi: the app's own TCP socket on port 8959 (50489)  |  mb/n.java, mb/j.java
+-------------------------------------------------------------+
| discovery: mDNS / DNS-SD service "_oshare._tcp"              |  u9/n.java, u9/a.java
+-------------------------------------------------------------+
```

The file-transfer protocol itself is **not** encrypted and carries no
authentication: confidentiality is delegated to the PantaConnect socket the
system hands to the app. A re-implementation therefore speaks the protocol
fine over a plain TCP connection between two cooperating hosts, but it cannot
join an already-paired phone-to-phone session without the system service.

## 2. Discovery (mDNS)

[u9/n.java, u9/a.java]

| field        | value                                              |
|--------------|----------------------------------------------------|
| service type | `_oshare._tcp` (`"_oshare._tcp."` after join)      |
| instance     | `INSTANCE_NAME` + suffix                           |
| port         | `50489` registered through `NsdManager`            |
| TXT          | `SERVICE_NAME=oshare`, `SERVICE_MAC=<mac, no ':'>` |

Standard DNS-SD: PTR `_oshare._tcp.local` → instance, SRV instance →
`host.local:port`, TXT instance → key/values, A `host.local` → IPv4.
Android's `NsdManager` is a plain mDNS responder, so any mDNS stack sees it.

## 3. Frame format (verified)

[mb/f.java encoder, mb/d.java decoder, mb/i.java file header]

All integers are big-endian (`IoBuffer.putInt/putLong` defaults).

```
frame := magic:int32 type:int32 length:int64 payload
magic := 1001 (0x000003E9)
```

### 3.1 command (type `4096`)

```
payload := cmd:int32  nameLen:int32  name[nameLen] utf8  flag:int32
length  := 12 + nameLen          // payload only, header excluded
```

* `name` is the command *payload*; multi-field payloads are joined with the
  literal separator `&&_&&` [pb/a.java].
* `flag` is `1` for every command the app builds through `pb/c.java`
  (`g(1)`, `e(true)`).
* A keepalive command is `cmd=0`, empty name, `flag=1`.

### 3.2 keepalive (type `16384`)

```
payload := value:int32      // 1 = request, 2 = response
length  := 4
```

[mb/s.java] `KeepAliveFilter`: request every 10 s, timeout 30 s, idle 10 s,
and the session attribute `keep_alive` must be true.

### 3.3 file (type `8192`)

```
payload := bodyLen:int64  nameLen:int32 name[nameLen] utf8
           fileType:int32 size:int64 flag:int32   <size bytes of data>
bodyLen := 28 + nameLen      // counts bodyLen itself
length  := 44 + nameLen      // whole frame, header included (unlike 3.1/3.2)
```

`name` is the proposed destination path/name, `fileType` is a category the
Android app always sends as `0` for regular files, `size` is the exact byte
count that follows.

`flag` bits [mb/d.java, pb/c.java]:

| bit        | meaning                                                        |
|------------|----------------------------------------------------------------|
| `0x02|0x04`| directory: the receiver prepends its base directory            |
| `0x10`     | source was a `java.io.File` (not a content `Uri`)              |
| `0x20`     | payload was converted/re-encoded by the sender                 |

The decoder emits an internal "file complete" event (`12288`) once exactly
`size` bytes have been written; the next frame header follows immediately, so
the reader must stop after `size` bytes and not consume more.

## 4. Commands

Decoded from the sender (`r9/g.java`, internally "P2pServer") and the receiver
(`r9/b.java`, "P2pClient"). `S` = sender, `R` = receiver.

| id | name              | dir | payload                                                        |
|----|-------------------|-----|----------------------------------------------------------------|
| 0  | keepalive         | any | empty, flag 1                                                  |
| 1  | announce          | S→R | `count, deviceName, totalSize, dirHint, dataType, [version], [accountId], [verifyKey]` |
| 2  | ack allow         | R→S | data type that was accepted                                    |
| 3  | ack reject        | R→S | `reject` (user) or `cancel`                                    |
| 4  | ack timeout       | R→S | `TEST`                                                        |
| 5  | transfer over     | S→R | `count, dataType, dirHint, entrance`                            |
| 5  | transfer over     | R→S | `dataType` (confirmation once all bytes are on disk)           |
| 6  | no space          | R→S | `TEST`                                                        |
| 7  | cancel            | S→R or R→S | `TEST`                                                |
| 8  | cancel ok         | peer | `TEST`                                                       |
| 9  | thumbnail         | S→R | encoded preview (first 8 chars are the cache key)              |
| 10 | short text        | S→R | the shared text                                                |
| 11 | client info       | R→S | `deviceId, unknown, protocolVersion` (sent right after connect) |
| 12 | progress          | R→S | percentage as decimal string                                   |
| 13 | nothing           | S→R | `TEST`                                                        |
| 21 | server info       | S→R | `deviceId` (reply to 11)                                      |
| 22 | log instruction   | any | log directive, no protocol effect                              |

`TEST` is the literal dummy string the app uses where the payload is unused.

### 4.1 Flow

```
R                                   S
|  cmd 11 (client info) ----------->|
|<---------------- cmd 21 (server info)
|                                   |
|<---------------- cmd 1  (announce: count, name, total, hint, type)
|  cmd 2 (allow, type) ------------>|
|  ... or cmd 3 / 4 / 6             |   (reject / timeout / no space)
|                                   |
|<---------------- file frame  (repeated, count times)
|                                   |
|<---------------- cmd 5 (count, type, hint, entrance)
|  cmd 5 (type) ------------------->|
```

Cancel: either side sends `cmd 7`, the peer answers `cmd 8`. Progress is
reported by the *receiver* with `cmd 12`. Text-only transfers skip the file
frames and use `cmd 10` before `cmd 5`.

## 5. Ports and connection setup

[mb/n.java, mb/j.java]

* The receiving side binds the control port (`8959` by default, or the port
  handed out by the PantaConnect/soft-ap service) and a second "file socket"
  on `port + 1` (`o` thread, raw `SocketChannel`s, 4 MiB receive buffer).
* The sender dials `port + 1` and retries up to 5 times per round, 10 rounds,
  with a 1.5 s back-off; it writes `cmd 0` (keepalive) immediately.
* mDNS advertises `50489`; the app's own default when no service is present
  is `8959`.

## 6. What this repository implements

* `protocol` — the frame codec (3.1–3.3), command constants and the
  `&&_&&` payload helpers.
* `transfer` — the sender (`Push`) and receiver (`Receive`) roles with the
  flow from 4.1, keepalive handling, path sanitising and progress callbacks.
* `discovery` — a dependency-free mDNS responder/browser for `_oshare._tcp`.
* `cmd/oshare` — CLI (`send`, `recv`, `discover`).

Not implemented, because it needs the ColorOS system service: the PantaConnect
transport (device pairing, certificates from
`assets/crypto_android_sdk/oplus_prod_cert_chain`, SRP-based key exchange in
`libomes_srp_si.so`) and the raw `port+1` fast path, whose writer was not
decompilable (`Method not decompiled: mb.a.RunnableC0234a.e`).
