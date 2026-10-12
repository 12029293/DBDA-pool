from __future__ import annotations

import logging
import time

from outlook_auto_register.config.constants import PX_COLLECTOR_BASE, PX_APP_ID
from outlook_auto_register.common.http_session import OutlookHttpSession
from outlook_auto_register.model.entity.register_models import SignupSession

logger = logging.getLogger(__name__)


# HAR 坐实：PX collector 的 beacon/bundle POST 由 PX captcha.js 在
# iframe.hsprotect.net 内发起，Origin/Referer 均为 iframe.hsprotect.net，
# 而非 signup.live.com（用错 Origin 会与真实浏览器指纹不符）。
PX_IFRAME_ORIGIN = "https://iframe.hsprotect.net"


def _collector_headers(ctx: SignupSession, *, referer: str = "") -> dict[str, str]:
    return {
        "Referer": referer or f"{PX_IFRAME_ORIGIN}/",
        "Origin": PX_IFRAME_ORIGIN,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def post_px_beacon(http: OutlookHttpSession, ctx: SignupSession, *, tag: str = "") -> None:
    """对齐 HAR：向 PX collector 发送 beacon（保持会话活跃）。"""
    url = f"{PX_COLLECTOR_BASE}/api/v2/msft/beacon"
    try:
        http.post(
            url,
            headers=_collector_headers(ctx),
            data={"payload": ""},
            timeout=15,
        )
        logger.debug("PX beacon ok tag=%s", tag)
    except Exception as exc:
        logger.debug("PX beacon skip tag=%s: %s", tag, exc)


def post_px_bundle(http: OutlookHttpSession, ctx: SignupSession, *, tag: str = "") -> None:
    """对齐 HAR：bundle 端点 POST（exe/浏览器在 challenge 前后都会打）。"""
    url = f"{PX_COLLECTOR_BASE}/assets/js/bundle"
    try:
        http.post(
            url,
            headers=_collector_headers(ctx),
            data={"payload": ""},
            timeout=15,
        )
        logger.debug("PX bundle POST ok tag=%s", tag)
    except Exception as exc:
        logger.debug("PX bundle POST skip tag=%s: %s", exc)


def warmup_px_session(http: OutlookHttpSession, ctx: SignupSession) -> None:
    """challenge 阶段前后的 collector 预热（HAR 195/187/185 序列）。"""
    post_px_beacon(http, ctx, tag="warmup")
    time.sleep(0.3)
    post_px_bundle(http, ctx, tag="warmup")


def build_challenge_iframe_url(ctx: SignupSession, challenge_meta: dict) -> str:
    challenge_url = challenge_meta.get("challengeUrl", "")
    if challenge_url:
        return challenge_url
    sid = ctx.uaid
    if len(sid) == 32 and "-" not in sid:
        sid = f"{sid[:8]}-{sid[8:12]}-{sid[12:16]}-{sid[16:20]}-{sid[20:]}"
    return (
        f"https://iframe.hsprotect.net/index.html"
        f"?app_id={challenge_meta.get('appId', PX_APP_ID)}&session_id={sid}"
    )


def load_challenge_iframe(http: OutlookHttpSession, ctx: SignupSession, challenge_meta: dict) -> None:
    """加载 challenge iframe（session_id 为 uaid 带连字符）。"""
    challenge_url = build_challenge_iframe_url(ctx, challenge_meta)
    if challenge_url:
        logger.info("加载 challenge iframe…")
        http.get(challenge_url, headers={"Referer": ctx.signup_page_url})
