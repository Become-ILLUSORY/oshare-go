#!/usr/bin/env python3
"""Minimal OShare HTTPS + WebSocket server to see what the phone sends.

TLS serves two things on one port, matching the captures:

    wss://<us>:<port>/websocket        control channel, text frames
                                       "<flow>:<code>:<name>?<json>"
    GET    <us>:<port>/download?...    data channel (not implemented yet)

Run with the self-signed cert and point the phone at the port:
    python3 tools/oshare_ws.py --port 50856 [--cert X --key Y]

Every frame is printed raw and, when it parses, as flow/code/name/json.
"""
import argparse
import base64
import hashlib
import socket
import ssl
import struct
import sys
import threading
import time

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=50856)
ap.add_argument("--cert", default="/tmp/oshare-srv.crt")
ap.add_argument("--key", default="/tmp/oshare-srv.key")
args = ap.parse_args()


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


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
    return bytes([0x80 | opcode, len(payload)]) + payload


def parse_frame(sock):
    """Read one client frame (masked). Returns (opcode, payload)."""
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


def handle_ws(sock):
    log("[ws] upgrade accepted")
    try:
        while True:
            op, payload = parse_frame(sock)
            if op == 0x8:
                log("[ws] close frame")
                return
            if op == 0x9:
                sock.sendall(ws_frame(payload, 0xA))
                continue
            text = payload.decode(errors="replace")
            log(f"[ws] <- {text!r}")
            head, _, body = text.partition("?")
            parts = head.split(":")
            if len(parts) >= 3:
                log(f"[ws]    flow={parts[0]!r} code={parts[1]!r} name={parts[2]!r}")
            if body:
                log(f"[ws]    json={body}")
    except (ConnectionError, OSError) as e:
        log("[ws] closed:", e)


def handle_client(sock, addr):
    peer = f"{addr[0]}:{addr[1]}"
    log(f"[tls] connection from {peer}")
    try:
        method, path, headers, _rest = read_http_head(sock)
        log(f"[http] {method} {path}")
        for k in ("host", "upgrade", "connection", "sec-websocket-key",
                  "user-agent", "content-length"):
            if k in headers:
                log(f"[http]   {k}: {headers[k]}")

        if headers.get("upgrade", "").lower() == "websocket" and "sec-websocket-key" in headers:
            key = headers["sec-websocket-key"].encode()
            accept = base64.b64encode(hashlib.sha1(key + GUID).digest()).decode()
            sock.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n")
            handle_ws(sock)
            return

        # Anything else: answer with a small JSON body so the phone logs a
        # useful error instead of hanging.
        body = b'{"result":0}'
        sock.sendall(b"HTTP/1.1 200 OK\r\n"
                     b"Content-Type: application/json\r\n"
                     b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                     b"Connection: close\r\n\r\n" + body)
        log(f"[http] answered 200 for {path} (not a websocket upgrade)")
    except (ConnectionError, OSError) as e:
        log(f"[tls] {peer}: {e}")
    finally:
        try:
            sock.close()
        except OSError:
            pass


def main():
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
        threading.Thread(target=handle_client, args=(tls, addr), daemon=True).start()


main()
