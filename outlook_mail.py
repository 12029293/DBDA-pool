# -*- coding: utf-8 -*-
"""Outlook 临时邮箱接口 —— 把 lxf746/outlook-auto-register 接进 dola-pool 注册模块。

背景
----
本项目的「临时邮箱接口」原本是：**先有邮箱地址、再轮询公开收件 API 取码**
（``register_service.fetch_code`` + ``dola_probe.TEMPMAIL_API``，域名 mailto.plus 等）。

Outlook 这条链路的形态不同：邮箱**没有**公开收件 API，必须先「现造」一个可收信的
Outlook 账号，再用它的 Graph/REST 令牌读信。因此这里把接口统一成与原临时邮箱
一致的**两步语义**：

    mb   = open_mailbox(cfg, log)      # 现造一个可收信的 Outlook 邮箱
    code = mb.poll_code(wait, stop)    # 轮询该邮箱，按 dola 取码规则抽 6 位验证码

``register_service`` 里当邮箱来源 = ``outlook`` 时走这条链；其余来源仍走原 tempmail.plus。

底层实现 vendor 自 https://github.com/lxf746/outlook-auto-register
（纯 HTTP Fluent Web API 协议注册，无浏览器依赖；已隔离为 ``outlook_auto_register`` 包）。

依赖：requests + python-dotenv。配置优先取 ``cfg`` 入参，其次环境变量（``.env.local``
已被 ``config._load_local_env`` 注入 ``os.environ``，故写进 .env.local 即生效）：

    DOLA_OUTLOOK_ENABLED       1/0，总开关（默认 1）
    DOLA_OUTLOOK_COUNTRY       注册国家，须与代理出口一致（默认 US）
    DOLA_OUTLOOK_PX_MODE       solver|captcha（captcha.run）|local|offcaptcha（默认 solver）
    DOLA_OUTLOOK_TOKEN_MODE    graph|outlook_rest|login_exe（默认 graph，注册完即可读信）
    DOLA_OUTLOOK_SKIP_PROOFS   1=跳过 proofs 恢复邮箱绑定（默认 1，本用途不需微软恢复邮箱）
    DOLA_OUTLOOK_DOMAIN        邮箱后缀（默认 @outlook.com）
    DOLA_OUTLOOK_PROXY         注册专用代理；留空回落 config.REGISTER_PROXY / config.PROXY
    CAPTCHA_RUN_API_KEY 等     打码平台 key（见 vendored .env.example），PX 打码必需
"""
from __future__ import annotations

import calendar
import html as _html
import os
import re
import time
from typing import Callable, Optional

import config

SOURCE = "outlook"
SOURCE_LABEL = "Outlook 自动注册（Graph 读信）"

_PKG_IMPORT_ERROR = ""


# ---------------------------------------------------------------- 可用性 / 环境
def available() -> bool:
    """vendored 包是否可导入（不可用时面板给出明确提示，而不是静默失败）。"""
    global _PKG_IMPORT_ERROR
    if _env_bool("DOLA_OUTLOOK_ENABLED", True) is False:
        _PKG_IMPORT_ERROR = "DOLA_OUTLOOK_ENABLED=0"
        return False
    try:
        import outlook_auto_register.service.registration.register_service  # noqa: F401
        return True
    except Exception as exc:  # noqa: BLE001
        _PKG_IMPORT_ERROR = str(exc)
        return False


def unavailable_reason() -> str:
    return _PKG_IMPORT_ERROR


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or "").strip() or default


def _env_bool(key: str, default: bool = False) -> bool:
    v = (os.environ.get(key) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# 收码要扫的文件夹：微软常把 dola 的验证码邮件判为垃圾邮件投进 junkemail，
# 只读 inbox 会整批漏码（2026-10-10 实测：inbox 命中 1/4，junkemail 补回 3/4）。
_POLL_FOLDERS = ("inbox", "junkemail")


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    t = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.I | re.S)
    t = re.sub(r"<script[^>]*>.*?</script>", " ", t, flags=re.I | re.S)
    t = _TAG_RE.sub(" ", t)
    return _WS_RE.sub(" ", _html.unescape(t)).strip()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- 邮箱对象
