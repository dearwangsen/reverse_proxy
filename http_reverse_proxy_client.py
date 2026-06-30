#!/usr/bin/env python3
"""
HTTP Reverse Proxy Client - runs on Windows.

Connects to the Linux server's control port, receives forwarded HTTP/HTTPS
TCP tunnel requests, opens them against the company intranet, and relays bytes.

Auto-reconnects on disconnect with exponential backoff.
"""

import argparse
import json
import logging
import socket
import struct
import threading
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [client] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

BUFSIZE = 65536
CONNECT_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Wire protocol helpers (mirror of server)
# ---------------------------------------------------------------------------

def send_msg(sock, header: dict, body: bytes = b"") -> None:
    header_bytes = json.dumps(header).encode()
    frame = struct.pack(">I", len(header_bytes)) + header_bytes + body
    sock.sendall(frame)


def try_send_msg(sock, send_lock, header: dict, body: bytes = b"") -> bool:
    try:
        with send_lock:
            send_msg(sock, header, body)
        return True
    except OSError as e:
        log.warning(
            "Control send failed type=%s id=%s: %s",
            header.get("type"),
            header.get("id"),
            e,
        )
        return False


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
# Active tunnel state
# ---------------------------------------------------------------------------

class TunnelState:
    def __init__(self):
        self.tunnels = {}       # id -> remote socket
        self.lock = threading.Lock()

    def add(self, tid, sock):
        with self.lock:
            self.tunnels[tid] = sock

    def get(self, tid):
        with self.lock:
            return self.tunnels.get(tid)

    def remove(self, tid):
        with self.lock:
            s = self.tunnels.pop(tid, None)
        if s:
            try:
                s.close()
            except OSError:
                pass


def handle_connect(ctrl_sock, send_lock, tunnels: TunnelState, header: dict):
    req_id = header["id"]
    host = header["host"]
    port = header["port"]

    log.info("CONNECT %s:%d", host, port)

    try:
        remote = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        remote.settimeout(None)
    except OSError as e:
        log.warning("CONNECT %s:%d failed: %s", host, port, e)
        try_send_msg(ctrl_sock, send_lock, {"type": "connect_ack", "id": req_id, "success": False, "error": str(e)})
        return

    tunnels.add(req_id, remote)
    if not try_send_msg(ctrl_sock, send_lock, {"type": "connect_ack", "id": req_id, "success": True}):
        tunnels.remove(req_id)
        return

    # remote → server relay
    def relay_remote_to_server():
        try:
            while True:
                chunk = remote.recv(BUFSIZE)
                if not chunk:
                    break
                if not try_send_msg(ctrl_sock, send_lock, {"type": "data", "id": req_id, "body_len": len(chunk)}, chunk):
                    break
        except OSError:
            pass
        finally:
            tunnels.remove(req_id)
            try_send_msg(ctrl_sock, send_lock, {"type": "close", "id": req_id})

    threading.Thread(target=relay_remote_to_server, daemon=True).start()


def handle_data(tunnels: TunnelState, header: dict, body: bytes):
    req_id = header["id"]
    sock = tunnels.get(req_id)
    if sock and body:
        try:
            sock.sendall(body)
        except OSError:
            tunnels.remove(req_id)


def handle_close(tunnels: TunnelState, header: dict):
    tunnels.remove(header.get("id", ""))


# ---------------------------------------------------------------------------
# Main connection loop
# ---------------------------------------------------------------------------

def run(server_host: str, control_port: int):
    backoff = 1
    while True:
        try:
            log.info("Connecting to %s:%d ...", server_host, control_port)
            ctrl_sock = socket.create_connection((server_host, control_port), timeout=10)
            ctrl_sock.settimeout(None)
            log.info("Connected to Linux server")
            backoff = 1

            send_lock = threading.Lock()
            tunnels = TunnelState()

            while True:
                header, body = recv_msg(ctrl_sock)
                if header is None:
                    log.warning("Control channel closed")
                    break

                msg_type = header.get("type")

                if msg_type == "connect":
                    threading.Thread(
                        target=handle_connect,
                        args=(ctrl_sock, send_lock, tunnels, header),
                        daemon=True,
                    ).start()

                elif msg_type == "data":
                    handle_data(tunnels, header, body)

                elif msg_type == "close":
                    handle_close(tunnels, header)

                elif msg_type == "ping":
                    try_send_msg(ctrl_sock, send_lock, {"type": "pong"})

        except (OSError, ConnectionRefusedError) as e:
            log.warning("Connection error: %s", e)
        finally:
            try:
                ctrl_sock.close()
            except Exception:
                pass

        log.info("Reconnecting in %ds ...", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="HTTP Reverse Proxy Client (Windows side)")
    parser.add_argument("server", help="Linux server hostname or IP")
    parser.add_argument("--control-port", type=int, default=7000, help="Control port on Linux server (default: 7000)")
    args = parser.parse_args()

    run(args.server, args.control_port)


if __name__ == "__main__":
    main()
