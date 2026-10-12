# -*- coding: utf-8 -*-
"""Dola 账号自动注册服务（register_service.py）。

注册引擎移植自 ``dola_probe``（gui.py 的 ``do_register`` / ``fetch_code``，零第三方依赖）：
  发码(/passport/web/email/send_code/ type=16) -> tempmail.plus 收码
  -> /passport/web/email/register_verify_login/ -> 补 ttwid/slardar 风控 cookie -> CK

注册成功后把 Cookie 头文本交给 ``cookie_login.import_cookie_accounts_from_text``
自动导入号池（按 sessionid 去重、自动命名 acc<N>、可选登录态验证），实现
「注册好之后自动导入账号列表」。

代理：默认回落 config.PROXY（.env.local -> IPRocket JP 粘性会话）。
本副本的 dola_probe.Client 已支持 socks5://user:pass@host:port 账密认证。
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import random
import socket
import string
import threading
import time
import urllib.parse
from typing import Any, Callable, Optional

import dola_probe as dp
import config
import outlook_mail

MAIL_DOMAINS = ["mailto.plus", "fexpost.com", "fexbox.org", "fexbox.net", "tempmail.plus"]

# 邮箱来源：tempmail      = 原公开临时邮箱（先有地址、轮询公开收件 API）；
#          outlook       = 现造 Outlook 邮箱（纯协议注册 + Graph 读信），见 outlook_mail.py
#          outlook_pool  = 已有 Outlook 邮箱池（读根目录 outlook_mailboxes.txt，不注册、不建箱）
MAIL_SOURCES = ["tempmail", "outlook", "outlook_pool"]

MAX_LOGS = 400
MAX_RESULTS = 200
CK_BACKUP_DIR = "registered_ck"


def rnd_local(n: int = 10) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits)
                   for _ in range(n))


def _proxy_alive(proxy: str, timeout: float = 1.5) -> bool:
    """探测代理端口能否建立 TCP 连接（只看端口，不做协议握手）。

    用途：Clash 换混合端口（7897 -> 7890）后，旧配置会让每个账号白跑 5 次
    socks 重试、几十秒后才报「发码失败 [None]」。开机探一次即可快速回落。
    """
    try:
        p = proxy if "://" in proxy else "http://" + proxy
        u = urllib.parse.urlparse(p)
        if not u.hostname or not u.port:
            return False
        with socket.create_connection((u.hostname, u.port), timeout=timeout):
            return True
    except Exception:
        return False


# ---------------------------------------------------------------- 收码
def fetch_code(cli, email: str, wait: int, stop: threading.Event):
    """轮询 tempmail.plus 收验证码（移植 gui.fetch_code）。"""
    deadline = time.time() + wait
    seen: set = set()
    while time.time() < deadline:
        if stop.is_set():
            return None
        st, body, _, _ = cli.get("%s?email=%s&limit=5"
                                 % (dp.TEMPMAIL_API, urllib.parse.quote(email)))
        if st == 200:
            try:
                for m in (json.loads(body).get("mail_list") or []):
                    mid = m.get("mail_id") or m.get("id")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    subj = m.get("subject") or ""
                    code = dp.extract_code(subj)
                    if not code:
                        st2, b2, _, _ = cli.get("%s/%s?email=%s"
                                                % (dp.TEMPMAIL_API, mid,
                                                   urllib.parse.quote(email)))
                        try:
                            j2 = json.loads(b2)
                            code = dp.extract_code((j2.get("text") or "") + " "
                                                   + (j2.get("subject") or ""))
                        except Exception:
                            code = dp.extract_code(b2)
                    if code:
                        return code
            except Exception:
                pass
        time.sleep(3)
    return None


# ---------------------------------------------------------------- 注册单个
def register_one(proxy: str, email: str, password: Optional[str],
                 wait: int, timeout: int, stop: threading.Event,
                 log: Callable[[str, str], None],
                 mailbox=None) -> tuple[Optional[dict], Optional[str]]:
    """注册单个账号。返回 (row, err)。成功时 row["cookie"] 为 Cookie 头文本。

    ``mailbox`` 非空时（Outlook 来源）用它的 poll_code 收码，否则走原 tempmail.plus。
    """
    cli = dp.Client(proxy=proxy, timeout=timeout)
    try:
        cli.get(dp.BASE_HOST + "/chat/")
    except Exception:
        pass

    st, b, _, _ = cli.get(dp._url("/passport/web/email/send_code/",
                                  {"email": email, "type": "16"}))
    if "success" not in (b or ""):
        ec = dp.parse_ec(b)
        return None, "发码失败 [%s] %s" % (ec["error_code"], ec["description"])
    log("ok", "验证码已发送 -> %s" % email)

    if mailbox is not None:
        code = mailbox.poll_code(wait, stop, log)
    else:
        code = fetch_code(cli, email, wait, stop)
    if stop.is_set():
        return None, "已停止"
    if not code:
        return None, "未收到验证码（等 %ss）" % wait
    log("hl", "验证码: %s" % code)

    payload = dict(dp.BASE_PARAMS)
    payload.update({"email": email, "type": "16", "code": code})
    if password:
        payload["password"] = password
    st, b, _, _ = cli.post(dp.BASE_HOST + dp.REGISTER_PATH, payload)
    try:
        dat = (json.loads(b) or {}).get("data") or {}
    except Exception:
        dat = {}
    uid = dat.get("user_id_str")
    if not uid:
        ec = dp.parse_ec(b)
        return None, "注册失败 [%s] %s" % (ec["error_code"], ec["description"])

    # 带登录态回访页面，补齐 ttwid / slardar 等风控 cookie
    for u in (dp.BASE_HOST + "/", dp.BASE_HOST + "/chat/"):
        try:
            cli.get(u)
        except Exception:
            pass

    cks = cli.cookies()
    # 补全到「网页授权样本」字段集（真实抓取优先，联网失败自动降级为本地派生；
    # 见 ck_enrich.py。副作用只增字段，不动已有登录态。）
    try:
        from ck_enrich import enrich, active_mode
        if active_mode() != "off":
            cks = enrich(cks, proxy=proxy, timeout=timeout, log=log)
    except Exception as e:  # 补全失败绝不影响注册主流程
        try:
            log("hl", "CK 补全跳过: %r" % e)
        except Exception:
            pass
    # 浏览器补真：flow_cur_user_sec_id / s_v_web_id 只有页面里的 passport SDK 会写
    # （请求带 sign/msToken/a_bogus，协议侧不可复现，见 ck_browser.py）。
    # 实测该值「同账号恒定、异账号不同」，是稳定的真凭据，值得为它多跑一次页面。
    got_browser = []
    try:
        import ck_browser
        if ck_browser.touch_enabled():
            ck_browser.set_logger(log)
            got = ck_browser.harvest(cks, proxy=proxy, timeout=90.0)
            for k, v in got.items():
                if v and k in ck_browser.HARVEST_KEYS:
                    cks[k] = v
            got_browser = sorted(got)
    except Exception as e:  # 补真失败绝不影响注册主流程
        try:
            log("hl", "CK 浏览器补真跳过: %r" % e)
        except Exception:
            pass
    ckstr = "; ".join("%s=%s" % (k, v) for k, v in cks.items())

    return {
        "email": email,
        "password": password or "",
        "user_id": uid,
        "name": dat.get("name") or dat.get("screen_name") or "",
        "cookies": len(cks),
        "cookie": ckstr,
        "ck_browser": got_browser,
        "time": time.strftime("%H:%M:%S"),
    }, None


def _backup_ck(row: dict) -> str:
    """CK 落盘备份（registered_ck/cookie_<邮箱前缀>_<user_id>.txt），失败不影响主流程。"""
    try:
        os.makedirs(CK_BACKUP_DIR, exist_ok=True)
        path = os.path.join(CK_BACKUP_DIR, "cookie_%s_%s.txt"
                            % (row["email"].split("@")[0], row.get("user_id") or "x"))
        with open(path, "w", encoding="utf-8") as f:
            f.write(row["cookie"])
        return path
    except Exception:
        return ""


async def _import_one(pool, row: dict, auto_verify: bool) -> tuple[dict, Optional[str]]:
    """把注册产出的 Cookie 头文本导入号池，回写邮箱；返回 (导入结果, 账号名)。"""
    from cookie_login import import_cookie_accounts_from_text
    res = await import_cookie_accounts_from_text(
        pool, row["cookie"], name_prefix="acc", require_login=auto_verify)
    created = res.get("created") or []
    name = created[0] if created else None
    if name:
        try:
            pool.set_email(name, row["email"])
        except Exception:
            pass
    return res, name


# ---------------------------------------------------------------- 管理器
class RegistrationManager:
    """多线程并发注册：N 个 worker 线程跑注册，事件循环里串行化导入入池。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._import_lock = asyncio.Lock()
        self.running = False
        self.total = 0
        self.done = 0
        self.ok = 0
        self.fail = 0
        self.logs: list[dict] = []
        self.results: list[dict] = []

    # -- 日志 --
    def log(self, lv: str, msg: str) -> None:
        with self._log_lock:
            self.logs.append({"t": time.strftime("%H:%M:%S"), "lv": lv, "msg": msg})
            if len(self.logs) > MAX_LOGS:
                del self.logs[: len(self.logs) - MAX_LOGS]

    # -- 对外 --
    def start(self, cfg: dict, pool, loop: asyncio.AbstractEventLoop) -> bool:
        with self._lock:
            if self.running:
                return False
            self._stop = threading.Event()
            self.running = True
            self.total = int(cfg.get("count") or 1)
            self.done = 0
            self.ok = 0
            self.fail = 0
            self.logs = []
            self.results = []
            self._thread = threading.Thread(
                target=self._run, args=(cfg, pool, loop), daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {
            "running": self.running,
            "total": self.total,
            "done": self.done,
            "ok": self.ok,
            "fail": self.fail,
            "logs": self.logs[-120:],
            "results": self.results[-MAX_RESULTS:],
        }

    # -- 线程体 --
    def _run(self, cfg: dict, pool, loop) -> None:
        try:
            self._run_inner(cfg, pool, loop)
        except Exception as exc:  # 兜底，绝不让线程静默死亡
            self.log("err", "注册线程异常: %s" % str(exc)[:300])
        finally:
            self.running = False
            self.log("info", "注册任务结束：成功 %d · 失败 %d" % (self.ok, self.fail))

    def _run_inner(self, cfg: dict, pool, loop) -> None:
        # 代理回落链：请求参数 > 注册专用 DOLA_REGISTER_PROXY > 全局 PROXY
        proxy = (cfg.get("proxy") or "").strip()
        if not proxy:
            proxy = (getattr(config, "REGISTER_PROXY", "") or "").strip()
        # 端口探活：连不上（Clash 换了混合端口 / 代理没启动）就直接回落全局 PROXY，
        # 否则每个号都要白等 5 次 socks 重试才报「发码失败 [None]」。
        if proxy and not _proxy_alive(proxy):
            alt = (getattr(config, "PROXY", "") or "").strip()
            if alt and alt != proxy and _proxy_alive(alt):
                self.log("warn", "注册代理 %s 端口不可达，已回落 %s"
                         % (proxy[:60], alt[:60]))
                proxy = alt
            else:
                self.log("err", "注册代理 %s 端口不可达，且无可用回落（检查代理是否在运行）"
                         % proxy[:60])
        if not proxy:
            proxy = (getattr(config, "PROXY", "") or "").strip()
        count = max(1, int(cfg.get("count") or 1))
        conc = max(1, min(8, int(cfg.get("concurrency") or 1)))
        # 注意不能用 `or 5`：gap 合法值为 0，会被 or 短路成 5
        gap_raw = cfg.get("gap")
        gap = max(0, int(gap_raw)) if gap_raw is not None else 5
        wait = max(10, int(cfg.get("wait") or 90))
        timeout = max(10, int(cfg.get("timeout") or 25))
        prefix = (cfg.get("email_prefix") or "").strip()
        domain = (cfg.get("domain") or MAIL_DOMAINS[0]).strip()
        set_pw = bool(cfg.get("set_password"))
        password = (cfg.get("password") or "").strip()
        auto_verify = bool(cfg.get("auto_verify", True))

        # 邮箱来源：outlook = 现造邮箱（register_service 里现注册 Outlook 号）；
        # 其余 = 原公开临时邮箱（先有地址、轮询收码）
        mail_source = (cfg.get("mail_source") or "").strip().lower()
        if mail_source not in MAIL_SOURCES:
            mail_source = ("outlook" if domain == outlook_mail.SOURCE else "tempmail")
        if mail_source in ("outlook", "outlook_pool") and not outlook_mail.available():
            self.log("err", "Outlook 邮箱后端不可用：%s"
                     % (outlook_mail.unavailable_reason() or "未知"))
            return

        # outlook_pool：先从池文件取号（取多少条就注册多少个，不现造）
        pool_mailboxes: list = []
        if mail_source == "outlook_pool":
            pool_mailboxes = outlook_mail.load_pool()
            if not pool_mailboxes:
                self.log("err", "邮箱池为空或文件不存在：%s（一行一个 "
                                "email----password----client_id----refresh_token）"
                         % outlook_mail.pool_path())
                return
            if len(pool_mailboxes) < count:
                self.log("warn", "邮箱池只有 %d 条，少于请求的 %d 个，按 %d 个执行"
                         % (len(pool_mailboxes), count, len(pool_mailboxes)))
                count = len(pool_mailboxes)

        self.log("hl", "开始注册：共 %d 个 · 并发 %d · 来源 %s · 域名 %s · %s · 代理 %s"
                 % (count, min(conc, count),
                    {"outlook": "Outlook 现造邮箱",
                     "outlook_pool": "已有 Outlook 邮箱池"}.get(mail_source, "临时邮箱"),
                    domain if mail_source == "tempmail" else (cfg.get("outlook_domain") or "@outlook.com"),
                    "设密码" if (set_pw and password) else "不设密码",
                    (proxy[:40] + "...") if len(proxy) > 40 else (proxy or "(未配置)")))
        if set_pw and not password:
            self.log("err", "勾选了设密码但密码为空，按不设密码执行")

        if mail_source == "outlook":
            # Outlook 邮箱由注册链路现造，地址此时未知：任务只带序号，worker 里先建邮箱
            self.log("info", "Outlook 每条邮箱需现造（约 1~3 分钟/个，含打码），请耐心等待")
            task_items = [(i, None) for i in range(count)]
        elif mail_source == "outlook_pool":
            # 池里一条记录 = 一个待注册邮箱；email 已知，收码走池内令牌
            task_items = [(i, pool_mailboxes[i]) for i in range(count)]
        else:
            # 预生成全部邮箱：前缀模式下编号一次性分配，避免并发重号
            task_items = [(i, (("%s%03d" % (prefix, i + 1)) if prefix else rnd_local())
                           + "@" + domain) for i in range(count)]
        task_q: queue.Queue = queue.Queue()
        for i, em in task_items:
            task_q.put((i, em))

        stat_lock = threading.Lock()
        pw = password if (set_pw and password) else None
        # 入池在事件循环里串行化，避免并发导入时 acc<N> 命名冲突
        self._import_lock = asyncio.Lock()

        def worker() -> None:
            while not self._stop.is_set():
                try:
                    i, payload = task_q.get_nowait()
                except queue.Empty:
                    return
                email = payload if isinstance(payload, str) else ""
                try:
                    mb = None
                    if mail_source == "outlook":
                        self.log("info", "[%d/%d] 现造 Outlook 邮箱…" % (i + 1, count))
                        mb = outlook_mail.open_mailbox(cfg, self.log)
                        email = mb.email
                    elif mail_source == "outlook_pool":
                        self.log("info", "[%d/%d] 取邮箱池账号…" % (i + 1, count))
                        mb = outlook_mail.open_pooled_mailbox(payload, cfg, self.log)
                        email = mb.email
                    self._register_and_import(i, count, email, proxy, pw, wait,
                                              timeout, auto_verify, pool, loop,
                                              stat_lock, mailbox=mb)
                except Exception as exc:  # 单账号异常不拖垮整个 worker
                    self.log("err", "[%s] 任务异常: %s"
                             % (email or ("#%d" % (i + 1)), str(exc)[:200]))
                finally:
                    task_q.task_done()
                # 同一 worker 相邻任务间保留间隔；队列已空则不必再等
                if gap > 0 and not task_q.empty() and not self._stop.is_set():
                    for _ in range(gap):
                        if self._stop.is_set():
                            break
                        time.sleep(1)

        threads = [threading.Thread(target=worker, daemon=True, name="reg-w%d" % w)
                   for w in range(min(conc, count))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _register_and_import(self, i: int, count: int, email: str, proxy: str,
                             password: Optional[str], wait: int, timeout: int,
                             auto_verify: bool, pool, loop,
                             stat_lock: threading.Lock, mailbox=None) -> None:
        """注册单个账号并导入号池（在 worker 线程里执行）。"""
        self.log("info", "[%d/%d] 注册 %s" % (i + 1, count, email))
        row, err = register_one(proxy, email, password, wait, timeout, self._stop,
                                self.log, mailbox=mailbox)
        with stat_lock:
            self.done += 1
        if not row:
            with stat_lock:
                self.fail += 1
                self.results.append({"email": email, "user_id": "", "password": "",
                                     "cookies": 0, "imported": "", "status": "fail",
                                     "error": err or "", "time": time.strftime("%H:%M:%S")})
            self.log("err", "[%s] %s" % (email, err))
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._import_one_locked(pool, row, auto_verify), loop)
            res, name = fut.result(300)
            with stat_lock:
                self.ok += 1
            backup = _backup_ck(row)
            if name:
                self.log("ok", "%s 注册成功 -> 已导入 %s（user_id=%s，CK 备份 %s）"
                         % (email, name, row["user_id"], backup or "失败"))
            else:
                self.log("err", "%s 注册成功但未新增账号（可能 sessionid 重复）: %s"
                         % (email, json.dumps(res, ensure_ascii=False)[:160]))
            with stat_lock:
                self.results.append({
                    "email": row["email"], "user_id": row["user_id"],
                    "password": row["password"], "cookies": row["cookies"],
                    "imported": name or "", "status": "ok" if name else "dup",
                    "error": "", "time": row["time"]})
        except Exception as exc:
            with stat_lock:
                self.fail += 1
                self.results.append({"email": row["email"], "user_id": row["user_id"],
                                     "password": row["password"], "cookies": row["cookies"],
                                     "imported": "", "status": "import_fail",
                                     "error": str(exc)[:200], "time": row["time"]})
            self.log("err", "%s 导入号池失败: %s" % (email, str(exc)[:200]))

    async def _import_one_locked(self, pool, row: dict, auto_verify: bool):
        async with self._import_lock:
            return await _import_one(pool, row, auto_verify)


REG = RegistrationManager()
