"""reg-factory 同款 PerimeterX 按住逻辑（register_outlook_standalone.py）。"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
from typing import Any, Optional

logger = logging.getLogger(__name__)


def max_press_attempts(default: int = 8) -> int:
    env = os.environ.get("OUTLOOK_REG_MAX_PRESS", "").strip()
    if env.isdigit():
        return int(env)
    return default


async def captcha_visible(page) -> bool:
    try:
        for sel in [
            'button:has-text("Press and hold")',
            'button:has-text("Appuyer et maintenir")',
            'button:has-text("按住")',
            'button:has-text("长按")',
            'button:has-text("Halten")',
            "#px-captcha",
        ]:
            el = page.locator(sel).first
            if await el.count() > 0:
                box = await el.bounding_box()
                if box and box["width"] > 30:
                    return True
        ifr = page.locator('iframe[src*="hsprotect.net"], iframe[src*="arkose"], iframe[src*="funcaptcha"]')
        for i in range(await ifr.count()):
            box = await ifr.nth(i).bounding_box()
            if box and box["width"] > 50 and box["height"] > 30:
                return True
    except Exception:
        pass
    return False


async def find_press_target(page) -> tuple[Optional[dict[str, float]], bool]:
    """返回 (bounding_box, box_is_button)。"""
    target_box: Optional[dict[str, float]] = None
    box_is_button = False

    frames = list(page.frames)
    if "hsprotect.net" in (page.url or ""):
        frames.insert(0, page.main_frame)

    for frame in frames:
        if frame != page.main_frame and "hsprotect.net" not in (frame.url or ""):
            continue
        try:
            px = frame.locator("#px-captcha").first
            if await px.count() > 0:
                box = await px.bounding_box()
                if box and box["width"] > 30 and box["height"] > 8:
                    return box, True
        except Exception:
            pass

    try:
        hs = page.locator('iframe[src*="hsprotect.net"]')
        for i in range(await hs.count()):
            box = await hs.nth(i).bounding_box()
            if box and box["width"] > 50 and box["height"] > 30:
                return box, False
    except Exception:
        pass

    return target_box, box_is_button


async def perform_press_hold(page, target_box: dict[str, float], *, box_is_button: bool) -> bool:
    """贝塞尔移动 + 按住直到 captcha 消失或超时。返回是否检测到通过。"""
    bx, by, bw, bh = target_box["x"], target_box["y"], target_box["width"], target_box["height"]
    if box_is_button:
        cx = bx + bw * random.uniform(0.40, 0.60)
        cy = by + bh * random.uniform(0.40, 0.60)
    else:
        cx = bx + bw * random.uniform(0.42, 0.58)
        cy = by + bh * random.uniform(0.48, 0.62)

    sx, sy = random.uniform(200, 800), random.uniform(200, 400)
    await page.mouse.move(sx, sy)
    await asyncio.sleep(random.uniform(0.3, 0.8))

    steps = random.randint(15, 30)
    ctrl_x = (sx + cx) / 2 + random.uniform(-100, 100)
    ctrl_y = (sy + cy) / 2 + random.uniform(-80, 80)
    for step in range(1, steps + 1):
        t = step / steps
        mx = (1 - t) ** 2 * sx + 2 * (1 - t) * t * ctrl_x + t ** 2 * cx + random.uniform(-1.5, 1.5)
        my = (1 - t) ** 2 * sy + 2 * (1 - t) * t * ctrl_y + t ** 2 * cy + random.uniform(-1.5, 1.5)
        await page.mouse.move(mx, my)
        await asyncio.sleep(random.uniform(0.005, 0.025))

    await asyncio.sleep(random.uniform(0.1, 0.3))
    await page.mouse.down()

    max_hold = random.uniform(11.0, 15.0)
    hold_start = asyncio.get_event_loop().time()
    drift_phase = random.uniform(0, 2 * math.pi)
    drift_freq = random.uniform(1.5, 2.8)
    last_chk = 0.0
    passed_in_hold = False

    while True:
        elapsed = asyncio.get_event_loop().time() - hold_start
        if elapsed >= max_hold:
            break
        ph = drift_phase + elapsed * drift_freq
        await page.mouse.move(cx + 1.0 * math.sin(ph), cy + 0.6 * math.cos(ph * 1.3))
        if elapsed > 1.5 and elapsed - last_chk > 0.5:
            last_chk = elapsed
            try:
                if not await captcha_visible(page):
                    passed_in_hold = True
                    break
            except Exception:
                pass
        await asyncio.sleep(random.uniform(0.03, 0.08))

    await asyncio.sleep(random.uniform(0.05, 0.18))
    await page.mouse.up()
    held = asyncio.get_event_loop().time() - hold_start
    logger.info("按住 %.1fs passed=%s at (%.0f,%.0f)", held, passed_in_hold, cx, cy)
    await asyncio.sleep(random.uniform(2, 4))
    return passed_in_hold


async def inject_arkose_token(page, token: str) -> bool:
    try:
        injected = await page.evaluate(
            f"""
            () => {{
                const frames = document.querySelectorAll('iframe[id*="enforcement"], iframe[data-e2e="enforcement-frame"]');
                if (frames.length > 0 && window.CE_READY) {{
                    window.CE_READY("{token}");
                    return "ce_ready";
                }}
                const hidden = document.querySelector('input[name="fc-token"], input[name="FunCaptcha"]');
                if (hidden) {{ hidden.value = "{token}"; return "hidden_field"; }}
                if (typeof window.fcCallback === 'function') {{ window.fcCallback("{token}"); return "fc_callback"; }}
                if (typeof window.ArkoseEnforcement !== 'undefined') {{
                    try {{ window.ArkoseEnforcement.setConfig({{data: {{token: "{token}"}}}}) }} catch(e) {{}}
                    return "arkose_enforcement";
                }}
                return "no_method";
            }}
            """
        )
        logger.info("Arkose inject: %s", injected)
        return injected != "no_method"
    except Exception as exc:
        logger.warning("Arkose inject 失败: %s", exc)
        return False


async def _confirm_gone(page, *, checks: int = 3, gap: float = 1.2) -> bool:
    """连续多次确认 captcha 不再出现，避免 iframe 刷新空隙的误判。"""
    for _ in range(checks):
        if await captcha_visible(page):
            return False
        await asyncio.sleep(gap)
    return not await captcha_visible(page)


async def run_press_rounds(page, *, max_press: int) -> bool:
    """在已打开的 signup 页执行多轮按住。返回是否确认 captcha 消失。"""
    had_captcha = False
    no_btn_rounds = 0
    for press_num in range(1, max_press + 1):
        if not await captcha_visible(page):
            if had_captcha and await _confirm_gone(page):
                logger.info("captcha 已消失并确认（press #%s 后）", press_num - 1)
                return True
            no_btn_rounds += 1
            logger.debug("未检测到 captcha 按钮 press_round=%s", press_num)
            await asyncio.sleep(1.5)
            continue

        no_btn_rounds = 0
        had_captcha = True
        target_box, box_is_button = await find_press_target(page)
        if not target_box or target_box["width"] <= 30 or target_box["height"] < 8:
            await asyncio.sleep(1.5)
            continue

        logger.info("reg-factory press #%s btn=%s", press_num, box_is_button)
        passed = await perform_press_hold(page, target_box, box_is_button=box_is_button)
        # 只有连续确认 captcha 消失才算通过，防止按一次就误判
        if passed and await _confirm_gone(page):
            logger.info("按住通过并确认（press #%s）", press_num)
            return True

    return await _confirm_gone(page) if had_captcha else False
