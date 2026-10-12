"""dola-pool OpenAI 兼容视频服务（FastAPI）+ 管理面板后端。

对外协议（异步两段式）：
  POST /v1/videos/generations   -> {"id","status","model","prompt"}
  GET  /v1/videos/{id}          -> {"id","status","video_url","error"}
  GET  /videos/<file>           -> 出片转存静态服务

管理面板：GET / -> web/index.html；管理接口 /api/admin/*。
"""
import asyncio
import aiohttp
import base64
import functools
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import time
import uuid
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import cookie_export as ck_export
import cred_store
import failure_text
import proxy_store
from add_account import add_account_flow
from cookie_login import import_cookie_account, import_cookie_accounts_from_text
import register_service
from register_service import REG as register_mgr
import take_service
import video_pool_service
from video_pool_service import manager as videopool_mgr
from credential_import import allocate_credentials, parse_account_text
import doubao_login
import doubao_pool
from doubao_pool import DoubaoAccountStore, DoubaoPool
from browser_pool import (
    ACTIVE_GROUPS,
    GROUP_ABNORMAL,
    GROUP_BUSY,
    GROUP_COOLING,
    GROUP_FULL,
    GROUP_HALF,
    GROUP_ORDER,
    GROUP_PENDING,
    GROUP_RISK,
    AllAccountsLimitedError,
    AllAccountsQuotaBlockedError,
    BrowserPool,
)
from media import (
    cleanup_local_references,
    delete_reference_thumbnails,
    download_reference_images,
    materialize_data_urls,
    save_reference_thumbnails,
    sweep_stale_references,
    validate_reference_urls,
)
from proxy_store import (
    create_proxy,
    delete_proxy,
    get_account_proxy,
    list_proxies,
    set_account_proxy,
    update_proxy,
)
from store import PendingTaskLimitExceeded, TaskQuotaExceeded, TaskStore
from stress_test import run_stress
from user_store import (
    create_user as user_create,
    delete_user as user_delete,
    get_user as user_get,
    list_users as user_list,
    update_user as user_update,
    verify_login as user_verify,
)

Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path("web").mkdir(parents=True, exist_ok=True)

APP_VERSION = "2.2.0"

app = FastAPI(title="dola-pool", version=APP_VERSION)

