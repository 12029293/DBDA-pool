# -*- coding: utf-8 -*-
"""收件后端：为 Google 账号注册提供「任意前缀 @ 自建 catch-all 域名」的验证码收取。

三种后端（由环境变量 MAIL_BACKEND 选择）：
  imap     自建 catch-all 域名邮箱（IMAP4_SSL）。同一账号收全域名来信，
           按 To / Delivered-To / X-Original-To 过滤出目标地址，抽码。
           —— 自建产线的**推荐后端**（自有域名 + 邮件托管即可）。
  http     自建网页收件站 API（如 google111111.asia 那类）：
           GET <MAIL_HTTP_URL>（内含 {addr} 占位），返回 JSON 或纯文本，抽码。
  tempmail tempmail.plus 公开临时邮箱（仅演示；**不支持自有域名**）。

统一接口：
    mb = MailCatcher.from_env()
    code = mb.wait_code("wx0e8f@izqz.kdns.fr", timeout=180)

铁律：本模块**只收信、只抽码**，绝不寄送任何邮件，也不改邮箱内其它邮件。
"""
from __future__ import annotations

import email
import imaplib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from email.header import decode_header

# Google 的验证码是 6 位数字（少数场景 8 位，形如 G-123456）。
_CODE_STD = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_CODE_G = re.compile(r"G-(\d{6})", re.I)


def extract_code(text: str) -> str:
    """从邮件正文/主题里抽 Google 验证码。优先「验证码」上下文，兜底全局 6 位数字。"""
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", text)          # 去 HTML 标签
    # 屏蔽邮箱地址与链接里的数字：否则 probe712989@mailto.plus 里的 712989
    # 会被兜底规则当成验证码（无关键词的邮件必踩）。
    t = re.sub(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+", " <mail> ", t)
    t = re.sub(r"(?:https?://|www\.)\S+", " <url> ", t)
    t = re.sub(r"\s+", " ", t)
    m = _CODE_G.search(t)
    if m:
        return m.group(1)
    # 带关键词的上下文优先
    for kw in ("code", "verification", "verify", "验证码", "認証コード", "コード"):
        for mm in re.finditer(kw, t, re.I):
            seg = t[max(0, mm.start() - 40): mm.end() + 60]
            m2 = _CODE_STD.search(seg)
            if m2:
                return m2.group(1)
    m3 = _CODE_STD.search(t)
    return m3.group(1) if m3 else ""


def _decode_hdr(v: str) -> str:
    if not v:
        return ""
    out = []
    for part, enc in decode_header(v):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", "replace"))
        else:
            out.append(part)
    return "".join(out)


def _mail_text(msg) -> str:
    """把一封邮件正文拼成纯文本（优先 text/plain，其次 text/html）。"""
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                chunks.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
            except Exception:
                continue
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            chunks.append(payload.decode(msg.get_content_charset() or "utf-8", "replace"))
        except Exception:
            chunks.append(str(msg.get_payload()))
    plain = [c for c in chunks if c.strip()]
    return "\n".join(plain)


class MailCatcher:
    """基类：统一 wait_code 接口。"""

    name = "base"

    def prime(self, addr: str = "") -> None:
        """记录「只看这之后的邮件」基线（在触发发码**之前**调用）。默认无操作。"""
        return None

    def wait_code(self, addr: str, timeout: int = 180, interval: float = 5.0,
                  seen: set | None = None) -> str:
        raise NotImplementedError

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls) -> "MailCatcher":
        backend = (os.environ.get("MAIL_BACKEND") or "imap").strip().lower()
        if backend == "imap":
            return ImapCatcher.from_env()
        if backend == "http":
            return HttpCatcher.from_env()
        if backend == "tempmail":
            return TempmailCatcher.from_env()
        raise ValueError("未知 MAIL_BACKEND=%r（可选 imap / http / tempmail）" % backend)


# ---------------------------------------------------------------------- IMAP
class ImapCatcher(MailCatcher):
    """自建 catch-all 域名邮箱（IMAP4_SSL）。

    环境变量：
      MAIL_IMAP_HOST  如 imap.qq.com / outlook.office365.com / 自建
      MAIL_IMAP_PORT  默认 993
      MAIL_IMAP_USER  登录账号（catch-all 的收信账号）
      MAIL_IMAP_PASS  密码 / 应用专用密码
      MAIL_IMAP_BOX   邮箱文件夹，默认 INBOX
      MAIL_IMAP_INSECURE 1=跳过证书校验（不推荐）
      MAIL_IMAP_NO_BASELINE 1=关闭「只认新邮件」基线（默认开启）

    基线：首次连接时把当前最大 UID 记下，之后只看 UID 更大的新邮件 —— 防止
    抽到历史邮件的旧验证码。故**正确时序是：先构造 catcher（或先 prime），再触发发码**。
    若你想连历史邮件一起看，设 MAIL_IMAP_NO_BASELINE=1。
    """

    name = "imap"

    def __init__(self, host, port, user, password, box="INBOX", insecure=False,
                 no_baseline=False):
        self.host, self.port = host, int(port)
        self.user, self.password = user, password
        self.box = box
        self.insecure = insecure
        self.no_baseline = no_baseline
        self._baseline = None
        self._junk_cache = None            # 垃圾箱类文件夹缓存
        self._primed_at = 0.0              # prime 时刻（懒基线时按时间兜底判新邮件）
        self.on_poll = None                # 可选回调 on_poll(轮次, 最近错误)，用于外部打日志

    @classmethod
    def from_env(cls) -> "ImapCatcher":
        host = os.environ.get("MAIL_IMAP_HOST", "").strip()
        user = os.environ.get("MAIL_IMAP_USER", "").strip()
        password = os.environ.get("MAIL_IMAP_PASS", "").strip()
        if not (host and user and password):
            raise ValueError(
                "IMAP 后端缺少配置：需要 MAIL_IMAP_HOST / MAIL_IMAP_USER / MAIL_IMAP_PASS"
            )
        return cls(host, os.environ.get("MAIL_IMAP_PORT", "993"), user, password,
                   os.environ.get("MAIL_IMAP_BOX", "INBOX"),
                   os.environ.get("MAIL_IMAP_INSECURE", "0") == "1",
                   os.environ.get("MAIL_IMAP_NO_BASELINE", "0") == "1")

    def prime(self) -> None:
        """把当前最大 UID 记为基线（在触发发码**之前**调用最稳）。"""
        self._primed_at = time.time()
        if self.no_baseline:
            return
        try:
            M = self._conn()
            try:
                M.login(self.user, self.password)
                M.select(self.box)
                typ, d = M.search(None, "ALL")
                uids = (d[0] or b"").split() if typ == "OK" and d else []
                self._baseline = int(uids[-1]) if uids else 0
            finally:
                try:
                    M.logout()
                except Exception:
                    pass
        except Exception:
            self._baseline = None

    def _conn(self):
        # 必须带超时：imap.qq.com 连不上时无超时会卡住整个流程几十秒起步
        try:
            timeout = float(os.environ.get("MAIL_IMAP_TIMEOUT", "25") or 25)
        except Exception:
            timeout = 25.0
        if self.insecure:
            import ssl as _ssl
            ctx = _ssl._create_unverified_context()
            return imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx, timeout=timeout)
        return imaplib.IMAP4_SSL(self.host, self.port, timeout=timeout)

    def _junk_boxes(self, M) -> list:
        """从 IMAP 文件夹列表里挑出垃圾箱类文件夹（Google 来信常被 QQ 归进垃圾箱）。"""
        if self._junk_cache is not None:
            return self._junk_cache
        boxes = []
        try:
            typ, data = M.list()
            if typ == "OK" and data:
                for raw in data:
                    s = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
                    low = s.lower()
                    if "\\noselect" in low:
                        continue
                    if any(k in low for k in ("junk", "spam", "垃圾")):
                        m = re.search(r'"([^"]+)"\s*$', s.strip())
                        if m and m.group(1).upper() != "INBOX":
                            boxes.append(m.group(1))
        except Exception:
            pass
        self._junk_cache = boxes
        return boxes

    def _mail_time(self, M, uid) -> float:
        """取邮件 INTERNALDATE 的 unix 时间戳；失败返回 0。"""
        try:
            typ, d = M.fetch(uid, "(INTERNALDATE)")
            if typ == "OK" and d and d[0]:
                s = d[0].decode("utf-8", "ignore") if isinstance(d[0], bytes) else str(d[0])
                m = re.search(r'"([^"]+)"', s)
                if m:
                    try:
                        return time.mktime(time.strptime(m.group(1), "%d-%b-%Y %H:%M:%S %z"))
                    except Exception:
                        try:
                            return time.mktime(time.strptime(m.group(1)[:20], "%d-%b-%Y %H:%M:%S"))
                        except Exception:
                            return 0.0
        except Exception:
            pass
        return 0.0

    def scan_once(self, addr: str, seen: set | None = None) -> str:
        """连一次 IMAP、扫一遍主文件夹 + 垃圾箱类文件夹。

        返回验证码；没扫到返回 ""；连接/认证失败抛异常（由 wait_code 记录）。"""
        addr_l = addr.lower()
        seen = seen if seen is not None else set()
        base_lazy = False
        M = self._conn()
        try:
            M.login(self.user, self.password)
            base = self._baseline
            boxes = [self.box] + [b for b in self._junk_boxes(M) if b != self.box]
            for box in boxes:
                try:
                    typ, _ = M.select(box)
                    if typ != "OK":
                        continue
                except Exception:
                    continue
                is_primary = (box == self.box)
                if base is None and not self.no_baseline and is_primary:
                    # 基线没在 prime 时建好（IMAP 慢/超时）：收码时懒建。
                    # 懒建的基线会把「填表期间已到达的验证码」误判为旧邮件，
                    # 所以这类邮件改按 INTERNALDATE >= prime时刻-5min 兜底放行。
                    try:
                        typ0, d0 = M.search(None, "ALL")
                        u0 = (d0[0] or b"").split() if typ0 == "OK" and d0 else []
                        self._baseline = int(u0[-1]) if u0 else 0
                        base = self._baseline
                        base_lazy = True
                    except Exception:
                        self._baseline = 0
                        base = 0
                        base_lazy = True
                base = base or 0
                for crit in (["UNSEEN"], ["ALL"]):
                    typ, data = M.search(None, *crit)
                    if typ != "OK":
                        continue
                    uids = (data[0] or b"").split()
                    for uid in reversed(uids[-40:]):     # 新的在后
                        key = uid.decode() if isinstance(uid, bytes) else str(uid)
                        if key in seen:
                            continue
                        if not self.no_baseline and is_primary:
                            try:
                                if int(key) <= base:
                                    ok_time = (base_lazy and self._primed_at
                                               and self._mail_time(M, uid)
                                               >= self._primed_at - 300)
                                    if not ok_time:
                                        continue
                            except Exception:
                                pass
                        typ2, md = M.fetch(uid, "(RFC822)")
                        if typ2 != "OK" or not md or not md[0]:
                            continue
                        msg = email.message_from_bytes(md[0][1])
                        hdrs = " ".join([
                            _decode_hdr(msg.get("To", "")),
                            _decode_hdr(msg.get("Delivered-To", "")),
                            _decode_hdr(msg.get("X-Original-To", "")),
                            _decode_hdr(msg.get("Cc", "")),
                        ]).lower()
                        if addr_l not in hdrs:
                            continue
                        seen.add(key)
                        subj = _decode_hdr(msg.get("Subject", ""))
                        code = extract_code(subj + "\n" + _mail_text(msg))
                        if code:
                            return code
                    if crit == ["UNSEEN"] and seen:
                        break
            return ""
        finally:
            try:
                M.logout()
            except Exception:
                pass

    def wait_code(self, addr, timeout=180, interval=5.0, seen=None):
        addr_l = addr.lower()
        seen = seen if seen is not None else set()
        deadline = time.time() + timeout
        last_err = ""
        polled = 0
        while time.time() < deadline:
            polled += 1
            try:
                code = self.scan_once(addr, seen)
                if code:
                    return code
                last_err = ""
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, str(e)[:100])
            if callable(getattr(self, "on_poll", None)):
                try:
                    self.on_poll(polled, last_err)
                except Exception:
                    pass
            time.sleep(interval)
        raise TimeoutError("IMAP 收码超时（%ds）%s" % (timeout, ("；最近错误 " + last_err) if last_err else ""))


