# -*- coding: utf-8 -*-
"""pool_engine_service.py —— DOLA 出片引擎（run_pool.py）的服务化包装。

背景 / 为什么要有这一层
--------------------------------------------------------------------------
本项目的出片链路**完全按 A（Desktop\\DBDA）走**：
    注册(dc.py 纯协议) → 探针嗅窗 → 整池爆发 → 延迟收割 → 无水印 MP4
这条链路的实现体是 ``run_pool.py``（外加它依赖的 dc.py / reg.py / dola_window.py）。
它天生是一个**长驻命令行程序**（自带探针线程 / 协调线程 / 补号线程 / 收割线程池），
不是能在事件循环里 await 的协程。

所以这里不做"重写进 server.py"（那等于把已验证的引擎拆了重装），而是**进程级包装**：
    server.py(=B 的 UI 后端)  ──HTTP──▶ 本模块 ──subprocess──▶ run_pool.py
B 的面板负责「下发参数 / 显示状态 / 看日志 / 看成片」，引擎负责干活。

关键设计点
--------------------------------------------------------------------------
1. **参数用环境变量下发**：run_pool.py 的参数全部读 ``I794_*`` 环境变量，
   所以 UI 上的改动不需要改引擎文件，只在本进程 spawn 时注入 env。
2. **号池共享**：``acc/`` 与 ``accounts/`` 都是指向 ``Desktop\\DBDA\\acc`` 的 junction，
   引擎注册出来的号面板立刻可见，面板删的号引擎也能感知。
3. **嗅探器互斥（fact 629 反模式）**：面板自己也会起一个窗口嗅探器
   （``dola_window`` 全局单例）。两个嗅探器会互抢 710 窗口相位、把出口打热，
   结果是**两边都错过窗口**。所以引擎启动前把面板那个停掉，引擎停止后再放回去。
   这是本项目最容易踩、且最难从现象上看出来的坑。
4. **优雅停止**：run_pool.py 靠数据根下的 ``STOP`` 空文件收尾（它会写完 summary、
   把已在收割的 conv 收干净）。所以 stop() 先落 STOP、给足宽限期，超时才强杀。
"""
from __future__ import annotations

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
ENGINE = RUN / "engine"          # 兼容：A 迁移后 run_pool.py 可能落在 engine\
ENGINE_SCRIPT = RUN / "run_pool.py"
if not ENGINE_SCRIPT.is_file() and (ENGINE / "run_pool.py").is_file():
    ENGINE_SCRIPT = ENGINE / "run_pool.py"

ACC = RUN / "acc"
OUT = RUN / "out"
MP4DIR = OUT / "mp4"
# ★ 图片产物目录（Seedream 出图）：一条图片任务产多张图，前端直接 <img> 渲染，
#   与 mp4 分开放，避免"视频库"把图片一起扫进去（也避免 feed 认错类型）。
IMGDIR = OUT / "img"
TASKS_FILE = RUN / "tasks.txt"
STOP_FILE = RUN / "STOP"
PID_FILE = OUT / "engine.pid"
ENGINE_LOG = OUT / "engine_console.log"
LEDGER = OUT / "ledger.jsonl"
SUMMARY = OUT / "summary_pool.json"
SUBMIT_LOG = OUT / "submit.jsonl"
# 队列快照（引擎侧 queue_watch_loop 原子写）+ 清空/覆盖哨兵（面板落文件，引擎消费）
QUEUE_STATE = OUT / "queue_state.json"
# 快照认领的最大年龄（秒）。引擎的 watch 线程每 ~3 秒重写一次，
# 所以「新鲜」意味着 30 秒内有过写入；超过这个年龄说明引擎早就不在跑了，
# 那份快照是上一轮的陈旧状态，不能拿来当当前队列显示。
QUEUE_STATE_MAX_AGE = 30.0
# ---- 进程枚举缓存 ----
# 为什么需要：``_scan_engine_pids`` / ``_proc_elapsed`` 都要起 wmic 子进程
# （实测每次 ~0.2s），而一次 ``status()`` 会顺着调用链跑 **4 次** wmic ≈ 0.8~1.0s。
# 前端「运行状态」每 5 秒自动刷一次，等于把 CPU 时间全花在枚举进程表上，
# 用户看到的就是"每次打开运行状态都要从零统计一遍"。
# 这里把这两类查询按 PID 做短 TTL 缓存：同一 PID 在 TTL 内只查一次。
# ★ TTL 必须大于前端自动刷新间隔（「运行状态」页是 5 秒），详见下方 _PROC_CACHE_TTL。
#
# ★ 与引擎启动安全相关：``start()`` 里的"有没有孤儿引擎"判重**必须**看到实时结果，
#   否则会拉起第二个引擎（fact 629 反模式：两个嗅探器互抢窗口）。
#   所以缓存只用于**展示**路径，安全路径显式传 fresh=True 绕过缓存。
# ★ TTL 必须**大于前端自动刷新间隔**（「运行状态」页是 5 秒），否则每次刷新
#   都恰好过期，缓存等于没做 —— 实测 TTL=3s + 5s 刷新时，每次仍要付 4 个 wmic
#   子进程的代价（0.45s），用户感受就是"每次打开都在从零统计"。
#   代价是"引擎刚死"最多晚 TTL 秒才在面板上反映出来；但真正的存活判断还有
#   一条**廉价且实时**的旁路 —— submit.jsonl / engine_console.log / ledger.jsonl
#   的文件新鲜度心跳（见 status() 的 alive_beat，阈值 90s）。两者互补：
#   进程枚举负责"是谁、跑了多久"，心跳负责"是否还在动"。
#   安全路径（启动判重/停止/清队列/覆盖/提交）一律 fresh=True，不受 TTL 影响。
_PROC_CACHE_TTL = 20.0
_proc_cache_lock = threading.Lock()
_proc_cache: dict[str, tuple[float, object]] = {}


def _proc_cache_get(key: str):
    with _proc_cache_lock:
        hit = _proc_cache.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _PROC_CACHE_TTL:
        return None
    return val


def _proc_cache_put(key: str, value) -> None:
    with _proc_cache_lock:
        _proc_cache[key] = (time.time(), value)

CLEAR_FLAG = RUN / "QUEUE_CLEAR"
RELOAD_FLAG = RUN / "QUEUE_RELOAD"
# 优先任务标记（必须与 run_pool.PRIO_MARK 一致）：面板「发起出片」用它把这批
# 小任务插到普通批量任务之前，避免排在几百条后面干等好几个出片窗口。
PRIO_MARK = "@@PRIO@@"

# 多行提示词的换行转义符（必须与 run_pool._MULTILINE_ESC 一致）。
# tasks.txt 的协议是「一行一条提示词」，而创作台的输入框天然是多行的
# （分镜剧本 / 分段描述 / 旁白字幕）。直接把多行文本写进文件 → 每一行都被
# 当成独立任务 → 用户提交 1 条 30 秒视频，任务列表里却多出十几条碎片任务。
# 因此写入前把换行转成 \n 字面量、读出来时再还原；单行提示词字节级不变。
MULTILINE_ESC = "\\n"


def escape_prompt_newlines(prompt: str) -> str:
    """把提示词里的真实换行转成 \\n 字面量，使其能安全放进 tasks.txt 的一行。"""
    s = str(prompt or "")
    if "\r\n" in s or "\n" in s or "\r" in s:
        s = (s.replace("\r\n", MULTILINE_ESC)
              .replace("\n", MULTILINE_ESC)
              .replace("\r", MULTILINE_ESC))
    return s


