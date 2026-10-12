#!/usr/bin/env python3
"""同会话架构：在 **同一个 SwiftShader headless 浏览器** 里完成 signup.live.com 全流程建号。

背景（prior work 决定性结论）：
  - “独立浏览器收割 press + 协议回放”架构已死：press `_px3` 与解算浏览器自身 PX vid/session
    加密绑定，注册（Python requests）会话无法加入解算浏览器的 vid，verify#2 恒被拒（vid mismatch）。
  - 唯一干净架构：整段注册都在同一个浏览器会话里跑——同一个 SwiftShader 浏览器既【解 press】
    又【提交注册表单】，账号的 PX vid == press vid 原生一致，无跨会话交接。

本模块做的事：
  1) 用 px_swiftshader_solver.py 验证过的 SwiftShader 软件渲染启动参数拉起 headless Chromium
     （带代理 host:port:user:pass、真实 Windows Chrome UA、en-US、美区时区）。
  2) 复用 bit_register.py 的表单状态机（邮箱→密码→生日下拉→姓名→_click_next→成功/proofs 识别），
     但用 SwiftShader 浏览器自己的 playwright page 驱动，而非 BitBrowser CDP connect。
  3) PX 按压步用 px_swiftshader_solver.py 里【自动 CDP 真按】（浮点坐标+微抖，按住 1000-3000ms），
     绝不 manual、绝不 PX_MANUAL。
  4) 一直跑到：账号建成（到达 proofs/Add 或 account.microsoft.com 或 outlook.live.com），
     记录 final_url + cookies；关键步截图。

不改动、不依赖 bit_register.py 与 risk.py（仅从 px_swiftshader_solver.py 复用已验证的 CDP/启动件）。

用法：
    python px_solver/ss_register.py "gate.kookeey.info:1000:user:pass-US-xxxx"
    python px_solver/ss_register.py -            # 直连（无代理，仅调试）
"""
from __future__ import annotations

import json
import logging
import os
import random
import string
import sys
import time
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 复用 px_swiftshader_solver.py 里【已实测生效】的 SwiftShader 启动件 + CDP 真按 + 工具函数。
from outlook_auto_register.service.px_solver.px_swiftshader_solver import (  # noqa: E402
    SWIFTSHADER_ARGS,
    UA,
    HOLD_MS_MIN,
    HOLD_MS_MAX,
    _parse_proxy,
    _import_sync_playwright,
    _cdp_move,
    _cdp_press_hold,
    _find_captcha,
    _visible_captcha_box,
    _page_state,
)

# ── 日志（文件 + 控制台）────────────────────────────────────────────────
_RUN_TS = time.strftime("%Y%m%d_%H%M%S")
_LOG_MAIN = os.path.join(_HERE, "ss_register.log")
_LOGS_DIR = os.path.join(os.path.dirname(_HERE), "logs")
try:
    os.makedirs(_LOGS_DIR, exist_ok=True)
    _LOG_RUN = os.path.join(_LOGS_DIR, f"ss_register_{_RUN_TS}.log")
except Exception:  # noqa: BLE001
    _LOG_RUN = os.path.join(_HERE, f"ss_register_{_RUN_TS}.log")

logger = logging.getLogger("ss_register")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for _path in (_LOG_MAIN, _LOG_RUN):
        try:
            _fh = logging.FileHandler(_path, encoding="utf-8")
            _fh.setFormatter(_fmt)
            logger.addHandler(_fh)
        except Exception:  # noqa: BLE001
            pass
    _sh = logging.StreamHandler()
    _sh.setFormatter(logging.Formatter("[ss_register] %(message)s"))
    logger.addHandler(_sh)


SIGNUP_URL = os.environ.get("PX_SIGNUP_URL", "https://signup.live.com/signup?lic=1")
HEADFUL = os.environ.get("SS_HEADFUL", "0").strip() == "1"
# PX_OS_PRESS=1：用 macOS CGEvent HID 真按（绕过 CDP）。隐含 headed。
OS_PRESS = os.environ.get("PX_OS_PRESS", "0").strip() == "1"
if OS_PRESS:
    HEADFUL = True
# SS_REAL_GPU=1：本机有真实 GPU（如 Apple Silicon Mac 的 Metal），关闭 SwiftShader 软件渲染，
# 让 WebGL renderer 变成真实 Apple/ANGLE Metal 串（消除最强 bot 信号）。同时去掉 --headless=new，
# 以便 headful 真窗口跑（配合 SS_HEADFUL=1）。stealth（AutomationControlled）与稳定性 flag 保留。
REAL_GPU = os.environ.get("SS_REAL_GPU", "0").strip() == "1"
# 真 GPU headful 时需从 SwiftShader 启动件里剔除的 flag（软件渲染 + 强制 headless + GPU 沙箱）
_REAL_GPU_SKIP = {"--headless=new", "--use-gl=angle", "--use-angle=swiftshader",
                  "--enable-unsafe-swiftshader", "--disable-gpu-sandbox"}
MAX_STEPS = int(os.environ.get("SS_MAX_STEPS", "26"))


def _build_launch_args() -> list:
    """按 SS_REAL_GPU 选择启动参数：
      - REAL_GPU=1：剔除 SwiftShader 软件渲染 + --headless=new，交给真实 GPU（Apple/Metal）跑，
        WebGL renderer 变成真实串；仅保留 stealth（--disable-blink-features=AutomationControlled）
        与无害稳定性 flag。
      - 否则：沿用 px_swiftshader_solver 里实测生效的 SwiftShader 全套参数。
    """
    if REAL_GPU:
        return [a for a in SWIFTSHADER_ARGS if a not in _REAL_GPU_SKIP]
    args = list(SWIFTSHADER_ARGS)
    if HEADFUL or OS_PRESS:
        args = [a for a in args if a != "--headless=new" and not a.startswith("--headless")]
    return args

FIRST_NAMES = ["James", "John", "Robert", "Michael", "David", "William",
               "Mary", "Jennifer", "Linda", "Emma", "Olivia", "Sophia"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
              "Miller", "Davis", "Wilson", "Moore", "Taylor", "Anderson"]