# ---------------------------------------------------------------------- HTTP
class HttpCatcher(MailCatcher):
    """自建网页收件站 API。

    环境变量：
      MAIL_HTTP_URL    URL 模板，含 {addr}（会 urlencode），如
                       https://mail.example.com/api/inbox?to={addr}
      MAIL_HTTP_TOKEN  可选，作为 Authorization: Bearer <token>
      MAIL_HTTP_FIELD  可选，JSON 里取文本的字段（默认自动拼接 subject+text/body）
      MAIL_HTTP_LIMIT  单次最多看几条（默认 10，取最新）

    铁律：**只认「本实例构造之后到达」的邮件**（按 ts 过滤），并**最新优先**抽码。
    否则历史旧邮件里的旧验证码会被误当本次的码用 —— 注册必失败。
    """

    name = "http"

    def __init__(self, url, token="", field="", limit=10, since_skew=2.0):
        self.url, self.token, self.field = url, token, field
        try:
            self.limit = max(1, int(limit))
        except Exception:
            self.limit = 10
        self._born = time.time()          # 基线：早于此（减去容差）的邮件一律忽略
        self._skew = since_skew

    def prime(self, addr: str = "") -> None:
        self._born = time.time()

    @classmethod
    def from_env(cls) -> "HttpCatcher":
        url = os.environ.get("MAIL_HTTP_URL", "").strip()
        if not url or "{addr}" not in url:
            raise ValueError("HTTP 后端需要 MAIL_HTTP_URL 且含 {addr} 占位符")
        return cls(url, os.environ.get("MAIL_HTTP_TOKEN", "").strip(),
                   os.environ.get("MAIL_HTTP_FIELD", "").strip(),
                   os.environ.get("MAIL_HTTP_LIMIT", "10"))

    @staticmethod
    def _ts(m: dict) -> float:
        for k in ("ts", "time", "timestamp", "date", "receivedAt", "received_at"):
            v = m.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except Exception:
                continue
        return 0.0

    def _pick(self, j) -> list:
        """把后端返回的 JSON 归一成「按时间倒序、只含新邮件」的列表。"""
        if isinstance(j, list):
            items = j
        elif isinstance(j, dict):
            if isinstance(j.get("messages"), list):
                items = j["messages"]
            elif isinstance(j.get("mail_list"), list):
                items = j["mail_list"]
            else:
                items = [j]
        else:
            return []
        items = [m for m in items if isinstance(m, dict)]
        has_ts = any(self._ts(m) > 0 for m in items)
        if has_ts:
            cutoff = self._born - self._skew
            items = [m for m in items if self._ts(m) >= cutoff]
            items.sort(key=self._ts, reverse=True)      # 最新优先
        else:
            items = list(reversed(items))               # 无时间戳则假定「新的在后」
        return items[: self.limit]

    def wait_code(self, addr, timeout=180, interval=3.0, seen=None):
        deadline = time.time() + timeout
        last_err = ""
        n_seen = 0
        while time.time() < deadline:
            try:
                url = self.url.format(addr=urllib.parse.quote(addr))
                req = urllib.request.Request(url, headers={"User-Agent": "gacc/1.0"})
                if self.token:
                    req.add_header("Authorization", "Bearer " + self.token)
                with urllib.request.urlopen(req, timeout=15) as r:
                    raw = r.read().decode("utf-8", "replace")
                try:
                    j = json.loads(raw)
                except Exception:
                    j = raw
                for m in self._pick(j):
                    n_seen += 1
                    if self.field and isinstance(m, dict):
                        text = str(m.get(self.field, ""))
                    elif isinstance(m, dict):
                        text = " ".join(str(m.get(k, "")) for k in
                                        ("subject", "text", "body", "content", "html"))
                    else:
                        text = str(m)
                    code = extract_code(text)
                    if code:
                        return code
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, str(e)[:100])
            time.sleep(interval)
        raise TimeoutError("HTTP 收码超时（%ds，已看 %d 封）%s"
                           % (timeout, n_seen, ("；最近错误 " + last_err) if last_err else ""))


