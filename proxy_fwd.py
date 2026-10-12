"""本地 SOCKS5 免认证转发器 → 上游代理（SOCKS5 或 HTTP CONNECT，支持前置代理）。

Chromium/Playwright 不方便用的代理形态（带账密 SOCKS5、按客户端 IP 拒大陆的
HTTP 网关等），统一收敛成本地免认证 SOCKS5：

    浏览器 --socks5(免认证)--> 本地转发器 --上游协议(账密)--> 代理节点

上游协议按 URL scheme 自动选择：
    socks5://...  → 上游走 SOCKS5 握手（旧行为，IPRocket 等）
    http://...    → 上游走 HTTP CONNECT（UDeal 等）

前置代理（front）：
    部分上游网关按「客户端 IP」做地域限制（如 UDeal：大陆客户端直接 602
    「当前IP属于China」）。转发器在直连上游失败/被拒时，自动改经前置代理
    连上游。front 的取值优先级：
        1) SocksForwarder.start(..., front=...) 显式传入
        2) 环境变量 DOLA_PROXY_FRONT（http://host:port）
        3) 自动探测系统代理（Windows 注册表，仅认 127.0.0.1/localhost）

用法（在调用方的事件循环上）：
    fwd = await SocksForwarder.start("socks5://user:pass@host:port")
    ... 用 socks5://127.0.0.1:{fwd.port} 起浏览器 ...
    await fwd.stop()
"""
import asyncio
import base64
import os
import socket
import struct
import time
import urllib.parse
import urllib.request

_DEBUG = False

# IPRocket 网关顶不住并发 CONNECT 握手（Chromium 一次页面加载并发 7+ 条，
# 实测全部超时）。全局信号量把上游握手串行化；握完进入 relay 阶段就释放。
_CONNECT_SEM: asyncio.Semaphore | None = None

# Chromium 自身后台流量（安全浏览/更新检查等），与页面无关还抢握手预算，
# 快速拒绝让 Chromium 立刻放弃。
_NOISE_DOMAINS = ("google.com", "googleapis.com", "gstatic.com", "doubleclick.net",
                  "googleusercontent.com", "google.cn", "chrome.com",
                  # 字节 CN 静态 CDN：HK/海外出口本就连不通（认证失败/超时），
                  # 不影响 Dola 页面功能，快拒避免 Chromium 反复重试拖慢加载
                  "yhgfb-cn-static.com", "yhgfb-cn.com", "byteimg.cn", "zjcdn.com")