# 反 abuse：浏览器 locale/timezone 跟随 --country（与住宅代理地区一致，降低批量指纹）。
_COUNTRY_BROWSER = {
    "US": ("en-US", "America/New_York"),
    "CA": ("en-CA", "America/Toronto"),
    "GB": ("en-GB", "Europe/London"),
    "AU": ("en-AU", "Australia/Sydney"),
    "SG": ("en-SG", "Asia/Singapore"),
    "DE": ("de-DE", "Europe/Berlin"),
    "FR": ("fr-FR", "Europe/Paris"),
    "JP": ("ja-JP", "Asia/Tokyo"),
}


def _locale_tz(country: str) -> tuple[str, str]:
    return _COUNTRY_BROWSER.get((country or "US").strip().upper(), ("en-US", "America/New_York"))


def _rand_user() -> str:
    return ("".join(random.choice(string.ascii_lowercase) for _ in range(3))
            + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(7)))


def _rand_pwd() -> str:
    return "Ab" + "".join(random.choice(string.ascii_letters + string.digits) for _ in range(11)) + "!7"


def _screenshot(page, name: str) -> None:
    try:
        path = os.path.join(_HERE, f"ss_{_RUN_TS}_{name}.png")
        page.screenshot(path=path, full_page=False)
        logger.info("截图 -> %s", path)
    except Exception as e:  # noqa: BLE001
        logger.info("截图 %s 失败: %s", name, e)


def _cdp_warmup(cdp, log) -> None:
    """无 hik 依赖的真人化热身：CDP 在视口里连续浮点移动鼠标，制造真实 mousemove 序列。"""
    try:
        x, y = 420.0, 320.0
        for _ in range(random.randint(6, 10)):
            nx = max(30.0, min(1240.0, x + random.uniform(-160, 160)))
            ny = max(30.0, min(860.0, y + random.uniform(-120, 120)))
            steps = random.randint(6, 12)
            for i in range(steps):
                _cdp_move(cdp, x + (nx - x) * (i + 1) / steps + random.uniform(-1.5, 1.5),
                          y + (ny - y) * (i + 1) / steps + random.uniform(-1.5, 1.5))
                time.sleep(random.uniform(0.008, 0.02))
            x, y = nx, ny
            time.sleep(random.uniform(0.1, 0.35))
        log("CDP 鼠标热身完成")
    except Exception as e:  # noqa: BLE001
        log("CDP 热身异常: %s", e)


def _click_next(page, log) -> bool:
    """点“下一步/创建账户”。综合 bit_register + px_swiftshader 两套已验证选择器。"""
    for sel in ('#nextButton', 'button[data-testid="primaryButton"]', '#iSignupAction',
                'button[type="submit"]', 'input[type="submit"]', 'button.win-button'):
        try:
            b = page.query_selector(sel)
            if b and b.is_visible():
                b.click(timeout=4000)
                log("点击 Next (sel=%s)", sel)
                return True
        except Exception:
            continue
    for how in (lambda: page.get_by_role("button", name="Next").first.click(timeout=3500),
                lambda: page.get_by_role("button", name="Create account").first.click(timeout=3500)):
        try:
            how()
            log("点击 Next (role button)")
            return True
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
        log("回车提交（无按钮匹配）")
        return True
    except Exception:
        return False


def _dump_px_after_state(page, log) -> None:
    """PX 通过后仍停留时，记录可推进元素摘要，避免盲目回车循环。"""
    try:
        st = page.evaluate(
            """() => {
              const vis = (e) => { const r=e.getBoundingClientRect();
                const s=getComputedStyle(e);
                return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'; };
              const pack = (e) => ({
                tag:e.tagName, id:e.id||'', name:e.name||'', type:e.type||'',
                role:e.getAttribute('role')||'', testid:e.getAttribute('data-testid')||'',
                ariaDisabled:e.getAttribute('aria-disabled')||'', disabled:!!e.disabled,
                txt:((e.innerText||e.value||e.getAttribute('aria-label')||'').trim()).slice(0,80)
              });
              const buttons=[...document.querySelectorAll('button,input[type=submit],input[type=button],[role=button]')]
                .filter(vis).slice(0,20).map(pack);
              const forms=[...document.forms].slice(0,10).map(f=>({
                id:f.id||'', name:f.name||'', method:f.method||'', action:(f.action||'').slice(0,120),
                inputs:f.querySelectorAll('input').length, buttons:f.querySelectorAll('button,input[type=submit]').length
              }));
              const iframes=[...document.querySelectorAll('iframe')].slice(0,10).map(f=>({
                id:f.id||'', name:f.name||'', title:f.title||'', src:(f.src||'').slice(0,120),
                visible:vis(f)
              }));
              const active=document.activeElement ? pack(document.activeElement) : null;
              const text=(document.body&&document.body.innerText||'').replace(/\\s+/g,' ').trim().slice(0,300);
              return {buttons, forms, iframes, active, text};
            }"""
        )
        log("PX 后 DOM 摘要=%s", json.dumps(st, ensure_ascii=False)[:1600])
    except Exception as e:  # noqa: BLE001
        log("PX 后 DOM 摘要失败: %s", e)


def _advance_after_px_passed(page, log) -> bool:
    """PX 已通过但页面未跳转时，尝试明确触发表单继续动作。"""
    selectors = (
        'button[data-testid="primaryButton"]',
        '#nextButton',
        '#iSignupAction',
        'button[type="submit"]',
        'input[type="submit"]',
        '[role="button"]',
    )
    for sel in selectors:
        try:
            els = page.query_selector_all(sel)
        except Exception:
            els = []
        for el in els:
            try:
                if not el.is_visible():
                    continue
                disabled = el.evaluate(
                    "(e)=>!!e.disabled || e.getAttribute('aria-disabled')==='true'"
                )
                txt = (el.inner_text() if el.evaluate("(e)=>e.tagName!=='INPUT'") else el.get_attribute("value")) or ""
                if disabled:
                    log("PX 后跳过禁用按钮 sel=%s text=%r", sel, txt[:40])
                    continue
                el.click(timeout=3000)
                log("PX 后点击继续按钮 sel=%s text=%r", sel, txt[:40])
                return True
            except Exception as e:  # noqa: BLE001
                log("PX 后按钮尝试失败 sel=%s: %s", sel, e)
    try:
        page.locator("body").click(position={"x": 640, "y": 450}, timeout=2000)
    except Exception:
        pass
    for key in ("Enter", "Space"):
        try:
            page.keyboard.press(key)
            log("PX 后聚焦页面并按 %s", key)
            return True
        except Exception:
            continue
    return False


