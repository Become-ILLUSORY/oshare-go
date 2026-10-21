#!/usr/bin/env python3
"""Serve the OShare receive flow with a real ZIP so the phone can finish.

Sequence (fa/p.java as server, ib/b.java as client, eb/d.java for the schema):

    action:0:versionNegotiation?{"versions":[1]}
    ack:0:versionNegotiation?{"version":1}
    action:0:sendRequest?{id,senderId,senderName,fileName,mimeType,fileCount,
                          version,totalSize,thumbnail*,wlan}
    -> the phone pops its accept dialog and answers ack:0:sendRequest
    GET /download?taskId=<id>  ->  chunked application/zip
    action:0:status?{"taskId":<id>,"type":1}      (1 = ok; never 2 mid-flight)

usage: oshare_ws.py --port 50856 [--file PATH] [--delay 1.5]
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
import threading
import time
import zipfile

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
VERSIONS = [1]
LOGFILE = None

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=50856)
ap.add_argument("--cert", default="/tmp/oshare-srv.crt")
ap.add_argument("--key", default="/tmp/oshare-srv.key")
ap.add_argument("--delay", type=float, default=1.5,
                help="seconds between the upgrade and versionNegotiation")
ap.add_argument("--file", default=None,
                help="file to serve; defaults to a small generated one")
ap.add_argument("--task-id", default="0001")
ap.add_argument("--log", default="/tmp/oshare-ws.log",
                help="also append every line here (world-readable)")
ap.add_argument("--stall-download", action="store_true",
                help="log /download and never answer, to see who times out")
args = ap.parse_args()


def log(*a):
    line = " ".join([time.strftime("[%H:%M:%S]"), *[str(x) for x in a]])
    print(line, flush=True)
    if LOGFILE:
        try:
            with open(LOGFILE, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def read_http_head(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed during header")
        data += chunk
        if len(data) > 65536:
            raise ConnectionError("header too large")
    head, rest = data.split(b"\r\n\r\n", 1)
    lines = head.decode(errors="replace").split("\r\n")
    method, path, _ver = lines[0].split(" ", 2)
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return method, path, headers, rest


def ws_frame(payload: bytes, opcode=0x1):
    n = len(payload)
    if n < 126:
        return bytes([0x80 | opcode, n]) + payload
    if n < 65536:
        return bytes([0x80 | opcode, 126]) + struct.pack(">H", n) + payload
    return bytes([0x80 | opcode, 127]) + struct.pack(">Q", n) + payload


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


def parse_msg(text):
    head, sep, body = text.partition("?")
    parts = head.split(":")
    if len(parts) < 3:
        return None
    msg = {"flow": parts[0], "code": parts[1], "name": parts[2]}
    if sep:
        try:
            msg["json"] = json.loads(body)
        except json.JSONDecodeError:
            msg["raw"] = body
    return msg


def build_payload():
    """File to send plus the sendRequest fields describing it.

    The bytes here are the *content*; zip_stream() wraps them in the zip the
    phone expects on /download, so a --file is sent as-is inside one entry.
    """
    if args.file:
        with open(args.file, "rb") as fh:
            data = fh.read()
        name = os.path.basename(args.file)
    else:
        data = ("hello from oshare-go\n" * 8).encode()
        name = "oshare-hello.txt"
    return data, name


def zip_stream(data, name):
    """Zip the payload the way ea/a.java does: a single entry, streamed."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, data)
    return buf.getvalue()


class Chan:
    def __init__(self, sock):
        self.sock = sock
        self.mu = threading.Lock()

    def send(self, flow, name, code=0, payload=None):
        text = f"{flow}:{code}:{name}"
        if payload is not None:
            text += "?" + json.dumps(payload, separators=(",", ":"))
        with self.mu:
            self.sock.sendall(ws_frame(text.encode()))
        log(f"[ws] -> {text}")