class OutlookMailbox:
    """一个已注册、可读信的 Outlook 邮箱（供 dola 收验证码用）。"""

    def __init__(self, *, email: str, password: str, client_id: str,
                 refresh_token: str, mail_mode: str = "graph",
                 proxy_url: str = "", recovery_email: str = "",
                 login_refresh_token: str = "", created_iso: str = "",
                 imap_host: str = "", imap_port: int = 0, source: str = "auto"):
        self.email = email
        self.password = password
        self.client_id = client_id
        self.refresh_token = refresh_token
        self.mail_mode = mail_mode or "graph"
        self.proxy_url = proxy_url or ""
        self.recovery_email = recovery_email or ""
        self.login_refresh_token = login_refresh_token or ""
        self.created_iso = created_iso or _now_iso()
        self.imap_host = imap_host or ""
        self.imap_port = int(imap_port or 0)
        self.source = source  # auto=现造 / pool=已有邮箱池

    # ---- IMAP 账密收码（两段式邮箱池；Outlook 个人号已停基本认证，多数需应用密码/第三方域） ----
    def _poll_code_imap(self, wait: int, stop, log,
                        since_iso: str = "") -> Optional[str]:
        import email as _email
        import email.utils as _eu
        import imaplib

        import dola_probe as dp

        host = self.imap_host or "outlook.office365.com"
        port = self.imap_port or 993
        since_ts = 0.0
        if since_iso:
            try:
                since_ts = calendar.timegm(
                    time.strptime(since_iso, "%Y-%m-%dT%H:%M:%SZ"))
            except Exception:  # noqa: BLE001
                since_ts = 0.0

        deadline = time.time() + max(10, int(wait))
        round_no = 0
        while time.time() < deadline:
            if stop is not None and stop.is_set():
                return None
            round_no += 1
            try:
                M = imaplib.IMAP4_SSL(host, port, timeout=25)
                try:
                    M.login(self.email, self.password)
                except imaplib.IMAP4.error as exc:
                    if log:
                        log("err", "[outlook] IMAP 登录失败 %s: %s"
                            % (self.email, str(exc)[:140]))
                    return None
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
                    for mid in data[0].split()[-20:][::-1]:
                        try:
                            typ, md = M.fetch(mid, "(RFC822)")
                        except Exception:  # noqa: BLE001
                            continue
                        if typ != "OK" or not md or not md[0]:
                            continue
                        msg = _email.message_from_bytes(md[0][1])
                        try:
                            rcv = _eu.parsedate_to_datetime(
                                msg.get("Date", "")).timestamp()
                        except Exception:  # noqa: BLE001
                            rcv = 0.0
                        if since_ts and rcv and rcv < since_ts - 60:
                            continue
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() in ("text/plain", "text/html"):
                                    try:
                                        body += " " + part.get_payload(decode=True).decode(
                                            part.get_content_charset() or "utf-8", "replace")
                                    except Exception:  # noqa: BLE001
                                        pass
                        else:
                            try:
                                body = msg.get_payload(decode=True).decode(
                                    msg.get_content_charset() or "utf-8", "replace")
                            except Exception:  # noqa: BLE001
                                body = str(msg.get_payload())
                        text = " ".join(x for x in (
                            msg.get("Subject", ""), _strip_html(body)) if x)
                        code = dp.extract_code(text)
                        if code:
                            if log:
                                log("hl", "[outlook] 读到验证码 %s（IMAP，%s）"
                                    % (code, msg.get("From", "")[:48]))
                            return code
                try:
                    M.logout()
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                if log and round_no % 3 == 1:
                    log("warn", "[outlook] IMAP 读信异常: %s" % str(exc)[:120])
            time.sleep(3)
        return None

    # 与原临时邮箱对齐的收码接口：轮询 -> 取 6 位码
    def poll_code(self, wait: int, stop, log: Optional[Callable[[str, str], None]] = None,
                  since_iso: str = "") -> Optional[str]:
        if self.mail_mode == "imap":
            return self._poll_code_imap(wait, stop, log, since_iso)

        from outlook_auto_register.service.account import graph_mail
        import dola_probe as dp

        since = (since_iso or self.created_iso or "").strip()
        deadline = time.time() + max(10, int(wait))
        # 优先用注册时判定的 mode，失败再退另一资源
        modes = [self.mail_mode] + [m for m in ("graph", "outlook_rest")
                                    if m != self.mail_mode]
        seen: set = set()
        round_no = 0
        while time.time() < deadline:
            if stop is not None and stop.is_set():
                return None
            round_no += 1
            got_any = False
            for mode in modes:
                msgs = []
                for folder in _POLL_FOLDERS:
                    try:
                        part = graph_mail.list_messages(
                            self.refresh_token, mode=mode, top=15,
                            folder=folder, proxy_url=self.proxy_url)
                    except Exception as exc:  # noqa: BLE001
                        if log:
                            log("warn", "[outlook] 读信(%s/%s)异常: %s"
                                % (mode, folder, str(exc)[:120]))
                        part = []
                    if part:
                        msgs.extend(part)
                if not msgs:
                    continue
                got_any = True
                for m in msgs:
                    key = (m.get("subject") or "", m.get("received") or "")
                    if key in seen:
                        continue
                    seen.add(key)
                    recv = (m.get("received") or "")
                    if since and recv and recv < since:
                        continue
                    text = " ".join(x for x in (
                        m.get("subject") or "",
                        m.get("preview") or "",
                        _strip_html(m.get("body") or ""),
                    ) if x)
                    code = dp.extract_code(text)
                    if code:
                        if log:
                            log("hl", "[outlook] 读到验证码 %s（%s）"
                                % (code, (m.get("from") or "")[:48]))
                        return code
                # 该 mode 读到信了就不必再试另一资源
                break
            if not got_any and log and round_no % 5 == 1:
                log("info", "[outlook] 等待 %s 收信中…（已等 %ds）"
                    % (self.email, int(time.time() - (deadline - wait))))
            time.sleep(3)
        return None


