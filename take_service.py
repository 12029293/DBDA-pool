# -*- coding: utf-8 -*-
"""面板「取号」服务：用住宅代理跑两条链路，全部产出直接落到 dola-pool。

A) **谷歌注册**（google_register）
   accounts.google.com 注册页 -> 邮箱收码 -> 设密码 -> 产出 `email----password----totp`
   （默认写 谷歌账号_产出.txt，即号池文件格式）

B) **网页登录取 dola 号**（ck_weblogin）
   拿 Google 号走 dola 真网页授权登录 -> 拿到含 passport_auth_status 的完整 CK
   -> 自动导入 accounts/ 并绑定面板默认代理（住宅）

两者都由面板后端 `asyncio.create_task` 驱动（与 server 同事件循环，不跨线程动 sqlite）。

代理：统一走「代理管理」的池默认代理（或显式 proxy_id）。
  * dynamic 型（IPRocket 住宅）：**每个号 rotate 一个新 sticky session = 一个新出口 IP**
    —— 天然「一号一IP」，且不消耗提取额度。

用到的伪账号名 `__take__` 只用来借 `proxy_store.proxy_url_for` 取一条出口，
任务结束会 `forget_account` 清掉，不会出现在账号列表。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

WORKDIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(WORKDIR))

_TAKE_ACCOUNT = "__take__"
_STOP = threading.Event()

STATE: dict = {
    "running": False, "kind": "", "started_at": 0.0, "finished_at": 0.0,
    "total": 0, "done": 0, "ok": 0, "fail": 0,
    "log": [], "results": [], "error": "",
}

GOOGLE_POOL_FILES = ["谷歌账号_产出.txt", "谷歌账号.txt"]
DEFAULT_OUT = str(WORKDIR / "谷歌账号_产出.txt")


# ------------------------------------------------------------------ 状态
def _log(msg: str) -> None:
    line = "%s %s" % (time.strftime("%H:%M:%S"), str(msg))
    STATE["log"].append(line)
    if len(STATE["log"]) > 500:
        del STATE["log"][:-400]
    try:
        print("[take]", line, flush=True)
    except Exception:
        pass


def status() -> dict:
    s = dict(STATE)
    s["log"] = list(STATE["log"])
    s["results"] = list(STATE["results"])
    s["stopping"] = _STOP.is_set()
    return s


def is_running() -> bool:
    return bool(STATE["running"])


def request_stop() -> None:
    _STOP.set()
    _log("收到停止请求（当前号跑完后停止）")


def _reset(kind: str, total: int) -> None:
    _STOP.clear()
    STATE.update(running=True, kind=kind, started_at=time.time(), finished_at=0.0,
                 total=int(total or 0), done=0, ok=0, fail=0,
                 log=[], results=[], error="")


def _cleanup_proxy_session() -> None:
    """清掉伪账号的代理绑定/会话，别污染代理管理里的绑定计数。"""
    try:
        import proxy_store
        proxy_store.forget_account(_TAKE_ACCOUNT)
    except Exception:
        pass


# ------------------------------------------------------------------ 代理
def _resolve_proxy_url(proxy_id: str | None = None) -> str:
    """取一条可用于浏览器的上游代理 URL（每次调用 = 换一个新出口，dynamic 型）。

    proxy_id 留空 → 用「代理管理」里的池默认代理；没有默认代理 → 回落 config.PROXY。
    """
    import proxy_store
    import config
    pid = (proxy_id or "").strip() or (proxy_store.get_default_proxy_id() or "")
    if not pid:
        return (getattr(config, "PROXY", "") or "").strip()
    proxy_store.set_account_proxy(_TAKE_ACCOUNT, pid)
    return (proxy_store.proxy_url_for(_TAKE_ACCOUNT, rotate=True) or "").strip()


async def _open_browser_proxy(upstream: str):
    """上游代理 → 本地免认证转发器（统一走转发器：socks5 需要免认证转换，
    http 型上游（如 UDeal）也经转发器以获得前置代理自动回退能力）。

    目标是 Google / dola，转发器必须放行 google.com（noise_domains=()）。
    返回 (playwright_proxy_server, forwarder_or_None)。
    """
    import proxy_fwd
    if not upstream:
        return "", None
    fwd = await proxy_fwd.SocksForwarder.start(upstream, noise_domains=())
    return "socks5://127.0.0.1:%d" % fwd.port, fwd


def _short_egress(url: str) -> str:
    """显示出口网关 + sticky session 标识。

    网关 host:port 对同一供应商是固定的，光看它无法判断「一号一 IP」是否生效，
    所以额外把 user 段里的 session 标记（IPRocket 的 Lsid-xxxx）带出来。
    """
    if not url:
        return "(直连)"
    head, _, tail = url.rpartition("@")
    tail = tail or url
    m = re.search(r"(Lsid-[0-9A-Za-z]+|session-[A-Za-z0-9]+)", head)
    return (tail + "|" + m.group(1)) if m else tail


# ------------------------------------------------------------------ 收件后端 env
def _apply_mail_env(cfg: dict) -> None:
    """把面板表单里的收件后端参数写进环境（mail_catcher 走 env 读配置）。"""
    mb = (cfg.get("mail_backend") or "tempmail").strip().lower()
    os.environ["MAIL_BACKEND"] = mb
    if mb == "imap":
        os.environ["MAIL_IMAP_HOST"] = (cfg.get("imap_host") or "").strip()
        os.environ["MAIL_IMAP_PORT"] = str(cfg.get("imap_port") or "993")
        os.environ["MAIL_IMAP_USER"] = (cfg.get("imap_user") or "").strip()
        os.environ["MAIL_IMAP_PASS"] = cfg.get("imap_pass") or ""
        os.environ["MAIL_IMAP_BOX"] = (cfg.get("imap_box") or "INBOX").strip()
    elif mb == "http":
        os.environ["MAIL_HTTP_URL"] = (cfg.get("http_url") or "").strip()
        os.environ["MAIL_HTTP_TOKEN"] = (cfg.get("http_token") or "").strip()
    elif mb == "tempmail":
        pass
    else:
        raise RuntimeError("不支持的收件后端: %s（可选 tempmail/imap/http）" % mb)


# ================================================================== A. 谷歌注册
async def _job_google_register(cfg: dict, pool=None) -> None:
    import google_register as gr
    from mail_catcher import MailCatcher

    _apply_mail_env(cfg)
    domain = (cfg.get("domain") or "mailto.plus").strip()
    count = int(cfg.get("count") or 1)
    engine = (cfg.get("engine") or "camoufox").strip().lower()
    headless = bool(cfg.get("headless", True))
    # 防呆：patchright 无头会泄漏 HeadlessChrome UA（实测 5 项硬伤），强制切有头
    if engine == "patchright" and headless:
        _log("⚠️ patchright 无头会暴露 HeadlessChrome UA，已自动改为有头模式")
        headless = False
    want_totp = bool(cfg.get("want_totp", True))
    prefix_len = int(cfg.get("prefix_len") or 8)
    step_wait = int(cfg.get("step_wait_ms") or 2500)
    password = (cfg.get("password") or "").strip()
    out_path = Path(cfg.get("out") or DEFAULT_OUT)

    try:
        catcher = MailCatcher.from_env()
    except Exception as exc:
        raise RuntimeError("收件后端不可用: %s" % str(exc)[:160]) from exc

    try:
        from browser import LAUNCH_ARGS          # 与面板浏览器同一套启动参数
    except Exception:
        LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled",
                       "--no-first-run", "--no-default-browser-check",
                       "--disable-background-networking"]

    _log("谷歌注册开始：%d 个 | 域名=%s | 收件=%s | 有头=%s | 引擎=%s（每号换新住宅出口）"
         % (count, domain, catcher.name, not headless, engine))
    async with gr._maybe_playwright(engine) as p:
        for i in range(count):
            if _STOP.is_set():
                _log("已停止（剩余 %d 个未跑）" % (count - i))
                break
            # ★ 每个号单独取一条新住宅出口（dynamic 型 rotate = 新 sticky session = 新 IP）。
            #   原实现把取出口放在循环外，count>1 时所有号共用同一出口 IP —— 2026-10-12 修复。
            upstream = _resolve_proxy_url(cfg.get("proxy_id"))
            if not upstream:
                STATE["error"] = ("没有可用代理：请在「代理管理」设置默认代理，或指定代理")
                _log("  ✗ %s" % STATE["error"])
                break
            fwd = None
            try:
                proxy_server, fwd = await _open_browser_proxy(upstream)
                email = gr.rand_email(domain, prefix_len)
                pwd = password or gr.rand_password()
                _log("[%d/%d] 注册 %s | 出口=%s"
                     % (i + 1, count, email, _short_egress(upstream)))
                r = await gr.register_one(
                    p, email, pwd, catcher, gr._proxy_kwargs(proxy_server),
                    headless, "en-US", want_totp, step_wait, False,
                    LAUNCH_ARGS, "", engine)
                item = {"email": r["email"], "password": r["password"],
                        "totp": r.get("totp", ""), "ok": bool(r.get("ok")),
                        "error": r.get("error", "")}
                if item["ok"]:
                    line = "----".join([item["email"], item["password"], item["totp"]])
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(out_path, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                    STATE["ok"] += 1
                    _log("  ✓ 产出 %s%s" % (item["email"],
                                            "（含 TOTP）" if item["totp"] else "（无 2FA）"))
                else:
                    STATE["fail"] += 1
                    _log("  ✗ %s" % str(item["error"])[:180])
                STATE["results"].append(item)
                STATE["done"] += 1
            finally:
                if fwd is not None:
                    try:
                        await fwd.stop()
                    except Exception:
                        pass
    _log("谷歌注册结束：成功 %d / %d → %s" % (STATE["ok"], STATE["done"], out_path))


# ================================================================== B. 网页登录取 dola 号
def _load_google_items(cfg: dict) -> list[str]:
    """谷歌号来源：优先粘贴文本，其次号池文件（谷歌账号_产出.txt / 谷歌账号.txt）。"""
    items: list[str] = []
    txt = (cfg.get("accounts_text") or "").strip()
    if txt:
        items = [l.strip() for l in txt.splitlines()
                 if l.strip() and "----" in l and not l.strip().startswith("#")]
    else:
        src = (cfg.get("source_file") or "").strip()
        files = [src] if src else GOOGLE_POOL_FILES
        read_any = False
        for f in files:
            p = Path(f) if os.path.isabs(f) else (WORKDIR / f)
            if not p.exists():
                continue
            read_any = True
            for l in p.read_text(encoding="utf-8", errors="replace").splitlines():
                l = l.strip()
                if l and "----" in l and not l.startswith("#"):
                    items.append(l)
        if not read_any and not src:
            return []
    seen, uniq = set(), []
    for l in items:
        k = l.split("----")[0].strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        uniq.append(l)
    limit = int(cfg.get("limit") or 0)
    return uniq[:limit] if limit else uniq


async def _finish_import(res: dict, item: dict, pool, verify: bool,
                         name_prefix: str) -> dict:
    """网页登录结果 -> 硬闸校验 -> 入池。glogin / weblogin 两条链路共用。

    硬闸：cookie 里必须有 sessionid 或 passport_auth_status 才算真登录，
    只有匿名访客 cookie 一律判失败（2026-10-11 假成功入池踩坑）。
    返回 import 结果 dict（created/updated/errors/unverified）。
    """
    from cookie_login import import_cookie_accounts_from_text

    full = res.get("cookies_full") or []
    item["cookies"] = len(full)
    item["auth_status"] = bool(res.get("auth_status"))
    if not full:
        raise RuntimeError("未拿到 dola cookie（登录未完成；auth_status=%s）"
                           % ("有" if res.get("auth_status") else "无"))
    names_set = {c.get("name") for c in full if isinstance(c, dict)}
    if not (res.get("auth_status") or "sessionid" in names_set):
        raise RuntimeError(
            "登录未完成：cookie 里没有 sessionid/passport_auth_status"
            "（只拿到 %d 项匿名 cookie，页面可能没渲染出登录入口）" % len(full))
    raw = json.dumps(full, ensure_ascii=False)
    imp = await import_cookie_accounts_from_text(
        pool, raw, name_prefix=name_prefix, require_login=verify)
    created = imp.get("created") or []
    updated = imp.get("updated") or []
    names = created or updated
    item["account"] = names[0] if names else ""
    if imp.get("errors"):
        raise RuntimeError("; ".join(imp["errors"])[:180])
    if imp.get("unverified"):
        raise RuntimeError("登录态校验未通过，未入池：%s"
                           % ", ".join(imp["unverified"])[:150])
    if not names:
        raise RuntimeError("入池未产生账号（cookies 可能不含 sessionid）")
    return imp


async def _job_weblogin(cfg: dict, pool=None) -> None:
    import ck_weblogin

    if pool is None:
        raise RuntimeError("内部错误：缺少账号池引用")

    items = _load_google_items(cfg)
    if not items:
        raise RuntimeError(
            "没有可用的谷歌号：请粘贴 email----password----totp，"
            "或先在「谷歌注册」产出号池文件")
    STATE["total"] = len(items)
    headless = bool(cfg.get("headless", True))
    verify = bool(cfg.get("verify", True))
    name_prefix = (cfg.get("name_prefix") or "acc").strip() or "acc"

    _log("网页登录取号开始：%d 个谷歌号（每个号一个新住宅出口）| 验证登录态=%s"
         % (len(items), verify))

    for idx, line in enumerate(items, 1):
        if _STOP.is_set():
            _log("已停止（剩余 %d 个未跑）" % (len(items) - idx + 1))
            break
        parts = line.split("----")
        email = parts[0].strip()
        pwd = parts[1].strip() if len(parts) > 1 else ""
        secret = parts[2].strip() if len(parts) > 2 else ""
        item = {"email": email, "account": "", "cookies": 0, "auth_status": False,
                "ok": False, "error": ""}
        try:
            upstream = _resolve_proxy_url(cfg.get("proxy_id"))
            if not upstream:
                raise RuntimeError("没有可用代理：请在「代理管理」设置默认代理")
            _log("[%d/%d] 网页登录 %s | 出口=%s"
                 % (idx, len(items), email, _short_egress(upstream)))
            res = await ck_weblogin.run(email, pwd, secret,
                                        proxy=upstream, headless=headless)
            imp = await _finish_import(res, item, pool, verify, name_prefix)
            item["ok"] = True
            STATE["ok"] += 1
            _log("  ✓ 入池 %s | CK %d 项 | auth_status=%s%s"
                 % (item["account"], item["cookies"],
                    "有" if item["auth_status"] else "无",
                    "（已存在，更新）" if (not imp.get("created") and imp.get("updated")) else ""))
        except Exception as exc:  # noqa: BLE001 - 失败原因要回面板
            item["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            STATE["fail"] += 1
            _log("  ✗ %s" % item["error"])
        STATE["results"].append(item)
        STATE["done"] += 1

    _log("网页登录取号结束：成功 %d / %d" % (STATE["ok"], STATE["done"]))


# ================================================================== C. Google 渠道登录
def _load_glogin_items(cfg: dict) -> list[dict]:
    """Google 渠道号来源：粘贴文本，格式 `账号|密码|取件地址`（| 分隔，一行一个）。

    取件地址可空（Google 登录不触发邮件验证码时不需要）。
    """
    items: list[dict] = []
    txt = (cfg.get("accounts_text") or "").strip()
    if not txt:
        raise RuntimeError("请粘贴 Google 渠道号，格式：账号|密码|取件地址")
    for l in txt.splitlines():
        l = l.strip()
        if not l or l.startswith("#"):
            continue
        parts = [p.strip() for p in l.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise RuntimeError("格式错误：%s（应为 账号|密码|取件地址）" % l[:60])
        items.append({"email": parts[0], "password": parts[1],
                      "mail_api": parts[2] if len(parts) > 2 else ""})
    seen, uniq = set(), []
    for it in items:
        k = it["email"].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    limit = int(cfg.get("limit") or 0)
    return uniq[:limit] if limit else uniq


async def _job_glogin(cfg: dict, pool=None) -> None:
    import gchannel_login

    if pool is None:
        raise RuntimeError("内部错误：缺少账号池引用")

    items = _load_glogin_items(cfg)
    STATE["total"] = len(items)
    verify = bool(cfg.get("verify", True))
    name_prefix = (cfg.get("name_prefix") or "acc").strip() or "acc"

    _log("Google 渠道登录开始：%d 个号（每个号一个新住宅出口）| 验证登录态=%s"
         % (len(items), verify))

    for idx, it in enumerate(items, 1):
        email, pwd, mail_api = it["email"], it["password"], it["mail_api"]
        item = {"email": email, "account": "", "cookies": 0, "auth_status": False,
                "ok": False, "error": ""}
        fwd = None
        try:
            upstream = _resolve_proxy_url(cfg.get("proxy_id"))
            if not upstream:
                raise RuntimeError("没有可用代理：请在「代理管理」设置默认代理")
            _log("[%d/%d] Google 渠道 %s | 取件=%s | 出口=%s"
                 % (idx, len(items), email, mail_api or "（无）", _short_egress(upstream)))
            # ★ 统一过本地转发器（socks5 免认证化 + UDeal 等网关的前置代理自动回退），
            #   直接把原始 URL 给浏览器会「无网络」（UDeal 拒大陆客户端 IP，2026-10-12 实测）。
            proxy_server, fwd = await _open_browser_proxy(upstream)
            # ★ run_sync 在独立线程 + 独立 loop 里跑浏览器：server 主 loop 里
            #   launch patchright 会把整个 uvicorn 进程带走（2026-10-12 实测）。
            #   run_sync 内部 join 线程是阻塞调用，必须丢线程池，别堵主 loop。
            res = await asyncio.get_running_loop().run_in_executor(
                None, lambda: gchannel_login.run_sync(email, pwd, mail_api,
                                                      proxy_server, False))
            await _finish_import(res, item, pool, verify, name_prefix)
            item["ok"] = True
            STATE["ok"] += 1
            _log("  ✓ 入池 %s | CK %d 项 | auth_status=%s"
                 % (item["account"], item["cookies"],
                    "有" if item["auth_status"] else "无"))
        except Exception as exc:  # noqa: BLE001
            item["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
            STATE["fail"] += 1
            _log("  ✗ %s" % item["error"])
        finally:
            if fwd is not None:
                try:
                    await fwd.stop()
                except Exception:
                    pass
        STATE["results"].append(item)
        STATE["done"] += 1

    _log("Google 渠道登录结束：成功 %d / %d" % (STATE["ok"], STATE["done"]))


# ------------------------------------------------------------------ 启动入口
def start(kind: str, cfg: dict, pool=None) -> bool:
    """启动一个取号任务（与 server 同事件循环）。已在跑则返回 False。"""
    if STATE["running"]:
        return False
    _reset(kind, int(cfg.get("count") or 0))

    async def _runner():
        try:
            if kind == "google":
                await _job_google_register(cfg, pool)
            elif kind == "weblogin":
                await _job_weblogin(cfg, pool)
            elif kind == "glogin":
                await _job_glogin(cfg, pool)
            else:
                raise RuntimeError("未知任务类型: %s" % kind)
        except Exception as exc:  # noqa: BLE001
            STATE["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
            _log("✗ 任务异常：%s" % STATE["error"])
        finally:
            STATE["running"] = False
            STATE["finished_at"] = time.time()
            _cleanup_proxy_session()

    asyncio.get_running_loop().create_task(_runner())
    return True
