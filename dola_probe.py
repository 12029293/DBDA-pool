#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dola (豆包海外版 / Cici) 邮箱注册链路探测器
=========================================
用途：定位 Dola 网页端「邮箱注册」到底走的哪几个 API，并把真实链路跑通。

零第三方依赖，只用 Python 标准库。Python 3.8+ 可直接跑。

已知事实（已实证，勿改）：
  * Dola 前端 UI 没有任何邮箱入口（chat.js / login-modal 里 email 出现 0 次）
  * Passport 域是 www.dola.com（不是 accounts.doubao.com）
  * 固定参数：aid=495671  account_sdk_source=web  sdk_version=2.2.11-doubao.0  device_platform=web
  * 已实证存在的接口：/passport/web/email/send_code/
      1031 "Enter a valid email address"  -> 邮箱格式本地校验失败（接口存在）
      1204 "Something went wrong. Try again." -> 格式通过后被服务端拒（大陆 IP 高频命中）
  * 16 = 该应用无权限 / 接口不存在（排除项）
  * 22 = 非法应用（缺 app 参数）

用法速查：
  python dola_probe.py check                     # 1. 查出口 IP / 是否大陆 / 代理是否生效
  python dola_probe.py probe                     # 2. 批量枚举候选接口，找出哪些真实存在
  python dola_probe.py send  --email x@mailto.plus     # 3. 发验证码
  python dola_probe.py inbox --email x@mailto.plus     # 4. 从 tempmail.plus 收取验证码
  python dola_probe.py auto  --email x@mailto.plus     # 5. 全自动：发码 -> 收码 -> 撞所有 verify 接口

代理：
  --proxy http://127.0.0.1:7890      # Clash 混合端口（推荐，HTTP 协议）
  --proxy socks5://127.0.0.1:1080   # 也支持 SOCKS5（内置纯标准库实现，无需 pysocks）
  --proxy http://user:pass@host:port
  --proxy 10090                     # 只给端口也行，自动按 http 试、再按 socks5 试
"""
import argparse
import http.cookiejar
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# hint855：外部进程（ffmpeg/ffprobe/tasklist 等）不得弹控制台窗。
# 注意 CREATE_NO_WINDOW 在父进程已有控制台时会被忽略，必须叠加 DETACHED_PROCESS。
_NO_WIN = 0
if os.name == "nt":
    _NO_WIN = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
               | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

# ---------------------------------------------------------------- 控制台中文
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 常量
BASE_HOST = "https://www.dola.com"
BASE_PARAMS = {
    "aid": "495671",
    "account_sdk_source": "web",
    "sdk_version": "2.2.11-doubao.0",
    "device_platform": "web",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TEMPMAIL_API = "https://tempmail.plus/api/mails"

# 候选接口清单：注册/登录/验证码/设密码 全路径轰炸
CANDIDATES = [
    # === 已实证跑通（2026-09-26，HK 出口）===
    "/passport/web/email/register_verify_login/",   # 【第二步】注册+校验+登录，返回完整登录态
    "/passport/web/email/send_code/",               # 【第一步】发验证码，必须 type=16
    "/passport/web/email/code_login/",              # 已存在账号的验证码登录（新号不走这个）
    # 验证码校验
    "/passport/web/email/verify_code/",
    "/passport/web/email/code/verify/",
    "/passport/web/email/verify/",
    "/passport/web/email/check_code/",
    "/passport/web/email/validate_code/",
    # 注册 / 注册或登录
    "/passport/web/email/register/",
    "/passport/web/email/register_or_login/",
    "/passport/web/email/login_or_register/",
    "/passport/web/email/signup/",
    "/passport/web/user/register/",
    "/passport/web/user/signup/",
    "/passport/web/account/register/",
    "/passport/web/register/",
    # 登录
    "/passport/web/email/login/",
    "/passport/web/email/password_login/",
    "/passport/web/user/login/",
    # 密码
    "/passport/web/email/set_password/",
    "/passport/web/user/set_password/",
    "/passport/web/password/set/",
    "/passport/web/account/set_password/",
    # 绑定
    "/passport/web/email/bind/",
    "/passport/web/user/bind_email/",
    # 其他版本前缀
    "/passport/v2/email/send_code/",
    "/passport/email/send_code/",
    "/passport/web/email/send/",
    # 手机对照（用来确认 16 是"不存在"而非网络问题）
    "/passport/web/mobile/send_code/",
]

# verify 阶段的参数名变体（字节各产品命名不统一）
VERIFY_PARAM_SETS = [
    {"email": "{email}", "code": "{code}", "type": "16"},
    {"email": "{email}", "code": "{code}"},
    {"email": "{email}", "verify_code": "{code}"},
    {"email": "{email}", "captcha": "{code}"},
    {"email": "{email}", "code": "{code}", "type": "34"},
    {"account": "{email}", "code": "{code}"},
    {"email": "{email}", "email_code": "{code}"},
]

EXTRA_PARAM_VARIANTS = [
    {"type": "16"},
    {},
    {"type": "34"},
    {"mix_mode": "1"},
    {"scene": "register"},
]

# 错误码词典
ERR_DICT = {
    16: "接口不存在 / 该应用无权限 -> 排除",
    22: "非法应用（缺 app 参数） -> 排除",
    1031: "邮箱格式校验失败 -> 【接口真实存在】",
    1204: "通用错误 / 被服务端拒绝（大陆 IP 或风控）",
    1202: "验证码无效（新鲜码首次校验常返此码，待攻破）",
    1702: "Type error（type 参数值不对）",
    1703: "验证码过期或已被消耗",
    1355: "邮件模板不存在（type 值不对）",
    1011: "账号不存在",
    1014: "邮箱不能为空（参数名或 Content-Type 不对）",
    1024: "验证码错误 / 过期",
    1009: "验证码错误",
    1068: "发送过于频繁，稍后再试",
    1000: "未知错误",
    0: "成功",
}


def explain(code):
    return ERR_DICT.get(code, "未登记错误码")


# ---------------------------------------------------------------- TLS
TLS_CA_BUNDLE = os.path.join("certs", "tempmail-plus-chain.pem")


def tls_ca_bundle_path():
    """Return the bundled CA supplement path for source and PyInstaller runs."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, TLS_CA_BUNDLE)


