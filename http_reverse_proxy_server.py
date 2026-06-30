#!/usr/bin/env python3
"""
HTTP Reverse Proxy Server - runs on Linux.

Listens on two ports:
  - control_port (default 7000): accepts the persistent connection from the Windows client
  - proxy_port   (default 9000): accepts HTTP proxy requests from local apps (curl, wget, etc.)

HTTP and HTTPS (via CONNECT) are both supported.
"""

import argparse
import base64
import json
import logging
import socket
import struct
import threading
from urllib.parse import urlsplit
import uuid

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [server] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

TIMEOUT = 30          # seconds to wait for a response from client
HEARTBEAT_INTERVAL = 20
BUFSIZE = 65536


# ---------------------------------------------------------------------------
# Wire protocol helpers
# ---------------------------------------------------------------------------

def send_msg(sock, header: dict, body: bytes = b"") -> None:
    header_bytes = json.dumps(header).encode()
    frame = struct.pack(">I", len(header_bytes)) + header_bytes + body
    sock.sendall(frame)


def recv_msg(sock):
    raw = _recv_exactly(sock, 4)
    if raw is None:
        return None, None
    (hlen,) = struct.unpack(">I", raw)
    header_bytes = _recv_exactly(sock, hlen)
    if header_bytes is None:
        return None, None
    header = json.loads(header_bytes)
    body = b""
    if header.get("body_len", 0) > 0:
        body = _recv_exactly(sock, header["body_len"])
        if body is None:
            return None, None
    return header, body


def _recv_exactly(sock, n: int):
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

class ProxyState:
    def __init__(self):
        self.control_sock = None          # socket to Windows client
        self.send_lock = threading.Lock()
        self.pending = {}                 # id -> {"event": Event, "response": dict, "body": bytes}
        self.tunnels = {}                 # id -> app-side socket (CONNECT tunnels)
        self.tunnels_lock = threading.Lock()
        self.connected = threading.Event()

    def set_client(self, sock):
        self.control_sock = sock
        self.connected.set()
        log.info("Windows client connected from %s", sock.getpeername())

    def clear_client(self):
        self.connected.clear()
        old = self.control_sock
        self.control_sock = None
        # wake all waiting proxy threads with an error signal
        for entry in list(self.pending.values()):
            entry["error"] = True
            entry["event"].set()
        self.pending.clear()
        with self.tunnels_lock:
            for s in self.tunnels.values():
                try:
                    s.close()
                except OSError:
                    pass
            self.tunnels.clear()
        if old:
            try:
                old.close()
            except OSError:
                pass
        log.warning("Windows client disconnected, cleared state")

    def send(self, header, body=b""):
        with self.send_lock:
            send_msg(self.control_sock, header, body)


# ---------------------------------------------------------------------------
# Control channel reader
# ---------------------------------------------------------------------------

def control_reader(state: ProxyState):
    while True:
        state.connected.wait()
        sock = state.control_sock
        while True:
            header, body = recv_msg(sock)
            if header is None:
                state.clear_client()
                break

            msg_type = header.get("type")
            msg_id = header.get("id")

            if msg_type == "connect_ack":
                entry = state.pending.get(msg_id)
                if entry:
                    entry["response"] = header
                    entry["event"].set()

            elif msg_type == "data":
                with state.tunnels_lock:
                    app_sock = state.tunnels.get(msg_id)
                if app_sock and body:
                    try:
                        app_sock.sendall(body)
                    except OSError:
                        _close_tunnel(state, msg_id)

            elif msg_type == "close":
                _close_tunnel(state, msg_id)

            elif msg_type == "pong":
                pass


def _close_tunnel(state: ProxyState, tunnel_id: str):
    with state.tunnels_lock:
        sock = state.tunnels.pop(tunnel_id, None)
    if sock:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Proxy connection handler
# ---------------------------------------------------------------------------

def handle_proxy_conn(conn: socket.socket, addr, state: ProxyState):
    try:
        _handle_proxy_conn(conn, addr, state)
    except Exception as e:
        log.debug("proxy conn %s error: %s", addr, e)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _handle_proxy_conn(conn: socket.socket, addr, state: ProxyState):
    # Read the first HTTP request line + headers
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = conn.recv(4096)
        if not chunk:
            return
        raw += chunk

    header_section, _, leftover = raw.partition(b"\r\n\r\n")
    lines = header_section.decode(errors="replace").split("\r\n")
    if not lines:
        return
    request_line = lines[0]
    parts = request_line.split()
    if len(parts) < 2:
        return
    method, target = parts[0], parts[1]
    version = parts[2] if len(parts) >= 3 else "HTTP/1.1"

    if not state.connected.is_set():
        _send_http_error(conn, 503, "No client connected")
        return

    if method.upper() == "CONNECT":
        _handle_connect(conn, target, state)
    else:
        _handle_http(conn, method, target, version, lines[1:], leftover, state)


