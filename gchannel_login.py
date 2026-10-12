# -*- coding: utf-8 -*-
"""Google 渠道登录 -> dola（面板取号 C 链路）。

流程（对应面板输入 `账号|密码|取件地址`）：
  1. 打开 www.google.com → 点「登录」→ 输入账号 → 密码 → 点登录；
  2. 自动处理登录中的弹页：
     * 「两步验证提醒」强推页 → 点「以后再说」（Not now）；
     * 「邮件验证码」挑战 → 从取件地址 API 拉码回填
       （GET {取件地址}/v1.php?email=<账号>&action=otp，空串=未到，轮询）；
  3. 登录成功后打开 dola → 点登录 → 选「Google 登录」→
     同一浏览器已有 Google 会话，OAuth 自动过账号选择/同意页 →
     轮询拿 sessionid / passport_auth_status。

返回结构与 ck_weblogin.run 同构（cookies_full / auth_status / cookies），
硬闸校验与入池统一由 take_service 处理。

引擎：patchright Chromium。Google 登录页对无头检测严格
（HeadlessChrome UA 等 5 项泄漏，2026-10-11 实测），**本链路强制有头**。
"""
from __future__ import annotations

import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WORKDIR = Path(__file__).parent.absolute()
BASE = "https://www.dola.com"

# dola 首页登录入口 / Google 选项（多语言，与 ck_weblogin 一致）
LOGIN_TEXTS = ["ログイン", "Log In", "Sign in", "登录", "로그인"]
GOOGLE_TEXTS = ["Googleで続ける", "Continue with Google", "Googleでログイン",
                "Sign in with Google", "登录 Google", "Google"]

# Google 官网首页登录入口
SIGNIN_TEXTS = ["登录", "Sign in", "ログイン", "로그인"]

# 「以后再说」类弹页按钮（两步验证强推 / 备用邮箱提醒等）
LATER_TEXTS = ["以后再说", "稍后再说", "暂不启用", "暂不", "以后",
               "Not now", "not now", "後で", "あとで", "スキップ",
               "Skip", "Bỏ qua", "Để sau"]

# 同意/继续类按钮
CONSENT_TEXTS = ["#submit_button"]
CONSENT_LABELS = ["继续", "Continue", "続行", "I agree", "同意", "Tôi đồng ý"]

# 「试试其他方式」（卡 2FA 且无 TOTP 时自救）
OTHER_WAY_TEXTS = ["试试其他方式", "尝试其他方式", "其他方式", "Try another way",
                   "別の方法", "別の方法を試す", "Thử cách khác"]


def say(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ 取件 API
def fetch_otp_once(mail_api: str, email: str, timeout: int = 15) -> str:
    """从取件地址拉一次验证码：优先 action=otp（纯文本），回落到列表 JSON 的 otp 字段。"""
    base = (mail_api or "").strip().rstrip("/")
    if not base:
        return ""
    q = urllib.parse.quote(email, safe="")
    try:
        txt = urllib.request.urlopen(
            "%s/v1.php?email=%s&action=otp" % (base, q), timeout=timeout).read()
        code = txt.decode("utf-8", "replace").strip()
        if re.fullmatch(r"[0-9A-Za-z]{4,10}", code):
            return code
    except Exception:
        pass
    try:
        data = json.loads(urllib.request.urlopen(
            "%s/v1.php?email=%s" % (base, q), timeout=timeout).read().decode("utf-8", "replace"))
    except Exception:
        return ""
    mails = data.get("mails") or data.get("messages") or data.get("data") or []
    if isinstance(mails, dict):
        mails = list(mails.values())
    for m in reversed(mails):          # 最新在前
        if isinstance(m, dict) and m.get("otp"):
            return str(m["otp"]).strip()
    return ""


def wait_otp(mail_api: str, email: str, deadline_s: int = 150) -> str:
    """轮询取件地址拿验证码；拿到立刻返回，超时返回空串。"""
    if not (mail_api or "").strip():
        return ""
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        code = fetch_otp_once(mail_api, email)
        if code:
            return code
        time.sleep(5)
    return ""


# ------------------------------------------------------------------ 页面工具
async def _visible(g, sel: str) -> bool:
    try:
        loc = g.locator(sel).first
        return bool(await loc.count()) and await loc.is_visible()
    except Exception:
        return False


async def _click_texts(g, texts, label: str) -> str:
    """按文本候选点击可见按钮（get_by_text 模糊匹配，兼容 button/[role]/a）。"""
    for t in texts:
        try:
            loc = g.get_by_text(t, exact=True).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=5000)
                say("[g] 点击 %s: %r" % (label, t))
                return t
        except Exception:
            continue
        try:
            loc = g.locator("button:has-text('%s'), [role='button']:has-text('%s')"
                            % (t.replace("'", ""), t.replace("'", ""))).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=5000)
                say("[g] 点击 %s: %r" % (label, t))
                return t
        except Exception:
            continue
    return ""


