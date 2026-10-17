# -*- coding: utf-8 -*-
"""豆包域（www.doubao.com）账号池与出片调度
================================================================
与 dola 号池（`browser_pool.BrowserPool`）**平行**的第二条通道：

  · dola 账号：`accounts/<name>/`（浏览器 profile），出片走 www.dola.com
  · 豆包账号：`doubao_accounts/<name>/`（纯 Cookie），出片走 www.doubao.com

两者账号载体、额度模型、风控形态都不同，因此**不混进一个类**，由
`server.py` 按 `channel` 分流（channel=dola | doubao）。

账号目录结构
----------------------------------------------------------------
    doubao_accounts/
        <name>/
            ck.txt       完整 Cookie 头（一行）
            meta.json    {email, mobile, nickname, source, created_at, note, ...}

`source` 取值：`qr_doubao`（豆包 App 扫码）/ `qr_douyin`（抖音 App 扫码）
             / `sms`（手机号验证码）/ `cookie`（手工粘贴 CK）。

额度与冷却
----------------------------------------------------------------
豆包域实测：**非会员也有每日免费额度**（见 `豆包域出片接口排查结论.md` §8.2）。
单号每日按 `DOUBAO_DAILY_LIMIT`（默认 1）条记账，用满进【冷却】组等跨日重置。
风控（710022002/710022004）与登录失效 → 冷却 `DOUBAO_COOLDOWN_SECONDS`（默认 1800s）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path

import config
from doubao_cn_video import (
    CN_DOMAIN,
    DoubaoButtonConfirmError,
    DoubaoCreditError,
    DoubaoLoginError,
    DoubaoPaidQuotaError,
    DoubaoThrottleError,
    DoubaoVideoClient,
    ck_domain,
    parse_ck,
)

ACCOUNTS_DIR = Path(os.environ.get("DOUBAO_ACCOUNTS_DIR", "doubao_accounts"))
UPLOAD_DIR = Path(os.environ.get("DOUBAO_UPLOAD_DIR", "doubao_uploads"))


def default_proxy() -> str:
    """豆包域出口代理。

    优先 `DOUBAO_PROXY`；留空则探测本机常见 Clash 混合端口（7897 / 7890），
    都没有才回落 `config.PROXY`。豆包域走香港出口即可（实测 7897 可用）。
    """
    env = (os.environ.get("DOUBAO_PROXY") or "").strip()
    if env:
        return env
    for host, port in (("127.0.0.1", 7897), ("127.0.0.1", 7890)):
        try:
            import socket
            with socket.create_connection((host, port), timeout=0.4):
                return f"socks5://{host}:{port}"
        except OSError:
            continue
    return getattr(config, "PROXY", "") or ""

# 豆包网页版模型名（ability_param.model 口径）。
# `_std` 是标准档（`seedance_v2.0_std`），与 `seedance_v2.0`（Fast）画质不同。
MODELS = ("seedance_v2.0_mini", "seedance_v2.0", "seedance_v2.0_std", "seedance_v2.5")
# 面板/接口统一用的对外模型名 → 豆包域模型名。
# 默认映射到 **Fast**（seedance_v2.0），与网页版默认档一致、额度消耗最低；
# 需要标准画质时可显式传 seedance-2.0-std。
MODEL_ALIASES = {
    "seedance-2.0": "seedance_v2.0",
    "seedance-2.0-fast": "seedance_v2.0",
    "seedance-2.0-std": "seedance_v2.0_std",
    "seedance-2.0-mini": "seedance_v2.0_mini",
    "seedance-2.5": "seedance_v2.5",
    "seedance_v2.0": "seedance_v2.0",
    "seedance_v2.0_std": "seedance_v2.0_std",
    "seedance_v2.0_mini": "seedance_v2.0_mini",
    "seedance_v2.5": "seedance_v2.5",
}

# ★ 时长：豆包网页版是**滑动条**，不是固定档位（2026-10-12 从
#   `alice/slot/action_bar_v3/get_item_conf` 真实响应解出）：
#       时长选择器 = {lower_bound:4, upper_bound:N, step_length:1}
#   各模型上限不同：
#       seedance_v2.5      → [4, 30]
#       seedance_v2.0_std  → [4, 15]
#       seedance_v2.0      → [4, 15]
#       seedance_v2.0_mini → [4, 15]
#   即服务端本来就接受 4~30 秒任意整数秒。此前硬编码 (5,10) 是错的
#   （git 上 `extensions/dola30` 的做法正是把 30 秒选项注入前端，
#     再让页面自己发请求 —— 说明服务端接受 30 秒）。
DURATION_RANGE = {
    "seedance_v2.5": (4, 30),
    "seedance_v2.0_std": (4, 15),
    "seedance_v2.0": (4, 15),
    "seedance_v2.0_mini": (4, 15),
}
DURATION_STEP = 1
# 面板/生成设置里展示的常用档位（下发给用户选择；实际支持上面整个区间）
DURATIONS = (5, 10, 15, 30)
# 按模型分的展示档位
MODEL_DURATIONS = {
    "seedance_v2.0": (5, 10, 15),
    "seedance_v2.0_std": (5, 10, 15),
    "seedance_v2.0_mini": (5, 10, 15),
    "seedance_v2.5": (5, 10, 15, 30),
}
# ★ 2026-10-16：比例表加 3:2 / 2:3 / 4:5 / 5:4。
#   **只有 Seedream 图片**原生支持 3:2 系：视频侧的比例档位来自网页版真实配置
#   （extensions/dola30/doubao-skill-pack-response.json 的 ratio.options 只有
#    1:1/3:4/4:3/9:16/16:9/21:9），所以视频用 VIDEO_RATIOS 子集，
#   避免用户"选了 3:2 却出 16:9"（静默不生效比报错更糟）。
RATIOS = ("21:9", "16:9", "3:2", "4:3", "1:1",
          "4:5", "5:4", "3:4", "2:3", "9:16")

# 视频可用比例：不含 3:2 系（上游视频对这几档不生效）
VIDEO_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")

# ---- Seedream 文生图档位（2026-10-16 新增）----
# 对外模型键；正文模型文案与吸附逻辑见 `doubao_image.resolve_image_model`。
IMAGE_MODELS = ("seedream-4.0", "seedream-4.5",
                "seedream-5.0-pro", "seedream-5.0-flash")
IMAGE_RATIOS = RATIOS

DAILY_LIMIT = int(os.environ.get("DOUBAO_DAILY_LIMIT", "1"))
COOLDOWN_SECONDS = int(os.environ.get("DOUBAO_COOLDOWN_SECONDS", "1800"))
# 上游**限流**（710022002「当前服务访问频繁」）的冷却时长。
# ★ 与 COOLDOWN_SECONDS（登录失效/风控，默认 30 分钟）分开：
#   限流是上游对账号/IP 的短时拒绝，打的是对话/出片通道（登录探针照常可用），
#   绝不能按「登录失效」处理，也不该锁半小时。默认 10 分钟，对齐 dola 池的
#   TRANSIENT_COOLDOWN_SEC 口径。
TRANSIENT_COOLDOWN_SEC = int(os.environ.get("DOUBAO_TRANSIENT_COOLDOWN_SEC", "600"))

# ★ 2026-10-14：命中「付费额度确认卡」（今日免费额度已用完）时，把该账号
#   **临时挪出调度**多久。理由：这类卡只有客户端能点，且**每次免费额度耗尽后
#   都会重现**——留在池里只会让后续任务继续撞同一堵墙。挪出后号池会自动换到
#   仍有免费额度的号出片；免费额度按自然日重置，故默认 6 小时。
PAID_QUOTA_COOLDOWN_SEC = int(
    os.environ.get("DOUBAO_PAID_QUOTA_COOLDOWN_SEC", str(6 * 3600)))

NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fa5]{1,32}$")

# 分组名与 dola 池保持一致，面板的分组筛选按钮可直接复用
GROUP_FULL = "满额"
GROUP_COOLING = "冷却"
GROUP_BUSY = "生成中"
GROUP_RISK = "风控"
GROUP_PENDING = "待激活"
GROUP_ABNORMAL = "异常"
GROUP_WARMUP = "预热"
ACTIVE_GROUPS = (GROUP_FULL, GROUP_COOLING, GROUP_BUSY)

# ★ 2026-10-13（真号实测）：新扫码入池的号存在**风控预热期**。
#   证据：db002 扫码入池后 4.3 分钟就发任务 → `/chat/completion` 直接
#   SSE_HEARTBEAT→STREAM_ERROR 710022002（连 SSE_ACK 都没有，会话没建起来）；
#   同一出口 IP 下 13 小时前的 db001 probe=ok ⇒ 不是 IP 问题，是新号被观察。
#   处置：配合 `DOUBAO_WARMUP_SECONDS` 让新号静默一段再参与**自动调度**
#   （避免刚入池即被风控、并把限流冷却时间省下来）。
#
#   ★ 默认 **0 = 关闭**：预热是「锦上添花」的优化，不是必须行为。
#     默认开启会改变既有语义（刚加号不能马上用），也影响夹具测试；
#     需要时显式设置，例如 `DOUBAO_WARMUP_SECONDS=1800`（30 分钟）。
#     无论是否开启，「冷却中」的账号始终不参与调度（既有逻辑）。
#     手动「测试生成」不受预热限制（用户明确意图，且能立刻拿到真实反馈）。
WARMUP_SECONDS = int(os.environ.get("DOUBAO_WARMUP_SECONDS", "0"))

_DAY_FMT = "%Y-%m-%d"


def usage_day(now: float | None = None) -> str:
    """额度日：与 dola 池同一口径（LIMIT_RESET_TZ 的当日）。"""
    try:
        from browser_pool import usage_day as _dola_day
        return _dola_day()
    except Exception:
        return time.strftime(_DAY_FMT, time.localtime(now or time.time()))


def _is_image_rejected(exc: Exception) -> bool:
    """是否为「提示词级拒绝」（上游明确回复了拒绝理由，换号无效）。

    惰性 import：号池模块在无豆包环境时也要能 import（`doubao_image`
    本身依赖 `doubao_cn_video`，缺失时退化成"永不命中"）。
    """
    try:
        from doubao_image import DoubaoImageRejectedError
    except Exception:  # noqa: BLE001
        return False
    return isinstance(exc, DoubaoImageRejectedError)


def resolve_model(model: str | None) -> str:
    """对外模型名 → 豆包域 ability_param.model。

    ★ 顺序要紧：`seedance_v2.0_std` 归一成 `seedancev2.0std` 后也含 "20"，
    若先判 "20" 就会把标准档悄悄降级成 Fast（旧实现的 bug）。所以先判后缀。
    """
    raw = (model or "").strip().lower()
    m = raw.replace("_", "").replace("-", "")
    if "2.5" in m or "25" in m:
        return "seedance_v2.5"
    # 先看变体后缀，再看基础版本
    if "std" in m or "standard" in m:
        return "seedance_v2.0_std"
    if "mini" in m:
        return "seedance_v2.0_mini"
    if "2.0" in m or "20" in m:
        return "seedance_v2.0"
    return "seedance_v2.0"


def duration_range_for(model: str | None) -> tuple[int, int]:
    """返回该模型允许的 (最小, 最大) 时长；未知模型按最保守的 4~15。"""
    return DURATION_RANGE.get(resolve_model(model), (4, 15))


def duration_supported(model: str | None, duration: int) -> bool:
    """时长是否落在该模型的滑条区间内（步长 1）。"""
    lo, hi = duration_range_for(model)
    try:
        d = int(duration)
    except (TypeError, ValueError):
        return False
    return lo <= d <= hi


def durations_for(model: str | None) -> tuple[int, ...]:
    """该模型在面板上展示的档位（过滤掉超出上限的）。"""
    lo, hi = duration_range_for(model)
    out = tuple(d for d in MODEL_DURATIONS.get(resolve_model(model), DURATIONS)
                if lo <= d <= hi)
    return out or (5, 10)


class DoubaoAccountStore:
    """豆包账号的磁盘仓库（无数据库依赖，面板直接读写目录）。

    防「删除复活」：删号时会在**上级目录**写一条墓碑
    `.deleted/<name>`。仍在出片的任务收尾时会回调写 meta/冷却，
    若不做拦截就会把账号目录重新创建出来 —— 面板上「删了又回来」。
    `write_meta` / `save` 见到墓碑就静默丢弃写入；重新添加同名账号
    （`save` 显式调用）会清掉墓碑。
    """

    def __init__(self, root: Path | str = ACCOUNTS_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------- 路径 ----------

    def dir_of(self, name: str) -> Path:
        return self.root / name

    def ck_path(self, name: str) -> Path:
        return self.dir_of(name) / "ck.txt"

    def meta_path(self, name: str) -> Path:
        return self.dir_of(name) / "meta.json"

    def _tomb_dir(self) -> Path:
        return self.root / ".deleted"

    def _tomb(self, name: str) -> Path:
        return self._tomb_dir() / name

    def is_deleted(self, name: str) -> bool:
        return self._tomb(name).exists()

    def clear_tomb(self, name: str) -> None:
        try:
            self._tomb(name).unlink(missing_ok=True)
        except OSError:
            pass

    # ---------- 读写 ----------

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(d.name for d in self.root.iterdir()
                      if d.is_dir() and not d.name.startswith("."))

    def read_ck(self, name: str) -> str:
        p = self.ck_path(name)
        if not p.is_file():
            return ""
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def read_meta(self, name: str) -> dict:
        p = self.meta_path(name)
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def write_meta(self, name: str, meta: dict) -> None:
        """写 meta.json。**已删除的账号拒绝复活**。

        删号之后，仍在出片的协程收尾时会回调进来写冷却/失败原因，
        如果照写就会把目录重新建出来（面板上「删了又回来」）。
        墓碑存在时静默丢弃 —— 调用方无需感知，账号保持已删除状态。
        """
        if self.is_deleted(name):
            return
        d = self.dir_of(name)
        d.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path(name).with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.meta_path(name))

    def patch_meta(self, name: str, **fields) -> dict:
        meta = self.read_meta(name)
        meta.update(fields)
        self.write_meta(name, meta)
        return meta

    def save(self, name: str, ck: str, meta: dict | None = None) -> dict:
        """落盘一个账号（ck.txt + meta.json），返回合并后的 meta。"""
        if not NAME_RE.match(name or ""):
            raise ValueError("账号名不合法（1-32 位字母数字下划线横线或中文）")
        if not (ck or "").strip():
            raise ValueError("Cookie 不能为空")
        jar = parse_ck(ck)
        if not (jar.get("sessionid") or ""):
            raise ValueError("Cookie 里缺少 sessionid（不是豆包域登录态）")
        # 显式重新添加同名账号 → 清掉墓碑（这是「用户确实要它回来」的唯一路径）
        self.clear_tomb(name)
        d = self.dir_of(name)
        d.mkdir(parents=True, exist_ok=True)
        self.ck_path(name).write_text(ck.strip(), encoding="utf-8")
        old = self.read_meta(name)
        sessionid = jar.get("sessionid") or ""
        merged = {
            "created_at": old.get("created_at") or time.time(),
            # ★ 首次入池时间（新号预热期基准）：老号没有该字段 → 不打扰
            "added_at": float(old.get("added_at") or old.get("created_at")
                              or time.time()),
            "updated_at": time.time(),
            "sessionid": sessionid,
            "sessionid_head": sessionid[:16],
            "cookie_count": len(jar),
            "domain": ck_domain(ck),
            "usage_day": old.get("usage_day") or "",
            "used_today": int(old.get("used_today") or 0),
            "cooldown_until": float(old.get("cooldown_until") or 0),
            "last_err": old.get("last_err") or "",
            "login_ok": old.get("login_ok"),
            "verified_at": float(old.get("verified_at") or 0),
            "scheduling": bool(old.get("scheduling", True)),
            "weight": int(old.get("weight") or 1),
            "note": old.get("note") or "",
            "source": old.get("source") or "cookie",
        }
        if meta:
            merged.update({k: v for k, v in meta.items() if v is not None})
        self.write_meta(name, merged)
        return merged

    def delete(self, name: str) -> bool:
        """删除账号目录。返回是否**确实删干净了**。

        ★ 之前用 `rmtree(ignore_errors=True)` 后无条件 return True：
        Windows 上目录里还有被占用的文件（例如该号正在出片/刚出片，mp4 或
        ck.txt 被别的进程持有）时 rmtree 会静默失败，接口却回 `{"ok":true}`，
        账号在面板上「删了但还在」。这里改成删完复查，没删掉就返回 False，
        让调用方给出真实提示。

        ★ 同时写墓碑 `.deleted/<name>`：删号之后仍在出片的协程收尾会回调
        `write_meta`，没有墓碑就会把目录重建出来（「删了又回来」）。
        """
        d = self.dir_of(name)
        if not d.is_dir():
            # 目录已不在，但可能还有在跑的任务会重建它 → 仍要立墓碑
            self._write_tomb(name)
            return False
        self._write_tomb(name)          # 先立墓碑，再删目录
        shutil.rmtree(d, ignore_errors=True)
        if d.exists():
            # 再试一次（占用往往是瞬时的），仍失败就如实返回 False
            time.sleep(0.3)
            shutil.rmtree(d, ignore_errors=True)
        ok = not d.exists()
        if not ok:
            # 没删干净：撤掉墓碑，否则用户既删不掉也改不动
            self.clear_tomb(name)
        return ok

    def _write_tomb(self, name: str) -> None:
        try:
            t = self._tomb_dir()
            t.mkdir(parents=True, exist_ok=True)
            self._tomb(name).write_text(
                str(time.time()), encoding="utf-8")
        except OSError:
            pass

    def cleanup_tombstones(self, max_age: float = 7 * 86400) -> int:
        """清理过期墓碑（避免 .deleted/ 无限增长）。返回清理条数。"""
        t = self._tomb_dir()
        if not t.is_dir():
            return 0
        n = 0
        now = time.time()
        for p in t.iterdir():
            try:
                if not p.is_file():
                    continue
                if now - p.stat().st_mtime > max_age:
                    p.unlink()
                    n += 1
            except OSError:
                continue
        return n

    def rename(self, name: str, new_name: str) -> None:
        if not NAME_RE.match(new_name or ""):
            raise ValueError("新账号名不合法")
        src, dst = self.dir_of(name), self.dir_of(new_name)
        if not src.is_dir():
            raise FileNotFoundError(f"账号不存在: {name}")
        if dst.exists():
            raise FileExistsError(f"目标账号名已存在: {new_name}")
        src.rename(dst)

    def find_by_sessionid(self, sessionid: str) -> str:
        if not sessionid:
            return ""
        for n in self.names():
            if (self.read_meta(n).get("sessionid") or "") == sessionid:
                return n
        return ""

    # ---------- 记账 ----------

    def roll_day(self, meta: dict) -> dict:
        """跨额度日自动清零用量与冷却。"""
        today = usage_day()
        if (meta.get("usage_day") or "") != today:
            meta["usage_day"] = today
            meta["used_today"] = 0
        return meta

    def charge(self, name: str, cost: int = 1) -> dict:
        meta = self.roll_day(self.read_meta(name))
        meta["used_today"] = int(meta.get("used_today") or 0) + max(1, int(cost))
        meta["updated_at"] = time.time()
        self.write_meta(name, meta)
        return meta

    def cooldown(self, name: str, seconds: int = COOLDOWN_SECONDS, reason: str = "",
                 kind: str = "cooldown") -> dict:
        """进入冷却。kind 区分原因，供面板显示不同提示：
        · "throttled" 上游限流（可重试，短冷却）
        · "login"     登录失效（需重新加号）
        · "cooldown"  其它
        """
        meta = self.roll_day(self.read_meta(name))
        meta["cooldown_until"] = max(float(meta.get("cooldown_until") or 0),
                                     time.time() + int(seconds))
        meta["last_err"] = str(reason)[:200]
        meta["cooldown_kind"] = kind
        meta["updated_at"] = time.time()
        self.write_meta(name, meta)
        return meta

    def reset_all(self) -> int:
        """手动重置全池额度（面板「重置额度」）。"""
        n = 0
        for name in self.names():
            meta = self.read_meta(name)
            meta.update(usage_day=usage_day(), used_today=0, cooldown_until=0.0,
                        last_err="", updated_at=time.time())
            self.write_meta(name, meta)
            n += 1
        return n


class DoubaoPool:
    """豆包号池：账号视图 + 出片调度（与 BrowserPool 同形的最小面）。"""

    def __init__(self, store: DoubaoAccountStore | None = None,
                 proxy: str | None = None, daily_limit: int = DAILY_LIMIT,
                 cooldown_seconds: int = COOLDOWN_SECONDS):
        self.store = store or DoubaoAccountStore()
        self.proxy = proxy if proxy is not None else default_proxy()
        self.daily_limit = int(daily_limit)
        self.cooldown_seconds = int(cooldown_seconds)
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------- 账号发现 ----------

    def reload(self) -> None:
        self.store.root.mkdir(parents=True, exist_ok=True)

    @property
    def accounts(self) -> list[str]:
        return self.store.names()

    def __contains__(self, name: str) -> bool:
        return name in self.store.names()

    def lock_for(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    # ---------- 视图 ----------

    def _client_for(self, name: str, with_image: bool = False):
        ck = self.store.read_ck(name)
        if not ck:
            raise FileNotFoundError(f"豆包账号缺少 ck.txt: {name}")
        if with_image:
            from doubao_image_video import DoubaoImageVideoClient
            return DoubaoImageVideoClient(ck, proxy=self.proxy or None)
        return DoubaoVideoClient(ck, proxy=self.proxy or None)

    def _image_client_for(self, name: str):
        """**文生图**（Seedream）客户端。

        ★ 与 `_client_for(with_image=True)` 不是一回事，别搞混：
          · `with_image=True` = **图生视频**（视频通道 + 参考图上传，ability_type=17）；
          · 本方法 = **文生图**（content_type=2009 + skill 3，结果直接在 SSE 里）。
        """
        ck = self.store.read_ck(name)
        if not ck:
            raise FileNotFoundError(f"豆包账号缺少 ck.txt: {name}")
        from doubao_image import DoubaoImageClient
        return DoubaoImageClient(ck, proxy=self.proxy or None)

    def _warmup_left(self, meta: dict, now: float) -> int:
        """新号预热剩余秒数（0 = 已过预热期或预热被关闭）。

        基准取 `added_at`；老号没有该字段时回落 `created_at`
        （老库账号创建已久 → 自然为 0，不会被误伤；刚入池的新号则照常预热）。
        `DOUBAO_WARMUP_SECONDS=0` 可整机关闭该机制。
        """
        if WARMUP_SECONDS <= 0:
            return 0
        added = float(meta.get("added_at") or meta.get("created_at") or 0)
        if added <= 0:
            return 0
        left = int(added + WARMUP_SECONDS - now)
        return left if left > 0 else 0

    def _group_of(self, meta: dict, now: float, busy: bool) -> tuple[str, str]:
        if meta.get("login_ok") is False:
            return GROUP_RISK, str(meta.get("last_err") or "登录态失效")
        if meta.get("login_ok") is None:
            return GROUP_PENDING, "尚未完成首次验活"
        # ★ 新号预热期（见 WARMUP_SECONDS 注释）：静默等待，不派任务
        warm_left = self._warmup_left(meta, now)
        if warm_left > 0:
            return GROUP_WARMUP, (
                "新号预热中（避免刚入池即被风控，%d 分 %d 秒后自动可调度）"
                % (warm_left // 60, warm_left % 60))
        if busy:
            return GROUP_BUSY, "正在生成"
        cd = float(meta.get("cooldown_until") or 0)
        if cd > now:
            left = int(cd - now)
            wait = f"（{left // 60}分{left % 60}秒后可再用）"
            kind = str(meta.get("cooldown_kind") or "")
            if kind == "throttled":
                # 限流：账号是好的，到点会自动恢复 → 明确说清「不是风控」
                return GROUP_COOLING, f"上游限流（账号正常，到点自动恢复）{wait}"
            if kind == "login":
                return GROUP_COOLING, f"登录态失效{wait}"
            return GROUP_COOLING, (str(meta.get("last_err") or "冷却中") + wait)
        if int(meta.get("used_today") or 0) >= self.daily_limit:
            return GROUP_COOLING, f"今日额度已用满（{self.daily_limit} 条）"
        return GROUP_FULL, f"今日剩余 {max(0, self.daily_limit - int(meta.get('used_today') or 0))} 条"

    def list_accounts(self) -> list[dict]:
        now = time.time()
        out = []
        for name in self.accounts:
            meta = self.store.roll_day(self.store.read_meta(name))
            lock = self._locks.get(name)
            busy = bool(lock and lock.locked())
            group, reason = self._group_of(meta, now, busy)
            used = int(meta.get("used_today") or 0)
            out.append({
                "name": name,
                "channel": "doubao",
                "scheduling": bool(meta.get("scheduling", True)),
                "note": meta.get("note") or "",
                "email": meta.get("email") or "",
                "mobile": meta.get("mobile") or "",
                "nickname": meta.get("nickname") or "",
                "source": meta.get("source") or "cookie",
                "created_at": float(meta.get("created_at") or 0),
                "last_used_at": float(meta.get("updated_at") or 0),
                "login_ok": meta.get("login_ok"),
                "verified_at": float(meta.get("verified_at") or 0),
                "cooldown_until": float(meta.get("cooldown_until") or 0),
                "cooling": float(meta.get("cooldown_until") or 0) > now,
                "cooldown_kind": meta.get("cooldown_kind") or "",
                "cooldown_left": max(0, int(float(meta.get("cooldown_until") or 0) - now)),
                "last_err": meta.get("last_err") or "",
                "weight": int(meta.get("weight") or 1),
                "busy": busy,
                "used_today": used,
                "limit": self.daily_limit,
                "remaining": max(0, self.daily_limit - used),
                "group": group,
                "group_reason": reason,
                "warmup_left": self._warmup_left(meta, now),
                "sessionid_head": meta.get("sessionid_head") or "",
            })
        out.sort(key=lambda x: x["created_at"], reverse=True)
        return out

    def summary(self) -> dict:
        accs = self.list_accounts()
        counts = {g: 0 for g in (GROUP_FULL, GROUP_COOLING, GROUP_BUSY,
                                 GROUP_RISK, GROUP_PENDING, GROUP_ABNORMAL,
                                 GROUP_WARMUP)}
        for a in accs:
            counts[a["group"]] = counts.get(a["group"], 0) + 1
        return {
            "accounts": accs,
            "groups": counts,
            "all_count": len(accs),
            "valid_count": sum(counts[g] for g in ACTIVE_GROUPS),
            "warmup_count": counts.get(GROUP_WARMUP, 0),
            "remaining_quota": sum(a["remaining"] for a in accs
                                   if a["group"] in ACTIVE_GROUPS),
            "daily_limit": self.daily_limit,
            "warmup_seconds": WARMUP_SECONDS,
        }

    @property
    def available(self) -> bool:
        now = time.time()
        for name in self.accounts:
            meta = self.store.roll_day(self.store.read_meta(name))
            if not meta.get("scheduling", True):
                continue
            if meta.get("login_ok") is not True:
                continue
            if float(meta.get("cooldown_until") or 0) > now:
                continue
            if int(meta.get("used_today") or 0) >= self.daily_limit:
                continue
            return True
        return False

    @property
    def all_accounts_quota_blocked(self) -> bool:
        """全池都因额度/冷却不可用（用于对外 429）。"""
        if not self.accounts:
            return False
        now = time.time()
        for name in self.accounts:
            meta = self.store.roll_day(self.store.read_meta(name))
            if (meta.get("login_ok") is True
                    and meta.get("scheduling", True)
                    and float(meta.get("cooldown_until") or 0) <= now
                    and int(meta.get("used_today") or 0) < self.daily_limit):
                return False
        return True

    # ---------- 增删改 ----------

    def add(self, name: str, ck: str, meta: dict | None = None) -> dict:
        return self.store.save(name, ck, meta)

    def delete(self, name: str) -> bool:
        return self.store.delete(name)

    def rename(self, name: str, new_name: str) -> None:
        self.store.rename(name, new_name)

    def set_scheduling(self, name: str, on: bool) -> None:
        self.store.patch_meta(name, scheduling=bool(on), updated_at=time.time())

    def set_note(self, name: str, note: str) -> None:
        self.store.patch_meta(name, note=str(note)[:500], updated_at=time.time())

    def set_weight(self, name: str, weight: int) -> None:
        self.store.patch_meta(name, weight=max(1, int(weight)), updated_at=time.time())

    def reset_all(self) -> int:
        return self.store.reset_all()

    def recover(self, name: str) -> dict:
        """人工恢复（对齐 dola 池 / USAGE.md「号被风控了」的处置流程）。

        - 清冷却（含限流冷却）
        - 把 `login_ok` 放回 None（=【待激活】），要求调用方随后点「验证」
          复核登录态 —— 与 dola 池「风控恢复后回到待激活，再探测一次」一致。
        - 清掉错误原因
        """
        meta = self.store.roll_day(self.store.read_meta(name))
        meta.update(cooldown_until=0.0, cooldown_kind="", last_err="",
                    login_ok=None, updated_at=time.time())
        self.store.write_meta(name, meta)
        return {"ok": True, "account": name, "group": GROUP_PENDING,
                "hint": "已放回【待激活】，请点「验证」复核登录态"}

    def clear_cooldown(self, name: str) -> dict:
        """只清冷却（限流到点前想立刻重试时用），不动登录态。"""
        meta = self.store.roll_day(self.store.read_meta(name))
        meta.update(cooldown_until=0.0, cooldown_kind="", last_err="",
                    updated_at=time.time())
        self.store.write_meta(name, meta)
        return {"ok": True, "account": name}

    def export_ck(self, name: str) -> str:
        return self.store.read_ck(name)

    # ---------- 验活 ----------

    async def probe_throttle(self, name: str) -> dict:
        """发一句纯文本「你好」判**限流**（对齐 USAGE.md 的「你好探测」）。

        为什么用纯文本：
          · 纯文本同样走 `/chat/completion`，**与出片共用同一条限流通道**
          · 但不消耗视频额度、也不建视频会话 —— 最便宜的探针
          · 实测被限流时纯文本也返 710022002；不被限流时正常 SSE_ACK

        只做探测，不改账号状态（是否记冷却由调用方决定）。
        """
        ck = self.store.read_ck(name)
        if not ck:
            return {"ok": False, "status": "no_ck", "reason": "缺少 ck.txt"}
        cli = self._client_for(name)
        try:
            res = await cli.probe_text("你好")
        except Exception as exc:
            return {"ok": False, "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}"[:180]}
        return res

    async def diagnose(self, name: str, *, probe: bool = True) -> dict:
        """诊断该账号当前到底是「登录失效」还是「被限流」。

        实测判据（2026-10-12，四个出口 IP 交叉验证）：
          · 登录探针 `/alice/user/get_account_info` 正常 → 账号是好的
          · 但 `/chat/completion` 返 710022002      → 账号级限流
          · 换出口 IP **无效**（4 个不同住宅 IP 全被拒）→ 限流绑账号，不绑 IP
        所以面板应告诉用户「等」而不是「换 IP」。

        probe=True 时额外发一句纯文本真探限流状态（cooldown 已过期但实际
        仍被限流的情况只有真探才知道 —— 上一版只看元数据会误报「正常」）。
        """
        from doubao_login import check_login

        ck = self.store.read_ck(name)
        if not ck:
            return {"ok": False, "reason": "缺少 ck.txt"}
        meta = self.store.roll_day(self.store.read_meta(name))
        out: dict = {"account": name, "cooldown_left": max(
            0, int(float(meta.get("cooldown_until") or 0) - time.time())),
            "cooldown_kind": meta.get("cooldown_kind") or ""}
        try:
            login_ok, info = await check_login(ck, proxy=self.proxy)
        except Exception as exc:
            out.update(login_ok=False,
                       reason=f"探针异常: {type(exc).__name__}: {exc}"[:160])
            return out
        out["login_ok"] = bool(login_ok)
        out["mobile"] = info.get("mobile") or ""
        out["connects"] = info.get("connects") or []
        if not login_ok:
            out["verdict"] = "login_expired"
            out["advice"] = "登录态已失效 → 重新加号（扫码或手机号）"
            out["throttled"] = None
            return out

        # 真探限流：只有真探才能发现「冷却已过期但仍在限流」的情况
        if probe:
            pr = await self.probe_throttle(name)
            out["throttled"] = pr.get("status") == "throttled"
            out["probe"] = pr
            if out["throttled"]:
                out["verdict"] = "throttled"
                out["advice"] = ("账号正常，但被上游限流（对话与出片共用同一通道）。"
                                 "实测换出口 IP 无效（限流绑账号），只能等冷却；"
                                 "期间该号无法出片。")
                return out

        if out["cooldown_left"] > 0 and out["cooldown_kind"] == "throttled":
            out["verdict"] = "throttled"
            out["advice"] = (f"账号正常，被上游限流；还可重试 "
                             f"{out['cooldown_left'] // 60} 分 "
                             f"{out['cooldown_left'] % 60} 秒。"
                             "实测换出口 IP 无效（限流绑账号），等即可")
        else:
            out["verdict"] = "ok"
            out["advice"] = "账号与登录态都正常，可直接测试生成"
        return out

    async def verify(self, name: str) -> dict:
        from doubao_login import check_login

        ck = self.store.read_ck(name)
        if not ck:
            return {"ok": False, "reason": "缺少 ck.txt"}
        try:
            ok, info = await check_login(ck, proxy=self.proxy)
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
        meta = self.store.roll_day(self.store.read_meta(name))
        meta.update(login_ok=bool(ok), verified_at=time.time(), updated_at=time.time())
        if ok:
            if info.get("mobile"):
                meta["mobile"] = info["mobile"]
            if info.get("nickname"):
                meta["nickname"] = info["nickname"]
            if info.get("user_id"):
                meta["user_id"] = info["user_id"]
            meta["last_err"] = ""
        else:
            meta["last_err"] = str(info.get("error") or "登录态无效")[:200]
        self.store.write_meta(name, meta)
        return {"ok": bool(ok), "info": info,
                "reason": "" if ok else (info.get("error") or "登录态无效")}

    # ---------- 出片 ----------

    def _select(self, exclude: set[str] | None = None) -> list[str]:
        """按权重/创建时间排出可调度账号。"""
        now = time.time()
        exclude = exclude or set()
        cands = []
        for name in self.accounts:
            if name in exclude:
                continue
            meta = self.store.roll_day(self.store.read_meta(name))
            if not meta.get("scheduling", True):
                continue
            if meta.get("login_ok") is not True:
                continue
            # ★ 新号预热期内不派任务（打了也是 710022002，还加重风控）
            if self._warmup_left(meta, now) > 0:
                continue
            if float(meta.get("cooldown_until") or 0) > now:
                continue
            if int(meta.get("used_today") or 0) >= self.daily_limit:
                continue
            cands.append((int(meta.get("weight") or 1),
                          -float(meta.get("updated_at") or 0), name))
        cands.sort(reverse=True)
        return [c[2] for c in cands]

    async def _run_one(self, name: str, prompt: str, ratio: str, duration: int,
                       model: str, image_paths: list[str] | None = None,
                       timeout: int | None = None, on_poll=None) -> dict:
        """单账号出片（参考图走 DoubaoImageVideoClient 上传链路）。

        ★ 时长按**模型各自的滑条区间**校验（从真实 action_bar 配置解出）：
        `seedance_v2.5` 4~30 秒、v2.0 系列 4~15 秒。越界直接报错，
        不要发出去让上游拒（浪费一次额度/冷却）。
        """
        model_cn = resolve_model(model)
        lo, hi = duration_range_for(model_cn)
        try:
            dur = int(duration)
        except (TypeError, ValueError):
            raise ValueError(f"时长非法: {duration!r}") from None
        if not (lo <= dur <= hi):
            raise ValueError(
                f"{model_cn} 支持的时长为 {lo}~{hi} 秒（收到 {dur} 秒）")
        # ★ 2026-10-14：出片等待窗口默认取 DOUBAO_VIDEO_TIMEOUT（900 秒）。
        #   上游对 30 秒片自述「预计等待 15 分钟」，旧的 300 秒必在出片前超时。
        if timeout is None:
            timeout = int(os.environ.get("DOUBAO_VIDEO_TIMEOUT", "900"))
        with_image = bool(image_paths)
        cli = self._client_for(name, with_image=with_image)
        if with_image:
            # 豆包网页版参考图只吃一张（图文混排的 attachment_block 单附件）
            path = image_paths[0]
            url = await cli.generate_video_with_image(
                path, prompt, ratio=ratio, duration=dur, model=model_cn,
                timeout=timeout, on_poll=on_poll)
        else:
            url = await cli.generate_video(prompt, ratio=ratio, duration=dur,
                                           model=model_cn, timeout=timeout,
                                           on_poll=on_poll)
        # ★ 用上游真实会话 ID（SSE_ACK 回填）；`_local_conv_id` 只是 referer 占位。
        conv_id = getattr(cli, "last_conversation_id", "") or ""
        return {"video_url": url, "conversation_id": conv_id, "account": name,
                "duration": dur, "model": model_cn}

    async def _download(self, url: str, name: str) -> str:
        import aiohttp

        Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
        fname = Path(config.DOWNLOAD_DIR) / (
            f"doubao_{name}_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}.mp4")
        proxy = None if getattr(config, "DOWNLOAD_DIRECT", True) else (self.proxy or None)
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=600)) as s:
            kw = {"proxy": proxy} if proxy else {}
            async with s.get(url, **kw) as r:
                r.raise_for_status()
                with open(fname, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 16):
                        f.write(chunk)
        return str(fname)

    # ---------- 文生图（Seedream） ----------

    async def _download_image(self, url: str, name: str) -> str:
        """下载一张生成图到 ``doubao_images/``（扩展名按 URL/内容判断）。"""
        import aiohttp

        ext = ".png"
        low = (url or "").lower()
        for cand in (".jpeg", ".jpg", ".png", ".webp"):
            if cand in low:
                ext = cand
                break
        out_dir = Path(os.environ.get("DOUBAO_IMAGE_DIR", "doubao_images"))
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = out_dir / (
            f"{name}_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}{ext}")
        proxy = None if getattr(config, "DOWNLOAD_DIRECT", True) else (self.proxy or None)
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300)) as s:
            kw = {"proxy": proxy} if proxy else {}
            async with s.get(url, **kw) as r:
                r.raise_for_status()
                with open(fname, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 16):
                        f.write(chunk)
        return str(fname)

    async def _run_one_image(self, name: str, prompt: str, ratio: str,
                             model: str, timeout: int | None = None) -> dict:
        """单账号文生图（Seedream，不走轮询：结果直接在 SSE 里）。"""
        if timeout is None:
            timeout = int(os.environ.get("DOUBAO_IMAGE_TIMEOUT", "300"))
        cli = self._image_client_for(name)
        res = await cli.generate_image(prompt, ratio=ratio or "1:1",
                                       model=model or IMAGE_MODELS[1],
                                       timeout=timeout)
        return {"images": list(res.get("images") or []),
                "conversation_id": res.get("conversation_id", ""),
                "text": res.get("text", ""),
                "account": name,
                "model": model or IMAGE_MODELS[1],
                "ratio": ratio or "1:1"}

    async def generate_image(self, prompt: str, ratio: str | None = None,
                             model: str = "seedream-4.5",
                             on_account_try=None, max_attempts: int = 0,
                             deadline: float | None = None) -> dict:
        """号池出图：挑号 → 失败换号 → 到点抛错。

        与 ``generate_video`` 同一套换号/额度/冷却纪律（这是号池的核心价值），
        差别只在①没有时长参数 ②结果不走轮询。
        """
        if not self.accounts:
            raise RuntimeError("豆包号池为空（doubao_accounts/ 下没有账号）")
        tried: list[str] = []
        errors: list[str] = []
        last_exc: Exception | None = None
        while deadline is None or time.time() < deadline:
            if max_attempts and len(tried) >= max_attempts:
                break
            cands = [n for n in self._select() if n not in tried]
            if not cands:
                if not self._select():
                    break
                if all(n in tried for n in self.accounts):
                    break
                await asyncio.sleep(2)
                continue
            name = cands[0]
            tried.append(name)
            if on_account_try:
                try:
                    on_account_try(name, len(tried))
                except Exception:
                    pass
            try:
                async with self.lock_for(name):
                    res = await self._run_one_image(name, prompt,
                                                    ratio or "1:1", model)
                    locals_: list[str] = []
                    for url in res["images"]:
                        try:
                            locals_.append(await self._download_image(url, name))
                        except Exception:
                            continue
                self.store.charge(name, 1)
                self.store.patch_meta(name, login_ok=True, verified_at=time.time(),
                                      last_err="")
                return {"images": res["images"], "local_paths": locals_,
                        "source_urls": list(res["images"]),
                        "conversation_id": res.get("conversation_id", ""),
                        "text": res.get("text", ""), "account": name,
                        "model": res.get("model", model)}
            except DoubaoCreditError as exc:
                self.store.patch_meta(name, used_today=self.daily_limit,
                                      last_err=str(exc)[:200], updated_at=time.time())
                last_exc = exc
                errors.append(f"{name}: {str(exc)[:80]}")
            except DoubaoLoginError as exc:
                self.store.cooldown(name, self.cooldown_seconds, str(exc))
                self.store.patch_meta(name, login_ok=False)
                last_exc = exc
                errors.append(f"{name}: {str(exc)[:80]}")
            except Exception as exc:  # noqa: BLE001
                # ★ 提示词级拒绝（上游明确回复了拒绝理由）：换号也一样被拒，
                #   直接抛出，别把整池号白跑一遍。按**异常类型**判定，
                #   不靠消息里的关键词（该类型的提示文案自带"风控/拒绝"字样，
                #   字符串匹配会把可重试的空响应也误判成不可重试）。
                if _is_image_rejected(exc):
                    raise
                kind = self._classify_failure(exc)
                msg = f"{type(exc).__name__}: {exc}"
                if kind == "throttled":
                    self.store.cooldown(name, TRANSIENT_COOLDOWN_SEC,
                                        "上游限流: " + msg[:160], kind="throttled")
                elif kind == "credit":
                    self.store.patch_meta(name, used_today=self.daily_limit,
                                          last_err=msg[:200], updated_at=time.time())
                elif kind == "login":
                    self.store.cooldown(name, self.cooldown_seconds, msg, kind="login")
                    self.store.patch_meta(name, login_ok=False)
                elif kind == "paid":
                    self.store.cooldown(
                        name, PAID_QUOTA_COOLDOWN_SEC,
                        "今日免费额度已用完，需客户端点「确认生成」同意付费；"
                        "已临时移出调度 %d 小时（跨日自动恢复）: %s"
                        % (PAID_QUOTA_COOLDOWN_SEC // 3600, msg[:140]),
                        kind="paid")
                else:
                    self.store.patch_meta(name, last_err=msg[:200],
                                          updated_at=time.time())
                last_exc = exc
                errors.append(f"{name}: {str(exc)[:80]}")
        if last_exc is not None:
            raise RuntimeError(
                "豆包域生图失败（已尝试: %s）· %s"
                % ("、".join(tried) or "无", "；".join(errors[-3:]) or str(last_exc)))
        warm = [(n, self._warmup_left(
                    self.store.roll_day(self.store.read_meta(n)), time.time()))
                for n in self.accounts]
        warming = [(n, w) for n, w in warm if w > 0]
        if warming and len(warming) == len(self.accounts):
            soon = min(w for _, w in warming)
            raise RuntimeError(
                "豆包号池全部处于新号预热期（最近一个还需 %d 分 %d 秒）"
                % (soon // 60, soon % 60))
        raise RuntimeError("豆包域没有可用账号（全部冷却/额度用尽/未验活）")

    def _classify_failure(self, exc: Exception) -> str:
        """把出片失败归类：`login` / `throttled` / `credit` / `other`。

        ★ 关键区分（本轮修正的 bug）：`710022002 当前服务访问频繁` 是
        **账号/IP 级限流**，不是登录失效 —— 实测同一时刻登录探针
        `/alice/user/get_account_info` 仍正常返回手机号（限流只打对话/出片通道）。
        旧实现把它当 `DoubaoLoginError` → 标 `login_ok=False` 进「风控」永久组，
        还锁 25 分钟，把好号误杀。

        现在对齐 dola 池的 `classify_upstream_failure` 口径：
          · throttled → 只短冷却（TRANSIENT_COOLDOWN_SEC），**不动 login_ok**
          · login     → 真登录失效（710012000 Invalid User ID / 会话过期）→ 进风控
          · credit    → 当日额度用尽
        """
        msg = f"{type(exc).__name__}: {exc}"
        low = msg.lower()
        if isinstance(exc, DoubaoCreditError):
            return "credit"
        # ★ 限流优先判定：`DoubaoThrottleError` 是明确的限流类型，先于 login 处理
        if isinstance(exc, DoubaoThrottleError):
            return "throttled"
        # 真登录失效：用户无效 / 会话过期（与限流区分开）
        for marker in ("710012000", "invalid user id", "会话过期", "会话已过期",
                       "登录态无效", "user invalid"):
            if marker.lower() in low:
                return "login"
        # ★ 2026-10-14：付费额度确认卡（今日免费额度已用完）——**先于**通用
        #   manual 判定。与「素材授权卡」不同，这类卡每次免费额度耗尽的号都会
        #   重现，换号可解（号池核心价值），故单独归类为 `paid`。
        if isinstance(exc, DoubaoPaidQuotaError) or "免费额度已用完" in msg:
            return "paid"
        # ★ 2026-10-13：按钮确认卡（10103）= 需要人工去 App 点击，
        #   既不是账号问题也不是限流 —— 不冷却、不记账，直接把提示带回面板。
        if "确认生成」按钮卡" in msg or "DoubaoButtonConfirmError" in msg:
            return "manual"
        # ★ 2026-10-14：已建成会话、只是**轮询超时**（实测 30 秒片要等 5~15 分钟）。
        #   上游很可能仍在生成，换号重提只会重复出片 + 重复扣额度，
        #   所以单独归类，让调用方停止换号。
        if "超时未出片" in msg:
            return "timeout"
        # 限流 / 访问频繁：短冷却，绝不标登录失效
        for marker in ("710022002", "710022004", "访问频繁", "限流", "rate limit",
                       "too many requests", "稍后重试", "服务繁忙", "请求过于频繁",
                       "频繁"):
            if marker.lower() in low:
                return "throttled"
        if isinstance(exc, DoubaoLoginError):
            # 兜底：明确是 DoubaoLoginError 但文案没命中上面任何一条 → 按登录失效处理
            return "login"
        return "other"

    async def test_generate(self, name: str, prompt: str, ratio: str | None = None,
                            duration: int = 5, model: str = "seedance-2.0",
                            image_paths: list[str] | None = None,
                            on_poll=None) -> dict:
        """账号级测试生成：钉死指定账号跑一条真实出片。"""
        if name not in self.accounts:
            raise FileNotFoundError(f"豆包账号不存在: {name}")
        ck = self.store.read_ck(name)
        if not ck:
            return {"ok": False, "account": name, "error": "该账号缺少 ck.txt"}
        # ★ 冷却中直接拦掉：上游限流期间再打一次只会加重限流、白等一轮，
        #   而且面板上明明写着「X 分钟可重试」。给出准确的剩余时间让用户等或换号。
        meta_now = self.store.roll_day(self.store.read_meta(name))
        cd_left = int(float(meta_now.get("cooldown_until") or 0) - time.time())
        if cd_left > 0:
            kind = str(meta_now.get("cooldown_kind") or "")
            why = ("上游限流" if kind == "throttled"
                   else "登录态失效" if kind == "login" else "冷却")
            wait = f"{cd_left // 60} 分 {cd_left % 60} 秒"
            hint = ("账号本身正常，等冷却结束即可；或换一个豆包账号测试"
                    if kind == "throttled"
                    else "该账号登录态已失效，请重新加号后再测")
            return {"ok": False, "account": name, "kind": kind,
                    "cooldown_left": cd_left,
                    "error": f"{why}中（还可重试 {wait}）：{hint}"}
        if int(meta_now.get("used_today") or 0) >= self.daily_limit:
            return {"ok": False, "account": name, "kind": "credit",
                    "error": f"今日额度已用满（{self.daily_limit} 条），跨日自动重置"
                             "；或点「重置额度」"}
        async with self.lock_for(name):
            try:
                res = await self._run_one(name, prompt, ratio or "16:9", duration,
                                          model, image_paths=image_paths,
                                          on_poll=on_poll)
            except Exception as exc:
                kind = self._classify_failure(exc)
                msg = f"{type(exc).__name__}: {exc}"
                if kind == "throttled":
                    # 上游限流：只短冷却，**不碰 login_ok**（账号是好的）
                    self.store.cooldown(name, TRANSIENT_COOLDOWN_SEC,
                                        f"上游限流（{TRANSIENT_COOLDOWN_SEC // 60} 分钟可重试）: "
                                        + msg[:160], kind="throttled")
                elif kind == "credit":
                    self.store.patch_meta(name, used_today=self.daily_limit,
                                          last_err=msg[:200], updated_at=time.time())
                elif kind == "login":
                    self.store.cooldown(name, self.cooldown_seconds, msg, kind="login")
                    self.store.patch_meta(name, login_ok=False)
                elif kind == "paid":
                    # ★ 2026-10-14：今日免费额度已用完 → 该号临时挪出调度，
                    #   号池自动换到仍有免费额度的号（免费额度按自然日重置）。
                    self.store.cooldown(
                        name, PAID_QUOTA_COOLDOWN_SEC,
                        "今日免费额度已用完，需客户端点「确认生成」同意付费；"
                        "已临时移出调度 %d 小时（跨日自动恢复）: %s"
                        % (PAID_QUOTA_COOLDOWN_SEC // 3600, msg[:140]),
                        kind="paid")
                else:
                    self.store.patch_meta(name, last_err=msg[:200],
                                          updated_at=time.time())
                return {"ok": False, "account": name, "error": msg, "kind": kind}
            local = ""
            try:
                local = await self._download(res["video_url"], name)
            except Exception as exc:
                return {"ok": False, "account": name,
                        "error": f"出片成功但下载失败: {type(exc).__name__}: {exc}"[:300],
                        "video_url": res["video_url"]}
            self.store.charge(name, 1)
            self.store.patch_meta(name, login_ok=True, verified_at=time.time(),
                                  last_err="")
            return {
                "ok": True,
                "account": name,
                "local_path": local,
                "video_url": f"{config.PUBLIC_BASE}/videos/{Path(local).name}",
                "source_url": res["video_url"],
                "conversation_id": res.get("conversation_id", ""),
            }

    async def generate_video(self, prompt: str, ratio: str | None = None,
                             duration: int = 5, model: str = "seedance-2.0",
                             image_paths: list[str] | None = None,
                             on_account_try=None, max_attempts: int = 0,
                             deadline: float | None = None,
                             on_poll=None) -> dict:
        """号池出片：挑号 → 失败换号 → 到点抛错。返回与 BrowserPool 同形。"""
        if not self.accounts:
            raise RuntimeError("豆包号池为空（doubao_accounts/ 下没有账号）")
        tried: list[str] = []
        errors: list[str] = []
        last_exc: Exception | None = None
        while deadline is None or time.time() < deadline:
            if max_attempts and len(tried) >= max_attempts:
                break
            cands = [n for n in self._select() if n not in tried]
            if not cands:
                # ★ 2026-10-13 修复死循环：`_select()` 会因**冷却/额度用尽/
                #   调度关闭/新号预热**过滤掉账号，因此「候选为空」并不等于
                #   「所有号都试过了」。原实现只看 `all(n in tried)`，当池里
                #   没有任何可调度号时 tried 仍为空 → 该判断为 False →
                #   无限 sleep(2) 直到 deadline（实测把测试卡死 240s+）。
                #   正确做法：先看**全量可调度号**是否为空；空 = 号池当前不可用，
                #   立即判失败（附预热/冷却剩余时间），不要空等。
                if not self._select():
                    break
                if all(n in tried for n in self.accounts):
                    break
                await asyncio.sleep(2)
                continue
            name = cands[0]
            tried.append(name)
            if on_account_try:
                try:
                    on_account_try(name, len(tried))
                except Exception:
                    pass
            try:
                async with self.lock_for(name):
                    res = await self._run_one(name, prompt, ratio or "16:9", duration,
                                              model, image_paths=image_paths,
                                              on_poll=on_poll)
                    local = await self._download(res["video_url"], name)
                self.store.charge(name, 1)
                self.store.patch_meta(name, login_ok=True, verified_at=time.time(),
                                      last_err="")
                return {"video_url": f"{config.PUBLIC_BASE}/videos/{Path(local).name}",
                        "local_path": local, "source_url": res["video_url"],
                        "conversation_id": res.get("conversation_id", ""),
                        "account": name}
            except DoubaoCreditError as exc:
                self.store.patch_meta(name, used_today=self.daily_limit,
                                      last_err=str(exc)[:200], updated_at=time.time())
                last_exc = exc
                errors.append(f"{name}: {str(exc)[:80]}")
            except DoubaoLoginError as exc:
                # ★ 注意顺序：DoubaoThrottleError **不继承** DoubaoLoginError，
                #   所以限流不会被这里误判成登录失效（见 _classify_failure 注释）。
                self.store.cooldown(name, self.cooldown_seconds, str(exc))
                self.store.patch_meta(name, login_ok=False)
                last_exc = exc
                errors.append(f"{name}: {str(exc)[:80]}")
            except Exception as exc:
                kind = self._classify_failure(exc)
                msg = f"{type(exc).__name__}: {exc}"
                if kind == "throttled":
                    # 限流：短冷却、不标登录失效（与 test_generate 同口径）
                    self.store.cooldown(name, TRANSIENT_COOLDOWN_SEC,
                                        "上游限流: " + msg[:160], kind="throttled")
                elif kind == "credit":
                    self.store.patch_meta(name, used_today=self.daily_limit,
                                          last_err=msg[:200], updated_at=time.time())
                elif kind == "login":
                    self.store.cooldown(name, self.cooldown_seconds, msg, kind="login")
                    self.store.patch_meta(name, login_ok=False)
                elif kind == "paid":
                    # 付费额度确认卡：换号可解 → 挪出调度后继续换号
                    self.store.cooldown(
                        name, PAID_QUOTA_COOLDOWN_SEC,
                        "今日免费额度已用完，需客户端点「确认生成」同意付费；"
                        "已临时移出调度 %d 小时（跨日自动恢复）: %s"
                        % (PAID_QUOTA_COOLDOWN_SEC // 3600, msg[:140]),
                        kind="paid")
                else:
                    self.store.patch_meta(name, last_err=msg[:200],
                                          updated_at=time.time())
                last_exc = exc
                errors.append(f"{name}: {str(exc)[:80]}")
                # ★ manual（按钮确认卡）：换号也无济于事（每号都会弹卡），
                #   立刻终止换号循环，把「去 App 点确认」的提示直接带给调用方。
                if kind == "manual":
                    raise DoubaoButtonConfirmError(
                        "图生视频需人工确认（豆包 App 内点击「确认生成」按钮后重试）: "
                        + str(exc)[:200]) from exc
                # ★ timeout（会话已建、只是轮询超时）：上游可能仍在出片，
                #   换号重提 = 重复出片 + 重复扣额度，直接判失败并把诊断带出去。
                if kind == "timeout":
                    raise
        if last_exc is not None:
            raise RuntimeError(
                "豆包域出片失败（已尝试: %s）· %s"
                % ("、".join(tried) or "无", "；".join(errors[-3:]) or str(last_exc)))
        # 池里没有可调度号：区分「全在预热」与其它原因，给出可操作提示
        warm = [(n, self._warmup_left(
                    self.store.roll_day(self.store.read_meta(n)), time.time()))
                for n in self.accounts]
        warming = [(n, w) for n, w in warm if w > 0]
        if warming and len(warming) == len(self.accounts):
            soon = min(w for _, w in warming)
            raise RuntimeError(
                "豆包号池全部处于新号预热期（最近一个还需 %d 分 %d 秒）："
                "刚入池的账号需静默一段时间以避免风控，到期自动可调度"
                % (soon // 60, soon % 60))
        raise RuntimeError("豆包域没有可用账号（全部冷却/额度用尽/未验活）")


__all__ = [
    "DoubaoPool", "DoubaoAccountStore", "resolve_model", "default_proxy",
    "ACCOUNTS_DIR", "UPLOAD_DIR", "MODELS", "DURATIONS", "RATIOS",
    "IMAGE_MODELS", "IMAGE_RATIOS", "VIDEO_RATIOS",
    "DAILY_LIMIT", "COOLDOWN_SECONDS", "TRANSIENT_COOLDOWN_SEC", "usage_day",
    "GROUP_FULL", "GROUP_COOLING", "GROUP_BUSY", "GROUP_RISK",
    "GROUP_PENDING", "GROUP_ABNORMAL", "GROUP_WARMUP", "ACTIVE_GROUPS",
    "WARMUP_SECONDS", "CN_DOMAIN",
]
