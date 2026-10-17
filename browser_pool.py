"""浏览器版号池：扫描 accounts/ profile，每号每天 2 片硬限额，同号互斥。

v2：accounts_meta 元数据表（调度开关/备注/登录态缓存/风控冷却），面板读写同一份数据。
配额计数落 SQLite（pool_usage.db），保守计数：出片成功或额度报错才记 1 次。
"""
import asyncio
import logging
import shutil
import sqlite3
try:
    import _safe_sqlite  # thread-safe sqlite3 wrapper (fixes SQLITE_MISUSE)
except Exception:  # pragma: no cover
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    import _safe_sqlite

import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from dola_client import CreditError
from video_worker_ui import (
    AccountLimitedError, CreditInsufficientError, LoginExpiredError, RiskControlError,
    generate_video, resume_video,
)
from pure_api_gen import (
    AbnormalNoAckError, ShortClipError, WindowClosedError, ensure_web_playable,
    generate_pure_video, hello_probe, probe_login, probe_login_detail,
)
from browser30 import generate_video_30
import config
import cred_store
import dola_window
import failure_text
import proxy_store
from pool import AccountConfig as _PoolAccountConfig, AccountPool as _PoolEngine

logger = logging.getLogger("dola-pool.browser_pool")

DAILY_LIMIT = config.DAILY_LIMIT
COOLDOWN_SEC = 1800  # 风控冷却 30 分钟
FAIL_COOLDOWN_SEC = 120   # 任一任务试号失败后的共享冷却：并发任务在此窗口内不再选同一账号
WAIT_FREE_ACCOUNT_SEC = 15  # 候选账号全被其他任务占用时，最长等待重扫时间
GROUP_WAIT_STEP = 5       # 目标额度组暂时无号时的重扫步长（秒）
# _sync_engine 可能在事件循环和线程池里并发被调（generate_video 走 to_thread），
# 重建是「整体替换 self._engine」，用锁串行化防止互相覆盖。
_SYNC_ENGINE_LOCK = threading.Lock()
FAIL_STREAK_TO_ABNORMAL = 3   # 同号连续失败几次后进【异常】组（到期自动恢复）

# ===== 账号分组（2026-09-23 大迭代）=====
# 满额 = 剩余 == 每日上限（能出 1 条 2.0 / 2 条 2.5）；
# 半额 = 2 ≤ 剩余 < 上限（只够 1 条 2.5）；
# 冷却 = 剩余 ≤ 1（0 或 1 点一律如此，1 点是 2.0 用剩的碎片）→ 不参与派发，额度日 23:00 重置后自动回满额。
GROUP_FULL = "满额"
GROUP_HALF = "半额"
GROUP_COOLING = "冷却"
GROUP_BUSY = "生成中"
GROUP_RISK = "风控"
GROUP_PENDING = "待激活"
GROUP_ABNORMAL = "异常"
# 【有效】= 【正常】：能参与派发的四个组（含正在出片与当日冷却）
ACTIVE_GROUPS = (GROUP_FULL, GROUP_HALF, GROUP_COOLING, GROUP_BUSY)
GROUP_ORDER = (GROUP_FULL, GROUP_HALF, GROUP_COOLING, GROUP_BUSY,
               GROUP_RISK, GROUP_PENDING, GROUP_ABNORMAL)


def group_of(a: dict, now: float, *, busy: bool = False,
             processing_accounts: set | None = None) -> tuple[str, str]:
    """账号所属分组 + 原因。优先级：风控 > 待激活 > 异常 > 生成中 > 冷却 > 半额 > 满额。

    纯函数，便于单测；`busy` 是内存锁，`processing_accounts` 是任务库里正在跑该号的集合
    （服务重启后内存锁会丢，靠任务库兜底）。
    """
    # 风控 = 你好探测不通过（被登出/没回复）或已被标记风控；永久停留，人工恢复才出组。
    # login_ok == 0（验证过的账号掉登录）也按「被登出」处理，进风控而不是悄悄失效。
    if a.get("risk_control") or a.get("login_ok") == 0:
        return GROUP_RISK, str(a.get("risk_reason") or "登录态失效")
    if a.get("login_ok") is None:
        return GROUP_PENDING, "尚未完成首次激活探测"
    if float(a.get("abnormal_until") or 0) > now:
        return GROUP_ABNORMAL, str(a.get("abnormal_reason") or "生成过程异常")
    if busy or (processing_accounts and a.get("name") in processing_accounts):
        return GROUP_BUSY, "正在生成"
    remaining = int(a.get("remaining") or 0)
    # 上限以该号自己的上限为准（面板 dict 里带 limit），拿不到才退回全局配置
    limit = int(a.get("limit") or config.DAILY_LIMIT or DAILY_LIMIT)
    # 上游自己报「今日次数用完 / 积分不足」的号同样不能出片，一并算进冷却组
    # （不新增「限流」概念，只在原因里写清楚）
    if a.get("rate_limited"):
        return GROUP_COOLING, str(a.get("limit_reason") or "上游报今日次数已用完")
    if a.get("quota_blocked"):
        # quota_reason 有时只是来源标记（upstream/local），不是给人看的解释
        reason = str(a.get("quota_reason") or "")
        if reason and reason not in ("upstream", "local"):
            return GROUP_COOLING, reason
        balance = a.get("credit_balance")
        detail = f"（今日剩余 {balance}）" if balance is not None else ""
        return GROUP_COOLING, f"上游报额度不足{detail}"
    if remaining <= 1:
        return GROUP_COOLING, f"剩余 {remaining} 点（额度日 23:00 重置）"
    if remaining < limit:
        return GROUP_HALF, f"剩余 {remaining} 点，只够 seedance-2.5"
    return GROUP_FULL, f"剩余 {remaining} 点"
WAIT_FREE_ACCOUNT_STEP = 2  # 等待重扫间隔（秒）

# 上游「访问频繁」类瞬时限流：只冷却一小段时间，不能按「失败」锁到次日重置。
TRANSIENT_COOLDOWN_SEC = config.TRANSIENT_COOLDOWN_SEC
TRANSIENT_THROTTLE_MARKERS = (
    "访问频繁", "710022002", "稍后重试", "服务繁忙", "系统繁忙", "请求过于频繁",
    "too many requests", "rate limit exceeded", "temporarily unavailable",
    "please try again later", "try again later",
)
# 上游明确的「当日次数用完」：等额度刷新（本地也按每日上限处理）。
DAILY_LIMIT_MARKERS = (
    "今天的生成次数已经达到上限", "今日次数已达上限", "达到每日上限",
    "每日上限", "每日次数", "daily limit",
    # 2026-10-06 实测新话术：「今日视频生成免费数量用完了，先使用快速模式和我聊聊别的吧。」
    # 不认这句 → 账号不会被标「每日上限」计入冷却，任务白等到超时。
    "今日视频生成免费数量用完", "视频生成免费数量用完", "免费数量用完",
    "今日视频生成免费次数用完", "免费生成次数用完",
)
# 账号还有额度、只是不够这一单（例如 10 秒要 4 点而当日只剩 2 点）。
# 上游原话：「本次视频生成需要消耗 N 个视频生成额度，今日剩余 M 个视频生成额度 ，无法生成该视频。」
# 注意**不能**用「剩余 0 个」/「剩余 2 个」当标记：链路播报里正常也会出现这句，
# 会把版权/内容拒稿误判成额度不足，把好号白锁到次日。
QUOTA_SHORT_MARKERS = (
    "额度不足", "积分不足", "无法生成该视频", "需要消耗",
)
# 内容/版权拒稿：与账号无关，换号也过不了，不能把号标记成失败或额度不足。
CONTENT_REFUSAL_MARKERS = (
    "版权限制", "涉及版权", "请更换输入内容", "内容违规", "违反社区",
    # 审核类拒稿的其余上游话术（2026-09-23 补：以前「肖像保护」没进这个表，
    # 被判成 other 后会把好号标成失败，且拒稿原文没人记录）。
    "肖像保护", "未认证人脸", "人脸暂不支持", "内容审核", "审核不通过",
    "不合规", "涉嫌侵权", "未授权使用",
    "copyright", "policy violation",
)
# 上游只给了短视频（例如 30 秒的请求只回 15 秒）：按「不拼接」策略换号重试，
# 属于上游行为，不该把号标记成失败/额度不足。
SHORT_CLIP_MARKERS = (
    "只给了", "不拼接",
)


def classify_upstream_failure(message: str) -> str:
    """把上游失败文案归类：window / transient / daily / content / quota / short / other。

    window（2026-10-15 新增）：710022002 是【全局窗口关】而不是「本号/本IP 瞬时限流」——
    实测一个窗口 ≈2~12s、≈15~23 个/h，开时整池同刻放行。它必须单独归一类，因为处置
    完全不同：不是「换号重试」，而是【交回窗口嗅探器排队，等下一个窗口再提交】。
    """
    blob = (message or "").lower()
    if config.WINDOW_TAKEOVER_710 and "710022002" in blob:
        return "window"
    if any(marker.lower() in blob for marker in TRANSIENT_THROTTLE_MARKERS):
        return "transient"
    if any(marker.lower() in blob for marker in DAILY_LIMIT_MARKERS):
        return "daily"
    if any(marker.lower() in blob for marker in CONTENT_REFUSAL_MARKERS):
        return "content"
    if any(marker.lower() in blob for marker in SHORT_CLIP_MARKERS):
        return "short"
    if any(marker.lower() in blob for marker in QUOTA_SHORT_MARKERS):
        return "quota"
    return "other"


def _reset_tz():
    """额度重置时区：config.LIMIT_RESET_TZ，缺 tzdata 时用固定偏移兜底。"""
    try:
        return ZoneInfo(config.LIMIT_RESET_TZ)
    except Exception:
        offsets = {"Asia/Tokyo": 9, "Asia/Hong_Kong": 8, "UTC": 0}
        return timezone(timedelta(hours=offsets.get(config.LIMIT_RESET_TZ, 9)))


def quota_day_anchor(now: datetime | None = None) -> datetime:
    """now 所属「额度日」的起点：当地 LIMIT_RESET_HOUR:00（默认日本时间 00:00）。"""
    now = now or datetime.now(_reset_tz())
    anchor = now.replace(hour=config.LIMIT_RESET_HOUR, minute=0, second=0, microsecond=0)
    if now < anchor:
        anchor -= timedelta(days=1)
    return anchor


def usage_day() -> str:
    """当前额度「日」：以 config.LIMIT_RESET_HOUR 为界的当地日期。

    Dola 的每日免费额度按当地零点滚动（2026-09-15 实测修正，原先按 11:00 记界偏晚），
    号池的计数日必须与它一致，否则会出现「额度已刷新但本地计数仍挡着」或
    「本地还有额度、上游已经用完」。上游回执里的「今日剩余 N 个」会实时回写校正。
    """
    return quota_day_anchor().date().isoformat()


def next_quota_reset_at() -> float:
    """下一次额度重置的时间戳（当地 LIMIT_RESET_HOUR:00）。"""
    return (quota_day_anchor() + timedelta(days=1)).timestamp()


class AllAccountsLimitedError(RuntimeError):
    """所有已开启调度的账号都达到 Dola 每日视频上限。"""


class AllAccountsQuotaBlockedError(RuntimeError):
    """所有已开启调度的账号都已知积分不足。"""