def _pick_dropdown(page, trigger_sel, log, want_index: Optional[int] = None) -> bool:
    """Fluent v9 自定义下拉：label 会拦点击 → force 点触发器/label/键盘 → 选 role=option。"""
    bid = trigger_sel.lstrip("#")
    opened = False
    for how in (lambda: page.click(trigger_sel, force=True, timeout=3000),
                lambda: page.click(f'label[for="{bid}"]', force=True, timeout=2000),
                lambda: (page.focus(trigger_sel), page.keyboard.press("Enter"))):
        try:
            how()
            opened = True
            break
        except Exception:
            continue
    if not opened:
        # 兜底：原生 <select>
        try:
            native = page.query_selector(trigger_sel)
            if native:
                native.select_option(index=(want_index or random.randint(1, 5)))
                log("原生 select %s 选中", trigger_sel)
                return True
        except Exception:
            pass
        return False
    page.wait_for_timeout(500)
    try:
        opts = page.query_selector_all('[role="option"], li[role="option"], .ms-Dropdown-item')
        opts = [o for o in opts if o.is_visible()]
        if opts:
            if want_index is not None:
                pick = opts[min(want_index, len(opts) - 1)]
            else:
                pick = random.choice(opts[1:] or opts)
            try:
                pick.click(force=True, timeout=3000)
            except Exception:
                pick.click()
            log("下拉 %s 选中 option（共 %d）", trigger_sel, len(opts))
            page.wait_for_timeout(250)
            return True
    except Exception:
        pass
    # 键盘兜底
    try:
        for _ in range(random.randint(1, 4)):
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(80)
        page.keyboard.press("Enter")
        log("下拉 %s 键盘选择", trigger_sel)
        return True
    except Exception:
        return False


def _type_into(page, el, text: str, log, *, human: bool = True) -> str:
    """把 text 写入 el 并【回读校验】：先人手打字（触发 React onChange），
    回读不一致再用 el.fill 兜底（fill 直写目标元素，杜绝“打进了别的框”）。返回最终回读值。"""
    try:
        el.click()
    except Exception:
        pass
    try:
        el.fill("")  # 清空，避免叠加旧值
    except Exception:
        pass
    page.wait_for_timeout(random.randint(150, 350))
    if human:
        try:
            page.keyboard.type(text, delay=random.randint(35, 95))
        except Exception:
            pass
    got = ""
    try:
        got = el.input_value() or ""
    except Exception:
        pass
    if got != text:
        # 人手打字进了错的框 / 未完整 → 直写目标元素兜底
        try:
            el.fill(text)
            got = el.input_value() or ""
        except Exception:
            pass
        log("回读修正: 打字得到 len=%d，用 fill 兜底后 len=%d", len(got) if got != text else 0, len(got))
    return got


def _fill_email(page, email: str, log) -> bool:
    # 仅用【邮箱专有】选择器：绝不用泛型 input[type=text]（会误吞“姓名”页的名字框）。
    for sel in ('#usernameInput', 'input[name="MemberName"]', 'input[type="email"]', '#MemberName'):
        el = page.query_selector(sel)
        if el and el.is_visible():
            cur = (el.input_value() or "").strip()
            if cur == email:
                return True
            got = _type_into(page, el, email, log, human=True)
            log("填邮箱 %s (sel=%s 回读len=%d)", email, sel, len(got))
            return True
    return False


def _email_error(low: str) -> bool:
    """邮箱页报错（已被占用/不可用）→ 需换邮箱重填。"""
    for kw in ("already has this email", "is already taken", "someone already has",
               "already a microsoft account", "try another", "isn't available",
               "not available", "choose a different"):
        if kw in low:
            return True
    return False


def _password_error(low: str) -> bool:
    """密码页报错（含邮箱片段/太短/太常见等）→ 需换密码重填。"""
    for kw in ("password can't contain", "password cant contain",
               "part of your email", "enter a password", "8 characters",
               "too common", "password you entered", "isn't strong"):
        if kw in low:
            return True
    return False


def _rand_pwd_safe(email_local: str) -> str:
    """随机强密码，且保证不含邮箱 @ 前片段（避免 MS “不能含邮箱片段”校验）。"""
    local = (email_local or "").lower()
    for _ in range(20):
        pwd = _rand_pwd()
        low = pwd.lower()
        if local and local in low:
            continue
        # 不与邮箱本地部分共享 ≥4 连续字符
        bad = any(len(local) >= 4 and local[i:i + 4] in low for i in range(max(0, len(local) - 3)))
        if not bad:
            return pwd
    return _rand_pwd()


def _detect_stage(url: str, low: str, heading: str) -> str:
    """SPA 阶段判定（URL 常年停在 signup?lic=1，必须靠 heading/正文）。"""
    u = (url or "").lower()
    h = (heading or "").lower()
    if any(k in u for k in ("account.microsoft.com", "outlook.live.com",
                            "login.live.com/oauth20", "account.live.com/account")):
        return "created"
    if ("you're all set" in low or "your account has been created" in low
            or "welcome to outlook" in low or "you’re all set" in low):
        return "created"
    if ("account.live.com/proofs" in u or "frmaddproof" in low
            or "add security info" in low or "help us protect your account" in low
            or "security info" in h or "add a way to prove" in low
            or ("proof" in u and "add" in u)):
        return "proofs"
    return ""


def _px_challenge_still_active(page, low: Optional[str] = None) -> bool:
    """PX iframe/提示文本仍在时，不把单纯按钮消失当作通过。"""
    frame_active = False
    try:
        frame_active = bool(page.evaluate(
            """() => {
              const vis = (e) => { const r=e.getBoundingClientRect();
                const s=getComputedStyle(e);
                return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'; };
              return [...document.querySelectorAll('iframe')].some(f => {
                const title=(f.title||'').toLowerCase();
                const src=(f.src||'').toLowerCase();
                const testid=(f.getAttribute('data-testid')||'').toLowerCase();
                return vis(f) && (
                  title.includes('verification challenge') ||
                  title.includes('human') ||
                  testid.includes('humancaptcha') ||
                  src.includes('iframe.hsprotect.net')
                );
              });
            }"""
        ))
    except Exception:
        frame_active = False
    if low is None:
        try:
            low = (page.content() or "").lower()
        except Exception:
            low = ""
    text_active = "press and hold" in low or "let's prove you're human" in low
    return frame_active or text_active