def unescape_prompt_newlines(line: str) -> str:
    """把 \\n 字面量还原成真实换行（escape_prompt_newlines 的逆操作）。

    不含该字面量的行原样返回 —— 老队列 / 手编 tasks.txt 行为完全不变。
    """
    s = str(line or "")
    if MULTILINE_ESC in s:
        return s.replace(MULTILINE_ESC, "\n")
    return s

# 引擎参数（UI 可改）→ 环境变量名。默认值与 run_pool.py 内建默认一致。
PARAM_ENV = {
    "pool_target": "I794_POOL_TARGET",
    "max_per_acc": "I794_MAX_PER_ACC",
    "probe_standby": "I794_PROBE_STANDBY",
    "sniff_period": "I794_SNIFF_PERIOD",
    "sniff_jitter": "I794_SNIFF_JITTER",
    "burst_conc": "I794_BURST_CONC",
    "harvest_workers": "I794_HARVEST_WORKERS",
    "harvest_timeout": "I794_HARVEST_TIMEOUT",
    "harvest_interval": "I794_HARVEST_INTERVAL",
    "register_workers": "I794_REGISTER_WORKERS",
    "task_attempts": "I794_TASK_ATTEMPTS",
    "run_seconds": "I794_RUN_SECONDS",
    "ratio": "I794_RATIO",
    "duration": "I794_DURATION",
    "model": "I794_MODEL",
    "watermark_fallback": "I794_WATERMARK_FALLBACK",
    "burst_pats": "I794_BURST_PATS",
    "probe_pat": "I794_PROBE_PAT",
}

DEFAULTS = {
    "pool_target": 50, "max_per_acc": 2, "probe_standby": 6,
    "sniff_period": 6, "sniff_jitter": 2, "burst_conc": 50,
    "harvest_workers": 16, "harvest_timeout": 9600, "harvest_interval": 60,
    "register_workers": 5, "task_attempts": 3, "run_seconds": 0,
    "ratio": "16:9", "duration": 30, "model": "seedance_v2.5",
    "watermark_fallback": "save", "burst_pats": "pool", "probe_pat": "probe",
}

MAX_LOG_LINES = 4000


def _no_window_flags() -> int:
    """hint855：外部进程不得弹控制台窗。

    注意 CREATE_NO_WINDOW 在父进程已有控制台时会被忽略，必须叠加 DETACHED_PROCESS。
    这里**不能**用 DETACHED_PROCESS：引擎要能被父进程优雅管理（STOP/terminate），
    且我们要接管它的 stdout。故只用 CREATE_NO_WINDOW。
    """
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _tail(path: Path, n: int) -> list[str]:
    """读文件最后 n 行（不整文件读入内存）。"""
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        block = min(size, max(64 * 1024, n * 400))
        with open(path, "rb") as fh:
            fh.seek(max(0, size - block))
            data = fh.read().decode("utf-8", errors="replace")
        return data.splitlines()[-n:]
    except OSError:
        return []


def _count_jsonl(path: Path, key: str, value: str, limit: int = 4000) -> int:
    """统计 jsonl 里 kind==value 的行数（只扫尾部 limit 行，够用且快）。"""
    n = 0
    for line in _tail(path, limit):
        try:
            if json.loads(line).get(key) == value:
                n += 1
        except Exception:
            continue
    return n


def _ledger_events(path: Path, limit: int = 6000) -> list[dict]:
    out = []
    for line in _tail(path, limit):
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _analyze_events(events: list[dict], tasks: list[dict],
                    since_tstr: str = "") -> dict:
    """从台账事件流实时算出运行状态（**对齐 A 的 monitor.py**）。

    为什么不能只看 summary_pool.json：那个文件 run_pool.py **只在进程退出时**
    （main() 的 finally）写一次。引擎跑着的时候它一直是上一轮的旧值 ——
    表现为「明明已经受理成功，面板却全 0」。A 自己的实时看板 monitor.py 读的
    正是 ledger.jsonl（每受理/爆发/收割都立刻落一条），这里采用同一数据源。

    since_tstr：只认这个时间（含）之后的**任务态事件**（accepted / harvest /
    task_failed）。为什么必须按时间切：tid 是每轮重用的（t001、t002…），
    ledger 里同一个 tid 会有多代记录（实测：14:26 那轮 t001 成功出片，
    21:09 这轮 t001 还在排队）。不切时间就会把上一轮的成功算到这一轮头上 ——
    面板显示"已完成 3 个"，实际这轮一个都没出。
    窗口/爆发计数仍统计全量（它们是累计事实，与轮次无关）。
    """
    tstate = {}
    for t in tasks:
        tstate[t["tid"]] = {"tid": t["tid"], "status": "pending", "account": "",
                            "conv": "", "hits": 0, "bytes": 0,
                            "unmarked": None, "prompt": t.get("prompt", "")}

    windows = bursts = accepted = harvest_ok = harvest_bad = unmarked = marked = 0
    fails = 0
    last_window = ""
    walls: list[float] = []

    def _in_scope(ev) -> bool:
        """任务态事件是否属于本轮（按 tstr 串比较，格式统一为 YYYY-MM-DD HH:MM:SS）。"""
        if not since_tstr:
            return True
        t = str(ev.get("tstr") or "")
        return bool(t) and t >= since_tstr

    for ev in events:
        k = ev.get("kind")
        if k == "window_open":
            windows += 1
            last_window = ev.get("tstr") or last_window
        elif k == "burst_done":
            bursts += 1
            try:
                if ev.get("wall") is not None:
                    walls.append(float(ev["wall"]))
            except (TypeError, ValueError):
                pass
        elif k == "accepted":
            accepted += 1
        elif k == "harvest":
            if ev.get("ftypisom"):
                harvest_ok += 1
                if ev.get("unwatermarked"):
                    unmarked += 1
                else:
                    marked += 1
            else:
                harvest_bad += 1
        elif k == "task_failed":
            fails += 1

        tid = ev.get("task")
        st = tstate.get(tid)
        if st is None:
            continue
        # 任务态事件只认本轮（since_tstr 之后）；窗口/爆发计数已在上面全量统计。
        # 不切轮次会把上一轮同 tid 的成功/失败算到这一轮头上。
        if not _in_scope(ev):
            continue
        if k == "accepted":
            st["status"] = "accepted"
            st["account"] = ev.get("account") or st["account"]
            st["conv"] = str(ev.get("conv") or st["conv"])
        elif k == "harvest":
            # 完成判定：ftypisom（dola 域）或 status=="succeeded"（豆包域）任一为真。
            # 只认 ftypisom 会把豆包 harvest 漏掉 —— 它不带这个字段，
            # 于是面板永远显示 accepted，而成片早已落盘（实测两处数字对不上）。
            done_ok = bool(ev.get("ftypisom")) or (
                str(ev.get("status") or "") == "succeeded" and bool(ev.get("local_path")))
            if done_ok:
                st["status"] = "done" if ev.get("unwatermarked") else "done_watermarked"
                st["unmarked"] = bool(ev.get("unwatermarked"))
                st["bytes"] = int(ev.get("bytes") or 0)
                st["channel"] = str(ev.get("channel") or st.get("channel") or "")
                # 记下这个任务的成片文件名：磁盘兜底时按**文件名**认，不按 tid。
                # tid 每轮都会重用（t001、t002…），按 tid 会把上一轮的成片
                # 张冠李戴到这一轮的同名任务上（实测踩到：上一轮 14:26 的
                # t001.mp4 把本轮还在 accepted 的 t001 标成了 done）。
                lp = str(ev.get("local_path") or "")
                if lp:
                    st["mp4"] = Path(lp).name
            elif st["status"] not in ("done", "done_watermarked"):
                st["unmarked"] = False
        elif k == "task_failed":
            st["status"] = "failed"
        elif k in ("submit", "submit_retry"):
            st["hits"] = st.get("hits", 0) + 1

    # 磁盘成片兜底：引擎可能来不及写 harvest 事件（或写在 6000 行窗口之外）。
    # ★ 按**文件名**认领，绝不按 tid：tid 每轮重用（t001…），上一轮的
    #   t001_xxx.mp4 会把本轮同名任务误标成 done（实测踩到）。
    #   只认「本任务台账里出现过、且磁盘上确实存在」的那个文件。
    try:
        want = {st.get("mp4"): st for st in tstate.values() if st.get("mp4")}
        if want:
            for p in MP4DIR.glob("*.mp4"):
                st = want.get(p.name)
                if st is None:
                    continue
                st["status"] = ("done_watermarked" if p.stem.endswith("_WM") else "done")
                st["unmarked"] = not p.stem.endswith("_WM")
                st["bytes"] = p.stat().st_size
    except OSError:
        pass

    counts = {"pending": 0, "accepted": 0, "done": 0, "failed": 0}
    for st in tstate.values():
        s = st["status"]
        if s in ("done", "done_watermarked"):
            counts["done"] += 1
        elif s == "failed":
            counts["failed"] += 1
        elif s == "accepted":
            counts["accepted"] += 1
        else:
            counts["pending"] += 1

    return {
        "tasks": counts,
        "task_rows": list(tstate.values()),
        "windows": windows,
        "bursts": bursts,
        "accepted": accepted,
        "harvest_ok": harvest_ok,
        "harvest_bad": harvest_bad,
        "unmarked": unmarked,
        "marked": marked,
        "fails": fails,
        "last_window": last_window,
        "burst_wall_last": round(walls[-1], 2) if walls else None,
    }


