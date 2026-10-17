"""视频池生产管理器：连跑换号 + 单号守望取片。

流程（每个订单）：
  1. 子进程跑 _diag_page_register.py（页面注册 → 选 Seedance 2.5 → 首次提交，timeout 保护）
  2. 解析结果：710022002=账号风控 → 立即弃号换下一个（不等待）
  3. 受理 → 起独立守望线程：纯 API 轮询会话，出片自动下载，最长 watch_hours
  4. worker 不等出片，继续注册下一个号（上游渲染在云端，不占本机）

状态经 status() 供面板 /api/admin/videopool/status 轮询。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import config

RUN = Path(__file__).resolve().parent
DOWNLOAD_DIR = RUN / "downloads" / "video_pool"
REGISTER_SCRIPT = RUN / "_diag_page_register.py"
REGISTER_TIMEOUT = 300          # 单号注册+提交上限（正常 ~60s，留足余量）
REGISTER_GAP = 8                # 两个号之间的间隔秒
MAX_LOGS = 400
MAX_ACCOUNTS = 500

_RE_ACCOUNT = re.compile(r"accounts/(s\d+)/cookie_state\.json")
_RE_CONV = re.compile(r"conversation_id\\+?\":\\+?\"(38\d{14,})")


class VideoPoolManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False
        self.orders_target = 0
        self.produced = 0
        self.rejected = 0
        self.reg_fail = 0
        self.accounts: list[dict] = []
        self.logs: list[dict] = []
        self._domain = ""

    # ---- 日志与记录 ----
    def log(self, lv: str, msg: str) -> None:
        with self._log_lock:
            self.logs.append({"t": time.strftime("%H:%M:%S"), "lv": lv, "msg": msg})
            if len(self.logs) > MAX_LOGS:
                del self.logs[: len(self.logs) - MAX_LOGS]

    def _account(self, name: str, **kw) -> Optional[dict]:
        with self._lock:
            for rec in self.accounts:
                if rec["account"] == name:
                    if kw:
                        rec.update(kw)
                    return rec
            if name:
                rec = {"account": name, "stage": "registering", "conv_id": "",
                       "video_path": "", "created": time.strftime("%H:%M:%S"),
                       "note": ""}
                rec.update(kw)
                self.accounts.append(rec)
                if len(self.accounts) > MAX_ACCOUNTS:
                    del self.accounts[: len(self.accounts) - MAX_ACCOUNTS]
                return rec
        return None

    # ---- 对外接口（与 RegistrationManager 同风格） ----
    def start(self, cfg: dict) -> bool:
        with self._lock:
            if self.running:
                return False
            self._stop = threading.Event()
            self.running = True
            self.orders_target = max(1, int(cfg.get("orders") or 1))
            self.produced = 0
            self.rejected = 0
            self.reg_fail = 0
            self.accounts = []
            self.logs = []
            self._thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        with self._lock:
            accounts = [dict(r) for r in self.accounts[-200:]]
        return {
            "running": self.running,
            "orders": self.orders_target,
            "produced": self.produced,
            "rejected": self.rejected,
            "reg_fail": self.reg_fail,
            "watching": sum(1 for r in accounts if r.get("stage") == "watching"),
            "accounts": accounts,
            "logs": self.logs[-150:],
        }

    # ---- 线程体 ----
    def _run(self, cfg: dict) -> None:
        try:
            self._run_inner(cfg)
        except Exception as exc:
            self.log("err", "视频池线程异常: %s" % str(exc)[:300])
        finally:
            self.running = False
            self.log("info", "视频池任务结束：出片 %d · 风控弃号 %d · 注册失败 %d"
                     % (self.produced, self.rejected, self.reg_fail))

    def _run_inner(self, cfg: dict) -> None:
        orders = self.orders_target
        max_accounts = max(orders, int(cfg.get("max_accounts") or orders * 4))
        prompt = (cfg.get("prompt") or "一只柴犬在樱花树下奔跑，镜头跟随").strip()
        watch_hours = float(cfg.get("watch_hours") or 2)
        proxy = (cfg.get("proxy") or "").strip() or "socks5://127.0.0.1:7897"
        self._domain = (cfg.get("domain") or "").strip()
        self.log("info", "目标 %d 单，提示词=%r，邮箱域=%s" % (orders, prompt[:40], self._domain or "mailto.plus"))

        for i in range(1, max_accounts + 1):
            if self._stop.is_set() or self.produced >= orders:
                break
            self.log("info", "—— 第 %d 个号开始 ——（已出 %d/%d 单）"
                     % (i, self.produced, orders))
            acc, accepted = self._register_and_submit(prompt, proxy)
            if self._stop.is_set():
                break
            if acc is None:
                self.reg_fail += 1
                self.log("warn", "注册/提交失败，换下一个号")
                continue
            if not accepted:
                self.rejected += 1
                self._account(acc, stage="rejected_710", note="710022002 账号风控，弃号")
                self.log("warn", "%s 首提被风控(710)，弃号换下一个" % acc)
                continue
            # 受理：起守望线程，worker 继续注册
            self._account(acc, stage="watching", note="已受理，守望取片中")
            self.log("info", "%s 受理成功，守望线程启动" % acc)
            t = threading.Thread(target=self._watch_and_download,
                                 args=(acc, watch_hours), daemon=True)
            t.start()
            self._stop.wait(REGISTER_GAP)

        # 收尾：等所有守望线程结束（stop 时最多再等 5 分钟）
        deadline = time.time() + (0 if self._stop.is_set() else watch_hours * 3600 + 120)
        while time.time() < deadline:
            with self._lock:
                busy = any(r.get("stage") in ("registering", "watching") for r in self.accounts)
            if not busy or (self._stop.is_set() and time.time() > deadline - watch_hours * 3600 + 300):
                break
            time.sleep(15)

    # ---- 注册+提交（子进程） ----
    def _register_and_submit(self, prompt: str, proxy: str):
        env = dict(os.environ)
        env["DIAG_PROXY"] = proxy
        env["DOLA_PROMPT"] = prompt
        if self._domain:
            env["DOLA_DOMAIN"] = self._domain
        log_path = RUN / "_videopool_register.log"
        try:
            with open(log_path, "w", encoding="utf-8", errors="ignore") as f:
                subprocess.run(
                    [sys.executable, "-u", str(REGISTER_SCRIPT)],
                    cwd=str(RUN), env=env, stdout=f, stderr=subprocess.STDOUT,
                    timeout=REGISTER_TIMEOUT,
                    # hint855：注册子进程（python）不得弹控制台窗
                    creationflags=(
                        (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
                        if os.name == "nt" else 0))
        except subprocess.TimeoutExpired:
            # 脚本可能已完成全部工作只是清理挂起：照常解析日志，出结果不算失败
            self.log("warn", "注册子进程超时 %ds，尝试按日志判定结果" % REGISTER_TIMEOUT)
            acc2, ok2 = self._parse_register_log(log_path)
            if acc2 and ok2:
                return acc2, True
            return acc2, False
        except Exception as exc:
            self.log("err", "注册子进程启动失败: %s" % str(exc)[:200])
            return None, False
        return self._parse_register_log(log_path)

    def _parse_register_log(self, log_path: Path):
        text = log_path.read_text(encoding="utf-8", errors="ignore")
        m = _RE_ACCOUNT.search(text)
        acc = m.group(1) if m else ""
        if acc:
            self._account(acc)
        if "710022002" in text:
            return acc, False
        if "实验终止" in text or "发码失败" in text or "未收到验证码" in text:
            if acc:
                self._account(acc, stage="reg_fail", note="注册链路失败")
            self.log("err", "%s 注册链路失败（收码/发码/页面异常）" % (acc or "?"))
            return acc, False
        if "completion_resp" not in text:
            self.log("err", "未捕获提交结果（无 completion_resp）")
            return acc, False
        if not acc:
            self.log("err", "提交完成但未能解析账号名")
            return "", False
        return acc, True

    # ---- 守望取片（独立线程） ----
    def _watch_and_download(self, acc: str, watch_hours: float) -> None:
        try:
            self._watch_inner(acc, watch_hours)
        except Exception as exc:
            self._account(acc, stage="video_failed", note="守望异常: %s" % str(exc)[:150])
            self.log("err", "%s 守望异常: %s" % (acc, str(exc)[:200]))

    def _watch_inner(self, acc: str, watch_hours: float) -> None:
        sys.path.insert(0, str(RUN))
        import pure_api_gen  # noqa: E402

        api_state = RUN / "accounts" / acc / "cookie_state_api.json"
        page_state = RUN / "accounts" / acc / "cookie_state.json"
        if not api_state.exists() and page_state.exists():
            st = json.loads(page_state.read_text(encoding="utf-8"))
            st["cookies"] = st.get("cookies_list") or st.get("cookies")
            api_state.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

        dola = pure_api_gen._dola()
        deadline = time.time() + watch_hours * 3600
        conv_id = ""
        last_status = ""
        while time.time() < deadline:
            if self._stop.is_set():
                self._account(acc, note="守望被停止")
                return
            try:
                session, ctx, headers = dola.setup_session(
                    region=config.PURE_API_REGION, pc_version=config.PURE_API_PC_VERSION,
                    proxy="socks5h://127.0.0.1:7897", login_pure=True,
                    login_state_file=str(api_state))
                if not conv_id:
                    recent = dola.fetch_recent_conversations(
                        session, ctx, limit=5, message_count_per_conv=4)
                    body = (recent.get("downlink_body") or {}).get(
                        "pull_recent_conv_chain_downlink_body") or {}
                    convs = [c.get("conversation") or {}
                             for c in (body.get("cells") or []) if isinstance(c, dict)]
                    convs = [c for c in convs if c.get("conversation_id")]
                    if not convs:
                        time.sleep(60)
                        continue
                    conv_id = str(convs[0]["conversation_id"])
                    self._account(acc, conv_id=conv_id)
                    self.log("info", "%s 视频会话 conv=%s" % (acc, conv_id))
                poll = dola.inspect_video_once(session, ctx, conv_id)
                if poll.urls:
                    out = DOWNLOAD_DIR / f"{acc}.mp4"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    import requests as rq
                    with open(out, "wb") as f:
                        r = rq.get(poll.urls[0], timeout=180, stream=True)
                        for chunk in r.iter_content(65536):
                            f.write(chunk)
                    self.produced += 1
                    self._account(acc, stage="video_ready", video_path=str(out),
                                  note="出片 %.1fMB" % (out.stat().st_size / 1048576))
                    self.log("info", "🎉 %s 出片 %s (%.2f MB)"
                             % (acc, out.name, out.stat().st_size / 1048576))
                    return
                if poll.status != last_status:
                    self._account(acc, note="render status=%s" % poll.status)
                    last_status = poll.status
                if poll.failure_reasons:
                    self._account(acc, stage="video_failed",
                                  note=str(poll.failure_reasons)[:200])
                    self.log("err", "%s 渲染失败: %s" % (acc, str(poll.failure_reasons)[:200]))
                    return
            except Exception as e:
                self.log("warn", "%s poll err: %s" % (acc, str(e)[:120]))
            time.sleep(60)
        self._account(acc, stage="video_timeout", note="超 %g 小时未出片" % watch_hours)


manager = VideoPoolManager()