def handle_ws(sock, send_req):
    ch = Chan(sock)
    threading.Timer(args.delay, lambda: ch.send(
        "action", "versionNegotiation", payload={"versions": VERSIONS})).start()
    try:
        while True:
            op, payload = read_frame(sock)
            if op == 0x8:
                log("[ws] close frame from peer")
                return
            if op == 0x9:
                sock.sendall(ws_frame(payload, 0xA))
                continue
            if op != 0x1:
                log(f"[ws] opcode {op:#x} len {len(payload)}")
                continue
            text = payload.decode(errors="replace")
            log(f"[ws] <- {text}")
            msg = parse_msg(text)
            if not msg:
                continue
            name, flow = msg["name"], msg["flow"]
            if flow == "ack" and name == "versionNegotiation":
                ver = (msg.get("json") or {}).get("version", VERSIONS[0])
                log(f"[ws]    negotiated version {ver}")
                ch.send("action", "sendRequest", payload=send_req)
            elif flow == "ack" and name == "sendRequest":
                log("[ws]    phone accepted the transfer request"
                    " (it should now GET /download)")
            elif flow == "action" and name == "status":
                log(f"[ws]    peer status {msg.get('json')}")
            else:
                log(f"[ws]    unhandled {flow}:{name}")
    except (ConnectionError, OSError) as e:
        log("[ws] closed:", e)


def serve_download(sock, path, data, name):
    body = zip_stream(data, name)
    log(f"[http] /download -> {len(body)}B zip, 1 entry")
    head = (
        "HTTP/1.1 200 OK\r\n"
        "Server: VS-HTTP\r\n"
        "Content-Type: application/zip\r\n"
        "Content-Disposition: attachment; filename=\"files.zip\"\r\n"
        "Transfer-Encoding: chunked\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    sock.sendall(head)
    # Chunked: 8 KiB pieces, then the terminating zero chunk.
    for i in range(0, len(body), 8192):
        piece = body[i:i + 8192]
        sock.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
    sock.sendall(b"0\r\n\r\n")
    log(f"[http] sent {len(body)}B for {path}")


def handle_client(sock, addr, send_req, data, name):
    peer = f"{addr[0]}:{addr[1]}"
    log(f"[tls] connection from {peer}")
    try:
        method, path, headers, _rest = read_http_head(sock)
        log(f"[http] {method} {path}")
        for k in ("host", "upgrade", "sec-websocket-key", "user-agent",
                  "transfer-encoding", "range"):
            if k in headers:
                log(f"[http]   {k}: {headers[k]}")

        if headers.get("upgrade", "").lower() == "websocket":
            key = headers.get("sec-websocket-key", "").encode()
            accept = base64.b64encode(hashlib.sha1(key + GUID).digest()).decode()
            sock.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                         b"Upgrade: websocket\r\n"
                         b"Connection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n")
            handle_ws(sock, send_req)
            return

        if path.startswith("/download"):
            if args.stall_download:
                log("[http] stalling (--stall-download)")
                time.sleep(120)
                return
            serve_download(sock, path, data, name)
            return

        body = b'{"result":0}'
        sock.sendall(b"HTTP/1.1 200 OK\r\n"
                     b"Content-Type: application/json\r\n"
                     b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                     b"Connection: close\r\n\r\n" + body)
        log(f"[http] answered 200 for {path}")
    except (ConnectionError, OSError) as e:
        log(f"[tls] {peer}: {e}")
    finally:
        try:
            sock.close()
        except OSError:
            pass


def main():
    global LOGFILE
    LOGFILE = args.log
    try:
        open(LOGFILE, "w").close()
        os.chmod(LOGFILE, 0o644)
    except OSError:
        LOGFILE = None

    data, name = build_payload()
    send_req = {
        "id": args.task_id,
        "senderId": "0123456789abcdef",
        "senderName": "LinuxBox",
        "fileName": name,
        "mimeType": "file/*",
        "fileCount": 1,
        "version": str(VERSIONS[0]),
        "totalSize": len(data),
        "thumbnail": "",
        "thumbnail_height": 0,
        "thumbnail_width": 0,
        "wlan": True,
    }
    log(f"payload: {name} ({len(data)}B)")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(args.cert, args.key)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", args.port))
    srv.listen(8)
    log(f"listening on {args.port} (TLS, /websocket + /download)")
    while True:
        raw, addr = srv.accept()
        try:
            tls = ctx.wrap_socket(raw, server_side=True)
        except ssl.SSLError as e:
            log(f"[tls] handshake failed from {addr}: {e}")
            raw.close()
            continue
        threading.Thread(target=handle_client,
                         args=(tls, addr, send_req, data, name),
                         daemon=True).start()


main()
