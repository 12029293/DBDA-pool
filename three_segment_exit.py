# -*- coding: utf-8 -*-
r"""
three_segment_exit.py  --  drop-in three-segment egress policy for the DBDA-pool
dola delivery engine (hint 830, goal 612/712).  Copy next to config.py and
`import three_segment_exit as T; T.apply()` early in the process (before any
requests.Session is built).

    (1) REGISTER  send_code / register_verify_login / login_check / temp-mail
            -> Clash HTTP proxy  http://127.0.0.1:7897   (env DOLA_REGISTER_PROXY)
               direct is 1206-rate-limited; 127.0.0.1:7890 has NO listener
               (ProxyError / "Max retries exceeded") -> banned.

    (2) SUBMIT    POST /chat/completion (ability_type=17, asset 1266) + the 710 probe
            -> iprocket RESIDENTIAL socks5h.  This box has NO pysocks, so a
               requests `socks5h://` proxy raises InvalidSchema; we therefore install a
               bare-socket SOCKS5 shim over urllib3.util.connection (i529/i728 path) and
               set config.PROXY="" so the protocol layer adds no proxy of its own.
               iprocket mixes good/bad endpoints: SOCKS5 CONNECT reply code=1 is a
               per-endpoint quality problem -> fetch ANOTHER endpoint and retry (i749).

    (3) DOWNLOAD  get_play_info CDN direct link (v16-dola.dola.com)
            -> LOCAL DIRECT, no proxy at all (config.DOWNLOAD_DIRECT=1; the CDN host is
               deliberately absent from the submit tunnel set so it falls through to the
               machine's own connection).

Env switches (all optional, sane defaults = hint 830):
    DOLA_REGISTER_PROXY   default http://127.0.0.1:7897
    DOLA_SUBMIT_EXIT      residential (default) | direct | http://host:port
    DOLA_SUBMIT_TUNNEL    comma list of hosts to tunnel for the submit segment
                          (default www.dola.com) -- keep the CDN OUT of this list
    DOLA_REGISTER_FALLBACK_7897  1 (default) = keep the 7897 register fallback alive
"""
from __future__ import annotations

import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request

REGISTER_PROXY = os.environ.get("DOLA_REGISTER_PROXY", "http://127.0.0.1:7897")
SUBMIT_EXIT = os.environ.get("DOLA_SUBMIT_EXIT", "residential").strip()
SUBMIT_TUNNEL = tuple(h.strip() for h in
                      os.environ.get("DOLA_SUBMIT_TUNNEL", "www.dola.com").split(",") if h.strip())
BANNED_PROXY = "127.0.0.1:7890"

# ---- 提交段出口：clash / 7897 是「本机 Clash」的别名 -------------------------
# ★ 2026-10-16：`clash` 原本只是 run_pool.py 自己认识的值（它把 clash 改写成
#   direct + DOLA_PROXY=7897）。但面板 server.py 直接 import config，不认识 clash
#   → config.PROXY 留空 → 提交走直连 → 必然 710022003/WinError 10054。
#   这里把它提升为一等值：两条路径（引擎 run_pool.py、面板 server.py）都能正确解析。
CLASH_PROXY = os.environ.get("DOLA_PROXY_7897", "http://127.0.0.1:7897").strip() \
    or "http://127.0.0.1:7897"


def is_clash_alias(value: str) -> bool:
    """提交段出口是不是「走本机 Clash」的写法。"""
    return (value or "").strip().lower() in ("clash", "7897", "clash7897", "local-clash")

IPROCKET_BASE = "https://api.iprocket.io/api"
IPROCKET_USER = "com38730086"
IPROCKET_PASS = "EwT5JARgS7uRe8f"

