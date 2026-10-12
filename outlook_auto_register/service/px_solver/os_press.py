#!/usr/bin/env python3
"""macOS 系统级鼠标按住（CGEvent HID），绕过 CDP Input.dispatchMouseEvent。

背景：PX Press&Hold 对 Playwright/CDP 合成鼠标不认（用户必须人手点才过）。
本模块用 Quartz HID 事件往真实屏幕坐标发 mouseMoved / leftMouseDown / 微抖 / leftMouseUp。
必须 headed 窗口 + 本机辅助功能权限。

用法（单独测坐标）：
  python px_solver/os_press.py --demo 400 400
接入 harvest：
  PX_OS_PRESS=1 PX_SWIFTSHADER_HEADFUL=1 python px_solver/px_swiftshader_solver.py <proxy>
"""
from __future__ import annotations

import math
import os
import random
import time
from typing import Callable, Optional

# CoreGraphics CGEventType / tap / button
_CG_MOUSE_MOVED = 5
_CG_LEFT_DOWN = 1
_CG_LEFT_UP = 2
_CG_LEFT_DRAGGED = 6
_CG_HID_TAP = 0
_CG_BTN_LEFT = 0


def xdotool_available() -> tuple[bool, str]:
    import shutil

    if shutil.which("xdotool"):
        return True, "xdotool ok"
    return False, "xdotool 未安装"


def xdotool_press_hold(
    screen_x: float,
    screen_y: float,
    hold_ms: int,
    log: Optional[Callable] = None,
) -> float:
    """Linux/Xvfb：xdotool 发 OS 级鼠标事件（PX press 按住）。"""
    import subprocess

    def _log(msg: str) -> None:
        if log:
            log(msg)

    ok, why = xdotool_available()
    if not ok:
        raise RuntimeError(why)

    sx, sy = float(screen_x), float(screen_y)
    hold_s = max(0.05, hold_ms / 1000.0)

    def _xdo(*args: object) -> None:
        subprocess.run(["xdotool", *[str(a) for a in args]], check=False)

    startx = sx - random.randint(90, 170)
    starty = sy - random.randint(60, 130)
    ctrlx = (startx + sx) / 2 + random.randint(-40, 40)
    ctrly = (starty + sy) / 2 + random.randint(-30, 30)
    steps = random.randint(22, 34)
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * startx + 2 * (1 - t) * t * ctrlx + t ** 2 * sx + random.uniform(-1.2, 1.2)
        y = (1 - t) ** 2 * starty + 2 * (1 - t) * t * ctrly + t ** 2 * sy + random.uniform(-1.0, 1.0)
        _xdo("mousemove", int(x), int(y))
        time.sleep(random.uniform(0.006, 0.022))
    _xdo("mousemove", "--sync", int(sx), int(sy))
    time.sleep(random.uniform(0.15, 0.3))
    t_down = time.time()
    _xdo("mousedown", 1)
    t0 = time.time()
    while time.time() - t0 < hold_s:
        _xdo("mousemove_relative", "--", random.randint(-2, 2), random.randint(-2, 2))
        time.sleep(random.uniform(0.05, 0.16))
    _xdo("mouseup", 1)
    dur = (time.time() - t_down) * 1000.0
    _log("xdotool pressDuration=%.1fms screen=(%.1f,%.1f)" % (dur, sx, sy))
    return dur


def os_press_available() -> tuple[bool, str]:
    ok, why = xdotool_available()
    if ok:
        return ok, why
    try:
        from Quartz import CGEventCreateMouseEvent  # noqa: F401
        return True, "Quartz HID ok"
    except Exception:
        pass
    try:
        _coregraphics()
        return True, "ctypes CoreGraphics HID ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"Quartz/CoreGraphics 不可用: {exc}"


