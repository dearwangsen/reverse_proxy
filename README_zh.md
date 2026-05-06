# http-reverse-proxy

轻量级 HTTP/HTTPS 反向代理隧道，纯 Python 实现。适用于 Linux 服务器需要通过有内网访问权限的 Windows 机器访问私有网络的场景。

**[English](README.md)**

---

## 工作原理

```
┌──────────── Linux（服务端）─────────────┐     ┌── Windows（客户端）──┐
│                                          │     │                      │
│  curl / wget / requests                  │     │                      │
│       │                                  │     │                      │
│       ▼  HTTP 代理请求                   │     │                      │
│  server.py :8080  ←── 控制通道 ───────────────── client.py           │
│       │  （按 request id 多路复用）       │     │       │              │
│       └──── 转发请求 ─────────────────────►           │              │
│       ◄──── 返回响应 ◄─────────────────────            │              │
│                                          │     │       ▼              │
│  :7000（等待 Windows 连接）              │     │    公司内网           │
└──────────────────────────────────────────┘     └──────────────────────┘
```

设计灵感来自 [frp](https://github.com/fatedier/frp)：

- **Windows client** 主动连接 Linux server，建立持久 TCP 控制通道。
- Linux 应用向本地代理端口发起 HTTP/HTTPS 请求，server 通过控制通道将请求转发给 Windows。
- Windows client 对内网执行实际请求，将响应原路返回。
- 多个并发请求通过唯一 request ID 在同一条控制连接上多路复用。

## 功能特性

- 支持 HTTP 和 HTTPS（通过标准 `CONNECT` 隧道）
- 标准 HTTP 代理接口，兼容 `curl`、`wget`、`requests` 及所有支持 `http_proxy` / `https_proxy` 的工具
- 单条持久控制连接 + request ID 多路复用
- 客户端断线自动重连（指数退避，最长 60s）
- 心跳保活
- 服务端零依赖（Python 3.6+ 标准库）
- 客户端推荐安装 `requests` 库以获得更好的 HTTPS 支持

## 环境要求

| 端 | 要求 |
|----|------|
| 服务端（Linux） | Python 3.6+ |
| 客户端（Windows） | Python 3.6+，`pip install requests` |

## 快速开始

**1. Linux 上启动服务端：**

```bash
python3 http_reverse_proxy_server.py
```

默认监听：
- `:7000` — 控制端口（Windows client 连接此端口）
- `:8080` — HTTP 代理端口（Linux 应用连接此端口）

**2. Windows 上启动客户端：**

```cmd
pip install requests
python http_reverse_proxy_client.py <linux服务器IP>
```

客户端连接成功后，断线会自动重连。

**3. Linux 上使用代理：**

```bash
export http_proxy=http://127.0.0.1:8080
export https_proxy=http://127.0.0.1:8080

curl http://internal.example.com/api
wget https://internal.example.com/file
```

Python `requests`：

```python
import requests

proxies = {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}
r = requests.get("http://internal.example.com/api", proxies=proxies)
```

## 参数说明

### `http_reverse_proxy_server.py`

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--control-port` | `7000` | Windows client 连接的控制端口 |
| `--proxy-port` | `8080` | Linux 本地 HTTP 代理端口 |
| `--bind` | `0.0.0.0` | 监听地址 |

### `http_reverse_proxy_client.py`

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `server` | （必填）| Linux 服务器 IP 或域名 |
| `--control-port` | `7000` | 需与服务端 `--control-port` 一致 |

## 通信协议

控制通道消息采用 length-prefixed 帧格式：

```
[ 4字节 header_len（大端序）][ JSON header ][ 可选 body ]
```

| 消息类型 | 方向 | 说明 |
|---|---|---|
| `http_request` | Server → Client | 普通 HTTP 请求 |
| `http_response` | Client → Server | HTTP 响应 |
| `connect` | Server → Client | 建立 HTTPS CONNECT 隧道 |
| `connect_ack` | Client → Server | 隧道建立结果 |
| `data` | 双向 | 隧道原始字节流 |
| `close` | 双向 | 关闭指定隧道 |
| `ping` / `pong` | 双向 | 心跳保活 |

## 注意事项

- 服务端同一时间只接受**一个** client 连接。
- HTTPS 流量透明隧道转发，TLS 在内网服务器端终止，代理不解密。
- 客户端自动绕过 Windows 系统代理（`proxies={"http": None, "https": None}`），确保直连内网。
- 响应压缩（gzip/deflate）由客户端透明处理，下游调用方始终收到原始字节。

## 许可证

[MIT](LICENSE)
