# -*- coding: utf-8 -*-
"""本地 catch-all 收件站：零依赖的「自建网页收件箱」。

用途：给 Google 账号注册骨架（google_register.py）提供真实可收信的邮箱端点。
自建产线的两种收信姿势：

  A. 自有域名 MX 直指本机（需公网 IP + 25 端口可达）：
       域名 DNS: MX  ->  本机公网 IP
       启动:    python mail_server.py --smtp-port 25 --http-port 8010
       注册:    MAIL_BACKEND=http \
                MAIL_HTTP_URL="http://<本机IP>:8010/api/inbox?to={addr}"

  B. 域名托管在 Cloudflare（Email Routing 免费，但只能转发到"已存在"的邮箱）：
       catch-all -> 转发到某 IMAP 邮箱 -> 用骨架的 imap 后端。见文档 §4。

本模块只做三件事，绝不外发、不改动别的邮件：
  1) 内置 SMTP 服务器（socketserver，纯标准库）接收任意 RCPT TO 的来信；
  2) 内存按收件地址归档（可 --dump-dir 落盘原始 .eml）；
  3) HTTP GET /api/inbox?to=<addr> 返回 JSON **顶层数组** —— 与
     mail_catcher.HttpCatcher 的解析约定完全对齐（它会把 list 里每条的
     subject/text/body/content 拼起来抽码）。

安全：默认只监听 127.0.0.1。要对外收信显式 --host 0.0.0.0；建议同时给 --token，
HTTP 侧会校验 `?token=` 或 `Authorization: Bearer`。SMTP 侧不做认证（公网收信本就无认证），
请自行用防火墙只放行 25 端口。
"""
from __future__ import annotations

import argparse
import email
import json
import os
import socketserver
import threading
import time
from email.header import decode_header
from email.policy import default as _default_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 收件箱：addr(lower) -> [item, ...]；item = {from,to,subject,text,ts,size}
STORE: dict[str, list[dict]] = {}
_LOCK = threading.Lock()

DUMP_DIR = ""          # 非空则把原始 .eml 落盘
HTTP_TOKEN = ""        # 非空则 HTTP 侧要求 token
MAX_PER_ADDR = 200     # 单地址封顶，防内存膨胀


def _norm(a: str) -> str:
    """归一化收件地址：去掉尖括号与空白，转小写。"""
    return (a or "").strip().strip("<>").strip().lower()


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


def _extract_text(raw: bytes) -> tuple[str, str]:
    """返回 (subject, 正文文本)。优先 text/plain，含 text/html 一并拼接。"""
    try:
        msg = email.message_from_bytes(raw, policy=_default_policy)
    except Exception:
        return "", ""
    subject = _decode_hdr(str(msg.get("Subject", "")))
    chunks: list[str] = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for p in parts:
        if p.get_content_type() not in ("text/plain", "text/html"):
            continue
        try:
            payload = p.get_payload(decode=True) or b""
            chunks.append(payload.decode(p.get_content_charset() or "utf-8", "replace"))
        except Exception:
            continue
    return subject, "\n".join(c for c in chunks if c.strip())


def _store(mail_from: str, rcpt: list[str], raw: bytes) -> None:
    subject, text = _extract_text(raw)
    item = {
        "from": _norm(mail_from),
        "to": [_norm(r) for r in rcpt],
        "subject": subject,
        "text": text,
        "ts": time.time(),
        "size": len(raw),
    }
    with _LOCK:
        for r in rcpt:
            key = _norm(r)
            if not key:
                continue
            box = STORE.setdefault(key, [])
            box.append(item)
            if len(box) > MAX_PER_ADDR:
                del box[: len(box) - MAX_PER_ADDR]
    if DUMP_DIR:
        try:
            stamp = time.strftime("%H%M%S", time.localtime())
            fn = os.path.join(DUMP_DIR, "%s_%s.eml" % (stamp, (rcpt[0] if rcpt else "x").replace("@", "_at_")))
            with open(fn, "wb") as f:
                f.write(raw)
        except Exception:
            pass


# --------------------------------------------------------------------- SMTP
class _ClientGone(Exception):
    """SMTP 客户端提前断开（QUIT 后立刻 close）。"""