def build_http_origin_request(method, target, version, raw_header_lines, first_body=b""):
    parsed = urlsplit(target)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("HTTP proxy requests must use an absolute URL")
    if parsed.scheme.lower() != "http":
        raise ValueError(f"Unsupported proxy URL scheme: {parsed.scheme}")

    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    skip_headers = {
        "proxy-connection",
        "proxy-authorization",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
    }
    output_headers = []
    has_host = False
    has_auth = False
    for line in raw_header_lines:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.lstrip()
        lower = key.lower()
        if lower in skip_headers:
            continue
        if lower == "host":
            value = host if port == 80 else f"{host}:{port}"
            has_host = True
        elif lower == "authorization":
            has_auth = True
        output_headers.append((key, value))

    if not has_host:
        output_headers.insert(0, ("Host", host if port == 80 else f"{host}:{port}"))
    if parsed.username and not has_auth:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo += ":" + parsed.password
        token = base64.b64encode(userinfo.encode()).decode()
        output_headers.append(("Authorization", f"Basic {token}"))
    output_headers.append(("Connection", "close"))

    request = f"{method} {path} {version}\r\n"
    request += "".join(f"{key}: {value}\r\n" for key, value in output_headers)
    return host, port, request.encode() + b"\r\n" + first_body


def _handle_http(conn, method, url, version, raw_header_lines, leftover, state: ProxyState):
    try:
        host, port, initial_data = build_http_origin_request(
            method,
            url,
            version,
            raw_header_lines,
            leftover,
        )
    except ValueError as e:
        _send_http_error(conn, 400, str(e))
        return

    _open_tunnel_and_relay(conn, host, port, state, initial_data=initial_data, send_established=False)


def _handle_connect(conn, target, state: ProxyState):
    if ":" in target:
        host, port_str = target.rsplit(":", 1)
        port = int(port_str)
    else:
        host, port = target, 443

    _open_tunnel_and_relay(conn, host, port, state, initial_data=b"", send_established=True)


def _open_tunnel_and_relay(conn, host, port, state: ProxyState, initial_data=b"", send_established=False):
    req_id = uuid.uuid4().hex
    event = threading.Event()
    state.pending[req_id] = {"event": event, "response": None, "error": False}

    try:
        state.send({"type": "connect", "id": req_id, "host": host, "port": port})
    except OSError as e:
        state.pending.pop(req_id, None)
        _send_http_error(conn, 502, str(e))
        return

    triggered = event.wait(timeout=TIMEOUT)
    entry = state.pending.pop(req_id, {})

    if not triggered or entry.get("error") or not entry.get("response", {}).get("success"):
        reason = "" if not entry.get("response") else entry["response"].get("error", "")
        _send_http_error(conn, 502 if triggered else 504, reason or "Connect failed")
        return

    if send_established:
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

    with state.tunnels_lock:
        state.tunnels[req_id] = conn

    try:
        if initial_data:
            state.send({"type": "data", "id": req_id, "body_len": len(initial_data)}, initial_data)
        while True:
            chunk = conn.recv(BUFSIZE)
            if not chunk:
                break
            try:
                state.send({"type": "data", "id": req_id, "body_len": len(chunk)}, chunk)
            except OSError:
                break
    finally:
        _send_tunnel_close(state, req_id)


def _send_tunnel_close(state: ProxyState, tunnel_id: str):
    try:
        state.send({"type": "close", "id": tunnel_id})
    except OSError:
        pass
    _close_tunnel(state, tunnel_id)


def _send_http_error(conn, code, msg):
    body = msg.encode()
    resp = (
        f"HTTP/1.1 {code} Error\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Content-Type: text/plain\r\n\r\n"
    ).encode() + body
    try:
        conn.sendall(resp)
    except OSError:
        pass




# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def heartbeat(state: ProxyState):
    import time
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        if state.connected.is_set():
            try:
                state.send({"type": "ping"})
            except OSError as e:
                log.warning("Heartbeat failed: %s", e)
                state.clear_client()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="HTTP Reverse Proxy Server (Linux side)")
    parser.add_argument("--control-port", type=int, default=7000, help="Port for Windows client to connect (default: 7000)")
    parser.add_argument("--proxy-port", type=int, default=9000, help="Local HTTP proxy port for apps (default: 9000)")
    parser.add_argument("--bind", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    args = parser.parse_args()

    state = ProxyState()

    # Control channel acceptor
    def accept_control():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((args.bind, args.control_port))
        srv.listen(1)
        log.info("Waiting for Windows client on control port %d", args.control_port)
        while True:
            conn, addr = srv.accept()
            if state.connected.is_set():
                log.warning("Rejecting second client from %s, already have one", addr)
                conn.close()
                continue
            state.set_client(conn)

    threading.Thread(target=accept_control, daemon=True).start()
    threading.Thread(target=control_reader, args=(state,), daemon=True).start()
    threading.Thread(target=heartbeat, args=(state,), daemon=True).start()

    # Proxy acceptor
    proxy_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proxy_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    proxy_srv.bind((args.bind, args.proxy_port))
    proxy_srv.listen(128)
    log.info("HTTP proxy listening on %s:%d", args.bind, args.proxy_port)
    log.info("Set http_proxy=http://127.0.0.1:%d on this machine", args.proxy_port)

    while True:
        conn, addr = proxy_srv.accept()
        threading.Thread(target=handle_proxy_conn, args=(conn, addr, state), daemon=True).start()


if __name__ == "__main__":
    main()
