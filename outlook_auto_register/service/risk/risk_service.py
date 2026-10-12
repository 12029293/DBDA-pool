from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Optional

import requests

from outlook_auto_register.common.msa_api import (
    build_msa_create_signature,
    build_msa_risk_verify_signature,
    evaluate_experiment_assignments,
    risk_initialize,
    risk_verify,
)
from outlook_auto_register.service.registration.bootstrap_service import preload_px_challenge_assets
from outlook_auto_register.config.constants import PX_APP_ID
from outlook_auto_register.service.captcha.captcha_service import CaptchaRunTask, create_captcha_run_task, poll_captcha_run_token, solve_perimeterx
from outlook_auto_register.common.http_session import OutlookHttpSession
from outlook_auto_register.model.entity.register_models import AccountInfo, SignupSession
from outlook_auto_register.service.risk.px_collector import build_challenge_iframe_url, load_challenge_iframe, post_px_beacon, post_px_bundle, warmup_px_session
from outlook_auto_register.service.risk.px_cookies import (
    bind_press_solution,
    build_challenge_solution,
    build_px_metadata,
    clear_px_cookies,
    solver_context,
)

logger = logging.getLogger(__name__)


class RegisterRetryable(RuntimeError):
    """可通过换新住宅 IP 重试整个注册的错误基类。"""


class Verify2Failed(RegisterRetryable):
    """verify #2（PX 按住）未通过。"""


class RiskBlocked(RegisterRetryable):
    """verify #1 riskBlock（AADSTS7005106，该 IP 被风控拦截）。"""


def _is_offcaptcha_solver() -> bool:
    return os.environ.get("PX_SOLVER", "").strip().lower() in {"offcaptcha", "off"}


def _offcaptcha_user_agent(http: OutlookHttpSession) -> str:
    return (
        os.environ.get("OFFCAPTCHA_USER_AGENT", "").strip()
        or http.session.headers.get("User-Agent", "")
        or ""
    )


def load_human_sensor(http: OutlookHttpSession, ctx: SignupSession) -> None:
    url = ctx.human_sensor_url
    if not url:
        return
    logger.info("加载 humanSensorUrl…")
    http.get(url, headers={"Referer": ctx.signup_page_url})


def _solver_ctx(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    proxy: Optional[str],
    challenge_meta: Optional[dict[str, Any]] = None,
    country: str = "US",
) -> dict[str, Any]:
    out = solver_context(
        http.session,
        page_url=ctx.signup_page_url,
        uaid=ctx.px_session_id or ctx.uaid,
        challenge_meta=challenge_meta or ctx.px_challenge_meta,
        proxy=proxy or http.proxy,
        country=country,
    )
    out["app_id"] = ctx.px_app_id or str(ctx.server_data.get("sHumanAppId") or PX_APP_ID)
    out["fpt_url"] = ctx.px_fpt_url or str(
        (ctx.server_data.get("oCaptchaInfo") or {}).get("urlDfp") or ""
    )
    return out


def _ensure_captcha_run_silent_task(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    proxy: Optional[str],
    country: str = "US",
) -> Optional[CaptchaRunTask]:
    existing = ctx.captcha_run_task
    if isinstance(existing, CaptchaRunTask):
        return existing
    sctx = _solver_ctx(http, ctx, proxy=proxy, challenge_meta=None, country=country)
    task = create_captcha_run_task(sctx)
    if task:
        ctx.captcha_run_task = task
    return task


def _poll_captcha_run_press(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    challenge_meta: dict[str, Any],
) -> dict[str, str]:
    """官方文档：verify#1 同一 taskId 上 GET press（须已 GET silent）。"""
    task = ctx.captcha_run_task
    if not isinstance(task, CaptchaRunTask):
        raise RuntimeError("captcha.run 须先在 verify#1 前 POST 建 task 并 GET silent")

    stable_vid = str(challenge_meta.get("vid", ""))
    if not task.silent_fetched:
        logger.info("captcha.run press 前补拉 silent（同 task=%s）", task.task_id)
        poll_captcha_run_token(task, "silent")

    logger.info(
        "captcha.run GET press task=%s challenge_uuid=%s vid=%s",
        task.task_id,
        challenge_meta.get("uuid", ""),
        challenge_meta.get("vid", ""),
    )
    solved = poll_captcha_run_token(task, "press", warmup_silent_before_press=False)
    if not solved or not solved.get("px3"):
        raise RuntimeError(
            f"captcha.run press 未返回 pressToken（task={task.task_id}），"
            "请核对代理与官方文档：同 task 先 silent 后 press"
        )
    px3 = solved.get("px3", "")
    if ":1000:" not in px3:
        logger.warning("pressToken px3 无 :1000: 段（HAR verify#2 成功样本均含 :1000:）")
    return http.apply_px_tokens(solved, preserve_vid=stable_vid)


def _registration_session_id(ctx: SignupSession) -> str:
    sid = ctx.uaid
    if len(sid) == 32 and "-" not in sid:
        sid = f"{sid[:8]}-{sid[8:12]}-{sid[12:16]}-{sid[16:20]}-{sid[20:]}"
    return sid


def _registration_iframe_url(ctx: SignupSession) -> str:
    return (
        f"https://iframe.hsprotect.net/index.html"
        f"?app_id={PX_APP_ID}&session_id={_registration_session_id(ctx)}"
    )