def _dbg(msg: str):
    if _DEBUG:
        print(f"[fwd {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _is_noise(addr: str, noise=_NOISE_DOMAINS) -> bool:
    a = (addr or "").lower()
    return any(a == d or a.endswith("." + d) for d in noise)


def _parse_proxy(url: str) -> tuple:
    """scheme://user:pass@host:port → (scheme, host, port, user, pass)。"""
    p = url if "://" in url else "socks5://" + url
    u = urllib.parse.urlparse(p)
    scheme = (u.scheme or "socks5").lower()
    return (scheme, u.hostname,
            u.port or (80 if scheme == "http" else 1080),
            urllib.parse.unquote(u.username or "") or None,
            urllib.parse.unquote(u.password or "") or None)


def _auto_front() -> tuple | None:
    """探测系统代理作为前置兜底；只认 127.0.0.1/localhost（避免误用远端）。

    注意：必须优先读 Windows 注册表 —— 本项目 .env.local 的 DOLA_PROXY 等自定义
    环境变量会被 getproxies_environment() 误认成代理配置，把注册表结果挤掉。
    """
    try:
        px = {}
        if hasattr(urllib.request, "getproxies_registry"):
            try:
                px = urllib.request.getproxies_registry() or {}
            except Exception:
                px = {}
        if not px:
            px = {k: v for k, v in urllib.request.getproxies().items()
                  if v and k in ("http", "https")}
        val = px.get("http") or px.get("https") or ""
        if not val:
            return None
        if "://" not in val:
            val = "http://" + val
        u = urllib.parse.urlparse(val)
        if u.hostname not in ("127.0.0.1", "localhost"):
            return None
        return (u.hostname, u.port or 80,
                urllib.parse.unquote(u.username or "") or None,
                urllib.parse.unquote(u.password or "") or None)
    except Exception:
        return None


class UpstreamAuthError(IOError):
    """上游 SOCKS5/HTTP 握手、认证或 CONNECT 失败。"""


def _b64(user: str, pwd: str | None) -> str:
    return base64.b64encode(("%s:%s" % (user, pwd or "")).encode()).decode()


async def _front_connect(front: tuple, host: str, port: int):
    """经前置代理（HTTP CONNECT）建隧道到 host:port，返回 (reader, writer)。"""
    fr, fw = await asyncio.open_connection(front[0], front[1])
    head = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (host, port, host, port)
    if front[2] is not None:
        head += "Proxy-Authorization: Basic %s\r\n" % _b64(front[2], front[3])
    head += "\r\n"
    fw.write(head.encode())
    await fw.drain()
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = await fr.read(4096)
        if not c:
            raise ConnectionError("前置代理连接中断")
        resp += c
    if b" 200" not in resp.split(b"\r\n")[0]:
        raise ConnectionError("前置代理 CONNECT 失败: %s" % resp[:100])
    return fr, fw


async def _upstream_tcp(front: tuple | None, host: str, port: int):
    """拿到到上游网关的 TCP 连接（直连，或经前置代理）。"""
    if front:
        return await _front_connect(front, host, port)
    return await asyncio.open_connection(host, port)


async def _http_upstream_connect(r, w, addr: str, port: int,
                                 user, pwd) -> tuple[bool, str]:
    """上游 HTTP CONNECT 握手（带账密）。返回 (是否成功, 失败详情)。"""
    head = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (addr, port, addr, port)
    if user is not None:
        head += "Proxy-Authorization: Basic %s\r\n" % _b64(user, pwd)
    head += "\r\n"
    w.write(head.encode())
    await w.drain()
    resp = b""
    while b"\r\n\r\n" not in resp:
        c = await r.read(4096)
        if not c:
            raise ConnectionError("上游连接中断")
        resp += c
    line = resp.split(b"\r\n")[0].decode(errors="replace")
    if " 200" not in line:
        body = resp.split(b"\r\n\r\n", 1)[-1][:80].decode(errors="replace")
        return False, "%s %s" % (line, body)
    return True, ""


async def _socks5_upstream_connect(r, w, addr: str, port: int,
                                   user, pwd) -> tuple[bool, str]:
    """上游 SOCKS5 握手（带账密）。返回 (是否成功, 失败详情)。"""
    if user is not None:
        w.write(b"\x05\x01\x02")
    else:
        # 无账密上游（如本机 Clash）：只宣告免认证，
        # 旧行为宣告 0x02 会被 Clash 回 method=2 而直接失败
        w.write(b"\x05\x01\x00")
    await w.drain()
    rep = await r.readexactly(2)
    if rep[1] == 2 and user is not None:
        ub, pb = user.encode(), pwd.encode()
        w.write(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
        await w.drain()
        ar = await r.readexactly(2)
        if ar[1] != 0:
            return False, "上游认证失败 code=%d" % ar[1]
    elif rep[1] != 0:
        return False, "上游握手失败 method=%d" % rep[1]
    ab = addr.encode()
    w.write(b"\x05\x01\x00\x03" + bytes([len(ab)]) + ab + struct.pack(">H", port))
    await w.drain()
    rh = await r.readexactly(4)
    atyp2 = rh[3]
    if atyp2 == 1:
        await r.readexactly(6)
    elif atyp2 == 3:
        ln2 = (await r.readexactly(1))[0]
        await r.readexactly(ln2 + 2)
    elif atyp2 == 4:
        await r.readexactly(18)
    if rh[1] != 0:
        return False, "上游 CONNECT 失败 code=%d（目标 %s:%d）" % (rh[1], addr, port)
    return True, ""


class SocksForwarder:
    def __init__(self, server: asyncio.AbstractServer, port: int, front: tuple | None):
        self.server = server
        self.port = port
        self.front = front

    @classmethod
    async def start(cls, upstream_url: str, listen_host: str = "127.0.0.1",
                    noise_domains=None, front: tuple | None = None) -> "SocksForwarder":
        """noise_domains=None 用默认名单（dola 场景）；传 () 可覆盖（Google 目标必传 ()）。

        front：前置代理 (host, port, user, pass)。None 时按 环境变量 DOLA_PROXY_FRONT
        → 系统代理 自动探测；传 () 可显式禁用。
        """
        up_scheme, up_host, up_port, up_user, up_pass = _parse_proxy(upstream_url)
        noise = _NOISE_DOMAINS if noise_domains is None else tuple(noise_domains)
        if front is None:
            env = os.environ.get("DOLA_PROXY_FRONT", "").strip()
            if env:
                front = _parse_proxy(env)
            else:
                front = _auto_front()
        server = await asyncio.start_server(
            lambda r, w: _handle_client(r, w, up_scheme, up_host, up_port,
                                        up_user, up_pass, noise, front),
            listen_host, 0)
        port = server.sockets[0].getsockname()[1]
        _dbg("forwarder on %s:%d -> %s://%s:%d front=%s"
             % (listen_host, port, up_scheme, up_host, up_port, front))
        return cls(server, port, front)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         up_scheme: str, up_host: str, up_port: int,
                         up_user, up_pass, noise=_NOISE_DOMAINS,
                         front: tuple | None = None):
    peer = writer.get_extra_info("peername")
    _dbg(f"client {peer} connected")
    try:
        # ---- 本地侧 greeting：只宣告免认证 ----
        ver, n = await reader.readexactly(2)
        await reader.readexactly(n)
        _dbg(f"client {peer} greeting ok")
        writer.write(b"\x05\x00")
        await writer.drain()
        # ---- 本地侧 CONNECT 请求 ----
        hdr = await reader.readexactly(4)
        atyp = hdr[3]
        if atyp == 1:
            addr = socket.inet_ntoa(await reader.readexactly(4))
        elif atyp == 3:
            ln = (await reader.readexactly(1))[0]
            addr = (await reader.readexactly(ln)).decode()
        elif atyp == 4:
            addr = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        else:
            raise UpstreamAuthError(f"不支持的地址类型 {atyp}")
        port = struct.unpack(">H", await reader.readexactly(2))[0]
        _dbg(f"client {peer} CONNECT {addr}:{port}")

        # Chromium 自身后台流量：立即拒绝，别浪费上游握手预算
        if _is_noise(addr, noise):
            _dbg(f"CONNECT {addr} = noise, reject fast")
            raise UpstreamAuthError(f"noise domain {addr}")

        # ---- 上游握手（带账密；IPRocket/UDeal 等网关有拒连抖动且顶不住并发
        #      握手——全局串行化 + 自动重试。直连被按客户端 IP 拒（如 UDeal 602
        #      「当前IP属于China」拒大陆）时，自动改经前置代理重试）----
        global _CONNECT_SEM
        if _CONNECT_SEM is None:
            _CONNECT_SEM = asyncio.Semaphore(1)
        up_r = up_w = None
        last_err: Exception | None = None
        use_front = False
        async with _CONNECT_SEM:
            for attempt in range(5):
                try:
                    up_r, up_w = await _upstream_tcp(front if use_front else None,
                                                     up_host, up_port)
                    if up_scheme == "http":
                        ok, detail = await _http_upstream_connect(
                            up_r, up_w, addr, port, up_user, up_pass)
                    else:
                        ok, detail = await _socks5_upstream_connect(
                            up_r, up_w, addr, port, up_user, up_pass)
                    if ok:
                        last_err = None
                        _dbg("CONNECT %s:%d OK via %s (attempt %d)"
                             % (addr, port, "front" if use_front else "direct",
                                attempt + 1))
                        break
                    raise UpstreamAuthError(detail)
                except (UpstreamAuthError, ConnectionError,
                        asyncio.IncompleteReadError, OSError) as e:
                    last_err = e
                    _dbg("attempt %d (use_front=%s) except: %r"
                         % (attempt + 1, use_front, e))
                    try:
                        if up_w is not None:
                            up_w.close()
                    except Exception:
                        pass
                    # 直连失败一律改走前置代理重试（UDeal 对大陆客户端：海外目标
                    # 直接 RST、国内目标回 602「当前IP属于China」，覆盖不全，
                    # 与其猜关键词不如无条件切换；直连本来就通的代理不受影响）
                    if not use_front and front:
                        use_front = True
                        _dbg("direct upstream failed (%s)，switch to front" % str(e)[:60])
                    if attempt < 4:
                        _dbg(f"CONNECT {addr}:{port} attempt {attempt + 1} 失败: {e}")
                        await asyncio.sleep(0.4 * (attempt + 1))
        if last_err is not None or up_r is None or up_w is None:
            raise UpstreamAuthError(f"上游连接重试 5 次仍失败: {last_err}")

        # ---- 双向 relay ----
        writer.write(b"\x05\x00\x00\x01" + socket.inet_aton("0.0.0.0") + struct.pack(">H", 0))
        await writer.drain()
        await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
    except (asyncio.IncompleteReadError, ConnectionError, UpstreamAuthError, OSError):
        # 尽量给客户端回一个失败应答（连接可能已断，忽略回写失败）
        try:
            writer.write(b"\x05\x01\x00\x01" + socket.inet_aton("0.0.0.0") + struct.pack(">H", 0))
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass
