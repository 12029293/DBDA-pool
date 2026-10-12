from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import urllib.parse
from base64 import urlsafe_b64encode
from typing import Any, Optional

from outlook_auto_register.config.constants import (
    COBRAND_ID,
    DEFAULT_LC,
    DEFAULT_MKT,
    LOGIN_MS_BASE,
    OUTLOOK_CLIENT_ID,
    OUTLOOK_REDIRECT_URI,
    OUTLOOK_SCOPE,
    PX_APP_ID,
    PX_COLLECTOR_BASE,
)
from outlook_auto_register.common.http_session import OutlookHttpSession
from outlook_auto_register.model.entity.register_models import SignupSession

logger = logging.getLogger(__name__)


def _b64url(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode()


def _parse_server_data(html: str) -> dict:
    marker = html.find("var ServerData=")
    if marker < 0:
        marker = html.find("var ServerData =")
    if marker < 0:
        raise ValueError("signup 页面未找到 ServerData")
    json_start = html.find("{", marker)
    obj, _ = json.JSONDecoder().raw_decode(html, json_start)
    return obj


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def bootstrap_session(http: OutlookHttpSession, *, mkt: str = DEFAULT_MKT, lc: str = DEFAULT_LC) -> SignupSession:
    code_verifier, code_challenge = _pkce_pair()

    auth_qs = urllib.parse.urlencode({
        "client_id": OUTLOOK_CLIENT_ID,
        "cobrandid": COBRAND_ID,
        "response_type": "code",
        "redirect_uri": OUTLOOK_REDIRECT_URI,
        "scope": OUTLOOK_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "mkt": mkt,
        "lc": lc,
    })
    auth_url = f"https://login.live.com/oauth20_authorize.srf?{auth_qs}"
    logger.info("获取 OAuth 登录页…")
    login_resp = http.get(auth_url, allow_redirects=True)
    login_resp.raise_for_status()

    m = re.search(r'"(https://signup\.live\.com/signup[^"]+)"', login_resp.text)
    if not m:
        raise RuntimeError("登录页未找到 signup 链接，可能 OAuth 参数有误")

    signup_link = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
    logger.info("加载注册页…")
    signup_resp = http.get(signup_link, allow_redirects=True)
    signup_resp.raise_for_status()

    sd = _parse_server_data(signup_resp.text)
    parsed = urllib.parse.urlparse(signup_link)
    qs = urllib.parse.parse_qs(parsed.query)

    uaid_m = re.search(r"uaid=([a-f0-9]{32})", signup_link)
    uaid = uaid_m.group(1) if uaid_m else qs.get("uaid", [""])[0]
    if not uaid:
        raise RuntimeError("未解析到 uaid")

    ctx = SignupSession(
        uaid=uaid,
        signup_url=signup_link,
        signup_page_url=signup_resp.url,
        cobrandid=qs.get("cobrandid", [COBRAND_ID])[0],
        contextid=qs.get("contextid", [""])[0],
        opid=qs.get("opid", [""])[0],
        bk=qs.get("bk", [""])[0],
        sru=qs.get("sru", [""])[0],
        canary=sd.get("apiCanary", ""),
        hpgid=int(sd.get("hpgid", 200225)),
        scid=int(sd.get("iScenarioId", 100118)),
        server_data=sd,
        code_verifier=code_verifier,
        code_challenge=code_challenge,
        mkt=mkt,
        lc=lc,
    )
    if not ctx.canary:
        raise RuntimeError("ServerData 中无 apiCanary")

    http.signup_ctx = ctx
    logger.info("会话就绪 uaid=%s opid=%s", ctx.uaid, ctx.opid)
    return ctx


def preload_perimeterx(http: OutlookHttpSession, ctx: SignupSession) -> None:
    """预加载 PerimeterX DFP + iframe + collector（对齐 HAR）。"""
    captcha_info = ctx.server_data.get("oCaptchaInfo", {})
    dfp_url = captcha_info.get("urlDfp")
    human_url = captcha_info.get("urlHumanIframe")

    urls = [
        dfp_url,
        human_url,
        f"{PX_COLLECTOR_BASE}/api/v2/msft/beacon",
        f"{PX_COLLECTOR_BASE}/assets/js/bundle",
    ]
    for url in urls:
        if not url:
            continue
        logger.info("PX 预加载: %s", url[:100])
        try:
            http.get(url, headers={"Referer": ctx.signup_page_url})
        except Exception as exc:
            logger.debug("PX 预加载跳过 %s: %s", url[:60], exc)

    logger.debug("PX 预加载后 cookies: %s", http.cookie_names())


def preload_px_challenge_assets(
    http: OutlookHttpSession,
    ctx: SignupSession,
    challenge_meta: dict[str, Any],
) -> None:
    """riskChallengeRequired 后加载 challenge 相关资源。"""
    uuid = challenge_meta.get("uuid", "")
    vid = challenge_meta.get("vid", "")
    challenge_url = challenge_meta.get("challengeUrl", "")

    urls = [
        challenge_url,
        f"https://stk.hsprotect.net/ns?c={uuid}" if uuid else "",
        (
            f"https://captcha.hsprotect.net/{PX_APP_ID}/captcha.js"
            f"?a=c&m=0&u={uuid}&v={vid}"
            if uuid and vid else ""
        ),
        f"https://df.cfp.microsoft.com/Clear.HTML?ctx=Ls1.0&wl=False&session_id={uuid}" if uuid else "",
        f"https://fpt.live.com/Images/Clear.PNG?ctx=jscb1.0&session_id={uuid}" if uuid else "",
    ]
    for url in urls:
        if not url:
            continue
        logger.info("挑战资源: %s", url[:100])
        try:
            http.get(url, headers={"Referer": ctx.signup_page_url})
        except Exception as exc:
            logger.debug("挑战资源跳过: %s", exc)

    ctx.px_challenge_meta = challenge_meta


# ---------------------------------------------------------------------------
# z-style pre-CreateAccount bootstrap
# ---------------------------------------------------------------------------


def _capture_json_stringify(text: str, name: str) -> dict[str, Any]:
    """Parse ``var <name> = JSON.stringify({...});`` from account.microsoft.com."""
    pattern = rf"var {re.escape(name)} = JSON\.stringify\((.+?)\);"
    match = re.search(pattern, text)
    if not match:
        raise RuntimeError(f"account.microsoft.com 未找到 {name}")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"无法解析 {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} 不是 JSON 对象")
    return value