def _px_preseed_cookies(http: OutlookHttpSession) -> list[dict[str, str]]:
    """把 HTTP 注册会话 cookie 预置进浏览器，使 press 与 verify#2 同一 PX 上下文。"""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for c in http.session.cookies:
        name = getattr(c, "name", "") or ""
        if not name:
            continue
        domain = (getattr(c, "domain", "") or "").lower()
        keep = (
            name.startswith("_px")
            or name in ("pxcts", "pxhd", "_pxhd", "amsc", "mkt", "MUID", "fptctx2")
            or "live.com" in domain
            or "microsoft" in domain
            or "hsprotect" in domain
        )
        if not keep:
            continue
        dom = getattr(c, "domain", "") or ".live.com"
        dom = dom if dom.startswith(".") else f".{dom.lstrip('.')}"
        path = getattr(c, "path", None) or "/"
        key = (name, dom, path)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name,
            "value": str(getattr(c, "value", "") or ""),
            "domain": dom,
            "path": path,
        })
    return out


def _solve_via_swiftshader(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    phase: str,
    proxy: Optional[str],
    challenge_meta: Optional[dict[str, Any]] = None,
    stable_vid: str = "",
) -> dict[str, str]:
    """自建 SwiftShader 浏览器 + xdotool/CDP 真按，不依赖 captcha.run。"""
    from outlook_auto_register.service.px_solver.px_swiftshader_solver import harvest

    p = proxy or http.proxy
    meta = challenge_meta or ctx.px_challenge_meta or {}
    preseed = _px_preseed_cookies(http)
    want_press = phase == "press"
    logger.info(
        "PX 走 SwiftShader 本地收割 phase=%s proxy=%s preseed=%d",
        phase, str(p)[:40], len(preseed),
    )

    if not want_press:
        px = http.px_cookies()
        if px.get("px3"):
            logger.info("SwiftShader silent 复用 HTTP 会话 _px3，跳过独立浏览器收割")
            return px
        logger.warning("HTTP 无 _px3，SwiftShader silent 定向注册 iframe（parent 策略，不走独立 signup 驱动）")

    if want_press and meta:
        challenge_url = build_challenge_iframe_url(ctx, meta)
        prev_target = os.environ.get("PX_SWIFTSHADER_TARGET")
        os.environ["PX_SWIFTSHADER_TARGET"] = "parent"
        try:
            sol = harvest(
                p,
                want_press=True,
                challenge_url=challenge_url,
                session_id=str(meta.get("sessionId") or meta.get("session_id") or ctx.uaid),
                vid=str(meta.get("vid", "") or stable_vid),
                uuid=str(meta.get("uuid", "")),
                app_id=str(meta.get("appId") or meta.get("app_id") or ""),
                preseed_cookies=preseed or None,
                signup_page_url=ctx.signup_page_url,
            )
        finally:
            if prev_target is None:
                os.environ.pop("PX_SWIFTSHADER_TARGET", None)
            else:
                os.environ["PX_SWIFTSHADER_TARGET"] = prev_target
    else:
        iframe_url = _registration_iframe_url(ctx)
        prev_target = os.environ.get("PX_SWIFTSHADER_TARGET")
        os.environ["PX_SWIFTSHADER_TARGET"] = "parent"
        try:
            sol = harvest(
                p,
                want_press=False,
                challenge_url=iframe_url,
                session_id=_registration_session_id(ctx),
                preseed_cookies=preseed or None,
                signup_page_url=ctx.signup_page_url,
            )
        finally:
            if prev_target is None:
                os.environ.pop("PX_SWIFTSHADER_TARGET", None)
            else:
                os.environ["PX_SWIFTSHADER_TARGET"] = prev_target

    if not sol.get("px3"):
        raise RuntimeError(f"SwiftShader 未收割到 _px3 phase={phase}")
    px3 = sol["px3"]
    challenge_vid = str(meta.get("vid", "") or stable_vid) if want_press else ""
    logger.info(
        "SwiftShader 收割成功 px3=%s... pressed=%s has_1000=%s press_vid_match=%s backend=%s",
        px3[:30], sol.get("pressed"), ":1000:" in px3,
        sol.get("press_vid_match_reg"), sol.get("press_backend", ""),
    )
    if want_press:
        if not sol.get("pressed"):
            allow_silent = os.environ.get("PX_ALLOW_SILENT_PRESS", "").strip().lower() in {
                "1", "true", "yes",
            }
            if not allow_silent:
                raise RuntimeError(
                    "SwiftShader press 未成功（pressed=False）；"
                    "未出现可见 #px-captcha 或按压未生效（:1000: 仅为 PBKDF2 迭代数，非 press 标记）。"
                    "试 PX_SWIFTSHADER_HEADFUL=1 + PX_OS_PRESS=1，或换 IP 后重试"
                )
            logger.warning("PX_ALLOW_SILENT_PRESS=1：pressed=False 仍提交（易 riskBlock）")
        last_pv = str(sol.get("last_press_vid", ""))
        if challenge_vid and last_pv and last_pv != challenge_vid:
            if sol.get("pressed") and ":1000:" in px3:
                logger.warning(
                    "SwiftShader press collector vid=%s != challenge vid=%s；"
                    "仍用 challenge vid 提交 verify#2",
                    last_pv[:24], challenge_vid[:24],
                )
            else:
                raise RuntimeError(
                    f"SwiftShader press collector vid={last_pv[:24]} != challenge vid={challenge_vid[:24]}"
                )
        if challenge_vid and sol.get("press_vid_match_reg") is False and last_pv:
            raise RuntimeError(
                f"SwiftShader press_vid 与 challenge 不匹配 challenge={challenge_vid[:24]}"
            )
        sr = sol.get("solve_result")
        strict_sr = os.environ.get("PX_STRICT_SOLVE_RESULT", "1").strip().lower() not in {
            "0", "false", "no",
        }
        if strict_sr and sr is not None and str(sr) not in {"0", "0.0"}:
            raise RuntimeError(f"SwiftShader press solve_result={sr}（期望 0）")
    preserve = challenge_vid or stable_vid or str(sol.get("pxvid", "") or "")
    return http.apply_px_tokens(
        {
            "px3": px3,
            "pxde": sol.get("pxde", ""),
            "pxvid": sol.get("pxvid", ""),
            "pxcts": sol.get("pxcts", ""),
        },
        preserve_vid=preserve,
    )


