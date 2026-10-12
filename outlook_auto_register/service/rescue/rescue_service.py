"""账号解封（rescue）：纯协议登录 abuse/locked 号 → 验证恢复邮箱 → 解锁。

流程（浏览器抓包确认）：
  GET  login.srf                        → flowToken(=PPFT) / uaid / opid / urlPost
  POST GetCredentialType.srf            → 提交邮箱，返回 OtcLoginEligibleProofs[].data(=SentProofIDE)
  POST GetOneTimeCode.srf?id=292841     → 发验证码（State 201/206 + 新 FlowToken）
  POST ppsecure/post.srf (type=27)      → 输码验证（SentProofIDE + ProofConfirmation + otc）
  POST ppsecure/post.srf (type=11)      → 密码 → 登录

复用 CFDomainMailClient 收码（fovts.dev）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

import requests

from outlook_auto_register.service.resource.recovery.cf_domain_mail import CFDomainMailClient, load_config
from outlook_auto_register.common.http_session import OutlookHttpSession

logger = logging.getLogger(__name__)

_SIGNIN_URL = (
    "https://login.live.com/login.srf?wa=wsignin1.0"
    "&wreply=https://outlook.live.com/mail/"
)
_PAGE_ID = "292841"


def _extract_tokens(html: str) -> dict[str, str]:
    """从 login.srf 提取 flowToken(=PPFT) / uaid / opid / urlPost。"""
    out: dict[str, str] = {}
    i = html.find("sFTTag")
    if i >= 0:
        j = html.find("value=", i)
        if j >= 0:
            j += len("value=")
            if j < len(html) and html[j] == "\\" and j + 1 < len(html) and html[j + 1] == '"':
                j += 2
            elif j < len(html) and html[j] == '"':
                j += 1
            end = j
            while end < len(html) and html[end] not in ('"', "\\", "<"):
                end += 1
            out["PPFT"] = html[j:end]
            out["flowToken"] = out["PPFT"]
    m = re.search(r'"urlPost"\s*:\s*"([^"]+)"', html)
    if m:
        out["urlPost"] = m.group(1).replace("\\/", "/").replace("\\u0026", "&")
    m = re.search(r"uaid=([a-f0-9]{32})", html)
    if m:
        out["uaid"] = m.group(1)
    m = re.search(r"opid=([A-F0-9]{16})", html)
    if m:
        out["opid"] = m.group(1)
    m = re.search(r'"correlationId"\s*:\s*"([0-9a-fA-F-]{36})"', html)
    if m:
        out["correlationId"] = m.group(1)
    m = re.search(r'"urlGetCredentialType"\s*:\s*"([^"]+)"', html)
    if m:
        out["urlGetCredentialType"] = m.group(1).replace("\\/", "/").replace("\\u0026", "&")
    m = re.search(r'"urlDfp"\s*:\s*"([^"]+)"', html)
    if m:
        out["urlDfp"] = m.group(1).replace("\\/", "/").replace("\\u0026", "&")
    m = re.search(r'"arrChainedAuthProbeUrls"\s*:\s*\[(.*?)\]', html)
    if m:
        urls = re.findall(r'"([^"]+)"', m.group(1))
        out["chainedProbeUrls"] = "\n".join(urls)
    return out


def _get_credential_type(http, email, flow_token, uaid, opid, url_get_credential_type="", correlation_id="") -> dict[str, Any]:
    url = url_get_credential_type or (
        f"https://login.live.com/GetCredentialType.srf"
        f"?opid={opid}&id={_PAGE_ID}&mkt=EN-US&lc=1033&uaid={uaid}"
    )
    body = {
        "checkPhones": False,
        "country": "",
        "federationFlags": 3,
        "flowToken": flow_token,
        "forceotclogin": False,
        "isCookieBannerShown": False,
        "isExternalFederationDisallowed": False,
        "isFederationDisabled": False,
        "isFidoSupported": True,
        "isOtherIdpSupported": False,
        "isReactLoginRequest": True,
        "isRemoteConnectSupported": False,
        "isRemoteNGCSupported": True,
        "isSignup": False,
        "originalRequest": "",
        "otclogindisallowed": False,
        "uaid": uaid,
        "username": email,
    }
    hdrs = {
        "Content-Type": "application/json",
        "Referer": _SIGNIN_URL,
        "Origin": "https://login.live.com",
        "Accept": "application/json",
        "client-request-id": correlation_id or uaid,
        "correlationid": correlation_id or uaid,
        "hpgact": "0",
        "hpgid": "33",
    }
    resp = http.post(url, json=body, headers=hdrs, allow_redirects=True)
    return resp.json() if resp.status_code == 200 else {}


def _get_one_time_code(http, email, flow_token, uaid) -> dict[str, Any]:
    url = f"https://login.live.com/GetOneTimeCode.srf?id={_PAGE_ID}"
    hdrs = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": _SIGNIN_URL,
        "Origin": "https://login.live.com",
        "Accept": "application/json",
        "client-request-id": uaid,
        "correlationid": uaid,
        "hpgact": "0",
        "hpgid": "33",
    }
    resp = http.post(url, data={"login": email, "flowtoken": flow_token}, headers=hdrs, allow_redirects=True)
    return resp.json() if resp.status_code == 200 else {}


def _post_form(http, url, fields, referer) -> requests.Response:
    return http.post(url, data=fields, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://login.live.com",
        "Referer": referer,
    }, allow_redirects=True)


def _follow_fmhf(http, resp):
    """跟随 fmHF 自动提交表单（ppsecure/post.srf 之后返回的 JS 跳转页）。"""
    body = resp.text or ""
    m = re.search(r'<form[^>]*name="fmHF"[^>]*action="([^"]*)"[^>]*>', body, re.I)
    if not m:
        return resp
    action = m.group(1).replace("&amp;", "&")
    fields = {}
    for fm in re.finditer(r'<input[^>]*type="hidden"[^>]*>', body):
        n = re.search(r'name="([^"]*)"', fm.group(0))
        v = re.search(r'value="([^"]*)"', fm.group(0))
        if n:
            fields[n.group(1)] = v.group(1) if v else ""
    return http.post(action, data=fields, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://login.live.com",
        "Referer": resp.url,
    }, allow_redirects=True)


def _follow_fmhf_until_done(http, resp, max_hop=6):
    """循环跟随 fmHF，直到不再返回 fmHF。"""
    for _ in range(max_hop):
        if 'name="fmHF"' not in (resp.text or ""):
            return resp
        resp = _follow_fmhf(http, resp)
    return resp


def _preload_login_cookies(http, tok):
    """预加载 fpt.live.com + chained-auth probe，拿到 fptctx2/ChainedAuthProbe cookie（GetOneTimeCode 需要）。"""
    urls = []
    if tok.get("urlDfp"):
        urls.append(tok["urlDfp"])
    for u in (tok.get("chainedProbeUrls") or "").split("\n"):
        if u:
            urls.append(u)
    for u in urls:
        try:
            http.get(u, headers={"Referer": _SIGNIN_URL}, allow_redirects=True)
        except Exception as exc:
            logger.debug("预加载 %s 失败: %s", u[:60], exc)


def _read_cf_code(recovery_email: str) -> str:
    try:
        return CFDomainMailClient(load_config()).read_security_code(
            recovery_email, timeout=150, poll_interval=4.0,
        )
    except Exception as exc:
        logger.error("CF 读码异常: %s", exc)
        return ""


def rescue_account(
    email: str,
    password: str,
    recovery_email: str = "",
    *,
    proxy: Optional[str] = None,
    country: str = "US",
) -> dict[str, str]:
    http = OutlookHttpSession(proxy=proxy)
    try:
        resp = http.get(_SIGNIN_URL, allow_redirects=True)
        tok = _extract_tokens(resp.text or "")
        if not tok.get("flowToken"):
            return {"state": "error", "detail": "未提取到 flowToken"}
        logger.info("login.srf 就绪 uaid=%s", tok.get("uaid", "")[:12])
        _preload_login_cookies(http, tok)

        cred = _get_credential_type(http, email, tok["flowToken"], tok.get("uaid", ""), tok.get("opid", ""), tok.get("urlGetCredentialType", ""), tok.get("correlationId", ""))
        if not cred or "ErrorHR" in cred:
            return {"state": "error", "detail": f"GetCredentialType 异常: {str(cred)[:100]}"}
        sent_proof_ide = ""
        proof_type = "1"
        credentials = cred.get("Credentials") or {}
        proofs = credentials.get("OtcLoginEligibleProofs") or []
        if isinstance(proofs, list) and proofs and isinstance(proofs[0], dict):
            sent_proof_ide = str(proofs[0].get("data") or "")
            proof_type = str(proofs[0].get("proofType") or "1")
        flow_token = cred.get("FlowToken") or tok["flowToken"]
        logger.info("GetCredentialType OK hasPassword=%s proof=%s", credentials.get("HasPassword"), sent_proof_ide[:16])

        otc = _get_one_time_code(http, email, flow_token, tok.get("uaid", ""))
        flow_token = otc.get("FlowToken") or flow_token
        logger.info("GetOneTimeCode State=%s", otc.get("State"))

        code = _read_cf_code(recovery_email) if recovery_email else ""
        if not code:
            return {"state": "needs_phone", "detail": "未读到恢复邮箱验证码（或需短信）"}

        urlpost = tok.get("urlPost") or "https://login.live.com/ppsecure/post.srf"
        fields = {
            "SentProofIDE": sent_proof_ide,
            "ProofConfirmation": recovery_email,
            "ProofType": proof_type,
            "otc": code,
            "ps": "3",
            "psRNGCDefaultType": "",
            "psRNGCEntropy": "",
            "psRNGCSLK": "",
            "canary": "",
            "ctx": "",
            "hpgrequestid": "",
            "PPFT": tok.get("PPFT", ""),
            "PPSX": "PassportR",
            "NewUser": "1",
            "FoundMSAs": "",
            "fspost": "0",
            "i21": "0",
            "CookieDisclosure": "0",
            "IsFidoSupported": "1",
            "isSignupPost": "0",
            "isRecoveryAttemptPost": "0",
            "i13": "0",
            "login": email,
            "loginfmt": email,
            "type": "27",
            "LoginOptions": "3",
            "lrt": "",
            "lrtPartition": "",
            "hisRegion": "",
            "hisScaleUnit": "",
            "cpr": "0",
        }
        resp = _post_form(http, urlpost, fields, _SIGNIN_URL)
        resp = _follow_fmhf_until_done(http, resp)
        body = resp.text or ""
        cur_url = (resp.url or "").lower()
        if "outlook.live.com" in cur_url or "code=" in cur_url:
            return {"state": "unlocked", "detail": cur_url}

        if "passwd" in body or "password" in body.lower():
            fields2 = {
                "login": email,
                "loginfmt": email,
                "passwd": password,
                "PPFT": tok.get("PPFT", ""),
                "PPSX": "PassportR",
                "NewUser": "1",
                "FoundMSAs": "",
                "fspost": "0",
                "i21": "0",
                "CookieDisclosure": "0",
                "IsFidoSupported": "1",
                "isSignupPost": "0",
                "isRecoveryAttemptPost": "0",
                "i13": "0",
                "type": "11",
                "LoginOptions": "3",
                "lrt": "",
                "lrtPartition": "",
                "hisRegion": "",
                "hisScaleUnit": "",
                "cpr": "0",
            }
            resp = _post_form(http, urlpost, fields2, cur_url or _SIGNIN_URL)
            resp = _follow_fmhf_until_done(http, resp)
            cur_url = (resp.url or "").lower()
            if "outlook.live.com" in cur_url or "code=" in cur_url:
                return {"state": "unlocked", "detail": cur_url}

        return {"state": "failed", "detail": f"未识别登录状态: {cur_url[:120]}"}
    except requests.RequestException as exc:
        return {"state": "error", "detail": str(exc)[:120]}
    except Exception as exc:
        logger.exception("rescue_account 异常")
        return {"state": "error", "detail": str(exc)[:120]}
