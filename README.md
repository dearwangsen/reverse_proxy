# http-reverse-proxy

A lightweight HTTP/HTTPS reverse proxy tunnel written in pure Python. Designed for scenarios where a remote machine (e.g. Linux server) needs to access a private network through a local machine (e.g. Windows workstation) that already has access.

**[中文文档](README_zh.md)**

---

## How It Works

```
┌──────────── Linux (server) ─────────────┐     ┌── Windows (client) ──┐
│                                          │     │                      │
│  curl / wget / requests                  │     │                      │
│       │                                  │     │                      │
│       ▼  HTTP proxy                      │     │                      │
│  server.py :8080  ←── control channel ────────── client.py           │
│       │  (multiplexed by request id)     │     │       │              │
│       └──── forward request ───────────────►           │              │
│       ◄──── return response ◄──────────────            │              │
│                                          │     │       ▼              │
│  :7000  (waits for Windows to connect)   │     │  Private Network     │
└──────────────────────────────────────────┘     └──────────────────────┘
```

Inspired by [frp](https://github.com/fatedier/frp):

- The **Windows client** initiates a persistent TCP connection to the Linux server (control channel).
- When a Linux app sends an HTTP/HTTPS request to the local proxy port, the server asks the Windows client to open a TCP connection to the target host.
- Both request and response bodies are relayed as raw byte streams, so large or long-lived transfers such as `git clone` and `git push` do not need to be buffered in memory.
- Multiple concurrent requests are multiplexed over a single control connection using unique request IDs.

## Features

- HTTP and HTTPS (via `CONNECT` tunnel) support
- Standard HTTP proxy interface — works with `curl`, `wget`, `requests`, and any tool that respects `http_proxy` / `https_proxy`
- Single persistent control connection with request-ID multiplexing
- Auto-reconnect with exponential backoff on the client side
- Heartbeat keepalive
- Zero dependencies on both sides (Python 3.6+ stdlib only)

## Requirements

| Side | Requirement |
|------|-------------|
| Server (Linux) | Python 3.6+ |
| Client (Windows) | Python 3.6+ |

## Quick Start

**1. Start the server on Linux:**

```bash
python3 http_reverse_proxy_server.py
```

By default, it listens on:
- `:7000` — control port (Windows client connects here)
- `:8080` — HTTP proxy port (your apps connect here)

**2. Start the client on Windows:**

```cmd
python http_reverse_proxy_client.py <linux-server-ip>
```

The client connects to Linux and auto-reconnects on disconnect.

**3. Use the proxy on Linux:**

```bash
export http_proxy=http://127.0.0.1:8080
export https_proxy=http://127.0.0.1:8080

curl http://internal.example.com/api
wget https://internal.example.com/file
```

Python `requests`:

```python
import requests

proxies = {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}
r = requests.get("http://internal.example.com/api", proxies=proxies)
```

## Options

### `http_reverse_proxy_server.py`

| Argument | Default | Description |
|----------|---------|-------------|
| `--control-port` | `7000` | Port the Windows client connects to |
| `--proxy-port` | `8080` | Local HTTP proxy port for applications |
| `--bind` | `0.0.0.0` | Bind address |

### `http_reverse_proxy_client.py`

| Argument | Default | Description |
|----------|---------|-------------|
| `server` | *(required)* | Linux server hostname or IP |
| `--control-port` | `7000` | Must match the server's `--control-port` |

## Protocol

Messages on the control channel use a simple length-prefixed framing:

```
[ 4-byte header_len (big-endian) ][ JSON header ][ optional body ]
```

| Message type | Direction | Purpose |
|---|---|---|
| `connect` | Server → Client | Open target TCP connection |
| `connect_ack` | Client → Server | Tunnel open result |
| `data` | Bidirectional | Raw bytes for an active tunnel |
| `close` | Bidirectional | Close a tunnel |
| `ping` / `pong` | Bidirectional | Heartbeat |

## Notes

- The server accepts only **one** client connection at a time.
- HTTP and HTTPS traffic are tunneled transparently. TLS terminates at the intranet server, not at the proxy.
- Plain HTTP proxy requests are rewritten from absolute-form to origin-form before being sent upstream, then the body is relayed as raw bytes.

## License

[MIT](LICENSE)
