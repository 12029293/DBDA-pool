# -*- coding: utf-8 -*-
"""浏览器补真：用账号自己的登录态起一次真实页面，收割「只有浏览器 SDK 才会写」的 cookie。

为什么必须走浏览器（2026-10-11 实测结论）：
  - `flow_cur_user_sec_id` / `s_v_web_id` 由 passport SDK 在页面里产出，其请求
    (`GET /passport/token/beat/web/`) 带 `sign` / `msToken` / `a_bogus` 签名，
    协议侧复刻不了（打过去恒 2035 非法请求）。
  - 实测：注入「纯协议注册态」CK（16 项）后访问 `/chat/` + `/`，浏览器会自己写入
    真实的 `flow_cur_user_sec_id`（104 字符，base64 解 76 字节），
    且**同账号恒定、异账号不同**（两次独立运行取值完全相同）→ 是稳定真凭据。
  - 拿不到的仍有两个：`passport_auth_status`（只有网页登录 /auth/callback 那一刻
    服务端才下发，浏览器跑页面也不给）、`flow_multi_user_sec_info`（单会话不写）。

对外入口只有一个 `harvest()`：**永不抛异常**，超时/失败/浏览器不可用一律返回 {}，
调用方按「补到就更好、补不到不影响」处理。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

# 只收割这些字段（其余一律不碰，避免把浏览器自己的怪 cookie 带进账号）
HARVEST_KEYS = (
    "flow_cur_user_sec_id",
    "flow_multi_user_sec_info",
    "s_v_web_id",
    "flow_user_country",
    "dbx-web-theme",
    "conversation_list_v2_group_mode",
    "i18next",
)

# 访问哪些页面能触发 SDK（/chat/ 是主战场，/ 补一轮）
VISIT_URLS = ("https://www.dola.com/chat/", "https://www.dola.com/")

DEFAULT_UA_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
]

_LOGGER: Optional[Callable[[str, str], None]] = None


def _log(lv: str, msg: str) -> None:
    if _LOGGER is not None:
        try:
            _LOGGER(lv, msg)
        except Exception:
            pass


def set_logger(fn: Optional[Callable[[str, str], None]]) -> None:
    """注册日志回调（形如 register_service 的 log(level, msg)）。"""
    global _LOGGER
    _LOGGER = fn


def touch_enabled() -> bool:
    """浏览器补真总开关：CK_BROWSER_TOUCH=0 关闭（默认开启）。"""
    return os.environ.get("CK_BROWSER_TOUCH", "1") != "0"


def looks_real(cookies: Dict[str, str]) -> bool:
    """判断 CK 里是否已经是「浏览器产出的真 flow_cur_user_sec_id」。

    真值结构：base64 → 76 字节，前 12 字节固定头（4 个独立真样本一致：
    2 个账号 3 次运行 + 上传样本）。本地 synth 占位解不出这个头 → 可作判据，
    导出时凭它跳过重复的浏览器访问。
    """
    import base64
    v = (cookies or {}).get("flow_cur_user_sec_id") or ""
    if not v:
        return False
    try:
        raw = base64.b64decode(v + "=" * (-len(v) % 4))
    except Exception:
        return False
    return raw[:12].hex() == "2b3f5b00611d24206c2c2032"


def _to_playwright(cookies: Dict[str, str]) -> list[dict]:
    """注入浏览器时只给「真·登录态核心」，**剔除待收割字段**。

    关键：若把 flow_multi_user_sec_info 之类的占位值也注入，页面对它不感兴趣、
    不会覆盖，harvest 读回来就会把注入值误判成「浏览器产出的真值」。
    剔除后，凡是从浏览器读回来的，就一定是页面自己写的。
    """
    return [{"name": k, "value": v, "domain": ".dola.com", "path": "/",
             "secure": True, "sameSite": "Lax"}
            for k, v in cookies.items()
            if k and v is not None and k not in HARVEST_KEYS]


async def _harvest_async(cookies: Dict[str, str], proxy: str, timeout: float,
                         headless: bool, wait_round: float) -> Dict[str, str]:
    from patchright.async_api import async_playwright

    fwd = None
    try:
        import proxy_fwd
        # 不让转发器把 SDK 的埋点域名当噪声拦掉（sec_id 就靠这些请求）
        try:
            proxy_fwd._NOISE_DOMAINS = ()
        except Exception:
            pass
        fwd = await proxy_fwd.SocksForwarder.start(proxy)
        launch_proxy: Any = {"server": "socks5://127.0.0.1:%d" % fwd.port}
    except Exception as e:  # 转发器起不来就直连（本机有 Clash 时也常可用）
        _log("warn", "CK浏览器补真: 转发器未起(%s)，改为直连代理" % str(e)[:60])
        launch_proxy = {"server": proxy} if proxy else None

    out: Dict[str, str] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, proxy=launch_proxy,
                                          args=list(DEFAULT_UA_ARGS))
        try:
            ctx = await browser.new_context(viewport={"width": 1280, "height": 850},
                                            locale="en-US", timezone_id="Asia/Tokyo")
            await ctx.add_cookies(_to_playwright(cookies))
            page = await ctx.new_page()
            deadline = time.time() + max(20.0, timeout)
            for url in VISIT_URLS:
                if time.time() > deadline:
                    break
                try:
                    await page.goto(url, wait_until="domcontentloaded",
                                    timeout=max(15.0, (deadline - time.time())) * 1000)
                except Exception as e:
                    _log("warn", "CK浏览器补真: goto %s 告警 %s" % (url, str(e)[:70]))
                for _ in range(2):
                    if time.time() > deadline:
                        break
                    try:
                        await page.wait_for_timeout(wait_round * 1000)
                    except Exception:
                        break
                    m = {c["name"]: c["value"] for c in await ctx.cookies()}
                    got = {k: m[k] for k in HARVEST_KEYS if m.get(k)}
                    # 核心两项都到手才算够：flow_cur_user_sec_id 首屏就有，
                    # s_v_web_id 往往要再一轮/再一个 URL 才写（早退会漏）
                    if got.get("flow_cur_user_sec_id") and got.get("s_v_web_id"):
                        out.update(got)
                        return out
            m = {c["name"]: c["value"] for c in await ctx.cookies()}
            out.update({k: m[k] for k in HARVEST_KEYS if m.get(k)})
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return out


def _run_in_thread(coro_factory, timeout: float) -> Dict[str, str]:
    box: Dict[str, Any] = {}

    def worker():
        try:
            box["v"] = asyncio.run(coro_factory())
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("浏览器补真超时（%.0fs）" % timeout)
    if "e" in box:
        raise box["e"]
    return box.get("v") or {}


def harvest(cookies: Dict[str, str], proxy: str = "", *, timeout: float = 90.0,
            headless: Optional[bool] = None, wait_round: float = 4.0) -> Dict[str, str]:
    """用 `cookies` 的登录态跑一次真实页面，返回可补进账号的新增字段。

    只增字段，不覆盖调用方已有值（覆盖由调用方决定）。
    **永不抛异常**：任何失败返回 {}。
    """
    if not cookies or not cookies.get("sessionid"):
        _log("info", "CK浏览器补真: 无 sessionid，跳过")
        return {}
    if headless is None:
        headless = os.environ.get("CK_BROWSER_HEADLESS", "1") != "0"
    hard = timeout + 60.0
    t0 = time.time()
    try:
        got = _run_in_thread(
            lambda: _harvest_async(cookies, proxy, timeout, headless, wait_round), hard)
    except Exception as e:  # noqa: BLE001
        _log("warn", "CK浏览器补真失败（已忽略）: %s: %s" % (type(e).__name__, str(e)[:90]))
        return {}
    cost = time.time() - t0
    # 兜底：与输入同值的字段不算「浏览器产出」（防止将来注入策略改动后误报）
    got = {k: v for k, v in got.items() if v and cookies.get(k) != v}
    if got:
        _log("info", "CK浏览器补真: 补到 %d 项 %s（%.1fs）"
             % (len(got), ", ".join(sorted(got)), cost))
    else:
        _log("info", "CK浏览器补真: 未补到字段（%.1fs）" % cost)
    return got
