#!/usr/bin/env python3
"""
HTTP Reverse Proxy Client - runs on Windows.

Connects to the Linux server's control port, receives forwarded HTTP/HTTPS
requests, executes them against the company intranet, and returns responses.

Auto-reconnects on disconnect with exponential backoff.

Dependencies: requests (pip install requests)  — used for HTTP/HTTPS handling.
"""

import argparse
import json
import logging
import socket
import ssl
import struct
import threading
import time
import uuid

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

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


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------

def handle_http_request(ctrl_sock, send_lock, header: dict, body: bytes):
    req_id = header["id"]
    method = header.get("method", "GET")
    url = header.get("url", "")
    req_headers = header.get("headers", {})
    # strip hop-by-hop proxy headers
    for h in ("Proxy-Connection", "Proxy-Authorization", "Transfer-Encoding"):
        req_headers.pop(h, None)

    log.info("HTTP %s %s", method, url)

    try:
        if HAS_REQUESTS:
            resp = requests.request(
                method,
                url,
                headers=req_headers,
                data=body or None,
                timeout=30,
                verify=False,
                allow_redirects=False,
                stream=True,
            )
            resp_body = resp.content
            resp_headers = dict(resp.headers)
            status = resp.status_code
            reason = resp.reason or ""
        else:
            resp_body, resp_headers, status, reason = _urllib_request(method, url, req_headers, body)

        with send_lock:
            send_msg(
                ctrl_sock,
                {
                    "type": "http_response",
                    "id": req_id,
                    "status": status,
                    "reason": reason,
                    "headers": resp_headers,
                    "body_len": len(resp_body),
                },
                resp_body,
            )
    except Exception as e:
        log.warning("HTTP request failed: %s", e)
        err_body = str(e).encode()
        with send_lock:
            send_msg(
                ctrl_sock,
                {
                    "type": "http_response",
                    "id": req_id,
                    "status": 502,
                    "reason": "Bad Gateway",
                    "headers": {"Content-Length": str(len(err_body))},
                    "body_len": len(err_body),
                },
                err_body,
            )


def _urllib_request(method, url, headers, body):
    import urllib.request
    req = urllib.request.Request(url, data=body or None, headers=headers, method=method)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            resp_body = r.read()
            resp_headers = dict(r.headers)
            return resp_body, resp_headers, r.status, r.reason
    except urllib.error.HTTPError as e:
        resp_body = e.read()
        return resp_body, dict(e.headers), e.code, e.reason


def handle_connect(ctrl_sock, send_lock, tunnels: TunnelState, header: dict):
    req_id = header["id"]
    host = header["host"]
    port = header["port"]

    log.info("CONNECT %s:%d", host, port)

    try:
        remote = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except OSError as e:
        log.warning("CONNECT %s:%d failed: %s", host, port, e)
        with send_lock:
            send_msg(ctrl_sock, {"type": "connect_ack", "id": req_id, "success": False, "error": str(e)})
        return

    tunnels.add(req_id, remote)
    with send_lock:
        send_msg(ctrl_sock, {"type": "connect_ack", "id": req_id, "success": True})

    # remote → server relay
    def relay_remote_to_server():
        try:
            while True:
                chunk = remote.recv(BUFSIZE)
                if not chunk:
                    break
                with send_lock:
                    send_msg(ctrl_sock, {"type": "data", "id": req_id, "body_len": len(chunk)}, chunk)
        except OSError:
            pass
        finally:
            tunnels.remove(req_id)
            try:
                with send_lock:
                    send_msg(ctrl_sock, {"type": "close", "id": req_id})
            except OSError:
                pass

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

                if msg_type == "http_request":
                    threading.Thread(
                        target=handle_http_request,
                        args=(ctrl_sock, send_lock, header, body),
                        daemon=True,
                    ).start()

                elif msg_type == "connect":
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
                    with send_lock:
                        send_msg(ctrl_sock, {"type": "pong"})

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

    if not HAS_REQUESTS:
        log.warning("'requests' library not found, falling back to urllib (HTTPS may have issues)")

    run(args.server, args.control_port)


if __name__ == "__main__":
    main()