# ---------------------------------------------------------------- 建邮箱
def _resolve_proxy(cfg: dict) -> str:
    p = (cfg.get("proxy") or "").strip()
    if p:
        return p
    p = _env("DOLA_OUTLOOK_PROXY", "")
    if p:
        return p
    return (getattr(config, "REGISTER_PROXY", "") or "").strip() \
        or (getattr(config, "PROXY", "") or "").strip()


def _detect_mail_mode(email: str, rt: str, proxy_url: str) -> str:
    """探测这枚 refresh_token 能读信的资源：graph / outlook_rest。"""
    try:
        from outlook_auto_register.service.account import graph_mail
        probe = graph_mail.probe_token(email, rt, proxy_url=proxy_url)
        usable = probe.get("usable") or []
        if "graph" in usable:
            return "graph"
        if "outlook_rest" in usable:
            return "outlook_rest"
        # transient（网络抖动）不判死，默认 graph 交给 poll_code 再试
        if probe.get("transient"):
            return "graph"
        raise RuntimeError("令牌不可读信：%s"
                           % (probe.get("detail") or probe.get("refresh") or probe))
    except RuntimeError:
        raise
    except Exception:  # noqa: BLE001 探测本身异常时按 graph 继续
        return "graph"


def open_mailbox(cfg: Optional[dict] = None,
                 log: Optional[Callable[[str, str], None]] = None,
                 ) -> OutlookMailbox:
    """现造一个可收信的 Outlook 邮箱（纯协议注册）。

    耗时较长（约 1~3 分钟/个，含 PX 打码），阻塞式；失败抛 RuntimeError。
    """
    cfg = cfg or {}
    if not available():
        raise RuntimeError("Outlook 邮箱后端不可用：%s" % (unavailable_reason() or "未知"))

    from outlook_auto_register.service.registration.register_service import (
        register_one as _oar_register,
    )

    country = (_env("DOLA_OUTLOOK_COUNTRY", "US") if not cfg.get("outlook_country")
               else str(cfg["outlook_country"]).strip().upper()) or "US"
    px_mode = (str(cfg.get("outlook_px_mode") or "").strip()
               or _env("DOLA_OUTLOOK_PX_MODE", "solver") or "solver")
    token_mode = (str(cfg.get("outlook_token_mode") or "").strip()
                  or _env("DOLA_OUTLOOK_TOKEN_MODE", "graph") or "graph")
    domain = (str(cfg.get("outlook_domain") or "").strip()
              or _env("DOLA_OUTLOOK_DOMAIN", "@outlook.com") or "@outlook.com")
    if not domain.startswith("@"):
        domain = "@" + domain
    skip_proofs = (_as_bool(cfg.get("outlook_skip_proofs"))
                   if cfg.get("outlook_skip_proofs") is not None
                   else _env_bool("DOLA_OUTLOOK_SKIP_PROOFS", True))
    proxy = _resolve_proxy(cfg)

    # 注入 vendored 包读取的环境变量（强制，覆盖 .env.local 旧值）
    os.environ["OUTLOOK_MAIL_TOKEN_MODE"] = token_mode
    if skip_proofs:
        os.environ["OUTLOOK_SKIP_PROOFS"] = "1"
    else:
        os.environ.pop("OUTLOOK_SKIP_PROOFS", None)

    if log:
        log("info", "[outlook] 现造邮箱：国家=%s · PX=%s · 令牌=%s · proofs=%s · 代理=%s"
            % (country, px_mode, token_mode,
               "跳过" if skip_proofs else "绑定恢复邮箱",
               (proxy[:48] + "...") if len(proxy) > 48 else (proxy or "(直连)")))

    res = _oar_register(
        email_prefix=None,
        email_domain=domain,
        country=country,
        proxy=(proxy or None),
        px_mode=px_mode,
        skip_post_login=False,
        fetch_mail_token=True,
    )

    if not res or not getattr(res, "success", False):
        raise RuntimeError("Outlook 注册失败: %s"
                           % (getattr(res, "error", "") or "未知错误"))
    if not getattr(res, "refresh_token", ""):
        raise RuntimeError("Outlook 注册成功但未拿到读信令牌（refresh_token 为空）")

    from outlook_auto_register.service.resource.proxy.proxy_utils import parse_proxy
    _pc = parse_proxy(proxy) if proxy else None
    proxy_url = _pc.url if _pc else ""

    mode = _detect_mail_mode(res.email, res.refresh_token, proxy_url)
    mb = OutlookMailbox(
        email=res.email,
        password=res.password,
        client_id=res.client_id,
        refresh_token=res.refresh_token,
        mail_mode=mode,
        proxy_url=proxy_url,
        recovery_email=getattr(res, "recovery_email", "") or "",
        login_refresh_token=getattr(res, "login_refresh_token", "") or "",
    )
    if log:
        log("ok", "[outlook] 邮箱就绪 %s（mode=%s）" % (mb.email, mode))
    return mb