def create_ssl_context():
    """Use system trust plus the current tempmail.plus issuer chain."""
    context = ssl.create_default_context()
    bundle = tls_ca_bundle_path()
    if os.path.isfile(bundle):
        context.load_verify_locations(cafile=bundle)
    return context


# ---------------------------------------------------------------- SOCKS5（纯标准库自实现）
def socks5_connect(host, port, proxy_host, proxy_port, timeout=12,
                   username=None, password=None):
    """SOCKS5 握手 + CONNECT，返回已连到目标的裸 socket（支持账密认证）"""
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    if username:
        # 同时提供「无认证」与「账密认证」两种方法，由代理端选择
        s.sendall(b"\x05\x02\x00\x02")
        ver, method = s.recv(2)
        if ver != 5 or method == 0xFF:
            s.close()
            raise IOError("socks5 握手失败")
        if method == 0x02:  # 账密认证子协商（RFC 1929）
            u = username.encode()
            p = (password or "").encode()
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            rep = s.recv(2)
            if len(rep) < 2 or rep[1] != 0:
                s.close()
                raise IOError("socks5 账密认证失败")
    else:
        s.sendall(b"\x05\x01\x00")
        ver, method = s.recv(2)
        if ver != 5 or method == 0xFF:
            s.close()
            raise IOError("socks5 握手失败")
    addr = host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(addr)]) + addr + struct.pack(">H", port))
    resp = s.recv(10)
    if len(resp) < 2 or resp[1] != 0:
        s.close()
        raise IOError("socks5 连接被拒: %s" % resp.hex())
    return s


def socks5_request(url, method="GET", headers=None, body=None,
                   proxy_host="127.0.0.1", proxy_port=1080, timeout=15,
                   tls_context=None, username=None, password=None):
    """经 SOCKS5 发一个 HTTP(S) 请求，返回 (status, body, set_cookies)"""
    u = urllib.parse.urlparse(url)
    host = u.hostname
    port = u.port or (443 if u.scheme == "https" else 80)
    path = u.path + ("?" + u.query if u.query else "")
    s = socks5_connect(host, port, proxy_host, proxy_port, timeout,
                       username=username, password=password)
    if u.scheme == "https":
        context = tls_context or create_ssl_context()
        s = context.wrap_socket(s, server_hostname=host)
    if isinstance(body, dict):
        body = urllib.parse.urlencode(body).encode()
    elif isinstance(body, str):
        body = body.encode()
    h = {"Host": host, "User-Agent": UA, "Accept": "*/*",
         "Accept-Encoding": "identity", "Connection": "close"}
    if body:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        h["Content-Length"] = str(len(body))
    for k, v in (headers or {}).items():
        h[k] = v
    raw = ("\r\n".join(["%s %s HTTP/1.1" % (method, path)] +
                       ["%s: %s" % (k, v) for k, v in h.items()]) + "\r\n\r\n").encode()
    if body:
        raw += body
    s.sendall(raw)
    buf = b""
    while True:
        chunk = s.recv(8192)
        if not chunk:
            break
        buf += chunk
    s.close()
    txt = buf.decode("utf-8", errors="replace")
    head, _, payload = txt.partition("\r\n\r\n")
    try:
        status = int(head.split("\r\n")[0].split(" ")[1])
    except Exception:
        status = -1
    cookies = {}
    for line in head.split("\r\n"):
        if line.lower().startswith("set-cookie:"):
            kv = line.split(":", 1)[1].split(";")[0].strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                cookies[k] = v
    # 有些服务端无视 Accept-Encoding: identity 仍然 chunked，必须解包，
    # 否则 body 前面会挂着 "13b\r\n" 之类的长度头，json.loads 直接失败。
    if "chunked" in head.lower() or re.match(r"^[0-9a-fA-F]{1,8}\r\n", payload):
        payload = dechunk(payload)
    return status, payload, cookies


def dechunk(payload):
    """解 HTTP chunked 编码（payload 需为 str）"""
    if isinstance(payload, str):
        data = payload.encode("utf-8", errors="replace")
    else:
        data = payload
    out = b""
    i = 0
    while i < len(data):
        j = data.find(b"\r\n", i)
        if j < 0:
            break
        try:
            size = int(data[i:j].split(b";")[0].strip() or b"0", 16)
        except Exception:
            break
        if size == 0:
            break
        out += data[j + 2: j + 2 + size]
        i = j + 2 + size + 2
    return out.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 网络层