# ------------------------------------------------------------------ tempmail
class TempmailCatcher(MailCatcher):
    """tempmail.plus 公开临时邮箱（**不支持自有域名**）。

    域名族：mailto.plus / fexpost.com / fexbox.org / fexbox.net / tempmail.plus
    —— dola_probe 用的就是这同一套 API（`TEMPMAIL_API`）。

    ⚠️ **公共收件箱**：知道地址即可读、不设密码 → 验证码可能被他人看到；
    且单一公共域名集中注册易被风控关联。仅适合跑通链路 / 小批量，不宜作稳定产线。

    环境变量：TEMPMAIL_LIMIT 单次最多看几封（默认 5）。
    基线：`prime(addr)` 记录该地址当前最大 mail_id，之后只看更大的 —— 防抽到旧码。
    """

    name = "tempmail"
    API = "https://tempmail.plus/api/mails"

    def __init__(self, token="", limit=5):
        self.token = token
        try:
            self.limit = max(1, int(limit))
        except Exception:
            self.limit = 5
        self._base: dict[str, int] = {}

    @classmethod
    def from_env(cls) -> "TempmailCatcher":
        return cls(os.environ.get("TEMPMAIL_TOKEN", "").strip(),
                   os.environ.get("TEMPMAIL_LIMIT", "5"))

    def _list(self, addr: str) -> list:
        url = "%s?email=%s&limit=%d" % (self.API, urllib.parse.quote(addr), self.limit)
        req = urllib.request.Request(url, headers={"User-Agent": "gacc/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        return j.get("mail_list") or []

    @staticmethod
    def _mid(m) -> int:
        try:
            return int(m.get("mail_id") or m.get("id") or 0)
        except Exception:
            return 0

    def prime(self, addr: str = "") -> None:
        """记录该地址当前最大 mail_id 作为基线（在触发发码**之前**调用）。"""
        a = (addr or "").strip().lower()
        if not a:
            return
        try:
            ids = [self._mid(m) for m in self._list(a)]
            self._base[a] = max(ids) if ids else 0
        except Exception:
            self._base[a] = self._base.get(a, 0)

    def wait_code(self, addr, timeout=180, interval=4.0, seen=None):
        a = (addr or "").strip().lower()
        seen = seen if seen is not None else set()
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            try:
                base = self._base.get(a, 0)
                msgs = [m for m in self._list(a) if self._mid(m) > base]
                msgs.sort(key=self._mid, reverse=True)      # 最新优先
                for m in msgs:
                    mid = m.get("mail_id") or m.get("id")
                    if not mid or str(mid) in seen:
                        continue
                    seen.add(str(mid))
                    code = extract_code((m.get("subject") or "") + " " + (m.get("text") or ""))
                    if not code:
                        u2 = "%s/%s?email=%s" % (self.API, mid, urllib.parse.quote(addr))
                        with urllib.request.urlopen(
                                urllib.request.Request(u2, headers={"User-Agent": "gacc/1.0"}),
                                timeout=15) as r2:
                            code = extract_code(r2.read().decode("utf-8", "replace"))
                    if code:
                        return code
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, str(e)[:90])
            time.sleep(interval)
        raise TimeoutError("tempmail 收码超时（%ds）%s"
                           % (timeout, ("；最近错误 " + last_err) if last_err else ""))


if __name__ == "__main__":
    # 自检：抽码 regex + 当前后端可用性
    print("extract_code 自检：")
    for s in ["Your Google verification code is 123456",
              "G-654321 is your Google verification code",
              "認証コード: 246810 を入力してください",
              "no digits here"]:
        print("  %-55r -> %r" % (s, extract_code(s)))
    try:
        mb = MailCatcher.from_env()
        print("当前后端: %s ✓" % mb.name)
    except Exception as e:
        print("当前后端不可用: %s" % e)