# 浏览器直连（画布这类纯前端）需要 CORS；默认放开所有来源，
# 用 DOLA_CORS_ORIGINS 收紧（逗号分隔）。鉴权依旧是 Bearer Key，放开来源不等于放开权限。
CORS_ORIGINS = [o.strip() for o in os.getenv("DOLA_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

store = TaskStore(config.DB_PATH)
pool = BrowserPool(max_concurrency=config.MAX_CONCURRENCY)
# 豆包域（www.doubao.com）平行通道 —— 纯 Cookie 号池，与 dola 浏览器号池互不干扰。
# 账号落在 doubao_accounts/<name>/（ck.txt + meta.json），由 channel=doubao 分流。
doubao_store = DoubaoAccountStore(doubao_pool.ACCOUNTS_DIR)
doubao = DoubaoPool(store=doubao_store)
# 豆包登录会话（内存态）：session_id -> LoginSession，面板扫码/发码后轮询它
DOUBAO_LOGIN_SESSIONS: dict[str, Any] = {}
DOUBAO_LOGIN_TTL = 600   # 会话保留 10 分钟

app.mount("/videos", StaticFiles(directory=config.DOWNLOAD_DIR), name="videos")
# 参考图缩略图（任务结束后唯一能回看的副本）；文件名 = <task_id>_<index>.jpg
Path(config.THUMB_DIR).mkdir(parents=True, exist_ok=True)
app.mount("/thumbs", StaticFiles(directory=config.THUMB_DIR), name="thumbs")
# 豆包测试生成上传的参考图（面板上传后回填给出片链路用，仅本机/内网访问）
Path(doubao_pool.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
app.mount("/doubao-uploads", StaticFiles(directory=str(doubao_pool.UPLOAD_DIR)),
          name="doubao-uploads")


@app.middleware("http")
async def _no_cache_panel_html(request, call_next):
    """管理面板 HTML 永远走协商缓存（no-cache）。

    StaticFiles 只带 ETag/Last-Modified，浏览器会启发式缓存 index.html 且普通
    打开不一定回源 —— 面板改版后用户看到的还是旧页面（2026-10-06 事故）。
    HTML 强制 no-cache 后每次打开都会带 ETag 回源校验，改版即生效。
    """
    response = await call_next(request)
    if request.url.path in ("/", "/index.html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# 后台 jobs（加号/验证），内存态
JOBS: dict[str, dict] = {}
# 账号级「测试生成」任务（内存态）：指定某个账号出片，观察是否成功
TEST_JOBS: dict[str, dict] = {}
# 账号登录态自动刷新（防抖：同一账号刷新间隔）
LAST_CRED_REFRESH: dict[str, float] = {}
# 加号并发上限：1.9GB 内存机器上同时开多个浏览器登录会 OOM，批量添加时逐个排队登录
ADD_ACCOUNT_SEM = asyncio.Semaphore(1)
# 浏览器并发压力测试状态（内存态）
STRESS_RUN: dict = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "recommended": None,
    "results": [],
    "logs": [],
}
# 批量操作任务（批量验证等，内存态）
BATCH_JOBS: dict[str, dict] = {}
# 批量验证串行执行：验证会开浏览器/打代理，同时跑多批会互相抢资源
BATCH_VERIFY_SEM = asyncio.Semaphore(1)
# 批量「你好」探测的并发：**0 = 不限（默认，按用户要求全部放开）**，>0 = 限制路数。
# 注意：上游对"同一出口 IP 短时间多次提交"敏感（实测并发 3 路就会被 710022002 整批拒），
# 放开后大概率会看到一批"未完成"——那些不会锁号，只是这次没探到。
BATCH_PROBE_CONCURRENCY = int(os.getenv("DOLA_HELLO_PROBE_CONCURRENCY", "0"))
# 每个探测之间的间隔（秒）：0 = 不等（默认，配合不限并发一起放开）。
BATCH_PROBE_INTERVAL_SECONDS = float(os.getenv("DOLA_HELLO_PROBE_INTERVAL", "0"))
# 软失败后的退避重试间隔（秒）。这不是并发限制，是防误锁：两次都失败才判风控。
BATCH_PROBE_RETRY_SECONDS = float(os.getenv("DOLA_HELLO_PROBE_RETRY_WAIT", "4.0"))


class _NoLimit:
    """并发不限时的空信号量（asyncio.Semaphore(0) 会死锁，所以单独给个 no-op）。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False
# 管理后台会话（token -> 用户信息），重启后需重新登录
ADMIN_SESSIONS: dict[str, dict] = {}
ADMIN_SESSION_TTL = 7 * 86400

SIZE_TO_RATIO = {
    "1280x720": "16:9", "1920x1080": "16:9",
    "720x1280": "9:16", "1080x1920": "9:16",
    "1024x1024": "1:1", "1440x1080": "4:3", "1080x1440": "3:4",
}
VIDEO_MAX_ATTEMPTS = config.VIDEO_MAX_ATTEMPTS  # 0 = 不限次数，在 TASK_DEADLINE 内持续换号
SUPPORTED_DURATIONS = tuple(config.ALL_DURATIONS)  # (5, 10, 15, 30)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


RATIO_TO_DIMENSIONS = {
    "16:9": (1280, 720), "9:16": (720, 1280),
    "1:1": (1024, 1024), "4:3": (1440, 1080), "3:4": (1080, 1440),
}


def _probe_with_ffprobe(path: str) -> dict | None:
    """用 ffprobe 读取真实视频元数据；未安装或解析失败返回 None。"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return None
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    fmt = data.get("format") or {}
    meta = {}
    if stream:
        try:
            meta["video_width"] = int(stream.get("width") or 0) or None
            meta["video_height"] = int(stream.get("height") or 0) or None
        except (TypeError, ValueError):
            pass
        dur = stream.get("duration") or fmt.get("duration")
        try:
            meta["video_duration"] = float(dur) if dur else None
        except (TypeError, ValueError):
            pass
    bit_rate = fmt.get("bit_rate")
    try:
        meta["video_bitrate"] = int(bit_rate) if bit_rate else None
    except (TypeError, ValueError):
        pass
    return {k: v for k, v in meta.items() if v is not None} or None


def _video_metadata(local_path: str, duration: int | None, ratio: str | None) -> dict:
    """出片完成后的视频元数据：优先 ffprobe 真实值，缺失时用任务记录推算。"""
    meta: dict = {}
    try:
        meta["video_size"] = os.path.getsize(local_path)
    except OSError:
        pass
    probed = _probe_with_ffprobe(local_path)
    if probed:
        meta.update(probed)
    else:
        w, h = RATIO_TO_DIMENSIONS.get(ratio or "", (0, 0))
        if w and h:
            meta["video_width"], meta["video_height"] = w, h
        if duration:
            meta["video_duration"] = float(duration)
    if meta.get("video_size") and meta.get("video_duration"):
        meta["video_bitrate"] = int(meta["video_size"] * 8 / meta["video_duration"])
    return meta


class KeyConcurrencyLimiter:
    """按 API Key 限制同时运行的任务；0 表示不限。"""

    def __init__(self):
        self._condition = asyncio.Condition()
        self._active: defaultdict[str, int] = defaultdict(int)

    async def acquire(self, api_key_hash: str | None, limit: int):
        if not api_key_hash or limit <= 0:
            return
        async with self._condition:
            while self._active[api_key_hash] >= limit:
                await self._condition.wait()
            self._active[api_key_hash] += 1

    async def release(self, api_key_hash: str | None):
        if not api_key_hash:
            return
        async with self._condition:
            if self._active[api_key_hash] > 0:
                self._active[api_key_hash] -= 1
            if self._active[api_key_hash] == 0:
                self._active.pop(api_key_hash, None)
            self._condition.notify_all()



key_limiter = KeyConcurrencyLimiter()


# ===== 鉴权 =====


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _anonymous_client() -> dict:
    return {
        "api_key_hash": None,
        "api_key_name": "匿名调用",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _env_client(key: str) -> dict:
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": f"环境变量 Key ({key[:8]}…)",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


# ===== store 落库线程化 helper =====
# 背景：store.* 是同步 SQLite 调用（内部共享一把 threading.Lock），async handler
# 里直接调用会占死事件循环（锁竞争最坏 5s），面板所有接口一起卡。
# 用 max_workers=1 的专用执行器而不是 asyncio.to_thread（默认多线程池）：
# 单线程保证 store.update 的提交顺序 == 实际执行顺序，避免「processing 状态回写」
# 排到「completed」之后把终态任务打回进行中的竞态。
_STORE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="store-db")


def _store_update_safe(task_id, **kw) -> None:
    try:
        store.update(task_id, **kw)
    except Exception as exc:
        print(f"[store] update {task_id} failed: {exc}", flush=True)


async def _update(task_id, **kw) -> None:
    """await 版 store.update：在专用单线程执行器里跑，不阻塞事件循环，保持保序。"""
    await asyncio.get_running_loop().run_in_executor(
        _STORE_EXECUTOR, functools.partial(store.update, task_id, **kw))


def _update_bg(task_id, **kw) -> None:
    """fire-and-forget 版 store.update：仅供被上游同步调用、无法 await 的回调用
    （on_poll / _beat 这类高频心跳）。失败只记日志，不打断生成流程。
    回调可能跑在事件循环线程，也可能跑在纯线程上下文（如 pure_api_gen 的心跳
    线程）：有 running loop 就挂到 loop 上提交，否则直接提交执行器。"""
    call = functools.partial(_store_update_safe, task_id, **kw)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _STORE_EXECUTOR.submit(call)
        return
    loop.run_in_executor(_STORE_EXECUTOR, call)


def _update_sync(task_id, **kw) -> None:
    """**就地**写库的状态迁移回调（on_account_try / on_conversation_id）。

    这些写入必须在下一次尝试真正开始之前落库：调用方（号池）拿到账号后立刻
    就往下走，若把它们也丢给后台执行器，`account` / `started_at` / `processing`
    会晚于实际尝试落地 —— 面板会先看到「还在排队」、测试用例会读到 None。
    写库是 WAL + synchronous=NORMAL（实测提交 ~0.02ms），就地写不会卡事件循环。
    """
    _store_update_safe(task_id, **kw)


async def _auth(authorization):
    """返回本次调用的客户策略；没有任何 Key 配置时保持开发模式免鉴权。"""
    if not config.API_KEYS and not await asyncio.to_thread(store.has_enabled_keys):
        return _anonymous_client()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    key = authorization[7:].strip()
    if not key:
        raise HTTPException(401, "missing bearer token")
    if key in config.API_KEYS:
        return _env_client(key)
    record = await asyncio.to_thread(store.get_key, key)
    if not record or not await asyncio.to_thread(store.is_key_valid, key):
        raise HTTPException(401, "invalid api key")
    await asyncio.to_thread(store.touch_key, key)
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": record["name"] or "未命名客户",
        "daily_limit": record["daily_limit"],
        "concurrency_limit": record["concurrency_limit"],
        "allowed_durations": record["allowed_durations"],
    }


def _admin_auth(x_admin_key: str | None):
    # 已移除管理面板登录：面板直接开放访问，任何请求一律放行。
    return


def _normalize_allowed_durations(values) -> list[int]:
    if values is None:
        return list(SUPPORTED_DURATIONS)
    try:
        normalized = sorted({int(value) for value in values})
    except (TypeError, ValueError):
        raise HTTPException(422, f"allowed_durations 必须是 {list(SUPPORTED_DURATIONS)} 的数组")
    if not normalized or any(value not in SUPPORTED_DURATIONS for value in normalized):
        raise HTTPException(
            422, f"allowed_durations 只能包含 {list(SUPPORTED_DURATIONS)}，且至少选择一个"
        )
    return normalized


# ===== 客户 API =====


class VideoGenRequest(BaseModel):
    model: str = "seedance-2.0"
    prompt: str = Field(..., min_length=1)
    size: str | None = None
    ratio: str | None = None
    duration: int | None = Field(None, ge=5, le=30)
    # 时长别名：new-api 的 sora/vinted 任务插件会把画布传来的 duration 统一改写成
    # `seconds` 再转发到本服务，所以这里必须一起认；否则 30 秒请求会被当成「没带时长」
    # 而落到默认 10 秒（画布「选 30 秒却生成 10 秒」就是这个原因）。
    seconds: int | None = Field(None, ge=5, le=30)
    duration_seconds: int | None = Field(None, ge=5, le=30)
    # 参考图：兼容各画布工具的常见字段形态（字符串/数组/{"url": ...}）
    reference_images: list[str] = Field(default_factory=list)
    image_url: Any = None
    image: Any = None
    input_image: Any = None
    input_images: Any = None


class TestGenRequest(BaseModel):
    """账号级「测试生成」：用指定账号跑一条真实出片，看该号能否成功。

    reference_images：参考图（本地落盘路径 / 公网 URL / data: URL）。
    dola 账号沿用原浏览器 / 纯 API 参考图链路；豆包账号走
    `upload_image() → prepare_upload → ApplyImageUpload → PUT TOS` 链路。
    """
    prompt: str = Field(..., min_length=1)
    model: str = "seedance-2.0"
    duration: int = Field(10, ge=5, le=30)
    ratio: str | None = None
    reference_images: list[str] = Field(default_factory=list)


class GenSetBody(BaseModel):
    """「生成设置」：测试生成弹窗的默认模型/时长/画幅（按渠道分域）。"""
    model: str = "seedance-2.0"
    duration: int = Field(5, ge=5, le=30)
    ratio: str = "16:9"
    channel: str = "dola"


class TaskResponse(BaseModel):
    id: str
    status: str
    model: str | None = None
    prompt: str | None = None
    video_url: str | None = None
    error: str | None = None
    # 排队/进度说明：客户端可以拿它显示「排队中，预计 N 分钟」。
    # 字段是附加的（老客户端忽略即可，不影响原有 status/video_url 语义）。
    progress: dict | None = None


def _eta_text(seconds: int) -> str:
    if seconds <= 45:
        return "不到 1 分钟"
    minutes = max(1, int(round(seconds / 60.0)))
    return f"约 {minutes} 分钟"


def _progress_payload(*, status: str, created_at: float | None, duration: int | None) -> dict:
    """把排队情况翻译成客户端能直接显示的一句话 + 结构化字段。"""
    info = store.queue_progress(created_at=created_at, duration=duration)
    ahead = info["ahead"]
    running = info["running"]
    typical = max(30, int(info["typical_seconds"]))
    workers = int(config.MAX_CONCURRENCY or 0)   # 0 = 不限并发
    if status == "queued":
        # 有并发槽时按槽位折算等待；不限并发时任务之间不互相排队
        batches = (ahead // workers) + 1 if workers > 0 else 1
        eta = batches * typical
        message = f"排队中，预计{_eta_text(eta)}"
        if ahead:
            message += f"（前面还有 {ahead} 个任务）"
        else:
            message += "（等待账号空闲）"
        state = "queued"
    elif status == "processing":
        eta = typical
        message = f"生成中，预计{_eta_text(eta)}"
        state = "running"
    elif status == "completed":
        eta = 0
        message = "已完成"
        state = "done"
    else:
        eta = 0
        message = "生成失败"
        state = "failed"
    return {
        "state": state,
        "position": ahead + 1 if state == "queued" else None,
        "ahead": ahead,
        "running": running,
        "workers": workers,
        "typical_seconds": typical,
        "eta_seconds": int(eta),
        "eta_text": _eta_text(int(eta)) if eta else "",
        "message": message,
    }


def _resolve_ratio(size, ratio):
    if size and size in SIZE_TO_RATIO:
        return SIZE_TO_RATIO[size]
    return ratio


def _model_version(model) -> str | None:
    """识别请求里显式声明的 seedance 版本；无法识别返回 None（交给时长规则选择）。"""
    m = (model or "").lower().replace("_", "-")
    if "2.5" in m or "2-5" in m:
        return "seedance-2.5"
    if "2.0" in m or "2-0" in m or "v2.0" in m:
        return "seedance-2.0"
    return None


def _resolve_model_for_duration(model, duration) -> str | None:
    """模型-时长组合归一化；无法组合时返回 None。

    - 请求里写明了 2.5/2.0 版本 → 只允许该版本支持的时长，避免悄悄换型号。
    - 模型名无法识别（如 __schema_probe_invalid_model__）→ 按时长挑一个能生成的版本：
      5/10/15 秒走 seedance-2.0，30 秒与非原生时长走 seedance-2.5；
      10 秒两种都支持时默认 seedance-2.0（消耗点数更低）。
    """
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        return None
    version = _model_version(model)
    if version is None:
        if duration in config.V20_DURATIONS:
            version = "seedance-2.0"
        else:
            # 30 秒一直是 2.5；任意时长（非原生档位）也只有 2.5 能出。
            version = "seedance-2.5"
    if not _duration_supported(version, duration):
        return None
    return version


def _supported_durations_for(version: str) -> list[int]:
    """某个 seedance 版本当前能下发的时长。

    用 config.all_durations() 而不是模块级常量 ALL_DURATIONS：后者是 import 时
    求值的快照，运行期调整 NATIVE_DURATION_MAX 不会反映出来。
    """
    if version == "seedance-2.5":
        return config.all_durations()
    return list(config.V20_DURATIONS)


def _duration_supported(version: str, duration: int) -> bool:
    """时长在当前白名单下是否可下发。

    原生档位**永远放行** —— 尤其 30 秒，绝不能被 NATIVE_DURATION_MAX 夹掉
    （夹了就是把 30 秒片悄悄变成 15 秒片，线上主力流量会崩）。
    非原生档位只在开了任意时长、落在区间内、且走 2.5 时才放行。
    """
    if duration in config.NATIVE_DURATIONS:
        return True
    if config.NATIVE_DURATION_MAX <= 0:
        return False
    if not (config.MIN_DURATION <= duration <= config.NATIVE_DURATION_MAX):
        return False
    return version == "seedance-2.5"


def _normalize_model(model: str) -> str:
    """宽松归一化：显式 2.5 → seedance-2.5，其余一律 seedance-2.0。"""
    return _model_version(model) or "seedance-2.0"


def _duration_cost(model: str, duration: int) -> int:
    """单次出片消耗的额度点数：只看模型，不看时长（对齐 dola-pool-cookie）。

    未知模型按最贵的 DEFAULT_CREDIT_COST 算。旧实现是「矩阵查不到就返回 1 点」，
    属于**少扣**：一旦放开任意时长，每个未登记的 (模型, 时长) 组合都会被按 1 点
    记账，一个号一天能跑 4 条。duration 参数保留仅为兼容调用方签名。
    """
    return config.MODEL_COSTS.get(_normalize_model(model), config.DEFAULT_CREDIT_COST)


def _pick_duration(requested, allowed: list[int]) -> int:
    """把 Ark 请求的时长映射到允许档位，优先取不小于请求值的档位。"""
    try:
        requested = max(1, int(requested or 0))
    except (TypeError, ValueError):
        requested = 10
    allowed = sorted(allowed) or list(SUPPORTED_DURATIONS)
    for duration in allowed:
        if duration >= requested:
            return duration
    return allowed[-1]


def _normalize_image_refs(*fields) -> list[str]:
    """兼容各画布工具常见的图片字段形态（字符串/列表/{"url": ...}）。"""
    out: list[str] = []
    for value in fields:
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                out.append(value.strip())
        elif isinstance(value, dict):
            url = value.get("url")
            if isinstance(url, str) and url.strip():
                out.append(url.strip())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    out.append(item.strip())
                elif isinstance(item, dict):
                    url = item.get("url")
                    if isinstance(url, str) and url.strip():
                        out.append(url.strip())
    seen = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def _deep_image_urls(value, out: list[str] | None = None, depth: int = 0) -> list[str]:
    """递归抓取请求里任意层级的图片 URL（兼容画布工具的各种嵌套格式）。"""
    if out is None:
        out = []
    if depth > 8 or len(out) >= 20 or value is None:
        return out
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("http://", "https://", "data:image/")) and s not in out:
            out.append(s)
        return out
    if isinstance(value, dict):
        keys = [str(k).lower() for k in value]
        has_image_ctx = (
            any("image" in k or "ref" in k or "参考" in k for k in keys)
            or "image" in str(value.get("type", "")).lower()
        )
        for k, v in value.items():
            kk = str(k).lower()
            if isinstance(v, str):
                s = v.strip()
                if ((has_image_ctx or kk in ("url", "uri", "image", "image_url", "file_url", "src"))
                        and s.startswith(("http://", "https://", "data:image/")) and s not in out):
                    out.append(s)
            elif isinstance(v, (dict, list)):
                _deep_image_urls(v, out, depth + 1)
        return out
    if isinstance(value, list):
        for x in value:
            _deep_image_urls(x, out, depth + 1)
    return out


def _log_image_fields(raw, endpoint: str) -> None:
    """把疑似带图的请求原始结构打日志，便于定位画布工具实际传图字段。"""
    if not isinstance(raw, dict):
        return
    try:
        sample = json.dumps(raw, ensure_ascii=False)
    except Exception:
        return
    markers = ('"image', "image_url", '"url"', '"content"', '"input', 'data:image', 'reference')
    if not any(m in sample for m in markers):
        return
    keys = sorted(str(k) for k in raw.keys())
    print(
        f"[imgreq] {endpoint} top_keys={json.dumps(keys, ensure_ascii=False)} "
        f"sample={sample[:700]}",
        flush=True,
    )


def _map_ark_ratio(ratio) -> str | None:
    """Ark 的 ratio 直接透传；adaptive/未知值回落 dola 默认。"""
    if not ratio:
        return None
    value = str(ratio).strip().lower()
    if value in ("adaptive", "auto", "default"):
        return None
    return value if value in {"16:9", "9:16", "1:1", "4:3", "3:4"} else None


async def _run_task(task_id, model, prompt, ratio, duration, reference_images, client):
    api_key_hash = client.get("api_key_hash")
    acquired = False
    reference_root = None
    try:
        # 重新受理：排队时钟（updated_at）与上一轮的报错一起清零。看门狗按「最后一次状态
        # 变化」算排队时长，不清零的话刚被重启捞回来的任务会带着旧的 updated_at 立刻被判
        # 「排队超时」——把刚修好的恢复路径又堵死。
        await _update(task_id, status="queued", error="")
        await key_limiter.acquire(api_key_hash, client.get("concurrency_limit", 0))
        acquired = True
        # 这里不能急着标 processing：接下来 pool.generate_video 还可能在全局并发槽 /
        # 账号锁上等待，那段等待必须让客户端看到 queued（排队中）。真正拿到账号
        # （= 拿到并发槽）的那一刻由下面的 on_account_try 置为 processing。

        def on_conversation_id(account, conversation_id, deadline_at):
            _update_sync(task_id, status="processing", account=account,
                         conversation_id=conversation_id, deadline_at=deadline_at,
                         last_poll_at=time.time())

        def on_poll(now, wait_minutes=None):
            # Dola 受理后回的「预计等待 N 分钟」原样落库，任务列表用它显示「预计 44M」。
            if wait_minutes:
                _update_bg(task_id, last_poll_at=now, eta_text=str(wait_minutes))
            else:
                _update_bg(task_id, last_poll_at=now)

        tried_accs: list[str] = []

        def on_account_try(account, attempt):
            # 拿到账号即拿到并发槽 —— 此刻才算真正开始生成（等待期间保持 queued）。
            tried_accs.append(account)
            fields = dict(status="processing", account=account, attempt=attempt,
                          attempted_accounts=json.dumps(tried_accs, ensure_ascii=False),
                          last_poll_at=time.time())
            if len(tried_accs) == 1:
                # started_at 只记首次尝试，否则换号会把出片耗时均值（ETA）拉长
                fields["started_at"] = time.time()
            _update_sync(task_id, **fields)

        reference_root, reference_paths = await download_reference_images(
            reference_images or [], task_id)
        if reference_paths:
            # 趁原图还在（终态会被清理）先存一份缩略图，供面板回看
            thumbs = await asyncio.to_thread(
                save_reference_thumbnails, reference_paths, task_id)
            if thumbs:
                await _update(task_id, reference_thumbs=json.dumps(thumbs))
        # 任务总时限内持续换号；到点不再派发新尝试。兜底超时额外留一次完整出片的等待时间，
        # 避免把已经在 Dola 端生成中的那一次强行中断（会白扣额度）。
        # 2026-10-07：4~30s（seedance-2.5）全部路由到浏览器通道（2 点计价），
        # 上游排队实测 30~80 分钟，3600s 通用时限会把还在线生成中的任务拦腰砍断，
        # 因此该区间统一用 TASK_DEADLINE_30S 级别的长时限。
        task_budget = config.TASK_DEADLINE
        if 4 <= int(duration or 0) <= 30:
            task_budget = max(task_budget, config.TASK_DEADLINE_30S)
        task_deadline = time.time() + task_budget
        try:
            result = await asyncio.wait_for(pool.generate_video(
                prompt, ratio, duration, model,
                on_conversation_id=on_conversation_id, on_poll=on_poll,
                reference_image_paths=reference_paths,
                on_account_try=on_account_try, max_attempts=VIDEO_MAX_ATTEMPTS,
                deadline=task_deadline),
                timeout=task_budget + config.VIDEO_TIMEOUT + 60)
        except asyncio.TimeoutError:
            chain = "、".join(tried_accs) or "无"
            # 兜底超时也要带原因：号池记录的「最后一次尝试失败原因 / 中断时正在出片的号」
            last_note = getattr(pool, "_last_attempt_err", None)
            cur_acc = getattr(pool, "_current_account", None)
            window_min = (task_budget + config.VIDEO_TIMEOUT + 60) // 60
            if last_note:
                detail = f"最后一次尝试（{cur_acc or '?'}）: {str(last_note)[:400]}"
            else:
                detail = (f"中断时 {cur_acc or '?'} 仍在上游生成中（已提交成功、未出片也未报错）"
                          "——通常是上游拥堵，受理后一直没给成片")
            raise RuntimeError(
                f"超过任务总时限（约 {window_min} 分钟）仍未生成成功，任务失败"
                f"（已尝试账号: {chain}）· {detail}")
        public_url = f"{config.PUBLIC_BASE}/videos/{Path(result['local_path']).name}"
        # ffprobe 是同步子进程（几十~几百毫秒），必须在事件循环外跑：
        # 高并发下每完成一条就卡一次 loop，会把所有轮询/接口/健康检查一起拖住。
        meta = await asyncio.to_thread(_video_metadata, result["local_path"], duration, ratio)
        await _update(task_id, status="completed", video_url=public_url,
                      account=result.get("account"), last_poll_at=time.time(),
                      finished_at=time.time(), **meta)
        # 出片目录超限就从旧到新删，避免磁盘被撑满（同样是同步目录遍历）
        await asyncio.to_thread(_prune_downloads)
    except (AllAccountsLimitedError, AllAccountsQuotaBlockedError) as e:
        await _update(task_id, status="failed", error=failure_text.summarize(str(e)),
                      failure_code="429", finished_at=time.time())
    except asyncio.CancelledError:
        # 服务重启/任务被取消：CancelledError 不是 Exception，别把它吞了，
        # 但也别把行留在 processing —— 退回 queued，下次启动或看门狗会重新派发。
        await _update(task_id, status="queued", account=None, last_poll_at=0,
                      error="任务被中断（服务重启），已重新排队")
        raise
    except Exception as e:
        await _update(task_id, status="failed", error=failure_text.summarize(str(e)),
                      finished_at=time.time())
    finally:
        if reference_root:
            # 参考图临时目录清理：磁盘删除本机实测 ~44ms/文件，不能占着事件循环
            await asyncio.to_thread(
                shutil.rmtree, reference_root, True)
        # 落盘的参考图只在任务**走到终态**时清理：任务被取消/服务重启时它还要留给
        # resume 用（库里存的是路径，文件删早了恢复时就报「参考图文件已丢失」）。
        row = await asyncio.to_thread(store.get, task_id)
        if row and row.get("status") in ("completed", "failed"):
            await asyncio.to_thread(cleanup_local_references, reference_images or [])
            await _update(task_id, reference_images="[]")
        if acquired:
            await key_limiter.release(api_key_hash)


async def _resume_task(row: dict):
    task_id = row["id"]
    account = row.get("account")
    # cookie 号（纯 API）重启后不该去调浏览器 resume —— 本机没有可用 Chromium，
    # 只会报 `Connection closed while reading from the driver` 把任务判死。
    # 直接按原参数重新受理一次（参考图落盘文件已保留，不会丢）。
    if account and pool.is_pure_account(account):
        ratio = None if row.get("ratio") == "default" else row.get("ratio")
        print(f"[resume] {task_id}（{account}）走纯 API 重新受理", flush=True)
        await _run_task(
            task_id, row["model"], row["prompt"], ratio, row.get("duration"),
            _task_reference_images(row.get("reference_images")), _task_client(row),
        )
        return
    deadline = row.get("deadline_at") or (
        time.time() + (1800 if row.get("duration") == 30 else config.VIDEO_TIMEOUT)
    )
    remaining = max(1, int(deadline - time.time()))
    api_key_hash = row.get("api_key_hash")
    acquired = False
    try:
        await key_limiter.acquire(
            api_key_hash, int(row.get("client_concurrency_limit") or 0)
        )
        acquired = True
        await _update(task_id, status="processing", last_poll_at=time.time(),
                      started_at=row.get("started_at") or time.time())

        def on_poll(now, wait_minutes=None):
            if wait_minutes:
                _update_bg(task_id, last_poll_at=now, eta_text=str(wait_minutes))
            else:
                _update_bg(task_id, last_poll_at=now)

        result = await pool.resume_video(
            row["account"], row["conversation_id"], remaining, on_poll=on_poll,
            duration=row.get("duration"),
            ratio=None if row.get("ratio") == "default" else row.get("ratio"),
            cost=_duration_cost(row.get("model"), row.get("duration") or 10))
        public_url = f"{config.PUBLIC_BASE}/videos/{Path(result['local_path']).name}"
        meta = await asyncio.to_thread(
            _video_metadata, result["local_path"], row.get("duration"), row.get("ratio"))
        await _update(task_id, status="completed", video_url=public_url,
                      account=result.get("account"), last_poll_at=time.time(),
                      finished_at=time.time(), **meta)
    except Exception as e:
        await _update(task_id, status="failed", error=failure_text.summarize(str(e)),
                      finished_at=time.time())
    finally:
        if acquired:
            await key_limiter.release(api_key_hash)


def _task_client(row: dict) -> dict:
    """从任务快照恢复调度所需的客户上下文，不依赖 Key 当前是否仍存在。"""
    return {
        "api_key_hash": row.get("api_key_hash"),
        "api_key_name": row.get("api_key_name") or "历史任务",
        "daily_limit": 0,
        "concurrency_limit": int(row.get("client_concurrency_limit") or 0),
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _row_channel(row: dict) -> str:
    """任务行属于哪个渠道。老库没有 channel 列（值是 NULL）→ 归 dola。"""
    ch = str((row or {}).get("channel") or "").strip().lower()
    return "doubao" if ch == "doubao" else "dola"


async def _redispatch_task(row: dict) -> None:
    """按渠道把「排队/中断」的任务重新受理。

    这是重启恢复与看门狗重派的**唯一分流点**：豆包任务必须回到
    `_run_doubao_test_generate`，否则会被当成 dola 任务去调 dola 号池
    （账号名对不上、额度记到错的池子里、上游也打错域）。
    """
    task_id = row["id"]
    if _row_channel(row) == "doubao":
        refs = _task_reference_images(row.get("reference_images"))
        asyncio.create_task(_run_doubao_test_generate(
            task_id, row.get("account") or "", row.get("prompt") or "",
            None if row.get("ratio") == "default" else row.get("ratio"),
            row.get("duration") or 5, row.get("model") or "seedance-2.0", refs))
        return
    ratio = None if row.get("ratio") == "default" else row.get("ratio")
    asyncio.create_task(_run_task(
        task_id, row["model"], row["prompt"], ratio, row.get("duration"),
        _task_reference_images(row.get("reference_images")), _task_client(row),
    ))


def _task_reference_thumbs(raw) -> list[str]:
    """任务行里的缩略图文件名列表（坏数据当空）。"""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data if x] if isinstance(data, list) else []


def _task_reference_images(raw) -> list[str]:
    """任务行里的参考图 URL/本地路径列表（坏数据当空）。重启恢复（_resume_task /
    看门狗重派）时从任务快照还原给 _run_task 用，字段与提交时 json.dumps 的一致。"""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data if x] if isinstance(data, list) else []


def _delete_task_files(row: dict) -> None:
    """删除任务关联的磁盘文件（视频 + 参考图缩略图）。

    本机磁盘删除实测 ~44ms/文件（Defender 实时扫描嫌疑），只能在线程里跑，
    调用方一律 asyncio.to_thread 包装，禁止在事件循环上直接调用。
    """
    url = row.get("video_url")
    if url:
        local = Path(config.DOWNLOAD_DIR) / Path(url).name
        try:
            if local.exists():
                local.unlink()
        except OSError:
            pass
    delete_reference_thumbnails(_task_reference_thumbs(row.get("reference_thumbs")))


_THREAD_POOL: ThreadPoolExecutor | None = None


def open_thread_pool() -> int:
    """把 asyncio 的默认线程池开大：出片/探测都跑在 to_thread 里。

    Python 默认只有 min(32, CPU+4) 个线程 —— 不限并发时它才是真正的瓶颈
    （第 33 个任务只能排队等）。0 = 自动，取 max(64, CPU×8)。
    """
    global _THREAD_POOL
    limit = int(getattr(config, "THREAD_POOL_MAX", 0) or 0)
    workers = limit if limit > 0 else max(64, (os.cpu_count() or 4) * 8)
    _THREAD_POOL = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dola")
    asyncio.get_running_loop().set_default_executor(_THREAD_POOL)
    print(f"[startup] 线程池 {workers} 路（DOLA_THREAD_POOL_MAX={limit}，0=自动）", flush=True)
    return workers


@app.on_event("startup")
async def resume_incomplete_tasks():
    """服务重启后恢复已受理会话，并重新排队尚未开始的 queued 任务。"""
    open_thread_pool()
    # 先处理崩溃时留下的 processing 孤儿任务（有 conversation 保留续轮询，
    # 无 conversation 清空账号重新派号），避免永久卡死。
    stale_refs = sweep_stale_references()
    if stale_refs:
        print(f"[startup] 清理 {stale_refs} 个残留参考图文件", flush=True)
    # 启动也跑一次保留期清理（进程长期不重启时靠每日循环兜着）
    try:
        await asyncio.to_thread(_maintenance)
    except Exception as exc:
        print(f"[startup] 维护任务失败: {exc}", flush=True)
    recovered = store.recover_runtime_jobs()
    if recovered:
        print(f"[startup] 恢复 {len(recovered)} 个运行中任务并重新排队", flush=True)
    for row in store.recoverable_tasks():
        if _row_channel(row) == "doubao":
            # 豆包任务没有 conversation 续轮询语义（走的是豆包域 /im/chain/single），
            # 一律按「重新受理」处理，绝不能进 _resume_task 的 dola 分支。
            asyncio.create_task(_redispatch_task(row))
            continue
        asyncio.create_task(_resume_task(row))
    requeued = store.recoverable_queued_tasks()
    if requeued:
        print(f"[startup] 重新受理 {len(requeued)} 个没有账号的任务"
              "（含被重启取消、只剩会话的）", flush=True)
    for row in requeued:
        asyncio.create_task(_redispatch_task(row))
    asyncio.create_task(_cred_refresher_loop())
    asyncio.create_task(_daily_quota_reset_loop())
    asyncio.create_task(_task_watchdog_loop())


WATCHDOG_DISPATCHED: set[str] = set()
WATCHDOG_RETRIES: dict[str, int] = {}


async def _task_watchdog_loop() -> None:
    """看门狗：把「挂着不动」和「排队等太久」的任务收掉，别让客户端一直等。

    背景：服务重启会把 asyncio 里正在跑的 `_run_task` 取消掉（CancelledError 不会被
    `except Exception` 捕获），数据库里那些行就留在 processing，客户端看起来像挂起。
    这里定期巡检：
    - processing 且超过 STALE_TASK_SECONDS 没有任何轮询更新 → 重新派发（最多 3 次）
    - 还没提交上游（没有 conversation）却等了超过 MAX_QUEUE_WAIT_SECONDS → 明确失败
    """
    while True:
        await asyncio.sleep(max(15, config.WATCHDOG_INTERVAL_SECONDS))
        try:
            actions = await asyncio.to_thread(_watchdog_plan)
        except Exception as exc:  # noqa: BLE001
            print(f"[watchdog] 巡检失败: {exc}", flush=True)
            continue
        for action in actions:
            row = action["row"]
            # ★ 豆包任务一律走 _redispatch_task（它内部按 channel 分流）；
            #   `resume`（续轮询 conversation）只对 dola 有意义。
            if action["kind"] == "resume" and _row_channel(row) != "doubao":
                asyncio.create_task(_resume_task(row))
            else:
                asyncio.create_task(_redispatch_task(row))


def _watchdog_plan() -> list[dict]:
    """只做数据库判定，返回需要上层重新派发的动作（线程里跑，不碰事件循环）。"""
    info = store.stale_pending_tasks(
        stale_seconds=config.STALE_TASK_SECONDS,
        wait_cap_seconds=config.MAX_QUEUE_WAIT_SECONDS,
    )
    failed_ids: set[str] = set()
    for row in info["waiting"]:
        task_id = row["id"]
        since = row.get("queued_since") or row["created_at"] or time.time()
        minutes = (time.time() - float(since)) / 60
        print(f"[watchdog] {task_id} 排队 {minutes:.0f} 分钟仍没轮到（上游持续限流），判失败",
              flush=True)
        store.update(task_id, status="failed", finished_at=time.time(),
                     error=f"排队等待超过 {config.MAX_QUEUE_WAIT_SECONDS // 60} 分钟仍未拿到可用账号"
                           "（上游限流），请稍后重试")
        failed_ids.add(task_id)
        WATCHDOG_DISPATCHED.discard(task_id)
        WATCHDOG_RETRIES.pop(task_id, None)

    actions: list[dict] = []
    for row in info["stuck"] + info["orphans"]:
        task_id = row["id"]
        if task_id in failed_ids:
            continue
        if task_id in WATCHDOG_DISPATCHED:
            continue
        tries = WATCHDOG_RETRIES.get(task_id, 0) + 1
        WATCHDOG_RETRIES[task_id] = tries
        if tries > 3:
            print(f"[watchdog] {task_id} 已重派 {tries - 1} 次仍无进展，判失败", flush=True)
            store.update(task_id, status="failed", finished_at=time.time(),
                         error="任务多次中断（服务重启/上游限流），请重新提交")
            WATCHDOG_RETRIES.pop(task_id, None)
            continue
        full = store.get(task_id)
        if not full:
            continue
        WATCHDOG_DISPATCHED.add(task_id)
        print(f"[watchdog] {task_id} 卡住无进展（第 {tries} 次重派）", flush=True)
        kind = "resume" if (full.get("conversation_id") and full.get("account")) else "submit"
        actions.append({"kind": kind, "row": full})
    # 派发记录只保留仍在排队的任务，避免集合无限增长
    pending_ids = {row["id"] for row in info["stuck"] + info["orphans"] + info["waiting"]}
    WATCHDOG_DISPATCHED.intersection_update(pending_ids)
    return actions


async def _daily_quota_reset_loop() -> None:
    """每天 LIMIT_RESET_HOUR 点（默认日本时间 00:00）自动恢复全池额度。"""
    first = True
    while True:
        delay = max(30.0, pool.next_quota_reset_at() - time.time())
        if first:
            first = False
            print(
                f"[reset] 每日额度重置已排程：{config.LIMIT_RESET_TZ} "
                f"每天 {config.LIMIT_RESET_HOUR}:00，下次 {delay / 3600:.1f} 小时后",
                flush=True,
            )
        await asyncio.sleep(delay)
        try:
            count = pool.reset_daily_quotas()
            print(
                f"[reset] 每日额度重置完成（{config.LIMIT_RESET_TZ} "
                f"{config.LIMIT_RESET_HOUR}:00），恢复 {count} 个账号",
                flush=True,
            )
        except Exception as exc:
            print(f"[reset] 每日额度重置失败: {exc}", flush=True)
        try:
            await asyncio.to_thread(_maintenance)
        except Exception as exc:
            print(f"[maint] 维护任务失败: {exc}", flush=True)


def _prune_downloads() -> dict:
    """出片目录超过体积上限就按 mtime 从旧到新删，避免把磁盘撑满。"""
    limit = config.DOWNLOAD_MAX_BYTES
    root = Path(config.DOWNLOAD_DIR)
    if limit <= 0 or not root.is_dir():
        return {}
    items = []
    for path in root.iterdir():
        try:
            if path.is_file():
                items.append((path.stat().st_mtime, path.stat().st_size, path))
        except OSError:
            continue
    total = sum(size for _, size, _ in items)
    if total <= limit:
        return {}
    removed = 0
    for _, size, path in sorted(items):
        if total <= limit:
            break
        try:
            path.unlink()
            total -= size
            removed += 1
        except OSError:
            continue
    return {"removed": removed, "total": total} if removed else {}


def _maintenance() -> None:
    """保留期清理：删过期任务记录 + 对应视频文件，再 VACUUM 还盘。

    目的：tasks.db 和 downloads/ 都不能无限增长 —— 2026-09-22 就是库被撑到 587MB
    把面板接口顶成 MemoryError 的。
    """
    removed = store.prune_finished(config.TASK_RETENTION_DAYS)
    files = 0
    for row in removed:
        delete_reference_thumbnails(_task_reference_thumbs(row.get("reference_thumbs")))
        name = (row.get("video_url") or "").rsplit("/", 1)[-1]
        if not name:
            continue
        target = Path(config.DOWNLOAD_DIR) / name
        try:
            if target.is_file():
                target.unlink()
                files += 1
        except OSError:
            continue
    if removed:
        store.vacuum()
    stats = store.storage_stats()
    print(
        "[maint] 清理 %d 条过期任务（保留 %s 天）/ %d 个视频文件，"
        "库 %.1f MB / 共 %d 条" % (len(removed), config.TASK_RETENTION_DAYS, files,
                                   stats["db_bytes"] / 1048576, stats["tasks"]),
        flush=True,
    )
    pruned = _prune_downloads()
    if pruned:
        print("[maint] 出片目录超过 %.0f MB，删除 %d 个旧文件"
              % (config.DOWNLOAD_MAX_BYTES / 1048576, pruned["removed"]), flush=True)
    # 豆包账号墓碑（防「删除复活」）也得定期清，否则 doubao_accounts/.deleted/ 无限增长
    try:
        tombs = doubao_store.cleanup_tombstones()
    except Exception:
        tombs = 0
    if tombs:
        print(f"[maint] 清理 {tombs} 条豆包账号墓碑", flush=True)


@app.post("/v1/videos/generations", response_model=TaskResponse)
async def create_video(request: Request, authorization: str | None = Header(default=None)):
    client = await _auth(authorization)
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        # 画布（infinite-canvas 之类）走 Sora 风格 multipart，字段名和 JSON 路径不一样
        raw = await _raw_from_multipart(request)
    else:
        try:
            raw = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json body")
    _log_image_fields(raw, "openai")
    try:
        req = VideoGenRequest.model_validate(raw)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    # 时长来源优先级：duration → seconds → duration_seconds（new-api 的 sora 插件只发 seconds）。
    duration = req.duration or req.seconds or req.duration_seconds or 10
    ratio = _resolve_ratio(req.size, req.ratio)
    refs = _normalize_image_refs(
        req.reference_images, req.image_url, req.image,
        req.input_image, req.input_images,
    )
    refs = list(dict.fromkeys(refs + _deep_image_urls(raw)))
    if refs:
        print(f"[api] 收到参考图 {len(refs)} 张: {refs[0][:80]}", flush=True)
    return await _submit_task(client, req.model, req.prompt, ratio, duration, refs)


def _data_url_from_upload(filename: str, data: bytes) -> str:
    """画布上传的参考图折成 data: URL —— 下游 validate_reference_urls 本来就支持这种格式。"""
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(suffix, "png")
    return "data:image/%s;base64,%s" % (mime, base64.b64encode(data).decode())


async def _raw_from_multipart(request: Request) -> dict[str, Any]:
    """把 Sora/画布风格的 multipart 表单折成和 JSON 路径同构的 dict。

    - model / prompt 直取；
    - seconds（画布的时长字段，字符串）→ duration；
    - size → 交给 _resolve_ratio 归一；
    - image[] / image / input_image / first_frame / last_frame 文件 → data: URL 进 reference_images；
    - video[] / audio[] 本项目暂不支持，记日志忽略。
    """
    form = await request.form()
    raw: dict[str, Any] = {}
    for key in ("model", "prompt"):
        value = form.get(key)
        if isinstance(value, str) and value.strip():
            raw[key] = value.strip()
    for key in ("duration", "seconds", "duration_seconds"):
        value = form.get(key)
        if isinstance(value, str) and value.strip():
            try:
                raw["duration"] = int(float(value))
                break
            except ValueError:
                continue
    for key in ("size", "ratio", "aspect_ratio"):
        value = form.get(key)
        if isinstance(value, str) and value.strip():
            raw[key] = value.strip()
    refs: list[str] = []
    for key in ("image[]", "image", "input_image", "first_frame", "last_frame"):
        for item in form.getlist(key):
            if hasattr(item, "read"):
                data = await item.read()
                if data:
                    refs.append(_data_url_from_upload(
                        getattr(item, "filename", "") or "", data))
            elif isinstance(item, str) and item.strip():
                refs.append(item.strip())
    ignored = [key for key in ("video[]", "audio[]") if form.getlist(key)]
    if ignored:
        print(f"[api] 画布 multipart 带了 {ignored}，本项目暂不支持参考视频/音频，已忽略", flush=True)
    if refs:
        raw["reference_images"] = refs
    return raw


@app.post("/v1/videos", response_model=TaskResponse, status_code=202)
async def create_video_vinted_alias(request: Request,
                                    authorization: str | None = Header(default=None)):
    """Vinted/New API 兼容入口：与 OpenAI 路径同逻辑，但返回 202 Accepted。"""
    return await create_video(request, authorization)


async def _submit_task(client, model, prompt, ratio, duration, reference_images):
    """公共受理逻辑：校验模型/时长/账号池 -> 入队 -> 后台跑，返回 TaskResponse。"""
    resolved = _resolve_model_for_duration(model, duration)
    if resolved is None:
        version = _model_version(model)
        if version is not None:
            supported = _supported_durations_for(version)
            raise HTTPException(
                422,
                f"{version} 仅支持 {'/'.join(map(str, supported))} 秒视频"
                f"（收到 {duration} 秒）",
            )
        raise HTTPException(
            422,
            "不支持的模型/时长组合："
            f"模型={model!r}，时长={duration}秒"
            f"（seedance-2.5 支持 {_supported_durations_for('seedance-2.5')} 秒，"
            f"seedance-2.0 仅支持 {list(config.V20_DURATIONS)} 秒）",
        )
    model = resolved
    if duration not in client["allowed_durations"]:
        raise HTTPException(422, f"当前 API Key 不允许生成 {duration} 秒视频")
    try:
        reference_images = await validate_reference_urls(reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # data: URL（客户端内联的参考图）先落盘：几 MB 的 base64 绝不能进 tasks.db，
    # 否则任务列表接口要序列化几百 MB，直接把进程顶成 MemoryError / 卡死。
    try:
        reference_images = await asyncio.to_thread(materialize_data_urls, reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # 账号暂时忙时允许任务进入 queued，由 BrowserPool 的全局并发控制实际排队。
    # 只有明确全部达到每日上限/积分不足时才立即拒绝。
    if not pool.available and pool.all_accounts_limited:
        raise HTTPException(429, "账号限流：所有已开启调度的账号均已达到 Dola 每日视频上限，请明天再试")
    if not pool.available and pool.all_accounts_quota_blocked:
        raise HTTPException(429, "积分不足：所有已开启调度的账号都没有足够积分，请等待额度刷新")
    if not pool.accounts:
        raise HTTPException(503, "no account in pool")
    task_id = "video_" + uuid.uuid4().hex
    if len(prompt or "") > config.MAX_PROMPT_CHARS:
        raise HTTPException(
            422, f"提示词过长（上限 {config.MAX_PROMPT_CHARS} 字符，收到 {len(prompt)}）")
    try:
        store.create(
            task_id,
            model,
            prompt,
            ratio or "default",
            duration,
            reference_images=json.dumps(reference_images, ensure_ascii=False),
            api_key_hash=client["api_key_hash"],
            api_key_name=client["api_key_name"],
            daily_limit=client["daily_limit"],
            concurrency_limit=client["concurrency_limit"],
            max_pending=config.MAX_PENDING_TASKS,
        )
    except TaskQuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except PendingTaskLimitExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    asyncio.create_task(_run_task(
        task_id, model, prompt, ratio, duration, reference_images, client
    ))
    return TaskResponse(
        id=task_id, status="queued", model=model, prompt=prompt,
        progress=_progress_payload(status="queued", created_at=time.time(),
                                   duration=duration),
    )


@app.get("/v1/videos/{task_id}", response_model=TaskResponse)
async def get_video(task_id: str, authorization: str | None = Header(default=None)):
    client = await _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row:
        raise HTTPException(404, "task not found")
    return TaskResponse(
        id=row["id"], status=row["status"], model=row["model"],
        prompt=row["prompt"], video_url=row["video_url"], error=row["error"],
        progress=_progress_payload(status=row["status"], created_at=row["created_at"],
                                   duration=row["duration"]),
    )



@app.get("/v1/videos/{task_id}/content")
async def get_video_content(task_id: str, authorization: str | None = Header(default=None)):
    """new-api 任务插件兼容端点：把已完成的视频文件内容返回给客户端。"""
    client = await _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row:
        raise HTTPException(404, "task not found")
    if row["status"] != "completed" or not row["video_url"]:
        raise HTTPException(409, "video not ready")
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(row["video_url"]) as resp:
            if resp.status != 200:
                raise HTTPException(502, "failed to fetch video from upstream")
            content_type = resp.headers.get("Content-Type", "video/mp4")
            data = await resp.read()
    return Response(content=data, media_type=content_type)



@app.get("/v1/models")
async def list_models():
    """OpenAI 风格模型列表，供 new-api 等上游获取模型用。"""
    return {
        "object": "list",
        "data": [
            {"id": "seedance-2.0", "object": "model", "owned_by": "dola-pool", "created": 0},
            {"id": "seedance-2.5", "object": "model", "owned_by": "dola-pool", "created": 0},
        ],
    }


# ===== 火山 Ark 任务式协议兼容端点（画布工具） =====

ARK_STATUS_MAP = {
    "queued": "queued",
    "processing": "running",
    "completed": "succeeded",
    "failed": "failed",
}


async def ark_create_task(body: dict, authorization: str | None = Header(default=None)):
    """POST .../contents/generations/tasks：Ark 风格创建，复用 dola 出片流程。"""
    client = await _auth(authorization)
    body = body or {}
    _log_image_fields(body, "ark")
    prompt_parts: list[str] = []
    reference_images: list[str] = []
    for item in body.get("content") or []:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").lower()
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if text:
                prompt_parts.append(text)
        elif item_type in ("image_url", "image"):
            url_obj = item.get("image_url")
            url = url_obj.get("url") if isinstance(url_obj, dict) else url_obj
            if isinstance(url, str) and url.strip():
                reference_images.append(url.strip())
    # 顶层兼容：部分画布工具把图片放在顶层 image_url/image 字段
    reference_images = _normalize_image_refs(
        reference_images, body.get("image_url"), body.get("image"),
        body.get("input_image"), body.get("input_images"),
    )
    reference_images = list(dict.fromkeys(reference_images + _deep_image_urls(body)))
    prompt = "\n".join(prompt_parts).strip()
    if not prompt:
        raise HTTPException(422, "content 中缺少 text 提示词")
    requested_model = body.get("model")
    ratio = _map_ark_ratio(body.get("ratio"))
    if reference_images:
        print(f"[api] Ark 收到参考图 {len(reference_images)} 张: {reference_images[0][:80]}", flush=True)
    version = _model_version(requested_model)
    if version is not None:
        # 显式指定了版本：只在该版本支持的时长里就近取档。
        allowed = [
            d for d in client["allowed_durations"]
            if d in _supported_durations_for(version)
        ]
    else:
        # 模型名无法识别：先按请求时长取档，再由时长挑一个支持的 seedance 版本。
        allowed = [
            d for d in client["allowed_durations"]
            if d in SUPPORTED_DURATIONS
        ]
    # 部分画布工具用 seconds 传时长（new-api 的 sora 插件也会改写成 seconds）。
    duration = _pick_duration(body.get("duration") or body.get("seconds"), allowed)
    model = _resolve_model_for_duration(requested_model, duration)
    if model is None:
        raise HTTPException(
            422,
            "不支持的模型/时长组合："
            f"模型={requested_model!r}，时长={duration}秒"
            f"（seedance-2.5 支持 {_supported_durations_for('seedance-2.5')} 秒，"
            f"seedance-2.0 仅支持 {list(config.V20_DURATIONS)} 秒）",
        )
    resp = await _submit_task(client, model, prompt, ratio, duration, reference_images)
    return {"id": resp.id, "status": resp.status}


async def ark_get_task(task_id: str, authorization: str | None = Header(default=None)):
    """GET .../contents/generations/tasks/{id}：Ark 风格查询。"""
    client = await _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row:
        raise HTTPException(404, "task not found")
    result = {
        "id": row["id"],
        "model": row["model"],
        "status": ARK_STATUS_MAP.get(row["status"], row["status"]),
        "created_at": int(row["created_at"]),
        "updated_at": int(row.get("updated_at") or row["created_at"]),
    }
    if row["status"] == "completed" and row.get("video_url"):
        result["content"] = {"video_url": row["video_url"]}
    if row.get("error"):
        result["error"] = row["error"]
    return result


ARK_TASK_PATHS = (
    "/v1/video/contents/generations/tasks",
    "/api/v3/contents/generations/tasks",
    "/v1/contents/generations/tasks",
    "/seedance/v3/contents/generations/tasks",
)
for _ark_path in ARK_TASK_PATHS:
    app.add_api_route(_ark_path, ark_create_task, methods=["POST"])
    app.add_api_route(_ark_path + "/{task_id}", ark_get_task, methods=["GET"])


@app.get("/health")


async def health():
    meta = pool.engine_snapshot()
    storage = store.storage_stats()
    downloads = 0
    download_root = Path(config.DOWNLOAD_DIR)
    if download_root.is_dir():
        for item in download_root.iterdir():
            try:
                if item.is_file():
                    downloads += item.stat().st_size
            except OSError:
                continue
    storage["downloads_bytes"] = downloads
    storage["retention_days"] = config.TASK_RETENTION_DAYS
    queue_info = store.queue_progress(created_at=None, duration=None)
    workers = int(config.MAX_CONCURRENCY or 0)   # 0 = 不限并发
    queue = {
        "queued": store.pending_task_count() - queue_info["running"],
        "running": queue_info["running"],
        "workers": workers,
        "typical_seconds": queue_info["typical_seconds"],
        "eta_text": _eta_text(queue_info["typical_seconds"]),
    }
    return {
        "ok": True,
        "version": APP_VERSION,
        "accounts": pool.account_status(),
        "pool": meta,
        "storage": storage,
        "queue": queue,
        "available": pool.available,
        "pending_tasks": store.pending_task_count(),
        "max_pending_tasks": config.MAX_PENDING_TASKS,
    }


# ===== 管理面板 API =====


class AdminLogin(BaseModel):
    username: str
    password: str


class AdminUserCreate(BaseModel):
    username: str
    password: str
    role: str = "admin"


class AdminUserUpdate(BaseModel):
    username: str | None = None
    password: str | None = None
    enabled: bool | None = None


class ProxyCreate(BaseModel):
    name: str
    protocol: str = "http"
    # 动态IP（mode="extract"）不填网关地址也行：出口由节点池里的节点决定
    host: str = ""
    port: int = Field(0, ge=0, le=65535)
    username: str = ""
    password: str = ""
    remark: str = ""
    # static = 一条记录一个固定出口；dynamic = 隧道/旋转网关，按 username 里的
    # sticky session 决定出口 IP，每个号自动分配一个独立 session。
    mode: str = "static"
    session_template: str = ""
    sticky_ttl: int = Field(0, ge=0)
    rotate_on_risk: bool = True
    rotate_min_interval: int = Field(0, ge=0)
    # mode="extract" 时用：调这个链接拿一批可直接使用的 host:port:user:pass
    extract_url: str = ""
    extract_interval: int = Field(0, ge=0)
    # mode="extract" 时也可以直接把节点粘进来（一行一个 host:port:user:pass），
    # 与提取链接二选一即可，都填就先建代理再把粘进来的节点入池。
    nodes_text: str = ""


class ProxyUpdate(BaseModel):
    name: str | None = None
    protocol: str | None = None
    host: str | None = None
    port: int | None = Field(None, ge=0, le=65535)
    username: str | None = None
    password: str | None = None
    remark: str | None = None
    enabled: bool | None = None
    mode: str | None = None
    session_template: str | None = None
    sticky_ttl: int | None = Field(None, ge=0)
    rotate_on_risk: bool | None = None
    rotate_min_interval: int | None = Field(None, ge=0)
    extract_url: str | None = None
    extract_interval: int | None = Field(None, ge=0)


class ProxyNodesIn(BaseModel):
    # 手工粘贴的节点：一行一个 host:port:user:pass（兼容 user:pass@host:port 等）
    text: str = ""
    # 有效期（秒）：None = 带 sessiontime-N 的按它算，其余默认不过期；0 = 全部不过期
    ttl: int | None = None


class AccountProxyBind(BaseModel):
    proxy_id: str | None = None


class ProxySetDefault(BaseModel):
    # 设为池默认代理的 id；None/空 = 取消默认
    proxy_id: str | None = None


class StressRequest(BaseModel):
    max_concurrency: int = Field(8, ge=1, le=12)
    hold_seconds: float = Field(5.0, ge=3.0, le=15.0)


class AccountPatch(BaseModel):
    scheduling: bool | None = None
    note: str | None = None
    email: str | None = None


class RecoverBody(BaseModel):
    """人工恢复：kind=risk（风控组）| abnormal（异常组）；批量恢复时带 names。"""

    kind: str = "risk"
    names: list[str] = []


class PreferBody(BaseModel):
    preferred: bool = True


class WeightBody(BaseModel):
    weight: int = Field(1, ge=1, le=100)


class BindProxiesBody(BaseModel):
    proxies: list[str]
    force: bool = False
    mode: str = "sticky"


class BatchAccountsBody(BaseModel):
    """批量操作（勾选账号后一键处理）。"""
    names: list[str] = Field(default_factory=list)


class BatchProxyBody(BaseModel):
    names: list[str] = Field(default_factory=list)
    proxy_id: str | None = None


class ImportTextBody(BaseModel):
    text: str = ""
    skip_verify: bool = False
    # 留空 = 用代理管理里的池默认代理（bind_default_proxy 兜底）；非空 = 显式绑定该代理
    proxy_id: str = ""


class ExportCookiesBody(BaseModel):
    """批量导出 CK（打成 ZIP，一个账号一个文件）。names 为空 = 导出全部账号。"""
    names: list[str] = Field(default_factory=list)
    # json / bundle / header / netscape（见 cookie_export）；默认 json = dola-pool 2.1.4 弹窗可直接粘贴
    format: str = "json"


class RegisterBody(BaseModel):
    """自动注册参数（register_service：发码->收码->注册->自动导入号池）。"""
    proxy: str = ""                # 留空 = 回落 config.PROXY（.env.local 的 IPRocket JP）
    mail_source: str = "tempmail"  # tempmail=公开临时邮箱；outlook=现造 Outlook；outlook_pool=已有 Outlook 邮箱池
    email_prefix: str = ""         # 留空 = 随机前缀；非空时按 prefix001/002 递增
    domain: str = "mailto.plus"    # mail_source=tempmail 时的邮箱域名
    count: int = Field(1, ge=1, le=100)
    concurrency: int = Field(1, ge=1, le=8)  # 并发 worker 数（收码等待并行，入池仍串行）
    gap: int = Field(5, ge=0, le=600)      # 同一 worker 相邻任务之间的间隔秒
    wait: int = Field(90, ge=10, le=600)   # 收码超时秒
    set_password: bool = False
    password: str = ""
    auto_verify: bool = True       # 导入时做登录态验证（失败也入池，标记未验证）
    # —— mail_source=outlook 专用（留空走 .env.local 的 DOLA_OUTLOOK_* 兜底）——
    outlook_country: str = ""      # 注册国家，须与代理出口一致（默认 US）
    outlook_domain: str = ""       # 邮箱后缀（默认 @outlook.com）
    outlook_token_mode: str = ""   # graph / outlook_rest / login_exe（默认 graph）
    outlook_px_mode: str = ""      # solver / local / offcaptcha（默认 solver=captcha.run）
    outlook_skip_proofs: bool = True  # 跳过 proofs 恢复邮箱绑定（本用途无需微软恢复邮箱）


class TakeGoogleBody(BaseModel):
    """面板「取号 · 谷歌注册」参数：走 google_register 骨架（邮箱收码 -> 建号 -> TOTP）。"""
    count: int = Field(1, ge=1, le=20)
    domain: str = "mailto.plus"    # tempmail 后端时：Google 必须接受该域（mailto.plus 实测可用）
    prefix_len: int = Field(8, ge=4, le=20)
    password: str = ""             # 留空 = 每号随机强密码
    want_totp: bool = True         # 注册后尝试绑 2FA 抓 TOTP
    headless: bool = True          # camoufox 无头安全；patchright 会被后端自动切成有头
    engine: str = "camoufox"       # camoufox（Firefox 引擎级反检测，推荐）/ patchright
    proxy_id: str = ""             # 留空 = 面板「代理管理」的池默认代理（住宅）
    step_wait_ms: int = Field(2500, ge=500, le=10000)
    # —— 收件后端（写进 MAIL_* 环境，mail_catcher 从 env 读）——
    mail_backend: str = "tempmail"  # tempmail / imap / http
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: str = ""
    imap_box: str = "INBOX"
    http_url: str = ""
    http_token: str = ""


class TakeWebloginBody(BaseModel):
    """面板「取号 · 网页登录取 dola 号」参数：Google 号 -> dola 真网页登录 -> 自动入池。"""
    accounts_text: str = ""        # 粘贴 email----password----totp（一行一个）；留空则读号池文件
    source_file: str = ""          # 号池文件路径；留空 = 谷歌账号_产出.txt / 谷歌账号.txt
    limit: int = Field(0, ge=0, le=100)   # 0 = 不限制
    proxy_id: str = ""             # 留空 = 池默认代理（住宅）；每个号自动换一个新出口
    headless: bool = True
    verify: bool = True            # 导入后验证 dola 登录态
    name_prefix: str = "acc"


class TakeGloginBody(BaseModel):
    """面板「取号 · Google 渠道」参数：账号|密码|取件地址 -> Google 官网登录 -> dola Google 登录入池。

    取件地址 = 邮件 API 基址（如 https://api.vf9p.us），
    收码接口 GET {base}/v1.php?email=<账号>&action=otp；Google 不要求验证码时可留空。
    """
    accounts_text: str = ""        # 账号|密码|取件地址，一行一个
    limit: int = Field(0, ge=0, le=100)   # 0 = 不限制
    proxy_id: str = ""             # 留空 = 池默认代理（住宅）；每个号自动换一个新出口
    verify: bool = True            # 导入后验证 dola 登录态
    name_prefix: str = "acc"


class VideoPoolBody(BaseModel):
    """视频池参数（video_pool_service：连跑注册→2.5 首提→风控弃号→守望取片）。"""
    orders: int = Field(1, ge=1, le=50)          # 目标出片数
    max_accounts: int = Field(0, ge=0, le=200)   # 最多尝试注册数；0=orders*4
    prompt: str = "一只柴犬在樱花树下奔跑，镜头跟随"
    watch_hours: float = Field(2.0, ge=0.5, le=6)  # 单号出片守望上限（小时）
    proxy: str = ""                # 留空 = socks5://127.0.0.1:7897
    domain: str = ""               # 邮箱域；留空 = mailto.plus（可选 fexpost.com 等）


class AccountAdd(BaseModel):
    name: str
    email: str = ""
    password: str = ""
    totp: str = ""
    cookies: str = ""
    cookie_skip_verify: bool = False
    # 留空 = 用代理管理里的池默认代理（bind_default_proxy 兜底）；非空 = 显式绑定该代理
    proxy_id: str = ""


class KeyCreate(BaseModel):
    name: str = ""
    daily_limit: int = Field(0, ge=0, le=1_000_000)
    concurrency_limit: int = Field(0, ge=0, le=1_000)
    allowed_durations: list[int] = Field(default_factory=lambda: list(SUPPORTED_DURATIONS))
    expires_at: float | None = Field(None, ge=0)


class KeyPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    daily_limit: int | None = Field(None, ge=0, le=1_000_000)
    concurrency_limit: int | None = Field(None, ge=0, le=1_000)
    allowed_durations: list[int] | None = None
    expires_at: float | None = Field(None, ge=0)


@app.post("/api/admin/login")
async def admin_login(body: AdminLogin):
    user = await asyncio.to_thread(user_verify, body.username.strip(), body.password)
    if not user:
        raise HTTPException(401, "用户名或密码错误")
    token = secrets.token_urlsafe(32)
    ADMIN_SESSIONS[token] = {
        "username": user["username"],
        "role": user["role"],
        "expires_at": time.time() + ADMIN_SESSION_TTL,
    }
    return {
        "ok": True,
        "auth_required": True,
        "token": token,
        "username": user["username"],
        "role": user["role"],
    }


@app.get("/api/admin/accounts")
async def admin_accounts(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    busy_accounts = await asyncio.to_thread(store.busy_account_ids)
    accounts = pool.list_accounts(processing_accounts=busy_accounts)
    sessions = proxy_store.sessions_snapshot()
    for acc in accounts:
        info = get_account_proxy(acc["name"])
        acc["proxy_id"] = info["id"] if info else None
        acc["proxy_name"] = info["name"] if info else None
        acc["proxy_mode"] = info.get("mode") if info else None
        sess = sessions.get(acc["name"]) or {}
        acc["ip_session"] = sess.get("session", "")
        acc["exit_ip"] = sess.get("last_exit_ip", "")
        acc["exit_probe_at"] = sess.get("last_probe_at", 0)
        acc["exit_probe_ok"] = bool(sess.get("last_probe_ok"))
        acc["exit_probe_detail"] = sess.get("last_probe_detail", "")
    return {
        "accounts": accounts,
        "engine": pool.engine_snapshot(),
        "next_reset_at": pool.next_quota_reset_at(),
        "reset_tz": config.LIMIT_RESET_TZ,
        "reset_hour": config.LIMIT_RESET_HOUR,
        **_group_summary(accounts),
    }


def _group_summary(accounts: list) -> dict:
    """分组计数 + 【正常/有效】组的剩余总额度（面板按钮与「剩余额度」用）。"""
    counts = {name: 0 for name in GROUP_ORDER}
    for acc in accounts:
        group = acc.get("group")
        if group in counts:
            counts[group] += 1
    return {
        "groups": counts,
        "group_order": list(GROUP_ORDER),
        "all_count": len(accounts),
        "valid_count": sum(counts[g] for g in ACTIVE_GROUPS),
        "remaining_points": sum(
            int(acc.get("remaining") or 0) for acc in accounts
            if acc.get("group") in ACTIVE_GROUPS),
    }


def _running_add_jobs() -> list[str]:
    return [name for name, job in JOBS.items()
            if job.get("status") == "running"]


async def _run_stress_job(max_concurrency: int, hold_seconds: float):
    try:
        result = await run_stress(max_concurrency, hold_seconds)
        STRESS_RUN.update(
            status="completed",
            finished_at=time.time(),
            recommended=result["recommended"],
            results=result["results"],
            logs=result["logs"],
        )
    except Exception as exc:
        STRESS_RUN.update(
            status="failed",
            finished_at=time.time(),
            logs=STRESS_RUN.get("logs", []) + [f"压测异常: {exc}"],
        )


@app.get("/api/admin/stress")
async def admin_stress_status(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {
        **STRESS_RUN,
        "busy_tasks": await asyncio.to_thread(store.pending_task_count),
        "running_jobs": len(_running_add_jobs()),
    }


@app.post("/api/admin/stress")
async def admin_stress_start(body: StressRequest,
                             x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if STRESS_RUN.get("status") == "running":
        raise HTTPException(409, "压力测试正在进行中，请等待完成")
    busy = await asyncio.to_thread(store.pending_task_count)
    if busy:
        raise HTTPException(
            409, f"当前有 {busy} 个视频任务在排队/运行，请先等待任务结束再压测"
        )
    adding = _running_add_jobs()
    if adding:
        raise HTTPException(
            409, "当前有账号登录任务进行中：" + "、".join(adding[:5])
        )
    STRESS_RUN.clear()
    STRESS_RUN.update(
        status="running",
        started_at=time.time(),
        finished_at=None,
        recommended=None,
        results=[],
        logs=["压力测试开始：从 1 个浏览器逐级加压…"],
    )
    asyncio.create_task(_run_stress_job(body.max_concurrency, body.hold_seconds))
    return {**STRESS_RUN, "busy_tasks": 0, "running_jobs": 0}


@app.patch("/api/admin/accounts/{name}")
async def admin_account_patch(name: str, body: AccountPatch,
                              x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if body.scheduling is not None:
        pool.set_scheduling(name, body.scheduling)
    if body.note is not None:
        pool.set_note(name, body.note)
    if body.email is not None:
        pool.set_email(name, body.email)
    return {"ok": True}


async def _account_row(name: str) -> dict | None:
    """单个账号的面板视图（含分组），用于恢复/探测后回显新状态。"""
    busy_accounts = await asyncio.to_thread(store.busy_account_ids)
    return next((a for a in pool.list_accounts(processing_accounts=busy_accounts)
                 if a["name"] == name), None)


@app.post("/api/admin/accounts/{name}/recover")
async def admin_account_recover(name: str, body: RecoverBody,
                                x_admin_key: str | None = Header(default=None)):
    """把号从【风控】/【异常】组放回正常流程（风控恢复后回到【待激活】，要重新探测）。"""
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if body.kind not in ("risk", "abnormal"):
        raise HTTPException(400, "kind 只能是 risk 或 abnormal")
    result = pool.recover_account(name, body.kind)
    return {**result, "account": await _account_row(name)}


@app.post("/api/admin/accounts/{name}/probe-hello")
async def admin_account_probe_hello(name: str,
                                    x_admin_key: str | None = Header(default=None)):
    """手动发一句「你好」判风控（强制真探，不吃缓存），返回回复原文与登录标记。"""
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    probe = await pool.probe_hello_async(name, force=True)
    return {"ok": True, "probe": probe, "account": await _account_row(name)}


class RenameAccount(BaseModel):
    new_name: str


@app.post("/api/admin/accounts/{name}/rename")
async def admin_account_rename(name: str, body: RenameAccount,
                               x_admin_key: str | None = Header(default=None)):
    """给账号改名（profile 目录、代理绑定、今日额度、登录凭据一起搬）。"""
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    try:
        result = pool.rename_account(name, body.new_name)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, **result}


@app.delete("/api/admin/accounts/{name}")
async def admin_account_delete(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    try:
        # delete_account 内含 shutil.rmtree（profile 目录可达上千个文件，本机实测
        # ~44ms/文件）——同步执行会把事件循环冻结数十秒，面板全部请求排队。
        # browser_pool 的 _conn 是 check_same_thread=False，可安全进线程。
        await asyncio.to_thread(pool.delete_account, name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/admin/accounts/{name}/verify")
async def admin_account_verify(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        result = await _verify_account_once(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        # 验证流程里浏览器/playwright/SQLite 等其它异常不再冒泡成 500：
        # 统一转 409 并给出原因（不落地 login_ok，避免误标，非破坏）。
        raise HTTPException(409, f"验证失败（服务器异常）: {str(e)[:200]}")
    return result


async def _verify_account_once(name: str) -> dict:
    """单账号验证（登录态失效时用加密凭据自动重登一次）。

    返回 {"ok": bool, "refreshed": bool, "reason": str, "soft": bool}；账号忙 / 不存在等异常向上抛。
    reason 是失败原因（成功时为空串），面板用它显示「为什么失效」。
    soft=True 表示网络软失败（探活没跑成）：login_ok 未动，只记原因，不算「失效」。
    """
    ok, reason, soft = await pool.verify_account_detail(name)
    refreshed = False
    if not ok and not soft:
        creds = cred_store.load(name)
        if creds and creds.get("password"):
            try:
                async with ADD_ACCOUNT_SEM:
                    await add_account_flow(
                        name, creds["email"], creds["password"], creds.get("totp") or "",
                        force_login=True)
                pool.set_email(name, creds["email"])
                pool.set_login_status(name, True)
                ok, reason, soft = await pool.verify_account_detail(name)
                refreshed = True
            except Exception as exc:
                ok = False
                refreshed = False
                reason = f"自动重登失败: {str(exc)[:160]}"
    return {"ok": ok, "refreshed": refreshed, "reason": reason, "soft": soft}


def _clean_batch_names(names: list[str]) -> list[str]:
    """去空、去重、保持勾选顺序；上限 500 个防止误提交超大请求。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out[:500]


async def _run_batch_verify_job(job_id: str, names: list[str]) -> None:
    """逐个验证勾选的账号，进度写回 BATCH_JOBS 供面板轮询。"""
    job = BATCH_JOBS[job_id]
    try:
        async with BATCH_VERIFY_SEM:
            for name in names:
                job["current"] = name
                item = {"name": name, "ok": None, "message": ""}
                try:
                    if name not in pool.accounts:
                        item["message"] = "账号不存在"
                    else:
                        result = await _verify_account_once(name)
                        item["ok"] = bool(result["ok"])
                        if result.get("soft"):
                            # 网络软失败：login_ok 没动，不能算「失效」
                            item["ok"] = None
                            item["message"] = f"未判定（网络）：{result.get('reason') or '探活未跑通'}"
                        else:
                            item["message"] = "有效" if item["ok"] else "失效"
                            if result["refreshed"]:
                                item["message"] += "（已自动重登）"
                except Exception as exc:
                    item["message"] = str(exc)[:200] or "验证失败"
                job["results"].append(item)
                job["done"] = len(job["results"])
                job["ok_count"] = sum(1 for r in job["results"] if r["ok"])
                job["failed_count"] = sum(1 for r in job["results"] if r["ok"] is False)
        job["status"] = "completed"
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)[:300]
    finally:
        job["current"] = ""
        job["finished_at"] = time.time()


async def _run_batch_probe_job(job_id: str, names: list[str]) -> None:
    """批量「你好」探测：并发受 BATCH_PROBE_SEM 限制，进度写回 BATCH_JOBS 供面板轮询。

    结论口径（与单号探测一致）：
      ok         → 登录有效且有回复；
      logged_out → 被登出 → 该号进【风控】组；
      no_reply   → 一句回复都没有 → 进【风控】组；
      error      → 探测没跑成（网络不通 / 上游 710022002 拒绝）→ **不锁号**，只记"未完成"；
      skipped    → 非纯 API 账号（浏览器登录号）。
    """
    job = BATCH_JOBS[job_id]
    # 并发 0 = 不限（全部放开）；>0 才建信号量。
    # 信号量在协程内部创建：模块级 asyncio 原语会绑死在"第一个"事件循环上。
    sem = (asyncio.Semaphore(BATCH_PROBE_CONCURRENCY)
           if BATCH_PROBE_CONCURRENCY > 0 else _NoLimit())
    label = {
        "ok": "通过",
        "logged_out": "被登出 → 已进【风控】组",
        "no_reply": "没回复 → 已进【风控】组",
        "error": "探测未完成（网络/上游拒绝，不判风控）",
        "skipped": "非纯 API 账号，跳过",
    }

    async def probe_one(name: str) -> None:
        item = {"name": name, "ok": None, "status": "", "message": "", "reply": "",
                "group": "", "attempts": 0}
        try:
            if name not in pool.accounts:
                item["message"] = "账号不存在"
            else:
                async with sem:
                    job["current"] = name
                    await asyncio.sleep(BATCH_PROBE_INTERVAL_SECONDS)   # 错峰，别打爆上游
                    # 第一次只记结论、不判风控（单次"没回复"多半是上游抖动，一次就锁会误封好号）
                    result = await pool.probe_hello_async(name, force=True, apply_risk=False)
                    item["attempts"] = 1
                    if result.get("status") == "logged_out":
                        # 「被登出」是强信号，一次就判风控（重试也救不回来）
                        pool.apply_probe_risk(name, result)
                    elif result.get("status") in ("error", "no_reply"):
                        # 上游限流/网络抖动 / 没回复 → 退避重试一次，仍失败才判风控（两振出局）
                        await asyncio.sleep(BATCH_PROBE_RETRY_SECONDS)
                        result = await pool.probe_hello_async(name, force=True, apply_risk=True)
                        item["attempts"] = 2
                item["status"] = str(result.get("status") or "")
                item["ok"] = bool(result.get("ok"))
                item["reply"] = str(result.get("reply") or "")[:40]
                item["message"] = label.get(item["status"], str(result.get("reason") or "")[:120])
        except Exception as exc:
            item["message"] = str(exc)[:200] or "探测失败"
        row = await _account_row(name)
        item["group"] = (row or {}).get("group", "")
        job["results"].append(item)
        job["done"] = len(job["results"])
        job["ok_count"] = sum(1 for r in job["results"] if r["ok"])
        # 只有"硬信号"才算未通过（进风控）；软失败（网络/上游拒绝）单列成"未完成"，
        # 免得把一批限流噪音显示成"一堆号被风控"。
        job["failed_count"] = sum(
            1 for r in job["results"] if r.get("status") in ("logged_out", "no_reply"))
        job["skipped_count"] = sum(
            1 for r in job["results"] if r.get("status") in ("error", "skipped"))

    try:
        await asyncio.gather(*(probe_one(n) for n in names))
        job["status"] = "completed"
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)[:300]
    finally:
        job["current"] = ""
        job["finished_at"] = time.time()


async def _run_batch_recover_job(job_id: str, names: list[str], kind: str) -> None:
    """批量把号从【风控】/【异常】组放回正常流程（风控恢复后退回【待激活】）。"""
    job = BATCH_JOBS[job_id]
    for name in names:
        item = {"name": name, "ok": None, "message": "", "group": ""}
        try:
            if name not in pool.accounts:
                item["message"] = "账号不存在"
            else:
                pool.recover_account(name, kind)
                item["ok"] = True
                item["message"] = ("已退出风控，回到【待激活】——要再探一次「你好」确认登录"
                                   if kind == "risk" else "已退出异常组")
        except Exception as exc:
            item["ok"] = False
            item["message"] = str(exc)[:200] or "恢复失败"
        row = await _account_row(name)
        item["group"] = (row or {}).get("group", "")
        job["results"].append(item)
        job["done"] = len(job["results"])
        job["ok_count"] = sum(1 for r in job["results"] if r["ok"])
        job["failed_count"] = sum(1 for r in job["results"] if r["ok"] is False)
    job["status"] = "completed"
    job["current"] = ""
    job["finished_at"] = time.time()


@app.post("/api/admin/accounts/batch-recover")
async def admin_batch_recover(body: RecoverBody,
                              x_admin_key: str | None = Header(default=None)):
    """批量恢复：把选中的号从【风控】/【异常】组放出来。

    风控恢复后登录态会被清空（回到【待激活】），所以建议接着点一次「你好探测」确认登录。
    """
    _admin_auth(x_admin_key)
    kind = body.kind or "risk"
    if kind not in ("risk", "abnormal"):
        raise HTTPException(400, "kind 只能是 risk 或 abnormal")
    names = _clean_batch_names(body.names)
    if not names:
        raise HTTPException(400, "没有要恢复的账号（先勾选，或用分组按钮筛出一批）")
    running = [jid for jid, item in BATCH_JOBS.items() if item.get("status") == "running"]
    if running:
        raise HTTPException(409, "已有批量任务在进行中，请等待完成")
    job_id = "batch_" + uuid.uuid4().hex[:12]
    BATCH_JOBS[job_id] = {
        "kind": "recover",
        "status": "running",
        "total": len(names),
        "done": 0,
        "ok_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "results": [],
        "current": "",
        "started_at": time.time(),
        "finished_at": None,
        "error": "",
    }
    asyncio.create_task(_run_batch_recover_job(job_id, names, kind))
    return {"ok": True, "job_id": job_id, "total": len(names), "names": names}


@app.post("/api/admin/accounts/batch-probe-hello")
async def admin_batch_probe_hello(body: BatchAccountsBody,
                                  x_admin_key: str | None = Header(default=None)):
    """批量发「你好」探风控：被登出 / 没回复 → 进【风控】组；软失败不锁号。"""
    _admin_auth(x_admin_key)
    names = _clean_batch_names(body.names)
    if not names:
        raise HTTPException(400, "没有要探测的账号（先勾选，或用分组按钮筛出一批）")
    running = [jid for jid, item in BATCH_JOBS.items() if item.get("status") == "running"]
    if running:
        raise HTTPException(409, "已有批量任务在进行中，请等待完成")
    job_id = "batch_" + uuid.uuid4().hex[:12]
    BATCH_JOBS[job_id] = {
        "kind": "probe",
        "status": "running",
        "total": len(names),
        "done": 0,
        "ok_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "results": [],
        "current": "",
        "started_at": time.time(),
        "finished_at": None,
        "error": "",
    }
    asyncio.create_task(_run_batch_probe_job(job_id, names))
    return {"ok": True, "job_id": job_id, "total": len(names), "names": names}


@app.post("/api/admin/accounts/batch-verify")
async def admin_batch_verify(body: BatchAccountsBody,
                             x_admin_key: str | None = Header(default=None)):
    """一键验证：对勾选的账号逐个验证登录态（后台任务，面板轮询进度）。"""
    _admin_auth(x_admin_key)
    names = _clean_batch_names(body.names)
    if not names:
        raise HTTPException(400, "请先勾选账号")
    running = [jid for jid, job in BATCH_JOBS.items() if job.get("status") == "running"]
    if running:
        raise HTTPException(409, "已有批量验证在进行中，请等待完成")
    job_id = "batch_" + uuid.uuid4().hex[:12]
    BATCH_JOBS[job_id] = {
        "kind": "verify",
        "status": "running",
        "total": len(names),
        "done": 0,
        "ok_count": 0,
        "failed_count": 0,
        "results": [],
        "current": "",
        "started_at": time.time(),
        "finished_at": None,
        "error": "",
    }
    asyncio.create_task(_run_batch_verify_job(job_id, names))
    return {"ok": True, "job_id": job_id, "total": len(names), "names": names}


@app.get("/api/admin/batch/{job_id}")
async def admin_batch_status(job_id: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    job = BATCH_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "批量任务不存在或已过期")
    return job


@app.post("/api/admin/accounts/batch-proxy")
async def admin_batch_proxy(body: BatchProxyBody,
                            x_admin_key: str | None = Header(default=None)):
    """一键更改代理：把勾选账号统一绑定到指定代理（proxy_id 为空 = 恢复默认代理）。"""
    _admin_auth(x_admin_key)
    names = _clean_batch_names(body.names)
    if not names:
        raise HTTPException(400, "请先勾选账号")
    if body.proxy_id and body.proxy_id not in {p["id"] for p in list_proxies()}:
        raise HTTPException(400, "代理不存在")
    busy = {acc["name"] for acc in pool.list_accounts() if acc.get("busy")}
    updated: list[str] = []
    failed: list[dict] = []
    for name in names:
        if name not in pool.accounts:
            failed.append({"name": name, "error": "账号不存在"})
            continue
        if name in busy:
            failed.append({"name": name, "error": "账号正在出片，未改动"})
            continue
        try:
            set_account_proxy(name, body.proxy_id)
            updated.append(name)
        except Exception as exc:
            failed.append({"name": name, "error": str(exc)[:150]})
    return {"ok": True, "updated": updated, "failed": failed,
            "proxy_id": body.proxy_id}


@app.post("/api/admin/accounts/reset-quotas")
async def admin_reset_quotas(x_admin_key: str | None = Header(default=None)):
    """手动触发一次额度重置（与每天 LIMIT_RESET_HOUR 点自动重置同一套逻辑）。"""
    _admin_auth(x_admin_key)
    count = pool.reset_daily_quotas()
    return {"ok": True, "reset": count, "next_reset_at": pool.next_quota_reset_at()}


_GENSET_MODELS = ("seedance-2.0", "seedance-2.5")
_GENSET_DURATIONS = {("seedance-2.0"): (5, 10, 15), ("seedance-2.5"): (5, 10, 30)}
_GENSET_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")

# 豆包域的模型/时长档位（网页版 ability_param 口径，与 dola 不同：只到 10 秒）
_GENSET_MODELS_DOUBAO = ("seedance-2.0", "seedance-2.0-std", "seedance-2.5")
# ★ 时长来自**真实 action_bar 配置**（2026-10-12 解出）：豆包网页版的时长是滑动条，
#   seedance_v2.5 为 4~30 秒、v2.0 系列（含 std/mini）为 4~15 秒，步长 1。
#   这里给的是面板展示档位；真正下发的区间校验在 doubao_pool.duration_range_for()。
#   ★ 用户明确要求：**2.0 不能有 30 秒**。
_GENSET_DURATIONS_DOUBAO = {
    "seedance-2.0": (5, 10, 15),
    "seedance-2.0-std": (5, 10, 15),
    "seedance-2.5": (5, 10, 15, 30),
}


def _load_genset(channel: str = "dola") -> dict:
    """从 pool 库的 app_settings 表读生成设置（无则返回空 dict）。

    按渠道分域存储：dola → `genset`，doubao → `genset_doubao`（互不影响）。
    """
    import sqlite3 as _sq
    key = "genset" if channel != "doubao" else "genset_doubao"
    conn = _sq.connect(config.POOL_DB_PATH, timeout=15)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        row = conn.execute(
            "SELECT v FROM app_settings WHERE k=?", (key,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    try:
        return json.loads(row[0])
    except Exception:
        return {}


def _save_genset(d: dict, channel: str = "dola") -> None:
    import sqlite3 as _sq
    key = "genset" if channel != "doubao" else "genset_doubao"
    conn = _sq.connect(config.POOL_DB_PATH, timeout=15)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO app_settings(k,v) VALUES(?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(d, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


@app.get("/api/admin/genset")
async def admin_get_genset(channel: str = "",
                           x_admin_key: str | None = Header(default=None)):
    """生成设置（按渠道分域）：channel=dola|doubao，留空=同时返回两套。"""
    _admin_auth(x_admin_key)
    ch = (channel or "").strip().lower()

    def _one(c: str) -> dict:
        d = _load_genset(c)
        if c == "doubao":
            return {"model": d.get("model") or "seedance-2.0",
                    "duration": int(d.get("duration") or 5),
                    "ratio": d.get("ratio") or "16:9",
                    "models": list(_GENSET_MODELS_DOUBAO),
                    "durations": {k: list(v) for k, v in _GENSET_DURATIONS_DOUBAO.items()},
                    "ratios": list(_GENSET_RATIOS)}
        return {"model": d.get("model") or "seedance-2.0",
                "duration": int(d.get("duration") or 5),
                "ratio": d.get("ratio") or "16:9",
                "models": list(_GENSET_MODELS),
                "durations": {k: list(v) for k, v in _GENSET_DURATIONS.items()},
                "ratios": list(_GENSET_RATIOS)}

    if ch in ("dola", "doubao"):
        return {"channel": ch, **_one(ch)}
    return {"channel": "all", "dola": _one("dola"), "doubao": _one("doubao")}


@app.post("/api/admin/genset")
async def admin_set_genset(body: GenSetBody, channel: str = "",
                           x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    ch = (channel or body.channel or "dola").strip().lower()
    if ch not in ("dola", "doubao"):
        raise HTTPException(422, "channel 只能是 dola 或 doubao")
    if ch == "doubao":
        models, durations = _GENSET_MODELS_DOUBAO, _GENSET_DURATIONS_DOUBAO
    else:
        models, durations = _GENSET_MODELS, _GENSET_DURATIONS
    if body.model not in models:
        raise HTTPException(422, f"model 仅支持 {models}")
    if body.duration not in durations.get(body.model, (5, 10, 15, 30)):
        raise HTTPException(
            422, f"{body.model} 的时长仅支持 {durations.get(body.model)}")
    if body.ratio not in _GENSET_RATIOS:
        raise HTTPException(422, f"ratio 仅支持 {_GENSET_RATIOS}")
    _save_genset({"model": body.model, "duration": body.duration,
                  "ratio": body.ratio}, ch)
    return {"ok": True, "channel": ch, "model": body.model,
            "duration": body.duration, "ratio": body.ratio}


@app.post("/api/admin/accounts/{name}/test-generate")
async def admin_test_generate(name: str, body: TestGenRequest,
                              x_admin_key: str | None = Header(default=None)):
    """账号级测试生成：用指定账号跑一条真实出片，返回 task_id（异步轮询结果）。"""
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if JOBS.get(name, {}).get("status") == "running":
        raise HTTPException(409, "该账号正在执行任务，稍后再试")
    task_id = "test_" + uuid.uuid4().hex
    refs = await _prepare_testgen_refs(body.reference_images)
    TEST_JOBS[task_id] = {
        "account": name,
        "channel": "dola",
        "prompt": body.prompt,
        "model": body.model,
        "duration": body.duration,
        "ratio": body.ratio,
        "reference_images": refs,
        "status": "running",
        "started_at": time.time(),
        "result": None,
        "error": "",
    }
    asyncio.create_task(_run_test_generate(
        task_id, name, body.prompt, body.ratio, body.duration, body.model, refs))
    return {"task_id": task_id, "account": name, "channel": "dola",
            "reference_images": refs}


async def _prepare_testgen_refs(raw_refs: list[str]) -> list[str]:
    """把测试生成收到的参考图统一成本地落盘路径（dola 出片链路吃本地文件）。

    - data: URL → 落盘（几 MB base64 不能塞进请求/任务库）
    - http(s) URL → 交给 media.download_reference_images 走原有安全校验后再下载
    - 本地路径 → 原样保留
    """
    refs = [r.strip() for r in (raw_refs or []) if (r or "").strip()]
    if not refs:
        return []
    local = [r for r in refs if os.path.isfile(r)]
    data_urls = [r for r in refs if r.startswith("data:image")]
    remote = [r for r in refs if r not in local and r not in data_urls]
    out = list(local)
    if data_urls:
        out.extend(await asyncio.to_thread(materialize_data_urls, data_urls))
    if remote:
        try:
            _root, paths = await download_reference_images(remote, "testgen")
            out.extend(paths)
        except ValueError as exc:
            raise HTTPException(422, f"参考图下载失败: {exc}") from exc
    return list(dict.fromkeys(out))


async def _run_test_generate(task_id: str, name: str, prompt: str,
                             ratio: str | None, duration: int, model: str,
                             reference_images: list[str] | None = None) -> None:
    def _beat(now):
        # 进度心跳：测试生成动辄 15~30 分钟且不走 /v1 的 on_poll 链路，
        # 不刷 last_poll_at 会被看门狗判成「孤儿」重派 → 同一任务双跑，
        # 僵尸测试协程收尾时还会把重派已完成的任务覆盖成失败（视频在、状态失败）。
        try:
            _update_bg(task_id, last_poll_at=now)
        except Exception:
            pass

    async def _row_taken_over() -> bool:
        """僵尸回写保护：任务已被看门狗重派接管（账号被换 / 已完成）时返回 True。"""
        row = await asyncio.to_thread(store.get, task_id) or {}
        if (row.get("status") or "") == "completed":
            return True
        account = row.get("account")
        return bool(account and account != name)

    refs = list(reference_images or [])
    try:
        # 测试生成同样消耗账号额度，因此写入正式任务库，便于在任务列表追踪结果。
        await asyncio.to_thread(store.create, task_id, model, prompt, ratio, duration,
                                account=name, channel="dola",
                                reference_images=json.dumps(refs, ensure_ascii=False))
        await _update(task_id, status="processing", started_at=time.time())
        # 只有在真的有参考图时才传 reference_image_paths：无图路径的调用签名与旧版
        # 完全一致，兼容只实现 (name, prompt, ratio, duration, model, on_poll) 的号池。
        extra = {"reference_image_paths": refs} if refs else {}
        result = await pool.test_generate(name, prompt, ratio, duration, model,
                                          on_poll=_beat, **extra)
        TEST_JOBS[task_id]["result"] = result
        TEST_JOBS[task_id]["status"] = "done"
        err = result.get("error", "") if not result.get("ok") else ""
        TEST_JOBS[task_id]["error"] = err
        if result.get("ok"):
            await _update(task_id, status="completed",
                          video_url=result.get("video_url"),
                          finished_at=time.time())
        else:
            if await _row_taken_over():
                print(f"[test-gen] {task_id} 已被重派接管/完成，跳过失败回写: {err[:160]}",
                      flush=True)
                return
            status = "风控" if _is_risk_error(err) else "failed"
            await _update(task_id, status=status, error=failure_text.summarize(err),
                          finished_at=time.time())
    except Exception as exc:
        TEST_JOBS[task_id]["status"] = "failed"
        TEST_JOBS[task_id]["error"] = failure_text.summarize(str(exc), limit=600)
        try:
            if await _row_taken_over():
                print(f"[test-gen] {task_id} 已被重派接管/完成，跳过异常回写: {str(exc)[:160]}",
                      flush=True)
                return
            status = "风控" if _is_risk_error(str(exc)) else "failed"
            await _update(task_id, status=status, error=failure_text.summarize(str(exc)),
                          finished_at=time.time())
        except Exception:
            pass


def _is_risk_error(err: str) -> bool:
    """仅当测试生成的失败原因是「登录态失效」时，才将任务状态标为「风控」。"""
    return "登录态失效" in (err or "")


@app.get("/api/admin/test-gen/{task_id}")
async def admin_test_gen_status(task_id: str,
                                x_admin_key: str | None = Header(default=None)):
    """测试生成状态：dola / 豆包两条通道共用这一个查询口（按 job.channel 区分）。"""
    _admin_auth(x_admin_key)
    job = TEST_JOBS.get(task_id)
    if not job:
        raise HTTPException(404, "test task not found")
    return job


@app.post("/api/admin/accounts/{name}/proxy")
async def admin_account_proxy_set(name: str, body: AccountProxyBind,
                                  x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    try:
        set_account_proxy(name, body.proxy_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@app.get("/api/admin/route")
async def admin_route(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return pool.preview_route()


@app.post("/api/admin/accounts/{name}/prefer")
async def admin_account_prefer(name: str, body: PreferBody,
                               x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    pool.set_preferred(name if body.preferred else None)
    return {"ok": True, "preferred": name if body.preferred else None}


@app.post("/api/admin/accounts/{name}/weight")
async def admin_account_weight(name: str, body: WeightBody,
                               x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    pool.set_weight(name, body.weight)
    return {"ok": True, "weight": body.weight}


@app.post("/api/admin/accounts/rebalance-proxies")
async def admin_rebalance_proxies(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        return pool.rebalance_proxy_bindings()
    except Exception as exc:
        code = getattr(exc, "code", "rebalance_failed")
        raise HTTPException(400, f"{code}: {exc}") from exc


@app.post("/api/admin/accounts/bind-proxies")
async def admin_bind_proxies(body: BindProxiesBody,
                             x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        return pool.bind_proxies(body.proxies, force=body.force, mode=body.mode)
    except Exception as exc:
        code = getattr(exc, "code", "bind_failed")
        raise HTTPException(400, f"{code}: {exc}") from exc


@app.post("/api/admin/register")
async def admin_register(body: RegisterBody,
                         x_admin_key: str | None = Header(default=None)):
    """启动后台批量注册；每个号注册成功即自动导入账号列表（面板「注册账号」页）。"""
    _admin_auth(x_admin_key)
    if register_mgr.running:
        raise HTTPException(409, "注册任务进行中，请先停止或等待结束")
    if body.mail_source not in register_service.MAIL_SOURCES:
        raise HTTPException(400, f"不支持的邮箱来源: {body.mail_source}")
    if body.mail_source == "tempmail":
        if body.domain not in register_service.MAIL_DOMAINS:
            raise HTTPException(400, f"不支持的邮箱域名: {body.domain}")
    else:
        if not register_service.outlook_mail.available():
            raise HTTPException(503, "Outlook 邮箱后端不可用: %s"
                                % (register_service.outlook_mail.unavailable_reason() or "未知"))
        if body.mail_source == "outlook_pool" and not register_service.outlook_mail.load_pool():
            raise HTTPException(400, "邮箱池为空或文件不存在：%s"
                                % register_service.outlook_mail.pool_path())
    if body.set_password and not body.password.strip():
        raise HTTPException(400, "勾选了设密码但密码为空")
    loop = asyncio.get_running_loop()
    cfg = body.model_dump()
    # 代理回落链（body.proxy > DOLA_REGISTER_PROXY > DOLA_PROXY）在 register_service 内处理
    if not register_mgr.start(cfg, pool, loop):
        raise HTTPException(409, "注册任务启动失败")
    return {"ok": True, "total": register_mgr.total}


@app.get("/api/admin/register/status")
async def admin_register_status(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return register_mgr.status()


@app.post("/api/admin/register/stop")
async def admin_register_stop(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    register_mgr.stop()
    return {"ok": True}


# ===== 取号（面板「取号」页）：谷歌注册 / 网页登录取 dola 号 =====
# 两条链路都吃住宅出口；代理统一走「代理管理」的池默认代理（或显式 proxy_id）。


def _take_guard() -> None:
    """同一时刻只允许一个取号任务；也与「注册账号」互斥（都抢浏览器与出口）。"""
    if take_service.is_running():
        raise HTTPException(409, "取号任务进行中，请先停止或等待结束")
    if register_mgr.running:
        raise HTTPException(409, "「注册账号」任务进行中，请先停止或等待结束")


@app.post("/api/admin/take/google-register")
async def admin_take_google(body: TakeGoogleBody,
                            x_admin_key: str | None = Header(default=None)):
    """用住宅代理跑 Google 注册，产出 email----password----totp（写 谷歌账号_产出.txt）。"""
    _admin_auth(x_admin_key)
    _take_guard()
    cfg = body.model_dump()
    if not (cfg.get("domain") or "").strip():
        raise HTTPException(400, "请填写邮箱域名（tempmail 后端建议 mailto.plus）")
    if (cfg.get("mail_backend") or "") == "imap" and not (cfg.get("imap_host") or "").strip():
        raise HTTPException(400, "IMAP 后端需要填服务器地址")
    if (cfg.get("mail_backend") or "") == "http" and "{addr}" not in (cfg.get("http_url") or ""):
        raise HTTPException(400, "HTTP 收件后端的 URL 必须包含 {addr} 占位符")
    if not take_service.start("google", cfg, pool):
        raise HTTPException(409, "取号任务启动失败")
    return {"ok": True, "total": cfg.get("count")}


@app.post("/api/admin/take/weblogin")
async def admin_take_weblogin(body: TakeWebloginBody,
                              x_admin_key: str | None = Header(default=None)):
    """用住宅代理把 Google 号换成 dola 号（真网页登录），成功即自动入池。"""
    _admin_auth(x_admin_key)
    _take_guard()
    cfg = body.model_dump()
    if not take_service.start("weblogin", cfg, pool):
        raise HTTPException(409, "取号任务启动失败")
    return {"ok": True}


@app.post("/api/admin/take/glogin")
async def admin_take_glogin(body: TakeGloginBody,
                            x_admin_key: str | None = Header(default=None)):
    """Google 渠道：Google 官网自动登录（含「以后再说」/邮件验证码）→ dola Google 登录 → 入池。

    本链路强制有头浏览器（无头 Chromium 登 Google 必被识别），忽略 headless 参数。
    """
    _admin_auth(x_admin_key)
    _take_guard()
    cfg = body.model_dump()
    txt = (cfg.get("accounts_text") or "").strip()
    if not txt:
        raise HTTPException(400, "请粘贴 Google 渠道号，格式：账号|密码|取件地址（一行一个）")
    for l in txt.splitlines():
        l = l.strip()
        if not l or l.startswith("#"):
            continue
        if len(l.split("|")) < 2:
            raise HTTPException(400, "格式错误：%s（应为 账号|密码|取件地址）" % l[:60])
    if not take_service.start("glogin", cfg, pool):
        raise HTTPException(409, "取号任务启动失败")
    return {"ok": True}


@app.get("/api/admin/take/status")
async def admin_take_status(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return take_service.status()


@app.post("/api/admin/take/stop")
async def admin_take_stop(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    take_service.request_stop()
    return {"ok": True}


@app.post("/api/admin/videopool")
async def admin_videopool_start(body: VideoPoolBody,
                                x_admin_key: str | None = Header(default=None)):
    """启动视频池：连跑换号注册+首次提交（2.5），风控弃号，受理后守望取片自动下载。"""
    _admin_auth(x_admin_key)
    cfg = body.model_dump()
    if not videopool_mgr.start(cfg):
        raise HTTPException(409, "视频池任务进行中，请先停止或等待结束")
    return {"ok": True, "orders": videopool_mgr.orders_target}


@app.get("/api/admin/videopool/status")
async def admin_videopool_status(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return videopool_mgr.status()


@app.post("/api/admin/videopool/stop")
async def admin_videopool_stop(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    videopool_mgr.stop()
    return {"ok": True}


@app.post("/api/admin/accounts/import-cookies")
async def admin_import_cookies(body: ImportTextBody,
                               x_admin_key: str | None = Header(default=None)):
    """Cookie 头文本 / Cookie JSON 批量导入（对齐 api-pool 的 import-cookies）。

    text 形如：一行一个 Cookie 头，或浏览器扩展导出的 Cookie JSON 数组；自动跳过注释/空行/邮箱行，按 sessionid 去重。
    """
    _admin_auth(x_admin_key)
    if not (body.text or "").strip():
        raise HTTPException(400, "text 不能为空")
    try:
        result = await import_cookie_accounts_from_text(
            pool, body.text, require_login=not body.skip_verify
        )
    except Exception as exc:
        raise HTTPException(400, str(exc)[:300]) from exc
    # 新导入账号按代理管理的池默认代理绑定；前端显式选了代理则以它为准
    pid = (body.proxy_id or "").strip()
    for n in result.get("created") or []:
        _bind_account_proxy(n, pid)
    return {"ok": True, **result}


def _acc_proxy_url(name: str) -> str:
    """账号绑定的代理 URL（导出时给 CK 补全/浏览器补真用），取不到返回空串。"""
    try:
        return proxy_store.account_proxy_url(name) or ""
    except Exception:
        return ""


@app.get("/api/admin/accounts/{name}/export-cookie")
async def admin_export_cookie(name: str, user_id: str = "",
                              x_admin_key: str | None = Header(default=None)):
    """导出单个账号的 CK。

    四种格式（Cookie JSON / 完整 JSON / Cookie 头文本 / Netscape）一次算好返回，
    前端切换格式不用再打接口。CK 源 = accounts/<name>/cookie_state.json（无则回落 cookie.txt）。
    默认 json 格式可直接粘贴到 dola-pool 2.1.4「添加账号 → Cookie 导入」。
    """
    _admin_auth(x_admin_key)
    row = await _account_row(name)
    if row is None:
        raise HTTPException(404, "account not found")
    data = ck_export.export_account(
        pool.accounts_dir, name, email=row.get("email") or "", user_id=user_id,
        proxy=_acc_proxy_url(name))
    if data is None:
        raise HTTPException(
            400, "该账号没有可导出的 Cookie（浏览器登录号不落 cookie_state.json）")
    return data


@app.post("/api/admin/accounts/export-cookies")
async def admin_export_cookies(body: ExportCookiesBody,
                               x_admin_key: str | None = Header(default=None)):
    """批量导出 CK → ZIP（一个账号一个文件）。names 为空 = 导出全部账号。"""
    _admin_auth(x_admin_key)
    fmt = ck_export.normalize_format(body.format)
    metas = {a["name"]: a for a in pool.list_accounts()}
    names = [n for n in (body.names or []) if n] or list(metas)

    buf = io.BytesIO()
    added = skipped = 0
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n in names:
            data = ck_export.export_account(
                pool.accounts_dir, n, email=(metas.get(n) or {}).get("email") or "",
                proxy=_acc_proxy_url(n))
            if data is None:
                skipped += 1
                continue
            fn = data["filenames"][fmt]
            if fn in used:          # 同名兜底（正常账号名唯一）
                fn = "%s_%s" % (added + 1, fn)
            used.add(fn)
            zf.writestr(fn, data["formats"][fmt])
            added += 1
    if not added:
        raise HTTPException(
            400, "所选账号都没有可导出的 Cookie（浏览器登录号不落 cookie_state.json）")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(
        content=buf.getvalue(), media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="dola_ck_%s_%s.zip"'
                                   % (fmt, stamp),
            "X-CK-Count": str(added),
            "X-CK-Skipped": str(skipped),
        })


@app.post("/api/admin/accounts/import-accounts")
async def admin_import_accounts(body: ImportTextBody,
                                x_admin_key: str | None = Header(default=None)):
    """多行账号凭据批量登录导入（对齐 api-pool 的 server.login）。

    支持 email----password----totp / | / , / : 及 JSON 数组；按 email 去重、自动命名 acc<N>，
    逐个走 Google OAuth 自动登录并写入 profile。
    """
    _admin_auth(x_admin_key)
    if not (body.text or "").strip():
        raise HTTPException(400, "text 不能为空")
    records = parse_account_text(body.text)
    if not records:
        raise HTTPException(400, "未解析到有效账号（需 email----password 或 JSON 数组）")
    pairs = allocate_credentials(records, pool.accounts, name_prefix="acc")
    created: list[str] = []
    failed: list[str] = []
    errors: list[str] = []
    try:
        async with ADD_ACCOUNT_SEM:
            for name, cred in pairs:
                dups = pool.find_accounts_by_email(cred.email)
                if dups:
                    failed.append(name)
                    errors.append(f"{name}: 邮箱已存在于 {','.join(dups)}")
                    continue
                try:
                    await add_account_flow(name, cred.email, cred.password, cred.totp)
                    pool.ensure_account(name)
                    pool.set_email(name, cred.email)
                    pool.set_source(name, "login")
                    pool.set_login_status(name, True)
                    if cred.email:
                        cred_store.store(name, cred.email, cred.password, cred.totp)
                    created.append(name)
                    # 导入自动绑定池默认代理（未手动指定代理时）
                    _bind_account_proxy(name, body.proxy_id)
                except Exception as exc:
                    failed.append(name)
                    errors.append(f"{name}: {str(exc)[:150]}")
    except Exception as exc:
        raise HTTPException(400, str(exc)[:300]) from exc
    return {"ok": True, "created": created, "failed": failed, "errors": errors}


@app.get("/api/admin/proxies")
async def admin_proxies(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"proxies": list_proxies(), "default_proxy": config.PROXY or "",
            "default_proxy_id": proxy_store.get_default_proxy_id()}


@app.post("/api/admin/proxies/set-default")
async def admin_proxy_set_default(body: ProxySetDefault,
                                  x_admin_key: str | None = Header(default=None)):
    """设置/取消池默认代理：之后导入、注册的账号会自动绑定它。"""
    _admin_auth(x_admin_key)
    pid = (body.proxy_id or "").strip() or None
    if pid and pid not in {p["id"] for p in list_proxies()}:
        raise HTTPException(404, "proxy not found")
    try:
        proxy_store.set_default_proxy(pid)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "default_proxy_id": pid}


@app.post("/api/admin/proxies")
async def admin_proxy_create(body: ProxyCreate,
                             x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        created = create_proxy(
            body.name, body.protocol, body.host, body.port,
            body.username, body.password, body.remark,
            mode=body.mode,
            session_template=body.session_template,
            sticky_ttl=body.sticky_ttl,
            rotate_on_risk=body.rotate_on_risk,
            rotate_min_interval=body.rotate_min_interval,
            extract_url=body.extract_url,
            extract_interval=body.extract_interval,
            nodes_text=body.nodes_text,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"created": created}


@app.patch("/api/admin/proxies/{proxy_id}")
async def admin_proxy_update(proxy_id: str, body: ProxyUpdate,
                             x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        updated = update_proxy(proxy_id, **fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not updated:
        raise HTTPException(404, "proxy not found")
    return {"updated": updated}


@app.delete("/api/admin/proxies/{proxy_id}")
async def admin_proxy_delete(proxy_id: str,
                             x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    delete_proxy(proxy_id)
    return {"ok": True}


# ===== 动态代理：出口 IP 探测 / 轮换 =====


@app.post("/api/admin/proxies/{proxy_id}/probe")
async def admin_proxy_probe(proxy_id: str,
                            x_admin_key: str | None = Header(default=None)):
    """探测该代理当前的出口 IP（动态代理用临时 session，不动已绑定的号）。"""
    _admin_auth(x_admin_key)
    return await asyncio.to_thread(proxy_store.probe_proxy_record, proxy_id)


@app.get("/api/admin/proxies/{proxy_id}/nodes")
async def admin_proxy_nodes(proxy_id: str,
                            x_admin_key: str | None = Header(default=None)):
    """提取型代理的节点池概览。"""
    _admin_auth(x_admin_key)
    nodes = proxy_store.list_nodes(proxy_id)
    now = time.time()
    return {
        "nodes": nodes,
        "total": len(nodes),
        "alive": sum(1 for n in nodes
                     if not n["dead"] and (not n["expires_at"] or n["expires_at"] > now)),
        "dead": sum(1 for n in nodes if n["dead"]),
    }


@app.post("/api/admin/proxies/{proxy_id}/nodes")
async def admin_proxy_add_nodes(proxy_id: str, body: ProxyNodesIn,
                                x_admin_key: str | None = Header(default=None)):
    """手工往节点池里粘贴/追加节点（一行一个 host:port:user:pass）。"""
    _admin_auth(x_admin_key)
    result = await asyncio.to_thread(
        proxy_store.add_nodes_from_text, proxy_id, body.text, body.ttl)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "添加节点失败"))
    return result


@app.post("/api/admin/proxies/{proxy_id}/refresh-nodes")
async def admin_proxy_refresh_nodes(proxy_id: str,
                                    x_admin_key: str | None = Header(default=None)):
    """手动调一次提取接口补节点（注意：GenerateLink 类接口通常按次计费）。"""
    _admin_auth(x_admin_key)
    result = await asyncio.to_thread(proxy_store.fetch_extract_nodes, proxy_id)
    return result


@app.post("/api/admin/proxies/{proxy_id}/prune-nodes")
async def admin_proxy_prune_nodes(proxy_id: str,
                                  x_admin_key: str | None = Header(default=None)):
    """清掉过期/连续失败的节点。"""
    _admin_auth(x_admin_key)
    return {"ok": True, "removed": proxy_store.prune_nodes(proxy_id)}


@app.post("/api/admin/accounts/{name}/rotate-ip")
async def admin_account_rotate_ip(name: str,
                                  x_admin_key: str | None = Header(default=None)):
    """给该号换一个出口 IP（动态代理换 sticky session；静态代理无操作）。"""
    _admin_auth(x_admin_key)
    # 注意：不要 to_thread。BrowserPool 的 sqlite 连接是跨线程共享的，
    # 从 worker 线程调用池方法会和事件循环交错使用同一条连接、留下未结束的事务，
    # 把 pool_usage.db 的写锁焊死（实测会让所有写操作 15s 超时 database is locked）。
    result = pool.rotate_account_ip(name, force=True)
    if not result.get("rotated"):
        raise HTTPException(400, result.get("reason", "轮换失败"))
    return result


@app.post("/api/admin/accounts/{name}/probe-ip")
async def admin_account_probe_ip(name: str,
                                 x_admin_key: str | None = Header(default=None)):
    """查该号当前真实出口 IP 与归属地。"""
    _admin_auth(x_admin_key)
    return await asyncio.to_thread(proxy_store.probe_account_exit_ip, name)


@app.post("/api/admin/proxies/{proxy_id}/rotate")
async def admin_proxy_rotate_all(proxy_id: str,
                                 x_admin_key: str | None = Header(default=None)):
    """给绑定在该代理上的所有号换出口 IP。"""
    _admin_auth(x_admin_key)
    rotated, skipped = [], []
    for acc in pool.list_accounts():
        if acc.get("proxy_id") != proxy_id:
            continue
        result = pool.rotate_account_ip(acc["name"], force=True)
        (rotated if result.get("rotated") else skipped).append(acc["name"])
    return {"ok": True, "rotated": rotated, "skipped": skipped}


@app.get("/api/admin/users")
async def admin_users(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    session = ADMIN_SESSIONS.get(x_admin_key or "", {})
    return {
        "users": await asyncio.to_thread(user_list),
        "current_username": session.get("username"),
    }


@app.post("/api/admin/users")
async def admin_user_create(body: AdminUserCreate,
                            x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        created = await asyncio.to_thread(
            user_create, body.username, body.password, role=body.role,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"created": created}


@app.patch("/api/admin/users/{username}")
async def admin_user_update(username: str, body: AdminUserUpdate,
                            x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        updated = await asyncio.to_thread(
            user_update,
            username,
            new_username=body.username,
            password=body.password,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"updated": updated}


@app.delete("/api/admin/users/{username}")
async def admin_user_delete(username: str,
                            x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    session = ADMIN_SESSIONS.get(x_admin_key or "", {})
    if session.get("username") == username:
        raise HTTPException(400, "不能删除当前登录的账号")
    try:
        await asyncio.to_thread(user_delete, username)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


def _bind_account_proxy(name: str, proxy_id: str = "") -> str | None:
    """新入池账号绑定代理：显式 proxy_id 优先，否则回落到「代理管理」里的池默认代理。

    添加账号 / 批量添加（文本、Cookie JSON、Cookie 头文本）全部走这里，
    所以默认就是代理管理里设为默认的那条代理；绑定失败不影响账号入池。
    """
    pid = (proxy_id or "").strip()
    try:
        if pid:
            proxy_store.set_account_proxy(name, pid)
            return pid
        return proxy_store.bind_default_proxy(name)
    except Exception as exc:
        print(f"[proxy] 账号 {name} 绑定代理失败: {str(exc)[:150]}", flush=True)
        return None


async def _run_add_job(name: str, email: str, password: str, totp: str,
                       cookies: str = "", cookie_skip_verify: bool = False,
                       proxy_id: str = "") -> None:
    JOBS[name] = {"kind": "add", "status": "running", "email": email,
                  "error": "", "started_at": time.time()}
    try:
        async with ADD_ACCOUNT_SEM:
            if cookies:
                result = await import_cookie_account(
                    name, json.loads(cookies),
                    require_login=not cookie_skip_verify,
                )
                pool.ensure_account(name)
                pool.set_email(name, email)
                pool.set_source(name, "cookie")
                pool.set_login_status(name, bool(result.get("verified")))
            else:
                await add_account_flow(name, email, password, totp)
                pool.ensure_account(name)
                pool.set_email(name, email)
                pool.set_source(name, "login")
                pool.set_login_status(name, True)
            # 入池后绑定代理：显式指定优先，否则用代理管理的池默认代理
            _bind_account_proxy(name, proxy_id)
        JOBS[name] = {**JOBS[name], "status": "success"}
    except Exception as e:
        JOBS[name] = {**JOBS[name], "status": "failed", "error": str(e)[:300]}


async def _cred_try_refresh_once() -> None:
    """发现有凭据但登录态失效的账号时，自动用存储的 Google 凭据重新登录刷新。"""
    now = time.time()
    for name in cred_store.all_names():
        if name in JOBS and JOBS[name].get("status") == "running":
            continue
        acct = next((a for a in pool.list_accounts() if a["name"] == name), None)
        if not acct:
            continue
        # cookie 来源账号靠重新导入 cookie 管理，不用 Google 凭据自动重登，
        # 否则会卡 Google OAuth 并占用浏览器 profile（导致验证 profile 锁）。
        if acct.get("source") == "cookie":
            continue
        if acct.get("login_ok") in (1, None):
            continue
        if now - LAST_CRED_REFRESH.get(name, 0) < 180:
            continue
        creds = cred_store.load(name)
        if not creds or not creds.get("email") or not creds.get("password"):
            continue
        LAST_CRED_REFRESH[name] = now
        print(f"[cred-refresh] 尝试自动刷新账号 {name}", flush=True)
        try:
            async with ADD_ACCOUNT_SEM:
                await add_account_flow(
                    name, creds["email"], creds["password"], creds.get("totp") or "",
                    force_login=True)
            pool.set_email(name, creds["email"])
            pool.set_login_status(name, True)
            print(f"[cred-refresh] {name} 刷新成功", flush=True)
        except Exception as e:
            print(f"[cred-refresh] {name} 刷新失败: {str(e)[:200]}", flush=True)


async def _cred_refresher_loop() -> None:
    while True:
        try:
            await _cred_try_refresh_once()
        except Exception:
            pass
        await asyncio.sleep(60)


@app.post("/api/admin/accounts", status_code=202)
async def admin_account_add(body: AccountAdd, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "invalid account name")
    if body.name in pool.accounts:
        raise HTTPException(409, "账号名已存在：" + body.name)
    if JOBS.get(body.name, {}).get("status") == "running":
        raise HTTPException(409, "add job running")
    email = (body.email or "").strip()
    cookies = (body.cookies or "").strip()
    # 代理校验放在任何副作用（写凭据 / 建 job）之前，拿不到就直接 404
    proxy_id = (body.proxy_id or "").strip()
    if proxy_id and proxy_id not in {p["id"] for p in list_proxies()}:
        raise HTTPException(404, "proxy not found")
    if not email and not cookies:
        raise HTTPException(400, "邮箱不能为空")
    if email:
        cred_store.store(body.name, email, body.password, body.totp)
        dups = pool.find_accounts_by_email(email)
        if dups:
            raise HTTPException(409, "该邮箱已存在于账号 " + "、".join(dups) + "，请勿重复添加")
        norm = email.lower()
        for n, job in JOBS.items():
            if (job.get("kind") == "add" and job.get("status") == "running"
                    and (job.get("email") or "").strip().lower() == norm):
                raise HTTPException(409, "该邮箱正在添加中（" + n + "），请勿重复提交")
    if cookies:
        try:
            json.loads(cookies)
        except Exception:
            raise HTTPException(422, "cookie JSON 格式错误")
    JOBS[body.name] = {"kind": "add", "status": "running", "email": email,
                       "error": "", "started_at": time.time()}
    asyncio.create_task(_run_add_job(
        body.name, email, body.password, body.totp,
        cookies=cookies, cookie_skip_verify=body.cookie_skip_verify,
        proxy_id=proxy_id,
    ))
    return {"ok": True, "job": "running"}


@app.get("/api/admin/jobs")
async def admin_jobs(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"jobs": JOBS}


# ===== 豆包域（www.doubao.com）通道 =====
#
# 账号管理：扫码（豆包 App / 抖音 App）+ 手机号验证码 三种加号方式；
# 测试生成 / 任务中心 / 视频库 / 生成设置 全部按 channel=dola|doubao 分域。
# 协议细节与实测证据见 doubao_login.py 顶部注释。


class DoubaoQrBody(BaseModel):
    """取豆包登录二维码；qr_mode：douyin（官方抖音码，默认）/ doubao（豆包 App 页）。"""
    qr_mode: str = "douyin"
    proxy: str = ""


class DoubaoPhoneSendBody(BaseModel):
    """手机号发短信验证码（实测无滑块）。"""
    mobile: str
    proxy: str = ""


class DoubaoPhoneLoginBody(BaseModel):
    """手机号验证码登录。"""
    session_id: str
    code: str
    name: str = ""
    proxy: str = ""


class DoubaoQrFinishBody(BaseModel):
    """扫码完成后落库（面板异步轮询到 logged_in 后调用）。"""
    session_id: str
    name: str = ""


class DoubaoAddrBody(BaseModel):
    """手工粘贴豆包 CK 入池。"""
    name: str
    cookies: str
    note: str = ""
    mobile: str = ""
    email: str = ""
    skip_verify: bool = False


class DoubaoTgBody(BaseModel):
    """豆包账号级测试生成（支持上传参考图）。

    时长只做**粗边界**（4~30，对应服务端滑动条的整体范围）；具体到模型的
    上限（v2.0 系列 15 / v2.5 30）由 `doubao_pool.duration_range_for` 精确校验。
    """
    prompt: str = Field(..., min_length=1)
    model: str = "seedance-2.0"
    duration: int = Field(5, ge=4, le=30)
    ratio: str | None = None
    # 参考图：本地落盘路径或 data: URL（面板上传后回填）
    reference_images: list[str] = Field(default_factory=list)


def _doubao_login_proxy(proxy: str = "") -> str:
    return (proxy or "").strip() or doubao.proxy


def _gc_doubao_sessions() -> None:
    now = time.time()
    for sid in [k for k, v in DOUBAO_LOGIN_SESSIONS.items()
                if now - float(v.get("touched") or 0) > DOUBAO_LOGIN_TTL]:
        DOUBAO_LOGIN_SESSIONS.pop(sid, None)


def _unique_doubao_name(prefix: str = "db") -> str:
    """给新扫码/短信号取一个不冲突的名字：db001 / db002 …，已占用则跳过。"""
    used = set(doubao.accounts)
    for i in range(1, 10000):
        name = f"{prefix}{i:03d}"
        if name not in used:
            return name
    return f"{prefix}{int(time.time())}"


def _register_doubao_account(name: str, ck: str, *, source: str, meta: dict) -> dict:
    """把登录拿到的 CK 落盘入池（含 sessionid 去重）。"""
    jar = doubao_login.parse_ck(ck)
    sid = jar.get("sessionid") or ""
    dup = doubao_store.find_by_sessionid(sid)
    if dup and dup != name:
        # 同一个豆包号重复加了：覆盖旧账号的 CK，不新建
        name = dup
    meta = dict(meta or {})
    meta.setdefault("source", source)
    return doubao.add(name, ck, meta)


async def _doubao_verify_and_tag(name: str) -> dict:
    """入池后验活（失败也保留账号，标记 login_ok=False 供面板复核）。"""
    try:
        res = await doubao.verify(name)
    except Exception as exc:
        res = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
    return res


@app.get("/api/admin/doubao/accounts")
async def admin_doubao_accounts(x_admin_key: str | None = Header(default=None)):
    """豆包号池账号列表（与 dola 账号同形，供面板复用分组/筛选）。"""
    _admin_auth(x_admin_key)
    return doubao.summary()


@app.post("/api/admin/doubao/login/qr")
async def admin_doubao_login_qr(body: DoubaoQrBody,
                                x_admin_key: str | None = Header(default=None)):
    """取豆包登录二维码（默认官方抖音码；可选豆包 App 扫码页）。"""
    _admin_auth(x_admin_key)
    _gc_doubao_sessions()
    mode = (body.qr_mode or "douyin").strip().lower()
    if mode not in ("doubao", "douyin"):
        raise HTTPException(422, "qr_mode 只能是 doubao（豆包 App）或 douyin（抖音 App）")
    cli = doubao_login.DoubaoLoginClient(proxy=_doubao_login_proxy(body.proxy))
    try:
        sess = await cli.create_qr(mode)
    except Exception as exc:
        await cli.close()
        raise HTTPException(502, f"取二维码失败: {type(exc).__name__}: {exc}"[:300])
    sid = "dblogin_" + uuid.uuid4().hex
    DOUBAO_LOGIN_SESSIONS[sid] = {"client": cli, "sess": sess, "mode": mode,
                                  "touched": time.time()}
    return {"ok": True, "session_id": sid, **sess.to_dict()}


@app.get("/api/admin/doubao/login/qr/{session_id}")
async def admin_doubao_login_poll(session_id: str,
                                  x_admin_key: str | None = Header(default=None)):
    """轮询扫码状态：new → scan → confirm → done。"""
    _admin_auth(x_admin_key)
    item = DOUBAO_LOGIN_SESSIONS.get(session_id)
    if not item:
        raise HTTPException(404, "登录会话不存在或已过期，请重新取码")
    item["touched"] = time.time()
    cli: doubao_login.DoubaoLoginClient = item["client"]
    sess: doubao_login.LoginSession = item["sess"]
    try:
        sess = await cli.poll_qr(sess)
    except Exception as exc:
        sess.status = "error"
        sess.message = f"轮询失败: {type(exc).__name__}: {exc}"[:200]
    item["sess"] = sess
    return {"ok": True, "session_id": session_id, **sess.to_dict()}


@app.post("/api/admin/doubao/login/qr/finish")
async def admin_doubao_login_finish(body: DoubaoQrFinishBody,
                                    x_admin_key: str | None = Header(default=None)):
    """扫码确认后落库入池（面板轮询到 logged_in 时调用）。"""
    _admin_auth(x_admin_key)
    item = DOUBAO_LOGIN_SESSIONS.get(body.session_id)
    if not item:
        raise HTTPException(404, "登录会话不存在或已过期")
    sess: doubao_login.LoginSession = item["sess"]
    jar = dict(sess.cookies or {})
    if not (jar.get("sessionid") or ""):
        # 再轮一次，给服务端一点时间把 sessionid 写下来
        try:
            sess = await item["client"].poll_qr(sess)
            item["sess"] = sess
            jar = dict(sess.cookies or {})
        except Exception:
            pass
    if not (jar.get("sessionid") or ""):
        raise HTTPException(409, f"还没有拿到登录态：{sess.message or sess.status}")
    name = (body.name or "").strip() or _unique_doubao_name("db")
    ck = doubao_login.ck_header_from_jar(jar)
    mode = item.get("mode") or "doubao"
    try:
        meta = _register_doubao_account(
            name, ck, source=("qr_doubao" if mode == "doubao" else "qr_douyin"),
            meta={"usage_day": doubao_pool.usage_day()})
    except Exception as exc:
        raise HTTPException(422, f"入池失败: {exc}"[:200])
    verify = await _doubao_verify_and_tag(name)
    DOUBAO_LOGIN_SESSIONS.pop(body.session_id, None)
    await item["client"].close()
    return {"ok": True, "account": name, "source": meta.get("source"),
            "verified": bool(verify.get("ok")), "verify_reason": verify.get("reason", "")}


@app.post("/api/admin/doubao/login/phone/send")
async def admin_doubao_phone_send(body: DoubaoPhoneSendBody,
                                  x_admin_key: str | None = Header(default=None)):
    """手机号发短信验证码（实测无滑块）。"""
    _admin_auth(x_admin_key)
    _gc_doubao_sessions()
    cli = doubao_login.DoubaoLoginClient(proxy=_doubao_login_proxy(body.proxy))
    try:
        sess = await cli.send_sms(body.mobile)
    except Exception as exc:
        await cli.close()
        raise HTTPException(502, f"发码失败: {type(exc).__name__}: {exc}"[:300])
    sid = "dbphone_" + uuid.uuid4().hex
    DOUBAO_LOGIN_SESSIONS[sid] = {"client": cli, "sess": sess, "mode": "sms",
                                  "touched": time.time()}
    return {"ok": True, "session_id": sid, **sess.to_dict()}


@app.post("/api/admin/doubao/login/phone/verify")
async def admin_doubao_phone_verify(body: DoubaoPhoneLoginBody,
                                    x_admin_key: str | None = Header(default=None)):
    """手机号验证码登录并入池。"""
    _admin_auth(x_admin_key)
    item = DOUBAO_LOGIN_SESSIONS.get(body.session_id)
    if not item:
        raise HTTPException(404, "登录会话不存在或已过期，请重新发码")
    item["touched"] = time.time()
    sess: doubao_login.LoginSession = item["sess"]
    try:
        sess = await item["client"].sms_login(sess, body.code)
    except Exception as exc:
        raise HTTPException(502, f"登录失败: {type(exc).__name__}: {exc}"[:300])
    item["sess"] = sess
    payload = {"ok": sess.status == "done", "session_id": body.session_id, **sess.to_dict()}
    if sess.status != "done":
        return payload
    name = (body.name or "").strip() or _unique_doubao_name("db")
    ck = doubao_login.ck_header_from_jar(sess.cookies)
    try:
        meta = _register_doubao_account(
            name, ck, source="sms",
            meta={"mobile": sess.mobile, "usage_day": doubao_pool.usage_day()})
    except Exception as exc:
        raise HTTPException(422, f"入池失败: {exc}"[:200])
    verify = await _doubao_verify_and_tag(name)
    DOUBAO_LOGIN_SESSIONS.pop(body.session_id, None)
    await item["client"].close()
    payload.update(account=name, source=meta.get("source"),
                   verified=bool(verify.get("ok")),
                   verify_reason=verify.get("reason", ""))
    return payload


@app.post("/api/admin/doubao/accounts/add")
async def admin_doubao_account_add(body: DoubaoAddrBody,
                                   x_admin_key: str | None = Header(default=None)):
    """手工粘贴豆包 CK 入池（浏览器 F12 复制 Cookie 头即可）。"""
    _admin_auth(x_admin_key)
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "invalid account name")
    jar = doubao_login.parse_ck(body.cookies)
    if not (jar.get("sessionid") or ""):
        raise HTTPException(422, "Cookie 里缺少 sessionid，不是豆包域登录态")
    try:
        meta = _register_doubao_account(
            body.name, body.cookies.strip(), source="cookie",
            meta={"mobile": body.mobile, "email": body.email, "note": body.note,
                  "usage_day": doubao_pool.usage_day(),
                  "login_ok": None})
    except Exception as exc:
        raise HTTPException(422, str(exc)[:200])
    verify = {"ok": False, "reason": "已跳过验证"}
    if not body.skip_verify:
        verify = await _doubao_verify_and_tag(body.name)
    return {"ok": True, "account": body.name, "source": meta.get("source"),
            "verified": bool(verify.get("ok")),
            "verify_reason": verify.get("reason", "")}


@app.delete("/api/admin/doubao/accounts/{name}")
async def admin_doubao_account_delete(name: str,
                                      x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in doubao.accounts:
        raise HTTPException(404, "account not found")
    # 账号正在出片时目录里的文件可能被占用 → 删除会失败，必须如实回报，
    # 不能让面板显示「已删除」而账号其实还在（delete 已改为删完复查）。
    removed = await asyncio.to_thread(doubao.delete, name)
    if not removed:
        raise HTTPException(
            409, f"{name} 目录删除失败（可能正在出片占用文件），请稍后重试")
    return {"ok": True, "deleted": name}


class DoubaoAccountPatch(BaseModel):
    scheduling: bool | None = None
    note: str | None = None
    email: str | None = None
    weight: int | None = Field(None, ge=1, le=100)


@app.patch("/api/admin/doubao/accounts/{name}")
async def admin_doubao_account_patch(name: str, body: DoubaoAccountPatch,
                                     x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in doubao.accounts:
        raise HTTPException(404, "account not found")
    if body.scheduling is not None:
        doubao.set_scheduling(name, body.scheduling)
    if body.note is not None:
        doubao.set_note(name, body.note)
    if body.weight is not None:
        doubao.set_weight(name, body.weight)
    if body.email is not None:
        doubao_store.patch_meta(name, email=body.email, updated_at=time.time())
    return {"ok": True}


class DoubaoRenameBody(BaseModel):
    new_name: str


@app.post("/api/admin/doubao/accounts/{name}/rename")
async def admin_doubao_account_rename(name: str, body: DoubaoRenameBody,
                                      x_admin_key: str | None = Header(default=None)):
    """豆包账号改名（搬目录：ck.txt 与 meta.json 一起走）。"""
    _admin_auth(x_admin_key)
    if name not in doubao.accounts:
        raise HTTPException(404, "account not found")
    new_name = (body.new_name or "").strip()
    if not new_name:
        raise HTTPException(422, "新账号名不能为空")
    if new_name == name:
        return {"ok": True, "account": name}
    if new_name in doubao.accounts:
        raise HTTPException(409, f"账号名已存在：{new_name}")
    if doubao.lock_for(name).locked():
        raise HTTPException(409, "该账号正在出片，不能改名")
    try:
        await asyncio.to_thread(doubao.rename, name, new_name)
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        raise HTTPException(409, str(exc)[:200]) from exc
    return {"ok": True, "account": new_name, "renamed_from": name}


@app.post("/api/admin/doubao/accounts/{name}/verify")
async def admin_doubao_account_verify(name: str,
                                      x_admin_key: str | None = Header(default=None)):
    """豆包账号验活（打 /alice/user/get_account_info）。"""
    _admin_auth(x_admin_key)
    if name not in doubao.accounts:
        raise HTTPException(404, "account not found")
    res = await doubao.verify(name)
    return {"ok": bool(res.get("ok")), "reason": res.get("reason", ""),
            "info": res.get("info", {})}


@app.post("/api/admin/doubao/accounts/reset-quotas")
async def admin_doubao_reset_quotas(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"ok": True, "reset": await asyncio.to_thread(doubao.reset_all)}


@app.post("/api/admin/doubao/accounts/{name}/diagnose")
async def admin_doubao_diagnose(name: str,
                                x_admin_key: str | None = Header(default=None)):
    """诊断：这个号是「登录失效」还是「被限流」，并给出该做什么。

    对齐 USAGE.md「号被风控了」的排查流程，把人工判断自动化。
    """
    _admin_auth(x_admin_key)
    if name not in doubao.accounts:
        raise HTTPException(404, "account not found")
    return await doubao.diagnose(name)


@app.post("/api/admin/doubao/accounts/{name}/recover")
async def admin_doubao_recover(name: str,
                               x_admin_key: str | None = Header(default=None)):
    """人工恢复：清冷却 + 放回【待激活】（随后应点「验证」复核）。"""
    _admin_auth(x_admin_key)
    if name not in doubao.accounts:
        raise HTTPException(404, "account not found")
    return await asyncio.to_thread(doubao.recover, name)


@app.post("/api/admin/doubao/accounts/{name}/clear-cooldown")
async def admin_doubao_clear_cooldown(name: str,
                                      x_admin_key: str | None = Header(default=None)):
    """只清冷却（限流到点前想立刻重试）。"""
    _admin_auth(x_admin_key)
    if name not in doubao.accounts:
        raise HTTPException(404, "account not found")
    return await asyncio.to_thread(doubao.clear_cooldown, name)


@app.get("/api/admin/doubao/accounts/{name}/export-cookie")
async def admin_doubao_export_ck(name: str,
                                 x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in doubao.accounts:
        raise HTTPException(404, "account not found")
    ck = doubao.export_ck(name)
    meta = doubao_store.read_meta(name)
    return {
        "name": name,
        "channel": "doubao",
        "count": len(doubao_login.parse_ck(ck)),
        "cookie_header": ck,
        "mobile": meta.get("mobile") or "",
        "source": meta.get("source") or "",
        "formats": {"header": ck, "json": ck},
    }


# ===== 豆包参考图上传（测试生成用） =====


@app.post("/api/admin/doubao/upload-image")
async def admin_doubao_upload_image(request: Request,
                                    x_admin_key: str | None = Header(default=None)):
    """接收面板上传的参考图并落盘，返回可传给豆包出片接口的本地路径。

    面板侧是 multipart（字段名 image），落盘到 DOUBAO_UPLOAD_DIR；
    出片时 `DoubaoImageVideoClient.upload_image()` 会把它送到豆包
    `prepare_upload → ApplyImageUpload → PUT TOS` 三段式链路。
    """
    _admin_auth(x_admin_key)
    form = await request.form()
    path = await _save_uploaded_image(form)
    if not path:
        raise HTTPException(400, "没有收到图片文件（字段名用 image）")
    return {
        "ok": True,
        "path": path,
        "url": f"{config.PUBLIC_BASE}/doubao-uploads/{Path(path).name}",
        "name": Path(path).name,
        "size": os.path.getsize(path),
    }


async def _save_uploaded_image(form) -> str:
    """把上传文件落盘到 DOUBAO_UPLOAD_DIR，做后缀/MIME/大小校验。"""
    item = None
    for key in ("image", "file", "image[]", "ref"):
        values = form.getlist(key)
        if values:
            item = values[0]
            break
    if item is None or not hasattr(item, "read"):
        return ""
    data = await item.read()
    if not data:
        return ""
    if len(data) > config.REFERENCE_IMAGE_MAX_BYTES:
        raise HTTPException(422, "参考图超过单文件大小限制")
    suffix = Path(getattr(item, "filename", "") or "").suffix.lower() or ".png"
    if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(422, "参考图仅支持 PNG / JPEG / WEBP")
    try:
        from PIL import Image
        from io import BytesIO

        with Image.open(BytesIO(data)) as im:
            im.verify()
    except Exception:
        raise HTTPException(422, "上传的文件不是有效图片")
    return await asyncio.to_thread(_write_upload_bytes, data, suffix)


def _write_upload_bytes(data: bytes, suffix: str) -> str:
    root = Path(doubao_pool.UPLOAD_DIR)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"ref_{int(time.time())}_{uuid.uuid4().hex[:8]}{suffix}"
    path.write_bytes(data)
    return str(path)


# ===== 豆包测试生成 =====


@app.post("/api/admin/doubao/accounts/{name}/test-generate")
async def admin_doubao_test_generate(name: str, body: DoubaoTgBody,
                                     x_admin_key: str | None = Header(default=None)):
    """豆包账号级测试生成：钉死该账号出片（可带参考图）。"""
    _admin_auth(x_admin_key)
    if name not in doubao.accounts:
        raise HTTPException(404, "account not found")
    # ★ 参数校验放在最前：越界时长直接 422，**不建任务、不打上游**。
    #   （放在冷却检查之后会被「冷却中」抢先返回，用户看不到"时长本身错了"。）
    model_cn = doubao_pool.resolve_model(body.model)
    lo, hi = doubao_pool.duration_range_for(model_cn)
    if not doubao_pool.duration_supported(model_cn, body.duration):
        raise HTTPException(
            422, f"{model_cn} 支持的时长为 {lo}~{hi} 秒（收到 {body.duration} 秒）")
    if body.ratio and body.ratio not in doubao_pool.RATIOS:
        raise HTTPException(422, f"ratio 仅支持 {doubao_pool.RATIOS}")
    if doubao.lock_for(name).locked():
        raise HTTPException(409, "该账号正在执行任务，稍后再试")
    image_paths: list[str] = []
    for ref in body.reference_images or []:
        ref = (ref or "").strip()
        if not ref:
            continue
        if ref.startswith("data:image"):
            data, suffix = _decode_data_image_upload(ref)
            image_paths.append(await asyncio.to_thread(_write_upload_bytes, data, suffix))
        elif os.path.isfile(ref):
            image_paths.append(ref)
        elif ref.startswith("http"):
            local = await _download_image_for_doubao(ref)
            if local:
                image_paths.append(local)
    task_id = "test_" + uuid.uuid4().hex
    TEST_JOBS[task_id] = {
        "account": name,
        "channel": "doubao",
        "prompt": body.prompt,
        "model": body.model,
        "duration": body.duration,
        "ratio": body.ratio,
        "reference_images": image_paths,
        "status": "running",
        "started_at": time.time(),
        "result": None,
        "error": "",
    }
    asyncio.create_task(_run_doubao_test_generate(
        task_id, name, body.prompt, body.ratio, body.duration, body.model, image_paths))
    return {"task_id": task_id, "account": name, "channel": "doubao",
            "reference_images": image_paths}


def _decode_data_image_upload(data_url: str) -> tuple[bytes, str]:
    m = re.match(r"^data:image/(\w+);base64,(.*)$", data_url, re.S)
    if not m:
        raise HTTPException(422, "参考图 data URL 格式错误")
    fmt = m.group(1).lower()
    suffix = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp"}.get(
        fmt, ".png")
    try:
        return base64.b64decode(m.group(2)), suffix
    except Exception as exc:
        raise HTTPException(422, f"参考图 base64 解码失败: {exc}") from exc


async def _download_image_for_doubao(url: str) -> str:
    """把公网参考图抓到本地（豆包上传链路只吃本地文件/字节）。"""
    import aiohttp

    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.get(url, headers={"User-Agent": "dola-pool/1.0"}) as r:
                if r.status != 200:
                    return ""
                data = await r.read()
    except Exception:
        return ""
    if not data or len(data) > config.REFERENCE_IMAGE_MAX_BYTES:
        return ""
    suffix = Path(url.split("?")[0]).suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
        suffix = ".png"
    return await asyncio.to_thread(_write_upload_bytes, data, suffix)


async def _run_doubao_test_generate(task_id: str, name: str, prompt: str,
                                    ratio: str | None, duration: int, model: str,
                                    image_paths: list[str]) -> None:
    """豆包测试生成的执行体（进度写 TEST_JOBS + 任务库，供面板轮询）。

    两条入口都会调到这里：
      1) 面板点「测试生成」（任务行已由 admin_doubao_test_generate 预建）；
      2) 重启恢复 / 看门狗重派（**任务行已存在**，不能再 create）。
    因此 create 必须容错、TEST_JOBS 必须能就地补建。
    """
    async def _beat(now=None):
        try:
            _update_bg(task_id, last_poll_at=time.time())
        except Exception:
            pass

    async def _job() -> dict:
        """取（或补建）TEST_JOBS 条目 —— 恢复路径下没有预建条目。"""
        job = TEST_JOBS.get(task_id)
        if job is None:
            job = {"account": name, "channel": "doubao", "prompt": prompt,
                   "model": model, "duration": duration, "ratio": ratio,
                   "reference_images": list(image_paths or []),
                   "status": "running", "started_at": time.time(),
                   "result": None, "error": "", "recovered": True}
            TEST_JOBS[task_id] = job
        return job

    try:
        # 任务行可能已存在（恢复/重派路径）→ 重复 create 会撞 UNIQUE 约束，
        # 这里只补建缺失的行，已存在的沿用原行（保留 created_at）。
        if not await asyncio.to_thread(store.get, task_id):
            try:
                await asyncio.to_thread(store.create, task_id, model, prompt,
                                        ratio or "default", duration, account=name,
                                        channel="doubao",
                                        reference_images=json.dumps(image_paths or [],
                                                                    ensure_ascii=False))
            except Exception as exc:
                # 并发下可能刚被别人建出来：不是错误
                if "UNIQUE" not in str(exc).upper():
                    raise
        job = await _job()
        await _update(task_id, status="processing", started_at=time.time(),
                      account=name, channel="doubao",
                      reference_images=json.dumps(image_paths or [], ensure_ascii=False))
        result = await doubao.test_generate(name, prompt, ratio, duration, model,
                                            image_paths=image_paths or None,
                                            on_poll=_beat)
        job["result"] = result
        err = "" if result.get("ok") else result.get("error", "")
        job["status"] = "done" if result.get("ok") else "failed"
        job["error"] = err
        if result.get("ok"):
            await _update(task_id, status="completed",
                          video_url=result.get("video_url"),
                          finished_at=time.time())
        else:
            await _update(task_id, status="failed",
                          error=failure_text.summarize(err),
                          finished_at=time.time())
    except Exception as exc:
        job = await _job()
        job["status"] = "failed"
        job["error"] = failure_text.summarize(str(exc), limit=600)
        try:
            await _update(task_id, status="failed",
                          error=failure_text.summarize(str(exc)),
                          finished_at=time.time())
        except Exception:
            pass


@app.get("/api/admin/doubao/test-gen/{task_id}")
async def admin_doubao_test_gen_status(task_id: str,
                                       x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    job = TEST_JOBS.get(task_id)
    if not job:
        raise HTTPException(404, "test task not found")
    return job


@app.get("/api/admin/tasks")
async def admin_tasks(limit: int = 50, channel: str = "",
                      x_admin_key: str | None = Header(default=None)):
    """任务中心：可按渠道筛选（channel=dola|doubao，留空=全部）。"""
    _admin_auth(x_admin_key)
    ch = (channel or "").strip().lower()
    rows = await asyncio.to_thread(
        store.recent_tasks, min(max(limit, 1), 200),
        None, ch if ch in ("dola", "doubao") else None)
    return {"tasks": rows, "channel": ch or "all"}


@app.delete("/api/admin/tasks/{task_id}")
async def admin_task_delete(task_id: str, x_admin_key: str | None = Header(default=None)):
    """删除任意状态的任务记录；已完成任务连带清理视频文件与参考图缩略图。"""
    _admin_auth(x_admin_key)
    row = await asyncio.to_thread(store.get, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    if row.get("status") in ("queued", "processing"):
        raise HTTPException(409, "任务进行中，不能删除；请等任务结束")
    # 视频文件（上百 MB）+ 缩略图删除走线程：磁盘 unlink 本机极慢，别冻事件循环
    await asyncio.to_thread(_delete_task_files, row)
    await asyncio.to_thread(store.delete_task, task_id)
    return {"ok": True, "deleted": task_id}


@app.get("/api/admin/videos")
async def admin_videos(limit: int = 100, channel: str = "",
                       x_admin_key: str | None = Header(default=None)):
    """视频管理：已完成任务的列表 + 文件元数据（老任务按需从磁盘补大小）。

    channel=dola|doubao 时只列该渠道（留空=全部）。
    """
    _admin_auth(x_admin_key)
    ch = (channel or "").strip().lower()
    ch = ch if ch in ("dola", "doubao") else None
    rows = await asyncio.to_thread(
        store.recent_tasks, min(max(limit, 1), 500), None, ch)
    out = []
    for r in rows:
        if r.get("status") != "completed":
            continue
        item = dict(r)
        item["channel"] = item.get("channel") or "dola"
        if item.get("video_url"):
            local = Path(config.DOWNLOAD_DIR) / Path(item["video_url"]).name
        else:
            local = None
        if local and local.exists():
            item["file_exists"] = True
            try:
                item["file_size"] = os.path.getsize(local)
            except OSError:
                item["file_size"] = item.get("video_size")
            if not item.get("video_size"):
                item["video_size"] = item["file_size"]
        else:
            item["file_exists"] = False
        out.append(item)
    out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    stats = await asyncio.to_thread(store.video_stats, ch)
    return {
        "videos": out,
        "total": stats["total"],
        "available": stats["available"],
        "channel": ch or "all",
    }


@app.delete("/api/admin/videos/{task_id}")
async def admin_video_delete(task_id: str, x_admin_key: str | None = Header(default=None)):
    """删除视频文件并同步删除对应的任务记录。"""
    _admin_auth(x_admin_key)
    row = await asyncio.to_thread(store.get, task_id)
    if not row or row.get("status") != "completed":
        raise HTTPException(404, "task not found")
    await asyncio.to_thread(_delete_task_files, row)
    await asyncio.to_thread(store.delete_task, task_id)
    return {"ok": True, "deleted": task_id}


@app.get("/api/admin/stats")
async def admin_stats(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    st = await asyncio.to_thread(store.stats)
    busy_accounts = await asyncio.to_thread(store.busy_account_ids)
    accs = pool.list_accounts(processing_accounts=busy_accounts)
    sched = [a for a in accs if a["scheduling"] and not a["cooling"]]
    st["total_accounts"] = len(accs)
    st["available_accounts"] = sum(1 for a in sched if a["remaining"] > 0)
    st["total_remaining"] = sum(a["remaining"] for a in sched)
    totals = st.pop("per_account_total", {})
    st["per_account"] = [{**a, "completed_total": totals.get(a["name"], 0)} for a in accs]
    st.update(_group_summary(accs))
    return st


@app.get("/api/admin/keys")
async def admin_keys(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    keys = []
    for key in await asyncio.to_thread(store.list_keys):
        usage = await asyncio.to_thread(store.key_usage, store.hash_api_key(key["key"]))
        keys.append({**key, **{
            "today_total": usage["total"],
            "today_completed": usage["completed"],
            "today_failed": usage["failed"],
            "today_active": usage["active"],
            "today_queued": usage["queued"],
        }})
    return {
        "keys": keys,
        "env_keys": len(config.API_KEYS),
        # 面板的「允许时长」勾选项必须跟着 NATIVE_DURATION_MAX 走：前端原先硬编码
        # 5/10/15/30，开了任意时长也勾不到新档位，等于功能从面板上用不了。
        "supported_durations": config.all_durations(),
    }


@app.post("/api/admin/keys")
async def admin_key_create(body: KeyCreate, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    allowed = _normalize_allowed_durations(body.allowed_durations)
    return {"created": await asyncio.to_thread(
        store.create_key,
        body.name,
        daily_limit=body.daily_limit,
        concurrency_limit=body.concurrency_limit,
        allowed_durations=allowed,
        expires_at=body.expires_at,
    )}


@app.patch("/api/admin/keys/{key}")
async def admin_key_patch(key: str, body: KeyPatch,
                          x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not await asyncio.to_thread(store.get_key, key):
        raise HTTPException(404, "api key not found")
    fields = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if body.daily_limit is not None:
        fields["daily_limit"] = body.daily_limit
    if body.concurrency_limit is not None:
        fields["concurrency_limit"] = body.concurrency_limit
    if body.allowed_durations is not None:
        fields["allowed_durations"] = _normalize_allowed_durations(body.allowed_durations)
    if body.expires_at is not None:
        fields["expires_at"] = body.expires_at
    await asyncio.to_thread(store.update_key, key, **fields)
    return {"ok": True}


@app.delete("/api/admin/keys/{key}")
async def admin_key_delete(key: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not await asyncio.to_thread(store.get_key, key):
        raise HTTPException(404, "api key not found")
    await asyncio.to_thread(store.delete_key, key)
    return {"ok": True}


# 面板单文件前端（放最后，保证上面的路由优先）
app.mount("/", StaticFiles(directory="web", html=True), name="web")