class Client:
    def __init__(self, proxy=None, timeout=20, verbose=False):
        self.timeout = timeout
        self.verbose = verbose
        self.cookiejar = http.cookiejar.CookieJar()
        self.socks = None
        self._cookies = {}
        self.ssl_context = create_ssl_context()
        handlers = [
            urllib.request.HTTPCookieProcessor(self.cookiejar),
            urllib.request.HTTPSHandler(context=self.ssl_context),
        ]
        if proxy:
            p = proxy if "://" in proxy else "http://" + proxy
            pu = urllib.parse.urlparse(p)
            if pu.scheme in ("socks5", "socks", "socks5h", "socks4"):
                # 支持账密：socks5://user:pass@host:port（IPRocket 粘性会话需要）
                self.socks = (pu.hostname, pu.port or 1080,
                              urllib.parse.unquote(pu.username or "") or None,
                              urllib.parse.unquote(pu.password or "") or None)
            else:
                handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
        else:
            # 不主动清理环境代理：留给用户控制
            handlers.append(urllib.request.ProxyHandler(urllib.request.getproxies()))
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [("User-Agent", UA)]

    def _raw(self, url, data=None, headers=None, method=None):
        if self.socks:
            return self._raw_socks(url, data, headers, method)
        req_body = None
        if data is not None:
            if isinstance(data, dict):
                req_body = urllib.parse.urlencode(data).encode()
            else:
                req_body = data.encode() if isinstance(data, str) else data
        req = urllib.request.Request(url, data=req_body, method=method or ("POST" if req_body else "GET"))
        req.add_header("Referer", BASE_HOST + "/chat/")
        req.add_header("Origin", BASE_HOST)
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("Accept-Language", "en-US,en;q=0.9")
        if req_body and method != "GET":
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        t0 = time.time()
        try:
            resp = self.opener.open(req, timeout=self.timeout)
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body, dict(resp.headers), time.time() - t0
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            return e.code, body, dict(e.headers or {}), time.time() - t0
        except Exception as e:
            return -1, "EXC: %r" % e, {}, time.time() - t0

    def get(self, url, headers=None):
        return self._raw(url, None, headers, "GET")

    def post(self, url, data, headers=None):
        return self._raw(url, data, headers, "POST")

    def _raw_socks(self, url, data=None, headers=None, method=None):
        m = method or ("POST" if data else "GET")
        h = {"Referer": BASE_HOST + "/chat/", "Origin": BASE_HOST,
             "Accept-Language": "en-US,en;q=0.9"}
        if self._cookies:
            h["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in self._cookies.items())
        for k, v in (headers or {}).items():
            h[k] = v
        t0 = time.time()
        last = None
        # 住宅代理网关的 CONNECT 有概率抖动（general failure / 超时）。
        # 失败发生在 SOCKS 握手/CONNECT 阶段，请求尚未到达目标服务器，重试安全。
        for attempt in range(5):
            try:
                st, body, ck = socks5_request(url, m, h, data,
                                              self.socks[0], self.socks[1], self.timeout,
                                              tls_context=self.ssl_context,
                                              username=self.socks[2] if len(self.socks) > 2 else None,
                                              password=self.socks[3] if len(self.socks) > 3 else None)
                self._cookies.update(ck)
                return st, body, {}, time.time() - t0
            except Exception as e:
                last = e
                time.sleep(0.6 * (attempt + 1))
        return -1, "EXC: %r" % last, {}, time.time() - t0

    def cookies(self):
        if self.socks:
            return self._cookies
        return {c.name: c.value for c in self.cookiejar}


HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):  # 单体 exe：写文件一律落到 exe 所在目录
    HERE = os.path.dirname(os.path.abspath(sys.executable))
HARVEST_FILE = os.path.join(HERE, "candidates_extra.json")