def _px_preseed_cookies(http: OutlookHttpSession) -> list[dict[str, str]]:
    """把 HTTP 注册会话 cookie 预置进浏览器，使 press 与 verify#2 同一 PX 上下文。"""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for c in http.session.cookies:
        name = getattr(c, "name", "") or ""
        if not name:
            continue
        domain = (getattr(c, "domain", "") or "").lower()
        keep = (
            name.startswith("_px")
            or name in ("pxcts", "pxhd", "_pxhd", "amsc", "mkt", "MUID", "fptctx2")
            or "live.com" in domain
            or "microsoft" in domain
            or "hsprotect" in domain
        )
        if not keep:
            continue
        dom = getattr(c, "domain", "") or ".live.com"
        dom = dom if dom.startswith(".") else f".{dom.lstrip('.')}"
        path = getattr(c, "path", None) or "/"
        key = (name, dom, path)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name,
            "value": str(getattr(c, "value", "") or ""),
            "domain": dom,
            "path": path,
        })
    return out


def _solve_via_swiftshader(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    phase: str,
    proxy: Optional[str],
    challenge_meta: Optional[dict[str, Any]] = None,
    stable_vid: str = "",
) -> dict[str, str]:
    """自建 SwiftShader 浏览器 + xdotool/CDP 真按，不依赖 captcha.run。"""
    from outlook_auto_register.service.px_solver.px_swiftshader_solver import harvest

    p = proxy or http.proxy
    meta = challenge_meta or ctx.px_challenge_meta or {}
    preseed = _px_preseed_cookies(http)
    want_press = phase == "press"
    logger.info(
        "PX 走 SwiftShader 本地收割 phase=%s proxy=%s preseed=%d",
        phase, str(p)[:40], len(preseed),
    )

    if not want_press:
        px = http.px_cookies()
        if px.get("px3"):
            logger.info("SwiftShader silent 复用 HTTP 会话 _px3，跳过独立浏览器收割")
            return px
        logger.warning("HTTP 无 _px3，SwiftShader silent 回退浏览器（可能与注册会话脱节）")

    if want_press and meta:
        challenge_url = build_challenge_iframe_url(ctx, meta)
        sol = harvest(
            p,
            want_press=True,
            challenge_url=challenge_url,
            session_id=str(meta.get("sessionId") or meta.get("session_id") or ctx.uaid),
            vid=str(meta.get("vid", "") or stable_vid),
            uuid=str(meta.get("uuid", "")),
            app_id=str(meta.get("appId") or meta.get("app_id") or ""),
            preseed_cookies=preseed or None,
            signup_page_url=ctx.signup_page_url,
        )
    else:
        sol = harvest(p, want_press=False, preseed_cookies=preseed or None)

    if not sol.get("px3"):
        raise RuntimeError(f"SwiftShader 未收割到 _px3 phase={phase}")
    px3 = sol["px3"]
    challenge_vid = str(meta.get("vid", "") or stable_vid) if want_press else ""
    logger.info(
        "SwiftShader 收割成功 px3=%s... pressed=%s has_1000=%s press_vid_match=%s backend=%s",
        px3[:30], sol.get("pressed"), ":1000:" in px3,
        sol.get("press_vid_match_reg"), sol.get("press_backend", ""),
    )
    if want_press:
        if not sol.get("pressed"):
            allow_silent = os.environ.get("PX_ALLOW_SILENT_PRESS", "").strip().lower() in {
                "1", "true", "yes",
            }
            if not allow_silent:
                raise RuntimeError(
                    "SwiftShader press 未成功（pressed=False）；"
                    "未出现可见 #px-captcha 或按压未生效（:1000: 仅为 PBKDF2 迭代数，非 press 标记）。"
                    "试 PX_SWIFTSHADER_HEADFUL=1 + PX_OS_PRESS=1，或换 IP 后重试"
                )
            logger.warning("PX_ALLOW_SILENT_PRESS=1：pressed=False 仍提交（易 riskBlock）")
        last_pv = str(sol.get("last_press_vid", ""))
        if challenge_vid and last_pv and last_pv != challenge_vid:
            raise RuntimeError(
                f"SwiftShader press collector vid={last_pv[:24]} != challenge vid={challenge_vid[:24]}"
            )
        if challenge_vid and sol.get("press_vid_match_reg") is False and last_pv:
            raise RuntimeError(
                f"SwiftShader press_vid 与 challenge 不匹配 challenge={challenge_vid[:24]}"
            )
        sr = sol.get("solve_result")
        strict_sr = os.environ.get("PX_STRICT_SOLVE_RESULT", "1").strip().lower() not in {
            "0", "false", "no",
        }
        if strict_sr and sr is not None and str(sr) not in {"0", "0.0"}:
            raise RuntimeError(f"SwiftShader press solve_result={sr}（期望 0）")
    preserve = challenge_vid or stable_vid or str(sol.get("pxvid", "") or "")
    return http.apply_px_tokens(
        {
            "px3": px3,
            "pxde": sol.get("pxde", ""),
            "pxvid": sol.get("pxvid", ""),
            "pxcts": sol.get("pxcts", ""),
        },
        preserve_vid=preserve,
    )


