# -*- coding: utf-8 -*-
"""真·网页登录（Google OAuth -> dola），目标是拿到 passport_auth_status。

技术背景（实测 + SDK 反混淆，见 .workbuddy/memory/2026-07-11.md）：
  * passport_auth_status 只在「网页授权登录」链路由服务端 Set-Cookie：
      authorize -> login_only -> 年龄门(confirmAgeGate) -> auth/login
    纯协议注册（register_verify_login）不走这条路，所以永远拿不到。
  * 服务端会**外呼 IdP 校验**：拿过期 Google token 打 auth/login ->
    error_code 1902 / name=profile_error / dialog_tips: google:{"Invalid Value"}。
  * 样本 Cookies_01 的 wap_state 解出 PL=2085 == Google。

与 _google_dola_test.py 的两处关键差异（也是旧脚本拿不到 auth_status 的原因）：
  1) **走真实入口**：打开 /chat/ -> 点「登录」-> 点「Google で続ける」，
     由 dola 前端自己发起 OAuth（而不是手拼 authorize URL —— 实测手拼的
     回跳 fragment 只有 state、没有 access_token，前端直接 "Couldn't load"）。
  2) **不冻结 /auth/callback**（旧脚本 window.stop() 把前端 SDK 掐死了），
     让 flow-account SDK 完整跑完 authLogin（含年龄门），并轮询等 auth_status。

用法：
  GPROXY="http://127.0.0.1:7890" python ck_weblogin.py "email----password----totp"
  GPROXY="http://127.0.0.1:7890" python ck_weblogin.py 1   # 读 谷歌账号.txt 第 1 行

环境变量：GPROXY(CLI 必填；程序调用可传 run(..., proxy=)) WL_ACCOUNT_FILE WL_HEADLESS WL_WAIT WL_OUT
程序接口：await run(email, password, secret, proxy="socks5://...", headless=True) -> dict
          （面板「取号」页走这个接口；返回 res["cookies_full"] = 含 domain 的完整 cookie 列表）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WORKDIR = Path(__file__).parent.absolute()
BASE = "https://www.dola.com"
PROXY = os.environ.get("GPROXY", "")
ACC_FILE = os.environ.get("WL_ACCOUNT_FILE",
                          r"C:\Users\Administrator\Desktop\谷歌账号.txt")
HEADLESS = os.environ.get("WL_HEADLESS", "0") == "1"
WAIT = int(os.environ.get("WL_WAIT", "100"))
TARGET = "passport_auth_status"

LOGIN_TEXTS = ["ログイン", "Log In", "Sign in", "登录", "ログイン/新規登録"]
GOOGLE_TEXTS = ["Googleで続ける", "Continue with Google", "Googleでログイン",
                "Sign in with Google", "Google"]


def say(*a):
    print(*a, flush=True)


def load_account(arg: str):
    if arg.isdigit():
        lines = [l.strip() for l in open(ACC_FILE, encoding="utf-8")
                 if l.strip() and "----" in l]
        i = int(arg)
        if i < 1 or i > len(lines):
            raise SystemExit("行号 %d 超范围（共 %d 个账号）" % (i, len(lines)))
        parts = lines[i - 1].split("----")
    else:
        parts = arg.split("----")
    email = parts[0].strip()
    password = parts[1].strip() if len(parts) > 1 else ""
    secret = parts[2].strip() if len(parts) > 2 else ""
    return email, password, secret


async def click_first(page, texts, label):
    """按文本候选逐个尝试点击（可见才点）。返回命中的文本。"""
    for t in texts:
        try:
            loc = page.get_by_text(t, exact=False).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=6000)
                say("[ui] 点击 %s: %r" % (label, t))
                return t
        except Exception:
            continue
    return ""


async def click_age_gate(page):
    try:
        return await page.evaluate("""() => {
            const want = ['OK','オーケー','確認','确认','Confirm','同意','Continue','続行'];
            const els = [...document.querySelectorAll('button,[role=button],div,span')];
            for (const w of want) {
                const t = els.find(e => (e.textContent||'').trim() === w && e.childElementCount === 0);
                if (t) { t.click(); return w; }
            }
            return '';
        }""")
    except Exception:
        return ""


async def run(email: str, password: str, secret: str,
              proxy: str | None = None, headless: bool | None = None) -> dict:
    """跑一次真网页登录。proxy 留空则用环境变量 GPROXY（CLI 用法）。

    面板调用时传 proxy=面板代理构造出的 socks5 URL（住宅出口）；
    headless 留空走 WL_HEADLESS。
    """
    from patchright.async_api import async_playwright
    from add_account import google_login
    import proxy_fwd

    up = (proxy or PROXY or "").strip()
    if not up:
        raise RuntimeError("必须提供代理（能访问 accounts.google.com 与 dola.com）")
    hl = HEADLESS if headless is None else bool(headless)

    res = {"email": email, "navigations": [], "google_redirects": [],
           "callback_hash": None, "cookies": {}, "cookies_full": [],
           "auth_status": None}
    async with async_playwright() as p:
        # 目标是 dola + Google：本地转发器**不能**把 google.com 当噪音拒掉，
        # 所以显式传 noise_domains=()（不再改全局 _NOISE_DOMAINS，避免污染其它调用方）。
        fwd = None
        if up.startswith("socks"):
            fwd = await proxy_fwd.SocksForwarder.start(up, noise_domains=())
            proxy_server = "socks5://127.0.0.1:%d" % fwd.port
            say("本地转发器: %s -> %s" % (proxy_server, up))
        else:
            proxy_server = up
            say("直接使用代理: %s" % up)
        browser = await p.chromium.launch(
            headless=hl,
            proxy={"server": proxy_server},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--no-default-browser-check", "--disable-background-networking"])
        # locale ja-JP：与 add_account_flow 一致（选择器已验证）
        ctx = await browser.new_context(viewport={"width": 1280, "height": 850},
                                        locale="ja-JP", timezone_id="Asia/Tokyo")

        def on_nav(f):
            u = f.url
            if "dola.com" in u:
                res["navigations"].append(u)
                if "/auth/callback" in u and "#" in u:
                    res["callback_hash"] = u.split("#", 1)[1]
                    say("[nav] /auth/callback fragment = %s" % res["callback_hash"][:300])
        page = await ctx.new_page()
        page.on("framenavigated", on_nav)

        async def on_resp(r):
            u = r.url
            if "accounts.google.com" in u and r.status in (301, 302, 303, 307, 308):
                loc = r.headers.get("location", "")
                res["google_redirects"].append({"status": r.status,
                                                "from": u[:120], "to": loc[:400]})
                say("[google-redirect] %s -> %s" % (r.status, loc[:260]))
            if "/passport/" in u and r.request.method == "POST":
                try:
                    body = await r.text()
                except Exception:
                    body = "<fail>"
                res.setdefault("passport_post", []).append(
                    {"url": u[:200], "status": r.status, "body": body[:500]})
                say("[passport-POST] %s -> %s | %s" % (u[:110], r.status, body[:200]))
            if "/passport/" in u and r.request.method == "GET" and \
                    ("auth" in u or "login" in u):
                res.setdefault("passport_get", []).append({"url": u[:200], "status": r.status})
        page.on("response", on_resp)

        # ---------- 1) 打开 chat 页 -> 点登录 -> 点 Google ----------
        try:
            await page.goto(BASE + "/chat/", wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            say("goto warn: %s" % str(e)[:100])
        # 等页面真正渲染出登录入口；同时识别 dola 的「temporarily unavailable」
        # 错误页（出口 IP 被 dola 拒时会出现）——早停，别拿匿名 cookie 当成功。
        hit_login = ""
        for _ in range(20):                    # 最长 ~60s
            await page.wait_for_timeout(3000)
            try:
                body = await page.evaluate(
                    "() => (document.body && document.body.innerText || '')")
            except Exception:
                body = ""
            if "temporarily unavailable" in (body or "").lower():
                raise RuntimeError(
                    "dola 返回「temporarily unavailable」：该出口 IP 被 dola 拒绝，"
                    "页面未渲染（请换代理出口/供应商后重试）")
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
        res["cf_url"] = page.url[:300]
        say("[ui] chat 页 url=%s | 登录入口=%r" % (page.url[:120], hit_login))
        if not hit_login:
            raise RuntimeError("dola 首页 %ds 内未渲染出登录入口"
                               "（页面可能未加载完或被打码）" % 60)

        hit_login = await click_first(page, LOGIN_TEXTS, "登录")
        if not hit_login:
            say("[ui] 没找到登录按钮，尝试直接打开 /login")
            try:
                await page.goto(BASE + "/login", wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(4000)
            except Exception:
                pass
        await page.wait_for_timeout(2500)
        hit_g = await click_first(page, GOOGLE_TEXTS, "Google 登录")
        if not hit_g:
            await page.screenshot(path=str(WORKDIR / "_wl_no_google_btn.png"))
            say("[ui] ✗ 没找到 Google 按钮（截图 _wl_no_google_btn.png）")
        # 等跳去 Google
        for _ in range(12):
            await page.wait_for_timeout(2000)
            if "accounts.google.com" in page.url:
                break

        # ---------- 2) Google 登录状态机 ----------
        try:
            await google_login(page, email, password, secret)
        except Exception as e:
            say("[login] 状态机结束: %s: %s" % (type(e).__name__, str(e)[:110]))

        # ---------- 3) 让 /auth/callback 跑完，轮询等 auth_status ----------
        t0 = time.time()
        while time.time() - t0 < WAIT:
            await page.wait_for_timeout(2500)
            ag = await click_age_gate(page)
            if ag:
                say("[age] 点击年龄门: %s" % ag)
            try:
                cookies = await ctx.cookies()
            except Exception:
                continue
            m = {c["name"]: c["value"] for c in cookies
                 if "dola.com" in c.get("domain", "")}
            res["cookies"] = m
            res["cookies_full"] = [c for c in cookies
                                   if "dola.com" in c.get("domain", "")]
            if m.get(TARGET):
                say("[ok] 拿到 %s = %s（%.1fs）" % (TARGET, m[TARGET][:48], time.time() - t0))
                break
            if time.time() - t0 > 25 and int(time.time() - t0) % 5 < 3:
                say("[..] 等 %s（%.0fs）| 已建 sessionid=%s | url=%s"
                    % (TARGET, time.time() - t0, "sessionid" in m, page.url[:70]))
        res["auth_status"] = res["cookies"].get(TARGET)
        try:
            await page.screenshot(path=str(WORKDIR / "_wl_callback.png"), timeout=8000)
        except Exception:
            pass
        if not res["cookies"]:
            try:
                cs = await ctx.cookies()
                res["cookies"] = {c["name"]: c["value"] for c in cs
                                  if "dola.com" in c.get("domain", "")}
                res["cookies_full"] = [c for c in cs
                                       if "dola.com" in c.get("domain", "")]
            except Exception:
                pass
        await browser.close()
        if fwd is not None:
            try:
                await fwd.stop()
            except Exception:
                pass
    return res


def main():
    if not PROXY:
        raise SystemExit("必须设置 GPROXY（需能访问 accounts.google.com 与 dola.com）")
    arg = sys.argv[1] if len(sys.argv) > 1 else "1"
    email, password, secret = load_account(arg)
    say("Google 账号: %s | 2FA: %s | 代理: %s | 有头: %s"
        % (email, "有" if secret else "无", PROXY, not HEADLESS))
    res = asyncio.run(run(email, password, secret))

    # 默认写到 _wl_out/（号池外）—— 不能写进 accounts/，否则会被当账号扫到
    out = Path(os.environ.get("WL_OUT")
               or (WORKDIR / "_wl_out" / ("wl_" + time.strftime("%m%d-%H%M%S"))))
    out.mkdir(parents=True, exist_ok=True)
    (out / "weblogin_result.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    cks = res.get("cookies") or {}
    (out / "cookie_header.txt").write_text(
        "; ".join("%s=%s" % (k, v) for k, v in cks.items()), encoding="utf-8")
    say("\n==== 结果 ====")
    say("dola cookie %d 项 | %s = %s" % (len(cks), TARGET, res.get("auth_status") or "❌ 未拿到"))
    say("callback fragment: %s" % (res.get("callback_hash") or "(未捕获)")[:200])
    say("产物目录: %s" % out)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        say("✗ %r" % e)
        sys.exit(1)
    os._exit(0)