def _file_age(path: Path) -> float | None:
    """文件最后修改距今秒数；不存在返回 None。"""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _proc_elapsed(pid: int, fresh: bool = False) -> float:
    """进程已运行秒数（从 wmic CreationDate 推算）。取不到返回 0。

    为什么要算：引擎常由"重启前的旧面板"拉起，本进程没有 _started_at，
    运行时长会一直显示 0。用进程创建时间换算，重启面板后时长仍然正确。

    进程创建时间是不可变事实，唯一值得缓存的对象：缓存里存 **CreationDate 的
    epoch**，返回值仍按"当前时间 - 创建时间"实时算，所以缓存不会让
    "已运行 X 分钟"停住不走。
    """
    if pid <= 0:
        return 0.0
    cache_key = f"proc_created:{pid}"
    created = None if fresh else _proc_cache_get(cache_key)
    if created is None:
        try:
            import subprocess as _sp
            out = _sp.run(["wmic", "process", "where", f"ProcessId={pid}",
                           "get", "CreationDate", "/value"],
                          capture_output=True, timeout=10,
                          creationflags=_no_window_flags())
            txt = (out.stdout or b"").decode("mbcs", errors="replace")
            for line in txt.splitlines():
                line = line.strip()
                if line.startswith("CreationDate=") and len(line) > 20:
                    raw = line.split("=", 1)[1].split("+")[0].split(".")[0]   # 20261016133903
                    created = time.mktime(time.strptime(raw, "%Y%m%d%H%M%S"))
                    break
        except Exception:  # noqa: BLE001
            created = None
        if created is not None:
            _proc_cache_put(cache_key, created)
    if created is None:
        return 0.0
    return max(0.0, time.time() - created)