def _px_preseed_cookies(http: OutlookHttpSession) -> list[dict[str, str]]:
    """把 HTTP 注册会话 cookie 预置进浏览器，使 press 与 verify#2 同一 PX 上下文。"""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for c in http.session.cookies:
        name = getattr(c, "name", "") or ""
        if not name:
            continue
        domain = (getattr(c, "domain", "") or "").lower()
        keep = (
            name.startswith("_px")
            or name in ("pxcts", "pxhd", "_pxhd", "amsc", "mkt", "MUID", "fptctx2")
            or "live.com" in domain
            or "microsoft" in domain
            or "hsprotect" in domain
        )
        if not keep:
            continue
        dom = getattr(c, "domain", "") or ".live.com"
        dom = dom if dom.startswith(".") else f".{dom.lstrip('.')}"
        path = getattr(c, "path", None) or "/"
        key = (name, dom, path)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name,
            "value": str(getattr(c, "value", "") or ""),
            "domain": dom,
            "path": path,
        })
    return out


def _solve_via_swiftshader(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    phase: str,
    proxy: Optional[str],
    challenge_meta: Optional[dict[str, Any]] = None,
    stable_vid: str = "",
) -> dict[str, str]:
    """自建 SwiftShader 浏览器 + xdotool/CDP 真按，不依赖 captcha.run。"""
    from outlook_auto_register.service.px_solver.px_swiftshader_solver import harvest

    p = proxy or http.proxy
    meta = challenge_meta or ctx.px_challenge_meta or {}
    preseed = _px_preseed_cookies(http)
    want_press = phase == "press"
    logger.info(
        "PX 走 SwiftShader 本地收割 phase=%s proxy=%s preseed=%d",
        phase, str(p)[:40], len(preseed),
    )

    if not want_press:
        px = http.px_cookies()
        if px.get("px3"):
            logger.info("SwiftShader silent 复用 HTTP 会话 _px3，跳过独立浏览器收割")
            return px
        logger.warning("HTTP 无 _px3，SwiftShader silent 回退浏览器（可能与注册会话脱节）")

    if want_press and meta:
        challenge_url = build_challenge_iframe_url(ctx, meta)
        sol = harvest(
            p,
            want_press=True,
            challenge_url=challenge_url,
            session_id=str(meta.get("sessionId") or meta.get("session_id") or ctx.uaid),
            vid=str(meta.get("vid", "") or stable_vid),
            uuid=str(meta.get("uuid", "")),
            app_id=str(meta.get("appId") or meta.get("app_id") or ""),
            preseed_cookies=preseed or None,
            signup_page_url=ctx.signup_page_url,
        )
    else:
        sol = harvest(p, want_press=False, preseed_cookies=preseed or None)

    if not sol.get("px3"):
        raise RuntimeError(f"SwiftShader 未收割到 _px3 phase={phase}")
    px3 = sol["px3"]
    challenge_vid = str(meta.get("vid", "") or stable_vid) if want_press else ""
    logger.info(
        "SwiftShader 收割成功 px3=%s... pressed=%s has_1000=%s press_vid_match=%s backend=%s",
        px3[:30], sol.get("pressed"), ":1000:" in px3,
        sol.get("press_vid_match_reg"), sol.get("press_backend", ""),
    )
    if want_press:
        if not sol.get("pressed"):
            allow_silent = os.environ.get("PX_ALLOW_SILENT_PRESS", "").strip().lower() in {
                "1", "true", "yes",
            }
            if not allow_silent:
                raise RuntimeError(
                    "SwiftShader press 未成功（pressed=False）；"
                    "未出现可见 #px-captcha 或按压未生效（:1000: 仅为 PBKDF2 迭代数，非 press 标记）。"
                    "试 PX_SWIFTSHADER_HEADFUL=1 + PX_OS_PRESS=1，或换 IP 后重试"
                )
            logger.warning("PX_ALLOW_SILENT_PRESS=1：pressed=False 仍提交（易 riskBlock）")
        last_pv = str(sol.get("last_press_vid", ""))
        if challenge_vid and last_pv and last_pv != challenge_vid:
            raise RuntimeError(
                f"SwiftShader press collector vid={last_pv[:24]} != challenge vid={challenge_vid[:24]}"
            )
        if challenge_vid and sol.get("press_vid_match_reg") is False and last_pv:
            raise RuntimeError(
                f"SwiftShader press_vid 与 challenge 不匹配 challenge={challenge_vid[:24]}"
            )
        sr = sol.get("solve_result")
        strict_sr = os.environ.get("PX_STRICT_SOLVE_RESULT", "1").strip().lower() not in {
            "0", "false", "no",
        }
        if strict_sr and sr is not None and str(sr) not in {"0", "0.0"}:
            raise RuntimeError(f"SwiftShader press solve_result={sr}（期望 0）")
    preserve = challenge_vid or stable_vid or str(sol.get("pxvid", "") or "")
    return http.apply_px_tokens(
        {
            "px3": px3,
            "pxde": sol.get("pxde", ""),
            "pxvid": sol.get("pxvid", ""),
            "pxcts": sol.get("pxcts", ""),
        },
        preserve_vid=preserve,
    )


