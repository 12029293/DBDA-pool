from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any, Optional

from outlook_auto_register.config.constants import (
    LOGIN_MS_BASE,
    PX_APP_ID,
    RISK_INITIALIZE_PATH,
    RISK_VERIFY_PATH,
    SIGNUP_API_BASE,
    SIGNUP_CLIENT_ID,
)
from outlook_auto_register.common.http_session import OutlookHttpSession
from outlook_auto_register.model.entity.register_models import AccountInfo, SignupSession

logger = logging.getLogger(__name__)


def _signup_api_url(endpoint: str, ctx: SignupSession) -> str:
    if ctx.signup_query:
        return f"{SIGNUP_API_BASE}/{endpoint}?{ctx.signup_query}"
    qs = urllib.parse.urlencode(ctx.common_query_params())
    return f"{SIGNUP_API_BASE}/{endpoint}?{qs}"


def check_available_signin_name(
    http: OutlookHttpSession,
    ctx: SignupSession,
    email: str,
) -> dict[str, Any]:
    url = _signup_api_url("CheckAvailableSigninNames", ctx)
    body = {
        "includeSuggestions": True,
        "signInName": email,
        "uiflvr": 1001,
        "scid": ctx.scid,
        "uaid": ctx.uaid,
        "hpgid": ctx.hpgid,
    }
    resp = http.post(url, headers=http.api_headers(ctx), json=body)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"CheckAvailable 失败: {data['error']}")
    http.update_canary(ctx, data)
    if data.get("telemetryContext"):
        ctx.telemetry_context = data["telemetryContext"]
    return data


_CLIENT_EXPERIMENTS = [
    {
        "parallax": "enablesisufeedback",
        "control": "enablesisufeedback_control",
        "treatments": ["enablesisufeedback_treatment"],
    },
    {
        "parallax": "addprivatebrowsingtexttofabricfooter",
        "control": "addprivatebrowsingtexttofabricfooter_control",
        "treatments": ["addprivatebrowsingtexttofabricfooter_treatment"],
    },
]


def evaluate_experiment_assignments(
    http: OutlookHttpSession,
    ctx: SignupSession,
) -> dict[str, Any]:
    """Run the reference flow's experiment step and refresh session context."""
    url = f"{SIGNUP_API_BASE}/EvaluateExperimentAssignments"
    resp = http.post(
        url,
        headers=http.api_headers(ctx, origin="https://signup.live.com"),
        json={"clientExperiments": _CLIENT_EXPERIMENTS},
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"EvaluateExperimentAssignments 失败: {data['error']}")
    new_canary = data.get("apiCanary")
    telemetry = data.get("telemetryContext")
    if not new_canary:
        raise RuntimeError("EvaluateExperimentAssignments 未返回 apiCanary")
    if not telemetry:
        raise RuntimeError("EvaluateExperimentAssignments 未返回 telemetryContext")
    http.update_canary(ctx, data)
    ctx.telemetry_context = str(telemetry)
    ctx.server_data["telemetryContext"] = str(telemetry)
    logger.debug("实验配置完成，telemetryContext 已更新")
    return data


def risk_initialize(
    http: OutlookHttpSession,
    ctx: SignupSession,
    continuation_token: str = "",
    *,
    origin: str = "https://signup.live.com",
) -> dict[str, Any]:
    """
    初始化风控。首次传空字符串即可拿到初始 continuationToken 与 humanSensorUrl。
    注意：字段必须是空字符串 ""，传 None 会 400。
    """
    url = ctx.risk_initialize_url or f"{LOGIN_MS_BASE}{RISK_INITIALIZE_PATH}"
    body = {"continuationToken": continuation_token or ""}
    resp = http.post(url, headers=http.api_headers(ctx, origin=origin), json=body)
    resp.raise_for_status()
    data = resp.json()
    if data.get("continuationToken"):
        ctx.continuation_token = data["continuationToken"]
    human_url = data.get("humanSensorUrl")
    if not human_url:
        init_data = data.get("riskInitializationData") or []
        if init_data and isinstance(init_data[0], dict):
            human_url = init_data[0].get("humanSensorUrl")
    if human_url:
        ctx.human_sensor_url = human_url
    return data


