"""Small macOS native-keyboard driver for visible RoxyChrome tests.

This intentionally does not use Playwright/CDP.  It drives the already-open
RoxyChrome window through macOS System Events and reads only the active tab's
title/URL through Chromium's AppleScript dictionary.

It is not the private Codex ``@oai/sky`` package.  That package is bound to a
running Codex turn and is not a supported standalone project dependency.  The
native events used here reproduce the important property needed by the Roxy
registration experiment: input reaches the visible browser as OS keyboard
events rather than DOM ``fill``/``click`` calls.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


class NativeUIError(RuntimeError):
    """Raised when macOS refuses or cannot complete a UI action."""


def accessibility_trusted() -> bool:
    """当前进程是否拿到 macOS「辅助功能」授权。

    没授权时 CGEvent 合成的按键会被系统静默丢弃 —— 不报错、也不输入，
    表现为注册页停在邮箱页不动（实测 2026-09-01）。故须在跑注册前先查。
    授权归属于「负责进程」（终端/IDE 宿主 App），不是 python 可执行文件本身。
    """
    import ctypes
    import ctypes.util
    import sys

    if sys.platform != "darwin":
        return True
    try:
        path = ctypes.util.find_library("ApplicationServices")
        if not path:
            return False
        lib = ctypes.cdll.LoadLibrary(path)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        return False


def responsible_app_hint() -> str:
    """猜出需要在「隐私与安全性 → 辅助功能」里勾选的 App 名，便于给出可执行提示。"""
    import os
    import subprocess

    try:
        pid = os.getppid()
        for _ in range(6):
            out = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if not out:
                break
            ppid_s, _, comm = out.partition(" ")
            comm = comm.strip()
            if ".app/" in comm:
                # /Applications/Foo.app/Contents/MacOS/… → Foo
                head = comm.split(".app/")[0]
                return head.rsplit("/", 1)[-1] + ".app"
            try:
                pid = int(ppid_s)
            except ValueError:
                break
            if pid <= 1:
                break
    except Exception:  # noqa: BLE001
        pass
    return "你的终端/IDE App"


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class RoxyNativeUI:
    app_name: str = "RoxyChrome"
    action_delay: float = 0.12
    # When False, never call ``activate`` — keyboard/mouse events only work if
    # RoxyChrome is already the frontmost app (script will not steal focus).
    steal_focus: bool = True
    runner: Runner = subprocess.run

    def _maybe_activate(self) -> None:
        if self.steal_focus:
            self.activate()

    def is_frontmost(self) -> bool:
        result = self._osascript(
            "on run argv\n"
            "set targetApp to item 1 of argv\n"
            "tell application \"System Events\"\n"
            "return (name of first application process whose frontmost is true) is targetApp\n"
            "end tell\n"
            "end run",
            self.app_name,
        )
        return result.lower() in {"true", "1", "yes"}

    @property
    def text_helper(self) -> Path:
        return Path(__file__).resolve().parent / "tools" / "macos_type_text.swift"

    @property
    def key_helper(self) -> Path:
        return Path(__file__).resolve().parent / "tools" / "macos_press_key.swift"

    @property
    def click_helper(self) -> Path:
        return Path(__file__).resolve().parent / "tools" / "macos_click.swift"

    def _osascript(self, script: str, *args: str) -> str:
        command = ["osascript", "-e", script]
        if args:
            command.extend(["--", *args])
        run_kwargs: dict[str, Any] = dict(check=False, capture_output=True, text=True)
        if self.runner is subprocess.run:
            run_kwargs["timeout"] = 20
        try:
            result = self.runner(command, **run_kwargs)
        except subprocess.TimeoutExpired as exc:
            raise NativeUIError(f"macOS UI action timed out after 20s: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise NativeUIError(f"macOS UI action failed: {detail}")
        if self.action_delay:
            time.sleep(self.action_delay)
        return (result.stdout or "").strip()

    def _helper_command(self, source: Path) -> list[str]:
        """Return a stable compiled helper in production, Swift script in tests."""
        if self.runner is not subprocess.run:
            return ["swift", str(source)]
        cache = Path(tempfile.gettempdir()) / "outlook-roxy-native-ui"
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        binary = cache / source.stem
        if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
            result = subprocess.run(
                ["swiftc", str(source), "-o", str(binary)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown error").strip()
                raise NativeUIError(f"cannot compile macOS native helper: {detail}")
            binary.chmod(0o700)
        return [str(binary)]

    def activate(self) -> None:
        self._osascript(
            "on run argv\n"
            "tell application (item 1 of argv) to activate\n"
            "end run",
            self.app_name,
        )

    def title(self) -> str:
        return self._osascript(
            "on run argv\n"
            "set targetApp to item 1 of argv\n"
            "using terms from application \"Google Chrome\"\n"
            "tell application targetApp to return title of active tab of front window\n"
            "end using terms from\n"
            "end run",
            self.app_name,
        )

    def url(self) -> str:
        return self._osascript(
            "on run argv\n"
            "set targetApp to item 1 of argv\n"
            "using terms from application \"Google Chrome\"\n"
            "tell application targetApp to return URL of active tab of front window\n"
            "end using terms from\n"
            "end run",
            self.app_name,
        )

    def activate_tab_with_url(self, fragment: str, *, prefer_last: bool = True) -> bool:
        """Select a tab in the front window whose URL contains fragment.

        Prefer the last matching tab by default so a freshly navigated signup
        page wins over stale signup.live.com tabs left from earlier attempts.
        """
        fragment = (fragment or "").strip().lower()
        if not fragment:
            return False
        direction = "reverse" if prefer_last else "forward"
        result = self._osascript(
            "on run argv\n"
            "set targetApp to item 1 of argv\n"
            "set needle to item 2 of argv\n"
            "set direction to item 3 of argv\n"
            "using terms from application \"Google Chrome\"\n"
            "tell application targetApp\n"
            "set tabCount to count of tabs of front window\n"
            "if direction is \"reverse\" then\n"
            "repeat with i from tabCount to 1 by -1\n"
            "set tabURL to URL of tab i of front window\n"
            "if tabURL contains needle then\n"
            "set active tab index of front window to i\n"
            "return \"1\"\n"
            "end if\n"
            "end repeat\n"
            "else\n"
            "repeat with i from 1 to tabCount\n"
            "set tabURL to URL of tab i of front window\n"
            "if tabURL contains needle then\n"
            "set active tab index of front window to i\n"
            "return \"1\"\n"
            "end if\n"
            "end repeat\n"
            "end if\n"
            "end tell\n"
            "end using terms from\n"
            "return \"0\"\n"
            "end run",
            self.app_name,
            fragment,
            direction,
        )
        return result == "1"

    def open_signup_url(self, url: str, *, fragment: str = "signup.live.com/signup") -> None:
        """Navigate the freshest matching signup tab, or the active tab if none."""
        self._maybe_activate()
        self.activate_tab_with_url(fragment, prefer_last=True)
        self.navigate(url)

    def ensure_signup_tab(self, fragment: str = "signup.live.com/signup") -> str:
        """Confirm the signup tab is selected (optionally bringing Roxy forward)."""
        fragment = (fragment or "").strip().lower()
        self._maybe_activate()
        if not self.activate_tab_with_url(fragment, prefer_last=True):
            raise NativeUIError(f"找不到含 {fragment!r} 的标签页")
        url = self.url()
        if fragment not in url.lower():
            raise NativeUIError(
                f"当前标签不是 signup（url={url!r} title={self.title()!r}）"
            )
        return url

    def close_about_blank_tabs(self) -> int:
        """Close extra about:blank tabs so native keys cannot land on Untitled."""
        result = self._osascript(
            "on run argv\n"
            "set targetApp to item 1 of argv\n"
            "set closedCount to 0\n"
            "using terms from application \"Google Chrome\"\n"
            "tell application targetApp\n"
            "repeat with w in windows\n"
            "set tabCount to count of tabs of w\n"
            "repeat with i from tabCount to 1 by -1\n"
            "if tabCount > 1 then\n"
            "set tabURL to URL of tab i of w\n"
            "if tabURL is \"about:blank\" or tabURL is \"\" then\n"
            "close tab i of w\n"
            "set closedCount to closedCount + 1\n"
            "set tabCount to tabCount - 1\n"
            "end if\n"
            "end if\n"
            "end repeat\n"
            "end repeat\n"
            "end tell\n"
            "end using terms from\n"
            "return closedCount as text\n"
            "end run",
            self.app_name,
        )
        try:
            return int(result)
        except ValueError:
            return 0

    def navigate(self, url: str) -> None:
        """Navigate the active tab without typing URL punctuation.

        AppleScript ``keystroke`` follows the current keyboard layout and can
        drop characters such as ``:`` and ``/``.  Chromium's scripting
        dictionary is reliable for navigation; form fields still use native
        System Events below.
        """
        self._osascript(
            "on run argv\n"
            "set targetApp to item 1 of argv\n"
            "set targetURL to item 2 of argv\n"
            "using terms from application \"Google Chrome\"\n"
            "tell application targetApp to set URL of active tab of front window to targetURL\n"
            "end using terms from\n"
            "end run",
            self.app_name,
            url,
        )

    def click_absolute(self, x: int, y: int) -> None:
        """在屏幕绝对坐标处发一次真实鼠标点击（不做 926x768 等比换算）。

        ``click_relative`` 把 926x768 参考系按窗口尺寸等比拉伸，但浏览器顶部
        工具栏是固定高度、不随窗口等比变化，窗口一旦不是 926x768，页面元素
        y 轴就整体偏移（实测 1000x1000 窗口下生日下拉框点不中）。
        元素坐标应由 DOM ``getBoundingClientRect`` + ``window.screenX/Y`` 现算，
        再走本方法点击 —— 输入仍是真实 OS 事件，只是坐标来源换成 DOM。
        """
        if self.runner is subprocess.run:
            self._maybe_activate()
        result = self.runner(
            [*self._helper_command(self.click_helper), str(int(x)), str(int(y))],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise NativeUIError(f"native click failed at ({x},{y}): {detail}")
        if self.action_delay:
            time.sleep(self.action_delay)

    def click_relative(self, x: int, y: int) -> None:
        """Click using coordinates from the standard 926x768 Roxy capture.

        Computer Use scales the actual Chromium window into a 926x768 image.
        Convert those stable reference coordinates back to the current window
        bounds so this remains valid when Roxy is resized.
        """
        raw_bounds = self._osascript(
            "on run argv\n"
            "set targetApp to item 1 of argv\n"
            "using terms from application \"Google Chrome\"\n"
            "tell application targetApp to return bounds of front window\n"
            "end using terms from\n"
            "end run",
            self.app_name,
        )
        try:
            bounds = [int(part.strip()) for part in raw_bounds.split(",")]
            left, top, right, bottom = bounds[:4]
        except (ValueError, IndexError) as exc:
            raise NativeUIError(f"cannot parse Roxy window bounds: {raw_bounds!r}") from exc
        absolute_x = left + round(int(x) * (right - left) / 926)
        absolute_y = top + round(int(y) * (bottom - top) / 768)
        if self.runner is subprocess.run:
            self._maybe_activate()
        result = self.runner(
            [*self._helper_command(self.click_helper), str(absolute_x), str(absolute_y)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise NativeUIError(f"macOS native click failed: {detail}")
        if self.action_delay:
            time.sleep(self.action_delay)

    def press(self, key: str) -> None:
        key_codes = {
            "Return": 36,
            "Tab": 48,
            "Escape": 53,
            "Space": 49,
            "Down": 125,
            "Up": 126,
            "Home": 115,
            "End": 119,
            "Backspace": 51,
        }
        if key not in key_codes:
            raise ValueError(f"unsupported native key: {key}")
        self._press_keycode(key_codes[key])

    def _press_keycode(self, key_code: int, *, command: bool = False) -> None:
        if self.runner is subprocess.run:
            self._maybe_activate()
        args = [*self._helper_command(self.key_helper), str(int(key_code))]
        if command:
            args.append("--command")
        result = self.runner(
            args,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise NativeUIError(f"macOS native key press failed: {detail}")
        if self.action_delay:
            time.sleep(self.action_delay)

    def shortcut(self, key: str) -> None:
        key_codes = {"t": 17, "l": 37, "a": 0, "v": 9}
        if key not in key_codes:
            raise ValueError(f"unsupported native shortcut: command+{key}")
        self._press_keycode(key_codes[key], command=True)

    def type_text(self, value: str) -> None:
        if self.runner is subprocess.run:
            self._maybe_activate()
        result = self.runner(
            self._helper_command(self.text_helper),
            input=value,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise NativeUIError(f"macOS native text input failed: {detail}")
        if self.action_delay:
            time.sleep(self.action_delay)

    def press_many(self, keys: Iterable[str], *, pause: float = 0.08) -> None:
        for key in keys:
            self.press(key)
            if pause:
                time.sleep(pause)

    def open_url_in_new_tab(self, url: str) -> None:
        self._maybe_activate()
        self.shortcut("t")
        self.navigate(url)

    def wait_for_title(self, expected: Iterable[str], *, timeout: float = 45.0) -> str:
        needles = tuple(value.lower() for value in expected)
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = self.title()
            if any(value in last.lower() for value in needles):
                return last
            time.sleep(0.5)
        raise NativeUIError(f"timed out waiting for title {needles!r}; last={last!r}, url={self.url()!r}")

    def wait_for_url(self, expected: Iterable[str], *, timeout: float = 45.0) -> str:
        needles = tuple(value.lower() for value in expected)
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            last = self.url()
            if any(value in last.lower() for value in needles):
                return last
            time.sleep(0.5)
        raise NativeUIError(f"timed out waiting for URL {needles!r}; last={last!r}, title={self.title()!r}")