# ---- IPRocket 总开关（2026-10-16）-----------------------------------------
# DOLA_IPROCKET_ENABLED=0 一键停用**所有** IPRocket 调用（API 提取 + 住宅路由 +
# keeper 后台线程）。留空/1 = 保持原有行为。
#
# 为什么需要它：提取接口一旦失败（实测 403 Forbidden），keeper 里的
# `ep = _state.get("socks")` 永远是 None → 判断条件恒真 → 每 15 秒重试一轮，
# 每轮最多 11 个 API 请求（1 次批量 + 10 次单次）。IPRocket 按调用次数计费，
# 这种「失败即无限重试」的形态会把额度打光，排查/换代理源期间必须先停掉。
IPROCKET_ENABLED = os.environ.get(
    "DOLA_IPROCKET_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def iprocket_disabled_error():
    return RuntimeError(
        "IPRocket 已被 DOLA_IPROCKET_ENABLED=0 停用（见 three_segment_exit.py 顶部说明）")


# ------------------------------------------------------------------ iprocket ----
def iprocket_url(cc="any", session=None, session_time=5, ptype="Socks5", n=1):
    q = {"username": IPROCKET_USER, "password": IPROCKET_PASS, "cc": cc, "ips": n,
         "type": "-res-", "proxyType": ptype, "responseType": "txt",
         "sessionTime": session_time}
    if session:
        q["session"] = session
    return IPROCKET_BASE + "?" + urllib.parse.urlencode(q)


def _direct_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _parse_endpoint(line):
    p = line.strip().split(":")
    if len(p) < 4:
        raise ValueError("unexpected iprocket line: %r" % line[:120])
    return (p[0], int(p[1]), p[2], p[3])


def fetch_iprocket(cc="any", session=None, session_time=5, timeout=40):
    if not IPROCKET_ENABLED:
        raise iprocket_disabled_error()
    req = urllib.request.Request(iprocket_url(cc=cc, session=session, session_time=session_time),
                                 headers={"User-Agent": "Mozilla/5.0"})
    line = _direct_opener().open(req, timeout=timeout).read().decode("utf-8", "replace")
    host, port, user, pwd = _parse_endpoint(line.strip().splitlines()[0])
    return {"host": host, "port": port, "user": user, "pwd": pwd, "raw": line.strip()}


def fetch_iprocket_multi(n=3, cc="any", session_time=300, timeout=12):
    """One API call returns n endpoints (faster + more likely to include a good gateway
    than fetching one at a time). The `-res-Lsid-...` session in each username holds
    that on-box IP for its TTL, so a validated endpoint can be reused for minutes."""
    if not IPROCKET_ENABLED:
        raise iprocket_disabled_error()
    req = urllib.request.Request(
        iprocket_url(cc=cc, session=None, session_time=session_time, n=n),
        headers={"User-Agent": "Mozilla/5.0"})
    txt = _direct_opener().open(req, timeout=timeout).read().decode("utf-8", "replace")
    out = []
    for ln in txt.strip().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            host, port, user, pwd = _parse_endpoint(ln)
            out.append({"host": host, "port": port, "user": user, "pwd": pwd, "raw": ln})
        except Exception:
            continue
    return out


# cached validated submit endpoint (avoids re-hitting the iprocket API every call)
_ROUTE_CACHE = {"route": None, "at": 0.0}


# ------------------------------------------------------- bare-socket SOCKS5 ----
def socks5_connect(socks, dst, dport, timeout=25):
    host, port, user, pwd = socks
    s = socket.create_connection((host, port), timeout=timeout)
    s.settimeout(timeout)
    s.sendall(b"\x05\x02\x00\x02")
    r = s.recv(2)
    if len(r) < 2:
        s.close(); raise RuntimeError("socks5 greeting short")
    if r[1] == 0x02:
        ub, pb = user.encode(), pwd.encode()
        s.sendall(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
        r2 = s.recv(2)
        if len(r2) < 2 or r2[1] != 0:
            s.close(); raise RuntimeError("socks5 auth failed %r" % r.hex())
    elif r[1] != 0x00:
        s.close(); raise RuntimeError("socks5 no acceptable method %r" % r.hex())
    d = dst.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(d)]) + d + struct.pack(">H", dport))
    rep = s.recv(4)
    if len(rep) < 4:
        s.close(); raise RuntimeError("socks5 short connect reply")
    atyp = rep[3]
    if atyp == 0x01:
        s.recv(4 + 2)
    elif atyp == 0x03:
        ln = s.recv(1)[0]; s.recv(ln + 2)
    elif atyp == 0x04:
        s.recv(16 + 2)
    if rep[1] != 0:
        code = rep[1]
        s.close()
        raise RuntimeError("socks5 connect rejected code=%d" % code)
    return s