class PoolEngineManager:
    """run_pool.py 的进程管理器（单例语义：同一时刻只跑一个引擎）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._params: dict = dict(DEFAULTS)
        self._started_at: float = 0.0
        self._stopping = False
        self._last_exit: dict = {}
        # 引擎退出回调（server.py 挂豆包降级用）。签名 (returncode) -> None，
        # 在收尾线程里调用，实现方必须自己吞异常，不能拖住收尾。
        self.on_exit: Optional[callable] = None
        self._sniffer_suspended = False
        self._task_count = 0

    # ------------------------------------------------------------------ 嗅探器互斥
    def _suspend_panel_sniffer(self) -> str:
        """把面板自己的窗口嗅探器停掉并**确认真停了**（引擎要独占窗口相位）。

        为什么要等：WindowSniffer.stop() 只 join 5 秒，而一次探针提交最长 30 秒。
        若不等就放引擎出去，两边会有一段时间**同时**在打 /chat/completion ——
        正是 fact 629 警告的反模式（互抢窗口相位 + 把出口打热，两边都错过窗口）。
        所以这里轮询到线程真正结束为止（上限 40s，覆盖单次提交最坏耗时）。
        """
        try:
            import dola_window
            gate = dola_window.get_gate()
            if gate is None:
                return "面板嗅探器未启动，无需暂停"
            gate.stop()
            self._sniffer_suspended = True
            # 确认哨兵线程真的退出（stop 只 join 5s，可能还在提交中）
            sniffer = getattr(gate, "sniffer", None)
            th = getattr(sniffer, "_thread", None)
            deadline = time.time() + 40
            while th is not None and th.is_alive() and time.time() < deadline:
                time.sleep(0.5)
            still = bool(th is not None and th.is_alive())
            # 若仍在跑，把 stop 标记保留（线程做完当前这发就会退），但要如实告知
            return ("面板嗅探器已暂停（避免与引擎互抢窗口）" if not still
                    else "面板嗅探器已请求停止（当前探针未返回，最多 30s 内退出）")
        except Exception as exc:  # noqa: BLE001
            return f"面板嗅探器暂停失败(忽略): {exc!r}"

    def _resume_panel_sniffer(self) -> str:
        """引擎停了，把面板嗅探器放回去（面板自己的出片路径仍然可用）。"""
        if not self._sniffer_suspended:
            return "无需恢复"
        try:
            import dola_window
            import browser_pool
            # 闸门是模块级单例；已 stop 的实例不能复用，先清掉再重新装配。
            dola_window._GATE = None
            pool = browser_pool.BrowserPool()
            win = pool.start_window_engine()
            self._sniffer_suspended = False
            return "面板嗅探器已恢复" if win.get("enabled") else "面板嗅探器恢复失败(未启用)"
        except Exception as exc:  # noqa: BLE001
            return f"面板嗅探器恢复失败: {exc!r}"

    # ------------------------------------------------------------------ 生命周期
    def _clear_stop(self, tries: int = 5) -> None:
        """删除 STOP 文件（引擎靠它优雅收尾；启动前必须确保它不存在）。

        重试几次：Windows 上文件可能被杀软/索引短暂占用，删失败会让新引擎秒退。
        """
        for _ in range(tries):
            try:
                STOP_FILE.unlink()
                return
            except FileNotFoundError:
                return
            except OSError:
                time.sleep(0.2)

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _clear_sentinels(self) -> None:
        """清掉上一轮残留的队列哨兵与快照。

        为什么必须清：哨兵是「一次性命令」，靠引擎的 queue 线程消费后删除。
        若上一轮引擎在消费前就退出了（或面板写完哨兵但引擎没起来），
        残留文件会让**下一轮引擎刚起来就执行清空/覆盖** —— 把刚写好的队列清掉。
        快照同理：留着会让面板前几秒显示上一轮的队列，看起来像"新队列没生效"。
        """
        for f in (CLEAR_FLAG, RELOAD_FLAG, QUEUE_STATE):
            for _ in range(3):
                try:
                    f.unlink()
                    break
                except FileNotFoundError:
                    break
                except OSError:
                    time.sleep(0.2)

    def _pid_file_running(self, fresh: bool = False) -> int:
        """有没有**别的进程**（例如重启前的旧 server）正在跑本引擎。

        为什么要查：引擎是独立进程，server 重启后内存里的 Popen 句柄就丢了。
        此时面板会显示"未运行"，用户一点启动就会拉起**第二个引擎** ——
        两个引擎各带一个嗅探器，正是 fact 629 那个反模式。
        两条线索取其一：① PID 文件；② 直接扫进程表里 commandline 含 run_pool.py
        且工作目录是本项目的 python 进程（覆盖"PID 文件还没写"的历史进程）。

        fresh=True：绕过进程枚举缓存，强制实时查（``start()`` 的判重必须用这个，
        否则可能因为缓存里"没有孤儿引擎"而拉起第二个引擎）。
        默认 False：走短 TTL 缓存 —— 展示路径（status/queue）每 5 秒刷一次，
        每次 4 个 wmic 子进程是纯浪费。
        """
        # ① PID 文件（正常路径，最精确）
        try:
            if PID_FILE.is_file():
                pid = int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
                if pid > 0 and self._pid_is_our_engine(pid, fresh=fresh):
                    return pid
        except (OSError, ValueError):
            pass
        # ② 进程表兜底：找 commandline 指向本目录 run_pool.py 的进程
        for pid in self._scan_engine_pids(fresh=fresh):
            return pid
        return 0

    def _scan_engine_pids(self, fresh: bool = False) -> list[int]:
        """扫进程表，返回跑着本项目 run_pool.py 的 PID（排除自己）。

        wmic 的输出是 **OEM/GBK** 编码且带 CSV 引号，直接用 text=True(utf-8)
        会抛 UnicodeDecodeError（实测踩到）。这里显式按 mbcs 解码、再宽松匹配。

        fresh=False 时结果进 _PROC_CACHE_TTL 秒 TTL 缓存：一次 status() 会连着
        问好几次（_pid_file_running / _pid_is_our_engine / elapsed），实测同一份
        进程表被枚举 2 次、每次 ~0.2s；加上 5 秒一次的自动刷新，等于把 CPU 全花
        在枚举进程表上。
        """
        cache_key = "scan_engine_pids"
        if not fresh:
            hit = _proc_cache_get(cache_key)
            if hit is not None:
                return list(hit)
        import subprocess as _sp
        pids: list[int] = []
        script = str(ENGINE_SCRIPT)
        try:
            out = _sp.run(["wmic", "process", "where", "name='python.exe'",
                           "get", "ProcessId,CommandLine", "/format:csv"],
                          capture_output=True, timeout=20,
                          creationflags=_no_window_flags())
            raw = out.stdout or b""
            try:
                text = raw.decode("mbcs", errors="replace")
            except (LookupError, AttributeError):
                text = raw.decode("utf-8", errors="replace")
            me = os.getpid()
            for line in text.splitlines():
                if "run_pool" not in line:
                    continue
                # 匹配本项目脚本路径：wmic 可能把路径里的 \ 转义，故用文件名+目录双重弱匹配
                if script not in line and "run_pool.py" not in line:
                    continue
                # 该行最后一个纯数字字段就是 ProcessId
                for cell in reversed(line.split(",")):
                    cell = cell.strip().strip('"')
                    if cell.isdigit():
                        pid = int(cell)
                        if pid != me:
                            pids.append(pid)
                        break
        except Exception:  # noqa: BLE001
            pass
        if not fresh:
            _proc_cache_put(cache_key, list(pids))
        return pids

    def _pid_is_our_engine(self, pid: int, fresh: bool = False) -> bool:
        """确认该 PID 仍活着且确实是本项目 run_pool.py（防 PID 复用误判）。"""
        if pid <= 0:
            return False
        if pid in self._scan_engine_pids(fresh=fresh):
            return True
        try:
            import subprocess as _sp
            out = _sp.run(["wmic", "process", "where", f"ProcessId={pid}",
                           "get", "CommandLine", "/value"],
                          capture_output=True, timeout=10,
                          creationflags=_no_window_flags())
            text = (out.stdout or b"").decode("mbcs", errors="replace")
            return "run_pool" in text
        except Exception:  # noqa: BLE001
            return False

    def start(self, cfg: dict) -> tuple[bool, str]:
        with self._lock:
            if self.running():
                return False, "引擎已在运行"
            # ★ fresh=True：判重绝不能吃缓存 —— 缓存里"没有孤儿引擎"可能只是
            #   3 秒前的事实，此刻引擎已被别的入口拉起，再 spawn 就是两个引擎。
            orphan = self._pid_file_running(fresh=True)
            if orphan:
                return False, (f"检测到已有引擎在运行（pid={orphan}，可能是重启前面板拉起的）。"
                               "请先在系统里结束它，或用『停止』按钮收尾后再启动。")

            if not ENGINE_SCRIPT.is_file():
                return False, f"找不到引擎脚本: {ENGINE_SCRIPT}"

            # 任务文件：UI 传来的 prompts 直接落盘（一行一条）。
            # 语义：prompts 为空/全注释 = **沿用现有 tasks.txt**（不是清空！）。
            # 面板上「留空则沿用现有 tasks.txt」就是这个分支；否则用户想复用旧队列
            # 时会被误判成"提示词为空"而启动失败。
            #
            # ★ 必须**成对**写 tasks.txt + tasks.meta.jsonl：只写 tasks.txt 会让
            #   两文件条数不一致，而引擎的 load_tasks() 对不一致是**拒绝启动**的
            #   （防止把 A 的素材错挂到 B 的提示词上）。旧「批量出片」路径就是这么
            #   只写 tasks.txt 的，实测把队列搞成 1 条 vs 103 条、引擎起不来。
            #   这里批量出片不带结构化参数，所以 meta 全部写空 dict。
            prompts = cfg.get("prompts")
            if prompts is not None and str(prompts).strip():
                # ★ 按 **空行分段** 而不是按行拆分：调用方传来的可能是一整段
                #   多行分镜剧本（创作台的输入框天然是多行的），按行 splitlines
                #   会把一条任务摊成 N 条。这里以「一个及以上空行」作为任务
                #   分隔符，段落内部的换行原样保留（写入时再转义成 \n 字面量）。
                #   完全单行的调用方（批量出片的 textarea）行为不变。
                lines = [str(p).strip() for p in str(prompts).splitlines()]
                keep = []
                for ln in lines:
                    if not ln or ln.startswith("#"):
                        continue
                    s, _ = self._strip_prio(ln)
                    s = unescape_prompt_newlines(s)
                    if s:
                        keep.append(s)
                if keep:
                    self._write_tasks_with_meta(
                        [escape_prompt_newlines(p) for p in keep],
                        [{} for _ in keep])
                else:
                    return False, "提示词为空（留空则沿用现有 tasks.txt）"
            # 沿用现有任务文件时，至少要有一个任务，否则引擎会立刻空跑退出
            if not TASKS_FILE.is_file():
                return False, f"没有任务：{TASKS_FILE} 不存在且未提供提示词"
            _existing = [ln for ln in TASKS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
                         if ln.strip() and not ln.strip().startswith("#")]
            if not _existing:
                return False, "tasks.txt 里没有有效提示词"
            self._task_count = len(_existing)

            # 参数合并（UI > 默认），并校验类型
            params = dict(DEFAULTS)
            for k, v in (cfg.get("params") or {}).items():
                if k in params and v not in (None, ""):
                    params[k] = v
            self._params = params

            for d in (ACC, OUT, MP4DIR):
                d.mkdir(parents=True, exist_ok=True)
            # 上一轮的 STOP 必须清掉，否则引擎一起来就收尾退出。
            # ★ 注意时机：STOP 只在【真正 spawn 之前】清一次是不够的 ——
            #   中间 _suspend_panel_sniffer() 最多要等 40 秒（等面板哨兵线程退出），
            #   若这期间另一个 stop()（或上一次收尾）落了 STOP，新引擎起来就会立刻收尾。
            #   实测踩到：15:34:03 启动，15:34:23 被一个更早的 STOP 杀掉。
            #   因此这里先清一次，spawn 前再清一次（见下面 _clear_stop()）。
            self._clear_stop()

            note = self._suspend_panel_sniffer()

            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            for k, env_name in PARAM_ENV.items():
                env[env_name] = str(params.get(k, DEFAULTS.get(k, "")))
            env["I794_TASKS_FILE"] = "tasks.txt"
            # 队列热加载（面板追加提示词后不必重启引擎）
            env["I794_QUEUE_WATCH"] = "1" if os.environ.get("I794_QUEUE_WATCH", "1") != "0" else "0"
            # 出口策略与面板一致（三段式；.env.local 已配 residential）
            env.setdefault("DOLA_WINDOW_SNIFF", "1")
            env["DOLA_WINDOW_REPORT_PATH"] = str(OUT / "gate.jsonl")

            ENGINE_LOG.parent.mkdir(parents=True, exist_ok=True)
            lf = open(ENGINE_LOG, "a", encoding="utf-8", errors="replace")
            lf.write("\n" + "=" * 78 + "\n")
            lf.write("[launcher] %s 启动引擎 params=%s\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), json.dumps(params, ensure_ascii=False)))
            lf.flush()

            # ★ spawn 前再清一次 STOP：上面 _suspend_panel_sniffer() 可能耗时数十秒，
            #   期间若有 stop() 落过 STOP，新引擎会在第一轮循环就收尾退出。
            self._clear_stop()
            # 队列哨兵/快照同理（残留会让新引擎一起来就清空刚写的队列）
            self._clear_sentinels()

            try:
                self._proc = subprocess.Popen(
                    [sys.executable, "-u", str(ENGINE_SCRIPT)],
                    cwd=str(RUN), env=env,
                    stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=_no_window_flags(),
                )
            except Exception as exc:  # noqa: BLE001
                self._resume_panel_sniffer()
                return False, f"引擎启动失败: {exc!r}"

            self._started_at = time.time()
            self._stopping = False
            self._last_exit = {}
            # 写 PID 文件：server 重启后仍能识别"已有引擎在跑"，避免重复启动
            try:
                PID_FILE.parent.mkdir(parents=True, exist_ok=True)
                PID_FILE.write_text(str(self._proc.pid), encoding="utf-8")
            except OSError:
                pass
            # 自愈看门狗：引擎可能**自己**退出（任务跑完 / RUN_SECONDS 到点 / 崩溃），
            # 这条路径不会经过 stop()。若不在此回收，面板的窗口嗅探器会一直被留在
            # 「已暂停」状态 —— 表现为面板出片永久失效，且没有任何报错。故起一个守护
            # 线程盯着进程，一旦退出就自动收尾（恢复嗅探器 + 记录退出码）。
            threading.Thread(target=self._reap_when_exit, args=(self._proc,),
                             name="pool-engine-reaper", daemon=True).start()
            return True, f"引擎已启动 pid={self._proc.pid}；{note}"

    def _reap_when_exit(self, proc: subprocess.Popen) -> None:
        """引擎进程结束后自动收尾（无论它是被 stop() 杀的还是自己退出的）。"""
        try:
            rc = proc.wait()
        except Exception:  # noqa: BLE001
            rc = None
        with self._lock:
            # 只回收"仍是当前引擎"的那一个：stop() 可能已经处理过并换了新实例
            if self._proc is not proc:
                return
            # 记住这次退出是不是「用户主动停的」：主动停意味着用户不想再出片，
            # 这时不该偷偷降级去豆包生产（尊重用户意图）。必须先于下面清零读取。
            was_stopping = bool(self._stopping)
            self._proc = None
            self._stopping = False
            self._last_exit = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "returncode": rc}
            try:
                PID_FILE.unlink()
            except OSError:
                pass
            restore = self._resume_panel_sniffer()
        try:
            with open(ENGINE_LOG, "a", encoding="utf-8", errors="replace") as fh:
                fh.write("[launcher] 引擎进程结束 rc=%s；%s\n" % (rc, restore))
        except OSError:
            pass
        # 引擎退出 → 通知外部（豆包降级器）：把没出片的任务兜底跑掉。
        # 放在 _lock 外面调用，且回调自己负责不抛异常，避免拖住收尾。
        cb = self.on_exit
        if cb is not None:
            try:
                cb(rc, was_stopping)
            except Exception as exc:  # noqa: BLE001
                try:
                    with open(ENGINE_LOG, "a", encoding="utf-8", errors="replace") as fh:
                        fh.write("[launcher] on_exit 回调异常: %r\n" % (exc,))
                except OSError:
                    pass

    def stop(self, grace: float = 60.0) -> tuple[bool, str]:
        """优雅停止：先落 STOP 让引擎自己收尾（会写 summary、收干净在飞的收割）。"""
        with self._lock:
            if not self.running():
                # server 重启过、手上没有句柄，但 PID 文件显示旧引擎还在跑：
                # 落 STOP 让它自己收尾（它每 5 秒查一次 STOP），并还原嗅探器。
                # 同上：停止路径也必须实时判重，不能因为缓存漏掉在跑的孤儿引擎。
                orphan = self._pid_file_running(fresh=True)
                if orphan:
                    STOP_FILE.write_text("stop\n", encoding="utf-8")
                    self._sniffer_suspended = False   # 新进程本就没暂停过
                    return True, (f"已向既有引擎(pid={orphan})发出停止信号（落 STOP 文件）；"
                                  "它会在收尾后自行退出")
                self._resume_panel_sniffer()
                return False, "引擎未在运行"
            STOP_FILE.write_text("stop\n", encoding="utf-8")
            proc = self._proc
            self._stopping = True

        deadline = time.time() + grace
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(1.0)

        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            note = "超过宽限期，已强制结束"
        else:
            note = "引擎已优雅收尾"

        with self._lock:
            self._proc = None
            self._stopping = False
            try:
                PID_FILE.unlink()
            except OSError:
                pass
            restore = self._resume_panel_sniffer()
        return True, f"{note}；{restore}"

    # ------------------------------------------------------------------ 状态
    def read_queue_state(self) -> dict:
        """读引擎写的队列快照 out/queue_state.json（内存真实状态）。

        引擎没跑时没有这份快照，退化为「按 tasks.txt 反推 pending」——
        这样面板在引擎停机时依然能看到「下次启动会跑哪些提示词」。

        ★ 快照还要过「新鲜度」这一关：引擎已停但快照文件还在（实测踩到——
          引擎停了几小时，快照仍是旧的，面板把它当当前队列显示，与 /status
          从台账算出的实时进度自相矛盾）。故只有 mtime 距今
          < QUEUE_STATE_MAX_AGE 秒才认它。
        """
        age = None
        try:
            age = _file_age(QUEUE_STATE)
        except Exception:  # noqa: BLE001
            age = None
        if QUEUE_STATE.is_file() and age is not None and age < QUEUE_STATE_MAX_AGE:
            try:
                d = json.loads(QUEUE_STATE.read_text(encoding="utf-8"))
                if isinstance(d, dict) and d.get("rows") is not None:
                    d["age"] = round(age, 1)
                    return d
            except Exception:  # noqa: BLE001
                pass
        rows, n = [], 0
        try:
            if TASKS_FILE.is_file():
                # 引擎没跑时按文件反推：连 tasks.meta.jsonl 一起读，
                # 让面板在停机状态下也能看到"哪些任务带了素材、按什么参数出"。
                metas: list[dict] = []
                meta_file = TASKS_FILE.parent / "tasks.meta.jsonl"
                if meta_file.is_file():
                    for raw in meta_file.read_text(
                            encoding="utf-8", errors="replace").splitlines():
                        s = raw.strip()
                        try:
                            metas.append(json.loads(s) if s else {})
                        except (TypeError, ValueError):
                            metas.append({})
                for idx, raw in enumerate(
                        TASKS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()):
                    s = raw.strip()
                    if not s or s.startswith("#"):
                        continue
                    prio = s.startswith(PRIO_MARK)
                    if prio:
                        # 优先标记是 tasks.txt 的文件内协议，不是提示词的一部分。
                        # 引擎跑着时 run_pool._parse_task_line 会剥掉；停机反推这条
                        # 路径必须自己剥，否则面板（任务中心/队列面板）会把
                        # "@@PRIO@@提示词" 原样显示给用户。
                        s = s[len(PRIO_MARK):].strip()
                        if not s:
                            continue
                    # 多行提示词还原成真换行：面板展示与用户输入一致，
                    # 不会出现 "第一行\n第二行\n第三行" 这种字面量。
                    s = unescape_prompt_newlines(s)
                    meta = metas[idx] if idx < len(metas) else {}
                    n += 1
                    row = {"tid": "t%03d" % n, "status": "pending", "account": "",
                           "conv": "", "attempts": 0, "mp4": "", "prompt": s,
                           "prio": prio}
                    row.update({
                        "channel": str(meta.get("channel") or "dola"),
                        # ★ 产物类型必须带出来：面板靠它判断"同队列不混渠道"
                        #   是按哪条车道判的，以及图片任务该用 <img> 还是 <video>。
                        #   缺省 video = 老队列/手编 tasks.txt 的既有语义不变。
                        "kind": str(meta.get("kind") or "video"),
                        "ratio": meta.get("ratio") or "",
                        "duration": meta.get("duration") or None,
                        "model": meta.get("model") or "",
                        "asset_count": len(meta.get("asset_paths") or []),
                        "asset_names": list(meta.get("asset_names") or []),
                    })
                    rows.append(row)
        except OSError:
            pass
        return {"generated_at": "", "count": n,
                "counts": {"pending": n, "accepted": 0, "done": 0, "failed": 0},
                "rows": rows, "stale": True}

    def queue_clear(self) -> tuple[bool, str, int]:
        """清空队列：只清尚未开工的 pending，已受理 / 在收割的不打断。

        实现：给引擎落一个 QUEUE_CLEAR 哨兵文件，由 run_pool 的 queue 线程消费
        （它持有 TaskQueue 引用，能安全地加锁标记 cancelled）。
        引擎没跑时，直接重写 tasks.txt 为空即可 —— 没有活动队列要保护。
        """
        # ★ fresh=True：写路径（清空会直接改写 tasks.txt）必须实时判引擎是否在跑。
        if not self.running() and not self._pid_file_running(fresh=True):
            try:
                # ★ 两个文件一起清：只清 tasks.txt 会留下条数不一致的 meta，
                #   引擎下次启动会被 load_tasks() 拒绝（见 start() 的同类注释）。
                self._write_tasks_with_meta([], [])
            except OSError as exc:
                return False, f"清空 tasks.txt 失败: {exc}", 0
            return True, "引擎未运行，已清空 tasks.txt", 0

        before = self.read_queue_state()
        pending = int((before.get("counts") or {}).get("pending") or 0)
        try:
            CLEAR_FLAG.write_text(str(time.time()), encoding="utf-8")
        except OSError as exc:
            return False, f"写入清空哨兵失败: {exc}", 0
        # 等引擎的 queue 线程消费（周期 3s，给足 8s）
        deadline = time.time() + 8.0
        while time.time() < deadline:
            time.sleep(0.4)
            if not CLEAR_FLAG.exists():
                break
        try:
            after = self.read_queue_state()
            left = int((after.get("counts") or {}).get("pending") or 0)
        except Exception:  # noqa: BLE001
            left = 0
        return True, (f"已清空 {max(0, pending - left)} 条待处理任务"
                      if pending else "队列里没有待处理任务"), max(0, pending - left)

    # ------------------------------------------------------------ 结构化提交
    def submit_tasks(self, items: list[dict], *, mode: str = "append",
                     params: dict | None = None,
                     prio: bool = False) -> dict:
        """创作台/发起出片提交：带结构化参数（参考素材/比例/时长/模型/渠道）的任务入队。

        items: [{"prompt": str, "meta": {...}}, ...]
        mode:  append 追加 / replace 覆盖（清空后重建）
        prio:  True = 这批任务带 @@PRIO@@ 优先标记（「发起出片」用，插到普通
               批量任务之前；否则排在几百条后面要等好几个出片窗口）。

        ★ 与旧「只写提示词」的区别：这里**成对**写 tasks.txt 与
          tasks.meta.jsonl（按下标一一对应）。两处会改变行数的改写
          （清空保留、reload 重装）在 run_pool 侧也同步维护 meta，
          因此覆盖后依然对齐；不一致时引擎会拒绝启动而不是错配。

        ★ 优先标记是 tasks.txt 的**文件内协议**（与 run_pool._parse_task_line
          一致）：写入时加在提示词前面，读取时由本类与 run_pool 各自剥掉，
          提示词/台账/面板展示里都不会残留。
        """
        clean: list[dict] = []
        for it in items or []:
            prompt = str((it or {}).get("prompt") or "").strip()
            # 允许调用方直接把带标记的提示词传进来（dispatch 的 prompt 可能
            # 已被 PRIO_MARK 拼接过）：这里统一剥一次再按 prio 决定是否重加，
            # 避免出现 "@@PRIO@@@@PRIO@@提示词" 这种双层标记。
            prompt, had_prio = self._strip_prio(prompt)
            # 已带换行转义的老数据再入队（热加载往返 / 二次提交）时先还原，
            # 否则 \n 会被二次转义成 \\n，提示词越跑越难看。
            prompt = unescape_prompt_newlines(prompt)
            if not prompt or prompt.startswith("#"):
                continue
            if had_prio:
                prio = True
            clean.append({"prompt": prompt,
                          "meta": dict((it or {}).get("meta") or {})})
        if not clean:
            return {"ok": False, "error": "提示词不能为空"}
        mode = (mode or "append").strip().lower()
        if mode not in ("append", "replace"):
            return {"ok": False, "error": "mode 只能是 append 或 replace"}

        # ★ fresh=True：提交要据此决定"引擎在跑就落热加载/覆盖哨兵"，判错会让
        #   新任务在引擎里静默不生效（或覆盖不生效）。
        running = self.running() or bool(self._pid_file_running(fresh=True))
        final_prompts: list[str] = []
        final_metas: list[dict] = []
        if mode == "replace":
            final_prompts = [c["prompt"] for c in clean]
            final_metas = [c["meta"] for c in clean]
        else:
            existing_prompts, existing_metas = self._read_tasks_with_meta()
            final_prompts = existing_prompts + [c["prompt"] for c in clean]
            final_metas = existing_metas + [c["meta"] for c in clean]

        # 覆盖模式：引擎在跑时先让它按新文件全量重建队列（否则旧 pending 会继续跑）
        if mode == "replace" and running:
            ok, msg = self.queue_reload()
            if not ok:
                return {"ok": False, "error": f"覆盖队列失败：{msg}"}
        # 优先标记只在**写入文件**这一刻加上；内存里始终是干净提示词，
        # 这样返回值/台账/面板都不会看到 @@PRIO@@。
        # 优先标记 + 换行转义只在**写入文件**这一刻加上；内存里始终是干净提示词，
        # 这样返回值/台账/面板都不会看到 @@PRIO@@ 或 \n 字面量。
        # 多行提示词必须转义：tasks.txt 是「一行一条」协议，不转义就会把一段
        # 分镜剧本按行拆成 N 条独立任务（用户提交 1 条、列表多出十几条）。
        file_prompts = [f"{PRIO_MARK}{escape_prompt_newlines(p)}" if prio
                        else escape_prompt_newlines(p) for p in final_prompts]
        self._write_tasks_with_meta(file_prompts, final_metas)

        return {"ok": True, "count": len(final_prompts),
                "added": len(clean), "mode": mode,
                "prio": bool(prio),
                "engine_running": running,
                "message": ("已入队（引擎运行中，热加载接单）" if running
                            else "已入队（引擎未运行，下次启动时生效）")}

    @staticmethod
    def _strip_prio(prompt: str) -> tuple[str, bool]:
        """剥掉提示词前面的优先标记，返回 (干净提示词, 是否带过标记)。

        幂等：无论加过几次标记都只剥一次最前面的，返回值可直接用于展示。
        """
        s = str(prompt or "")
        if s.startswith(PRIO_MARK):
            return s[len(PRIO_MARK):].strip(), True
        return s.strip(), False

    @staticmethod
    def _read_tasks_with_meta() -> tuple[list[str], list[dict]]:
        """读 tasks.txt + tasks.meta.jsonl → (提示词列表, meta 列表)，按下标对齐。

        没有 meta 文件（或条数不足）时按空 meta 补足，保证两列表等长。
        优先标记在这里剥掉（与 run_pool._parse_task_line 一致）：tasks.txt 里
        可能残留上一轮 dispatch 写的 @@PRIO@@，读出来必须是干净提示词，
        否则再次写入时会二次拼接，面板/台账也会看到标记字符。
        """
        prompts: list[str] = []
        metas: list[dict] = []
        try:
            if TASKS_FILE.is_file():
                for ln in TASKS_FILE.read_text(
                        encoding="utf-8", errors="replace").splitlines():
                    s = ln.strip()
                    if not s or s.startswith("#"):
                        continue
                    if s.startswith(PRIO_MARK):
                        s = s[len(PRIO_MARK):].strip()
                        if not s:
                            continue
                    # 多行提示词还原成真换行：调用方（面板展示 / 二次入队 /
                    # 引擎热加载）拿到的始终是用户原始输入。
                    prompts.append(unescape_prompt_newlines(s))
        except OSError:
            pass
        meta_file = TASKS_FILE.parent / "tasks.meta.jsonl"
        if meta_file.is_file():
            try:
                for raw in meta_file.read_text(
                        encoding="utf-8", errors="replace").splitlines():
                    s = raw.strip()
                    try:
                        metas.append(json.loads(s) if s else {})
                    except (TypeError, ValueError):
                        metas.append({})
            except OSError:
                metas = []
        if len(metas) != len(prompts):
            metas = (metas + [{}] * len(prompts))[:len(prompts)]
        return prompts, metas

    @staticmethod
    def _write_tasks_with_meta(prompts: list[str], metas: list[dict]) -> None:
        """原子成对写 tasks.txt + tasks.meta.jsonl（先写 meta 再写 tasks）。

        顺序说明：meta 先落盘、tasks 后落盘。若中途崩了，最坏是「meta 比 tasks 长」，
        引擎会在 load_tasks 里报条数不一致并拒绝启动（显式失败）；
        反过来「tasks 比 meta 长」则更危险（提示词被当成无参数任务照常跑）。
        """
        if len(metas) != len(prompts):
            metas = [{} for _ in prompts]
        meta_file = TASKS_FILE.parent / "tasks.meta.jsonl"
        tmp = meta_file.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for m in metas:
                fh.write(json.dumps(m or {}, ensure_ascii=False) + "\n")
        os.replace(tmp, meta_file)
        TASKS_FILE.write_text("\n".join(prompts) + ("\n" if prompts else ""),
                              encoding="utf-8")

    def read_summary(self) -> dict | None:
        """读引擎退出时写的 summary_pool.json（豆包降级要用它的 tasks_detail）。"""
        try:
            if SUMMARY.is_file():
                d = json.loads(SUMMARY.read_text(encoding="utf-8"))
                return d if isinstance(d, dict) else None
        except Exception:  # noqa: BLE001
            pass
        return None

    def queue_reload(self, wait_sec: float = 8.0) -> tuple[bool, str]:
        """让引擎按 tasks.txt 全量重建待处理队列（覆盖模式用）。

        与 queue_clear 的区别：clear 只清空；reload 清空后**重新装入文件里的全部提示词**。
        引擎没跑时无需操作 —— 下次启动 load_tasks() 本来就会读最新文件。
        """
        # ★ fresh=True：写路径（覆盖模式会重建队列）必须实时判引擎是否在跑。
        if not self.running() and not self._pid_file_running(fresh=True):
            n = 0
            try:
                if TASKS_FILE.is_file():
                    n = sum(1 for ln in TASKS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
                            if ln.strip() and not ln.strip().startswith("#"))
            except OSError:
                pass
            return True, f"引擎未运行（下次启动按 {n} 条装载）"
        try:
            RELOAD_FLAG.write_text(str(time.time()), encoding="utf-8")
        except OSError as exc:
            return False, f"写入覆盖哨兵失败: {exc}"
        deadline = time.time() + max(2.0, wait_sec)
        while time.time() < deadline:
            time.sleep(0.4)
            if not RELOAD_FLAG.exists():
                break
        snap = self.read_queue_state()
        return True, f"已按 tasks.txt 重建队列（当前 {snap.get('count') or 0} 条）"

    def _pool_groups(self) -> dict:
        """按前缀统计号池目录（引擎视角）。"""
        out = {"burst": 0, "probe": 0, "other": 0}
        try:
            burst = set(str(self._params.get("burst_pats") or "pool").split(","))
            probe = str(self._params.get("probe_pat") or "probe")
            for d in ACC.iterdir():
                if not d.is_dir() or d.name.startswith("."):
                    continue
                if not (d / "cookie_state.json").is_file():
                    continue
                if d.name.rpartition("_")[0] in burst:
                    out["burst"] += 1
                elif d.name.rpartition("_")[0] == probe:
                    out["probe"] += 1
                else:
                    out["other"] += 1
        except OSError:
            pass
        return out

    def _task_count_now(self) -> int:
        """当前 tasks.txt 里的有效提示词条数（不依赖是否启动过引擎）。"""
        try:
            if not TASKS_FILE.is_file():
                return 0
            return sum(1 for ln in TASKS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
                       if ln.strip() and not ln.strip().startswith("#"))
        except OSError:
            return 0

    def _videos(self, limit: int = 60) -> list[dict]:
        items = []
        try:
            files = sorted(MP4DIR.glob("*.mp4"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
            for p in files:
                st = p.stat()
                items.append({
                    "name": p.name,
                    "bytes": st.st_size,
                    "mb": round(st.st_size / 1048576, 2),
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                    "watermarked": p.stem.endswith("_WM"),
                    "url": f"{config.PUBLIC_BASE}/engine-mp4/{p.name}",
                })
        except OSError:
            pass
        return items

    def _run_started_tstr(self) -> str:
        """本轮引擎的启动时刻（台账 tstr 同格式），用于把任务态事件按轮次切开。

        为什么要它：tid 每轮重用，ledger 里同一 tid 有多代记录。不切轮次，
        上一轮的成功会被算到这一轮同名任务上（实测：这轮 t001~t003 还在
        排队，面板却显示"完成 3 个"——全是上一轮 14:26~15:13 的成绩）。

        取值优先级：
          1) 本进程拉起的引擎 → _started_at（最准）；
          2) 外部/重启前拉起的引擎 → 进程创建时间（wmic）；
          3) 都取不到 → 空串（退化成全量统计，至少不会更错）。

        ★ 只被 status() 调用，属纯展示路径 → 走进程查询缓存。
          引擎的创建时刻在它整个生命周期里不变，缓存它没有任何正确性代价。
        """
        pid = 0
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(self._started_at))
            pid = self._pid_file_running()
        if pid:
            el = _proc_elapsed(pid)          # 已运行秒数
            if el > 0:
                return time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(time.time() - el))
        # 兜底：用最近一次引擎启动日志的时间戳
        try:
            txt = ENGINE_LOG.read_text(encoding="utf-8", errors="replace")[-200000:]
            for line in reversed(txt.splitlines()):
                m = re.search(r"\[launcher\]\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+启动引擎",
                              line)
                if m:
                    return m.group(1)
        except OSError:
            pass
        return ""

    def status(self, log_lines: int = 120) -> dict:
        running = self.running()
        # server 若重启过，句柄丢了但引擎仍在跑 —— 如实反映，别让用户以为停了然后重复启动
        external_pid = 0 if running else self._pid_file_running()
        alive = running or bool(external_pid)

        # ---- 实时统计：以 ledger.jsonl 为准（对齐 A 的 monitor.py）----
        # summary_pool.json 只在引擎退出时写一次，跑着的时候是上一轮的旧值，
        # 拿它当"当前进度"会让面板永远显示 0（已受理也不更新）。
        events = _ledger_events(LEDGER)
        # 任务清单优先取**引擎内存快照**（queue_state.json，够新鲜时）——
        # 它反映引擎真正在跑的任务；tasks.txt 只是提示词的磁盘投影，
        # 可能因热加载/外部改写与内存不一致（实测：tasks.txt 3 行，
        # 内存队列 4 个任务 → 两个页签数字对不上）。
        tasks_text = []
        snap_rows = []
        try:
            age = _file_age(QUEUE_STATE)
            if QUEUE_STATE.is_file() and age is not None and age < QUEUE_STATE_MAX_AGE:
                d = json.loads(QUEUE_STATE.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    snap_rows = [r for r in (d.get("rows") or []) if r.get("tid")]
        except Exception:  # noqa: BLE001
            snap_rows = []
        if snap_rows:
            tasks_text = [{"tid": str(r.get("tid")),
                           "prompt": str(r.get("prompt") or "")} for r in snap_rows]
        else:
            try:
                if TASKS_FILE.is_file():
                    n = 0
                    for raw in TASKS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
                        s = raw.strip()
                        if not s or s.startswith("#"):
                            continue
                        n += 1
                        tasks_text.append({"tid": "t%03d" % n, "prompt": s})
            except OSError:
                pass
        live = _analyze_events(events, tasks_text, since_tstr=self._run_started_tstr())
        # 引擎内存快照的状态更权威（它直接来自 TaskQueue）：快照新鲜时，
        # 用快照的 status/account 覆盖台账推算值，避免两处不一致。
        if snap_rows:
            by_tid = {str(r.get("tid")): r for r in snap_rows}
            for row in live["task_rows"]:
                s = by_tid.get(str(row.get("tid")))
                if not s:
                    continue
                st = str(s.get("status") or "")
                if st:
                    row["status"] = st
                if s.get("account"):
                    row["account"] = s.get("account")
                if s.get("conv"):
                    row["conv"] = s.get("conv")
            recount = {"pending": 0, "accepted": 0, "done": 0, "failed": 0}
            for row in live["task_rows"]:
                s = str(row.get("status") or "pending")
                key = "done" if s.startswith("done") else s
                recount[key] = recount.get(key, 0) + 1
            live["tasks"] = recount

        # 引擎退出后若已有 summary，用它补全（含最终态与参数快照）
        summary = None
        if not running and SUMMARY.is_file():
            try:
                summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                summary = None

        submits = []
        for line in _tail(SUBMIT_LOG, 12):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            submits.append({k: rec.get(k) for k in
                            ("tstr", "account", "outcome", "error_code", "elapsed")})

        # 号池就绪数：引擎参数里 pool_target 与实际可见号的对照
        pool = self._pool_groups()

        # ---- 存活/心跳：区分「活着但暂时没窗口」和「卡死」----
        # 引擎活着时会持续写这些文件，取"最新鲜的那个"当心跳：
        #   * submit.jsonl      每 6~10 秒一条探针记录（正常工作时的主心跳）
        #   * engine_console.log 任何输出（含 seg-exit / prepare 阶段）
        # ★ 只用 submit.jsonl 是不够的：引擎启动后要先批量预建 50 个常驻 session
        #   （实测 ~35s），这期间一条 submit 都不会写 —— 旧实现会把**正在正常启动**的
        #   引擎误报成 alive=False。所以取三者中最小的 age。
        beat = _file_age(SUBMIT_LOG)
        console_age = _file_age(ENGINE_LOG)
        ages = [a for a in (beat, console_age, _file_age(LEDGER)) if a is not None]
        fresh = min(ages) if ages else None
        alive_beat = bool(running or external_pid) and fresh is not None and fresh < 90
        return {
            "running": alive,
            "managed": running,
            "external_pid": external_pid,
            "pid": (self._proc.pid if running and self._proc else external_pid),
            "stopping": self._stopping,
            "elapsed": (round(time.time() - self._started_at, 1) if running
                        else _proc_elapsed(external_pid)),
            "heartbeat_age": round(fresh, 1) if fresh is not None else None,
            "alive_beat": alive_beat,
            "submit_age": round(beat, 1) if beat is not None else None,
            "console_age": round(console_age, 1) if console_age is not None else None,
            "ledger_age": round(_file_age(LEDGER), 1) if _file_age(LEDGER) is not None else None,
            "params": self._params,
            # 实时进度（来自台账）
            "tasks": live["tasks"],
            "task_rows": live["task_rows"],
            "task_count": len(tasks_text),
            "windows": live["windows"],
            "bursts": live["bursts"],
            "accepted": live["accepted"],
            "harvest_ok": live["harvest_ok"],
            "harvest_bad": live["harvest_bad"],
            "unmarked": live["unmarked"],
            "marked": live["marked"],
            "fails": live["fails"],
            "last_window": live["last_window"],
            "burst_wall_last": live["burst_wall_last"],
            "summary_at": (summary or {}).get("generated_at") or "",
            "summary_tasks": (summary or {}).get("tasks") or {},
            "pool": pool,
            "videos": self._videos(),
            # 注意：台账是 append 的，6000 行窗口对长跑足够；给前端看最近 25 条
            "events": events[-25:],
            "submits": submits,
            "log_tail": _tail(ENGINE_LOG, log_lines) if log_lines else [],
            "engine_script": str(ENGINE_SCRIPT),
            "tasks_file": str(TASKS_FILE),
            "last_exit": self._last_exit,
        }


manager = PoolEngineManager()