# ---------------------------------------------------------------- 从 JS 里挖真实路径
def load_candidates():
    """内置候选 + harvest 挖出来的候选，去重"""
    extra = []
    if os.path.exists(HARVEST_FILE):
        try:
            with open(HARVEST_FILE, encoding="utf-8") as f:
                extra = json.load(f).get("paths", [])
        except Exception:
            pass
    seen, out = set(), []
    for p in list(CANDIDATES) + list(extra):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def cmd_harvest(cli, args):
    """抓取 www.dola.com 前端 JS，正则挖出所有 /passport/ 真实路径"""
    print("=" * 100)
    print("[harvest] 从 Dola 前端 JS bundle 挖掘真实 passport 接口路径")
    print("=" * 100)
    st, html, _, _ = cli.get(BASE_HOST + "/chat/")
    print("GET /chat/ -> HTTP %s  (%d bytes)" % (st, len(html)))
    if st != 200:
        print("首页都拿不到，放弃。"); return

    js_urls = set()
    # 1) <script src>
    for m in re.finditer(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', html):
        js_urls.add(urllib.parse.urljoin(BASE_HOST, m.group(1)))
    # 2) modern.js 路由 manifest（996 条路由，chunk 全在里面）
    for key in ("window._MODERNJS_ROUTE_MANIFEST", "window._MODERNJS_MANIFEST"):
        idx = html.find(key)
        if idx < 0:
            continue
        st_pos = html.find("{", idx)
        if st_pos < 0:
            continue
        depth, i, in_str, esc = 0, st_pos, False, False
        while i < len(html):
            ch = html[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
            i += 1
        blob = html[st_pos:i + 1]
        for m in re.finditer(r'["\']([A-Za-z0-9_./\-]+\.js)["\']', blob):
            js_urls.add(urllib.parse.urljoin(BASE_HOST, "/_modernjs/" + m.group(1)))
        print("  解析 %s -> 当前 JS 候选 %d 个" % (key, len(js_urls)))
    # 3) HTML 里其它裸 js 引用
    for m in re.finditer(r'["\']([^"\']*?/[A-Za-z0-9_.\-]+\.js)["\']', html):
        c = m.group(1)
        if c.startswith("http"):
            js_urls.add(c)
        elif c.startswith("/"):
            js_urls.add(urllib.parse.urljoin(BASE_HOST, c))
    # 关键字优先：登录/账号/passport/邮箱相关 chunk 最可能藏接口
    KW = ("login", "account", "passport", "auth", "user", "flow", "register",
          "signup", "email", "verify", "sso", "security", "modal", "phone")

    def score(u):
        lu = u.lower()
        return sum(1 for k in KW if k in lu)

    js_urls = sorted({u for u in js_urls if score(u) > 0}, key=lambda u: (-score(u), u))
    print("发现 JS 文件: %d 个（已按登录/账号关键字过滤排序），取前 %d 个并发下载"
          % (len(js_urls), args.max_js))

    paths = set()
    pat = re.compile(r'["\'`](/passport/[A-Za-z0-9_/\-\.]{3,90}?)["\'`]')
    pat2 = re.compile(r'["\'`](/passport/[A-Za-z0-9_/\-\.]*?\$\{[^}]*\}[A-Za-z0-9_/\-\.]*)["\'`]')
    pat3 = re.compile(r'["\'`](/[A-Za-z0-9_]+(?:/[A-Za-z0-9_\-\.]+){1,6}/)["\'`]')

    tot_email = tot_sendcode = 0
    def fetch(u):
        try:
            st2, body, _, _ = cli.get(u)
        except Exception:
            return None
        if st2 != 200 or not body or len(body) > 4_000_000:
            return None
        return body

    from concurrent.futures import ThreadPoolExecutor
    done = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for body in ex.map(fetch, js_urls[:args.max_js]):
            if not body:
                continue
            done += 1
            low = body.lower()
            tot_email += low.count("email")
            tot_sendcode += low.count("send_code")
            for m in pat.finditer(body):
                paths.add(m.group(1))
            for m in pat2.finditer(body):
                paths.add(m.group(1))
            # 不限前缀：任何含关键字段的路径常量
            for m in pat3.finditer(body):
                p = m.group(1)
                if any(k in p.lower() for k in ("email", "code", "verify", "register",
                                                "login", "passport", "account", "auth",
                                                "user", "password", "captcha", "sms")):
                    paths.add(p)
    print("实际下载: %d 个" % done)

    # 清洗：去掉明显是静态资源的，统一结尾带 /
    clean = set()
    for p in paths:
        if any(p.endswith(x) for x in (".js", ".css", ".png", ".svg", ".json", ".map")):
            continue
        if not p.endswith("/"):
            p = p + "/"
        clean.add(p)
    clean = sorted(clean)
    print("\n挖到的 /passport/ 路径: %d 个" % len(clean))
    for p in clean:
        print("   " + p)
    print("\n" + "-" * 100)
    print("前端代码统计（本次实际下载的 %d 个登录/账号相关 chunk 全量）:" % done)
    print("   'email'     出现次数: %d" % tot_email)
    print("   'send_code' 出现次数: %d" % tot_sendcode)
    if tot_email <= 5 and tot_sendcode == 0:
        print("   >>> 结论: Dola 网页前端没有任何邮箱注册/验证码代码。")
        print("       但 /passport/web/email/send_code/ 在后端真实存在（返 1031/1204），")
        print("       说明这是 Passport 后端通用能力被 aid=495671 开放、前端未接 UI。")
        print("       => 批量注册只能直调 API；后两步路径前端挖不到，必须抓包或继续盲撞。")
    with open(HARVEST_FILE, "w", encoding="utf-8") as f:
        json.dump({"source": "harvest from www.dola.com JS bundles", "paths": clean}, f, ensure_ascii=False, indent=2)
    print("\n已写入 %s" % HARVEST_FILE)
    print("下一步: python dola_probe.py probe --proxy http://127.0.0.1:7890")


def cmd_har(cli, args):
    """解析抓包 HAR，反推真实的 passport 接口（最可靠的路径来源）"""
    print("=" * 100)
    print("[har] 从抓包 HAR 反推真实接口")
    print("=" * 100)
    if not args.har_file or not os.path.exists(args.har_file):
        print("需要 --har-file <xxx.har>（Charles / Fiddler / mitmproxy / Chrome DevTools 均可导出）")
        return
    with open(args.har_file, encoding="utf-8", errors="replace") as f:
        har = json.load(f)
    entries = har.get("log", {}).get("entries", [])
    print("HAR 条目总数: %d" % len(entries))
    hits, paths = [], []
    for e in entries:
        req = e.get("request", {})
        url = req.get("url", "")
        if "passport" not in url.lower():
            continue
        host = urllib.parse.urlparse(url).netloc
        if not any(h in host for h in ("dola", "doubao", "cici", "bytedance")):
            continue
        path = urllib.parse.urlparse(url).path
        method = req.get("method", "GET")
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        body_txt = ""
        pd = req.get("postData")
        if pd:
            body_txt = pd.get("text", "")[:300]
        resp = e.get("response", {})
        rbody = ""
        rc = resp.get("content", {})
        if rc.get("text"):
            rbody = rc["text"][:300]
        info = parse_ec(rbody) if rbody else {"error_code": None, "description": ""}
        hits.append({
            "method": method, "path": path, "host": host,
            "query": params, "post_body": body_txt,
            "status": resp.get("status"),
            "error_code": info["error_code"], "desc": info["description"],
        })
        if path not in paths:
            paths.append(path)
    print("命中 passport 请求: %d 条，去重路径 %d 个\n" % (len(hits), len(paths)))
    for h in hits:
        print("%-5s %-52s HTTP:%-4s code=%-6s %s" % (
            h["method"], h["path"], h["status"], h["error_code"], (h["desc"] or "")[:30]))
    print("\n--- 去重路径清单 ---")
    for p in paths:
        print("   " + p)
    # 合并进 candidates_extra.json
    merged = []
    if os.path.exists(HARVEST_FILE):
        try:
            with open(HARVEST_FILE, encoding="utf-8") as f:
                merged = json.load(f).get("paths", [])
        except Exception:
            pass
    for p in paths:
        if p not in merged:
            merged.append(p)
    with open(HARVEST_FILE, "w", encoding="utf-8") as f:
        json.dump({"source": "harvest + har", "paths": merged}, f, ensure_ascii=False, indent=2)
    out = os.path.join(HERE, "har_hits.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(hits, f, ensure_ascii=False, indent=2)
    print("\n已写入 %s 和 %s" % (HARVEST_FILE, out))
    print("下一步: python dola_probe.py probe --harvested --proxy http://127.0.0.1:7890")


def parse_ec(body):
    """从返回体里抠出 error_code / description / message"""
    out = {"error_code": None, "description": "", "message": "", "raw": body[:400]}
    try:
        j = json.loads(body)
        d = j.get("data") if isinstance(j.get("data"), dict) else j
        out["error_code"] = d.get("error_code")
        out["description"] = d.get("description") or j.get("message") or ""
        out["message"] = j.get("message", "")
        if out["error_code"] is None:
            out["error_code"] = j.get("error_code")
    except Exception:
        m = re.search(r'"error_code"\s*:\s*(-?\d+)', body)
        if m:
            out["error_code"] = int(m.group(1))
    return out


# ---------------------------------------------------------------- 自动找代理
PROC_PAT = re.compile(r"(mihomo|clash|v2ray|sing-box|hiddify|nekoray|猫猫|maomao|verge|meta)", re.I)
COMMON_PORTS = [7890, 7891, 7892, 10809, 10808, 1080, 1081, 20171, 2080, 2081,
                8889, 9090, 33210, 4780, 7078, 2019, 10090, 10080, 10810, 9091]


def _run(cmd):
    """执行系统命令；Windows 中文环境 tasklist/netstat 输出是 GBK，需容错解码"""
    import subprocess
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=25,
                           creationflags=_NO_WIN)   # hint855：不弹控制台
    except Exception:
        return ""
    raw = p.stdout or b""
    for enc in ("utf-8", "gbk", "cp936", "mbcs", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace")


def candidate_proxy_ports():
    """代理进程监听的端口 + 常见代理端口"""
    ports = set(COMMON_PORTS)
    pids = []
    # tasklist /FO CSV: "name","pid","session","sess#","mem"
    for line in _run(["tasklist", "/FO", "CSV", "/NH"]).splitlines():
        m = re.match(r'^\s*"([^"]+)"\s*,\s*"(\d+)"', line)
        if m and PROC_PAT.search(m.group(1)):
            pids.append(m.group(2))
            print("  识别到代理进程: %s  PID=%s" % (m.group(1), m.group(2)))
    if not pids:
        print("  (没识别到代理进程，只测常见端口)")
        return sorted(ports), pids
    for line in _run(["netstat", "-ano"]).splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 4 or parts[0] != "TCP":
            continue
        if pids and parts[-1] not in pids:
            continue
        try:
            port = int(parts[1].rsplit(":", 1)[1])
        except Exception:
            continue
        if 1024 < port < 65535:
            ports.add(port)
    return sorted(ports), pids


def test_proxy(port, timeout=10):
    """依次试 HTTP 代理和 SOCKS5，返回 (mode, ip, cc, city)；都失败返回 None"""
    tls_context = create_ssl_context()
    # ① HTTP 代理
    proxy = "http://127.0.0.1:%d" % port
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=tls_context))
        j = json.loads(opener.open(
            urllib.request.Request("https://ipwho.is/", headers={"User-Agent": UA}),
            timeout=timeout).read().decode())
        if j.get("ip"):
            return ("http", j.get("ip"), j.get("country_code"), j.get("city"))
    except Exception:
        pass
    # ② SOCKS5
    try:
        st, body, _ = socks5_request("https://ipwho.is/", "GET", None, None,
                                     "127.0.0.1", port, timeout,
                                     tls_context=tls_context)
        j = json.loads(body)
        if j.get("ip"):
            return ("socks5", j.get("ip"), j.get("country_code"), j.get("city"))
    except Exception:
        pass
    return None


def detect_proxy(max_test=60):
    print("=" * 100)
    print("[auto-proxy] 扫描本机代理端口，找第一个能出非大陆的")
    print("=" * 100)
    ports, pids = candidate_proxy_ports()
    print("代理进程 PID: %s" % (pids or "(没识别到，仅测常见端口)"))
    print("待测端口 %d 个: %s\n" % (len(ports), ports[:40]))
    ok, cn, fail = [], [], 0
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(zip(ports[:max_test], ex.map(test_proxy, ports[:max_test])))
    for p, res in results:
        if not res:
            fail += 1
            continue
        mode, ip, cc, city = res
        if cc and cc != "CN":
            print("  [OK]  %-6d %-7s %-16s %s / %s" % (p, mode, ip, cc, city))
            ok.append((p, mode, ip, cc))
        else:
            print("  [CN]  %-6d %-7s %-16s %s" % (p, mode, ip, cc))
            cn.append(p)
    print("\n可用非大陆代理端口: %s" % ([(p, m, i, c) for p, m, i, c in ok] if ok else "(无)"))
    print("大陆出口端口: %s   连不上的: %d 个" % (cn, fail))
    if ok:
        port, mode, ip, cc = ok[0]
        scheme = "socks5" if mode == "socks5" else "http"
        print("\n>>> 采用 --proxy %s://127.0.0.1:%d  （%s / %s / %s）" % (scheme, port, ip, cc, mode))
        return "%s://127.0.0.1:%d" % (scheme, port)
    print("\n>>> 没扫到能出非大陆的代理端口。可能原因：")
    print("    1) 客户端没开 HTTP 代理端口 / 只允许浏览器走")
    print("    2) 需要用户名密码认证")
    print("    3) 节点本身是大陆/或代理未连接")
    print("    -> 可在客户端设置里把端口改成 7890 并开「允许局域网连接」，或改用浏览器抓 HAR。")
    return None


# ---------------------------------------------------------------- 菜单
def menu():
    print("=" * 78)
    print("  Dola 邮箱注册链路探测器")
    print("=" * 78)
    print("  1  自动找代理 + 验证出口 (check)")
    print("  2  挖前端真实接口路径 (harvest)")
    print("  3  枚举候选接口哪些真存在 (probe)")
    print("  4  发验证码 (send)")
    print("  5  收验证码 (inbox)")
    print("  6  拿码撞所有校验接口 (verify)")
    print("  7  全自动：发码 -> 收码 -> 撞接口 (auto)")
    print("  8  解析抓包 HAR (har)")
    print("  9  一键注册出号并导出 Cookie (register)   <<< 已跑通")
    print("  0  退出")
    print("=" * 78)
    ch = input("选择 [1-8, 0=退出] > ").strip()
    mode = {"1": "check", "2": "harvest", "3": "probe", "4": "send",
            "5": "inbox", "6": "verify", "7": "auto", "8": "har",
            "9": "register"}.get(ch)
    if not mode:
        return
    args = argparse.Namespace(email=None, code=None, proxy=None, wait=120,
                              timeout=20, max_js=120, harvested=True,
                              har_file=None, password=None, verbose=False)
    if mode != "harvest":
        auto = input("自动扫描代理端口? [Y/n] > ").strip().lower()
        if auto in ("", "y", "yes"):
            args.proxy = detect_proxy()
            if args.proxy:
                input("\n回车继续...")
        else:
            p = input("输入代理端口或完整 proxy (如 7890 / http://127.0.0.1:7890) > ").strip()
            args.proxy = p if p.startswith("http") else ("http://127.0.0.1:%s" % p if p else None)
    if mode in ("send", "inbox", "verify", "auto", "check", "register"):
        e = input("邮箱 [直接回车用一个随机 mailto.plus] > ").strip()
        if not e:
            import random, string
            e = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10)) + "@mailto.plus"
            print("  使用: %s" % e)
        args.email = e
    if mode == "verify":
        args.code = input("验证码 > ").strip()
    if mode == "register":
        pw = input("设置密码 [直接回车=不设密码] > ").strip()
        args.password = pw or None
    if mode == "har":
        args.har_file = input("HAR 文件路径 > ").strip().strip('"')
    if mode == "inbox":
        w = input("等待秒数 [120] > ").strip()
        args.wait = int(w) if w.isdigit() else 120

    print()
    cli = Client(proxy=args.proxy, timeout=args.timeout, verbose=args.verbose)
    {"check": cmd_check, "probe": cmd_probe, "send": cmd_send,
     "inbox": cmd_inbox, "verify": cmd_verify, "auto": cmd_auto,
     "harvest": cmd_harvest, "har": cmd_har}[mode](cli, args)