def _solve_via_bitbrowser(
    http: OutlookHttpSession,
    *,
    phase: str,
    proxy: Optional[str],
    stable_vid: str = "",
) -> dict[str, str]:
    """用比特指纹浏览器收割 _px3（与注册同 IP），替代 captcha.run。"""
    from outlook_auto_register.service.px_solver.bit_px_solver import harvest

    p = proxy or http.proxy
    logger.info("PX 走比特浏览器收割 phase=%s（同 IP=%s）", phase, str(p)[:40])
    sol = harvest(p, want_press=(phase == "press"))
    if not sol.get("px3"):
        raise RuntimeError(f"比特浏览器未收割到 _px3 phase={phase}")
    logger.info("比特收割成功 px3=%s... pressed=%s", sol["px3"][:30], sol.get("pressed"))
    if phase == "press" and not sol.get("pressed"):
        allow_silent = os.environ.get("PX_ALLOW_SILENT_PRESS", "").strip().lower() in {
            "1", "true", "yes",
        }
        if not allow_silent:
            raise RuntimeError("比特浏览器 press 未成功（pressed=False）")
    preserve = stable_vid or str(sol.get("pxvid", "") or "")
    return http.apply_px_tokens(
        {"px3": sol["px3"], "pxde": sol.get("pxde", ""), "pxvid": sol.get("pxvid", "")},
        preserve_vid=preserve,
    )


def _apply_offcaptcha_cookies(http: OutlookHttpSession, cookies: Any) -> None:
    """Install OffCaptcha cookies from either a map or JSON cookie records."""
    records: list[dict[str, Any]] = []
    if isinstance(cookies, dict):
        records = [
            {"name": str(name), "value": value}
            for name, value in cookies.items()
        ]
    elif isinstance(cookies, list):
        for item in cookies:
            record: Any = item
            if isinstance(item, str):
                try:
                    record = json.loads(item)
                except json.JSONDecodeError:
                    continue
            if isinstance(record, dict):
                records.append(record)

    for record in records:
        name = str(record.get("name") or "")
        value = record.get("value")
        if not name or value is None:
            continue
        domain = str(record.get("domain") or "")
        path = str(record.get("path") or "/")
        if domain:
            http.session.cookies.set(name, str(value), domain=domain, path=path)
            continue
        for fallback_domain in (".live.com", ".microsoftonline.com", ".hsprotect.net"):
            http.session.cookies.set(
                name, str(value), domain=fallback_domain, path=path,
            )


def _solve_via_offcaptcha(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    phase: str,
    proxy: Optional[str],
    challenge_meta: Optional[dict[str, Any]] = None,
    stable_vid: str = "",
) -> dict[str, str]:
    """offcaptcha.com：silent=PXCaptchaInvisible，press=PXCaptchaPressAndHold。"""
    from outlook_auto_register.service.captcha import offcaptcha_service as offcaptcha

    p = proxy or http.proxy
    meta = challenge_meta or ctx.px_challenge_meta or {}
    session_id = ctx.px_session_id or _registration_session_id(ctx)
    page_url = ctx.signup_page_url or "https://signup.live.com/"
    website_key = ctx.px_app_id or str(ctx.server_data.get("sHumanAppId") or PX_APP_ID)
    ua = _offcaptcha_user_agent(http)

    if phase == "silent":
        captcha_info = ctx.server_data.get("oCaptchaInfo") or {}
        fpt = (
            ctx.px_fpt_url
            or str(captcha_info.get("urlDfp") or "")
            or ctx.human_sensor_url
            or _registration_iframe_url(ctx)
        )
        logger.info("PX 走 offcaptcha invisible session=%s fpt=%s", session_id[:24], fpt[:80])
        sol = offcaptcha.solve_invisible(
            website_url=page_url,
            session_id=session_id,
            fpt_url=fpt,
            proxy=p,
            website_key=website_key,
            user_agent=ua,
        )
        _apply_offcaptcha_cookies(http, sol.get("cookies"))
        if sol.get("userAgent") and os.environ.get("OFFCAPTCHA_APPLY_UA", "").strip() in {"1", "true", "yes"}:
            http.session.headers["User-Agent"] = sol["userAgent"]
        logger.info("offcaptcha silent px3=%s...", sol["px3"][:30])
        return http.apply_px_tokens(sol, preserve_vid=sol.get("pxvid", ""))

    challenge_url = build_challenge_iframe_url(ctx, meta)
    # 官方 Microsoft 示例把 targetURL 指到 risk/verify；iframe 作为 data.iframeURL
    prefer = (os.environ.get("OFFCAPTCHA_PRESS_TARGET") or "verify").strip().lower()
    if prefer in {"iframe", "challenge"}:
        target = challenge_url or offcaptcha.default_press_target()
    else:
        target = ctx.px_press_target or offcaptcha.default_press_target()
    uuid = str(meta.get("uuid", ""))
    vid = str(meta.get("vid", "") or stable_vid)
    if not uuid or not vid:
        raise RuntimeError("offcaptcha press 需要 challengeMetadata.uuid 与 vid")
    logger.info("PX 走 offcaptcha press uuid=%s vid=%s target=%s iframe=%s", uuid[:24], vid[:24], target[:80], challenge_url[:60])
    sol = offcaptcha.solve_press(
        website_url=page_url,
        session_id=session_id,
        target_url=target,
        uuid=uuid,
        vid=vid,
        proxy=p,
        website_key=website_key,
        user_agent=ua,
        iframe_url=challenge_url,
    )
    logger.info("offcaptcha press px3=%s... vid=%s", sol["px3"][:30], sol.get("pxvid", "")[:24])
    return http.apply_px_tokens(sol, preserve_vid=vid or sol.get("pxvid", ""))


