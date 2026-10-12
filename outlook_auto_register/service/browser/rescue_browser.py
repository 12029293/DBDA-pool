"""浏览器版解封（rescue）：Roxy 浏览器登录 abuse/locked 号 → 解锁。

流程（用户确认）：
  1. 邮箱(#usernameEntry) → Next
  2. 若出现「Verify your email」→ 点「Use your password」切到密码登录（跳过 OTC）
  3. 密码 → Next
  4. abuse 页（如出现）→ 继续 TierRestore
  5. outlook.live.com = 已解锁
"""
from __future__ import annotations

import logging
from typing import Optional

from outlook_auto_register.service.browser.roxy_browser import RoxyBrowserClient, roxy_cdp_session

logger = logging.getLogger(__name__)

_SIGNIN_URL = "https://login.live.com/login.srf?wa=wsignin1.0&wreply=https://outlook.live.com/mail/"


def _click(page, *sels) -> bool:
    for sel in sels:
        try:
            b = page.locator(sel).first
            if b.count() and b.is_visible():
                b.click(timeout=4000)
                return True
        except Exception:
            pass
    return False


def _click_text(page, *texts) -> bool:
    for t in texts:
        try:
            el = page.get_by_text(t, exact=True).first
            if el.count() and el.is_visible():
                el.click(timeout=4000)
                return True
        except Exception:
            pass
    return False


def _body_text(page) -> str:
    try:
        return page.evaluate("() => document.body.innerText").lower()
    except Exception:
        return ""


def rescue_one(
    email: str,
    password: str,
    recovery_email: str = "",
    *,
    profile_id: Optional[str] = None,
    close_on_exit: bool = True,
) -> dict:
    """解封一个 locked/abuse 号。返回 {ok, state, detail}。"""
    roxy = RoxyBrowserClient()
    if not roxy.health():
        return {"ok": False, "state": "error", "detail": f"Roxy API 不可用: {roxy.last_error}"}
    if not profile_id:
        profiles = roxy.list_profiles()
        if not profiles:
            return {"ok": False, "state": "error", "detail": "无 Roxy profile"}
        profile_id = profiles[0].id

    try:
        with roxy_cdp_session(profile_id, client=roxy, close_on_exit=close_on_exit) as (_, context):
            page = context.new_page()
            page.goto(_SIGNIN_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(2500)

            # 1) 邮箱
            try:
                inp = page.locator('#usernameEntry, input[name="loginfmt"], #i0116').first
                inp.wait_for(state="visible", timeout=25000)
                inp.click()
                page.wait_for_timeout(300)
                inp.fill(email)
                page.wait_for_timeout(1000)
                _click(page, 'button[type="submit"]', "#idSIButton9", "#iNext")
            except Exception:
                # 可能已有 session 直接跳转，忽略
                pass

            # 2) 循环处理各状态
            for _ in range(15):
                page.wait_for_timeout(1500)
                url = page.url.lower()
                body = _body_text(page)

                if url.startswith("https://outlook.live.com"):
                    return {"ok": True, "state": "unlocked", "detail": url}

                # Verify your email → 点 Use your password 切密码登录
                if "verify your email" in body or "send code" in body or "we'll send a code" in body:
                    logger.info("Verify your email 页 → Use your password")
                    if _click_text(page, "Use your password", "使用你的密码"):
                        page.wait_for_timeout(1500)
                    continue

                # 密码页
                if "password" in body and page.locator('input[type="password"]').first.count() > 0:
                    logger.info("密码页 → 填密码")
                    p = page.locator('input[type="password"]').first
                    if p.is_visible():
                        p.click()
                        page.wait_for_timeout(300)
                        p.fill(password)
                        page.wait_for_timeout(800)
                        _click(page, 'button[type="submit"]', "#idSIButton9", "#iNext")
                    continue

                # abuse 页（TierRestore）—— 暂记录，看有什么按钮
                if "abuse" in url or "锁定" in body or "unusual" in body:
                    logger.info("abuse 页 → 尝试继续")
                    _click(page, 'button[type="submit"]', "#idSIButton9", "#iNext", 'a:has-text("Next")')
                    continue

            url = page.url.lower()
            if url.startswith("https://outlook.live.com"):
                return {"ok": True, "state": "unlocked", "detail": url}
            return {"ok": False, "state": "failed", "detail": f"未完成: {url[:120]}"}
    except Exception as exc:
        logger.exception("rescue_one 异常")
        return {"ok": False, "state": "error", "detail": str(exc)[:120]}