def _coregraphics():
    import ctypes
    import ctypes.util

    path = ctypes.util.find_library("CoreGraphics") or (
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    cg = ctypes.CDLL(path)

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    cg.CGEventSourceCreate.restype = ctypes.c_void_p
    cg.CGEventSourceCreate.argtypes = [ctypes.c_int32]
    cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
    cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
    cg.CFRelease = getattr(cg, "CFRelease", None)
    if cg.CFRelease:
        cg.CFRelease.argtypes = [ctypes.c_void_p]
    cg.CGPoint = CGPoint
    return cg


# CGEventField kCGMouseEventClickState = 1
_CG_CLICK_STATE = 1
# CGEventSourceStateID kCGEventSourceStateHIDSystemState = 1
_CG_SRC_HID = 1


def _hid_source():
    """复用 HID 系统鼠标源。NULL source 会被标成 private，Chrome/PX 能和真鼠标区分。"""
    try:
        from Quartz import CGEventSourceCreate, kCGEventSourceStateHIDSystemState
        src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
        if src:
            return src, True
    except Exception:
        pass
    cg = _coregraphics()
    src = cg.CGEventSourceCreate(_CG_SRC_HID)
    return src, False


def _post_mouse(kind, x: float, y: float, button=None) -> None:
    btn = _CG_BTN_LEFT if button is None else button
    src, is_quartz = _hid_source()
    try:
        if is_quartz:
            from Quartz import (
                CGEventCreateMouseEvent,
                CGEventPost,
                CGEventSetIntegerValueField,
                kCGHIDEventTap,
                kCGMouseButtonLeft,
            )
            qbtn = button if button is not None else kCGMouseButtonLeft
            ev = CGEventCreateMouseEvent(src, kind, (float(x), float(y)), qbtn)
            if ev is None:
                raise RuntimeError("CGEventCreateMouseEvent 返回 None（检查辅助功能权限）")
            CGEventSetIntegerValueField(ev, _CG_CLICK_STATE, 1)
            CGEventPost(kCGHIDEventTap, ev)
            return
        cg = _coregraphics()
        pt = cg.CGPoint(float(x), float(y))
        ev = cg.CGEventCreateMouseEvent(src, int(kind), pt, int(btn))
        if not ev:
            raise RuntimeError("CGEventCreateMouseEvent 返回 NULL（检查辅助功能权限）")
        cg.CGEventSetIntegerValueField(ev, _CG_CLICK_STATE, 1)
        cg.CGEventPost(_CG_HID_TAP, ev)
        if cg.CFRelease:
            cg.CFRelease(ev)
    finally:
        pass


def os_move(x: float, y: float) -> None:
    _post_mouse(_CG_MOUSE_MOVED, x, y)


def os_down(x: float, y: float) -> None:
    _post_mouse(_CG_LEFT_DOWN, x, y, _CG_BTN_LEFT)


def os_up(x: float, y: float) -> None:
    _post_mouse(_CG_LEFT_UP, x, y, _CG_BTN_LEFT)


def os_drag(x: float, y: float) -> None:
    """按住期间移动：用 LeftMouseDragged，比单纯 MouseMoved 更像真人按住。"""
    try:
        _post_mouse(_CG_LEFT_DRAGGED, x, y, _CG_BTN_LEFT)
    except Exception:
        os_move(x, y)


def _find_profile_window_quartz() -> Optional[dict]:
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGNullWindowID,
        kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionOnScreenOnly,
    )
    wins = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    ) or []
    cands = []
    for w in wins:
        owner = str(w.get("kCGWindowOwnerName") or "")
        if owner != "BitBrowser":
            continue
        b = w.get("kCGWindowBounds") or {}
        width = float(b.get("Width") or 0)
        height = float(b.get("Height") or 0)
        if width < 800 or height < 500:
            continue
        cands.append({
            "owner": owner,
            "pid": int(w.get("kCGWindowOwnerPID") or 0),
            "wid": int(w.get("kCGWindowNumber") or 0),
            "x": float(b.get("X") or 0),
            "y": float(b.get("Y") or 0),
            "w": width,
            "h": height,
        })
    if not cands:
        return None
    cands.sort(key=lambda d: d["w"] * d["h"], reverse=True)
    return cands[0]