def _solve_px_protocol(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    phase: str,
    proxy: Optional[str],
    challenge_meta: Optional[dict[str, Any]] = None,
    country: str = "US",
) -> dict[str, str]:
    prefer = "silent" if phase == "silent" else "press"
    logger.info("纯协议打码 phase=%s prefer=%s", phase, prefer)

    solver = os.environ.get("PX_SOLVER", "").strip().lower()
    _meta = challenge_meta or ctx.px_challenge_meta
    _vid = str(_meta.get("vid", "")) if phase == "press" else ""

    if solver in {"offcaptcha", "off"}:
        return _solve_via_offcaptcha(
            http, ctx, phase=phase, proxy=proxy,
            challenge_meta=_meta, stable_vid=_vid,
        )
    if solver in {"offcaptcha", "off"}:
        return _solve_via_offcaptcha(
            http, ctx, phase=phase, proxy=proxy,
            challenge_meta=_meta, stable_vid=_vid,
        )
    if solver in {"offcaptcha", "off"}:
        return _solve_via_offcaptcha(
            http, ctx, phase=phase, proxy=proxy,
            challenge_meta=_meta, stable_vid=_vid,
        )
    if solver == "bitbrowser":
        return _solve_via_bitbrowser(http, phase=phase, proxy=proxy, stable_vid=_vid)
    if solver in {"swiftshader", "local", "self"}:
        return _solve_via_swiftshader(
            http, ctx, phase=phase, proxy=proxy,
            challenge_meta=_meta, stable_vid=_vid,
        )

    meta = challenge_meta or ctx.px_challenge_meta
    stable_vid = str(meta.get("vid", "")) if phase == "press" else ""
    sctx = _solver_ctx(http, ctx, proxy=proxy, challenge_meta=meta, country=country)

    if phase == "silent":
        task = _ensure_captcha_run_silent_task(
            http, ctx, proxy=proxy, country=country,
        )
        if task:
            solved = poll_captcha_run_token(task, "silent")
            if solved and solved.get("px3"):
                logger.info("captcha.run silent 成功（task=%s）", task.task_id)
                return http.apply_px_tokens(solved, preserve_vid=stable_vid)
    elif phase == "press":
        try:
            return _poll_captcha_run_press(
                http, ctx, challenge_meta=meta,
            )
        except RuntimeError as exc:
            fallback = os.environ.get("PX_PRESS_FALLBACK", "").strip().lower()
            if fallback not in {"1", "true", "ez", "ezcaptcha", "capsolver", "auto"}:
                raise
            logger.warning("captcha.run press 失败，走 PX_PRESS_FALLBACK=%s: %s", fallback, exc)

    if phase == "silent":
        solved = solve_perimeterx(sctx, prefer_mode="silent")
    else:
        solved = solve_perimeterx(sctx, prefer_mode="press")
        if not solved or not solved.get("px3"):
            raise RuntimeError(
                "press 打码失败（captcha.run + fallback）。"
                "设 PX_PRESS_FALLBACK=ezcaptcha 可启用 EzCaptcha/CapSolver"
            )
    if not solved or not solved.get("px3"):
        raise RuntimeError(f"纯协议打码失败 phase={phase}，请检查 CAPTCHA_RUN_API_KEY / 代理 / 余额")
    return http.apply_px_tokens(solved, preserve_vid=stable_vid)


def _acquire_silent_px(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    mode: str,
    proxy: Optional[str],
    country: str,
    force_fresh: bool = False,
) -> dict[str, str]:
    """risk/verify #1 的 silent px：纯协议 captcha.run silent。"""
    px = http.px_cookies()
    if px.get("px3") and not force_fresh:
        logger.info("silent px 复用已有 cookie")
        return px

    return _solve_px_protocol(http, ctx, phase="silent", proxy=proxy, country=country)


def _verify2(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    challenge_meta: dict[str, Any],
    px: dict[str, str],
    challenge_type: str,
) -> dict[str, Any]:
    bound = bind_press_solution(px, challenge_meta)
    return risk_verify(
        http, ctx,
        continuation_token=ctx.continuation_token,
        risk_provider_metadata=build_px_metadata(bound),
        challenge_solution=build_challenge_solution(
            px, challenge_meta, challenge_type=challenge_type,
        ),
    )