def socks5_reply(socks, dst, dport, timeout=12):
    try:
        host, port, user, pwd = socks
        s = socket.create_connection((host, port), timeout=timeout)
    except Exception as e:
        return False, None, "dial:%s" % repr(e)[:60]
    s.settimeout(timeout)
    try:
        s.sendall(b"\x05\x02\x00\x02")
        r = s.recv(2)
        if len(r) < 2:
            s.close(); return False, None, "greeting"
        if r[1] == 0x02:
            ub, pb = user.encode(), pwd.encode()
            s.sendall(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
            r2 = s.recv(2)
            if len(r2) < 2 or r2[1] != 0:
                s.close(); return False, None, "auth"
        elif r[1] != 0x00:
            s.close(); return False, None, "method"
        d = dst.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(d)]) + d + struct.pack(">H", dport))
        rep = s.recv(4)
        if len(rep) < 4:
            s.close(); return False, None, "short_reply"
        if rep[3] == 0x01:
            s.recv(6)
        elif rep[3] == 0x03:
            ln = s.recv(1)[0]; s.recv(ln + 2)
        elif rep[3] == 0x04:
            s.recv(18)
        s.close()
        return (rep[1] == 0), rep[1], rep.hex()
    except Exception as e:
        try: s.close()
        except Exception: pass
        return False, None, "exc:%s" % repr(e)[:60]


_CCS = ["any", "jp", "us", "kr", "sg", "gb", "de", "fr", "br", "au", "ca", "th", "id", "mx", "vn"]


def pick_endpoint(host="www.dola.com", port=443, tries=10, timeout=14, on_try=None,
                  session_time=300):
    """Fetch iprocket endpoints until one carries a real CONNECT; code=1 -> refetch.

    i794 fix: (a) try a cached validated endpoint first (iprocket `-res-Lsid-...`
    holds the on-box IP for its TTL, so a recent good endpoint is very likely still
    good); (b) prefer ONE multi-IP API call (fetch_iprocket_multi) over N sequential
    single fetches -- far less exposure to the API's sporadic timeouts; (c) short
    per-fetch timeout (12s) so a hung API call fails fast instead of stalling the
    whole submit segment.
    """
    if not IPROCKET_ENABLED:
        if on_try:
            on_try({"i": -1, "cc": "-", "fetch": "DISABLED DOLA_IPROCKET_ENABLED=0"})
        return None, []

    seen = []

    def _note(rec):
        seen.append(rec)
        if on_try:
            on_try(rec)

    # (a) cached endpoint first
    cached = _ROUTE_CACHE.get("route")
    if cached and (time.time() - _ROUTE_CACHE.get("at", 0)) < 150:
        ok, code, rep = socks5_reply(cached, host, port, timeout=timeout)
        _note({"i": -1, "cc": "cache", "entry": "%s:%s" % (cached[0], cached[1]),
               "ok": ok, "reply_code": code, "reply": rep})
        if ok:
            return ({"host": cached[0], "port": cached[1], "user": cached[2],
                     "pwd": cached[3], "validated": True, "checks": seen, "cached": True}, seen)

    # (b) one batched API call
    try:
        batch = fetch_iprocket_multi(n=max(3, min(tries, 5)), cc=_CCS[0], session_time=session_time)
    except Exception as e:
        _note({"i": 0, "cc": _CCS[0], "fetch": "ERR %s" % repr(e)[:70]})
        batch = []

    for i, r in enumerate(batch):
        ep = (r["host"], r["port"], r["user"], r["pwd"])
        ok, code, rep = socks5_reply(ep, host, port, timeout=timeout)
        _note({"i": i, "cc": "batch", "entry": "%s:%s" % (ep[0], ep[1]),
               "ok": ok, "reply_code": code, "reply": rep})
        if ok:
            _ROUTE_CACHE["route"] = ep
            _ROUTE_CACHE["at"] = time.time()
            r["validated"] = True
            r["checks"] = seen
            return r, seen
        time.sleep(0.3)

    # (c) fallback: sequential single fetches with a short time-budget
    budget_end = time.time() + 25
    i = 0
    while time.time() < budget_end and i < tries:
        cc = _CCS[i % len(_CCS)]
        try:
            r = fetch_iprocket(cc=cc, session="seg_%d_%d" % (i, int(time.time() * 1000) % 1000000),
                               session_time=session_time, timeout=min(12, max(4, int(budget_end - time.time()))))
        except Exception as e:
            _note({"i": i, "cc": cc, "fetch": "ERR %s" % repr(e)[:70]})
            i += 1
            continue
        ep = (r["host"], r["port"], r["user"], r["pwd"])
        ok, code, rep = socks5_reply(ep, host, port, timeout=timeout)
        _note({"i": i, "cc": cc, "entry": "%s:%s" % (ep[0], ep[1]),
               "ok": ok, "reply_code": code, "reply": rep})
        if ok:
            _ROUTE_CACHE["route"] = ep
            _ROUTE_CACHE["at"] = time.time()
            r["validated"] = True
            r["checks"] = seen
            return r, seen
        i += 1
        time.sleep(0.3)
    return None, seen


