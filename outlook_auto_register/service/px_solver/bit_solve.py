#!/usr/bin/env python3
"""BitBrowser（比特指纹浏览器）+ 真实按压 PerimeterX solver。

架构（混合）：
  比特 API 起一个带 kookeey 同 IP 代理的 profile → CDP 连接 →
  打开 signup.live.com 触发 PX → 真实按住 8-10s → 收割 _px3/_pxvid/_pxde →
  交回 Python 的 API 注册流程复用（同 IP，PX 令牌才被微软认）。

用前提：
  1) 比特客户端已启动，设置里「本地 API 服务」已开启（默认 http://127.0.0.1:54345）。
  2) 环境变量可覆盖：BIT_API、BIT_PROFILE_ID、PX_PROXY（kookeey，host:port:user:pass 或 {sid} 模板）。
"""
from __future__ import annotations

import os
import time
import random
import json
from typing import Optional

import requests

BIT_API = os.environ.get("BIT_API", "http://127.0.0.1:54345").rstrip("/")
DEFAULT_PROXY = os.environ.get("PX_PROXY") or os.environ.get("HTTP_PROXY") or ""
SIGNUP_URL = "https://signup.live.com/signup"


# --------------------------- 比特 API ---------------------------
_local = requests.Session()
_local.trust_env = False  # 本地 API 绕过 HTTP_PROXY 环境代理


def bit(path: str, body: Optional[dict] = None, *, timeout: int = 30) -> dict:
    r = _local.post(f"{BIT_API}{path}", json=(body or {}), timeout=timeout, proxies={"http": None, "https": None})
    r.raise_for_status()
    return r.json()


def bit_health() -> bool:
    try:
        j = bit("/health")
        return bool(j.get("success", True))
    except Exception:
        return False


def parse_proxy(proxy: str) -> dict:
    """host:port:user:pass → 比特 profile 代理字段。"""
    parts = proxy.split(":")
    host, port, user, pwd = parts[0], parts[1], parts[2], ":".join(parts[3:])
    return {"host": host, "port": port, "user": user, "pwd": pwd}


def ensure_profile(proxy: str) -> str:
    """优先用 BIT_PROFILE_ID；否则创建一个带 kookeey 代理的 Windows Chrome profile。"""
    pid = os.environ.get("BIT_PROFILE_ID", "").strip()
    if pid:
        return pid
    p = parse_proxy(proxy)
    payload = {
        "name": "px-solver",
        "remark": "auto px solver",
        "proxyMethod": 2,          # 自定义代理
        "proxyType": "http",
        "host": p["host"],
        "port": p["port"],
        "proxyUserName": p["user"],
        "proxyPassword": p["pwd"],
        "browserFingerPrint": {
            "coreVersion": "124",
            "ostype": "PC",
            "os": "Win32",
            "version": "124",
            "userAgent": "",
            "isLanguageBaseIp": True,
            "isDisplayLanguageBaseIp": True,
            "timeZone": "",
            "webRTC": "0",
        },
    }
    j = bit("/browser/update", payload)
    pid = (j.get("data") or {}).get("id") or ""
    if not pid:
        raise RuntimeError(f"创建 profile 失败: {json.dumps(j, ensure_ascii=False)[:300]}")
    print(f"[bit] 新建 profile id={pid}")
    return pid


def open_profile(pid: str) -> str:
    """打开 profile，返回 CDP ws 端点（首次开需下载内核，超时放宽到 180s）。"""
    j = bit("/browser/open", {"id": pid}, timeout=180)
    data = j.get("data") or {}
    ws = data.get("ws") or ""
    http = data.get("http") or ""
    print(f"[bit] open ok ws={ws} http={http}")
    if not ws:
        raise RuntimeError(f"open 未返回 ws: {json.dumps(j, ensure_ascii=False)[:300]}")
    return ws


def close_profile(pid: str) -> None:
    try:
        bit("/browser/close", {"id": pid})
    except Exception:
        pass


# --------------------------- 真实按压 ---------------------------
def human_press_hold(page, box, hold_ms: int = 9000) -> None:
    """真实按住：移动到中心 → 按下 → 保持 8-10s（含微抖动）→ 抬起。"""
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    # 贝塞尔式接近（简化：分段移动）
    for i in range(1, 8):
        page.mouse.move(cx - 40 + i * 5 + random.uniform(-2, 2), cy - 20 + i * 3 + random.uniform(-2, 2), steps=3)
        time.sleep(random.uniform(0.02, 0.06))
    page.mouse.move(cx, cy, steps=5)
    time.sleep(random.uniform(0.1, 0.2))
    page.mouse.down()
    t0 = time.time()
    while (time.time() - t0) * 1000 < hold_ms:
        page.mouse.move(cx + random.uniform(-1.5, 1.5), cy + random.uniform(-1.5, 1.5), steps=1)
        time.sleep(random.uniform(0.08, 0.16))
    page.mouse.up()


def find_px_captcha(page):
    """在主页面或 hsprotect iframe 里找 #px-captcha，返回 (frame, boundingbox)。"""
    for fr in page.frames:
        try:
            el = fr.query_selector("#px-captcha")
            if el:
                box = el.bounding_box()
                if box and box["width"] > 5:
                    return fr, box
        except Exception:
            continue
    return None, None


# --------------------------- 主流程 ---------------------------
def solve(proxy: str = DEFAULT_PROXY, *, headless: bool = False, dwell: int = 8):
    from playwright.sync_api import sync_playwright

    if not bit_health():
        raise RuntimeError(f"比特 API 不可达（{BIT_API}）：请启动比特客户端并开启「本地 API 服务」")

    pid = ensure_profile(proxy)
    ws = open_profile(pid)
    result = {"px3": "", "pxvid": "", "pxde": "", "pressed": False, "final_url": ""}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 1) 出口 IP 校验（应为 kookeey 美国住宅）
            try:
                page.goto("https://api.myip.com/", wait_until="domcontentloaded", timeout=30000)
                print("[ip] 出口:", page.inner_text("body")[:120])
            except Exception as e:
                print("[ip] 校验失败:", e)

            # 2) 打开 signup 触发 PX
            print("[nav] 打开 signup.live.com …")
            page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
            time.sleep(dwell)

            # 3) 找按住挑战；有则真实按压
            for attempt in range(3):
                fr, box = find_px_captcha(page)
                if not box:
                    print(f"[px] 未见按住挑战（attempt={attempt}），可能 silent 直接过")
                    break
                print(f"[px] 命中按住挑战 box={box} → 真实按住")
                human_press_hold(page, box, hold_ms=random.randint(8500, 10500))
                result["pressed"] = True
                time.sleep(4)

            # 4) 收割 PX cookie
            cookies = ctx.cookies()
            for c in cookies:
                if c["name"] == "_px3":
                    result["px3"] = c["value"]
                elif c["name"] == "_pxvid":
                    result["pxvid"] = c["value"]
                elif c["name"] == "_pxde":
                    result["pxde"] = c["value"]
            result["final_url"] = page.url
            print("[harvest] _px3=%s... _pxvid=%s pressed=%s" % (
                result["px3"][:40], result["pxvid"][:20], result["pressed"]))
            browser.close()
    finally:
        close_profile(pid)
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=DEFAULT_PROXY)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    print(json.dumps(solve(args.proxy, headless=args.headless), ensure_ascii=False, indent=2))