def build_msa_risk_verify_signature(account: AccountInfo, ctx: SignupSession) -> dict[str, Any]:
    """HAR entry 203：首轮 risk/verify 必须携带注册信息签名。"""
    return {
        "memberName": account.email,
        "siteId": SIGNUP_CLIENT_ID,
        "uiFlavor": "Web",
        "appId": SIGNUP_CLIENT_ID,
        "birthdate": account.birth_date,
        "firstName": account.first_name,
        "lastName": account.last_name,
        "countryCode": account.country,
        "verificationCode": "",
        "deviceDetails": {"isRdm": False},
        "action": "SignUp",
    }


def build_msa_create_signature(account: AccountInfo, ctx: SignupSession) -> dict[str, Any]:
    """Build the signature used by the account.microsoft.com reference flow."""
    return {
        "memberName": account.email,
        "siteId": str(ctx.site_id or ctx.server_data.get("sSiteId") or "292666"),
        "uiFlavor": "Web",
        "appId": "",
        "birthdate": account.birth_date,
        "firstName": account.first_name,
        "lastName": account.last_name,
        "countryCode": account.country,
        "verificationCode": "",
        "deviceDetails": {"isRdm": False},
    }


def risk_verify(
    http: OutlookHttpSession,
    ctx: SignupSession,
    *,
    continuation_token: str,
    risk_provider_metadata: Optional[list[dict[str, str]]] = None,
    challenge_solution: Optional[dict[str, str]] = None,
    msa_risk_verify_signature: Optional[dict[str, Any]] = None,
    msa_create_signature: Optional[dict[str, Any]] = None,
    origin: str = "https://signup.live.com",
) -> dict[str, Any]:
    url = ctx.risk_verify_url or f"{LOGIN_MS_BASE}{RISK_VERIFY_PATH}"
    body: dict[str, Any] = {"continuationToken": continuation_token}
    if risk_provider_metadata:
        body["riskProviderMetadata"] = risk_provider_metadata
    if challenge_solution:
        body["challengeSolution"] = challenge_solution
    if msa_risk_verify_signature:
        body["msaRiskVerifySignature"] = msa_risk_verify_signature
    if msa_create_signature:
        body["msaCreateSignature"] = msa_create_signature

    resp = http.post_risk(url, headers=http.api_headers(ctx, origin=origin), json=body)
    if resp.status_code >= 400:
        logger.error("risk/verify HTTP %s body=%s", resp.status_code, resp.text[:800])
        resp.raise_for_status()
    data = resp.json()
    logger.debug("risk/verify response state=%s keys=%s", data.get("state"), list(data.keys()))
    if data.get("continuationToken"):
        ctx.continuation_token = data["continuationToken"]
    return data


def create_account(
    http: OutlookHttpSession,
    ctx: SignupSession,
    account: AccountInfo,
    *,
    check_avail_map: list[str],
    member_name_change_count: int = 1,
    member_name_available_count: int = 1,
    member_name_unavailable_count: int = 0,
) -> dict[str, Any]:
    url = _signup_api_url("CreateAccount", ctx)
    signup_return = ctx.server_data.get("urlLogin") or ctx.sru
    body = {
        "BirthDate": account.birth_date,
        "CheckAvailStateMap": check_avail_map,
        "Country": account.country,
        "EvictionWarningShown": [],
        "FirstName": account.first_name,
        "IsRDM": False,
        "IsOptOutEmailDefault": True,
        "IsOptOutEmailShown": 1,
        "IsOptOutEmail": True,
        "IsUserConsentedToChinaPIPL": True,
        "LastName": account.last_name,
        "LW": 1,
        "MemberName": account.email,
        "RequestTimeStamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "ReturnUrl": "",
        "SignupReturnUrl": signup_return,
        "SuggestedAccountType": "EASI",
        "SiteId": "",
        "VerificationCodeSlt": "",
        "PrivateAccessToken": "",
        "WReply": "",
        "MemberNameChangeCount": member_name_change_count,
        "MemberNameAvailableCount": member_name_available_count,
        "MemberNameUnavailableCount": member_name_unavailable_count,
        "Password": account.password,
        "uiflvr": 1001,
        "scid": ctx.scid,
        "uaid": ctx.uaid,
        "hpgid": ctx.hpgid,
    }
    if ctx.continuation_token:
        body["ContinuationToken"] = ctx.continuation_token

    resp = http.post(url, headers=http.api_headers(ctx), json=body)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"CreateAccount 失败: {data['error']}")
    http.update_canary(ctx, data)
    return data


def build_px_metadata(px_solution: dict[str, str]) -> list[dict[str, str]]:
    """兼容旧引用，实际逻辑在 px_cookies。"""
    from outlook_auto_register.service.risk.px_cookies import build_px_metadata as _build

    return _build(px_solution)