# ---------------------------------------------------------------- 子命令 1：check
def cmd_check(cli, args):
    print("=" * 72)
    print("[1] 出口 IP / 代理生效检查")
    print("=" * 72)
    print("使用代理配置: %s" % (args.proxy or "(继承系统/环境变量)"))
    for name, url in [
        ("ipwho.is", "https://ipwho.is/"),
        ("icanhazip", "https://icanhazip.com/"),
        ("ipinfo", "https://ipinfo.io/json"),
    ]:
        st, body, _, el = cli.get(url)
        print("\n-- %s (HTTP %s, %.2fs)" % (name, st, el))
        if st == 200:
            try:
                j = json.loads(body)
                print("   IP      : %s" % j.get("ip"))
                print("   归属    : %s / %s / %s" % (j.get("country"), j.get("region"), j.get("city")))
                print("   ISP     : %s" % j.get("org"))
                cc = j.get("country")
                if cc == "CN":
                    print("   >>> 判定: 中国大陆出口，Dola 大概率风控拦截（1204）。请换非大陆节点。")
                elif cc in (None, ""):
                    print("   >>> 判定: 无法识别出口归属，可能代理未生效或查询接口失败。")
                else:
                    print("   >>> 判定: 非大陆出口（%s），可以继续。" % cc)
            except Exception:
                print("   " + body[:200])
        else:
            print("   失败: " + body[:200])

    print("\n" + "=" * 72)
    print("[2] Dola 可达性 + send_code 冒烟")
    print("=" * 72)
    st, body, _, _ = cli.get(BASE_HOST + "/chat/")
    print("GET /chat/ -> HTTP %s" % st)
    st, body, _, _ = cli.get(_url("/passport/web/email/send_code/", {"email": "notanemail"}))
    r = parse_ec(body)
    print("send_code(notanemail) -> code=%s  %s" % (r["error_code"], r["description"]))
    print("   期望 1031（说明接口在线、在做业务校验；与 IP 无关）")
    st, body, _, _ = cli.get(_url("/passport/web/email/send_code/", {"email": args.email or "smoke_test_0001@mailto.plus"}))
    r = parse_ec(body)
    print("send_code(合法邮箱)   -> code=%s  %s" % (r["error_code"], r["description"]))
    if r["error_code"] == 1204:
        print("   >>> 1204：格式通过后被服务端拒。最常见的两个原因：")
        print("       a) 出口 IP 在大陆 / 数据中心 IP 被风控  -> 换住宅 HK/SG 节点重试")
        print("       b) 缺少风控参数（captcha / msToken / verify ticket）")
    elif r["error_code"] in (0, None):
        print("   >>> 疑似发送成功！快去收件箱确认。")
    print("\n提示：先跑 `check` 确认非大陆出口，再跑 `send`。")