def _find_profile_window_osascript() -> Optional[dict]:
    """列出 BitBrowser 所有窗口，取面积最大的那个（window 1 经常是「要保存密码吗」气泡）。"""
    import subprocess
    script = '''
tell application "System Events"
  tell process "BitBrowser"
    set pidv to unix id
    set bestPos to {0, 0}
    set bestSize to {0, 0}
    set bestArea to 0
    repeat with w in windows
      set s to size of w
      set area to (item 1 of s) * (item 2 of s)
      if area > bestArea then
        set bestArea to area
        set bestPos to position of w
        set bestSize to s
      end if
    end repeat
    return {pidv, item 1 of bestPos, item 2 of bestPos, item 1 of bestSize, item 2 of bestSize}
  end tell
end tell
'''
    r = subprocess.run(["osascript", "-e", script], timeout=8, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    parts = [p.strip() for p in (r.stdout or "").replace("\n", "").split(",") if p.strip()]
    if len(parts) < 5:
        return None
    pid, x, y, w, h = (int(float(parts[0])), float(parts[1]), float(parts[2]),
                       float(parts[3]), float(parts[4]))
    if w < 800 or h < 500:
        return None
    return {"owner": "BitBrowser", "pid": pid, "wid": 0, "x": x, "y": y, "w": w, "h": h}


def find_profile_window() -> Optional[dict]:
    """找指纹 Chromium 窗口（进程名 BitBrowser），不要管理端「比特浏览器」。

    管理端和 profile 窗经常叠在一起；HID 点到管理端 = 按不住 PX。
    venv 往往没有 pyobjc，Quartz 失败时走 osascript。
    """
    try:
        win = _find_profile_window_quartz()
        if win:
            return win
    except Exception:
        pass
    try:
        return _find_profile_window_osascript()
    except Exception:
        return None


def activate_profile_window(log: Optional[Callable] = None) -> Optional[dict]:
    """把 profile Chromium 提到最前（忽略管理端和密码气泡）。返回大窗 bounds。"""
    import subprocess
    script = '''
tell application "System Events"
  -- 管理端常盖住 fingerprint 窗，先别让它抢焦点
  try
    set frontmost of process "BitBrowser" to true
  end try
  tell process "BitBrowser"
    set frontmost to true
    delay 0.15
    -- 关掉 Chrome「要保存密码吗」气泡，避免抢 window 1 / 挡点击
    key code 53
    delay 0.1
    set best to missing value
    set bestArea to 0
    repeat with w in windows
      set s to size of w
      set area to (item 1 of s) * (item 2 of s)
      if area > bestArea then
        set bestArea to area
        set best to w
      end if
    end repeat
    if best is not missing value then
      try
        perform action "AXRaise" of best
      end try
    end if
  end tell
end tell
'''
    try:
        subprocess.run(["osascript", "-e", script], timeout=8, capture_output=True)
    except Exception:
        pass
    time.sleep(0.35)
    win = find_profile_window()
    if not win:
        if log:
            log("未找到 BitBrowser profile 窗口（可能被管理端挡住或未开）")
        return None
    if log:
        log("前置 profile 窗 pid=%s bounds=(%.0f,%.0f %.0fx%.0f)" % (
            win["pid"], win["x"], win["y"], win["w"], win["h"]))
    return win


def viewport_to_screen(page, vx: float, vy: float) -> tuple[float, float]:
    """Playwright 视口坐标 → 屏幕坐标。优先用 Quartz 的 BitBrowser profile 窗，避免 CDP 报 (0,0)。"""
    inset = page.evaluate(
        """() => ({
          outerW: window.outerWidth, outerH: window.outerHeight,
          innerW: window.innerWidth, innerH: window.innerHeight,
          dpr: window.devicePixelRatio || 1,
          screenX: window.screenX, screenY: window.screenY,
        })"""
    )
    native = find_profile_window()
    if native:
        ox, oy = native["x"], native["y"]
        chrome_left = max(0.0, (native["w"] - float(inset["innerW"])) / 2.0)
        chrome_top = max(0.0, native["h"] - float(inset["innerH"]) - chrome_left)
    else:
        cdp = page.context.new_cdp_session(page)
        info = cdp.send("Browser.getWindowForTarget")
        bounds = cdp.send("Browser.getWindowBounds", {"windowId": info["windowId"]})["bounds"]
        chrome_left = max(0.0, (inset["outerW"] - inset["innerW"]) / 2.0)
        chrome_top = max(0.0, inset["outerH"] - inset["innerH"] - chrome_left)
        ox = float(inset.get("screenX") if inset.get("screenX") is not None else (bounds.get("left") or 0))
        oy = float(inset.get("screenY") if inset.get("screenY") is not None else (bounds.get("top") or 0))
    extra = os.environ.get("BIT_OS_CHROME_TOP", "").strip()
    if extra:
        chrome_top = float(extra)
    sx = ox + chrome_left + vx
    sy = oy + chrome_top + vy
    return sx, sy


def os_press_hold(
    screen_x: float,
    screen_y: float,
    hold_ms: int,
    log: Optional[Callable] = None,
) -> float:
    """系统级按住：逼近 → down → 微抖 drag → up。返回 wall-clock ms。"""
    def _log(msg: str) -> None:
        if log:
            log(msg)

    ok, why = os_press_available()
    if not ok:
        raise RuntimeError(why)

    # 逼近
    sx0 = screen_x - 70 + random.uniform(-8, 8)
    sy0 = screen_y - 40 + random.uniform(-8, 8)
    steps = random.randint(10, 18)
    for i in range(1, steps + 1):
        t = i / steps
        mx = (1 - t) * sx0 + t * screen_x + random.uniform(-1.2, 1.2)
        my = (1 - t) * sy0 + t * screen_y + random.uniform(-1.2, 1.2)
        os_move(mx, my)
        time.sleep(random.uniform(0.008, 0.022))
    time.sleep(random.uniform(0.08, 0.18))

    px = screen_x + random.uniform(-0.4, 0.4)
    py = screen_y + random.uniform(-0.4, 0.4)
    t_down = time.time()
    os_down(px, py)
    _log("OS mouseDown screen=(%.1f,%.1f)" % (px, py))
    # 按住期间不发 move/drag：真人按住几乎无事件；高频 LeftMouseDragged 会被 PX 取消
    time.sleep(max(0.05, hold_ms / 1000.0))

    t_up = time.time()
    os_up(px + random.uniform(-0.4, 0.4), py + random.uniform(-0.4, 0.4))
    dur = (t_up - t_down) * 1000.0
    _log("OS mouseUp pressDuration=%.1fms" % dur)
    return dur


if __name__ == "__main__":
    import sys

    ok, why = os_press_available()
    print("available:", ok, why)
    if len(sys.argv) >= 4 and sys.argv[1] == "--demo":
        x, y = float(sys.argv[2]), float(sys.argv[3])
        hold = int(sys.argv[4]) if len(sys.argv) > 4 else 1800
        print("demo hold at", x, y, hold)
        os_press_hold(x, y, hold, print)