class AllAccountsGroupEmptyError(RuntimeError):
    """目标额度组（如 seedance-2.0 需要的【满额】组）暂时没有可用账号，排队超时。"""


class _UnlimitedSemaphore:
    """max_concurrency <= 0 时顶替 asyncio.Semaphore：不限制并发。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class BrowserPool:
    def __init__(self, accounts_dir: str = "accounts", db_path: str | None = None,
                 max_concurrency: int = 1):
        if not db_path:
            db_path = config.POOL_DB_PATH
        self.accounts_dir = Path(accounts_dir)
        # 0（或负数）= 取消全局并发闸门。
        self.semaphore = (asyncio.Semaphore(max_concurrency)
                          if max_concurrency > 0 else _UnlimitedSemaphore())
        self._locks: dict[str, asyncio.Lock] = {}
        self._fail_until: dict[str, float] = {}
        self._fail_streak: dict[str, int] = {}   # 连续失败计数（内存，成功即清零）
        self._probe_cache: dict[str, tuple[float, dict]] = {}   # 你好探测结果缓存
        # isolation_level=None（自动提交）：号池的 sqlite 连接是**多线程共用**的
        # （出片回调 on_balance、探测都在线程池里跑），sqlite3 模块的隐式事务会让
        # 两个线程的 BEGIN/COMMIT 互相踩，冒出 `cannot commit - no transaction is active`
        # 把任务判失败（2026-09-23 在并发用例里复现）。自动提交下每条语句各自原子，
        # 代码里原有的 commit() 变成无害的 no-op。
        self._conn = _safe_sqlite.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # WAL + synchronous=NORMAL：autocommit 下每条 UPDATE 都即时落盘，本机 fsync
        # 实测 ~40ms/次且这些写大多在事件循环线程里 → 一次写卡住整个服务 40ms+。
        # WAL+NORMAL 实测提交降到 0.02ms（2026-10-06 面板卡顿排查）。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS usage (account TEXT, day TEXT, used INTEGER, "
            "PRIMARY KEY(account, day))"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts_meta (
                name TEXT PRIMARY KEY,
                scheduling INTEGER DEFAULT 1,
                note TEXT DEFAULT '',
                email TEXT DEFAULT '',
                created_at REAL,
                last_used_at REAL DEFAULT 0,
                login_ok INTEGER,
                login_checked_at REAL DEFAULT 0,
                cooldown_until REAL DEFAULT 0,
                rate_limited_until REAL DEFAULT 0,
                limit_reason TEXT DEFAULT '',
                quota_blocked_until REAL DEFAULT 0,
                quota_reason TEXT DEFAULT '',
                credit_balance INTEGER,
                credit_checked_at REAL DEFAULT 0,
                failed_at REAL DEFAULT 0,
                failed_reason TEXT DEFAULT '',
                weight INTEGER DEFAULT 1,
                preferred INTEGER DEFAULT 0,
                egress TEXT DEFAULT '',
                sessionid TEXT DEFAULT '',
                source TEXT DEFAULT '',
                risk_control INTEGER DEFAULT 0,
                risk_reason TEXT DEFAULT ''
            )
            """
        )
        self._conn.commit()
        # 旧库兼容：补新元数据列
        for column, definition in (
            ("email", "TEXT DEFAULT ''"),
            ("rate_limited_until", "REAL DEFAULT 0"),
            ("limit_reason", "TEXT DEFAULT ''"),
            ("quota_blocked_until", "REAL DEFAULT 0"),
            ("quota_reason", "TEXT DEFAULT ''"),
            ("credit_balance", "INTEGER"),
            ("credit_checked_at", "REAL DEFAULT 0"),
            ("failed_at", "REAL DEFAULT 0"),
            ("failed_reason", "TEXT DEFAULT ''"),
            ("weight", "INTEGER DEFAULT 1"),
            ("preferred", "INTEGER DEFAULT 0"),
            ("egress", "TEXT DEFAULT ''"),
            ("sessionid", "TEXT DEFAULT ''"),
            ("source", "TEXT DEFAULT ''"),
            ("risk_control", "INTEGER DEFAULT 0"),
            ("risk_reason", "TEXT DEFAULT ''"),
            # ---- 2026-09-23 分组大迭代新增 ----
            ("risk_since", "REAL DEFAULT 0"),          # 进风控组的时间（永久，人工恢复）
            ("abnormal_until", "REAL DEFAULT 0"),      # 异常组（5 分钟无回执）到期时间
            ("abnormal_reason", "TEXT DEFAULT ''"),
            ("activated_at", "REAL DEFAULT 0"),        # 首次激活探测通过的时间
            ("probe_ok_at", "REAL DEFAULT 0"),         # 最近一次「你好」探测通过时间
            ("probe_result", "TEXT DEFAULT ''"),       # 最近一次探测结论（面板/排障用）
        ):
            try:
                self._conn.execute(f"ALTER TABLE accounts_meta ADD COLUMN {column} {definition}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass
        # 一次性回填：老号当年验证通过的时点当作「激活时间」，免得它们被误判成【待激活】
        self._conn.execute(
            "UPDATE accounts_meta SET activated_at=COALESCE(NULLIF(login_checked_at, 0), created_at) "
            "WHERE login_ok=1 AND COALESCE(activated_at, 0)=0"
        )
        self._conn.commit()
        # 一次性回填：号目录里有 cookie_state.json 的号一律标成 cookie 来源。
        # 2026-10-16 回灌：号池目录可由 junction 指向外部池（如 DBDA\acc），这些号是
        # 纯 cookie 号（只有 cookie_state.json、没有浏览器 profile），但 accounts_meta
        # 是在本库新建的行，source 为空 → _is_cookie_source()=False → 会被误判成 login 号
        # 而去走浏览器出片（本机没有可用 Chromium，只会报 driver 错误）。
        # 只填空值，绝不覆盖已有的 'cookie'/'login' 判定。
        _cookie_backfill = 0
        for _d in (self.accounts_dir.iterdir() if self.accounts_dir.exists() else []):
            if not _d.is_dir() or _d.name.startswith("."):
                continue
            if (_d / "cookie_state.json").is_file():
                cur = self._conn.execute(
                    "SELECT source FROM accounts_meta WHERE name=?", (_d.name,)).fetchone()
                if cur is None:
                    self._ensure_meta(_d.name)
                    cur = self._conn.execute(
                        "SELECT source FROM accounts_meta WHERE name=?", (_d.name,)).fetchone()
                if cur is not None and not (cur["source"] or "").strip():
                    self._conn.execute(
                        "UPDATE accounts_meta SET source='cookie' WHERE name=?", (_d.name,))
                    _cookie_backfill += 1
        if _cookie_backfill:
            self._conn.commit()
            logger.info("cookie 来源回填 %d 个号（有 cookie_state.json 但 source 为空）",
                        _cookie_backfill)
        self._engine: _PoolEngine | None = None

    # ===== 账号发现/元数据 =====

    def _ensure_meta(self, name: str):
        self._conn.execute(
            "INSERT OR IGNORE INTO accounts_meta (name, created_at) VALUES (?, ?)",
            (name, time.time()),
        )
        self._conn.commit()

    @property
    def accounts(self) -> list:
        if not self.accounts_dir.exists():
            return []
        names = sorted(d.name for d in self.accounts_dir.iterdir()
                       if d.is_dir() and not d.name.startswith("."))
        for n in names:
            self._ensure_meta(n)
        return names

    def _meta(self, name: str):
        return self._conn.execute(
            "SELECT * FROM accounts_meta WHERE name=?", (name,)).fetchone()

    def find_accounts_by_email(self, email: str) -> list:
        """按邮箱（忽略大小写与首尾空格）反查已有账号名，用于添加账号时去重。"""
        norm = (email or "").strip().lower()
        if not norm:
            return []
        rows = self._conn.execute(
            "SELECT name FROM accounts_meta WHERE lower(trim(email))=?", (norm,)
        ).fetchall()
        return [r["name"] for r in rows]

    def set_sessionid(self, name: str, sessionid: str) -> None:
        self._conn.execute(
            "UPDATE accounts_meta SET sessionid=? WHERE name=?", (sessionid or "", name)
        )
        self._conn.commit()

    def set_source(self, name: str, source: str) -> None:
        """记录账号来源：login（Google OAuth 登录） / cookie（cookie 载入）。"""
        self._conn.execute(
            "UPDATE accounts_meta SET source=? WHERE name=?", (source or "", name)
        )
        self._conn.commit()

    def ensure_account(self, name: str) -> None:
        """确保账号在 accounts_meta 有行（新账号打标/写邮箱前调用）。"""
        self._ensure_meta(name)

    def find_by_sessionid(self, sessionid: str) -> list:
        """按 sessionid 反查已有账号名（用于导入去重）。"""
        if not sessionid:
            return []
        rows = self._conn.execute(
            "SELECT name FROM accounts_meta WHERE sessionid=? AND sessionid<>''", (sessionid,)
        ).fetchall()
        return [r["name"] for r in rows]

    def used_today(self, account: str) -> int:
        row = self._conn.execute(
            "SELECT used FROM usage WHERE account=? AND day=?",
            (account, usage_day()),
        ).fetchone()
        return row[0] if row else 0

    def _claim(self, account: str, cost: int = 1):
        """按额度点数记账：出片/占额度事件扣除 cost 点（默认 1）。"""
        if cost <= 0:
            cost = 1
        self._conn.execute(
            "INSERT INTO usage(account, day, used) VALUES (?,?,?) "
            "ON CONFLICT(account, day) DO UPDATE SET used=used+?",
            (account, usage_day(), cost, cost),
        )
        self._conn.commit()

    def _claim_for(self, account: str, table_cost: int, result: dict | None) -> int:
        """按额度表记账（2.5 一条 2 点、2.0 一条 3 点）。

        上游回执里的「将消耗 N 个视频生成额度」是**报价**，与实际扣点不一致
        （30 秒报价 6 点、实扣 2 点），所以记账以额度表为准；表里查不到（未知模型/时长）
        才退回上游报价。上游真实余额由回执里的「今日剩余 N 个」回写校正。
        """
        quoted = int((result or {}).get("credits_used") or 0)
        charge = table_cost or quoted or 1
        if quoted and quoted != charge:
            print(f"[pool] {account} 记账 {charge} 点（额度表）· 上游报价 {quoted} 点", flush=True)
        self._claim(account, charge)
        return charge

    def _duration_cost(self, model: str, duration: int = 0) -> int:
        """单次出片消耗的点数：只看模型，不看时长（对齐 dola-pool-cookie）。

        旧实现对白名单里查不到的 (模型, 时长) 组合默认返回 1 点，属于**少扣**：
        一旦放开任意时长，每个未登记时长都会被按 1 点记账，一个号一天能跑 4 条。
        duration 参数保留，仅为兼容既有调用方签名。
        """
        m = (model or "").lower().replace("_", "-")
        if "2.5" in m or "2-5" in m:
            m = "seedance-2.5"
        else:
            # 认不出的模型一律按 2.0 计（= 最贵的 3 点）：宁可少派，也不要拿号去白撞额度。
            m = "seedance-2.0"
        return config.MODEL_COSTS.get(m, config.DEFAULT_CREDIT_COST)

    # ---- 模型 → 候选额度组（2026-09-23 P2）----
    # 不派发的组：冷却（额度 ≤1）、风控（永久）、待激活（未探测）、异常（出片异常）
    BLOCKED_GROUPS = (GROUP_COOLING, GROUP_RISK, GROUP_PENDING, GROUP_ABNORMAL)

    @staticmethod
    def _quota_group(account_row: dict) -> str:
        """只按额度的分组（满额/半额/冷却），不含风控/生成中这类状态。

        面板上的 group 是"状态组"（风控/生成中优先），派发时需要的是"额度组"，
        两者分开算，免得一个正在出片的满额号被当成半额。
        """
        remaining = int(account_row.get("remaining") or 0)
        limit = int(account_row.get("limit") or config.DAILY_LIMIT or DAILY_LIMIT)
        if remaining <= 1:
            return GROUP_COOLING
        if remaining < limit:
            return GROUP_HALF
        return GROUP_FULL

    def _allowed_quota_groups(self, model: str) -> tuple[str, ...]:
        """seedance-2.0（3 点）只吃满额；seedance-2.5（2 点）先半额、后满额。"""
        if self._duration_cost(model) >= 3:
            return (GROUP_FULL,)
        return (GROUP_HALF, GROUP_FULL)

    def _ordered_candidates(self, allowed: tuple[str, ...]) -> list:
        """按额度组优先级排序候选（2.5 先半额再满额），组内保持既有顺序（钉住 > weight > 名称）。"""
        rows = self._sorted_accounts()
        if len(allowed) < 2:
            return rows
        rank = {group: index for index, group in enumerate(allowed)}
        return sorted(rows, key=lambda a: rank.get(self._quota_group(a), len(allowed)))

    def _next_limit_reset(self) -> float:
        """下一次每日额度刷新时间（默认日本时间次日 00:00）。"""
        return next_quota_reset_at()

    def next_quota_reset_at(self) -> float:
        return next_quota_reset_at()

    def reset_daily_quotas(self) -> int:
        """每日额度重置：恢复所有被「额度不足/每日上限/失败」挡住的账号。

        风控冷却（cooldown_until）和登录态（login_ok）不动：
        前者是上游处罚窗口，后者要靠验证功能恢复。

        只重置实际存在的账号（accounts/<name> 目录）：这条 UPDATE 早先没有 WHERE，
        rowcount 会把已删除账号残留的元数据行一起算进来，面板就会出现
        「已重置 N 个账号」而实际账号数远没那么多（N = accounts_meta 的总行数）。
        """
        names = list(self.accounts)
        if not names:
            self._fail_until.clear()
            return 0
        placeholders = ",".join("?" for _ in names)
        cur = self._conn.execute(
            "UPDATE accounts_meta SET rate_limited_until=0, limit_reason='', "
            "quota_blocked_until=0, quota_reason='', "
            "credit_balance=NULL, credit_checked_at=0 "
            f"WHERE name IN ({placeholders})",
            names,
        )
        self._conn.commit()
        self._fail_until.clear()
        self._fail_streak.clear()
        return cur.rowcount

    def _clear_expired_rate_limits(self):
        now = time.time()
        self._conn.execute(
            "UPDATE accounts_meta SET rate_limited_until=0, limit_reason='', "
            "quota_blocked_until=0, quota_reason='' "
            "WHERE (rate_limited_until > 0 AND rate_limited_until <= ?) "
            "OR (quota_blocked_until > 0 AND quota_blocked_until <= ?)", (now, now))
        # 必须无条件 commit：即使一条都没命中，Python sqlite3 也已经为这条 UPDATE
        # 隐式开了写事务。不提交就把 pool_usage.db 的写锁一直攥在手里 ——
        # 之后所有写操作（建/删代理、导入 cookie、改账号元数据）都会 15s 超时
        # 报 "database is locked"。这个方法由 list_accounts() 调用，
        # 也就是「面板一打开就锁库」。
        self._conn.commit()

    def _mark_quota_blocked(self, account: str, reason: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=?, last_used_at=? WHERE name=?",
            (self._next_limit_reset(), reason[:300], time.time(), account),
        )
        self._conn.commit()

    def _mark_daily_limit(self, account: str, reason: str = ""):
        """Dola 明确返回每日上限：未成功不扣本地点数（2026-09-02 规则），
        只把账号限流标记到次日零点，期间跳过该号。"""
        self._conn.execute(
            "UPDATE accounts_meta SET last_used_at=?, rate_limited_until=?, limit_reason=? WHERE name=?",
            (time.time(), self._next_limit_reset(), reason[:300], account),
        )
        self._conn.commit()

    def _mark_failed(self, account: str, reason: str = ""):
        """【已废弃】旧的「叠加失败」概念（2.1.0 P3 删除）。

        它会把好号锁到「全池重置」为止，与新分组模型冲突；现在连续失败改走
        `_note_fail_streak()` → 异常组（到期自动恢复），不再有「失败(待全池重置)」状态。
        保留这个空壳只为兼容外部调用，不再写库。
        """
        return

    def _mark_abnormal(self, account: str, reason: str,
                       seconds: float | None = None) -> None:
        """把账号放进【异常】组：暂停派发，到期自动恢复（0 秒 = 立刻可恢复）。"""
        ttl = config.ABNORMAL_COOLDOWN_SEC if seconds is None else float(seconds)
        self._conn.execute(
            "UPDATE accounts_meta SET abnormal_until=?, abnormal_reason=? WHERE name=?",
            (time.time() + ttl, (reason or "生成过程异常")[:300], account),
        )
        self._conn.commit()

    def _note_fail_streak(self, account: str, reason: str) -> int:
        """连续失败计数：同号连续失败 3 次才进【异常】组，避免一次抖动就把号停掉。"""
        streak = self._fail_streak.get(account, 0) + 1
        self._fail_streak[account] = streak
        if streak >= FAIL_STREAK_TO_ABNORMAL:
            print(f"[pool] {account} 连续失败 {streak} 次 → 进【异常】组"
                  f"（{config.ABNORMAL_COOLDOWN_SEC // 60} 分钟后自动恢复）", flush=True)
            self._mark_abnormal(account, reason)
        return streak

    def _note_upstream_failure(self, account: str, message: str, attempt: int = 0) -> str:
        """按上游失败文案落状态，返回归类：window / transient / daily / quota / other。

        - window（710022002）：【全局窗口关】，不是本号/本IP 的问题 → 不冷却、不换号重试，
          交回窗口嗅探器排队（由上层 generate_video 的 except WindowClosedError 处理）；
        - transient：出口 IP/上游瞬时限流（非 710 的其它「访问频繁」话术）→ 只换号重试；
        - daily：当日次数用完 → 标记到额度刷新；
        - quota：本单额度不够（如 30 秒需 3 点只剩 2 点）→ 标记到额度刷新；
        - other：普通生成失败 → 只换号重试；同号连续失败 3 次才进【异常】组（到期自动恢复）。
        """
        kind = classify_upstream_failure(message)
        if kind == "window":
            print(f"[pool] {account} 全局窗口关闭（710022002），交回窗口嗅探器排队，不换号重试: "
                  f"{message[:160]}", flush=True)
            gate = dola_window.get_gate()
            if gate is not None:
                gate.note_closed()
        elif kind == "transient":
            print(f"[pool] {account} 上游拒绝本次提交（访问频繁），换号重试: {message[:160]}",
                  flush=True)
        elif kind == "daily":
            print(f"[pool] {account} 当日次数已用完，标记到额度刷新: {message[:160]}", flush=True)
            self._mark_daily_limit(account, message)
        elif kind == "quota":
            print(f"[pool] {account} 本单额度不够，标记到额度刷新: {message[:160]}", flush=True)
            self._mark_quota_blocked(account, message)
        elif kind == "content":
            # 版权/内容拒稿：同一条提示词换号也过不了，不能把号标记成失败或额度不足。
            print(f"[pool] {account} 内容/版权拒稿（与账号无关，不标记账号）: {message[:160]}", flush=True)
            # 拒稿原文同时落到账号备注：面板上能直接看到上游原话（肖像保护/内容审核/侵权…），
            # 不用再去翻日志或截断的任务报错。
            label = failure_text.label_for(message) or "内容/版权"
            self.append_note(account, failure_text.account_note_line(label, message))
            print(f"[pool] {account} 拒稿原文已写入账号备注", flush=True)
        elif kind == "short":
            print(f"[pool] {account} 只出了短视频（不拼接，不标记账号）: {message[:160]}", flush=True)
        else:
            print(f"[pool] {account} 生成失败（第 {attempt} 次），换号重试: {message[:200]}", flush=True)
            self._note_fail_streak(account, message)
        return kind

    def _clear_all_failed(self) -> int:
        """清空所有账号的叠加失败标记（全池兜底重置用）。"""
        cur = self._conn.execute(
            "UPDATE accounts_meta SET failed_at=0, failed_reason='' WHERE failed_at>0"
        )
        self._conn.commit()
        return cur.rowcount

    def _overlay_unlockable(self, cost: int = 1) -> bool:
        """【已废弃】2.1.0 P3：叠加失败与「全池重置」概念一并删除，恒返回 False。"""
        return False

    def _clear_all_failed(self) -> int:
        """【已废弃】2.1.0 P3：不再有叠加失败标记，恒返回 0。"""
        return 0

    def list_accounts(self, *, processing_accounts: set | None = None) -> list:
        """面板视图：meta + 配额 + 分组 + 是否忙合并。

        processing_accounts：任务库里 status='processing' 的账号集合（server 侧传入），
        用于服务重启后仍能正确显示【生成中】（内存锁会丢，任务库不会）。
        """
        self._clear_expired_rate_limits()
        now = time.time()
        out = []
        for a in self.accounts:
            m = self._meta(a)
            used = self.used_today(a)
            lock = self._locks.get(a)
            out.append({
                "name": a,
                "scheduling": bool(m["scheduling"]) if m else True,
                "note": m["note"] if m else "",
                "email": m["email"] if m else "",
                "created_at": m["created_at"] if m else 0,
                "last_used_at": m["last_used_at"] if m else 0,
                "login_ok": m["login_ok"] if m else None,
                "login_checked_at": m["login_checked_at"] if m else 0,
                "cooldown_until": m["cooldown_until"] if m else 0,
                "cooling": bool(m and m["cooldown_until"] > now),
                "rate_limited_until": m["rate_limited_until"] if m and m["rate_limited_until"] else 0,
                "rate_limited": bool(m and m["rate_limited_until"] > now),
                "limit_reason": m["limit_reason"] if m else "",
                "quota_blocked_until": m["quota_blocked_until"] if m and m["quota_blocked_until"] else 0,
                "quota_blocked": bool(m and m["quota_blocked_until"] > now),
                "quota_reason": m["quota_reason"] if m else "",
                "credit_balance": m["credit_balance"] if m else None,
                "credit_checked_at": m["credit_checked_at"] if m else 0,
                "failed_at": m["failed_at"] if m else 0,
                "failed_reason": m["failed_reason"] if m else "",
                "failed": bool(m and m["failed_at"] > 0),
                "weight": m["weight"] if m and m["weight"] else 1,
                "preferred": bool(m and m["preferred"]),
                "source": m["source"] if m else "",
                "risk_control": bool(m and m["risk_control"]),
                "risk_reason": m["risk_reason"] if m else "",
                "status": self._derive_status(m, now),
                # 展示字段：只读当前绑定，绝不触发补节点/探活（同步网络请求会冻结事件循环）
                "effective_egress": self._egress_for(a, ensure=False),
                "used_today": used,
                "limit": DAILY_LIMIT,
                "remaining": max(0, DAILY_LIMIT - used),
                "busy": bool(lock and lock.locked()),
                "risk_since": m["risk_since"] if m else 0,
                "abnormal_until": m["abnormal_until"] if m else 0,
                "abnormal_reason": m["abnormal_reason"] if m else "",
                "activated_at": m["activated_at"] if m else 0,
                "probe_ok_at": m["probe_ok_at"] if m else 0,
                "probe_result": m["probe_result"] if m else "",
            })
        for a in out:
            a["group"], a["group_reason"] = group_of(
                a, now, busy=a["busy"], processing_accounts=processing_accounts)
        return out

    def _derive_status(self, m, now: float) -> str:
        if not m or m["login_ok"] is None:
            return "unsigned"
        if m["cooldown_until"] and m["cooldown_until"] > now:
            return "cooldown"
        if m["rate_limited_until"] and m["rate_limited_until"] > now:
            return "cooldown"
        if m["quota_blocked_until"] and m["quota_blocked_until"] > now:
            return "cooldown"
        if m["login_ok"] == 0:
            return "expired"
        if not m["scheduling"]:
            return "standby"
        return "healthy"

    def set_scheduling(self, name: str, on: bool):
        self._conn.execute(
            "UPDATE accounts_meta SET scheduling=? WHERE name=?", (1 if on else 0, name))
        self._conn.commit()

    def set_email(self, name: str, email: str):
        self._conn.execute(
            "UPDATE accounts_meta SET email=? WHERE name=?", (email, name))
        self._conn.commit()

    def set_login_status(self, name: str, ok: bool):
        # 先确保 accounts_meta 有行：否则这条 UPDATE 会静默影响 0 行，
        # 账号永远停在「未验证/待激活」——加号流程之外（新号第一次验证）踩过这个坑。
        self._ensure_meta(name)
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=?, "
            "activated_at=COALESCE(NULLIF(activated_at,0), ?) WHERE name=?",
            (1 if ok else 0, time.time(), time.time() if ok else 0, name),
        )
        self._conn.commit()

    def mark_verify_pending(self, name: str, reason: str):
        """探活软失败（网络/代理异常）：不动 login_ok（保持待激活/现值），只记原因。

        网络抖动 ≠ 登录失效：把软失败落成 login_ok=0 会把好号误标「登录失效」
        （2026-10-06 注册批次整批复蹈此坑）。这里只更新检查时间和失败原因，
        登录态留待网络恢复后重新验证。
        """
        self._ensure_meta(name)
        self._conn.execute(
            "UPDATE accounts_meta SET login_checked_at=?, failed_reason=? WHERE name=?",
            (time.time(), str(reason)[:200], name),
        )
        self._conn.commit()

    def _set_risk(self, name: str, reason: str = "") -> None:
        """标记该账号被风控（登录态失效/生成即被踢），供账号管理面板显示「风控」。"""
        self._ensure_meta(name)
        self._conn.execute(
            "UPDATE accounts_meta SET risk_control=1, risk_reason=?, risk_since=? WHERE name=?",
            (str(reason)[:200], time.time(), name),
        )
        self._conn.commit()

    def _clear_risk(self, name: str) -> None:
        self._conn.execute(
            "UPDATE accounts_meta SET risk_control=0, risk_reason='', risk_since=0 WHERE name=?",
            (name,),
        )
        self._conn.commit()

    # ---- 「你好」风控探测（2026-09-23 P4）----
    def _probe_network(self, name: str, force: bool) -> dict:
        """只做网络探测，**不碰数据库**（会被丢进线程池执行）。

        带缓存（`DOLA_HELLO_PROBE_CACHE_SECONDS`，默认 300 秒），避免每个任务都多花
        3 秒、还和出片抢同一条上游提交通道；`force=True` 用于面板手动探测。
        非纯 API 账号（浏览器登录号）跳过探测，返回 ok=True 并注明原因。
        """
        now = time.time()
        ttl = config.HELLO_PROBE_CACHE_SECONDS
        cached = self._probe_cache.get(name)
        if cached and not force and ttl > 0 and now - cached[0] < ttl:
            return {**cached[1], "cached": True, "probed": False}

        state = self._pure_state(name)
        if state is None:
            result = {"ok": True, "status": "skipped", "login_ok": True, "replied": True,
                      "cached": False, "probed": False,
                      "reason": "非纯 API 账号（浏览器登录），跳过你好探测",
                      "reply": "", "elapsed": 0.0}
        else:
            try:
                result = hello_probe(
                    state, proxy=self._proxy_url_for(name),
                    prompt=config.HELLO_PROBE_PROMPT,
                    timeout_sec=config.HELLO_PROBE_TIMEOUT)
            except Exception as exc:  # 探测本身炸了也算不通过（宁可保守）
                result = {"ok": False, "status": "error", "login_ok": False, "replied": False,
                          "cached": False, "probed": True,
                          "reason": f"探测异常: {str(exc)[:180]}", "reply": "", "elapsed": 0.0}
            result.setdefault("cached", False)
            result.setdefault("probed", True)
        return result

    def _persist_probe(self, name: str, result: dict, *, apply_risk: bool = True) -> dict:
        """探测结论落库（只在事件循环线程里调用，SQLite 连接不跨线程写）。

        apply_risk=False：只记结论不判风控。批量探测的**第一次**用这个，
        重试仍失败才 apply_risk=True —— 单次「没回复」很可能是上游抖动，
        一次就锁号会把好号误封（线上实测踩过）。
        """
        self._probe_cache[name] = (time.time(), result)
        self._ensure_meta(name)
        remark = str(result.get("reason") or "")[:200]
        if result.get("ok"):
            was_risk = bool(self._meta(name) and self._meta(name)["risk_control"])
            print(f"[pool] {name} 你好探测通过（{result.get('elapsed')}s，"
                  f"回复 {len(str(result.get('reply') or ''))} 字）"
                  + ("；该号仍在【风控】组（按规则要人工恢复才会重新派发）" if was_risk else ""),
                  flush=True)
            self._conn.execute(
                "UPDATE accounts_meta SET probe_ok_at=?, probe_result=?, login_ok=1, "
                "login_checked_at=?, activated_at=COALESCE(NULLIF(activated_at,0), ?) "
                "WHERE name=?",
                (time.time(), remark, time.time(), time.time(), name))
            self._conn.commit()
        else:
            self._conn.execute(
                "UPDATE accounts_meta SET probe_ok_at=0, probe_result=? WHERE name=?",
                (remark, name))
            self._conn.commit()
            # 只有"硬信号"才判风控：被登出（x-tt-agw-login != 1）或 一句回复都没有。
            # 软失败（网络不通 / 上游 710022002 拒绝）**不能**判风控 ——
            # 2026-09-23 线上实测：健康号也会被上游拒绝一次，按软失败判风控会把好号永久锁死。
            if apply_risk and result.get("status") in ("logged_out", "no_reply"):
                self._set_risk(name, f"你好探测：{remark}")
            else:
                print(f"[pool] {name} 你好探测未完成（{remark}）→ 本次跳过该号，不判风控",
                      flush=True)
        return result

    def apply_probe_risk(self, name: str, result: dict) -> None:
        """按探测结论把号判进【风控】组。

        批量探测用：第一次探测先不落风控（避免上游抖动误封），但「被登出」是强信号，
        批量流程会立刻调用这里把它判掉。
        """
        remark = str((result or {}).get("reason") or "探测未通过")[:200]
        self._set_risk(name, f"你好探测：{remark}")

    # ---- 全局窗口闸门（2026-10-15 渠道改造）----
    def cookie_state_path(self, name: str) -> str | None:
        """cookie 源号的纯 API 状态文件（窗口哨兵探针要用）。"""
        return self._pure_state(name)

    def quota_left(self, name: str) -> int:
        """该号当日剩余出片配额（用于窗口爆发的准入阈值 WINDOW_QUOTA_THRESHOLD）。"""
        try:
            used = self.used_today(name)
        except Exception:  # noqa: BLE001
            return int(config.DAILY_LIMIT)
        return max(0, int(config.DAILY_LIMIT) - int(used or 0))

    def proxy_url_for(self, name: str) -> str:
        """该号出片用的代理 URL（窗口爆发提交时按号取出口）。"""
        try:
            return self._proxy_url_for(name)
        except Exception:  # noqa: BLE001
            return config.PROXY or ""

    def start_window_engine(self) -> dict:
        """启动【全局窗口嗅探器】（幂等）：server 启动时调一次即可。

        哨兵只用池里第一个 cookie 号当探针（省额度）；窗口命中后，所有走纯 API 的
        worker 协程会从同一个事件上同时醒过来、同刻各自打出自己号的那一发提交。
        """
        gate = dola_window.start_for_pool(
            self.accounts, cookie_state_of=self.cookie_state_path,
            report_path=dola_window._default_report_path())
        if gate is None:
            return {"enabled": False}
        return {"enabled": True, **gate.state()}

    def window_state(self) -> dict:
        gate = dola_window.get_gate()
        if gate is None:
            return {"enabled": False, "reason": "未启动 / DOLA_WINDOW_SNIFF=0"}
        return {"enabled": True, **gate.state()}

    def probe_hello(self, name: str, *, force: bool = False,
                    apply_risk: bool = True) -> dict:
        """同步探测（网络 + 落库）。测试与同步调用方用；异步路径见 probe_hello_async。"""
        return self._persist_probe(name, self._probe_network(name, force),
                                   apply_risk=apply_risk)

    async def probe_hello_async(self, name: str, *, force: bool = False,
                                apply_risk: bool = True) -> dict:
        """派发门用的异步探测：网络在线程池，落库回到事件循环线程。"""
        result = await asyncio.to_thread(self._probe_network, name, force)
        return self._persist_probe(name, result, apply_risk=apply_risk)

    def recover_account(self, name: str, kind: str = "risk") -> dict:
        """人工恢复：把号从【风控】或【异常】组放回正常流程。

        风控恢复会同时把登录态清空（回到【待激活】），因为「被登出」是事实，
        得靠下一次探测/验证重新确认登录，不能靠按一下按钮就假定它又能出片。
        """
        self._ensure_meta(name)
        if kind == "abnormal":
            self._conn.execute(
                "UPDATE accounts_meta SET abnormal_until=0, abnormal_reason='' WHERE name=?",
                (name,))
            self._conn.commit()
        else:
            self._conn.execute(
                "UPDATE accounts_meta SET risk_control=0, risk_reason='', risk_since=0, "
                "login_ok=NULL, probe_ok_at=0 WHERE name=?",
                (name,))
            self._conn.commit()
        self._probe_cache.pop(name, None)
        self._fail_streak.pop(name, None)
        self._fail_until.pop(name, None)
        return {"ok": True, "name": name, "kind": kind}

    def set_note(self, name: str, note: str):
        self._conn.execute(
            "UPDATE accounts_meta SET note=? WHERE name=?", (note, name))
        self._conn.commit()

    def append_note(self, name: str, line: str, limit: int = 500) -> str:
        """把一行说明追加到账号备注末尾（保留人工写的备注，超长只留最新内容）。

        账号备注是人工字段，所以只在审核类拒稿时追加（见 _note_upstream_failure），
        用换行分隔，超长时从头部截断，保证面板里看到的最新一条始终完整。
        """
        if not line:
            return ""
        row = self._conn.execute(
            "SELECT note FROM accounts_meta WHERE name=?", (name,)).fetchone()
        old = (row["note"] if row and row["note"] else "").strip()
        note = f"{old}\n{line}" if old else line
        if len(note) > limit:
            note = note[-limit:]
        self.set_note(name, note)
        return note

    def rename_account(self, name: str, new_name: str) -> dict:
        """给账号改名：profile 目录 + 所有以账号名为主键的记录一起搬。

        账号名同时是 accounts_meta / account_proxy / proxy_sessions / usage 的主键，
        也是 accounts/<name> 目录名和加密凭据的 key，所以要整组搬迁；只改一处会让代理绑定、
        今日额度、cookie_state 全部对不上号。
        """
        name = (name or "").strip()
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("新账号名不能为空")
        if any(ch in new_name for ch in "/\\") or new_name.startswith("."):
            raise ValueError("账号名不能包含路径分隔符，也不能以点开头")
        if name not in self.accounts:
            raise FileNotFoundError(f"账号不存在: {name}")
        if new_name == name:
            return {"renamed": False, "name": name}
        if new_name in self.accounts or self._meta(new_name):
            raise ValueError(f"账号名已存在: {new_name}")
        lock = self._locks.get(name)
        if lock and lock.locked():
            raise RuntimeError("账号正在出片，不能改名")
        src = self.accounts_dir / name
        dst = self.accounts_dir / new_name
        src.rename(dst)
        try:
            self._conn.execute("UPDATE accounts_meta SET name=? WHERE name=?",
                               (new_name, name))
            self._conn.execute("UPDATE usage SET account=? WHERE account=?",
                               (new_name, name))
            self._conn.commit()
            proxy_store.rename_account(name, new_name)
        except Exception:
            dst.rename(src)  # 元数据搬迁失败就把目录挪回去，避免半搬状态
            raise
        cred_store.rename(name, new_name)
        if name in self._locks:
            self._locks[new_name] = self._locks.pop(name)
        if name in self._fail_until:
            self._fail_until[new_name] = self._fail_until.pop(name)
        self._sync_engine()
        return {"renamed": True, "name": new_name, "previous": name}

    def delete_account(self, name: str):
        lock = self._locks.get(name)
        if lock and lock.locked():
            raise RuntimeError("账号正在出片，不能删除")
        d = self.accounts_dir / name
        if d.exists():
            shutil.rmtree(d)
        # 代理绑定 / 出口会话 / 今日额度都要一起清掉：否则同名新号（尤其是自动命名的 acc1）
        # 会继承旧状态 —— 表现为导入/出片时莫名套上一个旧代理（socks5 带鉴权时浏览器直接起不来），
        # 或者今天的额度一开始就被算掉。
        proxy_store.forget_account(name)
        self._conn.execute("DELETE FROM accounts_meta WHERE name=?", (name,))
        self._conn.execute("DELETE FROM usage WHERE account=?", (name,))
        self._conn.commit()

    async def verify_account(self, name: str) -> bool:
        """验证登录态并写回缓存。号忙抛 RuntimeError。"""
        ok, _reason, _soft = await self.verify_account_detail(name)
        return ok

    async def verify_account_detail(self, name: str) -> tuple[bool, str, bool]:
        """验证登录态并写回缓存，返回 (是否登录, 失败原因, 是否软失败)。号忙抛 RuntimeError。

        cookie 账号走纯 API 探活（不开浏览器，避免 profile 被并发占用）；
        login 账号走浏览器 check_login_state。

        软失败（网络/代理异常，探活没跑成）**不改 login_ok**：网络抖动 ≠ 登录失效，
        只把原因落库到 failed_reason 供面板排查，登录态等网络恢复后复核。
        只有硬信号（上游明确回未登录）才落 login_ok=0。
        """
        if name not in self.accounts:
            raise FileNotFoundError(f"profile 不存在: {name}")
        lock = self._locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("账号正在出片，稍后再验证")
        reason = ""
        soft = False
        pure_state = self._pure_state(name)
        if pure_state is not None and self._is_cookie_source(name):
            def _probe() -> tuple[bool, str, bool]:
                return probe_login_detail(
                    pure_state, proxy=self._proxy_url_for(name))

            ok, msg, hard = await asyncio.to_thread(_probe)
            if not ok and not hard and proxy_store.is_tls_cert_error(str(msg or "")):
                # 出口 TLS 被中间人劫持：这不是网络抖动，是**该节点确定不可用**。
                # 判废节点 + 换出口 IP 后重探一次，否则号会被一直钉死在坏节点上。
                if self._quarantine_bad_egress(name, str(msg or "")):
                    print(f"[verify] {name} 出口 TLS 证书异常(疑似劫持)，"
                          f"已判废该节点并换出口重试", flush=True)
                    ok, msg, hard = await asyncio.to_thread(_probe)
            if not ok:
                reason = str(msg or "")[:200]
                print(f"[verify] {name} pure_probe -> ok=False hard={hard} reason={reason}", flush=True)
                if hard:
                    self._conn.execute(
                        "UPDATE accounts_meta SET login_ok=0, login_checked_at=?, "
                        "failed_reason=? WHERE name=?",
                        (time.time(), reason, name),
                    )
                    self._conn.commit()
                else:
                    # 软失败：保持原 login_ok，只记原因（面板不再把网络抖动当成登录失效）
                    soft = True
                    self.mark_verify_pending(name, reason)
            else:
                # 成功：login_ok=1 + 清失败原因 + 补激活时间（对齐旧行为的整体落库）
                self._conn.execute(
                    "UPDATE accounts_meta SET login_ok=1, login_checked_at=?, "
                    "failed_reason='', activated_at=COALESCE(NULLIF(activated_at,0), ?) "
                    "WHERE name=?",
                    (time.time(), time.time(), name),
                )
                self._conn.commit()
        else:
            from browser import check_login_state, chat_liveness_probe
            ok = await check_login_state(name)
            if ok and config.VERIFY_CHAT_PROBE:
                ok, chat_reason = await chat_liveness_probe(name)
                print(f"[verify] {name} chat_probe -> ok={ok} reason={chat_reason}", flush=True)
                if not ok:
                    reason = f"chat_probe:{chat_reason}"[:200]
                    self._conn.execute(
                        "UPDATE accounts_meta SET failed_at=? WHERE name=?",
                        (time.time(), name),
                    )
                    self._conn.commit()
            # 浏览器路径（login 来源账号）沿用整行落库：login_ok + 检查时间 + 原因
            self._conn.execute(
                "UPDATE accounts_meta SET login_ok=?, login_checked_at=?, failed_reason=? "
                "WHERE name=?",
                (1 if ok else 0, time.time(), reason, name),
            )
            self._conn.commit()
        if ok:
            reason = ""
        return bool(ok), reason, soft

    # ===== 调度 =====

    def _set_credit_balance(self, account: str, balance: int, source: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET credit_balance=?, credit_checked_at=? WHERE name=?",
            (max(0, int(balance)), time.time(), account),
        )
        if balance < 2:
            self._conn.execute(
                "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=? WHERE name=?",
                (self._next_limit_reset(), source[:300] or "积分不足", account),
            )
        else:
            # 上游已经报出可用余额 → 之前那条「积分不足」的阻塞立刻解除。
            self._conn.execute(
                "UPDATE accounts_meta SET quota_blocked_until=0, quota_reason='' "
                "WHERE name=? AND quota_blocked_until>0",
                (account,),
            )
        self._conn.commit()

    def _credit_available(self, account: str, required: int = 2) -> bool:
        row = self._meta(account)
        return not row or row["credit_balance"] is None or row["credit_balance"] >= required

    def _schedulable(self, a: dict) -> bool:
        return (a["scheduling"] and not a["cooling"] and not a["rate_limited"]
                and not a["quota_blocked"] and a["login_ok"] != 0
                and a["used_today"] < DAILY_LIMIT
                and (a["credit_balance"] is None or a["credit_balance"] >= 2))

    def _recently_failed(self, account: str) -> bool:
        """账号刚被任一任务尝试并失败：并发任务应跳过，避免 A 失败后 B 立刻重试同一账号。"""
        return self._fail_until.get(account, 0) > time.time()

    def _mark_attempt_failed(self, account: str) -> None:
        self._fail_until[account] = time.time() + FAIL_COOLDOWN_SEC

    @property
    def all_accounts_limited(self) -> bool:
        """所有开启调度且未处于风控冷却的账号都已达到每日上限。"""
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["rate_limited"] or a["used_today"] >= DAILY_LIMIT for a in candidates
        )

    @property
    def all_accounts_quota_blocked(self) -> bool:
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["quota_blocked"] or a["rate_limited"] or a["used_today"] >= DAILY_LIMIT
            for a in candidates
        ) and any(a["quota_blocked"] for a in candidates)

    @property
    def available(self) -> bool:
        return any(self._schedulable(a) for a in self.list_accounts())

    @property
    def cookie_count(self) -> int:  # 兼容 /health 旧字段
        return len(self.accounts)

    def account_status(self) -> list:
        return [{
            "account": a["name"], "used_today": a["used_today"], "limit": a["limit"],
            "rate_limited": a["rate_limited"], "rate_limited_until": a["rate_limited_until"],
            "quota_blocked": a["quota_blocked"], "quota_blocked_until": a["quota_blocked_until"],
        } for a in self.list_accounts()]

    @staticmethod
    def _egress_of(proxy_url: str) -> str:
        proxy_url = (proxy_url or "").strip()
        if not proxy_url or proxy_url == "direct":
            return "direct"
        if "://" not in proxy_url:
            proxy_url = "http://" + proxy_url
        from urllib.parse import urlsplit
        parts = urlsplit(proxy_url)
        if not parts.hostname:
            return "direct"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return f"{parts.scheme}://{parts.hostname}:{port}"

    def _egress_for(self, name: str, *, ensure: bool = True) -> str:
        """账号出口身份。动态代理带上该号自己的 sticky session。

        ensure=False 只读模式（不补节点/不探活/不换 session）：
        那些是同步网络请求（提取接口 30s 超时 + 每节点 8s 探活×5 轮），
        在事件循环上执行会冻结整个服务（面板与 /v1 全部无响应）。
        list_accounts 的展示字段必须传 ensure=False。
        """
        return proxy_store.egress_key_for(name, ensure=ensure)

    def _egress_blocked(self, name: str) -> bool:
        """同出口隔离：该出口是否已被其它 in-flight 号占用，或已被标记为异常。"""
        egress = self._egress_for(name)
        engine = self._engine
        if engine and engine.egress_is_unhealthy(egress):
            return True
        if not config.ISOLATE_SHARED_EGRESS:
            return False
        for other in self.accounts:
            if other == name:
                continue
            if self._egress_for(other) != egress:
                continue
            lock = self._locks.get(other)
            if lock and lock.locked():
                return True
        return False

    def _sorted_accounts(self) -> list:
        """钉住账号优先，其次按 weight 降序，最后按名称；供派号扫描使用。"""
        rows = self.list_accounts()
        preferred = self._conn.execute(
            "SELECT name FROM accounts_meta WHERE preferred=1"
        ).fetchall()
        pref = {r["name"] for r in preferred}
        rows.sort(key=lambda a: (0 if a["name"] in pref else 1, -int(a.get("weight", 1)), a["name"]))
        return rows

    # ===== 选号引擎（移植自 api-pool 的 AccountPool）=====

    def _proxy_url_for(self, name: str, *, ensure: bool = True) -> str:
        """账号实际使用的代理 URL（动态代理会自动带上独立的 sticky session）。

        这条路径只喂 requests（探测登录态 / 纯 API 出片 / 参考图上传），所以用 socks5h：
        把域名交给代理远端解析。socks5:// 会在本机解析 Akamai 域名并挑到代理连不上的地址，
        直接导致参考图上传失败（NewConnectionError）。浏览器路径不走这里，
        见 browser.proxy_kwargs_for() → 它用的仍是 socks5://。

        ensure=False（只读）供事件循环上的路径使用（_sync_engine 构建配置），
        真正发包的 worker 线程里保持默认 ensure=True。
        """
        return proxy_store.requests_proxy_url(name, ensure=ensure)

    def rotate_account_ip(self, name: str, reason: str = "",
                          force: bool = False) -> dict:
        """给号换一个出口 IP（动态代理换 session；静态代理无操作）。

        force=True 是面板手动操作，不受「最小换IP间隔」限制。
        """
        new_session = proxy_store.rotate_account_session(
            name, reason=reason, force=force)
        if not new_session:
            info = proxy_store.get_account_proxy(name)
            if info and info.get("mode") == "dynamic":
                return {"ok": False, "account": name, "rotated": False,
                        "reason": "距上次轮换不足最小间隔"}
            return {"ok": False, "account": name, "rotated": False,
                    "reason": "该号未绑定动态代理"}
        if self._engine:
            self._engine.set_account_egress_key(name, proxy_store.egress_key_for(name))
        return {"ok": True, "account": name, "rotated": True,
                "session": new_session,
                "proxy_url": proxy_store.proxy_url_for(name)}

    def _quarantine_bad_egress(self, name: str, reason: str = "",
                               force: bool = True) -> bool:
        """把某号当前绑定的出口节点判废并换一个新出口；返回是否真的换了。

        用于「该节点确定不可用」的场景——典型是 TLS 证书校验失败（出口被中间人劫持，
        见 proxy_store._probe_node）。不换的话号会一直挂在坏节点上，每次都失败。
        force 默认为 True：已经确认节点坏了，不该再被「最小换IP间隔」挡住。
        """
        try:
            sess = proxy_store.get_session(name)
            if sess and sess.get("node_id"):
                proxy_store.mark_node_dead(
                    int(sess["node_id"]), (reason or "出口节点不可用")[:120])
        except Exception:  # noqa: BLE001
            pass
        try:
            result = self.rotate_account_ip(name, reason=reason, force=force)
        except Exception:  # noqa: BLE001
            return False
        return bool(result.get("rotated"))

    def _on_egress_unhealthy(self, egress: str, reason: str) -> None:
        """某个出口被上游判异常时：动态代理的号直接换 IP，而不是干等冷却。

        静态代理没有可换的出口，保持原有冷却行为。
        """
        for name in self.accounts:
            try:
                if proxy_store.egress_key_for(name) != egress:
                    continue
            except Exception:  # noqa: BLE001
                continue
            # 提取型：把当前节点标记废弃，否则轮换后可能又挑回同一个坏节点
            marked_dead = False
            try:
                sess = proxy_store.get_session(name)
                if sess and sess.get("node_id"):
                    proxy_store.mark_node_dead(
                        int(sess["node_id"]), reason or "egress unhealthy")
                    marked_dead = True
            except Exception:  # noqa: BLE001
                pass
            try:
                # 刚把这个节点判废，就必须换走 —— 否则号被钉死在一个已知坏节点上，
                # 还要等「最小换IP间隔」才动。这里 force 只用于「已判定为坏」的确定场景，
                # 自动轮换的防刷间隔对其它情况照旧生效。
                result = self.rotate_account_ip(name, reason=reason,
                                                force=marked_dead)
            except Exception:  # noqa: BLE001
                continue
            if result.get("rotated"):
                logger.info("出口 %s 异常，已为 %s 换出口 IP（%s）", egress, name, reason)

    def _pure_state(self, name: str) -> str | None:
        """cookie 账号的纯 API 状态文件；不存在则返回 None（走浏览器出片）。"""
        if not config.PURE_API_ENABLED:
            return None
        p = Path(self.accounts_dir) / name / "cookie_state.json"
        return str(p) if p.is_file() else None

    def _is_cookie_source(self, name: str) -> bool:
        meta = self._meta(name)
        return bool(meta and meta["source"] == "cookie")

    def is_pure_account(self, name: str) -> bool:
        """这个号能不能走纯 API（cookie 来源 + 有 cookie_state.json）。

        服务重启后 `_resume_task` 用它决定走纯 API 重新受理，而不是调浏览器 resume
        （本机没有可用的 Chromium，调了只会报 driver 错误）。
        """
        return self._pure_state(name) is not None and self._is_cookie_source(name)

    async def _generate_effective(self, account, prompt, ratio, duration, model, *,
                                  on_conversation_id, on_poll, on_balance,
                                  reference_image_paths):
        """cookie 账号走纯 API；失败直接抛出真实原因（不再回退浏览器）。

        浏览器 profile 对 cookie 号不可靠（导入的浏览器会话常显示已登出），
        所以 cookie 号只用纯 API；login 号走浏览器出片。
        """
        pure_state = self._pure_state(account)
        # 浏览器通道（hook30 把页面请求的 duration 改写成目标时长，页面默认档计价
        # = 一单 2 点）。路由矩阵（2026-10-07 拍板）：
        #   - 30s：DOLA_30S_CHANNEL=browser → 浏览器；=pure → 纯 API
        #     （2026-10-07 实测纯 API 30s 上游回执也是 2 点且稳定，故当前 pure）。
        #   - 4~29s（含 15s）：DOLA_BROWSER_SHORTS=1 → 浏览器（避开纯 API 短时长被
        #     上游按真实档报价拒单：15s 报 12 点 > 免费号 4 点，实测连环换号烧全池）。
        #     =0 → 纯 API（会被拒单，仅用于回归对比）。
        # 参考图上传复用 attach_reference_images，两条通道都支持。
        dur_n = int(duration or 0)
        model_n = (model or "").lower().replace("_", "-")
        use_browser30 = (self._is_cookie_source(account) and "2.5" in model_n
                         and 4 <= dur_n <= 30
                         and (config.USE_BROWSER_30S
                              or (config.BROWSER_SHORTS and dur_n < 30)))
        if use_browser30:
            if pure_state is None:
                raise PureApiError(f"{account} 缺少 cookie 状态文件，浏览器出片通道不可用")
            result = await generate_video_30(
                account, prompt, model=model, cookie_state_path=pure_state,
                # 浏览器路径必须用 socks5://（proxy_fwd 的转发器只认这个 scheme），
                # 不能用 requests_proxy_url() 的 socks5h:// —— 那条路径只喂 requests。
                proxy_url=proxy_store.proxy_url_for(account),
                on_poll=on_poll, max_wait=config.BROWSER_30S_MAX_WAIT,
                target_duration=dur_n,
                reference_image_paths=reference_image_paths or None)
            return await self._web_playable(result)
        if pure_state is not None and self._is_cookie_source(account):
            # cookie 号：纯 API 出片；失败抛真实错误，不再回退浏览器
            result = await asyncio.to_thread(
                generate_pure_video,
                account=account, prompt=prompt, ratio=ratio, duration=duration,
                model=model, cookie_state_path=pure_state,
                proxy=self._proxy_url_for(account),
                on_conversation_id=on_conversation_id, on_poll=on_poll,
                on_balance=on_balance,
                reference_image_paths=reference_image_paths,
            )
        else:
            # login 账号走浏览器出片
            result = await generate_video(
                account, prompt, ratio, duration, model=model,
                on_conversation_id=on_conversation_id, on_poll=on_poll,
                on_balance=on_balance, reference_image_paths=reference_image_paths)
        return await self._web_playable(result)

    async def _web_playable(self, result: dict) -> dict:
        """上游成片是 HEVC，转成 H.264 后浏览器（画布 <video>）才能直接预览。

        转码是同步 subprocess（libx264 全量重编码，几秒到几十秒）。老实现直接在
        事件循环上跑，高并发时每完成一条视频就把整个进程卡住几秒 —— 轮询、
        管理接口、/health 全停，健康看门狗甚至会判死重启。必须放线程池。
        """
        local = (result or {}).get("local_path")
        if local and Path(local).exists():
            result["local_path"] = str(await asyncio.to_thread(ensure_web_playable, local))
        return result

    async def test_generate(self, account: str, prompt: str, ratio: str | None = None,
                            duration: int = 10, model: str = "seedance-2.0",
                            on_poll=None,
                            reference_image_paths: list | None = None) -> dict:
        """账号级测试出片：强制用指定账号生成一条，返回成功/失败与成品 URL。

        不经过号池选号调度（跳过 _schedulable / 每日上限等），只验证该号本身能否出片。
        仍走该号的代理与 profile（cookie 号走纯 API，login 号走浏览器）。

        on_poll：进度心跳回调（透传给底层轮询）。测试生成动辄 15~30 分钟，
        不刷 last_poll_at 的话，看门狗会把活着的测试任务误判成「孤儿」重派，
        造成同一任务双跑 + 僵尸测试协程回写覆盖重派结果（视频已出但状态显示失败）。

        reference_image_paths：参考图本地路径（测试生成面板上传/下载后的落盘文件）。
        两条出片通道都支持参考图，这里原样透传给 _generate_effective。
        """
        if account not in self.accounts:
            raise FileNotFoundError(f"profile 不存在: {account}")
        lock = self._locks.setdefault(account, asyncio.Lock())
        async with lock:
            cost = self._duration_cost(model, duration)

            def on_balance(balance, source=""):
                self._set_credit_balance(account, balance, source)

            try:
                # 2026-10-15 改造：710022002 现在归类为 window（全局窗口关），不再走这里的
                # 「原地等待重交」——窗口排队已由 pure_api_gen 内部的窗口闸门完成。
                # 这里只保留对【真正的瞬时抖动】类文案的重交。
                result = None
                for attempt in range(config.TESTGEN_TRANSIENT_RETRIES + 1):
                    try:
                        result = await self._generate_effective(
                            account, prompt, ratio, duration, model,
                            on_conversation_id=None, on_poll=on_poll, on_balance=on_balance,
                            reference_image_paths=list(reference_image_paths or []),
                        )
                        break
                    except Exception as exc:
                        if (classify_upstream_failure(str(exc)) == "transient"
                                and attempt < config.TESTGEN_TRANSIENT_RETRIES):
                            wait = config.TESTGEN_TRANSIENT_WAIT_SEC
                            print(f"[test-gen] {account} 提交被上游拒绝（访问频繁），"
                                  f"{wait}s 后重交（第 {attempt + 1}/{config.TESTGEN_TRANSIENT_RETRIES} 次重试）"
                                  f": {str(exc)[:160]}", flush=True)
                            await asyncio.sleep(wait)
                            continue
                        raise
                local = result.get("local_path")
                if not local or not Path(local).exists():
                    raise RuntimeError("出片未产出本地视频文件")
                self._claim_for(account, cost, result)
                self._conn.execute(
                    "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                    (time.time(), account))
                self._conn.commit()
                self._clear_risk(account)
                return {
                    "ok": True,
                    "account": account,
                    "local_path": str(local),
                    "video_url": f"{config.PUBLIC_BASE}/videos/{Path(local).name}",
                }
            except TimeoutError as exc:
                return {"ok": False, "account": account, "error": f"生成超时: {exc}"}
            except WindowClosedError as exc:
                # 测试生成钉死指定账号：窗口关没法靠等换号解决，窗口嗅探器已在内部排队等过，
                # 如实回报「等待生成窗口超时」。
                return {"ok": False, "account": account, "error": f"等待生成窗口超时: {exc}"}
            except ShortClipError as exc:
                # 只给了短视频（例如 30 秒只回 15 秒）：不拼接、也不把这个号标成失败。
                return {"ok": False, "account": account, "error": str(exc)[:300]}
            except LoginExpiredError as exc:
                self._set_risk(account, str(exc))
                return {"ok": False, "account": account, "error": f"登录态失效: {exc}"}
            except FileNotFoundError as exc:
                return {"ok": False, "account": account, "error": f"profile 缺失: {exc}"}
            except Exception as exc:
                if "登录态失效" in str(exc):
                    self._set_risk(account, str(exc))
                else:
                    # 测试生成同样要按上游文案落状态：瞬时限流只短冷却，不当成永久失败。
                    self._note_upstream_failure(account, str(exc))
                return {"ok": False, "account": account, "error": str(exc)[:300]}

    def _meta_weight(self, name: str) -> int:
        m = self._meta(name)
        return int(m["weight"]) if m and m["weight"] else 1

    def _sync_engine(self) -> _PoolEngine:
        """从 accounts + proxy_store + accounts_meta 重建/刷新共享选号引擎。

        加模块锁：generate_video 里改为在线程池中调用后，可能出现并发重建；
        重建本身是「整体替换 self._engine」，串行化避免两线程互相覆盖。
        """
        with _SYNC_ENGINE_LOCK:
            return self._sync_engine_locked()

    def _sync_engine_locked(self) -> _PoolEngine:
        now = time.time()
        cfg_list = []
        for name in self.accounts:
            cfg_list.append(_PoolAccountConfig(
                id=name,
                state_file=Path(self.accounts_dir) / name / "state.json",
                profile_dir=str(Path(self.accounts_dir) / name),
                # ensure=False：_sync_engine 会在事件循环上被调到（generate_video/
                # engine_snapshot），构建配置阶段绝不触发补节点/探活——否则一次
                # 重建能把整个服务卡住几十秒。真发包时 worker 线程会再全量 ensure。
                proxy=self._proxy_url_for(name, ensure=False),
                # 同上：egress_key 也必须只读。ensure=True 会走 _ensure_node →
                # 提取接口（30s 超时）+ 节点探活（8s×5），在事件循环上直接冻结服务
                egress_key=self._egress_for(name, ensure=False),
                enabled=True,
                weight=self._meta_weight(name),
            ))
        engine = _PoolEngine(
            cfg_list,
            daily_success_limit=DAILY_LIMIT,
            max_open_accounts=config.MAX_OPEN_ACCOUNTS,
            isolate_shared_egress=config.ISOLATE_SHARED_EGRESS,
            fail_score_cap=config.FAIL_SCORE_CAP,
            min_submit_interval_seconds=config.MIN_SUBMIT_INTERVAL_SECONDS,
            probe_interval_seconds=config.PROBE_INTERVAL_SECONDS,
        )
        # 出口被判异常时给动态代理换 IP（静态代理走原冷却逻辑）
        engine.on_egress_unhealthy = self._on_egress_unhealthy
        for name in self.accounts:
            st = engine.states.get(name)
            m = self._meta(name)
            if not st:
                continue
            st.successes_today = self.used_today(name)
            if m and m["preferred"]:
                engine.set_preferred(name)
            if m:
                if m["cooldown_until"] and m["cooldown_until"] > now:
                    st.status = "cooldown"
                    st.cooldown_until = m["cooldown_until"]
                if m["rate_limited_until"] and m["rate_limited_until"] > now:
                    st.status = "cooldown"
                    st.cooldown_until = max(st.cooldown_until, m["rate_limited_until"])
                if m["quota_blocked_until"] and m["quota_blocked_until"] > now:
                    st.status = "cooldown"
                    st.cooldown_until = max(st.cooldown_until, m["quota_blocked_until"])
                if m["login_ok"] == 0 and m["login_checked_at"]:
                    st.status = "expired"
                elif m["login_ok"] == 1:
                    st.status = "healthy"
                elif m["login_ok"] is None:
                    st.status = "standby"
                if m["failed_at"] and m["failed_at"] > 0:
                    st.consecutive_failures = max(st.consecutive_failures, 1)
        self._engine = engine
        return engine

    def preview_route(self) -> dict:
        return self._sync_engine().preview_route()

    def set_preferred(self, name: str | None) -> None:
        self._conn.execute(
            "UPDATE accounts_meta SET preferred=? WHERE name=?",
            (1 if name else 0, name or ""),
        )
        if name:
            self._conn.execute(
                "UPDATE accounts_meta SET preferred=0 WHERE name<>?", (name,)
            )
        self._conn.commit()
        engine = self._sync_engine()
        engine.set_preferred(name)

    def set_weight(self, name: str, weight: int) -> None:
        self._conn.execute(
            "UPDATE accounts_meta SET weight=? WHERE name=?", (max(1, int(weight)), name)
        )
        self._conn.commit()

    def mark_egress_unhealthy(self, egress: str, reason: str = "", seconds: int = 120):
        engine = self._sync_engine()
        engine.mark_egress_unhealthy(egress, reason, seconds)
        return engine.isolation_report()

    def rebalance_proxy_bindings(self) -> dict:
        from pool import ProxyBindError
        proxy_ids = [p["id"] for p in proxy_store.list_proxies()
                     if p.get("enabled") is not False]
        if not proxy_ids:
            raise ProxyBindError("proxy_pool_empty")
        busy = {n for n in self.accounts if self._locks.get(n) and self._locks[n].locked()}
        idle = [n for n in self.accounts if n not in busy]
        moved = 0
        for i, name in enumerate(idle):
            pid = proxy_ids[i % len(proxy_ids)]
            if (proxy_store.get_account_proxy(name) or {}).get("id") == pid:
                continue
            proxy_store.set_account_proxy(name, pid)
            moved += 1
        distribution = {pid: 0 for pid in proxy_ids}
        for name in self.accounts:
            info = proxy_store.get_account_proxy(name)
            if info and info["id"] in distribution:
                distribution[info["id"]] += 1
        return {"moved_count": moved, "busy_count": len(busy), "distribution": distribution}

    def bind_proxies(self, proxies: list[str], force: bool = False, mode: str = "sticky") -> dict:
        from pool import ProxyBindError
        if mode != "sticky":
            raise ProxyBindError("unsupported_mode", {"mode": mode})
        if not proxies:
            raise ProxyBindError("proxy_pool_empty")
        ids = [self._ensure_proxy_row(u) for u in proxies]
        needed = len(self.accounts)
        if len(ids) < needed:
            if not force:
                raise ProxyBindError(
                    "proxy_pool_short",
                    {"needed": needed, "got": len(ids), "short": needed - len(ids)},
                )
            ids = (ids * ((needed // len(ids)) + 1))[:needed]
        bound = []
        skipped = []
        for i, name in enumerate(self.accounts):
            if self._locks.get(name) and self._locks[name].locked():
                skipped.append({"id": name})
                continue
            pid = ids[i]
            proxy_store.set_account_proxy(name, pid)
            bound.append({"id": name, "proxy": proxies[i % len(proxies)]})
        return {"ok": True, "bound": bound, "skipped": skipped}

    @staticmethod
    def _ensure_proxy_row(url: str) -> str:
        from urllib.parse import unquote, urlsplit
        if "://" not in (url or ""):
            url = "http://" + url
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        user = unquote(parts.username or "")
        pwd = unquote(parts.password or "")
        for p in proxy_store.list_proxies():
            if p["host"] == host and p["port"] == port:
                return p["id"]
        name = f"auto-{host}-{port}"
        try:
            return proxy_store.create_proxy(name, parts.scheme, host, port, user, pwd)["id"]
        except ValueError:
            for p in proxy_store.list_proxies():
                if p["host"] == host and p["port"] == port:
                    return p["id"]
            raise

    def activate_standby(self, probe_fn, region: str, version: str):
        engine = self._sync_engine()
        activated = engine.activate_standby(probe_fn, region, version)
        if activated:
            self.set_login_status(activated, True)
        return activated

    def probe_all(self, probe_fn, region: str, version: str) -> list:
        engine = self._sync_engine()
        results = engine.probe_all(probe_fn, region, version)
        for account, ok, detail in results:
            self.set_login_status(account, ok)
        return results

    def engine_snapshot(self) -> dict:
        return self._sync_engine().snapshot_meta()

    async def resume_video(self, account: str, conversation_id: str, timeout: int,
                           on_poll=None, duration: int | None = None,
                           ratio: str | None = None, cost: int = 1) -> dict:
        """恢复已受理会话；不参与选号，也不因账号当前额度状态跳过。"""
        async with self.semaphore:
            lock = self._locks.setdefault(account, asyncio.Lock())
            async with lock:
                def on_balance(balance, source=""):
                    self._set_credit_balance(account, balance, source)
                try:
                    result = await self._web_playable(await resume_video(
                        account, conversation_id, timeout,
                        on_poll=on_poll, on_balance=on_balance,
                        duration=duration, ratio=ratio))
                    self._claim_for(account, cost, result)
                    self._conn.execute(
                        "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                        (time.time(), account))
                    self._conn.commit()
                    return result
                except TimeoutError:
                    raise
                except LoginExpiredError as e:
                    print(f"[pool] {account} 登录态失效，标记需重新登录: {e}", flush=True)
                    self._set_risk(account, str(e))
                    self._conn.execute(
                        "UPDATE accounts_meta SET login_ok=0, login_checked_at=? WHERE name=?",
                        (time.time(), account))
                    self._conn.commit()
                    raise

    async def generate_video(self, prompt: str, ratio: str = None, duration: int = None,
                             model: str = "seedance_v2.0", on_conversation_id=None,
                             on_poll=None, on_balance=None,
                             reference_image_paths: list[str] | None = None,
                             on_account_try=None, max_attempts: int = 3,
                             deadline: float | None = None) -> dict:
        """挑一个可调度且空闲的号出片；失败自动换号重试（最多 max_attempts 次）。

        - 按模型分流：seedance-2.0（3 点）只从【满额】组取号；seedance-2.5（2 点）
          先取【半额】、半额空了再用【满额】兜底；冷却/风控/待激活/异常 一律不派发。
        - 2.0 撞上「满额暂时用完」→ 排队等待（DOLA_V20_WAIT_SECONDS），超时才报错。
        - 失败换号继续（额度不足→冷却、每日上限→冷却、内容拒稿→只记备注、
          普通失败→连续 3 次进【异常】组）；2.1.0 P3 起不再有「叠加失败/全池重置」。
        - TimeoutError（已拿到 conversation_id 后轮询超时）→ 不换号直接失败：
          Dola 端可能仍在生成，换号重提会重复扣额度/重复出片。
        - max_attempts <= 0 表示不限次数：在 deadline（任务总时限）之前一轮轮换号重试，
          到点仍未出片才判失败。
        """
        async with self.semaphore:
            # 放线程池：_sync_engine 会读全部账号的 state.json + sqlite 元数据，
            # 同步跑在事件循环上时，启动恢复/提交高峰会把 health 和所有接口一起拖住。
            await asyncio.to_thread(self._sync_engine)
            cost = self._duration_cost(model, duration)
            allowed_groups = self._allowed_quota_groups(model)
            last_err = None
            # 供外层（server 的兜底超时）读取：中断时正在哪个号、最后一次失败原因
            self._last_attempt_err = None
            self._current_account = None
            attempt = 0
            tried: set[str] = set()
            tried_order: list[str] = []
            wait_deadline = 0.0
            group_deadline = 0.0
            group_wait = False
            # max_attempts <= 0：不限次数，只要没到 deadline 就一轮轮换号重试。
            unlimited = max_attempts <= 0

            def attempts_left() -> bool:
                return unlimited or attempt < max_attempts

            def time_left() -> bool:
                return deadline is None or time.time() < deadline

            while attempts_left() and time_left():
                picked = False
                waiting_possible = False
                group_wait = False
                for a in self._ordered_candidates(allowed_groups):
                    if a["name"] in tried or self._recently_failed(a["name"]):
                        continue
                    # 冷却/风控/待激活/异常一律不派发
                    if a.get("group") in self.BLOCKED_GROUPS:
                        continue
                    # 额度组门槛：2.0 只吃满额；2.5 半额优先、满额兜底
                    if self._quota_group(a) not in allowed_groups:
                        group_wait = True
                        continue
                    if not self._schedulable(a) or a["remaining"] < cost:
                        continue
                    if config.ISOLATE_SHARED_EGRESS and self._egress_blocked(a["name"]):
                        continue
                    account = a["name"]
                    lock = self._locks.setdefault(account, asyncio.Lock())
                    # 并发任务跳过已占用账号，避免多个请求排队到同一个 profile。
                    if lock.locked():
                        waiting_possible = True
                        continue
                    async with lock:
                        # 必须给 next() 兜底：候选列表是「进 for 之前的一次快照」，而
                        # `self.accounts` 是实时扫目录的属性 —— 面板删号（shutil.rmtree
                        # accounts/<name>）会把快照里后面的号抹掉。等锁/探测期间账号消失时，
                        # 无默认值的 next() 抛 StopIteration，从协程里逃逸后被 asyncio 转成
                        # `RuntimeError: coroutine raised StopIteration`，整个任务直接判死。
                        row = next((x for x in self.list_accounts()
                                    if x["name"] == account), None)
                        if row is None or not self._schedulable(row):
                            continue  # 等待期间账号被删除 / 状态变化
                        # 派发前先发一句「你好」：被登出/没回复 → 该号进【风控】组，换下一个号。
                        probe = await self.probe_hello_async(account)
                        if not probe.get("ok"):
                            print(f"[pool] {account} 你好探测未通过（{probe.get('reason')}）"
                                  f"（{probe.get('status')}）→ 本次跳过该号"
                                  + ("，已进【风控】组" if probe.get("status") in ("logged_out", "no_reply")
                                     else "（软失败，不判风控）"), flush=True)
                            tried.add(account)
                            tried_order.append(account)
                            self._mark_attempt_failed(account)
                            continue
                        # 探测和出片是同一条上游提交通道：真探过的话留出间隔再提交，
                        # 否则容易被上游判「访问频繁」(710022002)。
                        gap = config.HELLO_PROBE_SUBMIT_GAP_SECONDS
                        if gap > 0 and probe.get("probed"):
                            await asyncio.sleep(gap)
                        picked = True
                        wait_deadline = 0.0
                        group_deadline = 0.0
                        attempt += 1
                        self._current_account = account
                        if on_account_try:
                            on_account_try(account, attempt)
                        try:
                            def on_balance(balance, source=""):
                                self._set_credit_balance(account, balance, source)

                            result = await self._generate_effective(
                                account, prompt, ratio, duration, model=model,
                                on_conversation_id=on_conversation_id, on_poll=on_poll,
                                on_balance=on_balance,
                                reference_image_paths=reference_image_paths)
                            self._claim_for(account, cost, result)
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            self._fail_streak.pop(account, None)   # 出片成功 → 连续失败清零
                            return result
                        except CreditInsufficientError as e:
                            print(f"[pool] {account} 生成前积分不足，跳过: {e}", flush=True)
                            self._mark_quota_blocked(account, str(e))
                            last_err = e; self._last_attempt_err = e
                        except ShortClipError as e:
                            # 30 秒只要原生成片：上游这次只给了短视频就换号重试，不做两段拼接，
                            # 也不把号标成失败（短视频是上游行为，不是这个号的错）。
                            print(f"[pool] {account} 只出了短视频，不拼接，换号重试: {e}", flush=True)
                            last_err = e; self._last_attempt_err = e
                        except AbnormalNoAckError as e:
                            # 派发后 5 分钟一句回执都没有：这是账号侧异常 → 进【异常】组，换号重试
                            print(f"[pool] {account} 派发后无任何回执 → 进【异常】组，换号: {e}", flush=True)
                            self._mark_abnormal(account, str(e))
                            last_err = e; self._last_attempt_err = e
                        except AccountLimitedError as e:
                            print(f"[pool] {account} 达到每日上限，立即换号: {e}", flush=True)
                            self._mark_daily_limit(account, str(e))
                            last_err = e; self._last_attempt_err = e
                        except CreditError as e:
                            print(f"[pool] {account} 额度不足，换号: {e}", flush=True)
                            # 额度不足 = 本次不可用，按额度口径落到冷却组（额度刷新时恢复）
                            self._mark_quota_blocked(account, f"额度不足: {e}")
                            last_err = e; self._last_attempt_err = e
                        except RiskControlError as e:
                            print(f"[pool] {account} 风控，冷却 30 分钟，换号: {e}", flush=True)
                            self._conn.execute(
                                "UPDATE accounts_meta SET cooldown_until=? WHERE name=?",
                                (time.time() + COOLDOWN_SEC, account))
                            self._conn.commit()
                            last_err = e; self._last_attempt_err = e
                        except TimeoutError as e:
                            # 请求已拿到 conversation_id 后仍可能在 Dola 端继续生成。
                            # 未成功出片不扣点（2026-09-02 规则）；也绝不能换号重提
                            # （Dola 端可能仍在生成，会重复出片）。
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            self._last_attempt_err = e
                            raise
                        except LoginExpiredError as e:
                            print(f"[pool] {account} 登录态失效，标记需重新登录并换号: {e}", flush=True)
                            self._set_risk(account, str(e))
                            self._conn.execute(
                                "UPDATE accounts_meta SET login_ok=0, login_checked_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            last_err = e; self._last_attempt_err = e
                        except FileNotFoundError as e:
                            print(f"[pool] {account} profile 缺失，跳过: {e}", flush=True)
                            # profile 缺失不能一直重试：进【异常】组，到期自动恢复
                            self._mark_abnormal(account, f"profile 缺失: {e}")
                            last_err = e; self._last_attempt_err = e
                        except WindowClosedError as e:
                            # 710022002＝全局窗口关。换号毫无意义（全池同一个窗口），
                            # 也不能把这号标失败。窗口排队已由 pure_api_gen 内部完成
                            # （gate.wait_for_window 循环），这里等超时了才冒出来 →
                            # 如实上报「等待窗口超时」，不再烧其它号各等一遍。
                            print(f"[pool] {account} 等待全局生成窗口超时，交回窗口嗅探器排队: {e}",
                                  flush=True)
                            self._last_attempt_err = e
                            raise
                        except Exception as e:
                            message = str(e)
                            self._note_upstream_failure(account, message, attempt)
                            last_err = e; self._last_attempt_err = e
                        self._mark_attempt_failed(account)
                        tried.add(account)
                        tried_order.append(account)
                        if not attempts_left() or not time_left():
                            break
                if not picked:
                    # 只剩被其他并发任务占用的账号：有界等待重扫，而不是立刻误判“无可用账号”。
                    if waiting_possible:
                        if wait_deadline == 0:
                            wait_deadline = time.time() + WAIT_FREE_ACCOUNT_SEC
                        if time.time() < wait_deadline:
                            await asyncio.sleep(WAIT_FREE_ACCOUNT_STEP)
                            continue
                    # seedance-2.0 要 3 点，只能用满额号：满额暂时用完时排队等，
                    # 而不是立刻判失败（2.5 会把满额号降到半额，过一会儿才有号空出来）。
                    if group_wait:
                        waited = int(config.V20_WAIT_SECONDS // 60)
                        if (config.V20_WAIT_SECONDS > 0
                                and not (self.all_accounts_limited
                                         or self.all_accounts_quota_blocked)):
                            if group_deadline == 0:
                                group_deadline = time.time() + config.V20_WAIT_SECONDS
                                print(f"[pool] {model} 暂无符合额度要求的账号，先排队等待"
                                     f"（上限 {waited} 分钟）", flush=True)
                            if time.time() < group_deadline:
                                await asyncio.sleep(GROUP_WAIT_STEP)
                                continue
                    # 2.1.0 P3：叠加失败 / 全池重置 概念删除，这里不再有"清标记重跑一轮"。
                    # 有时限且还没到点：这一轮号都试过了，等失败冷却结束后从头再来一轮。
                    # 全池都已达上限/积分不足时没有等待意义，直接走下方的 429 报错。
                    if (deadline is not None and unlimited and time_left() and tried_order
                            and not self.all_accounts_limited and not self.all_accounts_quota_blocked):
                        tried.clear()
                        wait_deadline = 0.0
                        await asyncio.sleep(min(10.0, max(1.0, deadline - time.time())))
                        continue
                    break
            if self.all_accounts_quota_blocked:
                raise AllAccountsQuotaBlockedError(
                    f"429: 所有已开启调度的账号均已知积分不足: {last_err or '无号'}"
                )
            if self.all_accounts_limited:
                raise AllAccountsLimitedError(
                    f"429: 所有已开启调度的账号均已达到 Dola 每日视频上限: {last_err or '无号'}"
                )
            if group_wait:
                # 池子没到「全池上限/积分不足」，但符合本次模型额度要求的号一个都没有
                waited = int(config.V20_WAIT_SECONDS // 60)
                need = (f"【{GROUP_FULL}】账号（每条 {cost} 点）" if cost >= 3
                        else f"剩余 ≥ {cost} 点的账号（每条 {cost} 点）")
                raise AllAccountsGroupEmptyError(
                    f"{f'排队 {waited} 分钟仍' if waited and config.V20_WAIT_SECONDS > 0 else ''}"
                    f"没有可用的{need}，{model} 暂时无法开工，请稍后重试"
                )
            if last_err is not None:
                chain = "、".join(tried_order) if tried_order else str(last_err)
                timed_out = deadline is not None and time.time() >= deadline
                prefix = "超过任务时限仍未生成成功，" if timed_out else ""
                raise RuntimeError(
                    # 不在这里截断：上游拒稿原文（审核/肖像/侵权）本身可能上百字，
                    # 截成 300 字会把原因切掉。长度上限由 server 落库时统一处理。
                    f"{prefix}连续 {attempt} 次生成均失败（依次尝试账号: {chain}）: {str(last_err)[:1500]}"
                )
            raise RuntimeError(f"号池无可用账号（调度关闭/冷却/额度用完）: {last_err or '无号'}")
