# -*- coding: utf-8 -*-
r"""run_pool.py — DOLA 出片引擎（任务队列版）：号池恒定轮换 + 探针轮换 + 延迟收割

用法:
    1) 把提示词写进 tasks.txt（一行一条，# 开头为注释，空行跳过）
    2) python run_pool.py          # 一直跑到全部任务完成；本目录建 STOP 空文件可停

参数（环境变量，均有默认值）:
    I794_TASKS_FILE       任务文件            默认 tasks.txt
    I794_POOL_TARGET      爆发池保持可用号数  默认 50
    I794_MAX_PER_ACC      每号每日出片数      默认 2（用满即删号；上游真实配额 3）
    I794_BURST_PATS       爆发号前缀          默认 pool   （号池目录 acc/pool_NNNN/）
    I794_PROBE_PAT        探针号前缀          默认 probe  （探针目录 acc/probe_NNNN/）
    I794_PROBE_STANDBY    探针备用数量        默认 6
    I794_SNIFF_PERIOD     探针周期(秒)        默认 6  （6~10s 推荐；<=12s 不会漏 12s 窗）
    I794_SNIFF_JITTER     探针抖动(秒)        默认 2
    I794_BURST_CONC       爆发并发上限        默认 50
    I794_HARVEST_WORKERS  收割线程            默认 16
    I794_HARVEST_TIMEOUT  单 conv 收割上限秒  默认 9600（2h40m）
    I794_HARVEST_INTERVAL 收割轮询间隔秒      默认 60
    I794_REGISTER_WORKERS 注册并发            默认 5
    I794_TASK_ATTEMPTS    单任务最大重试      默认 3
    I794_RUN_SECONDS      最长运行秒数        默认 0=不限（任务全部完成即停）

机制:
  * 探针轮换：单嗅探线程 + 专用探针号池（绝不进爆发池）。探针配额烧完/登录失效/
    连续异常自动切换下一个备用号；备用不足由注册线程自动补。
    探针命中的那次提交直接使用队列中的下一条真实 prompt（探针配额不浪费）。
  * 号池轮换：每号出 MAX_PER_ACC 片（默认 2）。受理即占用 → 收割成功或判死后
    整目录移入 acc/.deleted/ 留档；注册线程持续补号保持可用号 = POOL_TARGET。
  * 收割解耦：受理 conv 立即落 out/ledger.jsonl，独立收割线程长轮询
    （默认 2h40m 上限），MP4 按 <task_id>_<conv8>.mp4 落盘 + ftypisom 校验。
  * 提交失败（窗关/异常）任务自动回队重试；账号失效自动删除补充。
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

_SELF = os.path.dirname(os.path.abspath(__file__))


def _find_data_root(_start, _depth=4):
    """数据根 = 含 DOLA-pool-main\\ 或 tasks.txt 的那一级（向上最多找 4 层）。

    本文件可能就在数据根，也可能在其下的 engine\\ 子目录 —— 两种布局都定位到
    同一份 acc/ out/ pool/ tasks.txt/ STOP，不会各建一套空数据。
    """
    _cur = _start
    for _ in range(_depth):
        if (os.path.isdir(os.path.join(_cur, "DOLA-pool-main"))
                or os.path.exists(os.path.join(_cur, "tasks.txt"))):
            return _cur
        _parent = os.path.dirname(_cur)
        if _parent == _cur:
            break
        _cur = _parent
    return _start


HERE = _find_data_root(_SELF)
BASE = os.path.join(HERE, "DOLA-pool-main")
for _p in (_SELF, HERE, BASE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---- 强制 UTF-8 标准输出/错误流 ---------------------------------------------
# 双击 .bat 启动时控制台常是 GBK 代码页，中文日志会抛 UnicodeEncodeError
# 把工作线程打死（表现为"跑到一半突然没反应"）。这里在最早时机强制 UTF-8，
# 与启动脚本里的 chcp 65001 / PYTHONUTF8=1 双保险。
for _s in ("stdout", "stderr"):
    _f = getattr(sys, _s, None)
    try:
        if _f is not None and hasattr(_f, "reconfigure"):
            _f.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---- 出口策略（与 run.py 相同：注册/提交走 Clash 7897，下载本地直连）----
# DOLA_SUBMIT_EXIT=clash → 走本机 Clash：这里把值规范成 direct 并补上 DOLA_PROXY。
# （three_segment_exit.apply 也认 clash 别名；两处保持一致，避免面板/引擎行为分叉。）
_EXIT = os.environ.get("DOLA_SUBMIT_EXIT", "clash").strip().lower()
if _EXIT in ("clash", "7897", "clash7897", "local-clash", "direct", ""):
    os.environ["DOLA_SUBMIT_EXIT"] = "direct"
    os.environ["DOLA_PROXY"] = os.environ.get("DOLA_PROXY_7897", "http://127.0.0.1:7897")
elif _EXIT in ("residential", "resi", "iprocket"):
    os.environ["DOLA_SUBMIT_EXIT"] = "residential"
    os.environ["DOLA_PROXY"] = ""
os.environ.setdefault("DOLA_REGISTER_PROXY", "http://127.0.0.1:7897")

ACC = os.path.join(HERE, "acc")
OUT = os.path.join(HERE, "out")
TRASH = os.path.join(ACC, ".deleted")
MP4DIR = os.path.join(OUT, "mp4")
# ★ 图片产物目录（Seedream 出图）。与 mp4 分开：一条图片任务是**多张**产物，
#   且没有 ftypisom/时长这些视频概念，混在一个目录里会让"视频库"扫到图片。
IMGDIR = os.path.join(OUT, "img")
for _d in (ACC, OUT, TRASH, MP4DIR, IMGDIR):
    os.makedirs(_d, exist_ok=True)
os.environ.setdefault("DOLA_WINDOW_REPORT_PATH", os.path.join(OUT, "gate.jsonl"))
os.environ.setdefault("DOLA_WINDOW_SNIFF", "1")

# config 必须早于下面的参数区导入：无水印开关要读 config.PURE_API_REMOVE_WATERMARK，
# 而且 import config 会顺带装配三段出口策略（three_segment_exit.apply）。
import config            # noqa: E402  (DOLA-pool-main/config.py，含三段出口 apply)
import dola_window as W  # noqa: E402
import dc                # noqa: E402  (根目录注册协议)
import reg as regmod     # noqa: E402  (复用 to_dbda_state；仅用其函数，不触发注册)

# ---- 参数 ----
TASKS_FILE = os.path.join(HERE, os.environ.get("I794_TASKS_FILE", "tasks.txt"))
POOL_TARGET = int(os.environ.get("I794_POOL_TARGET", "50"))
MAX_PER_ACC = int(os.environ.get("I794_MAX_PER_ACC", "2"))
BURST_PATS = [p.strip() for p in os.environ.get("I794_BURST_PATS", "pool").split(",") if p.strip()]
PROBE_PAT = os.environ.get("I794_PROBE_PAT", "probe")
PROBE_STANDBY = int(os.environ.get("I794_PROBE_STANDBY", "6"))
SNIFF_PERIOD = float(os.environ.get("I794_SNIFF_PERIOD", "6"))
SNIFF_JITTER = float(os.environ.get("I794_SNIFF_JITTER", "2"))
BURST_CONC = int(os.environ.get("I794_BURST_CONC", "50"))
HARVEST_WORKERS = int(os.environ.get("I794_HARVEST_WORKERS", "16"))
HARVEST_TIMEOUT = int(os.environ.get("I794_HARVEST_TIMEOUT", "9600"))
# 【IPRocket 流量治理 · 2026-10-17】收割轮询间隔由 DOLA_PURE_POLL_INTERVAL 统一驱动
# （默认 120s）。旧默认 60s：单次 /im/chain/single 压缩后 3.85 KB，一条 60 分钟的
# 30 秒任务光收割就是 60 次 ≈ 0.23 MB；120s 减半，且出片发现延迟仍远小于上游抖动。
# 要单独给收割段一个值时，显式设 I794_HARVEST_INTERVAL 即可覆盖。
HARVEST_INTERVAL = int(os.environ.get(
    "I794_HARVEST_INTERVAL", str(getattr(config, "POLL_INTERVAL", 60))))
REGISTER_WORKERS = int(os.environ.get("I794_REGISTER_WORKERS", "5"))
TASK_ATTEMPTS = int(os.environ.get("I794_TASK_ATTEMPTS", "3"))
# 环境类结果（窗关/换号/出口异常）不消耗 attempts，但仍设一个总上限：
# 防止环境持续异常时任务无限循环、引擎永不退出。默认 400 次 ≈ 数十小时。
ENV_HIT_LIMIT = int(os.environ.get("I794_ENV_HIT_LIMIT", "400"))
RUN_SECONDS = int(os.environ.get("I794_RUN_SECONDS", "0"))
RATIO = os.environ.get("I794_RATIO", "16:9")
DURATION = int(os.environ.get("I794_DURATION", "30"))
MODEL = os.environ.get("I794_MODEL", "seedance_v2.5")   # 30s 只能走 v2.5（★下划线）
PROBE_FALLBACK_PROMPT = os.environ.get("I794_PROBE_PROMPT", "窗口探測")
# ---- 队列热加载（面板「批量出片」追加提示词后不必重启引擎）----
# 面板把新提示词追加到 tasks.txt；本线程按 mtime 感知，把新行接续编号塞进运行中的队列。
# 关掉它（I794_QUEUE_WATCH=0）即退回「只在启动时读一次」的旧行为。
QUEUE_WATCH = os.environ.get("I794_QUEUE_WATCH", "1").strip().lower() not in ("0", "false", "no", "")
QUEUE_WATCH_PERIOD = float(os.environ.get("I794_QUEUE_WATCH_PERIOD", "3"))
# 队列快照落盘（面板「队列面板」读它；以内存为准，不用 tasks.txt 反推）
QUEUE_STATE = os.path.join(OUT, "queue_state.json")
# ---- 结构化任务元数据（创作台传参）----
# tasks.txt 是**人可读/可手编的提示词投影**（一行一条）；每个任务的
# 素材/比例/时长/模型/渠道等结构化参数放在同目录的 tasks.meta.jsonl 里，
# **按行一一对应**（第 N 行 meta ↔ 第 N 条提示词）。
#
# 为什么不把 JSON 直接塞进 tasks.txt 每行：
#   tasks.txt 是对外可手编的接口（_read_task_lines / 面板"读取 tasks.txt"都用它），
#   内嵌 JSON 会让人工编辑一行就崩，且 _sync_tasks_file_after_clear 等
#   三处改写逻辑全要跟着改。sidecar 让老行为（无 meta 文件）**完全不变**。
#
# ★ 顺序对齐是硬约束：两处会改变行数/tid 的改写（清空保留、reload 重装）
#   必须同步维护 meta 文件；loader 发现条数不一致会**拒绝启动**而不是静默错配
#   （错配会把 A 的素材挂到 B 的提示词上，属数据正确性事故）。
TASKS_META_FILE = os.path.join(
    HERE, os.environ.get("I794_TASKS_META_FILE", "tasks.meta.jsonl"))
# 无 meta 时的默认渠道（老队列/手编 tasks.txt 都按 dola 走）
DEFAULT_CHANNEL = os.environ.get("I794_CHANNEL", "dola").strip().lower() or "dola"
# ---- 无水印（hint：成片必须无水印）----
# REMOVE_WATERMARK=True 时，除了 Dola 自带的 lr=unwatermarked 直链，还会调用第三方
# 去水印解析服务（http://47.104.150.143:8765/tools/dola/）兜底；失败自动回退直链。
# 对应 config.PURE_API_REMOVE_WATERMARK（默认 1=开）。
REMOVE_WATERMARK = os.environ.get("I794_REMOVE_WATERMARK",
                                  "1" if getattr(config, "PURE_API_REMOVE_WATERMARK", True) else "0"
                                  ).strip().lower() not in ("0", "false", "no", "")
# 拿不到无水印源时怎么办（I794_WATERMARK_FALLBACK）：
#   save  (默认) 仍保存该片，文件名加 _WM 后缀并在台账标 unwatermarked=false，
#                任务标为 done_watermarked —— 不浪费已消耗的额度，且结果一眼可辨；
#   retry        换号重试（最多 TASK_ATTEMPTS 次后判失败）—— 会重复消耗额度；
#   fail        直接判任务失败，不保存。
# 之所以默认 save：去水印解析是对所有号共用的外部服务，它一旦不可用，
# retry 会把每个任务的额度白烧 3 遍却仍然拿不到无水印片。
WATERMARK_FALLBACK = os.environ.get("I794_WATERMARK_FALLBACK", "save").strip().lower()
if WATERMARK_FALLBACK not in ("save", "retry", "fail"):
    WATERMARK_FALLBACK = "save"


def is_unwatermarked_url(url) -> bool:
    """判断下载链接是否是无水印源。

    与 protocol/dola_pure_api.is_direct_nowatermark_url 同判据：
    查询参数 lr=unwatermarked 或 logo_type=unwatermarked，或 URL 里出现该词；
    fplay 中转链接不算（它只是解密中转，不是成片直链）。
    """
    u = str(url or "")
    if not u or "/video/fplay/" in u.lower():
        return False
    low = u.lower()
    if "unwatermarked" in low:
        return True
    try:
        from urllib.parse import urlsplit, parse_qsl
        q = dict(parse_qsl(urlsplit(u).query, keep_blank_values=True))
        return (str(q.get("lr") or "").lower() == "unwatermarked"
                or str(q.get("logo_type") or "").lower() == "unwatermarked")
    except Exception:
        return False


# 环境类结果（非任务自身失败）：回队不消耗重试次数。
#   OUT_WINDOW_CLOSED   710022002 窗口中途关闭 / 探针未命中
#   OUT_QUOTA_EXHAUSTED 该号当日配额烧完（换号即可，任务本身没问题）
#   OUT_LOGIN_INVALID   该号登录态失效（换号即可）
#   OUT_COUNTRY         出口地区受限（换出口即可）
#   OUT_EXC             提交抛异常（多为会话/网络问题，不是 prompt 的问题）
ENV_OUTCOMES = (W.OUT_WINDOW_CLOSED, W.OUT_QUOTA_EXHAUSTED,
                W.OUT_LOGIN_INVALID, W.OUT_COUNTRY, W.OUT_EXC)


# ---------------------------------------------------------------- 公共工具 --
_lock_ledger = threading.Lock()


def ledger(**rec):
    rec.setdefault("tstr", time.strftime("%Y-%m-%d %H:%M:%S"))
    with _lock_ledger:
        with open(os.path.join(OUT, "ledger.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def pr(msg):
    """打印一行日志。

    防御性：控制台若仍是非 UTF-8 代码页（未跑 chcp 65001），中文会抛
    UnicodeEncodeError 并把线程直接打死 —— 宁可降级显示问号也不能中断引擎。
    """
    try:
        print(msg, flush=True)
    except (UnicodeEncodeError, UnicodeError):
        try:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(str(msg).encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:
            pass
    except Exception:
        pass


def is_env_outcome(oc) -> bool:
    """True = 非任务自身原因（窗关/换号即可/换出口即可），回队不吃重试次数。"""
    return oc in ENV_OUTCOMES


def ftypisom_ok(path):
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
        return (os.path.getsize(path) > 20000 and
                head[4:12] in (b"ftypisom", b"ftypiso2", b"ftypmp42", b"ftypM4V "))
    except OSError:
        return False


# ---------------------------------------------------------------- 任务队列 --
class Task:
    __slots__ = ("tid", "prompt", "status", "account", "conv", "mp4", "attempts",
                 "accepted_at", "last_outcome", "env_hits", "prio", "meta")

    def __init__(self, tid, prompt, prio=False, meta=None):
        self.tid, self.prompt = tid, prompt
        self.status = "pending"      # pending -> flying -> accepted -> done/failed
        self.account = ""
        self.conv = ""
        self.mp4 = ""
        self.attempts = 0
        self.accepted_at = 0.0
        self.last_outcome = ""       # 最近一次提交结果（决定是否消耗重试次数）
        self.env_hits = 0            # 环境类结果累计次数（上限兜底，防无限循环）
        # True = 优先任务（面板「发起出片」/创作台提交的小批量）：
        # 取任务时**插到普通任务前面**，否则它会排在上百条批量任务之后，
        # 等好几个窗口才轮到 —— 用户点了"发起出片"却半天不出片，看起来像坏了。
        self.prio = bool(prio)
        # 结构化参数（创作台传入）：asset_paths / asset_ids / asset_names /
        # ratio / duration / model / channel / client。
        # {} = 走全局 I794_* 默认值（老队列/手编 tasks.txt 的行为完全不变）。
        # 取值一律经 param()/ratio/duration/model/channel 属性，不要在别处直接读 meta。
        self.meta = dict(meta) if meta else {}

    # ---- 参数取值：meta 优先，回落全局默认 ----

    def param(self, key, default):
        """取结构化参数；meta 里没有（或为空）时回落全局默认。"""
        value = (self.meta or {}).get(key)
        return default if value in (None, "") else value

    @property
    def ratio(self):
        return self.param("ratio", RATIO)

    @property
    def duration(self):
        try:
            return int(self.param("duration", DURATION))
        except (TypeError, ValueError):
            return DURATION

    @property
    def model(self):
        return self.param("model", MODEL)

    @property
    def channel(self):
        return str(self.param("channel", DEFAULT_CHANNEL)).strip().lower() or DEFAULT_CHANNEL

    @property
    def kind(self):
        """产物类型：``video``（默认）或 ``image``。

        ★ 必须带默认值回落 video：老队列 / 手编 tasks.txt / 批量出片都不写
        kind，行为必须与从前**完全一致**（全部按视频跑）。
        """
        k = str(self.param("kind", "video")).strip().lower()
        return k if k in ("video", "image") else "video"

    @property
    def asset_paths(self):
        """参考图本地路径列表（素材库解析出来的绝对路径）。"""
        raw = (self.meta or {}).get("asset_paths") or []
        if isinstance(raw, str):
            return [raw] if raw else []
        return [str(x) for x in raw if x]

    def meta_public(self):
        """面板队列快照要暴露的字段（不含绝对路径全文，避免刷屏）。"""
        return {
            "channel": self.channel,
            "kind": self.kind,
            "ratio": self.ratio,
            "duration": self.duration,
            "model": self.model,
            "asset_count": len(self.asset_paths),
            "asset_names": list((self.meta or {}).get("asset_names") or []),
        }


class TaskQueue:
    def __init__(self, tasks):
        self._tasks = tasks
        self._lk = threading.Lock()

    def take_next(self, avoid=None, channel=None, kind=None):
        """取下一个待处理任务。

        avoid：**该号已经试过但没成功**的任务 id 集合（换号记忆的核心）。
        优先返回「这个号还没试过」的任务；只有当剩下的全是它试过的，
        才退回返回第一个 pending —— 否则一旦只剩这些任务，引擎会永远取不到活干。

        为什么需要它：任务失败/窗关会 put_back 回队，下一窗取号时若不避开，
        很可能又被**同一个号**拿走（号池遍历顺序稳定），于是这个任务反复
        栽在同一个号上，永远等不到别的号 —— 用户要的「换账号等下一次」就落空。

        优先级：prio=True 的任务（面板「发起出片」的小批量）**先于**普通批量任务
        被取走。否则它排在几百条批量任务之后，要等好几个窗口才轮到。

        channel：只取该渠道的任务。★ dola 的探针/爆发必须传 "dola" ——
        否则它们会把 channel=doubao 的任务取走，而 dola 的 SUB.submit 只认
        dola 域账号，取走也只会失败（把任务白白消耗一次尝试）。
        豆包车道用自己的 _next_doubao_task()（串行，不走这里）。

        kind：只取该产物类型的任务（video / image）。
        ★ dola 主车道必须传 kind="video"：dola 的窗口爆发提交器只出视频，
        图片任务被它取走只会失败一次；图片由豆包图片车道单独消费。
        """
        avoid = avoid or ()
        with self._lk:
            fallback = None          # 普通任务兜底
            prio_fb = None           # 优先任务兜底（它试败过也仍优先于普通任务）
            # 两轮扫描：先只看 prio，再看普通 —— 保证优先任务的"试败兜底"
            # 也排在普通任务之前（它已经被用户点名要出了）。
            for want_prio in (True, False):
                for t in self._tasks:
                    if t.status != "pending" or bool(t.prio) != want_prio:
                        continue
                    if channel and t.channel != channel:
                        continue
                    if kind and t.kind != kind:
                        continue
                    if t.tid not in avoid:
                        t.status = "flying"
                        return t
                    if want_prio:
                        if prio_fb is None:
                            prio_fb = t
                    elif fallback is None:
                        fallback = t
            for t in (prio_fb, fallback):
                if t is not None:
                    t.status = "flying"
                    return t
            return None

    def put_back(self, t, outcome=None, count_attempt=None):
        """任务回队（可从 flying 或 accepted 状态退回）。

        - flying：提交阶段未受理（窗关/异常）→ 退回等待下次窗口；
        - accepted：已受理但收割失败/超时（引擎会同时删掉那个号）→ 退回换号重试。

        count_attempt=None 时按「环境类结果不消耗重试次数」自动判定：
        探针每 6 秒嗅探一次都会借用并归还一条任务，窗口关闭是常态，
        绝不能让这些非任务自身原因吃掉任务的重试预算。
        """
        with self._lk:
            if t.status not in ("flying", "accepted"):
                return
            if outcome is not None:
                t.last_outcome = outcome
            if count_attempt is None:
                count_attempt = not is_env_outcome(t.last_outcome)
            if count_attempt:
                t.attempts += 1
                if t.attempts >= TASK_ATTEMPTS:
                    t.status = "failed"
                    ledger(kind="task_failed", task=t.tid, attempts=t.attempts,
                           last_outcome=t.last_outcome)
                    return
            else:
                # 环境类不消耗 attempts，但仍要有个上限：否则一旦环境持续异常
                # （例如出口长期被拒），任务会永远在队列里循环、引擎永不结束。
                t.env_hits += 1
                if t.env_hits >= ENV_HIT_LIMIT:
                    t.status = "failed"
                    ledger(kind="task_failed", task=t.tid, env_hits=t.env_hits,
                           last_outcome=t.last_outcome, reason="env_hit_limit")
                    return
            t.status = "pending"
            t.account = ""
            t.conv = ""

    def stats(self):
        with self._lk:
            c = {"pending": 0, "flying": 0, "accepted": 0, "done": 0, "failed": 0}
            for t in self._tasks:
                c[t.status] = c.get(t.status, 0) + 1
            return c

    def all_terminal(self):
        with self._lk:
            return all(t.status in ("done", "failed") for t in self._tasks)

    def sweep_stuck(self):
        """把长期卡在 flying 的任务放回队列（安全网，防引擎永不退出）。"""
        with self._lk:
            n = 0
            for t in self._tasks:
                if t.status == "flying":
                    t.status = "pending"
                    t.account = ""
                    t.conv = ""
                    n += 1
            return n

    def snapshot(self):
        with self._lk:
            return [dict(tid=t.tid, status=t.status, account=t.account, conv=t.conv,
                         mp4=t.mp4, attempts=t.attempts, prompt=t.prompt[:80])
                    for t in self._tasks]

    # ---- 热加载支持（面板追加/清空队列时调用）----

    def append_prompts(self, prompts, prio=False, metas=None):
        """把新提示词接续编号追加进队列。

        编号必须**接续**已有最大编号，否则新任务的 tid 会与历史台账/成片名
        （t001_xxx.mp4）撞车，面板上会看到两个 t001。

        prio=True：标记为优先任务（面板「发起出片」用），取任务时插到普通
        批量任务之前 —— 否则用户点完要等好几个窗口才轮到。
        metas：与 prompts 等长的结构化参数列表（创作台传参考图/时长/渠道用）；
               长度不符时整批按空 meta 处理（宁可丢参数，不允许错位）。
        返回 (新增条数, 新编号列表)。
        """
        metas = list(metas or [])
        if len(metas) != len(prompts):
            metas = [{} for _ in prompts]
        with self._lk:
            mx = 0
            for t in self._tasks:
                try:
                    mx = max(mx, int(str(t.tid).lstrip("t")))
                except (TypeError, ValueError):
                    pass
            added = []
            for p, m in zip(prompts, metas):
                mx += 1
                tid = "t%03d" % mx
                self._tasks.append(Task(tid, p, prio=prio, meta=m))
                added.append(tid)
            return len(added), added

    def cancel_pending(self):
        """把尚未开工的任务标为 cancelled（已受理/在收割的不动）。

        为什么不直接删：引擎的收尾逻辑（all_terminal / stats）依赖任务对象仍在，
        且 accepted 的任务还有收割线程持有引用。标记成 cancelled 既能让主循环
        立刻看到「没有 pending 了」，又不打断在飞的收割。
        """
        with self._lk:
            n = 0
            for t in self._tasks:
                if t.status == "pending":
                    t.status = "cancelled"
                    t.account = ""
                    t.conv = ""
                    n += 1
            return n

    def drop_cancelled(self):
        """把已取消的任务从列表里摘掉（只在收尾时调用，避免列表无限增长）。"""
        with self._lk:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t.status != "cancelled"]
            return before - len(self._tasks)

    def full_snapshot(self):
        """完整队列快照（供面板「队列面板」展示，含 pending）。

        带上结构化参数（渠道/比例/时长/模型/参考素材数）—— 创作台与队列面板
        要能一眼看出「这条任务带了哪些素材、按什么参数出」。
        """
        with self._lk:
            out = []
            for t in self._tasks:
                if t.status == "cancelled":
                    continue
                row = dict(tid=t.tid, status=t.status, account=t.account,
                           conv=t.conv, attempts=t.attempts, mp4=t.mp4,
                           prio=bool(t.prio), prompt=t.prompt)
                row.update(t.meta_public())
                out.append(row)
            return out


# 优先任务标记：面板「发起出片」往 tasks.txt 写这种前缀行。
# 为什么用文件内标记而不是内存标记：引擎可能随时重启（面板点停止/崩溃自愈/
# 热加载），内存里的 prio 会丢；写进文件则「重启后依然优先」，行为可预期。
PRIO_MARK = "@@PRIO@@"

# 多行提示词的换行转义符。
# 背景：tasks.txt 的协议是「一行一条提示词」，而创作台的输入框天然是多行的
#      （用户会写带分镜、分段、旁白的完整剧本）。历史实现直接把多行文本写进
#      文件 → 每一行都被当成独立任务 → 一条 30 秒视频被拆成 N 条 5~10 秒的
#      碎片任务，用户看到的是「提交 1 条，任务列表多出十几行」。
# 方案：写入前把 \r\n / \n / \r 统一换成 \n（字面量），读出来时再还原。
#      · 单行提示词完全不受影响（不含换行符 → 无转义 → 无反转义，字节级不变）；
#      · 已存在于 tasks.txt 的老数据不含该字面量，读出来也原样返回（向后兼容）；
#      · 提示词里真的写了 \n 字面量的概率极低，即便有也只在「写→读」往返时
#        变成换行，影响仅限展示层，不会把一条任务拆成多条。
_MULTILINE_ESC = "\\n"


def _escape_prompt_newlines(prompt: str) -> str:
    """把提示词里的真实换行转成 \\n 字面量，使其能安全地放进 tasks.txt 的一行。

    必须与 _unescape_prompt_newlines 严格互逆；单行文本原样返回。
    """
    s = str(prompt or "")
    if "\r\n" in s or "\n" in s or "\r" in s:
        s = s.replace("\r\n", _MULTILINE_ESC).replace("\n", _MULTILINE_ESC)
        s = s.replace("\r", _MULTILINE_ESC)
    return s


def _unescape_prompt_newlines(line: str) -> str:
    """把 \\n 字面量还原成真实换行（_escape_prompt_newlines 的逆操作）。

    不含该字面量的行原样返回 —— 老队列 / 手编 tasks.txt 行为完全不变。
    """
    s = str(line or "")
    if _MULTILINE_ESC in s:
        return s.replace(_MULTILINE_ESC, "\n")
    return s


# ------------------------------------------------------ 结构化元数据 sidecar --
# tasks.txt  = 人可读/可手编的提示词投影（一行一条，可带 @@PRIO@@ 前缀）
# tasks.meta.jsonl = 每行一条 JSON，**按下标与提示词一一对应**
#
# 两边必须由同一处原子成对维护；条数不一致时 load_tasks() 直接报错退出，
# 绝不静默错配（错配 = 把 A 的素材挂到 B 的提示词上，数据正确性事故）。
_META_FIELDS = ("asset_paths", "asset_ids", "asset_names", "ratio", "duration",
                "model", "channel", "client", "kind")


def _read_meta_file():
    """读 tasks.meta.jsonl → list[dict]（坏行按空 dict 占位，保持下标对齐）。"""
    out = []
    if not os.path.isfile(TASKS_META_FILE):
        return out
    try:
        with open(TASKS_META_FILE, "r", encoding="utf-8-sig") as fh:
            for raw in fh:
                s = raw.strip()
                if not s:
                    out.append({})
                    continue
                try:
                    rec = json.loads(s)
                except (TypeError, ValueError):
                    out.append({})          # 坏行占位：宁可丢参数也不许错位
                    continue
                if not isinstance(rec, dict):
                    out.append({})
                    continue
                out.append({k: v for k, v in rec.items() if k in _META_FIELDS})
    except OSError:
        return []
    return out


def _write_meta_file(metas):
    """原子写 tasks.meta.jsonl（临时文件 + os.replace，与队列快照同风格）。"""
    tmp = TASKS_META_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            for m in metas:
                fh.write(json.dumps(m or {}, ensure_ascii=False) + "\n")
        os.replace(tmp, TASKS_META_FILE)
    except OSError as exc:
        pr("[meta] 写 %s 失败（已忽略）: %r" % (os.path.basename(TASKS_META_FILE), exc))


def _parse_task_line(s):
    """把一行 tasks.txt 解析成 (提示词, 是否优先)。标记不会留在提示词里。"""
    s = (s or "").strip()
    if s.startswith(PRIO_MARK):
        return _unescape_prompt_newlines(s[len(PRIO_MARK):].strip()), True
    return _unescape_prompt_newlines(s), False


def load_tasks():
    """装载任务：提示词来自 tasks.txt，结构化参数来自 tasks.meta.jsonl。

    meta 是按**下标**与提示词对应的，所以严格校验条数：
      · 没有 meta 文件        → 全部走全局默认（老行为，完全不变）
      · 条数一致             → 逐条挂上
      · 条数不一致           → **直接报错退出**（见下方说明，绝不静默错配）
    """
    tasks = []
    if not os.path.isfile(TASKS_FILE):
        return tasks
    with open(TASKS_FILE, "r", encoding="utf-8-sig") as fh:
        n = 0
        for raw in fh:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            prompt, prio = _parse_task_line(s)
            if not prompt:
                continue
            n += 1
            tasks.append(Task("t%03d" % n, prompt, prio=prio))

    metas = _read_meta_file()
    if metas:
        if len(metas) != len(tasks):
            # ★ 绝不"尽力而为"地对齐：下标错位会把 A 任务的参考素材挂到 B 任务的
            #   提示词上，出的片子看着成功、内容却是错的，事后无法察觉。
            #
            # 但也不能**直接抛错让引擎起不来** —— 实测有第三方路径只写 tasks.txt
            # （旧「批量出片」、手工编辑、面板清空）会留下 meta 偏长，
            #   用户看到的是"引擎启动失败"，完全无法自行恢复。
            #
            # 所以按"能确定的部分"对齐并**自愈**：
            #   · meta 偏长（任务被删/覆盖）→ 截到与 tasks.txt 等长后重写文件。
            #     多出来的 meta 没有对应任务，本来就是死数据，丢掉不损失任何东西。
            #   · meta 偏短（任务被追加）→ 前面部分仍严格按下标对应，
            #     缺的尾部按空 meta 补齐（那些任务退化为无参考图/全局参数，
            #     与没有 meta 文件时的旧行为一致）。
            # 两种情况都**不打乱前 min(len) 条的对应关系**，因此不会错挂素材。
            if len(metas) > len(tasks):
                pr("[meta] ⚠ %s 有 %d 条但 %s 只有 %d 条："
                   "判定为任务被覆盖/删除，截断元数据并重写（多出的元数据无对应任务）"
                   % (os.path.basename(TASKS_META_FILE), len(metas),
                      os.path.basename(TASKS_FILE), len(tasks)))
                metas = metas[:len(tasks)]
                _write_meta_file(metas)
            else:
                pr("[meta] ⚠ %s 只有 %d 条但 %s 有 %d 条："
                   "尾部 %d 条任务按无参数处理（与无元数据文件的旧行为一致）"
                   % (os.path.basename(TASKS_META_FILE), len(metas),
                      os.path.basename(TASKS_FILE), len(tasks),
                      len(tasks) - len(metas)))
                metas = metas + [{}] * (len(tasks) - len(metas))
        for task, meta in zip(tasks, metas):
            task.meta = dict(meta or {})
        n_ref = sum(1 for t in tasks if t.asset_paths)
        if n_ref:
            pr("[meta] 已装载 %d 条任务，其中 %d 条带参考素材" % (len(tasks), n_ref))
    return tasks


def _read_task_lines():
    """读 tasks.txt 的有效提示词（跳过空行与 # 注释，**剥掉优先标记**）。

    剥标记是必须的：提示词会进台账/成片映射/面板展示，
    带着 @@PRIO@@ 前缀用户会看到莫名其妙的字符。

    多行提示词在这里**还原成真换行**（_parse_task_line 内部反转义）：
    调用方（指纹统计 / 队列装载 / 面板展示）拿到的始终是用户原始输入，
    不需要知道 tasks.txt 的单行存储协议。
    """
    out = []
    try:
        with open(TASKS_FILE, "r", encoding="utf-8-sig") as fh:
            for raw in fh:
                s = raw.strip()
                if not s or s.startswith("#"):
                    continue
                prompt, _ = _parse_task_line(s)
                if prompt:
                    out.append(prompt)
    except OSError:
        pass
    return out


def _read_task_entries():
    """读 tasks.txt 的 (提示词, 是否优先) 列表 —— 热加载用它保留优先级。"""
    out = []
    try:
        with open(TASKS_FILE, "r", encoding="utf-8-sig") as fh:
            for raw in fh:
                s = raw.strip()
                if not s or s.startswith("#"):
                    continue
                prompt, prio = _parse_task_line(s)
                if prompt:
                    out.append((prompt, prio))
    except OSError:
        pass
    return out


def _read_task_entries_with_meta():
    """(提示词, 是否优先, meta) 三元组 —— 热加载/覆盖必须连参数一起搬。

    两个文件按下标对齐；**长度不等时截断到较短的一侧并告警**：
    热加载路径不能因为元数据不全就把整个队列线程打死（那会让引擎
    看不见任何新任务），截断至少保证提示词能照常进队列。
    （启动路径 load_tasks() 是严格校验、直接报错的 —— 那里错配的代价更大。）
    """
    entries = _read_task_entries()
    metas = _read_meta_file()
    if len(metas) != len(entries):
        if metas:
            pr("[watch] ⚠ 元数据条数(%d)与提示词条数(%d)不一致，按较短一侧对齐；"
               "带参考图的任务可能丢失参数" % (len(metas), len(entries)))
        metas = (metas + [{}] * len(entries))[:len(entries)]
    return [(p, prio, m) for (p, prio), m in zip(entries, metas)]


def _tasks_signature():
    """tasks.txt(+meta) 的 (mtime_ns, 条数, meta_mtime_ns, meta 条数) 指纹。

    为什么用 mtime + 条数双条件：单看 mtime 会被「同秒内写两次」骗过（FAT/网络盘
    时间戳精度差），单看行数则漏掉「内容被替换但行数不变」。两者一起看足够稳。

    ★ 必须同时覆盖 tasks.meta.jsonl：创作台有时**只改参数不改提示词**
      （比如把同一条提示词的参考图换成另一张），只看 tasks.txt 的话
      热加载不会触发，参数改动要等引擎重启才生效。
    """
    try:
        st = os.stat(TASKS_FILE)
        sig = (st.st_mtime_ns, len(_read_task_lines()))
    except OSError:
        return None
    try:
        mst = os.stat(TASKS_META_FILE)
        sig += (mst.st_mtime_ns, len(_read_meta_file()))
    except OSError:
        sig += (0, 0)
    return sig


def queue_watch_loop(stop: threading.Event):
    """队列热加载 + 队列快照。

    两件事：
    1) 热加载：面板往 tasks.txt 追加提示词后，把它们接进运行中的队列。
       为什么需要：main() 只在启动时 load_tasks() 一次，TaskQueue 常驻内存；
       旧实现里跑着的时候追加提示词完全看不见，必须重启引擎才能吃 —— 而重启会
       丢掉窗口相位、打断在飞的收割。
    2) 快照：把**内存里的真实队列**写成 out/queue_state.json 供面板展示。
       为什么不继续用 tasks.txt 反推：清空队列后再追加时，内存 tid（接续编号）
       与 tasks.txt 的行号会脱钩，面板按行号反推会张冠李戴。以内存为准最可靠。

    只做追加 + 只读快照：绝不删改已有任务、不碰 probe/burst/harvest 任何逻辑。
    失败一律吞掉并继续（热加载坏了顶多回到旧行为，不能影响出片）。
    """
    sig = _tasks_signature()
    # ★ 基线必须取「队列里实际装了多少条」，**不能**重新读文件行数：
    #   面板可能在 load_tasks() 与本线程启动之间又追加了几行（两者相差几秒），
    #   重读文件会把这些行算进基线 → 它们永远不会被当作"新增"补进队列（静默丢失）。
    #   队列长度才是"已经吃进去的量"的权威值。
    loaded = len(tasks_q._tasks) if tasks_q is not None else len(_read_task_lines())
    # 立刻写一份快照：否则引擎起来后前几秒面板会显示上一轮（甚至在引擎未跑时的）
    # 反推结果，看起来像"队列没生效"。
    if tasks_q is not None:
        _write_queue_state(tasks_q)
    if QUEUE_WATCH:
        pr("[watch] 队列热加载已启动（周期 %.1fs，基线 %d 条，文件 %d 行）"
           % (QUEUE_WATCH_PERIOD, loaded, len(_read_task_lines())))
    else:
        pr("[watch] 队列热加载已关闭（I794_QUEUE_WATCH=0）；仅输出队列快照")
    while not stop.is_set():
        try:
            stop.wait(QUEUE_WATCH_PERIOD)
            if stop.is_set():
                break
            if tasks_q is None:
                continue

            # ---- 清空命令：面板落 QUEUE_CLEAR 哨兵文件 ----
            clear_flag = os.path.join(HERE, "QUEUE_CLEAR")
            if os.path.exists(clear_flag):
                try:
                    os.remove(clear_flag)
                except OSError:
                    pass
                n = tasks_q.cancel_pending()
                _sync_tasks_file_after_clear()
                loaded = len(_read_task_lines())
                sig = _tasks_signature()
                print("[watch] 收到清空命令：已取消 %d 条待处理任务（在飞的收割不打断）"
                      % n, flush=True)

            # ---- 覆盖命令：QUEUE_RELOAD —— 以 tasks.txt 为准重建待处理队列 ----
            # 为什么不能靠下面的「diff 追加」实现覆盖：追加是按行数差补的，
            # 若新列表比"保留行数"还短，差值 <= 0，新提示词就永远进不了队列。
            # 这里显式「取消全部 pending → 按文件全量重新装入」，结果与文件严格一致。
            reload_flag = os.path.join(HERE, "QUEUE_RELOAD")
            if os.path.exists(reload_flag):
                try:
                    os.remove(reload_flag)
                except OSError:
                    pass
                n = tasks_q.cancel_pending()
                entries = _read_task_entries_with_meta()
                added = 0
                tids = []
                for prompt, prio, meta in entries:
                    c, tt = tasks_q.append_prompts([prompt], prio=prio, metas=[meta])
                    added += c
                    tids += tt
                loaded = len(entries)
                sig = _tasks_signature()
                print("[watch] 收到覆盖命令：取消 %d 条，重装 %d 条（%s...）"
                      % (n, added, ",".join(tids[:5])), flush=True)

            # ---- 热加载：tasks.txt 增长 → 接续编号追加 ----
            if QUEUE_WATCH:
                cur = _tasks_signature()
                if cur is not None and cur != sig:
                    entries = _read_task_entries_with_meta()
                    if len(entries) > loaded:
                        new = entries[loaded:]
                        n = 0
                        tids = []
                        for prompt, prio, meta in new:
                            c, tt = tasks_q.append_prompts([prompt], prio=prio,
                                                           metas=[meta])
                            n += c
                            tids += tt
                        print("[watch] 追加 %d 条 → 队列共 %d 条（%s...）"
                              % (n, len(entries), ",".join(tids[:5])), flush=True)
                    loaded = len(entries)
                    sig = cur

            # ---- 快照：内存真实状态 ----
            _write_queue_state(tasks_q)
        except Exception as exc:  # noqa: BLE001
            try:
                pr("[watch] 队列线程异常（已忽略）: %r" % (exc,))
            except Exception:
                pass
    pr("[watch] 队列线程退出")


def _sync_tasks_file_after_clear():
    """清空后把 tasks.txt 也归零，让面板与本进程对「已提交队列」的认知一致。

    只删**待处理**的行：已受理/已完成的任务其 tid 与成片名已落台账，
    保留它们能让面板的提示词映射继续对得上。

    ★ 必须同步重写 tasks.meta.jsonl：本函数会**改变行数**（丢掉 pending 行），
      若 meta 不跟着裁，下次启动 load_tasks() 就会因条数不一致直接报错退出。
      meta 按下标与 tasks.txt 一一对应，所以按同一个 keep 列表裁。

    ★ 写回前要**转义提示词里的换行**：内存里的 prompt 可能是用户在多行输入框
      里写的完整分镜剧本，直接 write 会把一条任务摊成 N 行，下一轮 load_tasks()
      读回来就变成 N 条任务（实测：清空一次队列，一条 30 秒片被拆成十几条）。
    """
    try:
        if tasks_q is None:
            return
        keep = [t for t in tasks_q._tasks
                if t.status not in ("pending", "cancelled")]
        with open(TASKS_FILE, "w", encoding="utf-8") as fh:
            for t in keep:
                # 保留优先标记：否则清空一次就把已受理任务的优先级信息抹掉，
                # 万一引擎重启重读文件，它们会退化成普通任务。
                fh.write((PRIO_MARK if t.prio else "")
                         + _escape_prompt_newlines(t.prompt) + "\n")
        # meta 同步裁剪（与 tasks.txt 同顺序、同条数）
        if os.path.isfile(TASKS_META_FILE) or any(t.meta for t in keep):
            _write_meta_file([dict(t.meta or {}) for t in keep])
    except Exception:  # noqa: BLE001
        pass


def _write_queue_state(q):
    """原子写 out/queue_state.json（面板「队列面板」的数据源）。"""
    try:
        rows = q.full_snapshot()
        counts = q.stats()
        payload = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(rows),
            "counts": counts,
            "rows": rows,
        }
        tmp = QUEUE_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, QUEUE_STATE)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 号池管理 --
class Account:
    __slots__ = ("label", "state_path", "status", "quota_left", "self_built",
                 "tried_tasks")

    def __init__(self, label, state_path, self_built=True):
        self.label, self.state_path = label, state_path
        self.status = "available"    # available -> draining(额度用完待收割删) -> 删除
        self.quota_left = MAX_PER_ACC
        # True = 引擎自己注册出来的号（额度用尽可回收）；False = 存量号（只摘不搬）
        self.self_built = bool(self_built)
        # 该号本轮已经试过、但没成功的任务 id 集合 —— 用于「失败换号」：
        # 下一轮取任务时优先跳过"已经被这个号试败过"的组合，避免同一个号
        # 反复接同一个任务、而这个任务永远等不到别的号。
        self.tried_tasks = set()


pool: dict[str, Account] = {}
pool_lock = threading.Lock()
probes: dict[str, dict] = {}     # label -> {"state": path, "used": bool}
probe_lock = threading.Lock()
_ctr = {"pool": 0, "probe": 0}
_ctr_lock = threading.Lock()
harv_inflight = 0
harv_lock = threading.Lock()
harv_refs: dict[str, int] = {}   # label -> 该号在收的 conv 数（最后一个收完才删号）
harv_refs_lock = threading.Lock()

# 运行中的任务队列（main() 里装配）。模块级声明是为了让队列热加载线程
# 能直接引用它，而不用把引用一路传参下去。
tasks_q: "TaskQueue | None" = None


def harv_ref_inc(label):
    with harv_refs_lock:
        harv_refs[label] = harv_refs.get(label, 0) + 1


def harv_ref_dec(label):
    """最后一个 conv 收割完 → 返回 True（调用方删号）。"""
    with harv_refs_lock:
        n = harv_refs.get(label, 0) - 1
        if n <= 0:
            harv_refs.pop(label, None)
            return True
        harv_refs[label] = n
        return False


def harv_inc():
    """登记一个在跑的收割任务（主循环用它判断是否全部收完）。"""
    global harv_inflight
    with harv_lock:
        harv_inflight += 1


def harv_dec():
    global harv_inflight
    with harv_lock:
        harv_inflight = max(0, harv_inflight - 1)


def _next_label(prefix):
    with _ctr_lock:
        _ctr[prefix] = _ctr.get(prefix, 0) + 1
        n = _ctr[prefix]
    try:
        with open(os.path.join(OUT, "pool_counters.json"), "w", encoding="utf-8") as fh:
            json.dump(_ctr, fh)
    except OSError:
        pass
    return "%s_%04d" % (prefix, n)


def trash_account(label, reason):
    """把账号目录移入 acc/.deleted/ 并从内存池移除（含爆发池与探针池）。

    ★ 只回收【引擎自己注册出来的】号（pool_NNNN / probe_NNNN）。
    存量号（面板导入的 accN、以及任何引擎没登记为"自建"的号）**只移出内存、
    不搬目录** —— 它们是用户的资产，面板还在用；搬进 .deleted 等于把用户的号
    从面板上弄没（历史实测已误搬 59 个）。原因里会注明 no_move 便于核对。
    """
    with pool_lock:
        acc = pool.pop(label, None)
    with probe_lock:
        pr_rec = probes.pop(label, None)

    # self_built 可能来自爆发池或探针池（探针也会被回收）；两处都查
    self_built = bool(
        (acc is not None and getattr(acc, "self_built", False))
        or (pr_rec is not None and pr_rec.get("self_built", False)))
    if not self_built:
        # 存量号：只从本轮池子里摘掉，目录原地保留（额度用尽而已，不是坏了）
        ledger(kind="account_removed", label=label, reason=str(reason)[:120],
               moved=False, note="existing_account_kept")
        return

    src = os.path.join(ACC, label)
    dst = os.path.join(TRASH, "%s_%s" % (time.strftime("%m%d_%H%M%S"), label))
    moved = False
    try:
        if os.path.isdir(src):
            shutil.move(src, dst)
            moved = True
    except OSError as exc:
        ledger(kind="trash_error", label=label, err=repr(exc)[:150])
    ledger(kind="account_removed", label=label, reason=str(reason)[:120], moved=moved)


def add_burst_account(label, state_path, self_built=True):
    with pool_lock:
        pool[label] = Account(label, state_path, self_built=self_built)
    ledger(kind="account_added", label=label, role="burst",
           self_built=bool(self_built))


def add_probe_account(label, state_path, self_built=True):
    with probe_lock:
        probes[label] = {"state": state_path, "used": False,
                         "self_built": bool(self_built)}
    ledger(kind="account_added", label=label, role="probe",
           self_built=bool(self_built))


def probe_standby_count():
    with probe_lock:
        return sum(1 for p in probes.values() if not p["used"])


def _pool_ready_count():
    """当前可派发的号数（available 且有额度）。"""
    with pool_lock:
        return sum(1 for a in pool.values()
                   if a.status == "available" and a.quota_left > 0)


def note_tried(label, tid):
    """记住「这个号试过这个任务但没成」，供下一窗换号时避开。

    为什么必须有：任务失败会回队等待下一窗，而下一窗若还是这个号拿到它
    （号池遍历顺序稳定），这个任务就会反复栽在同一个号上 —— 用户要的
    「该号没成功就换账号等下一次」就永远不发生。
    """
    if not label or not tid:
        return
    with pool_lock:
        a = pool.get(label)
        if a is not None:
            a.tried_tasks.add(str(tid))


def refund_quota(label, n=1):
    """退回预扣的配额（提交失败/任务没发出去时调用）。

    为什么必须有它：爆发开始时按「计划提交数」预扣了 quota_left 并可能把号标成
    draining。若实际没发成功却没退回，该号就变成「不再派发、又不会因收割而删除」
    的僵尸号，号池会永久少一个位置（MAX_PER_ACC>1 时尤其明显）。
    """
    if n <= 0:
        return
    with pool_lock:
        a = pool.get(label)
        if a is None:
            return
        a.quota_left = min(MAX_PER_ACC, a.quota_left + int(n))
        if a.quota_left > 0 and a.status == "draining":
            a.status = "available"


def adopt_existing():
    """启动时收编 acc/ 下既有号目录（上次运行的存量），并把编号计数器同步到
    已存在的最大编号之后 —— 否则重新注册会生成同名目录并覆盖存量账号。

    ★ 收编范围 = **所有**含 cookie_state.json 的目录（不再只认带下划线的
      pool_NNNN/probe_NNNN）。面板导入的 acc1..acc50 同样具备纯 API 出片条件
      （sessionid 齐全），之前被 `"_" not in name` 直接跳过，导致引擎看得见的
      号比面板少一半 —— 用户要的「从账号池选号」就落不了地。

    命名规则（决定谁当探针、以及谁会被回收）：
      probe_*        → 探针池
      pool_*         → 爆发池
      其它（accN…）  → 爆发池，且标记 existing（额度用尽只摘不搬）
    """
    try:
        names = sorted(os.listdir(ACC))
    except OSError:
        return
    for name in names:
        if name.startswith("."):
            continue
        sp = os.path.join(ACC, name, "cookie_state.json")
        if not os.path.isfile(sp):
            continue
        if "_" in name:
            prefix, _, suffix = name.rpartition("_")
            if suffix.isdigit():
                with _ctr_lock:
                    _ctr[prefix] = max(_ctr.get(prefix, 0), int(suffix))
        else:
            prefix, suffix = name, ""
        # 存量号一律 self_built=False：它们是用户的资产，额度用尽时只移出内存。
        if prefix == PROBE_PAT:
            add_probe_account(name, sp, self_built=False)
        else:
            add_burst_account(name, sp, self_built=False)
    try:
        with open(os.path.join(OUT, "pool_counters.json"), "w", encoding="utf-8") as fh:
            json.dump(_ctr, fh)
    except OSError:
        pass


def register_account(prefix):
    """注册一个新号；成功返回 (label, state_path)，失败返回 (None, err)。
    内部对注册失败自动重试 2 次（一次性邮箱/网络抖动很常见）——号池要的是填满。"""
    last_err = "?"
    for _try in range(3):
        row, err = dc.register_one(wait=200)
        if not row:
            last_err = err or "register failed"
            continue
        label = _next_label(prefix)
        row["label"] = label
        try:
            dc.export_account(label, row)
        except Exception:
            pass
        d = os.path.join(ACC, label)
        os.makedirs(d, exist_ok=True)
        sp = os.path.join(d, "cookie_state.json")
        with open(sp, "w", encoding="utf-8") as fh:
            json.dump(regmod.to_dbda_state(label, row), fh, ensure_ascii=False, indent=2)
        try:
            chk = dc.login_check(row["cookie"])
        except Exception as exc:
            chk = {}
            last_err = "login_check exc %r" % (exc,)
        if chk.get("user_id") and not chk.get("code"):
            return (label, sp), ""
        # 注册成功但登录态没验过：删掉重来（不同步留废号污染池子）
        trash_account(label, "register_login_check_failed")
        last_err = "login_check failed"
    return None, last_err


def replenisher_loop(stop: threading.Event, SUB):
    """持续补号：爆发池保持 POOL_TARGET 可用号（含正在出片的），探针保持 PROBE_STANDBY 备用。

    关键：必须扣除「正在注册中」的数量，否则每 10 秒一轮会把同一个缺口重复派发，
    5 个 worker 排不完，最终注册出 3 倍于目标数量的账号。
    """
    inflight = {"burst": 0, "probe": 0}
    ilock = threading.Lock()
    with ThreadPoolExecutor(max_workers=REGISTER_WORKERS) as tp:
        while not stop.is_set():
            try:
                with pool_lock:
                    active = len(pool)
                with ilock:
                    pend_b, pend_p = inflight["burst"], inflight["probe"]
                want_pool = max(0, POOL_TARGET - active - pend_b)
                want_probe = max(0, PROBE_STANDBY - probe_standby_count() - pend_p)
                futs = []
                if want_pool or want_probe:
                    with ilock:
                        inflight["burst"] += want_pool
                        inflight["probe"] += want_probe
                    pr("[replenish] pool_total=%d(+%d注册中%d) probe_standby=%d(+%d注册中%d)"
                       % (active, want_pool, pend_b, probe_standby_count(), want_probe, pend_p))
                for _ in range(want_pool):
                    futs.append(("burst", tp.submit(register_account, BURST_PATS[0])))
                for _ in range(want_probe):
                    futs.append(("probe", tp.submit(register_account, PROBE_PAT)))

                new_burst = []

                def _done(kind, f):
                    with ilock:
                        inflight[kind] = max(0, inflight[kind] - 1)
                    try:
                        r = f.result()
                    except Exception as exc:
                        ledger(kind="register_error", err=repr(exc)[:150])
                        return
                    # register_account 返回 ((label, state_path), err)
                    # 必须逐层解包：r[0] 才是 (label, sp) 元组本身。
                    if r and r[0]:
                        (label, sp), _err = r
                        (add_burst_account if kind == "burst" else add_probe_account)(label, sp)
                        if kind == "burst":
                            new_burst.append(sp)
                    else:
                        ledger(kind="register_failed",
                               err=((r[1] if r and len(r) > 1 else "?"))[:150])

                for kind, f in futs:
                    _done(kind, f)
                # 新号的常驻 session 批量并行预建（逐个 prepare 每个 ~35s，会串行拖死）
                # ★ 顺带把队列引用的参考素材也预上传到新号（否则窗口里才传会吃掉窗口）
                if new_burst:
                    try:
                        SUB.prepare_many(new_burst, workers=min(16, max(4, len(new_burst))),
                                         assets=_queued_asset_paths() or None)
                    except Exception as exc:
                        ledger(kind="prepare_error", err=repr(exc)[:150])
            except Exception:
                ledger(kind="replenish_exc", err=traceback.format_exc()[-300:])
            stop.wait(10)


def _state_cookie_header(st: dict) -> str:
    """从 cookie_state.json 里取 Cookie 头文本，兼容两种落盘格式。

    为什么需要：号池里有两种号 ——
      * reg.py/dc.py 注册的号：有 ``cookie_header`` 文本；
      * 面板 cookie 导入的号（acc1..acc10）：只有 ``cookies`` 列表，没有 cookie_header。
    旧实现只读 cookie_header，对这 10 个号恒为空字符串 → login_check 必然失败
    → liveness_loop 会把**健康的号**当 dead 删掉。
    """
    ck = (st.get("cookie_header") or "").strip()
    if ck:
        return ck
    recs = st.get("cookies") or st.get("cookies_list") or []
    if isinstance(recs, list):
        return "; ".join("%s=%s" % (r.get("name"), r.get("value"))
                         for r in recs
                         if isinstance(r, dict) and r.get("name"))
    return ""


def _account_alive(state_path: str) -> bool:
    """读该号 state 做一次 login_check：True=登录态有效。"""
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            ck = _state_cookie_header(json.load(fh))
        if not ck:
            return False
        c = dc.login_check(ck)
        return bool(c.get("user_id") and not c.get("code"))
    except Exception:
        return False


def liveness_loop(stop: threading.Event):
    """每 15 分钟对可用号做 login_check；确认失效的号才删除（由补号线程补齐）。

    ★ 2026-10-16 加固（两处）：
    1. **两次判定才删号**。login_check 走网络，单次超时/代理抖动/上游 710 都会让它返回空；
       原实现"一次不过就删号"，会把完全健康的号扔进 .deleted。
       （实测：一次 60 秒短跑就误删 4 个事后复检仍存活的号。）
       现在：首轮失败 → 隔 3 秒重新请求一次；**两次都失败**才判死。
    2. **兼容 cookies 列表格式**的 state（见 _state_cookie_header）。
    """
    stop.wait(60)                              # 启动后 1 分钟先做一轮
    while not stop.is_set():
        try:
            with pool_lock:
                labs = [a for a in pool.values() if a.status == "available"]
            if labs:
                with ThreadPoolExecutor(max_workers=8) as tp:
                    first = list(tp.map(lambda a: (a, _account_alive(a.state_path)), labs))

                failed = [a for a, ok in first if not ok]
                dead = []
                if failed:
                    # 隔 3 秒复检：只有"两次都失败"才真判死，避免单次网络抖动误杀。
                    stop.wait(3)
                    with ThreadPoolExecutor(max_workers=8) as tp:
                        second = list(tp.map(lambda a: (a, _account_alive(a.state_path)), failed))
                    dead = [a for a, ok in second if not ok]

                for a in dead:
                    trash_account(a.label, "liveness_dead")
                pr("[liveness] checked %d, dead removed %d%s"
                   % (len(labs), len(dead),
                      ("（首轮 %d 个失败，复检后存活 %d）" % (len(failed), len(failed) - len(dead)))
                      if failed else ""))
        except Exception:
            ledger(kind="liveness_exc", err=traceback.format_exc()[-300:])
        stop.wait(900)


# ------------------------------------------------- 豆包车道（第二调度纪律）--
# ★ 为什么豆包不能套用 dola 的「整池爆发」（方案文档 §2.1，实测结论）：
#
#   两个渠道的 710022002 语义**完全相反**：
#     · dola  = **全局窗口**开关：关着时整池同刻被拒，开着时整池同刻放行
#               → 所以探针嗅窗 + 命中即整池爆发是正确的；
#     · 豆包  = **账号级限流**，绑 CK 不绑出口 IP（实测 4 个不同住宅 IP 全被拒；
#               同出口下匿名能过、真号被拒），且文档明确警告
#               「别靠反复重试去撞开它」。
#
#   ⇒ 把豆包号也丢进 _burst_round 会：没有共享窗口可命中（爆发毫无收益），
#     反而几十个号同刻打同一接口 → 大概率整批撞进 10 分钟冷却，一次打空号池。
#
#   所以豆包用**串行 + 按号冷却**：一次只发一条，失败就换下一个可调度号，
#     没有可调度号就睡到最近的冷却到期时间（不空转、不硬撞）。
#
# 复用而非重写：选号/冷却/额度/记账/失败分类全部用 doubao_pool 的既有实现
# （_select / _classify_failure / store.cooldown / store.charge），
# 本段只做「队列 ↔ 号池」的适配与台账。
DOUBAO_LANE_ENABLED = os.environ.get("I794_DOUBAO_LANE", "1").strip().lower() not in (
    "0", "false", "no", "")
# 一次轮询最多连发多少条（防止长任务把线程占死，也给其它任务留机会）
DOUBAO_LANE_BATCH = int(os.environ.get("I794_DOUBAO_LANE_BATCH", "1"))
# 没有可调度号时的轮询间隔（秒）；真正的等待由「最近冷却到期时间」决定
DOUBAO_LANE_IDLE = float(os.environ.get("I794_DOUBAO_LANE_IDLE", "15"))


def _doubao_pool():
    """惰性拿豆包号池（import 失败/无账号时返回 None，不影响 dola 主链路）。"""
    try:
        import doubao_pool as dp
        pool = dp.DoubaoPool()
        return pool if pool.accounts else None
    except Exception as exc:  # noqa: BLE001
        ledger(kind="doubao_lane_unavailable", err=repr(exc)[:200])
        return None


def _next_doubao_task():
    """从队列取一条 channel=doubao 的 pending **视频**任务（豆包车道串行）。"""
    if tasks_q is None:
        return None
    with tasks_q._lk:
        for t in tasks_q._tasks:
            if (t.status == "pending" and t.channel == "doubao"
                    and t.kind == "video"):
                t.status = "flying"
                return t
    return None


def _image_rejected_error():
    """惰性取「提示词级拒绝」异常类型（import 失败时退化成不可能命中的类型）。

    为什么惰性：run_pool 的 import 期不能硬依赖豆包模块（没装/没配号时也要能起）。
    """
    try:
        from doubao_image import DoubaoImageRejectedError
        return DoubaoImageRejectedError
    except Exception:  # noqa: BLE001
        class _Never(Exception):
            pass
        return _Never


def _next_doubao_image_task():
    """从队列取一条 channel=doubao 的 pending **图片**任务。

    ★ 与视频任务**分队列消费**：图片是「同步返回」的短任务（几秒~十几秒），
    视频是「受理 + 长轮询」的长任务（几分钟）。混在同一个串行车道里，
    一批图片会被一条长视频堵在后面；分成两条车道后**图片与视频并行**。
    """
    if tasks_q is None:
        return None
    with tasks_q._lk:
        for t in tasks_q._tasks:
            if (t.status == "pending" and t.channel == "doubao"
                    and t.kind == "image"):
                t.status = "flying"
                return t
    return None


def _cooldown_ready_at(pool) -> float:
    """最近的冷却/预热到期时间（没有可调度号时睡到这个点，不空转）。"""
    import time as _t
    now = _t.time()
    ready = []
    for name in pool.accounts:
        meta = pool.store.roll_day(pool.store.read_meta(name))
        cd = float(meta.get("cooldown_until") or 0)
        warm = pool._warmup_left(meta, now)
        if warm > 0:
            ready.append(now + warm)
        elif cd > now:
            ready.append(cd)
    return min(ready) if ready else now + DOUBAO_LANE_IDLE


def _doubao_classify(pool, exc: Exception) -> str:
    """失败分类（复用号池实现）：login/throttled/credit/paid/manual/timeout/other。"""
    try:
        return pool._classify_failure(exc)
    except Exception:  # noqa: BLE001
        return "other"


def doubao_dispatch_loop(stop: threading.Event):
    """豆包车道主循环：串行派发 → 按结果分类处理 → 无号则睡到冷却到期。

    ★ 与 dola 主循环的关键差异（不要"顺手统一"）：
      · 不参与窗口爆发（豆包没有全局窗口）；
      · 一次一条（DOUBAO_LANE_BATCH 可调，默认 1）；
      · manual（素材授权确认卡）→ **不冷却不换号重试**，把"去 App 点一次"的
        提示写进台账并让任务失败 —— 因为该卡是账号级一次性，人工点过才生效，
        反复换号/重试只会把好号推进冷却（§2.3）；
      · timeout（会话已建只是轮询超时）→ 直接判失败，**不换号重提**
        （重复出片 + 重复扣额度）；
      · throttled → 只短冷却（TRANSIENT_COOLDOWN_SEC），**不动 login_ok**；
      · login → 真登录失效，进风控组。
    """
    if not DOUBAO_LANE_ENABLED:
        pr("[doubao] 豆包车道已关闭（I794_DOUBAO_LANE=0）")
        return
    pool = _doubao_pool()
    if pool is None:
        pr("[doubao] 豆包号池为空或不可用，车道不启动（dola 车道不受影响）")
        return
    pr("[doubao] 豆包车道启动：串行派发 + 按号冷却（不参与窗口爆发）")

    import asyncio

    async def _run_one(task, name):
        """跑一条任务；返回 (ok, kind, detail)。"""
        assets = task.asset_paths
        try:
            res = await pool._run_one(
                name=name, prompt=task.prompt, ratio=task.ratio,
                duration=task.duration, model=task.model,
                image_paths=assets or None)
            return True, "ok", res
        except Exception as exc:  # noqa: BLE001
            return False, _doubao_classify(pool, exc), exc

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    idle_rounds = 0
    try:
        while not stop.is_set():
            task = _next_doubao_task()
            if task is None:
                idle_rounds += 1
                # 没有豆包任务：让出 CPU；有别渠道任务时也不该在这里空转
                stop.wait(1.0 if idle_rounds < 3 else DOUBAO_LANE_IDLE)
                continue
            idle_rounds = 0
            cands = pool._select()
            if not cands:
                # 没有可调度号：睡到最近的冷却/预热到期（不硬撞，避免整批进冷却）
                wait_for = max(1.0, min(_cooldown_ready_at(pool) - time.time(),
                                       3600))
                ledger(kind="doubao_idle", task=task.tid,
                       wait=round(wait_for, 1),
                       reason="no_schedulable_account")
                pr("[doubao] %s 暂无可调度号，%.0f 秒后重试（不换号硬撞）"
                   % (task.tid, wait_for))
                # 任务放回队列，等号恢复
                tasks_q.put_back(task, "DOUBAO_NO_ACCOUNT")
                stop.wait(wait_for)
                continue

            name = cands[0]
            task.account = name
            ledger(kind="doubao_dispatch", task=task.tid, account=name,
                   asset_count=len(task.asset_paths))
            pr("[doubao] %s → %s（串行，%d 个可调度号）"
               % (task.tid, name, len(cands)))

            async def _guarded(name=name, task=task):
                """在号锁内跑，避免同号并发（豆包对同号并发很敏感）。"""
                async with pool.lock_for(name):
                    return await _run_one(task, name)

            ok, kind, detail = loop.run_until_complete(_guarded())

            if ok:
                res = detail or {}
                try:
                    local = loop.run_until_complete(pool._download(
                        res.get("video_url") or "", name))
                except Exception as exc:  # noqa: BLE001
                    # 下载失败：号已经出片了，不能重提（重复扣额度）→ 任务失败
                    ledger(kind="doubao_download_fail", task=task.tid,
                           account=name, err=repr(exc)[:200])
                    pr("[doubao] %s 出片成功但下载失败（不重提，避免重复扣额度）: %r"
                       % (task.tid, exc))
                    task.status = "failed"
                    task.last_outcome = "DOUBAO_DOWNLOAD_FAIL"
                    ledger(kind="task_failed", task=task.tid, account=name,
                           reason="download_failed",
                           last_outcome="DOUBAO_DOWNLOAD_FAIL")
                    continue
                pool.store.charge(name, 1)
                pool.store.patch_meta(name, login_ok=True, verified_at=time.time(),
                                      last_err="")
                task.status, task.conv = "done", (res.get("conversation_id") or "")
                task.mp4 = local
                # ★ ftypisom 必须给：面板的 _analyze_events 只认带这个字段的
                #   harvest 事件才算"完成"。缺了它面板会一直显示 accepted，
                #   而成片其实早就落盘了（实测：状态 accepted=2/done=0，
                #   引擎内存快照却是 done=2，两处对不上）。
                ledger(kind="harvest", task=task.tid, account=name,
                       channel="doubao", status="succeeded",
                       local_path=local, bytes=os.path.getsize(local),
                       ftypisom=True, unwatermarked=True,
                       prompt=task.prompt[:400],
                       ratio=task.ratio, duration=task.duration,
                       model=task.model,
                       asset_names=list((task.meta or {}).get("asset_names") or []),
                       asset_count=len(task.asset_paths))
                pr("[doubao] %s 成片 → %s" % (task.tid, os.path.basename(local)))
                continue

            exc = detail
            msg = f"{type(exc).__name__}: {exc}"
            ledger(kind="doubao_submit", task=task.tid, account=name,
                   outcome=kind, err=msg[:200])
            # ---- 按分类处置（与 doubao_pool.generate_video 同口径）----
            if kind == "manual":
                # ★ 素材授权确认卡：账号级一次性，需人工去豆包 App 点一次。
                #   不冷却（号是好的）、不换号重试（每号都会弹卡，只会把池子拖垮）。
                pool.store.patch_meta(name, last_err="待人工素材授权: " + msg[:160],
                                      updated_at=time.time())
                task.status = "failed"
                task.last_outcome = "DOUBAO_NEED_MANUAL_CONFIRM"
                ledger(kind="task_failed", task=task.tid, account=name,
                       reason="need_manual_confirm",
                       last_outcome="DOUBAO_NEED_MANUAL_CONFIRM",
                       hint="请在豆包 App 内点一次「确认生成」完成素材授权后重试")
                pr("[doubao] %s 需要人工素材授权（账号级一次性）："
                   "去豆包 App 点一次「确认生成」后再重试，本线程不换号硬试"
                   % task.tid)
                continue
            if kind == "timeout":
                # 会话已建、只是轮询超时：上游可能仍在出片，重提=重复扣额度
                pool.store.patch_meta(name, last_err="轮询超时: " + msg[:160],
                                      updated_at=time.time())
                task.status = "failed"
                task.last_outcome = "DOUBAO_POLL_TIMEOUT"
                ledger(kind="task_failed", task=task.tid, account=name,
                       reason="poll_timeout",
                       last_outcome="DOUBAO_POLL_TIMEOUT",
                       hint="会话已建立只是等待超时，未换号重提（避免重复出片/扣额度）")
                pr("[doubao] %s 轮询超时（不换号重提）" % task.tid)
                continue
            if kind == "throttled":
                # 限流：短冷却、**不动 login_ok**（号是好的，只是被上游限流）
                pool.store.cooldown(name, pool.TRANSIENT_COOLDOWN_SEC,
                                    "上游限流: " + msg[:160], kind="throttled")
            elif kind == "login":
                pool.store.cooldown(name, pool.cooldown_seconds, msg, kind="login")
                pool.store.patch_meta(name, login_ok=False)
            elif kind == "credit":
                pool.store.patch_meta(name, used_today=pool.daily_limit,
                                      last_err=msg[:200], updated_at=time.time())
            elif kind == "paid":
                # 付费额度确认卡：换号可解 → 挪出调度后换号
                pool.store.cooldown(
                    name, pool.PAID_QUOTA_COOLDOWN_SEC,
                    "今日免费额度已用完，需客户端点「确认生成」同意付费；"
                    "已临时移出调度 %d 小时: %s"
                    % (pool.PAID_QUOTA_COOLDOWN_SEC // 3600, msg[:140]),
                    kind="paid")
            else:
                pool.store.patch_meta(name, last_err=msg[:200],
                                      updated_at=time.time())
            # 其余分类：换号重试（任务回队；attempts 由 put_back 统一累计）
            tasks_q.put_back(task, "DOUBAO_" + kind.upper())
            pr("[doubao] %s 失败（%s），换号重试" % (task.tid, kind))
    except Exception as exc:  # noqa: BLE001
        ledger(kind="doubao_lane_exc", err=repr(exc)[:250])
        pr("[doubao] 车道异常退出: %r" % exc)
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass
    pr("[doubao] 车道退出")


def doubao_image_dispatch_loop(stop: threading.Event):
    """豆包**图片**车道：串行出图，与视频车道**并行**。

    ★ 为什么单独起一条车道（而不是塞进视频车道）：
      图片是「同步返回」的短任务（一次 SSE 读完就有图），视频是
      「受理 + 长轮询」的长任务（几分钟到十几分钟）。混在一条串行车道里，
      一条视频会把后面几十张图全堵死 —— 用户提交图片后"一直不出图"。
      两条车道共享同一个号池（`_select` 自带冷却/额度过滤）与账号锁
      （`lock_for`），因此**并行也不会把同一个号打出并发**。

    失败纪律与视频车道同源（分类 → 冷却 / 换号），因为没有会话，
    所以没有 timeout / manual 这两个视频专有的终态分类。
    """
    if not DOUBAO_LANE_ENABLED:
        pr("[doubao-img] 图片车道已关闭（I794_DOUBAO_LANE=0）")
        return
    pool = _doubao_pool()
    if pool is None:
        pr("[doubao-img] 豆包号池为空或不可用，图片车道不启动")
        return
    pr("[doubao-img] 图片车道启动：串行出图 + 按号冷却（与视频车道并行）")

    import asyncio

    async def _run_one_image(task, name):
        return await pool._run_one_image(
            name, task.prompt, task.ratio, task.model,
            timeout=int(os.environ.get("DOUBAO_IMAGE_TIMEOUT", "300")))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    idle_rounds = 0
    try:
        while not stop.is_set():
            task = _next_doubao_image_task()
            if task is None:
                idle_rounds += 1
                stop.wait(1.0 if idle_rounds < 3 else DOUBAO_LANE_IDLE)
                continue
            idle_rounds = 0
            cands = pool._select()
            if not cands:
                wait_for = max(1.0, min(_cooldown_ready_at(pool) - time.time(), 3600))
                ledger(kind="doubao_image_idle", task=task.tid,
                       wait=round(wait_for, 1), reason="no_schedulable_account")
                pr("[doubao-img] %s 暂无可调度号，%.0f 秒后重试" % (task.tid, wait_for))
                tasks_q.put_back(task, "DOUBAO_IMAGE_NO_ACCOUNT")
                stop.wait(wait_for)
                continue

            name = cands[0]
            task.account = name
            ledger(kind="doubao_image_dispatch", task=task.tid, account=name,
                   model=task.model, ratio=task.ratio)
            pr("[doubao-img] %s → %s（%s %s）"
               % (task.tid, name, task.model, task.ratio))

            async def _guarded(name=name, task=task):
                """号锁内跑：同号不与视频/其它图片任务并发。"""
                async with pool.lock_for(name):
                    return await _run_one_image(task, name)

            try:
                res = loop.run_until_complete(_guarded())
            except _image_rejected_error() as exc:
                # ★ 提示词级拒绝（上游明确回复了拒绝理由）：换号也一样被拒，
                #   直接判失败，别把整池号白跑一遍。
                #   用**异常类型**判定，不靠消息里的关键词 —— 本类型的提示文案
                #   自带"风控/拒绝"字样，字符串匹配会恒真（把可重试的空响应
                #   也误判成不可重试）。
                msg = f"{type(exc).__name__}: {exc}"
                pool.store.patch_meta(name, last_err=msg[:200],
                                      updated_at=time.time())
                ledger(kind="doubao_image_submit", task=task.tid, account=name,
                       outcome="rejected", err=msg[:200])
                task.status = "failed"
                task.last_outcome = "DOUBAO_IMAGE_REJECTED"
                ledger(kind="task_failed", task=task.tid, account=name,
                       reason="content_rejected",
                       last_outcome="DOUBAO_IMAGE_REJECTED",
                       hint="提示词被上游内容风控拒绝，请调整描述后重试（换号无效）")
                pr("[doubao-img] %s 提示词被上游拒绝，不换号（调整描述后重试）"
                   % task.tid)
                continue
            except Exception as exc:  # noqa: BLE001
                kind = _doubao_classify(pool, exc)
                msg = f"{type(exc).__name__}: {exc}"
                ledger(kind="doubao_image_submit", task=task.tid, account=name,
                       outcome=kind, err=msg[:200])
                if kind == "throttled":
                    pool.store.cooldown(name, pool.TRANSIENT_COOLDOWN_SEC,
                                        "上游限流: " + msg[:160], kind="throttled")
                elif kind == "login":
                    pool.store.cooldown(name, pool.cooldown_seconds, msg, kind="login")
                    pool.store.patch_meta(name, login_ok=False)
                elif kind == "credit":
                    pool.store.patch_meta(name, used_today=pool.daily_limit,
                                          last_err=msg[:200], updated_at=time.time())
                elif kind == "paid":
                    pool.store.cooldown(
                        name, pool.PAID_QUOTA_COOLDOWN_SEC,
                        "今日免费额度已用完，需客户端点「确认生成」同意付费: "
                        + msg[:140], kind="paid")
                else:
                    pool.store.patch_meta(name, last_err=msg[:200],
                                          updated_at=time.time())
                tasks_q.put_back(task, "DOUBAO_IMAGE_" + kind.upper())
                pr("[doubao-img] %s 失败（%s），换号重试" % (task.tid, kind))
                continue

            urls = list(res.get("images") or [])
            if not urls:
                tasks_q.put_back(task, "DOUBAO_IMAGE_NO_URL")
                pr("[doubao-img] %s 未返回图片，换号重试" % task.tid)
                continue

            local_paths: list[str] = []
            for idx, url in enumerate(urls):
                try:
                    local_paths.append(loop.run_until_complete(
                        pool._download_image(url, name)))
                except Exception as exc:  # noqa: BLE001
                    ledger(kind="doubao_image_download_fail", task=task.tid,
                           account=name, idx=idx, err=repr(exc)[:200])
            if not local_paths:
                # 图已出但一张都没落地：判失败（重提会重复扣额度）
                task.status = "failed"
                task.last_outcome = "DOUBAO_IMAGE_DOWNLOAD_FAIL"
                ledger(kind="task_failed", task=task.tid, account=name,
                       reason="download_failed",
                       last_outcome="DOUBAO_IMAGE_DOWNLOAD_FAIL",
                       hint="图已生成但下载失败，未重提（避免重复扣额度）")
                continue

            # 产物改名成 <tid>_<序号>.<ext>，让面板/素材库能一对一认领
            finals: list[str] = []
            for idx, src in enumerate(local_paths):
                ext = os.path.splitext(src)[1] or ".png"
                dst = os.path.join(IMGDIR, "%s_%d%s" % (task.tid, idx, ext))
                try:
                    shutil.move(src, dst)
                except Exception as exc:  # noqa: BLE001
                    ledger(kind="doubao_image_move_fail", task=task.tid,
                           err=repr(exc)[:150])
                    dst = src
                finals.append(dst)

            pool.store.charge(name, 1)
            pool.store.patch_meta(name, login_ok=True, verified_at=time.time(),
                                  last_err="")
            task.status = "done"
            task.conv = str(res.get("conversation_id") or "")
            task.mp4 = finals[0]
            ledger(kind="harvest", task=task.tid, account=name,
                   channel="doubao", status="succeeded", kind_="image",
                   local_path=finals[0], local_paths=finals,
                   bytes=sum(os.path.getsize(p) for p in finals if os.path.exists(p)),
                   unwatermarked=True, image_count=len(finals),
                   prompt=task.prompt[:400], ratio=task.ratio,
                   model=task.model, duration=0,
                   asset_names=list((task.meta or {}).get("asset_names") or []),
                   asset_count=len(task.asset_paths))
            pr("[doubao-img] %s 出图 %d 张 → %s"
               % (task.tid, len(finals), os.path.basename(finals[0])))
    except Exception as exc:  # noqa: BLE001
        ledger(kind="doubao_image_lane_exc", err=repr(exc)[:250])
        pr("[doubao-img] 图片车道异常退出: %r" % exc)
    finally:
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass
    pr("[doubao-img] 图片车道退出")


# ---------------------------------------------------------------- 收割 -----
def harvest_one(label, state_path, conv, task, SUB):
    """长轮询收割一个已受理 conv。成功→无水印 MP4 + 任务done；失败/超时→任务回队。
    同号多个 conv 并发收割时，最后一个收完才删号（引用计数）。

    ★ 无水印选择全部交给 protocol 层：
      get_video_result(remove_watermark=...) 内部走 choose_video_download_url()，
      优先级 = Dola 自带 lr=unwatermarked 直链 → 第三方去水印 API → 原始直链兜底，
      并把结果写进 download_url / download_source / direct_unwatermarked。
      自己从 play_info_urls[0] 取会绕过这套逻辑、拿到带水印的 lr=cici_ai 链接。
    """
    rec = {"account": label, "conv": conv, "task": task.tid if task else "(bonus)",
           # 提示词随收割记录一起落盘：面板据此把成片一对一映射回提示词，
           # 不依赖 tasks.txt 的行号（队列清空/覆盖后行号与 tid 会脱钩）。
           "prompt": (task.prompt[:400] if task else ""),
           # 创作台 feed 按这几项展示卡片元信息与「详细信息」
           "channel": (task.channel if task else DEFAULT_CHANNEL),
           "ratio": (task.ratio if task else RATIO),
           "duration": (task.duration if task else DURATION),
           "model": (task.model if task else MODEL),
           "asset_names": list((task.meta or {}).get("asset_names") or []) if task else [],
           "asset_count": len(task.asset_paths) if task else 0}
    try:
        dola = SUB._proto()
        sess, ctx, hdrs = dola.setup_session(
            region=SUB.region, pc_version=SUB.pc_version, proxy=SUB.proxy or "",
            login_pure=True, login_state_file=state_path)
        try:
            res = dola.get_video_result(sess, ctx, conv, timeout=HARVEST_TIMEOUT,
                                        interval=HARVEST_INTERVAL,
                                        remove_watermark=REMOVE_WATERMARK,
                                        proxy=SUB.proxy or "")
        finally:
            try:
                sess.close()
            except Exception:
                pass

        status = res.get("status")
        # ---- 选片：优先用协议层选好的无水印 URL ----
        chosen = str(res.get("download_url") or "")
        src_kind = str(res.get("download_source") or "")
        direct_unmarked = bool(res.get("direct_unwatermarked"))
        api_used = bool(res.get("nowatermark_api_used"))
        if not chosen:
            raw = list(res.get("play_info_urls") or res.get("urls") or [])
            chosen = raw[0] if raw else ""
            src_kind = src_kind or "fallback_first_url"
        # URL 里是否自带无水印标记（lr/logo_type=unwatermarked）
        url_unmarked = is_unwatermarked_url(chosen)
        # 最终判定：三方解析服务返回的就是去水印成片（它的链接不一定带 unwatermarked
        # 字样，不能只看 URL）；Dola 自带 unwatermarked 直链也算；其余视为带水印。
        unmarked = bool(api_used or direct_unmarked or url_unmarked)

        rec.update({"status": status, "download_source": src_kind,
                    "unwatermarked": unmarked, "url_unmarked": url_unmarked,
                    "direct_unwatermarked": direct_unmarked,
                    "nowatermark_api_used": api_used,
                    "nowatermark_api_error": str(res.get("nowatermark_api_error") or "")[:160],
                    "n_urls": len(list(res.get("play_info_urls") or res.get("urls") or [])),
                    "failure_reasons": (res.get("failure_reasons") or [])[:2]})

        if status == "succeeded" and chosen:
            if not unmarked and WATERMARK_FALLBACK == "retry":
                # 只在去水印确实失败时走到这里；按配置换号重出。
                rec["note"] = "watermarked_rejected"
                if task:
                    tasks_q.put_back(task, "HARVEST_WATERMARKED")
                pr("[harvest] %s conv=%s 未拿到无水印源（source=%s），按配置换号重试"
                   % (label, conv[:8], src_kind))
            elif not unmarked and WATERMARK_FALLBACK == "fail":
                rec["note"] = "watermarked_rejected"
                if task:
                    task.status = "failed"
                pr("[harvest] %s conv=%s 未拿到无水印源，按配置判失败且不保存"
                   % (label, conv[:8]))
            else:
                # save（默认）：仍保存，但文件名加 _WM、台账标 false，结果一眼可辨。
                name = ("%s_%s" % (task.tid, conv[:8])) if task else ("probe_%s" % conv[:8])
                if not unmarked:
                    name += "_WM"
                dst = os.path.join(MP4DIR, name + ".mp4")
                dola.download_video(chosen, dst, referer="https://www.dola.com/", proxy="")
                ok = ftypisom_ok(dst)
                rec.update({"local_path": dst, "bytes": os.path.getsize(dst), "ftypisom": ok})
                if task and ok:
                    task.status = "done" if unmarked else "done_watermarked"
                    task.mp4 = dst
                    if not unmarked:
                        pr("[harvest] %s %s 已保存为 %s —— **注意：不是无水印源**"
                           "（source=%s, nowatermark_err=%s）"
                           % (label, task.tid, os.path.basename(dst), src_kind,
                              rec.get("nowatermark_api_error") or "-"))
                elif task:
                    note_tried(label, task.tid)      # 该号这次没出片：记住，下窗换号
                    tasks_q.put_back(task, "HARVEST_DOWNLOAD_BAD")   # 下载坏了：换号重试
        else:
            if task:
                note_tried(label, task.tid)          # 该号这次没出片：记住，下窗换号
                tasks_q.put_back(task, "HARVEST_NO_VIDEO")       # 收割无片：换号重试
            rec["note"] = "no_video_in_window"
        # 收尾说明：MAX_PER_ACC>1 时同一账号可能有多个 conv 在等收割，
        # 账号在「最后一个 conv 的收割调用」结束后统一删除（trash_account 幂等）。
    except Exception as exc:
        rec["exc"] = repr(exc)[:250]
        if task:
            note_tried(label, task.tid)
            tasks_q.put_back(task, "HARVEST_EXC")
    finally:
        if harv_ref_dec(label):                 # 该号最后一个 conv 收完才删
            trash_account(label, "harvest_finished")
        harv_dec()
    ledger(kind="harvest", **rec)
    _bits = []
    if rec.get("unwatermarked"):
        _bits.append("无水印✓")
    elif "unwatermarked" in rec:
        _bits.append("带水印✗")
    pr("[harvest] " + json.dumps({k: rec.get(k) for k in
                                  ("task", "account", "status", "download_source",
                                   "local_path", "ftypisom",
                                   "failure_reasons", "exc") if k in rec}, ensure_ascii=False)
       + (" " + " ".join(_bits) if _bits else ""))


# ---------------------------------------------------------------- 探针 -----
def _retire_probe(label, reason):
    with probe_lock:
        pr_ = probes.get(label)
        if pr_:
            pr_["used"] = True
    trash_account(label, reason)


def probe_loop(stop: threading.Event, window_open: threading.Event, SUB):
    """单嗅探线程 + 探针号轮换。命中窗口 → set 事件通知协调器整池爆发。"""
    consec_exc = 0
    waited = 0
    probe_round = 0
    while not stop.is_set():
        p = None
        with probe_lock:
            for label, pr_ in probes.items():
                if not pr_["used"]:
                    p = (label, pr_["state"])
                    break
        if not p:
            # 探针池空（还没注册好 / 全退役）：明确报出来，绝不允许静默空转。
            if waited % 6 == 0:
                pr("[probe] 无可用探针号（待补号线程注册 %d 个备用），%d 秒后重试"
                   % (PROBE_STANDBY, waited))
            waited += 5
            stop.wait(5)
            continue
        waited = 0
        label, state_path = p
        probe_round += 1
        try:
            SUB.prepare(state_path)             # 预建常驻 session（幂等）
        except Exception:
            pass
        task = tasks_q.take_next(channel="dola", kind="video")   # 探针命中即生产：借用下一条真实 prompt
        prompt = task.prompt if task else PROBE_FALLBACK_PROMPT
        # ★ 参数按任务取（创作台可给每条任务单独指定比例/时长/模型/参考素材）；
        #   无 meta 的老队列走 Task 属性里的全局默认，行为与从前完全一致。
        _p_ratio = task.ratio if task else RATIO
        _p_dur = task.duration if task else DURATION
        _p_model = task.model if task else MODEL
        _p_assets = task.asset_paths if task else []
        try:
            out = SUB.submit(cookie_state_path=state_path, prompt=prompt,
                             ratio=_p_ratio, duration=_p_dur, model=_p_model,
                             timeout_sec=30, asset_paths=_p_assets)
        except Exception as exc:
            out = W.SubmitOutcome(outcome=W.OUT_EXC, exc=repr(exc)[:160])
        oc = out.__dict__.get("outcome")
        conv = out.__dict__.get("conv")
        if oc == W.OUT_RECEIVED and conv:
            consec_exc = 0
            window_open.set()
            ledger(kind="window_open", probe=label, conv=conv, task=(task.tid if task else ""))
            pr("[window] %s 闸门开启（探针=%s%s）→ 通知整池爆发"
               % (time.strftime("%H:%M:%S"), label,
                  ("，探针即任务 " + task.tid) if task else ""))
            if task:
                task.status, task.account, task.conv = "accepted", label, conv
                task.accepted_at = time.time()
                ledger(kind="accepted", task=task.tid, account=label, via="probe", conv=conv)
            harv_ref_inc(label)
            harv_inc()
            HARV.submit(harvest_one, label, state_path, conv, task, SUB)
            with probe_lock:
                if label in probes:
                    probes[label]["used"] = True     # 该号配额已用于探针提交，轮换
            # ★ 等「爆发真正做完」再继续嗅探。不能 wait(window_open)——那个事件刚被
            #   自己 set，wait 会立即返回，等于没有防抖，探针会在爆发中途继续发下一发
            #   抢窗口（文档 fact 629 明确警告的反模式）。
            _burst_idle.wait(timeout=BURST_DEBOUNCE_WAIT)
        elif oc == W.OUT_WINDOW_CLOSED:
            consec_exc = 0
            if task:
                tasks_q.put_back(task, oc)      # 窗关=常态，不计重试次数
            # 节流报窗（每 5 轮一次）：否则每 6 秒一行 710 会把控制台刷满，
            # 反而淹掉「窗口开启」这种关键事件。
            if probe_round % 5 == 1:
                st = tasks_q.stats()
                pr("[probe] %s 窗关(710022002) 探测#%d | 待提交 %d 条 | 就绪号 %d"
                   % (time.strftime("%H:%M:%S"), probe_round, st.get("pending", 0),
                      _pool_ready_count()))
        elif oc in (W.OUT_QUOTA_EXHAUSTED, W.OUT_LOGIN_INVALID):
            if task:
                tasks_q.put_back(task, oc)      # 换号即可，任务本身没问题
            _retire_probe(label, oc)                 # ★ 配额烧完/失效 → 立即轮换
            pr("[probe] %s 退役（%s），切换下一个探针号" % (label, oc))
        else:                                        # EXC / OTHER
            if task:
                tasks_q.put_back(task, oc)      # 真异常才计重试次数
            consec_exc += 1
            if consec_exc >= 3:
                consec_exc = 0
                _retire_probe(label, "probe_exc_streak")
                pr("[probe] %s 连续异常退役，切换下一个探针号" % label)
        stop.wait(SNIFF_PERIOD + random.uniform(0, SNIFF_JITTER))


# ---------------------------------------------------------------- 爆发 -----
_burst_active = threading.Event()      # 有爆发在跑时，安全网不得插手
_burst_idle = threading.Event()        # 爆发结束信号（初始为「空闲」）
_burst_idle.set()


def coordinator_loop(stop: threading.Event, window_open: threading.Event, SUB):
    """窗口命中 → 从队列取任务，每号最多 MAX_PER_ACC 单，整池同刻爆发提交。"""
    while not stop.is_set():
        if not window_open.wait(timeout=5):
            continue
        window_open.clear()
        _burst_active.set()
        _burst_idle.clear()
        try:
            _burst_round(stop, SUB)
        except Exception:
            ledger(kind="coordinator_exc", err=traceback.format_exc()[-400:])
        finally:
            _burst_active.clear()
            _burst_idle.set()
        time.sleep(1.0)


def _burst_round(stop: threading.Event, SUB):
    """一轮整池爆发：取任务 → 同刻并发提交 → 结果分类 → 剩余号放回。"""
    with pool_lock:
        avail = [(a.label, a.state_path, min(a.quota_left, MAX_PER_ACC))
                 for a in pool.values() if a.status == "available"]
    jobs = []
    for lab, sp, n in avail:
        # 换号记忆：把「这个号试过且没成功」的任务排到后面，
        # 让本窗优先接它没碰过的任务（用户要的「失败就换账号」）。
        with pool_lock:
            a = pool.get(lab)
            avoid = set(a.tried_tasks) if a is not None else set()
        my = []
        for _ in range(max(0, n)):
            t = tasks_q.take_next(avoid=avoid, channel="dola", kind="video")
            if not t:
                break
            my.append(t)
        if my:
            jobs.append((lab, sp, my))     # 号内任务串行，号间并行
    if not jobs:
        return
    n_total = sum(len(j[2]) for j in jobs)
    # 记账：预扣配额，防同窗重复派发
    with pool_lock:
        for lab, _sp, my in jobs:
            if lab in pool:
                pool[lab].quota_left = max(0, pool[lab].quota_left - len(my))
                if pool[lab].quota_left <= 0:
                    pool[lab].status = "draining"   # 额度用完：不再派发，等收割后删
    pr("[burst] %s 整池爆发：%d 号 / %d 条（每号最多 %d）"
       % (time.strftime("%H:%M:%S"), len(jobs), n_total, MAX_PER_ACC))
    t_w = time.time()

    def fire_one(lab, sp, t):
        """号内单发：同一 Session 绝不并发使用（requests.Session 非线程安全）。

        返回 (ok, quota_dead, oc)：
          ok=False        → 本号本窗停发后续单（窗关 / 配额烧完 / 失效）
          quota_dead=True → 该号已不可再用，调用方负责删号补位
          oc              → 本次提交结果（供剩余任务回队时沿用）
        """
        try:
            out = SUB.submit(cookie_state_path=sp, prompt=t.prompt,
                             ratio=t.ratio, duration=t.duration, model=t.model,
                             timeout_sec=60, asset_paths=t.asset_paths)
        except Exception as exc:
            out = W.SubmitOutcome(outcome=W.OUT_EXC, exc=repr(exc)[:160], account=lab)
        d = out.__dict__
        oc, conv = d.get("outcome"), d.get("conv")
        rec = {"task": t.tid, "account": lab, "outcome": oc,
               "errcode": d.get("error_code"), "conv": conv,
               "since_window": round(time.time() - t_w, 2),
               # 把提示词一起落台账：成片名是「<tid>_<conv8>.mp4」，而 tid 的编号
               # 会随队列清空/覆盖而重排（t001 → t105 …）。面板若按 tasks.txt 的
               # **行号**去反查提示词，队列一动就会张冠李戴。这里写进台账，
               # 面板直接按文件名命中这条记录取提示词，才是权威映射。
               "prompt": t.prompt[:400],
               # 创作台要按渠道/参数/素材回溯这条成片，这里一并落台账
               "channel": t.channel, "ratio": t.ratio, "duration": t.duration,
               "model": t.model,
               "asset_names": list((t.meta or {}).get("asset_names") or []),
               "asset_count": len(t.asset_paths)}
        if oc == W.OUT_RECEIVED and conv:
            t.status, t.account, t.conv = "accepted", lab, conv
            t.accepted_at = time.time()
            ledger(kind="accepted", **rec)
            harv_ref_inc(lab)
            harv_inc()
            HARV.submit(harvest_one, lab, sp, conv, t, SUB)
            return True, False, oc
        if oc in (W.OUT_QUOTA_EXHAUSTED, W.OUT_LOGIN_INVALID, W.OUT_COUNTRY):
            note_tried(lab, t.tid)          # 该号已废：记住它接不了这个任务
            tasks_q.put_back(t, oc)         # 环境类：不计重试次数
            ledger(kind="submit", **rec)
            return False, True, oc          # 该号/出口已不可用 → 删号补位
        # 窗关/异常：任务回队，并把本单【预扣的配额退回】。
        # 不退回的话：MAX_PER_ACC=2 时两单都失败 → quota_left=0 → 号被标 draining，
        # 既不再派发、又因为没有受理所以永远不会被收割回调删除 —— 号池永久泄漏。
        refund_quota(lab, 1)
        note_tried(lab, t.tid)              # 换号记忆：下次优先换别的号接它
        tasks_q.put_back(t, oc)
        ledger(kind="submit_retry", **rec)
        return (not is_env_outcome(oc)), False, oc

    def fire_account(lab, sp, my):
        dead = False
        for idx, t in enumerate(my):
            if stop.is_set():
                tasks_q.put_back(t, "STOPPED")
                refund_quota(lab, 1)        # 没发出去 → 配额退回
                continue
            try:
                ok, quota_dead, oc = fire_one(lab, sp, t)
            except Exception as exc:            # 兜底：任务绝不能卡在 flying
                ledger(kind="burst_exc", task=t.tid, account=lab, err=repr(exc)[:200])
                tasks_q.put_back(t, "BURST_EXC")
                refund_quota(lab, 1)
                ok, quota_dead, oc = False, False, "BURST_EXC"
            if quota_dead:
                dead = True
            if not ok:
                # 本号本窗不再发：剩余任务立刻回队，并退回它们的预扣配额
                rest = my[idx + 1:]
                for t2 in rest:
                    tasks_q.put_back(t2, oc)   # 沿用同一原因（环境类不计重试）
                refund_quota(lab, len(rest))
                break
        if dead:
            try:
                trash_account(lab, "quota_or_login_dead")
            except Exception:
                pass

    with ThreadPoolExecutor(max_workers=min(BURST_CONC, len(jobs))) as tp:
        list(tp.map(lambda j: fire_account(*j), jobs))
    ledger(kind="burst_done", n=n_total, accounts=len(jobs),
           wall=round(time.time() - t_w, 2))
    with pool_lock:                          # 本窗结束：把仍有额度的号放回可派发
        for a in pool.values():
            if a.status == "draining" and a.quota_left > 0:
                a.status = "available"


# ---------------------------------------------------------------- 主流程 ---
def _queued_asset_paths():
    """队列里所有任务引用到的参考素材（去重、保序）。

    用于启动时**一次性预上传**：素材上传走账号自己的 session、单张数秒，
    放到爆发窗口里做会把约 12s 的窗口吃光（见 dola_window.prefetch_assets）。
    """
    out: list[str] = []
    seen = set()
    try:
        if tasks_q is None:
            return out
        for t in tasks_q._tasks:
            for p in t.asset_paths:
                if p not in seen:
                    seen.add(p)
                    out.append(p)
    except Exception:  # noqa: BLE001
        return out
    return out


def _warn_if_panel_running(port=8002):
    """检测 Web 面板(server.py)是否在跑。

    面板默认也会启动一个窗口嗅探器（DOLA_WINDOW_SNIFF=1），与本引擎的嗅探循环
    互抢窗口相位、把出口打热、并争抢 acc/ 号目录 —— 文档 fact 629 明确警告的
    反模式。这里只警告，不擅自杀掉别人的进程。
    """
    import socket
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect(("127.0.0.1", int(port)))
        alive = True
    except Exception:
        alive = False
    finally:
        try:
            s.close()
        except Exception:
            pass
    if alive:
        pr("")
        pr("!" * 68)
        pr("!! 警告：检测到 Web 面板仍在 127.0.0.1:%d 运行。" % port)
        pr("!! 面板默认也会开一个窗口嗅探器，会与本引擎互抢窗口相位、")
        pr("!! 把出口打热（文档 fact 629 的反模式），并争抢 acc/ 号目录。")
        pr("!! 强烈建议：先关掉面板（关闭dola池.bat），只保留本引擎。")
        pr("!" * 68)
        pr("")
        ledger(kind="warn_panel_running", port=int(port))


def main():
    global tasks_q, HARV, harv_inflight, BURST_DEBOUNCE_WAIT
    BURST_DEBOUNCE_WAIT = float(os.environ.get("I794_BURST_DEBOUNCE", "14"))

    try:
        with open(os.path.join(OUT, "pool_counters.json"), "r", encoding="utf-8") as fh:
            _ctr.update(json.load(fh))
    except Exception:
        pass

    tasks = load_tasks()
    if not tasks:
        pr("!! %s 没有任务（一行一条提示词，# 为注释）" % TASKS_FILE)
        return
    tasks_q = TaskQueue(tasks)
    HARV = ThreadPoolExecutor(max_workers=HARVEST_WORKERS, thread_name_prefix="harvest")

    pr("=== run_pool 启动 === tasks=%d pool_target=%d max_per_acc=%d sniff=%.0fs "
       "probe_standby=%d model=%s %ss %s" % (len(tasks), POOL_TARGET, MAX_PER_ACC,
                                             SNIFF_PERIOD, PROBE_STANDBY, MODEL,
                                             DURATION, RATIO))
    pr("     无水印: 解析服务=%s | 拿不到时=%s"
       % ("开" if REMOVE_WATERMARK else "关", WATERMARK_FALLBACK))
    SUB = W.DolaProtocolSubmitter(report_path=os.path.join(OUT, "submit.jsonl"))
    _warn_if_panel_running()
    adopt_existing()
    with pool_lock:
        avail_paths = [a.state_path for a in pool.values() if a.status == "available"]
    if avail_paths:
        t0 = time.time()
        # ★ 参考素材在此**预上传**（每个号各传一遍，缓存在 submitter 里）。
        #   窗口里只发最后一个 POST，否则上传会吃掉整个窗口。
        start_assets = _queued_asset_paths()
        if start_assets:
            pr("[assets] 队列引用 %d 个参考素材，开始预上传到 %d 个号（窗口内不再上传）"
               % (len(start_assets), len(avail_paths)))
        ok = SUB.prepare_many(avail_paths, workers=min(16, max(4, len(avail_paths))),
                              assets=start_assets or None)
        pr("resident sessions ready: %d/%d in %.1fs"
           % (sum(1 for v in ok.values() if v), len(ok), time.time() - t0))

    stop = threading.Event()
    window_open = threading.Event()
    threads = [
        threading.Thread(target=probe_loop, args=(stop, window_open, SUB),
                         name="probe", daemon=True),
        threading.Thread(target=coordinator_loop, args=(stop, window_open, SUB),
                         name="coordinator", daemon=True),
        threading.Thread(target=replenisher_loop, args=(stop, SUB),
                         name="replenish", daemon=True),
        threading.Thread(target=liveness_loop, args=(stop,), name="liveness", daemon=True),
        # 队列热加载：面板追加提示词后接进队列，不必重启（纯附加，不碰出片逻辑）
        threading.Thread(target=queue_watch_loop, args=(stop,), name="queuewatch",
                         daemon=True),
    ]
    # ★ 豆包车道：**常驻待命**，不按"启动时有没有豆包任务"决定是否起线程。
    #   创作台随时可能提交一条豆包任务（面板热加载接单），若线程没起，
    #   那条任务会一直躺在队列里没人管 —— 用户看不到任何报错，只是"不出片"。
    #   线程内部第一件事就是检查豆包号池是否可用，不可用就自己退出（不占资源）。
    threads.append(threading.Thread(target=doubao_dispatch_loop,
                                    args=(stop,), name="doubao", daemon=True))
    # ★ 豆包**图片**车道：与视频车道并行（图片是短任务，别被长视频堵住）。
    #   同样常驻待命，线程内部自己检查号池可用性。
    threads.append(threading.Thread(target=doubao_image_dispatch_loop,
                                    args=(stop,), name="doubao-img", daemon=True))
    _n_db = sum(1 for t in tasks if t.channel == "doubao")
    _n_db_img = sum(1 for t in tasks
                    if t.channel == "doubao" and t.kind == "image")
    if _n_db:
        pr("     豆包车道: 启用（队列含 %d 条豆包任务，其中图片 %d 条）"
           % (_n_db, _n_db_img))
    else:
        pr("     豆包车道: 待命（创作台提交豆包任务后自动接单）")
    pr("     豆包图片车道: %s" % ("启用" if _n_db_img else "待命（与视频车道并行）"))
    for th in threads:
        th.start()

    t0 = time.time()
    last_sweep = time.time()
    try:
        while True:
            if os.path.exists(os.path.join(HERE, "STOP")):
                pr("[STOP] 检测到 STOP 文件，准备收尾")
                break
            if RUN_SECONDS and time.time() - t0 > RUN_SECONDS:
                pr("[STOP] 达到 RUN_SECONDS=%d" % RUN_SECONDS)
                break
            st = tasks_q.stats()
            with harv_lock:
                inflight = harv_inflight
            # 安全网：每 5 分钟把「卡在 flying 且当前没有爆发在跑」的任务放回队列，
            # 防止个别未预期异常让引擎永远等不到终态。
            if time.time() - last_sweep > 300 and not _burst_active.is_set():
                last_sweep = time.time()
                swept = tasks_q.sweep_stuck()
                if swept:
                    pr("[sweep] 放回 %d 个卡住的提交任务" % swept)
            if st["pending"] == 0 and st["flying"] == 0 and inflight == 0:
                pr("[done] 全部任务到达终态：%s" % st)
                break
            time.sleep(5)
    finally:
        stop.set()
        time.sleep(2)
        st = tasks_q.stats()
        summary = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tasks": st, "model": MODEL, "pool_target": POOL_TARGET,
            "max_per_acc": MAX_PER_ACC, "sniff_period": SNIFF_PERIOD,
            "accounts_active": len(pool), "probes_standby": probe_standby_count(),
            "tasks_detail": tasks_q.snapshot(),
        }
        with open(os.path.join(OUT, "summary_pool.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
        ledger(kind="summary", **{k: v for k, v in summary.items() if k != "tasks_detail"})
        pr("=== SUMMARY ===\n" + json.dumps(summary, ensure_ascii=False, indent=1)[:3000])


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with open(os.path.join(OUT, "run_pool.err.log"), "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + traceback.format_exc())
        traceback.print_exc()