def _px_challenge_cleared(page, log=None) -> bool:
    """强判定：widget 消失且 challenge iframe/提示消失，或已进入后续阶段。"""
    try:
        low = (page.content() or "").lower()
    except Exception:
        low = ""
    stage = _detect_stage(page.url, low, "")
    widget_gone = _visible_captcha_box(page) is None
    still_active = _px_challenge_still_active(page, low)
    cleared = bool(stage) or (widget_gone and not still_active)
    if log:
        log("PX 清除复核 widget_gone=%s still_active=%s stage=%r cleared=%s",
            widget_gone, still_active, stage, cleared)
    return cleared


def _fill_password(page, pwd: str, log) -> bool:
    for sel in ('#PasswordInput', 'input[name="Password"]', 'input[type="password"]', '#passwordEntry'):
        el = page.query_selector(sel)
        if el and el.is_visible():
            cur = el.input_value() or ""
            if cur == pwd:
                return True  # 已是目标密码，无需重填
            got = _type_into(page, el, pwd, log, human=True)
            ok = got == pwd
            log("填密码 (sel=%s 回读len=%d 匹配=%s)", sel, len(got), ok)
            return True
    return False


def _fill_birthday(page, log) -> bool:
    has_bday = (page.query_selector('#BirthMonthDropdown') or page.query_selector('#BirthDayDropdown')
                or page.query_selector('#BirthYear') or page.query_selector('[name="BirthYear"]'))
    if not has_bday:
        return False
    # 可能存在国家/地区下拉（美区默认已选 United States，但保险起见处理）
    if page.query_selector('#countryList') or page.query_selector('#Country'):
        _pick_dropdown(page, '#countryList' if page.query_selector('#countryList') else '#Country', log)
        page.wait_for_timeout(300)
    _pick_dropdown(page, '#BirthMonthDropdown', log, want_index=random.randint(1, 12))
    page.wait_for_timeout(300)
    _pick_dropdown(page, '#BirthDayDropdown', log, want_index=random.randint(1, 27))
    page.wait_for_timeout(300)
    y = (page.query_selector('#BirthYear') or page.query_selector('[name="BirthYear"]')
         or page.query_selector('input[type="number"]'))
    if y and not (y.input_value() or "").strip():
        try:
            y.click()
            y.fill(str(random.randint(1988, 2000)))
        except Exception:
            pass
    log("填生日（month+day+year）")
    return True


def _fill_name(page, log) -> bool:
    fn = (page.query_selector('#firstNameInput') or page.query_selector('input[name="FirstName"]')
          or page.query_selector('input[name="firstNameInput"]'))
    if not (fn and fn.is_visible()):
        return False
    ln = (page.query_selector('#lastNameInput') or page.query_selector('input[name="LastName"]')
          or page.query_selector('input[name="lastNameInput"]'))
    if not (fn.input_value() or "").strip():
        try:
            fn.click()
            page.keyboard.type(random.choice(FIRST_NAMES), delay=random.randint(30, 90))
        except Exception:
            pass
    if ln and not (ln.input_value() or "").strip():
        try:
            ln.click()
            page.keyboard.type(random.choice(LAST_NAMES), delay=random.randint(30, 90))
        except Exception:
            pass
    log("填姓名")
    return True


# 按住策略：PX press 校验的是【行为真实性】而非单纯时长。进度环 ~2s 填满，人手在填满瞬间松开。
# 因此：贝塞尔逼近 + 阅读停顿 + 【近乎静止】的按住（仅偶发亚像素漂移，低频）+ 环填满即松。
PRESS_HOLD_MS = int(os.environ.get("SS_PRESS_HOLD_MS", "1600"))   # 填满检测失败时的兜底时长
PRESS_HOLD_JIT = int(os.environ.get("SS_PRESS_HOLD_JIT", "200"))  # ±抖动
PRESS_RELEASE_ON_FULL = os.environ.get("SS_PRESS_RELEASE_ON_FULL", "1").strip() == "1"


def _bezier(p0, p1, steps):
    """三次贝塞尔，控制点带随机弯曲 + ease-in/out，产出 (x,y) 序列。"""
    (x0, y0), (x1, y1) = p0, p1
    cx1 = x0 + (x1 - x0) * random.uniform(0.2, 0.4) + random.uniform(-40, 40)
    cy1 = y0 + (y1 - y0) * random.uniform(0.2, 0.4) + random.uniform(-40, 40)
    cx2 = x0 + (x1 - x0) * random.uniform(0.6, 0.8) + random.uniform(-40, 40)
    cy2 = y0 + (y1 - y0) * random.uniform(0.6, 0.8) + random.uniform(-40, 40)
    pts = []
    for i in range(steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)  # smoothstep（ease-in-out）
        mt = 1 - t
        x = (mt**3) * x0 + 3 * (mt**2) * t * cx1 + 3 * mt * (t**2) * cx2 + (t**3) * x1
        y = (mt**3) * y0 + 3 * (mt**2) * t * cy1 + 3 * mt * (t**2) * cy2 + (t**3) * y1
        pts.append((x, y))
    return pts


def _bar_fill_fraction(page, box) -> float:
    """截取按钮条，用页面 canvas 解码 PNG，测微软蓝填充占宽（0~1）。失败返回 -1。"""
    try:
        clip = {"x": float(box["x"]), "y": float(box["y"]),
                "width": float(box["width"]), "height": float(box["height"])}
        raw = page.screenshot(clip=clip)
    except Exception:
        return -1.0
    try:
        import base64
        b64 = base64.b64encode(raw).decode("ascii")
        frac = page.evaluate(
            """(b64) => new Promise((resolve) => {
              const img = new Image();
              img.onload = () => {
                const c = document.createElement('canvas');
                c.width = img.width; c.height = img.height;
                const ctx = c.getContext('2d');
                ctx.drawImage(img, 0, 0);
                let best = 0;
                const skip = Math.max(6, Math.floor(img.width * 0.04));
                const ys = [Math.floor(img.height/2), Math.floor(img.height*0.45), Math.floor(img.height*0.55)];
                for (const y of ys) {
                  if (y < 0 || y >= img.height) continue;
                  const data = ctx.getImageData(0, y, img.width, 1).data;
                  let blue = 0, tot = 0;
                  for (let x = skip; x < img.width - skip; x++) {
                    tot++;
                    const i = x * 4;
                    const r = data[i], g = data[i+1], b = data[i+2];
                    // 填充条：饱和微软蓝。白底+蓝字不要算进去（字是离散像素，前缀扫描会在字前断开）。
                    if (b > 150 && r < 120 && g < 180 && (b - r) > 40) blue++;
                  }
                  const f = tot ? blue / tot : 0;
                  if (f > best) best = f;
                }
                resolve(best);
              };
              img.onerror = () => resolve(-1);
              img.src = 'data:image/png;base64,' + b64;
            })""",
            b64,
        )
        return float(frac) if frac is not None else -1.0
    except Exception:
        return -1.0