# ---------------------------------------------------------------- 子命令 2：probe
def _url(path, extra=None):
    p = dict(BASE_PARAMS)
    p.update(extra or {})
    return BASE_HOST + path + "?" + urllib.parse.urlencode(p)


def cmd_probe(cli, args):
    print("=" * 100)
    print("接口枚举：16=不存在(排除)  1031=真实存在(在做校验)  1204=被拦  其他=重点检查")
    print("=" * 100)
    found = []
    email = args.email or "probe_enum_0001@mailto.plus"
    targets = load_candidates() if args.harvested else CANDIDATES
    print("本轮枚举 %d 条路径（%s）\n" % (len(targets), "内置+harvest" if args.harvested else "内置"))
    for path in targets:
        row = {"path": path, "GET": None, "POST": None}
        # GET
        st, body, _, _ = cli.get(_url(path, {"email": email}))
        r = parse_ec(body)
        row["GET"] = (r["error_code"], r["description"], st)
        # POST（带多组参数变体，取第一个非 16 的）
        best = None
        for var in EXTRA_PARAM_VARIANTS:
            payload = dict(BASE_PARAMS)
            payload.update(var)
            payload["email"] = email
            st2, body2, _, _ = cli.post(BASE_HOST + path, payload)
            r2 = parse_ec(body2)
            code2 = r2["error_code"]
            if code2 not in (16, 22):
                best = (code2, r2["description"], st2, var)
                break
            if best is None:
                best = (code2, r2["description"], st2, var)
        row["POST"] = best
        g = row["GET"][0]
        mark = ""
        if g == 1031 or (row["POST"] and row["POST"][0] == 1031):
            mark = "  <<< 真实存在"
            found.append(path)
        elif g not in (16, 22):
            mark = "  <<< 非16/22，值得深挖"
            found.append(path)
        print("%-46s GET:%-6s %-42s | POST:%-6s %s%s" % (
            path,
            g, (row["GET"][1] or "")[:40],
            (row["POST"][0] if row["POST"] else "-"),
            ((row["POST"][1] or "")[:28] if row["POST"] else ""),
            mark,
        ))
    print("\n" + "=" * 100)
    print("疑似真实接口: %s" % (found if found else "(无，全部 16 -> 这批路径都不在 www.dola.com 上)"))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"candidates": targets, "found": found}, f, ensure_ascii=False, indent=2)
    print("已写入: %s" % out)


