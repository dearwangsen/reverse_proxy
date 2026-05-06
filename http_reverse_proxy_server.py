#!/usr/bin/env python3
"""
HTTP Reverse Proxy Server - runs on Linux.

Listens on two ports:
  - control_port (default 7000): accepts the persistent connection from the Windows client
  - proxy_port   (default 8080): accepts HTTP proxy requests from local apps (curl, wget, etc.)

HTTP and HTTPS (via CONNECT) are both supported.
"""

import argparse
import json
import logging
import socket
import struct
import threading
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

            if msg_type == "http_response":
                entry = state.pending.get(msg_id)
                if entry:
                    entry["response"] = header
                    entry["body"] = body
                    entry["event"].set()

            elif msg_type == "connect_ack":
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

    headers = {}
    for line in lines[1:]:
        if ": " in line:
            k, _, v = line.partition(": ")
            headers[k] = v

    if not state.connected.is_set():
        _send_http_error(conn, 503, "No client connected")
        return

    if method.upper() == "CONNECT":
        _handle_connect(conn, target, headers, state)
    else:
        _handle_http(conn, method, target, headers, leftover, state)


def _handle_http(conn, method, url, headers, leftover, state: ProxyState):
    content_length = int(headers.get("Content-Length", 0))
    body = leftover
    while len(body) < content_length:
        chunk = conn.recv(BUFSIZE)
        if not chunk:
            break
        body += chunk

    req_id = uuid.uuid4().hex
    event = threading.Event()
    state.pending[req_id] = {"event": event, "response": None, "body": b"", "error": False}

    try:
        state.send(
            {
                "type": "http_request",
                "id": req_id,
                "method": method,
                "url": url,
                "headers": headers,
                "body_len": len(body),
            },
            body,
        )
    except OSError as e:
        state.pending.pop(req_id, None)
        _send_http_error(conn, 502, str(e))
        return

    triggered = event.wait(timeout=TIMEOUT)
    entry = state.pending.pop(req_id, {})

    if not triggered or entry.get("error"):
        _send_http_error(conn, 504 if not triggered else 502, "Gateway error")
        return

    resp = entry["response"]
    resp_body = entry["body"]
    status = resp.get("status", 502)
    resp_headers = resp.get("headers", {})

    status_line = f"HTTP/1.1 {status} {resp.get('reason', '')}\r\n"
    header_lines = "".join(f"{k}: {v}\r\n" for k, v in resp_headers.items())
    conn.sendall((status_line + header_lines + "\r\n").encode() + resp_body)


def _handle_connect(conn, target, headers, state: ProxyState):
    if ":" in target:
        host, port_str = target.rsplit(":", 1)
        port = int(port_str)
    else:
        host, port = target, 443

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

    if not triggered or entry.get("error") or not entry["response"].get("success"):
        reason = "" if not entry.get("response") else entry["response"].get("error", "")
        _send_http_error(conn, 502, reason or "Connect failed")
        return

    conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

    with state.tunnels_lock:
        state.tunnels[req_id] = conn

    # Forward app→client
    def forward_app_to_client():
        try:
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

    threading.Thread(target=forward_app_to_client, daemon=True).start()


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
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="HTTP Reverse Proxy Server (Linux side)")
    parser.add_argument("--control-port", type=int, default=7000, help="Port for Windows client to connect (default: 7000)")
    parser.add_argument("--proxy-port", type=int, default=8080, help="Local HTTP proxy port for apps (default: 8080)")
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