def _do_px_press(page, cdp, res, log, attempt: int = 0) -> str:
    """检测【可见】#px-captcha → 人手化真按（贝塞尔逼近+阅读停顿+近静止按住+环满即松）。

    返回 "none" | "cleared" | "failed"。
    """
    box = _visible_captcha_box(page)
    if not box:
        return "none"
    res["px_challenge_seen"] = True
    cx = box["x"] + box["width"] / 2.0
    # hsprotect iframe 约 90px 高，按钮在下半；#px-captcha 本身约 42px 取几何中心即可
    if box["height"] >= 70:
        cy = box["y"] + box["height"] * 0.58
    else:
        cy = box["y"] + box["height"] / 2.0
    log("发现【可见】#px-captcha box=%s 中心=(%.1f,%.1f) attempt=%d → 人手化真按",
        {k: round(v, 1) for k, v in box.items()}, cx, cy, attempt)
    _screenshot(page, f"px_before_{attempt}")

    # PX_MANUAL=1：人工在可见窗口里按住 PX 按钮，脚本只等候不代按。
    if os.environ.get("PX_MANUAL", "0").strip() == "1":
        wait_s = int(os.environ.get("PX_MANUAL_WAIT", "180"))
        log("✋ 手动模式：请在浏览器窗口按住 PX 按钮约 2 秒（不要 8-10 秒），最多等 %ds…", wait_s)
        dl = time.time() + wait_s
        while time.time() < dl:
            page.wait_for_timeout(1000)
            if _visible_captcha_box(page) is None:
                res["px_pressed"] = True
                res["press_backend"] = "manual"
                log("✅ 手动按住生效，PX 挑战已清除")
                return "cleared"
        log("⚠️ 手动等待超时（%ds），仍见 PX widget", wait_s)
        return "failed"

    use_os = os.environ.get("PX_OS_PRESS", "0").strip() == "1" or OS_PRESS
    if use_os:
        try:
            from os_press import (
                activate_profile_window,
                os_press_available,
                os_press_hold,
                viewport_to_screen,
            )
            ok, why = os_press_available()
            if not ok:
                log("OS-HID 不可用（%s）——不回退 CDP（会把挑战打成 bot）", why)
                return "failed"
            activate_profile_window(lambda m: log("%s", m))
            sx, sy = viewport_to_screen(page, cx, cy)
            hold = int(os.environ.get("SS_PRESS_HOLD_MS", "2600")) + random.randint(-150, 150)
            log("OS-HID 真按 hold=%dms screen=(%.1f,%.1f)（按住期间不截图）", hold, sx, sy)
            os_press_hold(sx, sy, hold, lambda m: log("%s", m))
            res["px_pressed"] = True
            res["press_backend"] = "os_hid"
            time.sleep(1.2)
            cleared = _px_challenge_cleared(page, log)
            log("OS-HID 真按后强清除=%s", cleared)
            return "cleared" if cleared else "failed"
        except Exception as e:  # noqa: BLE001
            log("OS-HID 异常（不回退 CDP）: %s", e)
            return "failed"

    # 1) 贝塞尔逼近（从当前视口某处到按钮），变速 + 亚像素
    try:
        start = (random.uniform(cx - 300, cx - 120), random.uniform(cy - 180, cy - 60))
        for (mx, my) in _bezier(start, (cx, cy), random.randint(22, 34)):
            _cdp_move(cdp, mx + random.uniform(-0.6, 0.6), my + random.uniform(-0.6, 0.6), buttons=0)
            time.sleep(random.uniform(0.012, 0.03))
        # 阅读/瞄准停顿（人在按前会顿一下）
        time.sleep(random.uniform(0.35, 0.85))
    except Exception as e:  # noqa: BLE001
        log("逼近异常: %s", e)

    px = cx + random.uniform(-1.2, 1.2)
    py = cy + random.uniform(-1.0, 1.0)
    try:
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": float(px), "y": float(py),
            "button": "left", "buttons": 1, "clickCount": 1,
        })
        res["px_pressed"] = True
        log("mousePressed x=%.2f y=%.2f", px, py)
    except Exception as e:  # noqa: BLE001
        log("mousePressed 异常: %s", e)
        return "failed"

    # 按住期间禁止截图/evaluate：会卡住 mouseMoved，进度环填不满。
    target_ms = int(os.environ.get("SS_PRESS_HOLD_MS", "2500")) + random.randint(-200, 300)
    hard_cap_ms = int(os.environ.get("SS_PRESS_CAP_MS", "3000"))
    t0 = time.time()
    dx = dy = 0.0
    next_move = 0.0
    filled = False
    while True:
        el = (time.time() - t0)
        el_ms = el * 1000.0
        if el > next_move:
            next_move = el + random.uniform(0.08, 0.15)
            dx += random.uniform(-1.1, 1.1)
            dy += random.uniform(-0.8, 0.8)
            dx = max(-3.0, min(3.0, dx))
            dy = max(-2.2, min(2.2, dy))
            try:
                cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseMoved", "x": float(px + dx), "y": float(py + dy),
                    "button": "left", "buttons": 1,
                })
            except Exception:
                pass
        if el_ms >= target_ms or el_ms > hard_cap_ms:
            log("达目标按住 %.0fms → 松开", el_ms)
            break
        time.sleep(0.02)

    t_up = time.time()
    try:
        cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": float(px + dx), "y": float(py + dy),
            "button": "left", "buttons": 0, "clickCount": 1,
        })
        log("mouseReleased pressDuration=%.0fms", (t_up - t0) * 1000.0)
    except Exception as e:  # noqa: BLE001
        log("mouseReleased 异常: %s", e)

    page.wait_for_timeout(3500)
    _screenshot(page, f"px_after_{attempt}")
    cleared = _px_challenge_cleared(page, log)
    res["px_passed"] = res.get("px_passed") or cleared
    log("按压结果 attempt=%d 强清除=%s", attempt, cleared)
    return "cleared" if cleared else "failed"