# ---------------------------------------------------------------- 已有邮箱池（不再现造）
POOL_FILE = "outlook_mailboxes.txt"


def pool_path() -> str:
    """邮箱池文件路径：DOLA_OUTLOOK_MAILBOXES_FILE 优先，否则项目根目录 outlook_mailboxes.txt。"""
    p = _env("DOLA_OUTLOOK_MAILBOXES_FILE", "")
    if p:
        return p
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), POOL_FILE)


def parse_pool_line(line: str) -> Optional[dict]:
    """解析一行邮箱池条目（`#` 开头/空行/无 ``----`` 一律返回 None）。

        email----password----client_id----refresh_token     # 四段（推荐，Graph/Outlook REST 读信）
        email----password----refresh_token                  # 三段（client_id 取项目默认）
        email----password                                   # 两段（IMAP 应用密码，需能过基本认证）
    """
    line = (line or "").strip()
    if not line or line.startswith("#") or "----" not in line:
        return None
    parts = [p.strip() for p in line.split("----")]
    email = parts[0]
    if not email or "@" not in email:
        return None
    entry = {"email": email, "password": "", "client_id": "",
             "refresh_token": "", "imap_host": "", "imap_port": 0}
    if len(parts) >= 4:
        entry["password"] = parts[1]
        entry["client_id"] = parts[2]
        entry["refresh_token"] = parts[3]
    elif len(parts) == 3:
        entry["password"] = parts[1]
        entry["refresh_token"] = parts[2]
    elif len(parts) == 2:
        entry["password"] = parts[1]
    return entry


def load_pool(path: str = "") -> list[dict]:
    """读邮箱池文件；文件不存在/为空返回 []（不抛异常）。"""
    fp = path or pool_path()
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    seen: set = set()
    for line in raw.splitlines():
        e = parse_pool_line(line)
        if e and e["email"].lower() not in seen:
            seen.add(e["email"].lower())
            out.append(e)
    return out


def open_pooled_mailbox(entry: dict,
                        cfg: Optional[dict] = None,
                        log: Optional[Callable[[str, str], None]] = None,
                        ) -> OutlookMailbox:
    """用邮箱池里的一条记录建 OutlookMailbox（不注册、不联网建箱）。"""
    cfg = cfg or {}
    email = entry["email"]
    proxy = _resolve_proxy(cfg)
    from outlook_auto_register.service.resource.proxy.proxy_utils import parse_proxy
    _pc = parse_proxy(proxy) if proxy else None
    proxy_url = _pc.url if _pc else ""

    rt = entry.get("refresh_token") or ""
    if rt:
        client_id = entry.get("client_id") or _env("OUTLOOK_DEFAULT_CLIENT_ID", "")
        try:
            mode = _detect_mail_mode(email, rt, proxy_url)
        except Exception as exc:  # noqa: BLE001 探测失败也先按 graph 交给 poll_code 再试
            if log:
                log("warn", "[outlook] 令牌探测未通过（%s），先按 graph 试读" % str(exc)[:100])
            mode = "graph"
    else:
        client_id = ""
        mode = "imap"
    mb = OutlookMailbox(
        email=email,
        password=entry.get("password", ""),
        client_id=client_id,
        refresh_token=rt,
        mail_mode=mode,
        proxy_url=proxy_url,
        imap_host=entry.get("imap_host", ""),
        imap_port=entry.get("imap_port", 0),
        source="pool",
    )
    if log:
        log("info", "[outlook] 邮箱池取号 %s（mode=%s）" % (email, mode))
    return mb


# ---------------------------------------------------------------- 收信自检（可选）
def read_all_codes(mb: OutlookMailbox, limit: int = 5) -> list[dict]:
    """调试用：列出邮箱最近邮件（不改动状态）。"""
    from outlook_auto_register.service.account import graph_mail
    return graph_mail.list_messages(mb.refresh_token, mode=mb.mail_mode,
                                    top=limit, folder="inbox",
                                    proxy_url=mb.proxy_url)
