"""用老号 refresh_token 走 IMAP 读微软安全验证码（proofs OTT）。

链式自产自销：新号注册时把某个已带 token 的老号填成 proof 备用邮箱，
微软把 OTT 验证码发到老号收件箱，这里用老号 refresh_token 换 access_token，
XOAUTH2 登录 IMAP 读出验证码回填 proofs/Verify。
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
import time
from email.header import decode_header
from typing import Optional

import requests

# mail_reader 走 IMAP 读老号验证码，固定用 IMAP scope（不受全局 MAIL_TOKEN_MODE 影响）
from outlook_auto_register.config.constants import IMAP_MAIL_SCOPE as MAIL_SCOPE
from outlook_auto_register.config.constants import MAIL_CLIENT_ID, MAIL_REDIRECT_URI

logger = logging.getLogger(__name__)

IMAP_HOST = "outlook.office.com"
IMAP_PORT = 993

# 微软安全码发件人
_SECURITY_SENDERS = (
    "accountprotection.microsoft.com",
    "account-security-noreply",
    "microsoftonline.com",
    "microsoft.com",
)


def refresh_access_token(refresh_token: str, *, proxy_url: str = "") -> str:
    """老号 refresh_token → access_token（含 IMAP 权限）。"""
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    resp = requests.post(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data={
            "client_id": MAIL_CLIENT_ID,
            "refresh_token": refresh_token,
            "redirect_uri": MAIL_REDIRECT_URI,
            "grant_type": "refresh_token",
            "scope": MAIL_SCOPE,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        proxies=proxies,
        timeout=30,
    )
    data = resp.json()
    tok = data.get("access_token", "")
    if not tok:
        logger.error("刷新 access_token 失败: %s %s", data.get("error"), data.get("error_description", "")[:120])
    return tok


def _xoauth2(user: str, token: str) -> bytes:
    # imaplib.authenticate 会自行 base64，这里返回原始字节
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()


def _decode(s) -> str:
    if not s:
        return ""
    out = []
    for part, enc in decode_header(s):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", "ignore"))
        else:
            out.append(part)
    return "".join(out)


def _body_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                except Exception:  # noqa: BLE001
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                except Exception:  # noqa: BLE001
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return msg.get_payload() or ""


def _extract_code(subject: str, body: str) -> str:
    # Prefer a number immediately following Microsoft's explicit code label;
    # alert mails also contain years (for example ``2026``) that must not be
    # mistaken for the OTP.
    labelled = re.search(
        r"(?:security\s+code|verification\s+code|single[- ]use\s+code|安全代码|安全码|验证码|一次性代码)"
        r"(?:为|is)?\s*[:：]?\s*(\d{6,8})",
        f"{subject or ''}\n{body or ''}",
        re.I,
    )
    if labelled:
        return labelled.group(1)
    # Then use independent digit runs in the subject only (legacy messages
    # sometimes omit the label and place the OTP there).  Do not scan the
    # whole body: security-alert messages contain dates, IPs and URL ids.
    m = re.search(r"(?<!\d)(\d{6,8})(?!\d)", subject or "")
    if m:
        return m.group(1)
    return ""


def read_security_code(
    email_addr: str,
    refresh_token: str,
    *,
    since_ts: float = 0.0,
    timeout: int = 150,
    proxy_url: str = "",
) -> str:
    """轮询老号 IMAP，读最新的微软安全验证码。

    since_ts：只认此刻之后收到的邮件（避免读到旧码）。返回验证码或空串。
    """
    token = refresh_access_token(refresh_token, proxy_url=proxy_url)
    if not token:
        return ""
    deadline = time.time() + timeout
    auth = lambda _: _xoauth2(email_addr, token)  # noqa: E731

    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            try:
                M.authenticate("XOAUTH2", auth)
            except imaplib.IMAP4.error as exc:
                logger.error("IMAP XOAUTH2 登录失败(%s): %s", email_addr, str(exc)[:160])
                return ""
            for folder in ("INBOX", "Junk"):
                try:
                    status, _ = M.select(folder)
                    if status != "OK":
                        continue
                except Exception:  # noqa: BLE001
                    continue
                typ, data = M.search(None, "ALL")
                if typ != "OK" or not data or not data[0]:
                    continue
                ids = data[0].split()[-15:][::-1]  # 最近 15 封，新的优先
                for mid in ids:
                    typ, md = M.fetch(mid, "(RFC822)")
                    if typ != "OK" or not md or not md[0]:
                        continue
                    msg = email.message_from_bytes(md[0][1])
                    frm = _decode(msg.get("From", ""))
                    if not any(s in frm.lower() for s in _SECURITY_SENDERS):
                        continue
                    # 时间过滤
                    try:
                        rcv = email.utils.parsedate_to_datetime(msg.get("Date", "")).timestamp()
                    except Exception:  # noqa: BLE001
                        rcv = 0.0
                    if since_ts and rcv and rcv < since_ts - 120:
                        continue
                    subject = _decode(msg.get("Subject", ""))
                    code = _extract_code(subject, _body_text(msg))
                    if code:
                        logger.info("读到安全验证码 %s（来自 %s）", code, email_addr)
                        try:
                            M.logout()
                        except Exception:  # noqa: BLE001
                            pass
                        return code
            try:
                M.logout()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("IMAP 读取异常，重试: %s", str(exc)[:120])
        time.sleep(5)
    logger.error("等待安全验证码超时(%ss)：%s", timeout, email_addr)
    return ""


def read_security_code_imap_password(
    email_addr: str,
    password: str,
    *,
    imap_host: str,
    imap_port: int = 993,
    since_ts: float = 0.0,
    timeout: int = 150,
) -> str:
    """第三方恢复邮箱（your-recovery-host.com 等）IMAP 账密登录读微软 OTT。"""
    if not imap_host:
        return ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(imap_host, imap_port)
            try:
                M.login(email_addr, password)
            except imaplib.IMAP4.error as exc:
                logger.error("外部恢复邮箱 IMAP 登录失败(%s): %s", email_addr, str(exc)[:160])
                return ""
            for folder in ("INBOX", "Junk", "Spam"):
                try:
                    status, _ = M.select(folder)
                    if status != "OK":
                        continue
                except Exception:  # noqa: BLE001
                    continue
                typ, data = M.search(None, "ALL")
                if typ != "OK" or not data or not data[0]:
                    continue
                ids = data[0].split()[-20:][::-1]
                for mid in ids:
                    typ, md = M.fetch(mid, "(RFC822)")
                    if typ != "OK" or not md or not md[0]:
                        continue
                    msg = email.message_from_bytes(md[0][1])
                    frm = _decode(msg.get("From", ""))
                    if not any(s in frm.lower() for s in _SECURITY_SENDERS):
                        continue
                    try:
                        rcv = email.utils.parsedate_to_datetime(msg.get("Date", "")).timestamp()
                    except Exception:  # noqa: BLE001
                        rcv = 0.0
                    if since_ts and rcv and rcv < since_ts - 120:
                        continue
                    subject = _decode(msg.get("Subject", ""))
                    code = _extract_code(subject, _body_text(msg))
                    if code:
                        logger.info("外部恢复邮箱读到 OTT %s（%s）", code, email_addr)
                        try:
                            M.logout()
                        except Exception:  # noqa: BLE001
                            pass
                        return code
            try:
                M.logout()
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("外部恢复 IMAP 异常，重试: %s", str(exc)[:120])
        time.sleep(5)
    logger.error("外部恢复邮箱等待 OTT 超时(%ss)：%s", timeout, email_addr)
    return ""