def _capture_me_control_options(text: str) -> dict[str, Any]:
    """Parse the meControlOptions object used by the reference flow."""
    match = re.search(
        r"meControlOptions:\s*(\{[\s\S]*?\})(?=,\s*\r?\n\s*events\s*:)",
        text,
    )
    if not match:
        raise RuntimeError("account.microsoft.com 未找到 meControlOptions")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"无法解析 meControlOptions: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("meControlOptions 不是 JSON 对象")
    return value


def _server_int(data: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(data.get(key, default))
    except (TypeError, ValueError):
        return default


def _absolute_login_endpoint(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"{LOGIN_MS_BASE}{value if value.startswith('/') else '/' + value}"


def bootstrap_account_session(
    http: OutlookHttpSession,
    *,
    mkt: str = DEFAULT_MKT,
    lc: str = DEFAULT_LC,
) -> SignupSession:
    """Bootstrap using the account.microsoft.com → signup route.

    The returned object deliberately keeps the existing ``SignupSession`` contract
    so the current CreateAccount and post-login pipeline can consume it unchanged.
    """
    logger.info("加载 account.microsoft.com 参数…")
    account_resp = http.get("https://account.microsoft.com/", allow_redirects=True)
    account_resp.raise_for_status()
    account_html = account_resp.text

    area_config = _capture_json_stringify(account_html, "areaConfig")
    create_account_url = str(area_config.get("createAccountUrl") or "").replace(":443", "")
    if not create_account_url:
        raise RuntimeError("areaConfig 中没有 createAccountUrl")

    # This value is not required by the current signup API, but parsing it keeps
    # the same bootstrap observability as the reference implementation.
    control_options = _capture_me_control_options(account_html)
    auth_provider = control_options.get("authProviderConfig")
    if isinstance(auth_provider, dict):
        aad = auth_provider.get("aad")
        if isinstance(aad, dict) and aad.get("appId"):
            logger.debug("account appId=%s", str(aad["appId"])[:32])

    logger.info("加载注册页…")
    signup_resp = http.get(create_account_url, allow_redirects=True)
    signup_resp.raise_for_status()
    signup_url = str(signup_resp.url or create_account_url)
    server_data = _parse_server_data(signup_resp.text)
    parsed = urllib.parse.urlparse(signup_url)
    qs = urllib.parse.parse_qs(parsed.query)

    uaid = str(server_data.get("sUnauthSessionID") or qs.get("uaid", [""])[0])
    if not uaid:
        raise RuntimeError("ServerData 中未找到 sUnauthSessionID")
    canary = str(server_data.get("apiCanary") or "")
    if not canary:
        raise RuntimeError("ServerData 中无 apiCanary")

    risk_init_url = _absolute_login_endpoint(server_data.get("urlRiskInitialize"))
    risk_verify_url = _absolute_login_endpoint(server_data.get("urlRiskVerify"))
    if not risk_init_url or not risk_verify_url:
        raise RuntimeError("ServerData 中缺少 risk initialize/verify endpoint")

    captcha_info = server_data.get("oCaptchaInfo")
    if not isinstance(captcha_info, dict):
        captcha_info = {}
    px_fpt_url = str(captcha_info.get("urlDfp") or "")
    px_app_id = str(server_data.get("sHumanAppId") or PX_APP_ID)
    ctx = SignupSession(
        uaid=uaid,
        signup_url=signup_url,
        signup_page_url=signup_url,
        cobrandid=str(qs.get("cobrandid", [COBRAND_ID])[0]),
        contextid=str(qs.get("contextid", [""])[0]),
        opid=str(qs.get("opid", [""])[0]),
        bk=str(qs.get("bk", [""])[0]),
        sru=str(
            qs.get("sru", [""])[0]
            or server_data.get("urlLogin")
            or server_data.get("sru")
            or ""
        ),
        canary=canary,
        hpgid=_server_int(server_data, "hpgid", 200225),
        scid=_server_int(server_data, "iScenarioId", 100118),
        server_data=server_data,
        signup_query=parsed.query,
        mkt=str(qs.get("mkt", [mkt])[0] or mkt),
        lc=str(qs.get("lc", [lc])[0] or lc),
        risk_initialize_url=risk_init_url,
        risk_verify_url=risk_verify_url,
        site_id=str(server_data.get("sSiteId") or ""),
        px_app_id=px_app_id,
        px_session_id=uaid,
        px_fpt_url=px_fpt_url,
        px_press_target="/api/v1.0/risk/verify",
    )
    server_data["_bootstrap_route"] = "account.microsoft.com"
    http.signup_ctx = ctx
    logger.info(
        "z-style 会话就绪 uaid=%s scid=%s hpgid=%s",
        ctx.uaid,
        ctx.scid,
        ctx.hpgid,
    )
    return ctx
