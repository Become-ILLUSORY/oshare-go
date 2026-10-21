#!/usr/bin/env python3
"""Receive files from the phone: dial its wss:// endpoint and unzip /download.

This is the reverse direction. The phone is the sender, so it runs the
HttpWlanServer (fa/p.java): it announces versionNegotiation, then sendRequest,
and serves the payload as a chunked ZIP on /download?taskId=<id>.

    sudo tools/p2p_join.sh                 # join the phone's P2P group first
    python3 tools/oshare_recv.py --host 192.168.49.1 --port 50856

The port is the one the phone reported in its 0x9989 notification, which we
cannot receive (no CCCD), so it is passed in explicitly; without it the script
scans the usual 40000-65535 range for a TLS endpoint that speaks /websocket.

Exit status is 0 when at least one file was written.
"""
import argparse
import base64
import hashlib
import io
import json
import os
import socket
import ssl
import struct
import sys
import time
import zipfile

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="192.168.49.1")
ap.add_argument("--port", type=int, default=0, help="0 = scan for it")
ap.add_argument("--dir", default="./received")
ap.add_argument("--timeout", type=float, default=15.0)
ap.add_argument("--scan-from", type=int, default=40000)
ap.add_argument("--scan-to", type=int, default=65535)
args = ap.parse_args()


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def tls(host, port, timeout):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # the peer uses CN=localhost
    raw = socket.create_connection((host, port), timeout=timeout)
    return ctx.wrap_socket(raw, server_hostname="localhost")


def ws_frame(payload: bytes, opcode=0x1):
    n = len(payload)
    if n < 126:
        return bytes([0x80 | opcode, n]) + payload
    if n < 65536:
        return bytes([0x80 | opcode, 126]) + struct.pack(">H", n) + payload
    return bytes([0x80 | opcode, 127]) + struct.pack(">Q", n) + payload


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def read_frame(sock):
    b1, b2 = recv_exact(sock, 2)
    opcode = b1 & 0x0F
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if b2 & 0x80 else b"\x00" * 4
    data = bytearray(recv_exact(sock, ln))
    for i in range(ln):
        data[i] ^= mask[i % 4]
    return opcode, bytes(data)


def send_msg(sock, flow, name, code=0, payload=None):
    text = f"{flow}:{code}:{name}"
    if payload is not None:
        text += "?" + json.dumps(payload, separators=(",", ":"))
    sock.sendall(ws_frame(text.encode()))
    log(f"[ws] -> {text}")


def parse_msg(text):
    head, sep, body = text.partition("?")
    parts = head.split(":")
    if len(parts) < 3:
        return None
    msg = {"flow": parts[0], "name": parts[2]}
    if sep:
        try:
            msg["json"] = json.loads(body)
        except json.JSONDecodeError:
            pass
    return msg


def read_http(path_sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = path_sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed during header")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode(errors="replace").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers, rest


def dechunk(sock, first):
    """Yield the body of a chunked response, then any trailer."""
    buf = first
    while True:
        while b"\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                return
            buf += chunk
        line, _, buf = buf.partition(b"\r\n")
        try:
            size = int(line.split(b";")[0], 16)
        except ValueError:
            return
        if size == 0:
            return
        while len(buf) < size + 2:
            chunk = sock.recv(65536)
            if not chunk:
                return
            buf += chunk
        yield buf[:size]
        buf = buf[size + 2:]


def probe_ws(host, port, timeout):
    """A port is ours if /websocket answers 101."""
    try:
        s = tls(host, port, timeout)
    except (OSError, ssl.SSLError):
        return False
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(("GET /websocket HTTP/1.1\r\nHost: %s:%d\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                   % (host, port, key)).encode())
        s.settimeout(3)
        head = s.recv(200)
        return head.startswith(b"HTTP/1.1 101")
    except (OSError, ssl.SSLError):
        return False
    finally:
        s.close()


def find_port(host):
    log(f"scanning {args.scan_from}-{args.scan_to} on {host} for /websocket")
    for port in range(args.scan_from, args.scan_to + 1):
        if probe_ws(host, port, 0.4):
            log(f"found TLS+websocket on {port}")
            return port
        if port % 2000 == 0:
            log(f"  ... {port}")
    return None


def save_zip(body, outdir):
    written = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile:
        path = os.path.join(outdir, f"raw-{int(time.time())}.bin")
        with open(path, "wb") as fh:
            fh.write(body)
        log(f"[file] not a zip, saved {path} ({len(body)}B)")
        return [path]
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = os.path.basename(info.filename)
        if not name:
            continue
        path = os.path.join(outdir, name)
        base, ext = os.path.splitext(path)
        i = 1
        while os.path.exists(path):
            path = f"{base}-{i}{ext}"
            i += 1
        with open(path, "wb") as fh:
            fh.write(zf.read(info))
        log(f"[file] {path} ({info.file_size}B)")
        written.append(path)
    return written


def main():
    host = args.host
    port = args.port or find_port(host)
    if not port:
        log("no OShare endpoint found; is the phone on its send page?")
        return 1
    os.makedirs(args.dir, exist_ok=True)

    log(f"connecting wss://{host}:{port}/websocket")
    ws = tls(host, port, args.timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    ws.sendall(("GET /websocket HTTP/1.1\r\nHost: %s:%d\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                % (host, port, key)).encode())
    head = b""
    while b"\r\n\r\n" not in head:
        head += ws.recv(4096)
    if not head.startswith(b"HTTP/1.1 101"):
        log("upgrade refused:", head.split(b"\r\n")[0])
        return 1
    log("[ws] upgraded")

    task = None
    deadline = time.time() + 120
    while time.time() < deadline:
        ws.settimeout(deadline - time.time())
        op, payload = read_frame(ws)
        if op == 0x8:
            log("[ws] server closed")
            break
        if op != 0x1:
            continue
        text = payload.decode(errors="replace")
        log(f"[ws] <- {text}")
        msg = parse_msg(text)
        if not msg:
            continue
        if msg["name"] == "versionNegotiation":
            ver = (msg.get("json") or {}).get("versions", [1])[0]
            send_msg(ws, "ack", "versionNegotiation", payload={"version": ver})
        elif msg["name"] == "sendRequest":
            info = msg.get("json") or {}
            task = info.get("id", "0001")
            log(f"[ws]    sending {info.get('fileCount')} file(s), "
                f"{info.get('totalSize')}B, mime {info.get('mimeType')}")
            send_msg(ws, "ack", "sendRequest")
            break

    if task is None:
        log("no sendRequest received")
        return 1

    log(f"GET /download?taskId={task}")
    dl = tls(host, port, args.timeout)
    dl.sendall((f"GET /download?taskId={task} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\nConnection: close\r\n\r\n").encode())
    status, headers, rest = read_http(dl)
    log(f"[http] {status} {headers.get('content-type')} "
        f"{headers.get('transfer-encoding', '')}")
    if status != 200:
        log("download refused")
        return 1

    body = b""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        for piece in dechunk(dl, rest):
            body += piece
    else:
        body = rest
        while True:
            chunk = dl.recv(65536)
            if not chunk:
                break
            body += chunk
    dl.close()
    log(f"[http] received {len(body)}B")

    written = save_zip(body, args.dir)
    send_msg(ws, "action", "status", payload={"taskId": task, "type": 1})
    try:
        ws.close()
    except OSError:
        pass
    log(f"done: {len(written)} file(s) in {args.dir}")
    return 0


sys.exit(main())