# -------------------------------------------------- urllib3 selective shim ----
_ORIG = None
_installed = False
_lock = threading.Lock()
_state = {"socks": None, "tunnel_hosts": set(), "bypass": set(), "no_lazy": False,
          "strict": False, "last_use": 0, "fetched_at": 0,
          # 【IPRocket 流量治理 · 2026-10-17】隧道排除名单。
          # shim 在 socket 层拦截，粒度是 hostname：只要 host 命中 tunnel_hosts，
          # 那个 host 上的**一切**都会走住宅隧道，连 proxies={"http": None} 都绕不开。
          # 所以需要一份「即使命中隧道名单也强制直连」的排除表，专门兜住
          # 「CDN/大对象恰好挂在被隧道的域名上」这类流量黑洞。
          "tunnel_exclude": set()}

# 隧道统计：用来在日志里证明「大对象没有走住宅隧道」。
# 只记 host + 是否隧道 + 次数，不含任何内容。
_TUNNEL_STATS: dict[str, dict] = {}
_STATS_LOCK = threading.Lock()


def _note_host(host: str, tunneled: bool) -> None:
    with _STATS_LOCK:
        rec = _TUNNEL_STATS.setdefault(host, {"tunneled": 0, "direct": 0})
        rec["tunneled" if tunneled else "direct"] += 1


def tunnel_stats() -> dict[str, dict]:
    """返回各 host 的隧道/直连计数（供诊断接口与日志使用）。"""
    with _STATS_LOCK:
        return {h: dict(v) for h, v in _TUNNEL_STATS.items()}


def _host_excluded(host: str) -> bool:
    """host 是否命中隧道排除名单（含子域后缀匹配）。"""
    hosts = _state.get("tunnel_exclude") or set()
    if not hosts:
        return False
    h = (host or "").lower()
    for d in hosts:
        if h == d or h.endswith("." + d):
            return True
    return False


def current_route_view():
    return _state.get("socks")


def _patched_create_connection(address, *args, **kwargs):
    host = address[0]
    host_s = str(host or "").lower()
    excluded = _host_excluded(host_s)
    tunneled = (host in _state["tunnel_hosts"] and host not in _state["bypass"]
                and not excluded)
    if _state["tunnel_hosts"]:
        _note_host(host_s, tunneled)
    if tunneled:
        socks = _state["socks"]
        if not socks and not _state.get("no_lazy"):
            _lazy_route()                     # first submit after a stale route: fetch now
            socks = _state["socks"]
        if socks:
            timeout = kwargs.get("timeout", None)
            if timeout is None and args:
                timeout = args[0]
            if timeout is None or timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
                timeout = 30
            try:
                sk = socks5_connect(socks, host, address[1], timeout=timeout)
                _state["last_use"] = time.time()
                return sk
            except Exception:
                # bad/dead endpoint (e.g. CONNECT reply code=1): drop the route so the
                # NEXT attempt lazily fetches a fresh endpoint instead of churning now.
                _state["socks"] = None
                _state["fetched_at"] = 0
                raise
        if _state.get("strict"):
            # residential mode + no validated endpoint: FAIL LOUDLY instead of silently
            # falling through to a direct connection (direct to dola triggers 710022003 /
            # hangs -- the exact opposite of the user's policy).
            raise ConnectionError("no validated residential SOCKS5 endpoint for %s" % host)
    return _ORIG(address, *args, **kwargs)


def install_shim():
    global _ORIG, _installed
    with _lock:
        if _installed:
            return True
        import urllib3.util.connection as u3c
        _ORIG = u3c.create_connection
        u3c.create_connection = _patched_create_connection
        _installed = True
    return True