# ---------------------------------------------------------------- 子命令 3：send
def cmd_send(cli, args):
    if not args.email:
        print("需要 --email"); return
    print("[send] 目标邮箱: %s" % args.email)
    for var in EXTRA_PARAM_VARIANTS:
        payload = dict(BASE_PARAMS); payload.update(var); payload["email"] = args.email
        st, body, hd, el = cli.post(BASE_HOST + "/passport/web/email/send_code/", payload)
        r = parse_ec(body)
        print("  POST variant=%-22s -> HTTP %s code=%s %s" % (var or "{}", st, r["error_code"], r["description"]))
        if r["error_code"] not in (16, 22, 1204, 1031):
            print("  >>> 疑似成功，响应全文:\n" + body[:800]); break
    st, body, _, _ = cli.get(_url("/passport/web/email/send_code/", {"email": args.email}))
    r = parse_ec(body)
    print("  GET  -> code=%s %s" % (r["error_code"], r["description"]))
    if r["error_code"] == 1204:
        print("\n  >>> 1204。建议：换非大陆住宅 IP 后重试；仍 1204 说明缺风控参数。")


# ---------------------------------------------------------------- 子命令 4：inbox
def cmd_inbox(cli, args):
    if not args.email:
        print("需要 --email"); return
    print("[inbox] 轮询 %s 的收件箱（tempmail.plus），最多 %s 秒" % (args.email, args.wait))
    deadline = time.time() + args.wait
    seen = set()
    code = None
    while time.time() < deadline:
        st, body, _, _ = cli.get("%s?email=%s&limit=5" % (TEMPMAIL_API, urllib.parse.quote(args.email)))
        if st == 200:
            try:
                j = json.loads(body)
                for m in (j.get("mail_list") or []):
                    mid = m.get("mail_id") or m.get("id")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    st2, b2, _, _ = cli.get("%s/%s?email=%s" % (TEMPMAIL_API, mid, urllib.parse.quote(args.email)))
                    text = ""
                    try:
                        j2 = json.loads(b2)
                        text = (j2.get("text") or "") + " " + (j2.get("subject") or "")
                    except Exception:
                        text = b2
                    print("\n  新邮件 #%s  from=%s  subject=%s" % (mid, m.get("from"), m.get("subject")))
                    c = extract_code((m.get("subject") or "") + " " + text)
                    if c:
                        print("  >>> 提取到验证码: %s" % c)
                        code = c
                        break
            except Exception as e:
                print("  解析失败: %r  body=%s" % (e, body[:200]))
        if code:
            break
        time.sleep(5)
    if not code:
        print("  超时未收到验证码（等 %ss）" % args.wait)
    else:
        print("\n下一步: python dola_probe.py verify --email %s --code %s" % (args.email, code))
    return code


def extract_code(text):
    if not text:
        return None
    pats = [
        r"(?:code|Code|CODE)[^0-9]{0,20}(\d{6})",
        r"验证码[^0-9]{0,10}(\d{6})",
        r"(?:is|:)\s*(\d{6})",
        r"\b(\d{6})\b",
    ]
    for p in pats:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------- 子命令 5：verify
def cmd_verify(cli, args):
    if not (args.email and args.code):
        print("需要 --email 和 --code"); return
    print("=" * 100)
    print("[verify] 用 code=%s 撞所有候选校验接口" % args.code)
    print("=" * 100)
    hits = []
    targets = [p for p in (load_candidates() if args.harvested else CANDIDATES) if "send_code" not in p]
    print("本轮撞 %d 条路径\n" % len(targets))
    for path in targets:
        for ps in VERIFY_PARAM_SETS:
            payload = dict(BASE_PARAMS)
            for k, v in ps.items():
                payload[k] = v.replace("{email}", args.email).replace("{code}", args.code)
            st, body, hd, el = cli.post(BASE_HOST + path, payload)
            r = parse_ec(body)
            c = r["error_code"]
            if c in (16, 22):
                continue
            tag = ""
            if c in (0, None):
                tag = "  <<<<<< 成功"
                hits.append((path, ps, body[:300]))
            elif c in (1024, 1009):
                tag = "  <<< 码被接受但错误/过期（接口真，码不对）"
                hits.append((path, ps, body[:300]))
            elif c == 1031:
                tag = "  <<< 在做业务校验（接口真）"
                hits.append((path, ps, body[:300]))
            print("%-44s %-46s -> %-6s %s%s" % (path, json.dumps(ps, ensure_ascii=False)[:44], c, (r["description"] or "")[:26], tag))
            if c in (16, 22):
                pass
    print("\n" + "=" * 100)
    if hits:
        print("命中的真接口：")
        for p, ps, b in hits:
            print("  %s  params=%s" % (p, json.dumps(ps, ensure_ascii=False)))
            print("    resp=%s" % b.replace("\n", " ")[:260])
        print("\nCookie（若已下发登录态）: %s" % json.dumps(cli.cookies(), ensure_ascii=False)[:400])
    else:
        print("全部返回 16/22 -> 这批候选路径都不对，需要换思路（抓 App / WebView 流量取真实 path）")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_hits.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump([{"path": p, "params": ps, "resp": b} for p, ps, b in hits], f, ensure_ascii=False, indent=2)
    print("已写入: %s" % out)