def _dump_proofs(page, log) -> None:
    try:
        fields = page.eval_on_selector_all(
            "input,select,button,[role=button]",
            "els=>els.filter(e=>e.offsetParent).slice(0,25)."
            "map(e=>({t:e.tagName,type:e.type||'',id:e.id||'',name:e.name||'',"
            "ph:e.placeholder||'',txt:(e.innerText||e.value||'').slice(0,30)}))")
        for f in fields:
            log("  proofs字段: %s", f)
    except Exception as e:  # noqa: BLE001
        log("proofs dump 失败: %s", e)


def register(proxy: Optional[str] = None, *, country: str = "US", verbose: bool = True) -> dict:
    sync_playwright = _import_sync_playwright()
    proxy_dict = _parse_proxy(proxy) if proxy else None
    locale, tz_id = _locale_tz(country)

    email_user = _rand_user()
    email = email_user + "@outlook.com"
    pwd = _rand_pwd()
    res = {
        "email": email, "password": pwd, "proxy": (proxy or "DIRECT"), "country": country,
        "created": False, "stage": "", "final_url": "", "note": "",
        # 逐步里程碑
        "email_filled": False, "password_filled": False, "birthday_filled": False,
        "name_filled": False, "px_challenge_seen": False, "px_pressed": False,
        "px_passed": False, "create_account_passed": False, "reached_proofs": False,
    }

    def log(msg, *a):
        if verbose:
            logger.info(msg % a if a else msg)

    launch_args = _build_launch_args()

    logger.info("=" * 84)
    log("ss_register 开始 email=%s proxy=%s country=%s headful=%s real_gpu=%s", email,
        (str(proxy)[:46] if proxy else "DIRECT"), country, HEADFUL, REAL_GPU)
    log("渲染模式=%s launch args=%s",
        ("真实 GPU(Apple/Metal)" if REAL_GPU else "SwiftShader 软件渲染"),
        " ".join(launch_args))

    net_notes: list[str] = []

    with sync_playwright() as pw:
        launch_kwargs = {"headless": not HEADFUL, "args": launch_args}
        if proxy_dict:
            launch_kwargs["proxy"] = proxy_dict
        browser = pw.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 900},
            locale=locale,
            timezone_id=tz_id,
        )
        page = ctx.new_page()

        def on_request(req):
            u = req.url
            if "CreateAccount" in u or "/API/CreateAccount" in u:
                net_notes.append("REQ " + req.method + " " + u[:120])

        def on_response(resp):
            u = resp.url
            if "CreateAccount" in u:
                try:
                    snippet = (resp.text() or "")[:200]
                except Exception:
                    snippet = "<无法读取>"
                net_notes.append("RESP %s status=%s %s" % (u[:80], resp.status, snippet[:120]))
                log("CreateAccount 响应 status=%s body=%s", resp.status, snippet[:160])

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            cdp = ctx.new_cdp_session(page)
        except Exception as e:  # noqa: BLE001
            log("new_cdp_session 失败，尝试 browser 级: %s", e)
            cdp = browser.new_browser_cdp_session()

        # WebGL renderer 自检 + stealth 自检（REAL_GPU 下确认已脱离 SwiftShader，且 webdriver=false）
        try:
            page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
            renderer = page.evaluate(
                "() => { try { const c=document.createElement('canvas');"
                "const gl=c.getContext('webgl')||c.getContext('experimental-webgl');"
                "if(!gl) return 'NO_WEBGL';"
                "const d=gl.getExtension('WEBGL_debug_renderer_info');"
                "const r=d?gl.getParameter(d.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER);"
                "const v=d?gl.getParameter(d.UNMASKED_VENDOR_WEBGL):gl.getParameter(gl.VENDOR);"
                "return v+' || '+r;"
                "} catch(e){ return 'ERR:'+e; } }")
            res["webgl_renderer"] = renderer
            log("WebGL renderer = %s", renderer)
            low_r = (renderer or "").lower()
            is_ss = "swiftshader" in low_r
            res["webgl_is_swiftshader"] = is_ss
            if REAL_GPU and is_ss:
                log("⚠️⚠️ SS_REAL_GPU=1 但 renderer 仍是 SwiftShader！真 GPU 未生效，PX 大概率仍拒。")
            elif REAL_GPU:
                log("✅ 渲染precondition达成：renderer 非 SwiftShader（真 GPU 生效）= %s", renderer)
            # stealth 自检：navigator.webdriver 应为 false，plugins 应存在
            stealth = page.evaluate(
                "() => ({ webdriver: navigator.webdriver,"
                " plugins: (navigator.plugins && navigator.plugins.length) || 0,"
                " langs: (navigator.languages||[]).join(','),"
                " ua: navigator.userAgent.slice(0,60) })")
            res["stealth"] = stealth
            log("stealth 自检: webdriver=%s plugins=%s langs=%s ua=%s",
                stealth.get("webdriver"), stealth.get("plugins"),
                stealth.get("langs"), stealth.get("ua"))
        except Exception as e:  # noqa: BLE001
            log("WebGL/stealth 自检异常: %s", e)

        try:
            page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
            log("已打开 %s", SIGNUP_URL)
        except Exception as e:  # noqa: BLE001
            log("goto 异常: %s", e)
        page.wait_for_timeout(random.randint(3000, 4500))
        _cdp_warmup(cdp, log)  # 先制造真实鼠标活动，避免“零 mousemove=机器人”
        _screenshot(page, "00_landing")

        seen_create_form = False
        prev_heading = None
        stuck = 0
        px_attempts = 0
        px_max_attempts = int(os.environ.get("SS_PX_MAX_ATTEMPTS", "3"))
        px_after_dumped = False
        for step in range(MAX_STEPS):
            page.wait_for_timeout(random.randint(2000, 2800))
            url = page.url
            try:
                title = page.title()
            except Exception:
                title = ""
            try:
                low = (page.content() or "").lower()
            except Exception:
                low = ""
            st = _page_state(page)
            if not isinstance(st, dict):
                st = {}
            heading = " ".join(st.get("heading", []))[:70]
            input_ids = [(i.get("id") or i.get("name") or i.get("type")) for i in st.get("inputs", [])]
            log("[step %d] title=%r heading=%r inputs=%s", step, title[:40], heading, input_ids)

            # 卡住检测：heading 连续未变 → 计数，达到阈值后 dump 诊断并退出
            if heading and heading == prev_heading:
                stuck += 1
            else:
                stuck = 0
            prev_heading = heading

            # 1) PX 按压（自动 CDP 真按）——优先处理，限次 + 冷却（避免快速重试被 PX 加难）
            captcha_box = _visible_captcha_box(page)
            px_visible_after_pass = False
            if captcha_box and res.get("px_passed"):
                log("PX 已通过，忽略可见 widget 残留并继续推进注册流程")
                stuck = 0
                px_visible_after_pass = True
            elif captcha_box:
                if px_attempts >= px_max_attempts:
                    res["stage"] = res["stage"] or "px_blocked"
                    res["note"] = f"PX 按压 {px_attempts} 次仍未通过（widget 未清除）"
                    log("⚠️ %s → 停止并报告", res["note"])
                    _screenshot(page, "px_blocked")
                    break
                outcome = _do_px_press(page, cdp, res, log, attempt=px_attempts)
                px_attempts += 1
                if outcome == "cleared":
                    log("✅ PX 挑战已通过（第 %d 次）", px_attempts)
                    stuck = 0
                    page.wait_for_timeout(2500)
                else:
                    # 失败后拉长冷却，降低 PX 难度升级
                    page.wait_for_timeout(random.randint(3500, 5500))
                continue

            # 2) 阶段判定（SPA：靠 heading/正文，URL 不变）
            stage = _detect_stage(url, low, heading)
            if stage == "created":
                res["created"] = True
                res["stage"] = "created"
                res["create_account_passed"] = True
                log("✅ 账号建成/已登录: url=%s heading=%r", url[:90], heading)
                break
            if stage == "proofs":
                res["reached_proofs"] = True
                res["created"] = True
                res["create_account_passed"] = True
                res["stage"] = "proofs"
                res["note"] = "到达 proofs/Add（加安全信息）——账号已在浏览器内建成"
                log("✅ 账号已建成，到达 proofs/Add。dump 字段:")
                _dump_proofs(page, log)
                _screenshot(page, "proofs")
                res["proofs_url"] = page.url
                try:
                    res["proofs_html"] = page.content() or ""
                except Exception as e:  # noqa: BLE001
                    res["proofs_html"] = ""
                    log("捕获 proofs HTML 失败: %s", e)
                from browser_proofs import bind_recovery_in_browser, browser_proofs_enabled
                if browser_proofs_enabled():
                    bound = bind_recovery_in_browser(page, log)
                    res["recovery_email"] = bound.get("recovery_email", "")
                    res["recovery_password"] = bound.get("recovery_password", "")
                    res["proofs_method"] = bound.get("proofs_method", "browser_proofs")
                    if bound.get("ok"):
                        res["stage"] = "proofs_bound"
                        res["note"] = "浏览器已绑定恢复邮箱"
                        try:
                            res["proofs_url"] = page.url
                        except Exception:
                            pass
                    else:
                        res["stage"] = "proofs_failed"
                        res["note"] = bound.get("note") or "浏览器绑定恢复邮箱失败"
                        log("✗ 浏览器绑恢复邮箱失败: %s", res["note"])
                break

            if px_visible_after_pass:
                if not px_after_dumped:
                    _dump_px_after_state(page, log)
                    px_after_dumped = True
                _advance_after_px_passed(page, log)
                page.wait_for_timeout(2500)
                continue

            # 顶层卡住熔断：同一 heading 连续多步无进展（含表单反复点 Next 不前进）→ dump 退出
            if stuck >= 5:
                res["note"] = f"卡在 heading={heading!r} 连续 {stuck} 步无进展"
                res["stage"] = res["stage"] or "stuck"
                log("⚠️ %s → dump 诊断退出", res["note"])
                log("卡点 page_state=%s", json.dumps(st, ensure_ascii=False)[:700])
                _screenshot(page, "stuck")
                break

            # 3) 表单状态机（专有选择器，顺序无关，逐类检测）
            #    邮箱页报错（占用/不可用）→ 换邮箱重填
            if _fill_email(page, email, log):
                if _email_error(low):
                    email = _rand_user() + "@outlook.com"
                    res["email"] = email
                    log("⚠️ 邮箱被占用/不可用 → 换新邮箱 %s 重填", email)
                    _fill_email(page, email, log)
                res["email_filled"] = True
                _screenshot(page, "01_email")
                page.wait_for_timeout(random.randint(500, 1100))
                _click_next(page, log)
                continue
            if _fill_password(page, pwd, log):
                res["password_filled"] = True
                if _password_error(low):
                    pwd = _rand_pwd_safe(email.split("@")[0])
                    res["password"] = pwd
                    log("⚠️ 密码被拒（含邮箱片段/不合格）→ 换新密码重填 len=%d", len(pwd))
                    _fill_password(page, pwd, log)
                _screenshot(page, "02_password")
                page.wait_for_timeout(random.randint(500, 1100))
                _click_next(page, log)
                continue
            if _fill_birthday(page, log):
                res["birthday_filled"] = True
                _screenshot(page, "03_birthday")
                page.wait_for_timeout(random.randint(400, 900))
                seen_create_form = True
                _click_next(page, log)
                continue
            if _fill_name(page, log):
                res["name_filled"] = True
                _screenshot(page, "04_name")
                page.wait_for_timeout(random.randint(400, 900))
                # 姓名/生日是建号前最后表单，点击“创建”后大概率触发 PX → CreateAccount
                seen_create_form = True
                _click_next(page, log)
                continue

            # 4) 无可填字段：若卡住太久，dump 诊断并退出
            if stuck >= 4:
                if "creation has been blocked" in low:
                    _stuck_stage = "account_creation_blocked"
                elif "we ran into a problem" in low:
                    _stuck_stage = "signup_risk_rejected"
                else:
                    _stuck_stage = "stuck"
                res["stage"] = res["stage"] or _stuck_stage
                res["note"] = f"卡在 heading={heading!r} 连续 {stuck} 步无可操作字段"
                log("⚠️ %s → dump 诊断退出", res["note"])
                log("卡点 page_state=%s", json.dumps(st, ensure_ascii=False)[:600])
                _screenshot(page, "stuck")
                break

            # 5) 兜底推进（部分插页只有 Next/Continue，无输入框）
            if not _click_next(page, log):
                log("本步无可操作元素且无法点击 Next")
            if seen_create_form and _detect_stage(page.url, low, heading):
                res["create_account_passed"] = True

        if not res.get("created") and not res.get("note"):
            if res.get("px_passed"):
                res["stage"] = res["stage"] or "px_passed_not_submitted"
                res["note"] = "PX 已通过但仍停在人机验证页，未触发 CreateAccount"
            elif res.get("px_challenge_seen") and _px_challenge_still_active(page):
                res["stage"] = res["stage"] or "px_challenge_still_active"
                res["note"] = "PX challenge iframe/按住提示仍存在，未达到通过状态"
            else:
                res["stage"] = res["stage"] or "max_steps"
                res["note"] = f"达到最大步骤 MAX_STEPS={MAX_STEPS}，未进入 created/proofs"
            log("⚠️ %s", res["note"])

        res["final_url"] = page.url
        _screenshot(page, "99_final")

        # 收割 cookie（供后续 requests 续 OAuth/proofs 交接用）：
        #  - res["cookies"]：精简版（name/value/domain），仅供日志/回看
        #  - res["_full_cookies"]：完整 Playwright cookie（含 path），交接 requests 会话用
        try:
            full = ctx.cookies()
            res["_full_cookies"] = full
            res["cookies"] = [{"name": c["name"], "value": c["value"], "domain": c["domain"]}
                              for c in full]
            names = sorted({c["name"] for c in res["cookies"]})
            log("cookie(%d): %s", len(res["cookies"]), ",".join(names))
        except Exception as e:  # noqa: BLE001
            log("收割 cookie 失败: %s", e)

        # 若最终 URL 已离开 signup.live.com 到 account/oauth/proof，则确认 CreateAccount 通过
        fu = res["final_url"]
        if any(k in fu for k in ("account.microsoft.com", "account.live.com", "outlook.live.com",
                                 "oauth20", "/proofs")):
            res["create_account_passed"] = True

        res["create_account_net"] = net_notes[-4:]
        log("网络事件(CreateAccount 相关) = %s", net_notes[-4:] if net_notes else "无")

        try:
            browser.close()
        except Exception:
            pass

    # ── 到达 proofs/Add 后续跑（浏览器会话已关闭，纯 requests 交接）：
    #    绑外部恢复邮箱 → Thunderbird OAuth 换 IMAP/REST refresh_token → 六段活号导出。
    #    反 abuse：并发天然=1（单脚本）、mkt/lc 随 country、建号后不做任何 keepalive/探测。
    if res.get("reached_proofs") and os.environ.get("SS_FINISH_PROOFS", "1") != "0":
        if res.get("stage") == "proofs_failed":
            log("✗ 浏览器绑恢复邮箱失败，跳过 OAuth：%s", res.get("note", ""))
            res["post_status"] = "proofs_failed"
        else:
            try:
                from ss_post import finish_after_proofs
                browser_bound = res.get("stage") == "proofs_bound" and bool(res.get("recovery_email"))
                if browser_bound:
                    log("── 浏览器已绑恢复邮箱，cookie 交接后只跑 Thunderbird OAuth ──")
                else:
                    log("── 续跑 proofs + Thunderbird OAuth（cookie 交接到 requests 会话）──")
                post = finish_after_proofs(
                    email=res["email"], password=res["password"],
                    proofs_url=res.get("proofs_url", res.get("final_url", "")),
                    proofs_html=res.get("proofs_html", ""),
                    cookies=res.get("_full_cookies", []),
                    proxy=proxy, country=country, log=log,
                    proofs_done=browser_bound,
                    recovery_email=res.get("recovery_email", ""),
                    recovery_password=res.get("recovery_password", ""),
                    proofs_method=res.get("proofs_method", ""),
                )
                res["post"] = post
                res["post_status"] = post.get("status", "")
                res["combo_recovery"] = post.get("combo_recovery", "")
                res["recovery_email"] = post.get("recovery_email", "")
                res["refresh_token"] = post.get("refresh_token", "")
                log("续跑结果 status=%s recovery=%s got_refresh_token=%s",
                    post.get("status"), post.get("recovery_email", ""), bool(post.get("refresh_token")))
            except Exception as e:  # noqa: BLE001
                res["post_status"] = "bridge_error"
                res["post"] = {"status": "bridge_error", "note": repr(e)}
                log("续跑桥接异常: %r", e)
    elif res.get("reached_proofs"):
        log("SS_FINISH_PROOFS=0 → 跳过 proofs+OAuth 续跑，仅停在 proofs/Add")

    log("ss_register 结束 created=%s px_passed=%s proofs=%s post=%s got_rt=%s final=%s",
        res["created"], res["px_passed"], res["reached_proofs"],
        res.get("post_status", "-"), bool(res.get("refresh_token")), res["final_url"][:80])
    return res


if __name__ == "__main__":
    proxy_arg: Optional[str] = None
    if len(sys.argv) > 1 and sys.argv[1] not in ("-", "direct", "DIRECT"):
        proxy_arg = sys.argv[1]
    elif os.environ.get("PX_PROXY"):
        proxy_arg = os.environ["PX_PROXY"]

    # 国家：argv[2] > SS_COUNTRY > US（浏览器 locale/timezone 与恢复邮箱 mkt/lc 均跟随它）
    country_arg = "US"
    if len(sys.argv) > 2 and sys.argv[2].strip():
        country_arg = sys.argv[2].strip().upper()
    elif os.environ.get("SS_COUNTRY"):
        country_arg = os.environ["SS_COUNTRY"].strip().upper()

    r = register(proxy_arg, country=country_arg)
    # 打印结果前剔除大体积/敏感中间量
    for _k in ("cookies", "_full_cookies", "proofs_html"):
        r.pop(_k, None)
    print("\n=== ss_register 结果 ===")
    print(json.dumps(r, ensure_ascii=False, indent=2))