def _refresh_press_challenge(
    http: OutlookHttpSession,
    ctx: SignupSession,
    account: AccountInfo,
    proxy: Optional[str],
    *,
    prev_meta: Optional[dict[str, Any]] = None,
) -> Optional[tuple[dict[str, Any], str]]:
    """press 打码失败后重新走 risk 链拿全新挑战（旧 uuid/vid 已过期，原地重打必 SESSION_EXPIRED）。"""
    force_fresh = _is_offcaptcha_solver()
    if force_fresh:
        clear_px_cookies(http.session)

    def _pull_challenge(*, fresh_silent: bool) -> Optional[tuple[dict[str, Any], str]]:
        risk_initialize(http, ctx, "")
        if not ctx.continuation_token:
            logger.warning("刷新挑战：risk/initialize 未返回 continuationToken")
            return None
        if ctx.human_sensor_url:
            load_human_sensor(http, ctx)
        px_meta = _acquire_silent_px(
            http, ctx, mode="solver", proxy=proxy, country=account.country,
            force_fresh=fresh_silent,
        )
        signature = build_msa_risk_verify_signature(account, ctx)
        resp1 = risk_verify(
            http, ctx,
            continuation_token=ctx.continuation_token,
            risk_provider_metadata=build_px_metadata(px_meta),
            msa_risk_verify_signature=signature,
        )
        state = resp1.get("state", "")
        logger.info("刷新挑战 risk/verify #1 state=%s", state)
        if state != "riskChallengeRequired":
            return None
        challenge = resp1.get("challengeDetails", {})
        meta = challenge.get("challengeMetadata", {}) or {}
        ctype = challenge.get("challengeType", "HumanCaptcha")
        ctx.px_challenge_meta = meta
        logger.info(
            "刷新挑战成功 uuid=%s vid=%s",
            str(meta.get("uuid", ""))[:24], str(meta.get("vid", ""))[:24],
        )
        return meta, ctype

    try:
        out = _pull_challenge(fresh_silent=force_fresh)
        if not out:
            return None
        meta, ctype = out
        prev_uuid = str((prev_meta or {}).get("uuid", ""))
        if force_fresh and prev_uuid and str(meta.get("uuid", "")) == prev_uuid:
            logger.warning(
                "刷新后 uuid 未变 (%s)，清 px cookie 并重打 silent",
                prev_uuid[:24],
            )
            clear_px_cookies(http.session)
            out = _pull_challenge(fresh_silent=True)
            if not out:
                return None
            meta, ctype = out
        if force_fresh:
            load_challenge_iframe(http, ctx, meta)
        return meta, ctype
    except Exception as exc:
        logger.warning("刷新挑战异常: %s", exc)
        return None

def _protocol_verify2(
    http: OutlookHttpSession,
    ctx: SignupSession,
    account: AccountInfo,
    *,
    proxy: Optional[str],
    challenge_meta: dict[str, Any],
    challenge_type: str,
) -> bool:
    """纯协议 verify #2：press 打码后提交 challengeSolution。

    captcha.run：单 task 重复取 press 多为同一 token，默认只 1 次。
    SwiftShader 本地：verify#2 可能返回新 challengeMetadata，允许多轮重收割。
    """
    solver = os.environ.get("PX_SOLVER", "").strip().lower()
    max_attempts = 3 if solver in {"swiftshader", "local", "self", "offcaptcha", "off"} else 1
    meta = challenge_meta
    for attempt in range(1, max_attempts + 1):
        try:
            challenge_px = _solve_px_protocol(
                http, ctx, phase="press", proxy=proxy, challenge_meta=meta,
                country=account.country,
            )
            logger.info(
                "verify #2 attempt=%s px3[:1000:]=%s vid=%s challenge_vid=%s",
                attempt, ":1000:" in challenge_px.get("px3", ""),
                challenge_px.get("pxvid", "")[:16], str(meta.get("vid", ""))[:16],
            )
            resp2 = _verify2_with_retry(
                http, ctx,
                challenge_meta=meta,
                px=challenge_px,
                challenge_type=challenge_type,
            )
            state = resp2.get("state", "")
            logger.info("纯协议 verify #2 attempt=%s state=%s", attempt, state)
            if state == "continue":
                return True
            logger.debug("verify #2 body keys=%s", list(resp2.keys()))
            if resp2.get("continuationToken"):
                ctx.continuation_token = resp2["continuationToken"]
            nxt = resp2.get("challengeDetails", {}).get("challengeMetadata", {})
            if nxt:
                meta = nxt
                ctx.px_challenge_meta = meta
                continue
            # verify #2 没给新挑战且未 continue → 挑战可能已过期，刷新后再打
            if attempt < max_attempts:
                refreshed = _refresh_press_challenge(
                    http, ctx, account, proxy, prev_meta=meta,
                )
                if refreshed:
                    meta, challenge_type = refreshed
                    continue
        except RuntimeError as exc:
            logger.warning("纯协议 press 打码失败 attempt=%s: %s", attempt, exc)
            # 旧挑战 uuid/vid 已过期（重打必 SESSION_EXPIRED），刷新挑战后再试
            if attempt < max_attempts:
                refreshed = _refresh_press_challenge(
                    http, ctx, account, proxy, prev_meta=meta,
                )
                if refreshed:
                    meta, challenge_type = refreshed
                    time.sleep(0.5 if _is_offcaptcha_solver() else 1.0)
                    continue
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 403:
                logger.error("纯协议 verify #2 403 riskBlock")
                return False
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            logger.warning("纯协议 verify #2 网络异常 attempt=%s: %s", attempt, exc)
        time.sleep(2.0)
    return False


