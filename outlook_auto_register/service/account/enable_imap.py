"""IMAP 可用性工具：验证四段式 refresh_token 能否 XOAUTH2 登录 IMAP，以及
（尽力而为地）通过 OWA SetConsumerMailbox 开启 IMAP/POP。

背景（基于仓库 HAR 抓包 + 微软文档实测结论）：
- 四段式 refresh_token 本身可在新号上通过 OAuth 授权码 + 消费者同意流程立即拿到
  （见 post_register.submit_consent）。但消费者 outlook.com 的 IMAP/POP «协议开关»
  默认关闭（GetConsumerMailbox: ImapEnabled=false），且新号短期内（约 10–24h）调
  SetConsumerMailbox 开启会被服务端以 412 OwaInvalidServiceRequestException 拒绝
  （反滥用）。微软官方与社区一致确认：即便持有 IMAP.AccessAsUser.All 令牌，
  只要邮箱协议未开启，XOAUTH2 IMAP 登录也会 NO AUTHENTICATE failed。
- 因此 refresh_token「拿得到」≠ IMAP「当下能读」。本模块用于把这两件事分别探测/操作。
"""
from __future__ import annotations

import imaplib
import logging
from typing import Optional

import requests

# 本模块专测 IMAP，固定用 IMAP scope（不随全局 MAIL_TOKEN_MODE 变成 graph）
from outlook_auto_register.config.constants import IMAP_MAIL_SCOPE as MAIL_SCOPE
from outlook_auto_register.config.constants import MAIL_CLIENT_ID, MAIL_REDIRECT_URI

logger = logging.getLogger(__name__)

IMAP_HOST = "outlook.office.com"
IMAP_PORT = 993
OWA_SERVICE = "https://outlook.live.com/owa/service.svc"


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str = MAIL_CLIENT_ID,
    scope: str = MAIL_SCOPE,
    proxy_url: str = "",
) -> dict:
    """refresh_token → token 响应（含 access_token）。返回完整 JSON。"""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    resp = requests.post(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data={
            "client_id": client_id,
            "refresh_token": refresh_token,
            "redirect_uri": MAIL_REDIRECT_URI,
            "grant_type": "refresh_token",
            "scope": scope,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        proxies=proxies,
        timeout=30,
    )
    try:
        return resp.json()
    except ValueError:
        return {"error": "non_json", "status": resp.status_code, "body": resp.text[:300]}


def imap_login_test(
    email: str,
    refresh_token: str,
    *,
    proxy_url: str = "",
    select_inbox: bool = True,
) -> dict:
    """用四段式 refresh_token 实测 IMAP XOAUTH2 登录。

    返回 {ok, stage, detail, message_count?}。ok=True 表示 IMAP 当前确实可用。
    典型失败：token 刷新失败(refresh)、XOAUTH2 被拒(login，多因协议未开启)。
    """
    tok = refresh_access_token(refresh_token, proxy_url=proxy_url)
    access = tok.get("access_token", "")
    if not access:
        return {
            "ok": False,
            "stage": "refresh",
            "detail": f"{tok.get('error')}: {str(tok.get('error_description', ''))[:160]}",
        }

    auth_str = f"user={email}\x01auth=Bearer {access}\x01\x01".encode()
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "connect", "detail": str(exc)[:200]}
    try:
        try:
            M.authenticate("XOAUTH2", lambda _: auth_str)
        except imaplib.IMAP4.error as exc:
            return {"ok": False, "stage": "login", "detail": str(exc)[:200]}
        if not select_inbox:
            return {"ok": True, "stage": "login", "detail": "XOAUTH2 认证通过"}
        typ, data = M.select("INBOX")
        if typ != "OK":
            return {"ok": True, "stage": "select", "detail": "登录成功但 select INBOX 非 OK"}
        try:
            count = int(data[0])
        except Exception:  # noqa: BLE001
            count = -1
        return {"ok": True, "stage": "select", "detail": "IMAP 可用", "message_count": count}
    finally:
        try:
            M.logout()
        except Exception:  # noqa: BLE001
            pass


def _owa_headers(usertoken: str, action: str) -> dict:
    return {
        "Authorization": f'MSAuth1.0 usertoken="{usertoken}", type="MSACT"'
        if not usertoken.startswith("MSAuth1.0")
        else usertoken,
        "Content-Type": "application/json; charset=utf-8",
        "Action": action,
        "X-OWA-ActionSource": action,
        "X-Req-Source": "Mail",
        "Prefer": 'IdType="ImmutableId"',
    }


def get_consumer_mailbox(usertoken: str, *, cookies: Optional[dict] = None, proxy_url: str = "") -> dict:
    """读取当前 IMAP/POP 开关状态（GetConsumerMailbox）。"""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    payload = {
        "__type": "GetConsumerMailboxRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "V2018_01_08",
        },
    }
    import json as _json

    resp = requests.post(
        f"{OWA_SERVICE}?action=GetConsumerMailbox&app=Mail",
        headers={**_owa_headers(usertoken, "GetConsumerMailbox"),
                 "X-OWA-UrlPostData": _json.dumps(payload)},
        cookies=cookies or {},
        proxies=proxies,
        timeout=30,
    )
    try:
        return {"status": resp.status_code, "json": resp.json()}
    except ValueError:
        return {"status": resp.status_code, "body": resp.text[:300]}


def enable_imap_via_owa(
    usertoken: str,
    *,
    cookies: Optional[dict] = None,
    proxy_url: str = "",
    pop: bool = True,
    imap: bool = True,
) -> dict:
    """尽力而为开启 IMAP/POP（SetConsumerMailbox）。

    需要调用方提供一枚有效的 OWA usertoken（MSAuth1.0 EwAI... 紧凑票据）及会话 cookie，
    通常来自网页登录后的 outlook.live.com 会话（本工具的纯 API 登录链尚未产出该票据，
    见模块 docstring 与交付说明）。
    新号会返回 412 OwaInvalidServiceRequestException —— 属正常反滥用限制，需等账号成熟。
    返回 {status, ok, error?}。
    """
    import json as _json

    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    payload = {
        "__type": "SetConsumerMailboxRequest:#Exchange",
        "Header": {
            "__type": "JsonRequestHeaders:#Exchange",
            "RequestServerVersion": "V2018_01_08",
        },
        "Options": {"PopEnabled": pop, "PopMessageDeleteEnabled": False, "ImapEnabled": imap},
    }
    resp = requests.post(
        f"{OWA_SERVICE}?action=SetConsumerMailbox&app=Mail",
        headers={**_owa_headers(usertoken, "SetConsumerMailbox"),
                 "X-OWA-UrlPostData": _json.dumps(payload)},
        cookies=cookies or {},
        proxies=proxies,
        timeout=30,
    )
    out: dict = {"status": resp.status_code, "ok": resp.status_code == 200}
    if resp.status_code != 200:
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:300]}
        out["error"] = body
        if resp.status_code == 412:
            out["hint"] = (
                "412 OwaInvalidServiceRequestException：新号反滥用限制，"
                "一般需等待约 10–24h 账号成熟后才能开启 IMAP/POP。"
            )
    return out