# ------------------------------------------------------------------ Google 登录状态机
async def google_login_state(g, email: str, password: str,
                             mail_api: str = "", otp_wait: int = 150,
                             max_steps: int = 40) -> None:
    """在 accounts.google.com 上跑完登录；离开该域即认为完成。

    比 add_account.google_login 多处理：
      * 两步验证强推页「以后再说」；
      * 邮件验证码挑战（从取件地址拉码）；
      * 2FA 卡死时先试「试试其他方式」。
    """
    otp_used = ""
    for step in range(max_steps):
        await g.wait_for_timeout(2200)
        if "accounts.google.com" not in g.url:
            say("[g] 已离开 accounts.google.com（登录完成）: %s" % g.url[:80])
            return
        body = ""
        try:
            body = (await g.evaluate(
                "() => (document.body && document.body.innerText || '')"))[:600]
        except Exception:
            pass

        # 1) 账号选择页（OAuth 回跳时直接点已有会话的账号）
        if "accountchooser" in g.url or "ServiceLogin" in g.url:
            acc = g.locator("text=%s" % email).first
            try:
                if await acc.count() and await acc.is_visible():
                    await acc.click(timeout=5000)
                    say("[g] 账号选择页 → 点 %s" % email)
                    continue
            except Exception:
                pass

        # 2) 邮箱页
        ident = g.locator("#identifierId").first
        if await ident.count() and await ident.is_visible():
            await ident.fill(email)
            try:
                await g.locator("#identifierNext").evaluate("e => e.click()")
            except Exception:
                await g.keyboard.press("Enter")
            say("[g] 邮箱页 → 提交")
            await g.wait_for_timeout(1500)
            continue

        # 3) 密码页
        pwd = g.locator('input[name="Passwd"]').first
        if await pwd.count() and await pwd.is_visible():
            await g.wait_for_timeout(600)
            await pwd.fill(password)
            try:
                await g.locator("#passwordNext").evaluate("e => e.click()")
            except Exception:
                await g.keyboard.press("Enter")
            say("[g] 密码页 → 提交")
            await g.wait_for_timeout(1800)
            continue

        # 4) 弹页「以后再说」（两步验证强推 / 备用邮箱提醒，见截图）
        if await _click_texts(g, LATER_TEXTS, "以后再说"):
            await g.wait_for_timeout(1500)
            continue

        # 5) 验证码挑战页（邮件码 / 短信码都有可能）：有取件地址就拉码回填
        code_input = g.locator(
            'input[name="pin"], input[type="tel"]:not([name="Passwd"]), '
            'input[aria-label*="代码"], input[aria-label*="验证码"], '
            'input[aria-label*="code" i]').first
        if await code_input.count() and await code_input.is_visible():
            wants_code = any(k in body for k in
                             ("验证码", "代码", "verification code", "Enter the code",
                              "コード", "mã", "Mã", "code"))
            if wants_code and mail_api and not otp_used:
                say("[g] 验证码挑战页 → 轮询取件地址 %s（最长 %ds）" % (mail_api, otp_wait))
                code = await asyncio_wrap(wait_otp, mail_api, email, otp_wait)
                if not code:
                    raise RuntimeError("Google 要求验证码，但取件地址 %s 在 %ds 内没给码"
                                       % (mail_api, otp_wait))
                otp_used = code
                await code_input.fill(code)
                say("[g] 验证码 → 填入 %s" % code)
                if not await _click_texts(g, ["下一步", "Next", "次へ", "Tiếp"], "提交验证码"):
                    await g.keyboard.press("Enter")
                await g.wait_for_timeout(1800)
                continue
            if not wants_code:
                # 纯 2FA（TOTP/密钥）页：先试「其他方式」
                if await _click_texts(g, OTHER_WAY_TEXTS, "试试其他方式"):
                    await g.wait_for_timeout(1500)
                    continue
                await g.screenshot(path=str(WORKDIR / "dbg_glogin_2fa.png"))
                raise RuntimeError("Google 要求两步验证（无 TOTP 密钥、无可用邮件挑战）；"
                                   "截图 dbg_glogin_2fa.png")

        # 6) 同意页
        if await _click_texts(g, CONSENT_LABELS, "同意页"):
            await g.wait_for_timeout(1500)
            continue

        say("[g] step%d 未识别页 url=%s | %s" % (step, g.url[:90], body[:150].replace("\n", " ")))
    if "accounts.google.com" in g.url:
        try:
            await g.screenshot(path=str(WORKDIR / "dbg_glogin_stuck.png"))
        except Exception:
            pass
        raise RuntimeError("Google 登录 %d 步内未完成（截图 dbg_glogin_stuck.png）" % max_steps)