def _verify2_with_retry(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    challenge_meta: dict[str, Any],
    px: dict[str, str],
    challenge_type: str,
    retries: int = 3,
) -> dict[str, Any]:
    last_exc: Optional[Exception] = None
    for i in range(1, retries + 1):
        try:
            return _verify2(
                http, ctx,
                challenge_meta=challenge_meta,
                px=px,
                challenge_type=challenge_type,
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            logger.warning("verify #2 请求超时/断连 retry=%s/%s: %s", i, retries, exc)
            time.sleep(3.0)
    if last_exc:
        raise last_exc
    raise RuntimeError("verify #2 重试耗尽")


def solve_risk_challenge(
    http: OutlookHttpSession,
    ctx: SignupSession,
    account: AccountInfo,
    *,
    mode: str = "solver",
    proxy: Optional[str] = None,
) -> None:
    """风控链（纯协议 only）：silent 打码 → verify #1 → press 打码 → verify #2。"""
    init = risk_initialize(http, ctx, "")
    logger.info("risk/initialize state=%s", init.get("state"))

    if ctx.human_sensor_url:
        load_human_sensor(http, ctx)

    if not ctx.continuation_token:
        raise RuntimeError("risk/initialize 未返回 continuationToken")

    px_meta = _acquire_silent_px(http, ctx, mode=mode, proxy=proxy, country=account.country)

    signature = build_msa_risk_verify_signature(account, ctx)
    try:
        resp1 = risk_verify(
            http, ctx,
            continuation_token=ctx.continuation_token,
            risk_provider_metadata=build_px_metadata(px_meta),
            msa_risk_verify_signature=signature,
        )
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 403:
            body = ""
            try:
                body = exc.response.text[:200]
            except Exception:  # noqa: BLE001
                pass
            # riskBlock：该住宅 IP 被微软拦截，同 IP 重试无意义，换新 IP 重试整个注册
            raise RiskBlocked(f"verify #1 riskBlock（该 IP 被拦，换新 IP 重试）: {body}") from exc
        raise

    state = resp1.get("state", "")
    logger.info("risk/verify #1 state=%s", state)
    if state == "continue":
        return
    if state != "riskChallengeRequired":
        raise RuntimeError(f"risk/verify #1 未预期状态: {state}")

    challenge = resp1.get("challengeDetails", {})
    challenge_meta = challenge.get("challengeMetadata", {}) or {}
    challenge_type = challenge.get("challengeType", "HumanCaptcha")
    ctx.px_challenge_meta = challenge_meta

    if _is_offcaptcha_solver():
        # OffCaptcha 自建浏览器解 press；协议侧跳过慢速资源预载，尽快提交打码任务
        load_challenge_iframe(http, ctx, challenge_meta)
        post_px_beacon(http, ctx, tag="pre-press")
    else:
        load_challenge_iframe(http, ctx, challenge_meta)
        preload_px_challenge_assets(http, ctx, challenge_meta)
        warmup_px_session(http, ctx)
        time.sleep(1.0)
        post_px_beacon(http, ctx, tag="pre-press")
        post_px_bundle(http, ctx, tag="pre-press")

    if _protocol_verify2(
        http, ctx, account,
        proxy=proxy,
        challenge_meta=challenge_meta,
        challenge_type=challenge_type,
    ):
        return
    raise Verify2Failed("risk/verify #2 未通过（PX press 打码未通过）")


# ---------------------------------------------------------------------------
# z-style pre-CreateAccount orchestration
# ---------------------------------------------------------------------------


def prepare_z_style_silent(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    mode: str,
    proxy: Optional[str],
    country: str,
) -> dict[str, str]:
    """Run risk initialization, experiments and silent PX before name check."""
    init = risk_initialize(http, ctx, "")
    state = str(init.get("state") or "")
    logger.info("z-style risk/initialize state=%s", state)
    if state != "riskInitializationRequired":
        raise RuntimeError(f"risk/initialize 未预期状态: {state}")
    if not ctx.continuation_token:
        raise RuntimeError("risk/initialize 未返回 continuationToken")

    evaluate_experiment_assignments(http, ctx)
    if ctx.human_sensor_url:
        load_human_sensor(http, ctx)

    return _acquire_silent_px(
        http,
        ctx,
        mode=mode,
        proxy=proxy,
        country=country,
    )


def verify_z_style_risk(
    http: OutlookHttpSession,
    ctx: SignupSession,
    account: AccountInfo,
    silent_px: dict[str, str],
    *,
    proxy: Optional[str],
) -> None:
    """Submit z-style verify #1/#2, then leave CreateAccount to the caller."""
    try:
        resp1 = risk_verify(
            http,
            ctx,
            continuation_token=ctx.continuation_token,
            risk_provider_metadata=build_px_metadata(silent_px),
            msa_create_signature=build_msa_create_signature(account, ctx),
        )
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 403:
            body = ""
            try:
                body = exc.response.text[:200]
            except Exception:  # noqa: BLE001
                pass
            raise RiskBlocked(
                f"verify #1 riskBlock（该 IP 被拦，换新 IP 重试）: {body}"
            ) from exc
        raise

    state = str(resp1.get("state") or "")
    logger.info("z-style risk/verify #1 state=%s", state)
    if state == "continue":
        return
    if state != "riskChallengeRequired":
        raise RuntimeError(f"risk/verify #1 未预期状态: {state}")

    challenge = resp1.get("challengeDetails")
    challenge = challenge if isinstance(challenge, dict) else {}
    challenge_meta = challenge.get("challengeMetadata")
    challenge_meta = challenge_meta if isinstance(challenge_meta, dict) else {}
    if not challenge_meta.get("uuid") or not challenge_meta.get("vid"):
        raise RuntimeError("risk/verify #1 缺少 challengeMetadata.uuid/vid")
    challenge_type = str(challenge.get("challengeType") or "HumanCaptcha")
    ctx.px_challenge_meta = challenge_meta

    if _is_offcaptcha_solver():
        load_challenge_iframe(http, ctx, challenge_meta)
        post_px_beacon(http, ctx, tag="z-style-pre-press")
    else:
        load_challenge_iframe(http, ctx, challenge_meta)
        preload_px_challenge_assets(http, ctx, challenge_meta)
        warmup_px_session(http, ctx)
        time.sleep(1.0)
        post_px_beacon(http, ctx, tag="z-style-pre-press")
        post_px_bundle(http, ctx, tag="z-style-pre-press")

    if not _protocol_verify2(
        http,
        ctx,
        account,
        proxy=proxy,
        challenge_meta=challenge_meta,
        challenge_type=challenge_type,
    ):
        raise Verify2Failed("z-style risk/verify #2 未通过（PX press 打码未通过）")
