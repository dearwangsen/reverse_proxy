# HTTP Reverse Proxy

让 Linux 通过 Windows 机器访问公司内网的 HTTP/HTTPS 反向代理。

## 原理

```
Linux App → [proxy :8080] → server.py ←─控制通道─ client.py → 内网
```

Windows client 主动连接 Linux server，建立持久控制通道。Linux 上的 HTTP 请求通过该通道转发给 Windows 执行，响应原路返回。

## 快速开始

**Linux（server）：**

```bash
python3 http_reverse_proxy_server.py
# 默认监听 control port 7000，proxy port 8080
```

**Windows（client）：**

```cmd
pip install requests
python http_reverse_proxy_client.py <linux-ip>
```

启动后 Windows 会自动连接 Linux，断线自动重连。

## 使用代理

在 Linux 上设置环境变量，所有 HTTP/HTTPS 工具自动走代理：

```bash
export http_proxy=http://127.0.0.1:8080
export https_proxy=http://127.0.0.1:8080

curl http://internal.corp.com/api
wget https://internal.corp.com/file
```

Python requests：

```python
import requests
proxies = {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}
r = requests.get("http://internal.corp.com/api", proxies=proxies)
```

## 参数

**server：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--control-port` | 7000 | Windows client 连接的端口 |
| `--proxy-port` | 8080 | Linux 本地代理端口 |
| `--bind` | 0.0.0.0 | 监听地址 |

**client：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `server` | （必填）| Linux 服务器 IP 或域名 |
| `--control-port` | 7000 | 控制端口，需与 server 一致 |

## 依赖

- **server**：Python 3.6+ 标准库，无需安装
- **client**：`pip install requests`（推荐），不安装则回退到 urllib
