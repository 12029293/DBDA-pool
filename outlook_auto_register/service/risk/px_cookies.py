from __future__ import annotations

from typing import Any, Optional

import requests

from outlook_auto_register.config.constants import DEFAULT_UA, PX_APP_ID
from outlook_auto_register.service.resource.proxy.proxy_utils import parse_proxy, timezone_for_country


def cookie_map(session: requests.Session) -> dict[str, str]:
    return {c.name: c.value for c in session.cookies}


_PX_COOKIE_NAMES = frozenset({"_px3", "_pxde", "_pxvid", "pxcts", "_pxhd", "pxhd"})


def clear_px_cookies(session: requests.Session) -> None:
    """清除 session 内 PX 相关 cookie，便于 offcaptcha 刷新挑战时重打 silent。"""
    for cookie in list(session.cookies):
        if cookie.name in _PX_COOKIE_NAMES:
            session.cookies.clear(cookie.domain, cookie.path, cookie.name)


def get_px_cookies(session: requests.Session) -> dict[str, str]:
    m = cookie_map(session)
    return {
        "px3": m.get("_px3", ""),
        "pxde": m.get("_pxde", ""),
        "pxvid": m.get("_pxvid", ""),
        "pxcts": m.get("pxcts", ""),
        "pxhd": m.get("_pxhd", "") or m.get("pxhd", ""),
    }


def apply_px_solution(session: requests.Session, solution: dict[str, Any], *, preserve_vid: str = "") -> None:
    """将打码平台返回的 token 写入 session cookie。"""
    mapping = {
        "_px3": solution.get("px3") or solution.get("_px3"),
        "_pxde": solution.get("pxde") or solution.get("_pxde"),
        "_pxvid": preserve_vid or solution.get("pxvid") or solution.get("_pxvid") or solution.get("vid"),
        "pxcts": solution.get("pxcts"),
        "_pxhd": solution.get("pxhd") or solution.get("_pxhd"),
    }
    for name, value in mapping.items():
        if value:
            session.cookies.set(name, str(value), domain=".live.com")
            session.cookies.set(name, str(value), domain=".microsoftonline.com")
            session.cookies.set(name, str(value), domain=".hsprotect.net")


def apply_captcha_run_token(solution: dict[str, Any]) -> dict[str, str]:
    """统一 captcha.run press/silent 响应格式。"""
    resp = solution.get("response") if isinstance(solution.get("response"), dict) else solution
    token = (
        solution.get("pressToken")
        or solution.get("silentToken")
        or resp.get("pressToken")
        or resp.get("silentToken")
        or solution
    )
    if not isinstance(token, dict):
        return {}
    return {
        "px3": token.get("_px3", "") or token.get("px3", ""),
        "pxde": token.get("_pxde", "") or token.get("pxde", ""),
        "pxvid": token.get("_pxvid", "") or token.get("pxvid", ""),
        "pxcts": token.get("pxcts", ""),
    }


def build_px_metadata(px: dict[str, str]) -> list[dict[str, str]]:
    return [{
        "riskProvider": "Human",
        "px3": px.get("px3", ""),
        "pxde": px.get("pxde", ""),
        "pxvid": px.get("pxvid", ""),
    }]


def bind_press_solution(
    press_px: dict[str, str],
    challenge_meta: dict[str, Any],
) -> dict[str, str]:
    """HAR：verify #2 的 metadata 与 challengeSolution 使用同一组 press token，pxvid 保持 challenge vid。"""
    stable_vid = str(challenge_meta.get("vid", "") or press_px.get("pxvid", ""))
    return {
        "px3": press_px.get("px3", ""),
        "pxde": press_px.get("pxde", ""),
        "pxvid": stable_vid,
        "pxcts": press_px.get("pxcts", ""),
    }


def build_challenge_solution(
    press_px: dict[str, str],
    challenge_meta: dict[str, Any],
    *,
    challenge_type: str = "HumanCaptcha",
) -> dict[str, str]:
    bound = bind_press_solution(press_px, challenge_meta)
    return {
        "challengeType": challenge_type,
        "px3": bound["px3"],
        "pxde": bound["pxde"],
        "pxvid": bound["pxvid"],
    }


def normalize_px_solution(solution: dict[str, Any]) -> dict[str, str]:
    resp = solution.get("response") if isinstance(solution.get("response"), dict) else {}
    if (
        "pressToken" in solution
        or "silentToken" in solution
        or "pressToken" in resp
        or "silentToken" in resp
    ):
        return apply_captcha_run_token(solution)
    return {
        "px3": solution.get("px3") or solution.get("_px3", ""),
        "pxde": solution.get("pxde") or solution.get("_pxde", ""),
        "pxvid": solution.get("pxvid") or solution.get("_pxvid") or solution.get("vid", ""),
        "pxcts": solution.get("pxcts", ""),
    }


def captcha_run_has_token(result: dict[str, Any]) -> bool:
    px = normalize_px_solution(result)
    return bool(px.get("px3"))


def cookies_header(session: requests.Session) -> str:
    return "; ".join(f"{c.name}={c.value}" for c in session.cookies)


def solver_context(
    session: requests.Session,
    *,
    page_url: str,
    uaid: str = "",
    challenge_meta: Optional[dict[str, Any]] = None,
    proxy: Optional[str] = None,
    country: str = "US",
) -> dict[str, Any]:
    px = get_px_cookies(session)
    meta = challenge_meta or {}
    challenge_vid = meta.get("vid", "")
    pcfg = parse_proxy(proxy)
    proxy_url = pcfg.url if pcfg else (proxy or "")
    return {
        "page_url": page_url,
        "uaid": uaid,
        "user_agent": DEFAULT_UA,
        "proxy": proxy_url,
        "cookies": cookie_map(session),
        "cookies_header": cookies_header(session),
        "px3": px.get("px3", ""),
        "pxde": px.get("pxde", ""),
        "pxvid": challenge_vid or px.get("pxvid", ""),
        "vid": challenge_vid,
        "pxcts": px.get("pxcts", ""),
        "uuid": meta.get("uuid", ""),
        "challenge_url": meta.get("challengeUrl", ""),
        "app_id": meta.get("appId", PX_APP_ID),
        "country": country,
        "timezone": timezone_for_country(country),
    }