def set_tunnel_exclude(hosts=None):
    """设置隧道排除名单（命中则强制直连，即使 host 在 SUBMIT_TUNNEL 里）。

    【IPRocket 流量治理 · 2026-10-17】shim 在 socket 层按 hostname 拦截，所以
    「被隧道的 host 上的大对象」会整段走住宅流量且无法用 proxies={None} 绕过。
    这里让调用方显式声明「这些域一律不走住宅隧道」，把成片/CDN 挡在外面。
    hosts=None/空 表示清空排除名单（回到旧的精确匹配行为）。
    """
    if isinstance(hosts, str):
        hosts = [h.strip() for h in hosts.split(",") if h.strip()]
    _state["tunnel_exclude"] = {str(h).strip().lower() for h in (hosts or []) if str(h).strip()}
    install_shim()
    return sorted(_state["tunnel_exclude"])


def set_submit_route(ep, tunnel_hosts=SUBMIT_TUNNEL):
    install_shim()
    if ep is None:
        _state["socks"] = None
        return None
    if isinstance(ep, dict):
        route = (ep["host"], int(ep["port"]), ep["user"], ep["pwd"])
    else:
        route = (ep[0], int(ep[1]), ep[2], ep[3])
    _state["socks"] = route
    _state["tunnel_hosts"] = set(tunnel_hosts or ())
    _state["bypass"] = {route[0]}
    return route


def clear_route():
    _state["socks"] = None


def egress_ip(route=None, timeout=25):
    import re
    if route is None:
        route = _state.get("socks")
    if not route:
        req = urllib.request.Request("http://ip-api.com/json/?fields=query",
                                     headers={"User-Agent": "Mozilla/5.0"})
        txt = _direct_opener().open(req, timeout=timeout).read().decode("utf-8", "replace")
        m = re.search(r'"query"\s*:\s*"([^"]+)"', txt)
        return m.group(1) if m else txt.strip()
    s = socks5_connect(route, "ip-api.com", 80, timeout=timeout)
    s.settimeout(timeout)
    s.sendall(b"GET /json/?fields=query HTTP/1.1\r\nHost: ip-api.com\r\nConnection: close\r\n\r\n")
    data = b""
    while True:
        try:
            c = s.recv(4096)
        except Exception:
            break
        if not c:
            break
        data += c
    s.close()
    text = data.decode("utf-8", "replace")
    m = re.search(r'"query"\s*:\s*"([^"]+)"', text)
    # ★ 2026-10-16 修复：原来兜底返回 data[-120:] 是 **bytes**，会被写进日志 dict 后
    #   触发 "TypeError: Object of type bytes is not JSON serializable"（日志里能看到
    #   keeper_exc）。统一 decode 成 str。
    return m.group(1) if m else text[-120:]


# --------------------------------------------------------------- apply() ----
_keeper_thread = None
_keeper_stop = threading.Event()
_lazy_lock = threading.Lock()
_log_path = None


