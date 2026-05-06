# HTTP Reverse Proxy Design

**Date:** 2026-05-06  
**Location:** `/root/frp/recerse_proxy/`

## Background

Linux machine needs to access the company intranet, but only the Windows machine has intranet access. The Windows machine can also reach the Linux machine. This system creates an HTTP reverse proxy tunnel: Windows (client) connects to Linux (server), and Linux apps route HTTP/HTTPS traffic through Windows to reach the intranet.

## Architecture

```
┌─────────────────── Linux ───────────────────┐    ┌─── Windows ───┐
│                                              │    │               │
│  curl / wget / requests                      │    │               │
│       │ HTTP proxy request                   │    │               │
│       ▼                                      │    │               │
│  [Proxy Port :8080]                          │    │               │
│  http_reverse_proxy_server.py  ←─ control ──────── http_reverse_proxy_client.py
│       │ multiplex by request_id              │    │       │       │
│       └──── forward request ───────────────────►         │       │
│       ◄──── return response ◄──────────────────          │       │
│                                              │    │       ▼       │
│  [Control Port :7000]                        │    │  Company Intranet
│  waits for Windows to connect                │    │               │
└─────────────────────────────────────────────┘    └───────────────┘
```

### Components

| Script | Runs on | Role |
|--------|---------|------|
| `http_reverse_proxy_server.py` | Linux | Proxy port + control port listener; forwards requests to Windows |
| `http_reverse_proxy_client.py` | Windows | Connects to Linux control port; executes actual HTTP/HTTPS requests |

## Wire Protocol

All messages on the control channel use a length-prefixed framing:

```
┌──────────────┬────────────────────┬──────────────┐
│  4 bytes     │  N bytes           │  M bytes     │
│  header_len  │  JSON header       │  body        │
│  (big-endian)│                    │  (optional)  │
└──────────────┴────────────────────┴──────────────┘
```

### Message Types

| `type` | Direction | Purpose |
|--------|-----------|---------|
| `http_request` | Server → Client | Plain HTTP request (GET/POST/etc.) |
| `http_response` | Client → Server | HTTP response |
| `connect` | Server → Client | Request HTTPS CONNECT tunnel |
| `connect_ack` | Client → Server | Tunnel established (or failed) |
| `data` | Bidirectional | Raw bytes for an active CONNECT tunnel |
| `close` | Bidirectional | Close a specific tunnel by id |
| `ping` / `pong` | Bidirectional | Heartbeat keepalive |

### Example: HTTP Request Message

```json
{
  "type": "http_request",
  "id": "a1b2c3",
  "method": "GET",
  "url": "http://internal.corp.com/api",
  "headers": {"Host": "internal.corp.com"},
  "body_len": 0
}
```

### HTTPS (HTTP CONNECT) Flow

```
App  →  CONNECT internal.corp.com:443 HTTP/1.1
Server  →  {type: "connect", id: "x1", host: "internal.corp.com", port: 443}
          ←  {type: "connect_ack", id: "x1", success: true}
App  ←  200 Connection Established
App  ↔  [raw TLS bytes via data messages, id="x1"]  ↔  Client  ↔  internal.corp.com:443
```

## Threading Model

### Server (Linux)

```
main thread
  ├── listen for Windows client on control port :7000
  ├── spawn control_reader thread  (reads & dispatches responses)
  └── listen for app connections on proxy port :8080
        └── per-connection thread
              ├── parse HTTP or CONNECT request
              ├── assign request id, register pending_requests[id] = Event()
              ├── send message on control channel (with send_lock)
              ├── event.wait(timeout=30s)
              └── write response to app socket
```

### Client (Windows)

```
main thread  (reads control channel messages)
  ├── http_request  →  worker thread: requests.request() → send http_response
  ├── connect       →  worker thread: connect to intranet, send connect_ack, bidirectional relay via data messages
  └── data / close  →  dispatch to active tunnel thread by id
```

### Key Data Structures (Server)

```python
control_sock: socket           # persistent connection to Windows
send_lock: threading.Lock      # serialize writes to control channel

pending: dict[str, dict]       # id → {event, response_headers, response_body}
tunnels: dict[str, socket]     # id → app-side socket (for CONNECT tunnels)
```

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Windows client disconnects | Server wakes all pending events with 502, waits for reconnect |
| Request timeout (30s) | Server returns 504, removes pending entry |
| Intranet request fails | Client sends `http_response {status: 502, body: error}` |
| CONNECT target unreachable | Client sends `connect_ack {success: false}`, Server returns 502 |
| App disconnects mid-CONNECT | Server sends `close` message, Client closes its socket |
| Control channel I/O error | Both sides log; Client auto-reconnects with exponential backoff (max 60s) |

## Configuration

Defaults (overridable via CLI args):

| Parameter | Default | Script |
|-----------|---------|--------|
| Control port | 7000 | server |
| Proxy port | 8080 | server |
| Server host | (required) | client |
| Server control port | 7000 | client |
| Request timeout | 30s | server |
| Reconnect max interval | 60s | client |

## Dependencies

- **Standard library only:** `socket`, `threading`, `http.server`, `urllib`, `json`, `struct`, `uuid`
- **Client optional:** `requests` library (recommended for easier HTTPS certificate handling)
