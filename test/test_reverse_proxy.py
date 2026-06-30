import socket
import threading
import time
import unittest

import http_reverse_proxy_server as server


class MemorySocket:
    def __init__(self, initial=b""):
        self._chunks = []
        if initial:
            self._chunks.append(initial)
        self._closed = False
        self.sent = bytearray()
        self._cond = threading.Condition()

    def recv(self, _size):
        with self._cond:
            while not self._chunks and not self._closed:
                self._cond.wait()
            if self._chunks:
                return self._chunks.pop(0)
            return b""

    def sendall(self, data):
        with self._cond:
            if self._closed:
                raise OSError("closed")
            self.sent.extend(data)
            self._cond.notify_all()

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def wait_for_sent(self, marker, timeout=2):
        deadline = time.monotonic() + timeout
        with self._cond:
            while marker not in self.sent:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True


class TestHttpProxyRewrite(unittest.TestCase):
    def test_build_http_origin_request_from_absolute_proxy_request(self):
        raw_headers = [
            "Host: user:secret@gitlab.example.com",
            "User-Agent: git/2.43.0",
            "Proxy-Connection: Keep-Alive",
            "Content-Length: 4",
        ]

        host, port, request_bytes = server.build_http_origin_request(
            "POST",
            "http://user:secret@gitlab.example.com/group/repo.git/git-upload-pack?service=git-upload-pack",
            "HTTP/1.1",
            raw_headers,
            b"want",
        )

        self.assertEqual(host, "gitlab.example.com")
        self.assertEqual(port, 80)
        self.assertTrue(
            request_bytes.startswith(
                b"POST /group/repo.git/git-upload-pack?service=git-upload-pack HTTP/1.1\r\n"
            )
        )
        self.assertIn(b"Host: gitlab.example.com\r\n", request_bytes)
        self.assertIn(b"Authorization: Basic dXNlcjpzZWNyZXQ=\r\n", request_bytes)
        self.assertIn(b"Connection: close\r\n", request_bytes)
        self.assertNotIn(b"Proxy-Connection", request_bytes)
        self.assertTrue(request_bytes.endswith(b"\r\n\r\nwant"))


class TestHttpStreamingProxy(unittest.TestCase):
    def test_plain_http_response_streams_without_waiting_for_full_body(self):
        upstream_seen_request = []

        state = server.ProxyState()
        state.connected.set()
        proxy_app = MemorySocket()

        def fake_send(header, body=b""):
            if header["type"] == "connect":
                req_id = header["id"]
                entry = state.pending[req_id]
                entry["response"] = {"success": True}
                entry["event"].set()
            elif header["type"] == "data":
                upstream_seen_request.append(body)

                def upstream_response():
                    app_sock = state.tunnels.get(header["id"])
                    if app_sock:
                        app_sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\nfirst-")
                    time.sleep(1.0)
                    app_sock = state.tunnels.get(header["id"])
                    if app_sock:
                        app_sock.sendall(b"second")

                threading.Thread(target=upstream_response, daemon=True).start()
            elif header["type"] == "close":
                pass

        state.send = fake_send

        target = "http://gitlab.example.com/repo.git/git-upload-pack"
        request = (
            f"GET {target} HTTP/1.1\r\n"
            "Host: gitlab.example.com\r\n"
            "Proxy-Connection: Keep-Alive\r\n"
            "\r\n"
        ).encode()
        proxy_app._chunks.append(request)

        handler = threading.Thread(
            target=server.handle_proxy_conn,
            args=(proxy_app, ("local", 1), state),
            daemon=True,
        )
        handler.start()

        self.assertTrue(proxy_app.wait_for_sent(b"first-"))
        self.assertFalse(handler.join(0.1))

        self.assertTrue(proxy_app.wait_for_sent(b"second"))
        proxy_app.close()

        handler.join(2)
        self.assertFalse(handler.is_alive())
        self.assertTrue(upstream_seen_request[0].startswith(b"GET /repo.git/git-upload-pack HTTP/1.1\r\n"))

    def test_connect_tunnel_stays_open_after_ack(self):
        state = server.ProxyState()
        state.connected.set()
        proxy_app = MemorySocket(
            b"CONNECT gitlab.example.com:443 HTTP/1.1\r\n"
            b"Host: gitlab.example.com:443\r\n"
            b"\r\n"
        )
        tunneled = []

        def fake_send(header, body=b""):
            if header["type"] == "connect":
                entry = state.pending[header["id"]]
                entry["response"] = {"success": True}
                entry["event"].set()
            elif header["type"] == "data":
                tunneled.append(body)
                app_sock = state.tunnels.get(header["id"])
                if app_sock:
                    app_sock.sendall(b"server-bytes")
            elif header["type"] == "close":
                pass

        state.send = fake_send

        handler = threading.Thread(
            target=server.handle_proxy_conn,
            args=(proxy_app, ("local", 1), state),
            daemon=True,
        )
        handler.start()

        self.assertTrue(proxy_app.wait_for_sent(b"200 Connection Established"))
        self.assertFalse(handler.join(0.1))

        proxy_app._chunks.append(b"client-bytes")
        with proxy_app._cond:
            proxy_app._cond.notify_all()

        self.assertTrue(proxy_app.wait_for_sent(b"server-bytes"))
        self.assertEqual(tunneled, [b"client-bytes"])

        proxy_app.close()
        handler.join(2)
        self.assertFalse(handler.is_alive())


if __name__ == "__main__":
    unittest.main()