def _log(rec):
    line = time.strftime("%H:%M:%S ") + (rec if isinstance(rec, str)
                                         else __import__("json").dumps(rec, ensure_ascii=False))
    try:
        print("[seg-exit] " + line, flush=True)
    except Exception:
        pass
    if _log_path:
        try:
            with open(_log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def acquire_route(ttl=300, margin=45, tries=8, log=None):
    """Validate a fresh residential endpoint and install it as the submit route."""
    log = log or _log
    ep, checks = pick_endpoint(tries=tries, on_try=log)
    if not ep:
        log({"msg": "no validated residential endpoint (all CONNECT rejected)"})
        return None
    set_submit_route(ep, SUBMIT_TUNNEL)
    _state["fetched_at"] = time.time()
    _state["ttl"] = ttl
    info = {"endpoint": "%s:%s" % (ep["host"], ep["port"])}
    try:
        info["egress"] = egress_ip()
    except Exception as e:
        info["egress"] = "ERR %r" % e
    log(info)
    return ep


def _lazy_route():
    """Acquire a route on demand (first tunneled connect), with a short budget."""
    with _lazy_lock:
        if _state.get("socks"):
            return True
        logger = _log
        ep, checks = pick_endpoint(tries=4, timeout=10, on_try=logger)
        if not ep:
            return False
        set_submit_route(ep, SUBMIT_TUNNEL)
        _state["fetched_at"] = time.time()
        _state["ttl"] = 300
        logger({"lazy_route": "%s:%s" % (ep["host"], ep["port"])})
        return True


def start_keeper(ttl=300, margin=45, log_path=None):
    """Daemon thread that keeps a validated residential submit route fresh.

    Refreshes BETWEEN submits (never mid-burst): it only re-validates when the TTL is
    nearly elapsed or the route disappeared.  Safe to call from config import.
    """
    global _keeper_thread, _log_path
    if log_path:
        _log_path = log_path
    # i794 fix: config.py calls this UNCONDITIONALLY; but the keeper only makes sense
    # in residential mode. In direct/http-proxy mode there is no residential route to
    # keep, and running it just spams the iprocket API with endpoint fetches.
    if SUBMIT_EXIT not in ("residential", ""):
        return None
    # 2026-10-16：IPRocket 停用时不启动 keeper —— 少一个后台线程，且不会再去打 API。
    if not IPROCKET_ENABLED:
        _log({"keeper": "disabled by DOLA_IPROCKET_ENABLED=0"})
        return None
    if _keeper_thread is not None:
        return _keeper_thread

    def _loop():
        grace = 60          # never rotate during the first minute (a submit cycle may be in flight)
        started = time.time()
        # ★ 2026-10-16 修复「失败即无限重试把额度打光」：
        # 原实现每 15 秒一轮，条件是 `ep is None or ...`。而 ep 只在**成功**拿到
        # 路由时才有值，所以一旦提取失败（如 403），ep 恒为 None → 条件恒真 →
        # 每 15 秒重试一轮，每轮最多 11 个 API 请求。IPRocket 按调用次数计费，
        # 这种形态会把额度烧光。三层保护：
        #   ① 指数退避 15s→30s→…→上限 600s；
        #   ② 连续失败达到 KEEPER_MAX_FAILS 次后**彻底停止**该线程，
        #      不再无休止重试（提取链接长期失效时的正确行为）；
        #   ③ 成功一次立刻把退避和失败计数都复位。
        backoff = 15
        max_backoff = 600
        max_fails = int(os.environ.get("DOLA_IPROCKET_KEEPER_MAX_FAILS", "5") or 5)
        fails = 0
        while not _keeper_stop.is_set():
            try:
                ep = _state.get("socks")
                age = time.time() - _state.get("fetched_at", 0)
                busy = (time.time() - _state.get("last_use", 0)) < 90      # recent traffic
                if ep is None or (age > (ttl - margin) and not busy
                                  and (time.time() - started) > grace):
                    got = acquire_route(ttl=ttl, margin=margin)
                    if got:
                        backoff = 15                    # 成功 → 复位
                        fails = 0
                    else:
                        fails += 1
                        if max_fails > 0 and fails >= max_fails:
                            _log({"keeper": "连续 %d 次拿不到住宅端点，停止重试"
                                             "（避免烧光提取配额）" % fails})
                            return
                        slept = backoff
                        backoff = min(max_backoff, backoff * 2)
                        _log({"keeper_backoff": "no endpoint; next retry in %ds (fail %d/%d)"
                              % (slept, fails, max_fails)})
                        _keeper_stop.wait(slept)
                        continue
            except Exception as e:  # noqa: BLE001
                fails += 1
                _log({"keeper_exc": repr(e)[:150]})
                if max_fails > 0 and fails >= max_fails:
                    _log({"keeper": "连续 %d 次异常，停止重试" % fails})
                    return
                slept = backoff
                backoff = min(max_backoff, backoff * 2)
                _keeper_stop.wait(slept)
                continue
            _keeper_stop.wait(15)

    _keeper_thread = threading.Thread(target=_loop, name="seg-exit-keeper", daemon=True)
    _keeper_thread.start()
    return _keeper_thread


def _safe_pre_acquire():
    if not IPROCKET_ENABLED:
        return
    try:
        acquire_route(ttl=300)
    except Exception as e:  # noqa: BLE001
        _log({"pre_acquire": "ERR %r" % e})


def apply(config=None, log=print):
    """Wire the three segments into the engine's config module.

    Call ONCE, early (before sessions are built).  Returns a dict describing the
    effective policy.  With SUBMIT_EXIT=residential it: (a) installs the bare-socket
    SOCKS5 shim, (b) blanks config.PROXY so the protocol layer adds no proxy of its
    own, (c) starts a background keeper that keeps a validated residential route
    fresh, (d) forces DOWNLOAD_DIRECT.  It never sends /chat/completion by itself.
    """
    global SUBMIT_EXIT, REGISTER_PROXY
    assert BANNED_PROXY not in REGISTER_PROXY, "127.0.0.1:7890 must not be used (hint 830)"

    # ★ clash 别名在这里就地解析成真实的 http 代理 URL，之后所有分支只看到
    #   residential / direct / http(s)://... 三种规范值（面板与引擎行为一致）。
    if is_clash_alias(SUBMIT_EXIT):
        SUBMIT_EXIT = "direct"
        os.environ["DOLA_SUBMIT_EXIT"] = "direct"
        os.environ.setdefault("DOLA_PROXY", CLASH_PROXY)
        if config is not None:
            try:
                config.PROXY = CLASH_PROXY
            except Exception:  # noqa: BLE001
                pass

    info = {"register_proxy": REGISTER_PROXY, "submit_exit": SUBMIT_EXIT,
            "submit_tunnel": list(SUBMIT_TUNNEL), "download": "direct"}

    # 【IPRocket 流量治理 · 2026-10-17】把排除名单装上（对所有出口模式都装，
    # 因为 shim 一旦 install 过就会常驻拦截 create_connection）。
    _exclude_env = os.environ.get("DOLA_SUBMIT_TUNNEL_EXCLUDE", "")
    if _exclude_env.strip():
        info["submit_tunnel_exclude"] = set_tunnel_exclude(_exclude_env)

    if SUBMIT_EXIT in ("residential", ""):
        install_shim()
        _state["tunnel_hosts"] = set(SUBMIT_TUNNEL)
        _state["bypass"] = set()
        if not IPROCKET_ENABLED:
            # 停用：不预取、不启动 keeper、不置 strict —— 让提交段直接落回
            # 「无路由」的普通直连路径，而不是抛「no validated residential endpoint」
            # 把出片彻底卡死。要出片请把 DOLA_SUBMIT_EXIT 改成 clash 或填 http 代理。
            _log({"apply": "IPRocket disabled; submit will NOT tunnel (set DOLA_SUBMIT_EXIT)"})
            info["mode"] = "iprocket-disabled"
        else:
            # pre-acquire in the BACKGROUND so `import config` never blocks on the iprocket
            # API (a hung fetch used to stall the whole process start). The lazy path in
            # _patched_create_connection will fetch one on the first submit if this hasn't
            # finished yet.
            threading.Thread(target=_safe_pre_acquire, name="seg-exit-praeacquire",
                             daemon=True).start()
            _state["strict"] = True           # never fall through to a direct dola connect
            info["mode"] = "residential-socks5-shim-lazy"

    # ---- 非住宅分支：mode 无条件算出来（面板/引擎的诊断与测试都读它）----
    elif SUBMIT_EXIT == "direct":
        # direct = 不经住宅 shim。若环境里给了 DOLA_PROXY（如 Clash 7897）就用它，
        # 空才是真正的本机直连。★ clash 别名在上面已把 DOLA_PROXY 设成 Clash，
        # 所以这里能拿到 http://127.0.0.1:7897 —— 面板与引擎行为因此一致。
        inherited = os.environ.get("DOLA_PROXY", "").strip()
        info["mode"] = "clash-http-proxy" if inherited else "direct"
        if inherited:
            info["proxy"] = inherited
    else:
        info["mode"] = "http-proxy"
        info["proxy"] = SUBMIT_EXIT

    if config is not None:
        try:
            config.REGISTER_PROXY = REGISTER_PROXY
        except Exception:
            pass
        if SUBMIT_EXIT in ("residential", "") and IPROCKET_ENABLED:
            start_keeper()
            try:
                config.PROXY = ""          # requests layer adds no proxy; shim tunnels dola
            except Exception:
                pass
        elif SUBMIT_EXIT == "direct":
            try:
                config.PROXY = os.environ.get("DOLA_PROXY", "").strip()
            except Exception:
                pass
        else:
            try:
                config.PROXY = SUBMIT_EXIT
            except Exception:
                pass
        try:
            config.DOWNLOAD_DIRECT = True
            config.SUBMIT_EXIT = SUBMIT_EXIT
            config.SUBMIT_TUNNEL = list(SUBMIT_TUNNEL)
        except Exception:
            pass
    return info


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(apply(None))
    start_keeper()
    time.sleep(3)
    print("route:", current_route_view(), "egress:", egress_ip())