# ---------------------------------------------------------------- 子命令 6：auto
def cmd_auto(cli, args):
    print("=" * 100)
    print("全自动链路：check -> send -> 收码 -> verify")
    print("=" * 100)
    st, body, _, _ = cli.get("https://ipinfo.io/json")
    try:
        j = json.loads(body)
        print("出口: %s / %s" % (j.get("ip"), j.get("country")))
        if j.get("country") == "CN":
            print("!! 大陆出口，成功率极低。建议 Ctrl+C 后加 --proxy 重跑。\n")
    except Exception:
        pass
    cmd_send(cli, args)
    code = cmd_inbox(cli, args)
    if not code:
        print("未拿到验证码，链路中断。"); return
    args.code = code
    cmd_verify(cli, args)


# ---------------------------------------------------------------- 子命令 7：register（一键注册出号）
REGISTER_PATH = "/passport/web/email/register_verify_login/"


def cmd_register(cli, args):
    import random
    import string
    print("=" * 100)
    print("一键注册：发码 -> 收码 -> register_verify_login -> 补风控 cookie -> 导出 CK")
    print("=" * 100)
    E = args.email or ("".join(random.choice(string.ascii_lowercase + string.digits)
                               for _ in range(10)) + "@mailto.plus")
    args.email = E
    print("邮箱: %s" % E)

    try:
        cli.get(BASE_HOST + "/chat/")
    except Exception:
        pass

    st, b, _, _ = cli.get(_url("/passport/web/email/send_code/", {"email": E, "type": "16"}))
    print("[1] 发码: %s" % (b or "")[:160])
    if "success" not in (b or ""):
        print("    发码失败，终止。检查代理出口是否非大陆。")
        return

    code = cmd_inbox(cli, args)
    if not code:
        print("    未收到验证码，终止。")
        return
    print("[2] 验证码: %s" % code)

    payload = dict(BASE_PARAMS)
    payload.update({"email": E, "type": "16", "code": code})
    if getattr(args, "password", None):
        payload["password"] = args.password
    st, b, _, _ = cli.post(BASE_HOST + REGISTER_PATH, payload)
    print("[3] 注册 HTTP %s" % st)
    try:
        dat = (json.loads(b) or {}).get("data") or {}
    except Exception:
        dat = {}
    if not dat.get("user_id_str"):
        print("    失败: %s" % (b or "")[:300])
        return
    print("    user_id = %s" % dat.get("user_id_str"))
    print("    name    = %s" % dat.get("name"))

    # 带登录态访问页面，补齐 ttwid / slardar 等风控 cookie
    for u in (BASE_HOST + "/", BASE_HOST + "/chat/"):
        try:
            cli.get(u)
        except Exception:
            pass

    cks = cli.cookies()
    ckstr = "; ".join("%s=%s" % (k, v) for k, v in cks.items())
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "cookie_%s.txt" % E.split("@")[0])
    with open(out, "w", encoding="utf-8") as f:
        f.write(ckstr)
    print("[4] Cookie %d 个 -> %s" % (len(cks), out))
    print("    %s" % ckstr[:400])

    csrf = cks.get("passport_csrf_token")
    st2, b2, _, _ = cli.get(_url("/passport/account/info/v2/", {}),
                            headers={"x-tt-passport-csrf-token": csrf} if csrf else None)
    print("[5] 反查账号: %s" % (b2 or "")[:300])


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Dola 邮箱注册链路探测器", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default=None,
                    choices=["check", "probe", "send", "inbox", "verify", "auto",
                             "harvest", "har", "proxy", "menu", "register"])
    ap.add_argument("--email", help="目标邮箱（mailto.plus 临时邮箱）")
    ap.add_argument("--password", help="注册时同时设置密码（可选；设了 has_password=1）")
    ap.add_argument("--code", help="邮箱验证码（verify 模式）")
    ap.add_argument("--proxy", help="HTTP 代理，如 http://127.0.0.1:7890")
    ap.add_argument("--wait", type=int, default=90, help="收件箱轮询秒数，默认 90")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--max-js", type=int, default=40, help="harvest 最多下载几个 JS，默认 40")
    ap.add_argument("--harvested", action="store_true", help="probe/verify 时合并 harvest 挖到的路径")
    ap.add_argument("--har-file", help="har 模式：抓包导出的 .har 文件路径")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.mode is None or args.mode == "menu":
        menu()
        return
    if args.mode == "proxy":
        detect_proxy()
        return
    cli = Client(proxy=args.proxy, timeout=args.timeout, verbose=args.verbose)
    {"check": cmd_check, "probe": cmd_probe, "send": cmd_send,
     "inbox": cmd_inbox, "verify": cmd_verify, "auto": cmd_auto,
     "harvest": cmd_harvest, "har": cmd_har,
     "register": cmd_register}[args.mode](cli, args)


if __name__ == "__main__":
    main()