async def asyncio_wrap(fn, *a):
    """同步轮询函数丢线程池，别堵事件循环。"""
    import asyncio
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)


# ------------------------------------------------------------------ 代理预检
def _probe(upstream: str, host: str, port: int = 443, timeout: int = 20) -> str:
    """经 upstream 探测能否连通 host:443。返回 '' 或失败原因。

    socks5:// → SOCKS5 握手（本地免认证转发器与远程带账密都支持）；
    http(s):// → HTTP CONNECT。TLS 证书校验失败（中间人）也算失败并注明。
    """
    from urllib.parse import urlparse
    try:
        u = urlparse(upstream)
        scheme = (u.scheme or "").lower()
        if scheme.startswith("socks"):
            s = socket.create_connection((u.hostname, u.port or 1080), timeout=timeout)
            s.settimeout(timeout)
            if u.username:
                s.sendall(b"\x05\x01\x02")
                r = s.recv(2)
                user, pwd = u.username.encode(), (u.password or "").encode()
                s.sendall(bytes([1, len(user)]) + user + bytes([len(pwd)]) + pwd)
                r = s.recv(2)
                if r[1] != 0:
                    return "SOCKS5 认证失败"
            else:
                s.sendall(b"\x05\x01\x00")
                s.recv(2)
            s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode()
                      + port.to_bytes(2, "big"))
            r = s.recv(4)
            if r[1] != 0:
                return "代理网关拒绝连接 %s（SOCKS5 reply=%d，出口可能降级/被墙）" % (host, r[1])
            if r[3] == 1:
                s.recv(6)
            elif r[3] == 3:
                s.recv(1 + s.recv(1)[0] + 2)
            elif r[3] == 4:
                s.recv(18)
        else:
            s = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
            s.settimeout(timeout)
            req = "CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n" % (host, port, host, port)
            if u.username:
                import base64
                tok = base64.b64encode(("%s:%s" % (u.username, u.password or "")).encode()).decode()
                req += "Proxy-Authorization: Basic %s\r\n" % tok
            req += "\r\n"
            s.sendall(req.encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                c = s.recv(1024)
                if not c:
                    break
                resp += c
            line = resp.split(b"\r\n", 1)[0].decode("utf-8", "replace")
            if " 200" not in line:
                return "代理网关拒绝连接 %s（%s）" % (host, line[:80] or "无响应")
        ctx = ssl.create_default_context()
        ss = ctx.wrap_socket(s, server_hostname=host)
        ss.close()
        return ""
    except ssl.SSLCertVerificationError as e:
        return "%s 返回的 TLS 证书校验失败（代理有中间人）： %s" % (host, str(e)[:100])
    except Exception as e:
        return "代理连不上 %s：%s" % (host, str(e)[:120])


def _precheck(upstream: str) -> None:
    """跑浏览器前先验出口；Google 或 dola 任一连不上就快速失败（原因回面板日志）。"""
    for host in ("accounts.google.com", "www.dola.com"):
        err = _probe(upstream, host)
        if err:
            raise RuntimeError("出口预检失败：%s（请换代理/稍后重试；"
                               "UDeal 网关需经前置代理，转发器会自动回退）" % err)
        say("[g] 预检 OK: %s 可达" % host)


# ------------------------------------------------------------------ 主流程
def run_sync(email: str, password: str, mail_api: str = "",
             proxy: str | None = None, headless: bool = False,
             timeout: int = 600) -> dict:
    """在独立线程 + 独立事件循环里跑 run()。

    服务进程（uvicorn）主 loop 里直接 launch patchright 浏览器会把整个
    server 进程带走（2026-10-12 实测：预检 OK → launch 后 loop 被
    "Task was destroyed" 拖死、8002 静默掉线）。ck_browser.py 早已用
    「daemon 线程 + asyncio.run」模式规避同类问题，这里照搬。
    超时后放弃等待（浏览器线程 daemon 继续跑，随进程退出）。
    """
    import asyncio
    import threading
    box: dict = {}

    def worker():
        try:
            box["v"] = asyncio.run(run(email, password, mail_api, proxy, headless))
        except BaseException as e:  # noqa: BLE001 - 异常要原样回面板
            box["e"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise RuntimeError("Google 渠道登录超时（%ds），已放弃等待" % timeout)
    if "e" in box:
        raise box["e"]
    if "v" not in box:
        raise RuntimeError("Google 渠道登录线程异常结束（无结果）")
    return box["v"]


async def run(email: str, password: str, mail_api: str = "",
        proxy: str | None = None, headless: bool = False) -> dict:
    """跑一次「Google 官网登录 → dola Google 登录」（协程；面板请用 run_sync）。

    proxy：本地免认证转发器 URL 直用；带认证 socks5/http 会起本地转发器。
    本链路强制有头（无头 Chromium 登 Google 必被识别）。
    """
    from patchright.async_api import async_playwright
    from add_account import google_login as oauth_state   # OAuth 回跳后的兜底状态机

    up = (proxy or "").strip()
    if not up:
        raise RuntimeError("必须提供代理（需能访问 google.com 与 dola.com）")

    res = {"email": email, "mail_api": mail_api, "navigations": [],
           "cookies": {}, "cookies_full": [], "auth_status": None}
    async with async_playwright() as p:
        import proxy_fwd
        # take_service 传入的已是本地免认证转发器（127.0.0.1）时直接用，不嵌套
        local_fwd = up.startswith("socks5://127.0.0.1") or up.startswith("socks://127.0.0.1")
        fwd = None
        if local_fwd:
            proxy_server = up
            say("[g] 复用本地转发器: %s" % up)
        elif up.startswith("socks"):
            fwd = await proxy_fwd.SocksForwarder.start(up, noise_domains=())
            proxy_server = "socks5://127.0.0.1:%d" % fwd.port
            say("[g] 本地转发器: %s -> %s" % (proxy_server, up))
        else:
            # http 型上游（UDeal 等）也经转发器：获得「拒大陆客户端 IP →
            # 前置代理自动回退」能力，直连必挂（2026-10-12 实测 10054）。
            fwd = await proxy_fwd.SocksForwarder.start(up, noise_domains=())
            proxy_server = "socks5://127.0.0.1:%d" % fwd.port
            say("[g] 本地转发器(http上游): %s -> %s" % (proxy_server, up))
        # ★ 预检必须在转发器建立之后：对浏览器真实出口（本地转发器）探测，
        #   转发器内部才有 UDeal「拒大陆客户端 → 前置代理自动回退」。
        #   放在转发器之前会拿原始网关直连探测，对 UDeal 必然误报 10054。
        await asyncio_wrap(_precheck, proxy_server)
        browser = await p.chromium.launch(
            headless=False,                                   # ★ 强制有头
            proxy={"server": proxy_server},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--no-default-browser-check", "--disable-background-networking"])
        ctx = await browser.new_context(viewport={"width": 1280, "height": 850},
                                        locale="ja-JP", timezone_id="Asia/Tokyo")

        # ---------- 1) Google 官网登录 ----------
        page = await ctx.new_page()
        try:
            await page.goto("https://www.google.com/", wait_until="domcontentloaded",
                            timeout=90000)
        except Exception as e:
            say("[g] goto google.com warn: %s" % str(e)[:100])
        await page.wait_for_timeout(2500)
        clicked = await _click_texts(page, SIGNIN_TEXTS, "Google 首页登录入口")
        if not clicked:
            # 找不到入口（页面变体）直接走登录 URL
            say("[g] 未找到首页登录按钮 → 直接打开 accounts.google.com")
            try:
                await page.goto("https://accounts.google.com/ServiceLogin?hl=zh-CN",
                                wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                say("[g] goto ServiceLogin warn: %s" % str(e)[:100])
        for _ in range(10):
            if "accounts.google.com" in page.url:
                break
            await page.wait_for_timeout(1500)
        if "accounts.google.com" not in page.url:
            try:
                await page.goto("https://accounts.google.com/ServiceLogin?hl=zh-CN",
                                wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
        await google_login_state(page, email, password, mail_api)
        # 确认真的有了 Google 会话（myaccount 能打开 = 已登录）
        g_ok = False
        try:
            await page.goto("https://myaccount.google.com/", wait_until="domcontentloaded",
                            timeout=45000)
            await page.wait_for_timeout(2500)
            body = await page.evaluate("() => (document.body && document.body.innerText || '')")
            g_ok = ("accounts.google.com" not in page.url or "登录" not in body[:200])
            say("[g] Google 会话自检：%s（url=%s）" % ("已登录" if g_ok else "未确认", page.url[:70]))
        except Exception as e:
            say("[g] 会话自检异常: %s" % str(e)[:100])

        # ---------- 2) dola：登录 → Google ----------
        try:
            await page.goto(BASE + "/chat/", wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            say("[g] goto dola warn: %s" % str(e)[:100])
        hit_login = ""
        for _ in range(20):                     # 最长 ~60s
            await page.wait_for_timeout(3000)
            try:
                body = await page.evaluate(
                    "() => (document.body && document.body.innerText || '')")
            except Exception:
                body = ""
            if "temporarily unavailable" in (body or "").lower():
                raise RuntimeError("dola 返回「temporarily unavailable」：该出口 IP 被 dola 拒绝"
                                   "（换代理出口/供应商后重试）")
            for _t in LOGIN_TEXTS:
                try:
                    _loc = page.get_by_text(_t, exact=False).first
                    if await _loc.count() and await _loc.is_visible():
                        hit_login = _t
                        break
                except Exception:
                    continue
            if hit_login:
                break
        if not hit_login:
            await page.screenshot(path=str(WORKDIR / "dbg_glogin_dola.png"))
            raise RuntimeError("dola 首页 60s 内未渲染出登录入口（截图 dbg_glogin_dola.png）")
        await _click_texts(page, LOGIN_TEXTS, "dola 登录")
        await page.wait_for_timeout(2500)
        if not await _click_texts(page, GOOGLE_TEXTS, "dola Google 登录"):
            await page.screenshot(path=str(WORKDIR / "dbg_glogin_nogoogle.png"))
            raise RuntimeError("dola 登录弹窗里没找到 Google 按钮（截图 dbg_glogin_nogoogle.png）")

        # 等 Google OAuth 页出现（可能在当前 tab 或 popup）
        g = None
        deadline = time.time() + 30
        while time.time() < deadline:
            if "accounts.google.com" in page.url:
                g = page
                break
            g = next((pg for pg in ctx.pages if "accounts.google.com" in pg.url), None)
            if g is not None:
                break
            await page.wait_for_timeout(1000)
        if g is not None:
            # 已有会话 → 一般自动过；兜底再跑一遍 OAuth 状态机（选账号/同意页）
            try:
                await oauth_state(g, email, password, "")
                say("[g] OAuth 兜底状态机跑完")
            except Exception as e:
                say("[g] OAuth 状态机提前结束（多数情况已自动过）: %s" % str(e)[:120])

        # ---------- 3) 轮询 dola 登录态 ----------
        t0 = time.time()
        while time.time() - t0 < 150:
            await page.wait_for_timeout(2500)
            try:
                cookies = await ctx.cookies()
            except Exception:
                continue
            full = [c for c in cookies if "dola.com" in c.get("domain", "")]
            res["cookies_full"] = full
            res["cookies"] = {c["name"]: c["value"] for c in full}
            if res["cookies"].get("passport_auth_status"):
                res["auth_status"] = res["cookies"]["passport_auth_status"]
                say("[g] ✓ 拿到 passport_auth_status（%.0fs）" % (time.time() - t0))
                break
            if res["cookies"].get("sessionid") and time.time() - t0 > 20:
                say("[g] ✓ 拿到 sessionid（%.0fs）" % (time.time() - t0))
                break
        try:
            await page.screenshot(path=str(WORKDIR / "dbg_glogin_final.png"), timeout=8000)
        except Exception:
            pass
        await browser.close()
        if fwd is not None:
            try:
                await fwd.stop()
            except Exception:
                pass
    return res