class SmtpHandler(socketserver.StreamRequestHandler):
    timeout = 120

    def _say(self, s: str) -> None:
        try:
            self.wfile.write((s + "\r\n").encode("ascii", "replace"))
            self.wfile.flush()
        except Exception:
            # 客户端已断开（QUIT 后立刻 close 很常见）——静默即可
            raise _ClientGone()

    def handle(self) -> None:
        try:
            self._session()
        except _ClientGone:
            pass
        except Exception:
            pass

    def _session(self) -> None:
        self._say("220 local-catchall ESMTP ready")
        mail_from = ""
        rcpt: list[str] = []
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").rstrip("\r\n")
            up = cmd.upper()
            if up.startswith("EHLO"):
                self._say("250-localhost")
                self._say("250 SIZE 52428800")
            elif up.startswith("HELO"):
                self._say("250 localhost")
            elif up.startswith("MAIL FROM:"):
                mail_from = cmd.split(":", 1)[1].strip()
                self._say("250 2.1.0 OK")
            elif up.startswith("RCPT TO:"):
                rcpt.append(cmd.split(":", 1)[1].strip())
                self._say("250 2.1.5 OK")
            elif up.startswith("DATA"):
                self._say("354 End data with <CR><LF>.<CR><LF>")
                raw = self._read_data()
                try:
                    _store(mail_from, rcpt, raw)
                except Exception:
                    pass
                self._say("250 2.0.0 OK queued")
                mail_from, rcpt = "", []
            elif up.startswith("RSET"):
                mail_from, rcpt = "", []
                self._say("250 2.0.0 OK")
            elif up.startswith("NOOP"):
                self._say("250 2.0.0 OK")
            elif up.startswith("QUIT"):
                self._say("221 2.0.0 Bye")
                return
            else:
                self._say("250 2.0.0 OK")

    def _read_data(self) -> bytes:
        """读 DATA 段直到单独一行的 '.'，并解除 dot-stuffing。"""
        buf: list[bytes] = []
        while True:
            line = self.rfile.readline()
            if not line:
                break
            if line in (b".\r\n", b".\n"):
                break
            if line.startswith(b".."):
                line = line[1:]
            buf.append(line)
        return b"".join(buf)


class SmtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# --------------------------------------------------------------------- HTTP
class HttpHandler(BaseHTTPRequestHandler):
    server_version = "LocalCatchall/1.0"

    def log_message(self, *a):        # 静音，避免刷屏
        pass

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self, q: dict) -> bool:
        if not HTTP_TOKEN:
            return True
        tok = (q.get("token", [""])[0] or "").strip()
        if tok == HTTP_TOKEN:
            return True
        hdr = self.headers.get("Authorization", "")
        return hdr.strip() == "Bearer " + HTTP_TOKEN

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/api/inbox", "/inbox"):
            if not self._authed(q):
                self._json(401, {"error": "unauthorized"})
                return
            to = _norm(q.get("to", [""])[0])
            try:
                limit = int(q.get("limit", ["20"])[0] or 20)
            except Exception:
                limit = 20
            with _LOCK:
                msgs = list(STORE.get(to, []))[-limit:]
            # 顶层数组：与 HttpCatcher 的 list 分支对齐（拼 subject/text/body/content 抽码）
            self._json(200, msgs)
            return
        if u.path in ("/", "/health"):
            with _LOCK:
                n_addr = len(STORE)
                n_msg = sum(len(v) for v in STORE.values())
            self._json(200, {"ok": True, "addresses": n_addr, "messages": n_msg,
                             "smtp_port": SMTP_PORT, "http_port": HTTP_PORT})
            return
        if u.path == "/addresses":
            if not self._authed(q):
                self._json(401, {"error": "unauthorized"})
                return
            with _LOCK:
                self._json(200, sorted(STORE.keys()))
            return
        self._json(404, {"error": "not found"})


class HttpServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


# ------------------------------------------------------------------- 启动
SMTP_PORT = 2525
HTTP_PORT = 8010


def start_background(host: str = "127.0.0.1", smtp_port: int = 2525,
                     http_port: int = 8010, token: str = "",
                     dump_dir: str = "") -> tuple[SmtpServer, HttpServer]:
    """在后台线程起 SMTP + HTTP，返回两个 server（用于自检/同进程调用）。"""
    global SMTP_PORT, HTTP_PORT, HTTP_TOKEN, DUMP_DIR
    SMTP_PORT, HTTP_PORT, HTTP_TOKEN = smtp_port, http_port, token
    DUMP_DIR = dump_dir
    if dump_dir:
        os.makedirs(dump_dir, exist_ok=True)
    smtp_srv = SmtpServer((host, smtp_port), SmtpHandler)
    http_srv = HttpServer((host, http_port), HttpHandler)
    threading.Thread(target=smtp_srv.serve_forever, daemon=True).start()
    threading.Thread(target=http_srv.serve_forever, daemon=True).start()
    return smtp_srv, http_srv


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 catch-all 收件站（SMTP + HTTP）")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（对外收信用 0.0.0.0）")
    ap.add_argument("--smtp-port", type=int, default=2525, help="SMTP 端口（公网直收用 25）")
    ap.add_argument("--http-port", type=int, default=8010, help="HTTP 查询端口")
    ap.add_argument("--token", default="", help="HTTP 访问令牌（留空则不校验）")
    ap.add_argument("--dump-dir", default="", help="把原始 .eml 落盘到此目录")
    a = ap.parse_args()
    smtp_srv, http_srv = start_background(a.host, a.smtp_port, a.http_port,
                                          a.token, a.dump_dir)
    print("收件站已启动：")
    print("  SMTP  %s:%d   （把域名 MX 指向本机即可全收 *@域）" % (a.host, a.smtp_port))
    print("  HTTP  http://%s:%d/api/inbox?to={addr}" % (a.host, a.http_port))
    print("  令牌  %s" % (a.token or "(不校验)"))
    print("\n注册机用法：")
    print('  MAIL_BACKEND=http MAIL_HTTP_URL="http://%s:%d/api/inbox?to={addr}"%s'
          % (a.host, a.http_port, (" MAIL_HTTP_TOKEN=" + a.token) if a.token else ""))
    print("\nCtrl+C 退出。")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n关闭中…")
    finally:
        smtp_srv.shutdown()
        http_srv.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
