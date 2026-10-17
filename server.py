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
import asset_store
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
import pool_engine_service
from pool_engine_service import manager as poolengine_mgr
import engine_fallback
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

# hint855：外部进程（ffmpeg/ffprobe/tasklist 等）不得弹控制台窗。
# 注意 CREATE_NO_WINDOW 在父进程已有控制台时会被忽略，必须叠加 DETACHED_PROCESS。
_NO_WIN = 0
if os.name == "nt":
    _NO_WIN = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
               | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

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
# 出片引擎（run_pool.py）的成片目录：out/mp4/。引擎与面板共用同一份 acc/ 号池，
# 但产物分开存（面板 downloads/、引擎 out/mp4/），避免两套命名互相覆盖。
Path(pool_engine_service.MP4DIR).mkdir(parents=True, exist_ok=True)
app.mount("/engine-mp4", StaticFiles(directory=str(pool_engine_service.MP4DIR)),
          name="engine-mp4")
# 出片引擎的**图片**产物目录：out/img/（Seedream 出图）。
# ★ 与 engine-mp4 分开：一条图片任务产**多张**图，且前端要直接 <img> 渲染。
Path(pool_engine_service.IMGDIR).mkdir(parents=True, exist_ok=True)
app.mount("/engine-img", StaticFiles(directory=str(pool_engine_service.IMGDIR)),
          name="engine-img")
# 参考图缩略图（任务结束后唯一能回看的副本）；文件名 = <task_id>_<index>.jpg
Path(config.THUMB_DIR).mkdir(parents=True, exist_ok=True)
app.mount("/thumbs", StaticFiles(directory=config.THUMB_DIR), name="thumbs")
# 豆包测试生成上传的参考图（面板上传后回填给出片链路用，仅本机/内网访问）
Path(doubao_pool.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
app.mount("/doubao-uploads", StaticFiles(directory=str(doubao_pool.UPLOAD_DIR)),
          name="doubao-uploads")
# 素材库（创作台）：用户上传的参考素材 + 生成产物入库。
# ★ 与 /thumbs、refs/ 严格分开：这里面是**用户长期资产**，
#   media.cleanup_local_references / sweep_stale_references 都不认这个目录，
#   所以任务终态与重启都不会删它（见 media.is_asset_reference 与方案 §3.1）。
asset_store.ensure_dirs()
assets_root = asset_store.assets_root()
app.mount("/assets", StaticFiles(directory=str(assets_root)), name="assets")
# 素材预览图（512px，面板网格墙用）；单独挂载避免把 .thumbs 暴露在 /assets 顶层
app.mount("/asset-thumbs", StaticFiles(directory=str(asset_store.thumbs_root())),
          name="asset-thumbs")


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
            creationflags=_NO_WIN,   # hint855：ffprobe 不弹控制台
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
    # ★ 2026-10-13 新增：出片渠道。`doubao` = 豆包域纯 HTTP 号池
    # （www.doubao.com，独立配额池，走 doubao_pool）；默认 `dola` 不变。
    channel: str = "dola"


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
    # 2026-10-15 渠道改造：启动【全局窗口嗅探器】。710022002 是全局窗口开/关，
    # 窗口一命中所有走纯 API 的 worker 会同刻爆发提交；池里没有 cookie 号时自动跳过。
    try:
        win = pool.start_window_engine()
        if win.get("enabled"):
            print(f"[startup] 窗口嗅探器已启动：探针号={win.get('probe_account')} "
                  f"周期={win.get('period_sec')}s（DOLA_WINDOW_SNIFF=1）", flush=True)
        else:
            print("[startup] 窗口嗅探器未启动（DOLA_WINDOW_SNIFF=0 或池中无 cookie 号）", flush=True)
    except Exception as exc:
        print(f"[startup] 窗口嗅探器启动失败（退回旧行为）: {exc}", flush=True)
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
    # 引擎退出 → 自动跑豆包降级（把没出片的兜底做掉）。
    # reaper 是普通线程，必须用 call_soon_threadsafe 挂回事件循环。
    _install_engine_exit_hook()


def _install_engine_exit_hook() -> None:
    """把「引擎退出」桥接到事件循环里的豆包降级。

    只在引擎**自己跑完/崩溃**时降级；用户点「停止」属于主动中断，不降级
    （否则用户想停，后台却偷偷去豆包生产，违背意图）。
    """
    loop = asyncio.get_running_loop()

    def _on_exit(rc, was_stopping: bool) -> None:
        if was_stopping:
            print("[fallback] 引擎由用户停止，跳过豆包降级", flush=True)
            return
        def _schedule() -> None:
            async def _go() -> None:
                try:
                    res = await _run_doubao_fallback(f"引擎退出 rc={rc}")
                    if res.get("skipped"):
                        print(f"[fallback] 跳过：{res.get('reason')}", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"[fallback] 降级失败（已忽略）: {exc}", flush=True)
            asyncio.create_task(_go())
        try:
            loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            # 事件循环已关闭（服务正在退出）：不降级，交给下次手动触发
            pass

    poolengine_mgr.on_exit = _on_exit


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
    # ★ 2026-10-13 新增：channel=doubao 时改走豆包域纯 HTTP 号池，
    #   不进 dola 号池/浏览器兜底（两域账号与配额完全隔离）。
    if (req.channel or "").strip().lower() == "doubao":
        return await _submit_doubao_task(client, req.model, req.prompt,
                                         ratio, duration, refs)
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


# ===== 豆包渠道对外 REST（2026-10-13 新增，channel=doubao 分流） =====


async def _submit_doubao_task(client, model, prompt, ratio, duration, reference_images):
    """channel=doubao 受理：校验 → 建任务行（channel=doubao）→ 后台跑豆包号池。"""
    model_cn = doubao_pool.resolve_model(model)
    if not doubao_pool.duration_supported(model_cn, duration):
        lo, hi = doubao_pool.duration_range_for(model_cn)
        raise HTTPException(
            422, f"{model_cn} 支持的时长为 {lo}~{hi} 秒（收到 {duration} 秒）")
    if ratio and ratio not in doubao_pool.RATIOS:
        raise HTTPException(422, f"ratio 仅支持 {doubao_pool.RATIOS}")
    if duration not in client["allowed_durations"]:
        raise HTTPException(422, f"当前 API Key 不允许生成 {duration} 秒视频")
    try:
        reference_images = await validate_reference_urls(reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        reference_images = await asyncio.to_thread(materialize_data_urls, reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not doubao.accounts:
        raise HTTPException(503, "doubao pool is empty（doubao_accounts/ 下没有账号）")
    if len(prompt or "") > config.MAX_PROMPT_CHARS:
        raise HTTPException(
            422, f"提示词过长（上限 {config.MAX_PROMPT_CHARS} 字符，收到 {len(prompt)}）")
    task_id = "video_" + uuid.uuid4().hex
    try:
        store.create(
            task_id,
            model_cn,
            prompt,
            ratio or "16:9",
            duration,
            reference_images=json.dumps(reference_images, ensure_ascii=False),
            channel="doubao",
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
    print(f"[api][doubao] 任务受理 task={task_id} model={model_cn} "
          f"duration={duration} refs={len(reference_images)}", flush=True)
    asyncio.create_task(_run_doubao_pool_task(
        task_id, model_cn, prompt, ratio or "16:9", duration,
        reference_images, client))
    return TaskResponse(
        id=task_id, status="queued", model=model_cn, prompt=prompt,
        progress=_progress_payload(status="queued", created_at=time.time(),
                                   duration=duration),
    )


async def _run_doubao_pool_task(task_id, model_cn, prompt, ratio, duration,
                                reference_images, client):
    """豆包渠道对外任务执行体：走 doubao_pool.generate_video（选号/换号/冷却/记账内建）。"""
    await _update(task_id, status="processing", started_at=time.time(),
                  channel="doubao")
    deadline = time.time() + config.TASK_DEADLINE

    def _beat(_now=None):
        """★ 2026-10-14：出片要等 5~15 分钟，必须持续心跳 last_poll_at，
        否则看门狗（processing 且超过 STALE_TASK_SECONDS 无更新）会把
        仍在正常生成的任务判成僵尸重派 → 上游重复出片、重复扣额度。"""
        try:
            _update_bg(task_id, last_poll_at=time.time())
        except Exception:
            pass

    try:
        res = await doubao.generate_video(
            prompt, ratio=ratio, duration=duration, model=model_cn,
            image_paths=reference_images or None,
            max_attempts=VIDEO_MAX_ATTEMPTS or 0,
            deadline=deadline, on_poll=_beat)
    except Exception as exc:
        await _update(task_id, status="failed",
                      error=f"{type(exc).__name__}: {exc}"[:500],
                      finished_at=time.time())
        return
    await _update(task_id, status="completed",
                  video_url=res.get("video_url", ""),
                  conversation_id=res.get("conversation_id", ""),
                  account=res.get("account", ""),
                  finished_at=time.time())


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
        # 2026-10-15 渠道改造：窗口嗅探器实时状态（窗口是否开着、探测次数、命中窗口数）
        "window": pool.window_state(),
    }


@app.get("/api/admin/window")
async def admin_window():
    """窗口嗅探器状态（诊断用）：开着/关着、探测次数、命中窗口数、最近窗口时刻。"""
    return pool.window_state()


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


class PoolEngineParams(BaseModel):
    """出片引擎参数（pool_engine_service → run_pool.py 的 I794_* 环境变量）。

    全部可选；留空的项用引擎内建默认值。字段名与 pool_engine_service.PARAM_ENV 对齐。
    """
    pool_target: int | None = Field(None, ge=1, le=500)      # 爆发池保持可用号数
    max_per_acc: int | None = Field(None, ge=1, le=10)       # 每号每日出片数
    probe_standby: int | None = Field(None, ge=1, le=50)     # 探针备用数
    sniff_period: float | None = Field(None, ge=2, le=60)    # 探针周期（秒）
    sniff_jitter: float | None = Field(None, ge=0, le=30)
    burst_conc: int | None = Field(None, ge=1, le=200)       # 爆发并发
    harvest_workers: int | None = Field(None, ge=1, le=64)
    harvest_timeout: int | None = Field(None, ge=60, le=86400)   # 单 conv 收割上限（秒）
    harvest_interval: int | None = Field(None, ge=5, le=1800)
    register_workers: int | None = Field(None, ge=1, le=32)
    task_attempts: int | None = Field(None, ge=1, le=20)
    run_seconds: int | None = Field(None, ge=0, le=86400)    # 0=不限
    ratio: str | None = None                                  # 16:9 / 9:16 / 1:1 ...
    duration: int | None = Field(None, ge=5, le=30)
    model: str | None = None           # seedance_v2.5（30s 必须）/ seedance_v2.0
    watermark_fallback: str | None = None    # save / retry / fail
    burst_pats: str | None = None     # 爆发号前缀，逗号分隔
    probe_pat: str | None = None      # 探针号前缀
    # ---- 以下不是 I794_* env，而是面板侧策略（引擎启动时由面板消费）----
    channel: str | None = None        # dola（优先，失败降级豆包）/ doubao（只用豆包）
    k_max: int | None = Field(None, ge=1, le=50)      # 自适应倍数上限
    k_min: int | None = Field(None, ge=1, le=20)      # 自适应倍数下限
    min_pool: int | None = Field(None, ge=1, le=500)  # 号池下限
    max_pool: int | None = Field(None, ge=0, le=5000) # 号池上限，0=不限


# 引擎策略参数的默认值（不落 env，只在本进程用于算号池规模 / 选渠道）
ENGINE_POLICY_DEFAULTS = {
    "channel": "dola", "k_max": 20, "k_min": 2, "min_pool": 10, "max_pool": 0,
}
ENGINE_SETTINGS_KEY = "engine_params"


def _load_engine_params() -> dict:
    """读已保存的引擎参数（app_settings.engine_params）；无则返回空 dict。"""
    import sqlite3 as _sq
    conn = _sq.connect(config.POOL_DB_PATH, timeout=15)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        row = conn.execute(
            "SELECT v FROM app_settings WHERE k=?", (ENGINE_SETTINGS_KEY,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    try:
        d = json.loads(row[0])
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_engine_params(d: dict) -> str:
    """保存引擎参数，返回保存时间字符串。"""
    import sqlite3 as _sq
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    payload = dict(d or {})
    payload["_saved_at"] = stamp
    conn = _sq.connect(config.POOL_DB_PATH, timeout=15)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_settings(k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        conn.execute(
            "INSERT INTO app_settings(k,v) VALUES(?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (ENGINE_SETTINGS_KEY, json.dumps(payload, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()
    return stamp


def _engine_effective_params() -> dict:
    """引擎内建默认 + 面板策略默认 + 已保存覆盖，合成一份完整参数。"""
    out = dict(pool_engine_service.DEFAULTS)
    out.update(ENGINE_POLICY_DEFAULTS)
    saved = _load_engine_params()
    for k, v in saved.items():
        if k.startswith("_"):
            continue
        if v not in (None, ""):
            out[k] = v
    return out


def _split_prompt_blocks(text: str, *, whole: bool = False) -> list[str]:
    """把提示词文本拆成任务列表。

    为什么不能按行拆：任务提示词天生是多行的 —— 用户会写带分镜时间轴、
    段落描述、旁白字幕的完整剧本（"0—4秒｜大远景｜宇宙战场\\n宇宙一片死寂…\\n
    4—9秒｜中景｜…"）。历史实现用 splitlines() 按行拆，结果一段剧本被拆成
    十几条独立任务，每条只拿到一两行，出来的片子全是碎片（用户提交 1 条 30 秒
    片，任务列表却多出十几条 5 秒任务）。

    两种调用方式：
      · ``whole=True``（**创作台提交 / 发起出片**）：整段输入作为**一条**任务，
        段内换行全部保留。用户在创作台写的就是一部完整片子，不该被拆。
      · ``whole=False``（**批量出片**，默认）：按「空行分段」拆成多条任务，
        一段一条；没有空行时退化为一行一条（textarea 一行一条的旧行为不变）。

    两种模式都跳过 # 注释行与纯空白段。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if whole:
        # 整段作为一条任务：只剔注释行与空行，其余原样拼接（保留换行）
        kept = [ln for ln in raw.split("\n")
                if ln.strip() and not ln.strip().startswith("#")]
        s = "\n".join(kept).strip()
        return [s] if s else []

    if "\n\n" not in raw:
        # 没有空行 → 一行一条（旧行为，批量出片走这里）
        out = []
        for ln in raw.split("\n"):
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)
        return out
    blocks: list[str] = []
    for chunk in raw.split("\n\n"):
        kept = [ln for ln in chunk.split("\n")
                if ln.strip() and not ln.strip().startswith("#")]
        if not kept:
            continue
        # 段内去掉首尾空行，中间的空行压成一个换行，避免提示词里出现连续空行
        s = "\n".join(kept).strip()
        if s:
            blocks.append(s)
    return blocks


def _compute_pool_target(n_tasks: int, params: dict) -> dict:
    """按任务量自适应算号池目标（与前端 ecPreview / bgRecount 同一公式）。

    need = ceil(任务数 / 每号出片数)              —— 一个窗口打完的硬需求
    K    = clamp(round(20 / sqrt(need)), K_MIN, K_MAX)  —— 任务少→高倍冗余，任务多→低倍
    pool = clamp(ceil(need * K), MIN_POOL, MAX_POOL)    —— MAX_POOL=0 表示不设上限

    为什么这样：pool_target 决定单个窗口能打出去多少条（每号吃 max_per_acc 条）。
    任务少时放大池子力争一窗清完；任务多时必须收窄，否则注册线程要现场补几百个号
    （≈15 秒/个、5 并发），补号期间窗口白过，反而更慢。
    """
    per = max(1, int(params.get("max_per_acc") or 2))
    k_max = max(1, int(params.get("k_max") or 20))
    k_min = max(1, int(params.get("k_min") or 2))
    min_pool = max(1, int(params.get("min_pool") or 10))
    max_pool = int(params.get("max_pool") or 0)
    need = max(1, -(-int(n_tasks) // per))          # ceil 除法
    k = int(round(20.0 / (need ** 0.5)))
    k = min(max(k, k_min), k_max)
    target = max(min_pool, -(-(need * k) // 1))
    if max_pool > 0:
        target = min(target, max_pool)
    return {"need": need, "k": k, "pool_target": int(target),
            "per": per, "k_min": k_min, "k_max": k_max,
            "min_pool": min_pool, "max_pool": max_pool}


class PoolEngineSubmit(BaseModel):
    """「批量出片」提交：提示词 + 队列模式 + 可选的本次出片参数覆盖。"""
    prompts: str = ""
    mode: str = "append"              # append（追加）/ replace（覆盖）
    duration: int | None = Field(None, ge=5, le=30)
    ratio: str | None = None
    model: str | None = None
    # ★ 测试/创作台场景：每个任务只用 1 个号（N 个视频 = N 个号，一号一片）。
    #   默认 None = 沿用「系统设置 · 每号出片数」。
    one_per_account: bool = False
    # 本次是否把既有号也算进可派发池（测试场景想尽量用存量号时打开）
    use_all_accounts: bool = True


class PoolDispatchBody(BaseModel):
    """池级「发起出片」：N 条提示词 → 引擎按探测窗口整池提交。

    与账号级 test-generate 的区别（用户明确要求后者改为走这条链路）：
      * 不钉死单个账号 —— 从**账号池**里选 N 个号；
      * 不在这里出片 —— 写进引擎队列，等 710 窗口放开时**同刻提交**；
      * 某个号这轮没成功 → 任务回队，**下一轮换别的号**再试。

    prompt 支持多行（一行一条提示词）；count = 每条提示词生成几个视频。
    最终视频数 = 有效行数 × count，也就是要占用的号数（一号一片）。
    """
    prompt: str = Field(..., min_length=1)
    count: int = Field(1, ge=1, le=200)      # 每条提示词生成几个
    duration: int = Field(30, ge=5, le=30)
    ratio: str | None = None
    model: str | None = None
    reference_images: list[str] = Field(default_factory=list)
    wait: bool = False                        # True=同步等窗口结果（测试用）；False=立即返回
    wait_seconds: int = Field(600, ge=30, le=7200)


class EngineParamsBody(BaseModel):
    """保存引擎参数：只提交要改的键；reset=True 时恢复默认。"""
    params: PoolEngineParams = Field(default_factory=PoolEngineParams)
    reset: bool = False


class PoolEngineStart(BaseModel):
    """启动出片引擎：提示词（一行一条）+ 参数。"""
    prompts: str = ""                 # 多行；留空 = 用现有 tasks.txt
    params: PoolEngineParams = Field(default_factory=PoolEngineParams)


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
# ★ 2026-10-16：比例表补 3:2 / 2:3 / 4:5 / 5:4。
#   证据：视频侧档位来自网页版真实配置（extensions/dola30/
#   doubao-skill-pack-response.json 的 ratio.options 只有 6 档，**没有** 3:2 系）；
#   图片侧参考实现（open-ai-canvas）的豆包图片吸附表更宽，含 3:2/2:3/4:5/5:4。
#   故**视频用子集**、图片用全集 —— 否则用户"选了 3:2 却出 16:9"，
#   属于静默不生效（比报错更糟）。
_GENSET_RATIOS = ("21:9", "16:9", "3:2", "4:3", "1:1",
                  "4:5", "5:4", "3:4", "2:3", "9:16")
_GENSET_VIDEO_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")
# Seedream 文生图档位（**没有时长**：出图没有"几秒"这个概念）
_GENSET_IMAGE_MODELS = ("seedream-4.0", "seedream-4.5",
                        "seedream-5.0-pro", "seedream-5.0-flash")

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
        # ★ 「生成设置」页管的是**视频**档位（模型/时长/比例），所以比例给
        #   视频子集：3:2 系在视频链路上游不生效，列出来会让用户"选了不生效"。
        #   图片的比例在创作台按模式下发（见 studio/capabilities 的 by_mode）。
        d = _load_genset(c)
        if c == "doubao":
            return {"model": d.get("model") or "seedance-2.0",
                    "duration": int(d.get("duration") or 5),
                    "ratio": d.get("ratio") or "16:9",
                    "models": list(_GENSET_MODELS_DOUBAO),
                    "durations": {k: list(v) for k, v in _GENSET_DURATIONS_DOUBAO.items()},
                    "ratios": list(_GENSET_VIDEO_RATIOS)}
        return {"model": d.get("model") or "seedance-2.0",
                "duration": int(d.get("duration") or 5),
                "ratio": d.get("ratio") or "16:9",
                "models": list(_GENSET_MODELS),
                "durations": {k: list(v) for k, v in _GENSET_DURATIONS.items()},
                "ratios": list(_GENSET_VIDEO_RATIOS)}

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
    # ★ 这里校验**视频**比例子集：本端点保存的是视频生成设置，
    #   收下 3:2 会存成一个"上游不生效"的档位（静默不生效比报错更糟）。
    if body.ratio not in _GENSET_VIDEO_RATIOS:
        raise HTTPException(422, f"ratio 仅支持 {_GENSET_VIDEO_RATIOS}")
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


# ===== 出片引擎（run_pool.py 服务化；链路完全按 A：注册→探针→爆发→收割）=====
# 说明：这条链路是一个长驻多线程引擎，不在事件循环里重写，而是进程级包装。
# 面板只管「下发参数 / 看状态 / 看日志 / 看成片」，引擎负责干活。

@app.post("/api/admin/pool-engine/start")
async def admin_pool_engine_start(body: PoolEngineStart,
                                  x_admin_key: str | None = Header(default=None)):
    """启动出片引擎。注意：会先暂停面板自己的窗口嗅探器（避免互抢窗口相位）。

    参数来源：显式传入的 > 「系统设置 · 引擎参数」里保存的 > 引擎内建默认。
    """
    _admin_auth(x_admin_key)
    cfg = body.model_dump()
    # 过滤掉未填写的参数（保留「用已保存值 / 默认值」的语义）
    params = {k: v for k, v in (cfg.get("params") or {}).items() if v not in (None, "")}
    # 没显式传的，用已保存的引擎参数补齐（这样「系统设置」改了才真的生效）
    if not params:
        params = _engine_effective_params()
    else:
        merged = _engine_effective_params()
        merged.update(params)
        params = merged
    # 清掉可能残留的清空/覆盖哨兵：否则引擎一启动就把队列清了/重装了
    for _flag in (pool_engine_service.CLEAR_FLAG, pool_engine_service.RELOAD_FLAG):
        try:
            _flag.unlink()
        except (FileNotFoundError, OSError):
            pass
    ok, msg = await asyncio.to_thread(
        poolengine_mgr.start, {"prompts": cfg.get("prompts"), "params": params})
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "message": msg}


@app.get("/api/admin/pool-engine/status")
async def admin_pool_engine_status(log_lines: int = 120,
                                   x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return await asyncio.to_thread(poolengine_mgr.status, max(0, min(1000, log_lines)))


@app.post("/api/admin/pool-engine/stop")
async def admin_pool_engine_stop(grace: float = 60.0,
                                 x_admin_key: str | None = Header(default=None)):
    """优雅停止：落 STOP 文件让引擎自己收尾（写 summary、收干净在飞的收割）。"""
    _admin_auth(x_admin_key)
    ok, msg = await asyncio.to_thread(poolengine_mgr.stop, max(5.0, min(600.0, grace)))
    return {"ok": ok, "message": msg}


@app.get("/api/admin/pool-engine/tasks")
async def admin_pool_engine_tasks(x_admin_key: str | None = Header(default=None)):
    """读现有 tasks.txt（提示词队列）。"""
    _admin_auth(x_admin_key)
    p = pool_engine_service.TASKS_FILE
    text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    prompts = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    return {"text": text, "count": len(prompts)}


@app.post("/api/admin/pool-engine/tasks")
async def admin_pool_engine_tasks_save(body: PoolEngineStart,
                                       x_admin_key: str | None = Header(default=None)):
    """保存提示词到 tasks.txt（不启动引擎）。

    ★ 多行提示词必须转义后再写：tasks.txt 是「一行一条」协议，而调用方
      （面板提示词编辑框）可能提交一整段分镜剧本。直接 splitlines 写入会把
      一条任务摊成 N 条，用户在任务中心看到的就是「提交 1 条、多出十几行」。
    """
    _admin_auth(x_admin_key)
    prompts = [ln.strip() for ln in (body.prompts or "").splitlines()
               if ln.strip() and not ln.strip().startswith("#")]
    if not prompts:
        raise HTTPException(400, "提示词为空")
    p = pool_engine_service.TASKS_FILE
    p.write_text(
        "\n".join(pool_engine_service.escape_prompt_newlines(x)
                  for x in prompts) + "\n",
        encoding="utf-8")
    return {"ok": True, "count": len(prompts)}


# ===== 引擎参数（系统设置 → 引擎参数）=====

@app.get("/api/admin/engine-params")
async def admin_engine_params(x_admin_key: str | None = Header(default=None)):
    """读引擎参数：已保存值（params）+ 内建默认（defaults）+ 策略默认。"""
    _admin_auth(x_admin_key)
    saved = _load_engine_params()
    return {
        "params": _engine_effective_params(),
        "saved": {k: v for k, v in saved.items() if not k.startswith("_")},
        "defaults": {**pool_engine_service.DEFAULTS, **ENGINE_POLICY_DEFAULTS},
        "saved_at": saved.get("_saved_at") or "",
    }


@app.post("/api/admin/engine-params")
async def admin_engine_params_save(body: EngineParamsBody,
                                   x_admin_key: str | None = Header(default=None)):
    """保存引擎参数（下次启动引擎生效）。reset=True 时清空已保存值。"""
    _admin_auth(x_admin_key)
    if body.reset:
        _save_engine_params({})
        return {"ok": True, "reset": True, "saved_at": _load_engine_params().get("_saved_at", "")}
    incoming = {k: v for k, v in body.params.model_dump().items() if v not in (None, "")}
    if not incoming:
        raise HTTPException(400, "没有要保存的参数")
    # 与已有值合并（只提交部分字段时不丢其它字段）
    merged = {k: v for k, v in _load_engine_params().items() if not k.startswith("_")}
    merged.update(incoming)
    stamp = _save_engine_params(merged)
    return {"ok": True, "params": _engine_effective_params(), "saved_at": stamp}


# ===== 批量出片（账号管理 → ＋批量出片）=====

@app.post("/api/admin/pool-engine/submit")
async def admin_pool_engine_submit(body: PoolEngineSubmit,
                                   x_admin_key: str | None = Header(default=None)):
    """批量出片：提示词 → 队列；按任务量自适应算号池；引擎没跑就自动启动。

    出片逻辑不变：仍是 run_pool.py 的「探针嗅窗 → 整池爆发 → 收割」。
    这里只负责入队、算 pool_target、拉起引擎。

    ★ 提示词按「空行分段」拆任务（_split_prompt_blocks）：一行一条的填法
      行为不变；粘进来的多行分镜剧本会作为**一条**任务整体入队，不会被
      按行拆成十几条碎片任务。
    """
    _admin_auth(x_admin_key)
    prompts = _split_prompt_blocks(body.prompts or "")
    if not prompts:
        raise HTTPException(400, "提示词不能为空")
    mode = (body.mode or "append").strip().lower()
    if mode not in ("append", "replace"):
        raise HTTPException(422, "mode 只能是 append 或 replace")

    p = pool_engine_service.TASKS_FILE
    mgr = poolengine_mgr
    engine_running = mgr.running() or bool(mgr._pid_file_running())
    if mode == "replace":
        final = list(prompts)
    else:
        existing = []
        if p.is_file():
            # 读回时还原换行转义：文件里存的是 \n 字面量，直接拿去拼接/去重
            # 会让「同一段多行提示词」看起来与本次提交不同，从而重复入队。
            existing = [pool_engine_service.unescape_prompt_newlines(ln.strip())
                        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                        if ln.strip() and not ln.strip().startswith("#")]
        # 追加时按内容去重（保留首次出现顺序），避免重复提交同一批提示词
        final = list(dict.fromkeys(existing + prompts))
    # ★ 写入前统一转义换行：tasks.txt 只能一行一条，多行提示词不转义就会被
    #   按行拆成 N 条独立任务（用户提交 1 条 30 秒片，列表多出十几条碎片）。
    p.write_text(
        "\n".join(pool_engine_service.escape_prompt_newlines(x)
                  for x in final) + "\n",
        encoding="utf-8")
    # 覆盖模式：引擎若在跑，必须让它按新文件全量重建队列。
    # 否则旧 pending 会继续出片、新提示词又被 diff 追加进去 —— "覆盖"会变成"叠加"。
    # 放在写盘之后：引擎重建时读到的就是新内容。
    if mode == "replace" and engine_running:
        ok, rmsg = await asyncio.to_thread(mgr.queue_reload)
        if not ok:
            raise HTTPException(409, f"覆盖队列失败：{rmsg}")

    # 引擎参数 = 已保存值；批量出片可按本次覆盖出片参数（时长/比例/模型）
    params = _engine_effective_params()
    for k in ("duration", "ratio", "model"):
        v = getattr(body, k, None)
        if v not in (None, ""):
            params[k] = v
    if body.one_per_account:
        # 测试/创作台：N 个视频 = N 个号（一号一片），不再一号吃多条
        params["max_per_acc"] = 1
    sizing = _compute_pool_target(len(final), params)
    params["pool_target"] = sizing["pool_target"]

    started = False
    if engine_running:
        # 已在跑：run_pool 的队列热加载线程会把新行吃进去，不需要重启
        msg = "已入队（引擎运行中，热加载接单）"
    else:
        ok, msg = await asyncio.to_thread(mgr.start, {"prompts": None, "params": params})
        if not ok:
            raise HTTPException(409, f"入队成功但引擎启动失败：{msg}")
        started = True
    return {"ok": True, "count": len(final), "added": len(prompts), "mode": mode,
            "started": started, "message": msg,
            "need": sizing["need"], "k": sizing["k"],
            "pool_target": sizing["pool_target"], "per": sizing["per"]}


@app.post("/api/admin/pool-engine/dispatch")
async def admin_pool_engine_dispatch(body: PoolDispatchBody,
                                     x_admin_key: str | None = Header(default=None)):
    """池级「发起出片」：N 个视频 → N 个号 → 等窗口同刻提交 → 失败换号重来。

    与账号级 test-generate 的分工（用户明确要求后者改为走这条链路）：
      * **不钉死账号**：任务进全局队列，任何可用号都能接；
      * **不立刻出片**：等 710 窗口放开时整池同刻提交（引擎的 probe/burst 机制）；
      * **失败换号**：某号这轮没成功 → note_tried 记下 → 任务回队，
        下一窗 take_next(avoid=...) 优先换别的号（run_pool 内已实现）。

    count 个视频 = count 条任务、每条只用 1 个号（one_per_account），
    所以「生成几个视频就选几个号」严格成立。
    """
    _admin_auth(x_admin_key)
    # ★ whole=True：整段输入作为**一条**任务。「发起出片」的提示词框里粘的
    #   往往是完整分镜剧本（多行、带时间轴），按行/按段拆都会把一部片子拆成
    #   碎片 —— 用户要的是「这几个视频各用几个号」，不是「一行一个视频」。
    #   段内换行由 submit_tasks 转义成 \n 字面量单行存储，读出来再还原。
    lines = _split_prompt_blocks(body.prompt or "", whole=True)
    if not lines:
        raise HTTPException(400, "提示词不能为空")
    count = max(1, int(body.count or 1))
    # 视频数 = 有效提示词条数 × 每条份数；每条任务占 1 个号（一号一片）
    prompts = [ln for ln in lines for _ in range(count)]
    n_tasks = len(prompts)
    batch = uuid.uuid4().hex[:8]
    task_ids = ["poolgen_%s_%03d" % (batch, i + 1) for i in range(n_tasks)]

    p = pool_engine_service.TASKS_FILE
    mgr = poolengine_mgr
    engine_running = mgr.running() or bool(mgr._pid_file_running())
    existing = 0
    if p.is_file():
        existing = sum(1 for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                       if ln.strip() and not ln.strip().startswith("#"))
    # 追加语义：创作台/测试不该清掉用户已有的批量队列。
    # ★ 给这批任务加优先标记（prio=True）：否则它们排在几百条批量任务之后，
    #   要等好几个出片窗口才轮到 —— 用户点完「发起出片」半天没动静，看起来像坏了。
    # ★ 必须走 submit_tasks 成对写 tasks.txt + tasks.meta.jsonl：旧代码只写
    #   tasks.txt，meta 条数对不上时引擎的 load_tasks() 会**拒绝启动**
    #   （实测把队列搞成 1 条 vs 103 条，连点三次全军覆没）。
    r = await asyncio.to_thread(
        mgr.submit_tasks,
        [{"prompt": x, "meta": {}} for x in prompts],
        mode="append", prio=True)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "入队失败")

    params = _engine_effective_params()
    params["max_per_acc"] = 1          # ★ 一号一片：N 个视频用 N 个号
    # ★ 「发起出片」是等窗口语义：引擎必须常驻到窗口放开（几分钟~几十分钟）。
    #   run_seconds 一旦非 0，引擎会在时限到达时主动收尾退出，任务永远等不到
    #   窗口（实测 run_seconds=25 → 25 秒后 [STOP]，队列原样躺着）。
    params["run_seconds"] = 0
    for k, v in (("duration", body.duration), ("ratio", body.ratio), ("model", body.model)):
        if v not in (None, ""):
            params[k] = v
    # 池规模：够本次这批用即可（创作台是即时小批量，不必按整队列放大）
    k_safe = max(1, min(int(params.get("k_max") or 20), 3))
    params["pool_target"] = max(1, n_tasks * k_safe)

    started = False
    if engine_running:
        msg = "已入队（引擎运行中，窗口放开即提交）"
    else:
        ok, msg = await asyncio.to_thread(mgr.start, {"prompts": None, "params": params})
        if not ok:
            raise HTTPException(409, f"任务已入队但引擎启动失败：{msg}")
        started = True

    result = {
        "ok": True, "count": n_tasks, "prompts": len(lines), "per_prompt": count,
        "task_ids": task_ids, "started": started, "message": msg,
        "pool_target": params["pool_target"], "max_per_acc": 1,
        "queue_count": r.get("count") or (existing + n_tasks),
        "mode": "window_burst",       # 明确告知前端：走的是探测窗口+整池提交
    }
    if not body.wait:
        return result

    # 同步等待（测试/脚本用）：轮询队列快照，等这批任务都到终态
    deadline = time.time() + int(body.wait_seconds)
    base = existing
    want_tids = {"t%03d" % (base + i + 1) for i in range(n_tasks)}
    final_rows: list[dict] = []
    while time.time() < deadline:
        await asyncio.sleep(3)
        snap = await asyncio.to_thread(mgr.read_queue_state)
        rows = {r.get("tid"): r for r in (snap.get("rows") or [])}
        final_rows = list(rows.values())
        pending = [r for r in final_rows
                   if r.get("status") in ("pending", "flying")]
        if not pending:
            break
    done = [r for r in final_rows if r.get("tid") in want_tids]
    accts = sorted({str(r.get("account")) for r in done if r.get("account")})
    result.update({
        "waited": True,
        "done": len([r for r in done if r.get("status") in
                     ("done", "done_watermarked", "failed")]),
        "succeeded": sum(1 for r in done if str(r.get("status", "")).startswith("done")),
        "failed": sum(1 for r in done if r.get("status") == "failed"),
        # 用了几个号 = 「N 个视频就选几个号」是否成立的直接证据
        "accounts_used": accts, "accounts_used_count": len(accts),
        "rows": [{"tid": r.get("tid"), "status": r.get("status"),
                  "account": r.get("account")} for r in done],
    })
    return result


# ===== 队列（运行状态 → 队列面板）=====

@app.get("/api/admin/pool-engine/queue")
async def admin_pool_engine_queue(x_admin_key: str | None = Header(default=None)):
    """当前队列（只读）。

    数据源优先级：
      1) 引擎内存快照（out/queue_state.json，且够新鲜）—— 最权威；
      2) 引擎没跑 / 快照过期 → 用**台账**算（ledger.jsonl 里受理/收割/失败事件），
         再叠加 tasks.txt 里还没开工的行。
    为什么需要第 2 条：只按 tasks.txt 反推会把「已受理但还没出片」的任务
    也算成 pending —— 实测面板显示 pending 102，而台账是 97 pending + 1 accepted，
    两个数字对不上，用户会以为队列卡了。
    """
    _admin_auth(x_admin_key)
    snap = await asyncio.to_thread(poolengine_mgr.read_queue_state)
    rows = snap.get("rows") or []
    stale = bool(snap.get("stale"))
    source = "engine_snapshot"
    if stale:
        # 快照过期/不存在 → 用台账合成。合成成功就不该再显示"过期"，
        # 但 source 要如实标成 ledger（用户能看出这不是引擎实时快照）。
        merged = await asyncio.to_thread(_queue_rows_from_ledger)
        if merged:
            rows, stale, source = merged, False, "ledger"
    # 队列面板把「在飞 / 待处理」排前面，其余按 tid 顺序
    order = {"flying": 0, "pending": 1, "accepted": 2, "failed": 3,
             "done_watermarked": 4, "done": 5, "cancelled": 6}
    rows = sorted(rows, key=lambda x: (order.get(x.get("status"), 9), str(x.get("tid"))))
    counts: dict[str, int] = {"pending": 0, "accepted": 0, "done": 0, "failed": 0}
    for r in rows:
        s = str(r.get("status") or "pending")
        key = "done" if s.startswith("done") else s
        if key in counts:
            counts[key] += 1
        else:
            counts[key] = counts.get(key, 0) + 1
    return {"ok": True, "count": len(rows), "counts": counts,
            "rows": rows[:300],
            "generated_at": snap.get("generated_at") or "",
            "stale": stale, "source": source}


def _queue_rows_from_ledger() -> list[dict]:
    """引擎没跑时，用 tasks.txt + ledger 事件合成队列行（尽量与 /status 一致）。"""
    rows: list[dict] = []
    n = 0
    # 1) tasks.txt 里所有有效行（先按行号给 tid；带优先标记的标 prio）
    try:
        for raw in pool_engine_service.TASKS_FILE.read_text(
                encoding="utf-8", errors="replace").splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            prio = s.startswith(pool_engine_service.PRIO_MARK)
            if prio:
                s = s[len(pool_engine_service.PRIO_MARK):].strip()
                if not s:
                    continue
            # 多行提示词还原成真换行：面板上看到的与用户输入一致
            s = pool_engine_service.unescape_prompt_newlines(s)
            n += 1
            rows.append({"tid": "t%03d" % n, "status": "pending", "account": "",
                         "conv": "", "attempts": 0, "mp4": "", "prompt": s,
                         "prio": prio})
    except OSError:
        return []
    by_tid = {r["tid"]: r for r in rows}

    # 2) 用台账事件覆盖状态（只认与这些 tid 对应的）
    def _idx(tid) -> str:
        try:
            return "t%03d" % int(str(tid).lstrip("t"))
        except (TypeError, ValueError):
            return str(tid)

    try:
        tail = pool_engine_service.LEDGER.read_text(
            encoding="utf-8", errors="replace").splitlines()[-8000:]
    except OSError:
        tail = []
    for line in tail:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        tid = _idx(ev.get("task") or "")
        row = by_tid.get(tid)
        if row is None:
            continue
        k = ev.get("kind")
        if k == "accepted":
            row["status"] = "accepted"
            row["account"] = ev.get("account") or row["account"]
            row["conv"] = str(ev.get("conv") or row["conv"])
        elif k == "task_failed":
            row["status"] = "failed"
        elif k == "harvest" and ev.get("ftypisom"):
            row["status"] = "done_watermarked" if not ev.get("unwatermarked") else "done"
            row["account"] = ev.get("account") or row["account"]
    # 3) 磁盘成片是"完成"的最终权威（引擎可能来不及写 harvest 事件）
    try:
        for p in Path(pool_engine_service.MP4DIR).glob("*.mp4"):
            row = by_tid.get(_idx(p.stem.split("_")[0]))
            if row is not None:
                row["status"] = "done_watermarked" if p.stem.endswith("_WM") else "done"
    except OSError:
        pass
    return rows


@app.post("/api/admin/pool-engine/queue/clear")
async def admin_pool_engine_queue_clear(x_admin_key: str | None = Header(default=None)):
    """清空队列：只清尚未开工的 pending，已受理 / 在收割的不打断。"""
    _admin_auth(x_admin_key)
    ok, msg, cleared = await asyncio.to_thread(poolengine_mgr.queue_clear)
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "cleared": cleared, "message": msg}


# ===== 创作台 =====
# 创作台的提交目标是**出片引擎**（run_pool.py 的队列），不是面板的 _run_task 路径：
# 引擎才有「探针嗅窗 → 整池爆发 → 延迟收割」这条已验证的出片链路。
# 参考素材（用户在素材库 @ 引用的图片）经 asset_store 解析成本地路径，
# 随任务写进 tasks.meta.jsonl，最终由 dola_window 上传到上游。


class StudioGenerateBody(BaseModel):
    mode: str = "video"                 # video | image
    channel: str = "dola"               # dola | doubao
    prompt: str = Field(..., min_length=1)
    asset_ids: list[str] = Field(default_factory=list)
    model: str | None = None
    duration: int | None = None
    ratio: str | None = None


@app.get("/api/admin/studio/capabilities")
async def admin_studio_capabilities(x_admin_key: str | None = Header(default=None)):
    """按渠道/模式下发可用模型、时长档、比例、参考图上限（前端据此禁用无效项）。

    dola 与豆包的档位本来就不同（见 _GENSET_* 两套表），创作台必须按渠道给，
    否则用户选了豆包渠道的 30 秒 + seedance-2.0 会被上游拒。
    """
    _admin_auth(x_admin_key)

    def _one(ch: str, mode: str) -> dict:
        """某个渠道在某个模式下的档位。

        ★ 模式差异必须在这里表达，前端才能"按模式给正确选项"：
          · **图片**：模型是 Seedream 四档、**没有时长**（出图没有秒数概念）、
            比例用全集（含 3:2 / 2:3）；
          · **视频**：模型是 Seedance、有时长、比例用视频子集（3:2 系不生效）。
        """
        if mode == "image":
            # 目前只有豆包域有 Seedream 生图通道；dola 域没有图片能力。
            if ch != "doubao":
                return {"models": [], "durations": {}, "ratios": [],
                        "max_reference_images": 0, "modes": ["video"],
                        "image_supported": False}
            return {"models": list(_GENSET_IMAGE_MODELS),
                    "durations": {},          # ★ 图片没有时长
                    "ratios": list(_GENSET_RATIOS),
                    "max_reference_images": int(config.REFERENCE_IMAGE_MAX_COUNT),
                    "modes": ["image"], "image_supported": True}
        if ch == "doubao":
            models, durations = _GENSET_MODELS_DOUBAO, _GENSET_DURATIONS_DOUBAO
        else:
            models, durations = _GENSET_MODELS, _GENSET_DURATIONS
        return {"models": list(models),
                "durations": {k: list(v) for k, v in durations.items()},
                "ratios": list(_GENSET_VIDEO_RATIOS),
                # 与 media.REFERENCE_IMAGE_MAX_COUNT 对齐（上游 dola 最多 10 张）
                "max_reference_images": int(config.REFERENCE_IMAGE_MAX_COUNT),
                "modes": ["image", "video"] if ch == "doubao" else ["video"],
                "image_supported": ch == "doubao"}

    # 结构：{渠道: {模式: 档位}}。保留旧的顶层扁平字段（第一次调用时的视频档位）
    # 做向后兼容，老前端/第三方脚本不会被这次改动打断。
    out: dict = {}
    for _ch in ("dola", "doubao"):
        out[_ch] = {"video": _one(_ch, "video"), "image": _one(_ch, "image")}
    legacy = {ch: dict(out[ch]["video"]) for ch in out}
    return {**legacy, "by_mode": out}


@app.get("/api/admin/studio/engine")
async def admin_studio_engine(x_admin_key: str | None = Header(default=None)):
    """引擎当前状态（创作台顶部提示条用）。"""
    _admin_auth(x_admin_key)
    running = bool(poolengine_mgr.running() or poolengine_mgr._pid_file_running())
    snap = await asyncio.to_thread(poolengine_mgr.read_queue_state)
    counts = snap.get("counts") or {}
    rows = list(snap.get("rows") or [])
    # 队列里出现过的渠道（用于提交前校验「同队列不混渠道」）
    channels = sorted({str(r.get("channel") or "dola") for r in rows})
    # ★ 图片/视频是**两条并行车道**，互不阻塞，所以"队列占用"要按车道分开看：
    #   队列里只有视频时，提交图片**不应该**被"同队列不混渠道"拦住。
    rows_video = [r for r in rows if str(r.get("kind") or "video") != "image"]
    rows_image = [r for r in rows if str(r.get("kind") or "video") == "image"]
    return {"running": running,
            "pending": int(counts.get("pending") or 0),
            "accepted": int(counts.get("accepted") or 0),
            "done": int(counts.get("done") or 0),
            "failed": int(counts.get("failed") or 0),
            "channels": channels,
            "lanes": {
                "video": {"count": len(rows_video),
                          "channels": sorted({str(r.get("channel") or "dola")
                                              for r in rows_video})},
                "image": {"count": len(rows_image),
                          "channels": sorted({str(r.get("channel") or "dola")
                                              for r in rows_image})},
            }}


@app.post("/api/admin/studio/generate")
async def admin_studio_generate(body: StudioGenerateBody,
                                x_admin_key: str | None = Header(default=None)):
    """创作台提交：解析素材 → 入队到出片引擎（带结构化参数）。

    ★ 素材解析在这里完成：asset_ids → 本地绝对路径 → tasks.meta.jsonl。
      引擎启动时把这些路径**预上传到上游**（dola_window.prefetch_assets），
      窗口一开只发最后一个 POST —— 这就是「引用素材真正同步提交给上游」。
    """
    _admin_auth(x_admin_key)
    mode = (body.mode or "video").strip().lower()
    if mode not in ("video", "image"):
        raise HTTPException(422, "mode 只能是 video 或 image")
    channel = (body.channel or "dola").strip().lower()
    if channel not in ("dola", "doubao"):
        raise HTTPException(422, "channel 只能是 dola 或 doubao")
    if len(body.prompt) > config.MAX_PROMPT_CHARS:
        raise HTTPException(422, f"提示词过长（上限 {config.MAX_PROMPT_CHARS} 字符）")

    # ---- 1) 解析引用素材 ----
    asset_paths: list[str] = []
    asset_names: list[str] = []
    if body.asset_ids:
        if len(body.asset_ids) > config.REFERENCE_IMAGE_MAX_COUNT:
            raise HTTPException(
                422, f"参考图最多 {config.REFERENCE_IMAGE_MAX_COUNT} 张"
                     f"（收到 {len(body.asset_ids)} 张）")
        try:
            paths, missing = await asyncio.to_thread(
                asset_store.resolve_paths, body.asset_ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if missing:
            raise HTTPException(
                422, f"有 {len(missing)} 个素材不存在或已被删除：{', '.join(missing[:5])}")
        asset_paths = paths
        for aid in body.asset_ids:
            asset = await asyncio.to_thread(asset_store.get_asset, aid)
            if asset:
                asset_names.append(str(asset.get("name") or aid))

    # ---- 2) 同队列不混渠道 ----
    # dola 与豆包的限流语义相反（dola=全局窗口、豆包=账号级），混在一个队列里
    # 会让两种调度纪律互相破坏（方案文档 §1.4）。
    # ★ 2026-10-16：这条约束**按车道（kind）分别判**。
    #   图片与视频是 run_pool 里两条并行的派发线程（doubao / doubao-img），
    #   互不阻塞；若仍按"整队列一把判"，用户就没法"图片和视频同时生成"——
    #   提交图片会被队列里的视频任务 409 拦住（正是用户截图里的报错）。
    snap = await asyncio.to_thread(poolengine_mgr.read_queue_state)
    lane_rows = [r for r in (snap.get("rows") or [])
                 if str(r.get("kind") or "video") == mode]
    existing_channels = {str(r.get("channel") or "dola") for r in lane_rows}
    existing_channels.discard("")
    if existing_channels and existing_channels != {channel}:
        _lane_label = "图片" if mode == "image" else "视频"
        raise HTTPException(
            409, "队列里已有「%s」渠道的%s任务，同一车道内两种渠道的调度方式不同，"
                 "不能混排。请先清空该车道的任务，或改用 %s 渠道。"
                 "（图片与视频是两条并行车道，互不影响）"
                 % ("、".join(sorted(existing_channels)), _lane_label,
                    "、".join(sorted(existing_channels))))

    # ★ 图片只有豆包域有通道（Seedream）；dola 域没有生图能力，早失败胜过静默不出图。
    if mode == "image" and channel != "doubao":
        raise HTTPException(
            422, "图片生成（Seedream）目前只支持「豆包」渠道，请把渠道切到豆包。")

    # ---- 3) 入队 ----
    meta = {
        "asset_paths": asset_paths,
        "asset_ids": list(body.asset_ids or []),
        "asset_names": asset_names,
        "ratio": body.ratio or "",
        "duration": body.duration,
        "model": body.model or "",
        "channel": channel,
        "kind": mode,
    }
    result = await asyncio.to_thread(
        poolengine_mgr.submit_tasks,
        [{"prompt": body.prompt, "meta": meta}], mode="append")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "入队失败")

    # 素材被引用过：登记关系（供"这条成片用了哪些素材"与「再次生成」）
    if body.asset_ids and result.get("added"):
        for aid in body.asset_ids:
            await asyncio.to_thread(asset_store.link_refs, "", aid)

    # ---- 4) 引擎没在跑就自动启动 ----
    # 创作台界面上写的是"引擎未运行 —— 提交后会自动启动"，这里必须兑现：
    # 否则任务躺在队列里没人管，用户看不到任何报错，只是"不出片"。
    # 复用面板的启动端点逻辑（含嗅探器互斥、STOP 清理、参数合并）。
    started, start_msg = False, ""
    if not result.get("engine_running"):
        try:
            for _flag in (pool_engine_service.CLEAR_FLAG,
                          pool_engine_service.RELOAD_FLAG):
                try:
                    _flag.unlink()
                except (FileNotFoundError, OSError):
                    pass
            ok, msg = await asyncio.to_thread(
                poolengine_mgr.start,
                {"prompts": None, "params": _engine_effective_params()})
            started = bool(ok)
            start_msg = msg
            if not ok:
                # 启动失败不阻断入队结果：任务已在队列里，引擎下次起来会跑
                print(f"[studio] 入队成功但引擎启动失败: {msg}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[studio] 自动启动引擎异常: {exc!r}", flush=True)

    return {"ok": True, "tid": None, "kind": mode, "channel": channel,
            "queued": result.get("added") or 0,
            "queue_count": result.get("count") or 0,
            "engine_running": bool(result.get("engine_running")) or started,
            "engine_started": started,
            "asset_count": len(asset_paths),
            "message": (result.get("message") or "")
                       + (("；" + start_msg) if started else "")}


@app.get("/api/admin/studio/feed")
async def admin_studio_feed(limit: int = 60,
                            x_admin_key: str | None = Header(default=None)):
    """创作台时间线：合并引擎台账（ledger.jsonl）与队列快照。

    数据源与引擎自己的看板（A 的 monitor.py）同源 —— 每受理/爆发/收割都立刻
    落台账，所以"刚提交就能看到"，不必等 summary_pool.json（它只在引擎退出时写）。
    """
    _admin_auth(x_admin_key)
    limit = max(1, min(int(limit or 60), 200))
    engine_state = await asyncio.to_thread(poolengine_mgr.running) or \
        bool(poolengine_mgr._pid_file_running())

    snap = await asyncio.to_thread(poolengine_mgr.read_queue_state)
    queue_rows = {str(r.get("tid")): r for r in (snap.get("rows") or [])}

    # 台账：按 task 聚合最近事件
    latest: dict[str, dict] = {}
    try:
        lines = pool_engine_service.LEDGER.read_text(
            encoding="utf-8", errors="replace").splitlines()[-6000:]
        for raw in lines:
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            tid = str(ev.get("task") or "")
            if not tid or tid == "(bonus)":
                continue
            prev = latest.setdefault(tid, {})
            # ★ 注意：台账事件里的 "kind" 是**事件名**（harvest/accepted/…），
            #   不是产物类型。产物类型由图片车道写在 `kind_` 上，必须分开取，
            #   否则把事件名当成产物类型，前端会把图片当视频渲染。
            ev_kind = ev.get("kind")
            if str(ev.get("kind_") or "") in ("image", "video"):
                prev["kind"] = str(ev["kind_"])
            prev["id"] = tid
            prev.setdefault("prompt", ev.get("prompt") or "")
            prev["channel"] = ev.get("channel") or prev.get("channel") or ""
            prev["ratio"] = ev.get("ratio") or prev.get("ratio") or ""
            prev["duration"] = ev.get("duration") or prev.get("duration") or None
            prev["model"] = ev.get("model") or prev.get("model") or ""
            prev["account"] = ev.get("account") or prev.get("account") or ""
            prev["conv"] = ev.get("conv") or prev.get("conv") or ""
            prev["asset_names"] = ev.get("asset_names") or prev.get("asset_names") or []
            if ev.get("created_ts"):
                prev["created_at"] = float(ev["created_ts"])
            else:
                prev.setdefault("created_at", _now_epoch(ev.get("tstr")))
            if ev_kind == "accepted":
                prev["status"] = "accepted"
            elif ev_kind == "submit_retry":
                prev["status"] = "retrying"
            elif ev_kind == "task_failed":
                prev["status"] = "failed"
                # ★ 失败原因码（如 DOUBAO_NEED_MANUAL_CONFIRM / DOUBAO_POLL_TIMEOUT）：
                #   前端据此给出"该干什么"的提示，而不是只显示一个失败。
                prev["last_outcome"] = str(ev.get("last_outcome")
                                           or ev.get("reason") or "")
                if ev.get("hint"):
                    prev["hint"] = str(ev.get("hint"))[:300]
                if not prev.get("error"):
                    prev["error"] = str(ev.get("last_outcome")
                                        or ev.get("reason") or "")[:200]
            elif ev_kind == "harvest":
                # ★ 完成判定要分渠道：
                #   dola  → 看 ftypisom（引擎会校验落盘的 MP4 是否完整）
                #   豆包  → 车道只写 status="succeeded" + local_path，没有 ftypisom
                #           （豆包域不判水印、也不做 ftyp 校验）
                # 若统一要求 ftypisom，豆包的成功会被误显示成"失败"。
                if ev.get("status") == "succeeded" or ev.get("ftypisom"):
                    prev["status"] = "completed"
                    prev["unwatermarked"] = bool(ev.get("unwatermarked"))
                    lp = str(ev.get("local_path") or "")
                    if lp:
                        # ★ 图片产物发到 /engine-img（与视频的 /engine-mp4 分开）：
                        #   前端图片卡片要直接 <img>，混在 mp4 目录里既认不出也扫不对。
                        _is_img = (str(ev.get("kind_") or "") == "image"
                                   or Path(lp).suffix.lower()
                                   in (".png", ".jpg", ".jpeg", ".webp"))
                        if _is_img:
                            prev["kind"] = "image"
                            urls = [f"{config.PUBLIC_BASE}/engine-img/{Path(p).name}"
                                    for p in (ev.get("local_paths") or [lp])]
                            prev["image_urls"] = urls
                            prev["image_url"] = urls[0] if urls else ""
                            prev["image_count"] = int(ev.get("image_count")
                                                      or len(urls))
                        else:
                            prev["kind"] = "video"
                            prev["video_url"] = (
                                f"{config.PUBLIC_BASE}/engine-mp4/{Path(lp).name}")
                            prev["video_size"] = ev.get("bytes")
                else:
                    prev["status"] = "failed"
                    prev["error"] = str(ev.get("failure_reasons")
                                        or ev.get("exc") or "")[:200]
    except OSError:
        pass

    items = list(latest.values())
    # 队列里还没有台账记录的（刚入队）也补上，保证"提交即可见"
    for tid, row in queue_rows.items():
        if tid in latest:
            continue
        items.append({
            "id": tid, "status": str(row.get("status") or "pending"),
            "prompt": row.get("prompt") or "",
            "channel": row.get("channel") or "dola",
            "kind": str(row.get("kind") or "video"),
            "ratio": row.get("ratio") or "", "duration": row.get("duration"),
            "model": row.get("model") or "",
            "asset_names": row.get("asset_names") or [],
            "account": row.get("account") or "",
        })
    # 用户删除过的记录不再出现在时间线（ledger 追加写、删不掉，只能过滤）
    hidden = await asyncio.to_thread(_studio_hidden_read)
    if hidden:
        items = [x for x in items if str(x.get("id") or "") not in hidden]
    # 磁盘成片是"完成"的最终权威证据（引擎可能来不及写 harvest 事件）
    for item in items:
        if item.get("status") != "completed":
            continue
        is_img = str(item.get("kind") or "") == "image"
        if is_img and not item.get("image_urls"):
            # 图片任务一条是**多张**产物：全扫出来（序号有序），不能只取第一张。
            imgs = sorted(pool_engine_service.IMGDIR.glob(f"{item['id']}_*"),
                          key=lambda p: p.name)
            if imgs:
                urls = [f"{config.PUBLIC_BASE}/engine-img/{p.name}" for p in imgs]
                item["image_urls"] = urls
                item["image_url"] = urls[0]
                item["image_count"] = len(urls)
        elif not is_img and not item.get("video_url"):
            mp4 = pool_engine_service.MP4DIR.glob(f"{item['id']}_*.mp4")
            for cand in sorted(mp4, key=lambda p: p.stat().st_mtime, reverse=True)[:1]:
                item["video_url"] = (f"{config.PUBLIC_BASE}/engine-mp4/"
                                     f"{cand.name}")
    # 失败原因翻成大白话（原始技术报错留在 error_raw 里，排查时还能看）
    for item in items:
        if item.get("status") != "failed":
            continue
        if item.get("error"):
            item["error_raw"] = str(item["error"])[:300]
        why = studio_failure_text(item)
        if why:
            item["error"] = why
    items.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
    counts = snap.get("counts") or {}
    return {"items": items[:limit], "total": len(items),
            "engine": {"running": bool(engine_state),
                       "pending": int(counts.get("pending") or 0),
                       "channel": (sorted({str(r.get("channel") or "dola")
                                           for r in (snap.get("rows") or [])}) or ["dola"])[0]}}


def _now_epoch(tstr: str | None) -> float:
    """把台账里的 'YYYY-MM-DD HH:MM:SS' 转成 epoch 秒；失败返回 0。"""
    if not tstr:
        return 0.0
    try:
        import datetime as _dt
        return _dt.datetime.strptime(str(tstr), "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return 0.0


# ===== 创作台：被用户删除的记录（持久化隐藏）=====
# ledger.jsonl 是引擎**追加写**的，删不掉；而成片 MP4 删了也只是"没片可播"，
# 那条记录下次刷新还会回来。所以删除必须落一份"已隐藏 tid"清单，
# 让 feed 侧过滤掉 —— 否则用户点完删除，刷新一下失败卡片又冒出来。
# 数据量极小（最多几百个 tid），直接一个 JSON 文件，不引入新库。
STUDIO_HIDDEN_FILE = pool_engine_service.OUT / "studio_hidden.json"


# ---- 失败原因的白话化 ----
# 引擎/协议层的报错是给开发看的（`RuntimeError('socks5 connect rejected code=1')`、
# `DOUBAO_NEED_MANUAL_CONFIRM`、`HTTPConnectionPool(...)`…），运营看不懂也不知道
# 该干什么。这里按关键词归类成「一句原因 + 一句建议」，规则顺序即优先级。
# 前端也有一份同款规则（web/index.html 的 STUDIO_ERR_RULES），两边口径保持一致；
# 后端先翻一遍，前端再兜底，保证任何来源的失败记录都能显示中文。
STUDIO_FAILURE_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    # 注意：这里**不能**匹配裸 "timeout" —— `DOUBAO_POLL_TIMEOUT`（轮询超时）
    # 是另一类失败（会话已建、只是等太久），必须落在下面专门那条规则上。
    # 只认 ConnectTimeout / timed out 这种明确的连接层超时。
    (re.compile(r"socks5|proxy|ConnectTimeout|Max retries exceeded|ConnectionError|"
                r"ECONNRESET|timed?\s*out|network", re.I),
     "网络不通：代理线路或上游连接超时/被拒，成片没能取回来。",
     "换个代理节点或稍后重试；连续失败就更换该线路。"),
    (re.compile(r"710022002|访问频繁|限流|rate\s*limit|throttl", re.I),
     "被上游限流：这个账号请求太频繁，暂时不能出片。",
     "等 10 分钟左右再提交，别连续反复重试（越撞越久）。"),
    (re.compile(r"710022004|风控|risk\s*control", re.I),
     "触发上游风控：账号被临时盯上，短时间不能再出片。",
     "停一段时间再用；必要时换账号。"),
    (re.compile(r"登录失效|未登录|401|403|凭证|cookie|session|login", re.I),
     "账号登录状态失效：凭证过期或掉线。",
     "重新登录该账号，或换一个可用账号。"),
    (re.compile(r"额度|quota|credit|次数|免费数量用完|今日次数", re.I),
     "账号额度用完了：今天的免费出片次数已耗尽。",
     "等次日刷新额度，或换有额度的账号。"),
    (re.compile(r"肖像保护|未认证人脸|人脸认证|肖像权", re.I),
     "上游拒绝：参考图涉及肖像保护（未认证人脸不能用来出片）。",
     "换一张不涉及人脸的参考图，或改用纯文字描述。"),
    (re.compile(r"侵权|版权|盗用|未授权使用|知识产权", re.I),
     "上游拒绝：涉嫌侵权/版权问题。",
     "更换原创素材，避免使用他人作品或名人形象。"),
    (re.compile(r"审核|不合规|违规|违反社区|敏感|涉黄|暴力|政治|擦边", re.I),
     "上游拒绝：提示词或参考图内容审核不合规。",
     "修改提示词描述，或更换参考图后再试。"),
    (re.compile(r"参考图|更换参考图|换张参考图|上传的图片", re.I),
     "上游拒绝：参考图不符合要求。",
     "更换参考图后重试。"),
    (re.compile(r"授权|确认生成|manual|NEED_MANUAL_CONFIRM|button_block|10103", re.I),
     "需要人工确认：该账号要在豆包 App 里点一次「确认生成」。",
     "去豆包 App 点一次授权后，再回来重新提交。"),
    (re.compile(r"轮询超时|poll_timeout|POLL_TIMEOUT", re.I),
     "等太久了：上游出片超过等待时间，任务已中断。",
     "重新提交一次；反复超时就换账号。"),
    (re.compile(r"下载失败|download_fail|DOWNLOAD_FAIL|坏片|ftyp", re.I),
     "成片下载失败或文件损坏，没能保存下来。",
     "重新提交一次；不行就换账号出片。"),
    (re.compile(r"没有视频|no_video|NO_VIDEO|no_video_in_window", re.I),
     "这个出片窗口里没拿到成片（上游没出片或窗口已关）。",
     "等下一个出片窗口，或重新提交。"),
    (re.compile(r"窗口关闭|WINDOW_CLOSED|window", re.I),
     "出片窗口已关闭，任务没能提交上去。",
     "稍后重新提交，或等引擎自动排到下一个窗口。"),
    (re.compile(r"没有任务|任务不存在|找不到|not\s*found|404", re.I),
     "任务记录找不到了（可能已被清理）。",
     "重新提交一次。"),
    (re.compile(r"env_hit|环境|attempts|重试次数|超过上限", re.I),
     "重试次数已用完：连续多次都没出片，任务被判定失败。",
     "稍后重新提交；连续失败就换个时间段或换渠道。"),
)


def studio_failure_text(item: dict) -> str:
    """把一条失败记录的技术报错翻成大白话；认不出来时返回空串（前端兜底）。

    引擎已经给过中文 hint 的（如素材授权、轮询超时）直接采用，不再套规则。
    """
    hint = str(item.get("hint") or "").strip()
    if len(hint) > 6 and re.search(r"[一-龥]", hint):
        return hint
    text = " ".join(str(item.get(k) or "").strip()
                    for k in ("error", "last_outcome", "reason", "exc"))
    text = text.strip()
    if not text:
        return ""
    for pattern, why, fix in STUDIO_FAILURE_RULES:
        if pattern.search(text):
            return f"{why}\n建议：{fix}"
    return ""


def _studio_hidden_read() -> set[str]:
    try:
        data = json.loads(STUDIO_HIDDEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    items = data.get("hidden") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return set()
    return {str(t).strip() for t in items if str(t).strip()}


def _studio_hidden_write(tids: set[str]) -> None:
    """原子写（tmp + replace）：写一半崩了也不会留下坏 JSON。"""
    tmp = STUDIO_HIDDEN_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"hidden": sorted(tids)}, ensure_ascii=False,
                              indent=0), encoding="utf-8")
    os.replace(tmp, STUDIO_HIDDEN_FILE)


@app.delete("/api/admin/studio/tasks/{tid}")
async def admin_studio_task_delete(tid: str,
                                   x_admin_key: str | None = Header(default=None)):
    """删除创作台的一条生成记录：删成片文件 + 记入隐藏清单（刷新不再出现）。

    失败记录没有成片文件，但同样要从时间线上消失 —— 靠隐藏清单实现。
    """
    _admin_auth(x_admin_key)
    safe = str(tid).strip()
    if not safe.startswith("t"):
        raise HTTPException(422, "非法任务号")
    # 删成片（引擎 out/mp4/<tid>_*.mp4）与生成图片（out/img/<tid>_*）
    removed = 0
    for cand in list(pool_engine_service.MP4DIR.glob(f"{safe}_*.mp4")) + \
            list(pool_engine_service.IMGDIR.glob(f"{safe}_*")):
        try:
            await asyncio.to_thread(cand.unlink)
            removed += 1
        except OSError:
            continue
    # 记入隐藏清单（ledger 是追加写、删不掉，只能在前端侧过滤）
    def _mark() -> None:
        hidden = _studio_hidden_read()
        hidden.add(safe)
        _studio_hidden_write(hidden)

    try:
        await asyncio.to_thread(_mark)
    except OSError:
        pass
    return {"ok": True, "removed": removed}


@app.post("/api/admin/studio/regenerate")
async def admin_studio_regenerate(body: dict,
                                  x_admin_key: str | None = Header(default=None)):
    """再次生成：按原任务的参数（含引用的素材）重新入队。"""
    _admin_auth(x_admin_key)
    task_id = str((body or {}).get("task_id") or "").strip()
    if not task_id:
        raise HTTPException(422, "task_id 不能为空")

    snap = await asyncio.to_thread(poolengine_mgr.read_queue_state)
    row = next((r for r in (snap.get("rows") or [])
                if str(r.get("tid")) == task_id), None)
    if row is None:
        raise HTTPException(404, "队列里找不到该任务（已收割的任务请从素材库取成片）")

    meta = {
        "asset_paths": list(row.get("asset_paths") or []),
        "asset_ids": list(row.get("asset_ids") or []),
        "asset_names": list(row.get("asset_names") or []),
        "ratio": row.get("ratio") or "",
        "duration": row.get("duration"),
        "model": row.get("model") or "",
        "channel": row.get("channel") or "dola",
        "kind": row.get("kind") or "video",
    }
    # asset_paths 在队列快照里不落盘（只落数量与名字），这里用素材名回查 id 时
    # 需要素材库仍在；拿不到就按无参考图重新入队，并如实告知。
    if meta["asset_names"] and not meta["asset_paths"]:
        names = set(meta["asset_names"])
        found = await asyncio.to_thread(asset_store.list_assets, limit=200)
        by_name = {str(a.get("name")): a for a in (found.get("assets") or [])}
        paths, ids = [], []
        for name in meta["asset_names"]:
            asset = by_name.get(name)
            if asset:
                try:
                    paths.append(str(asset_store.abs_path(asset["rel_path"])))
                except ValueError:
                    continue
                ids.append(asset["id"])
        meta["asset_paths"], meta["asset_ids"] = paths, ids
        meta["asset_names"] = [n for n in meta["asset_names"] if n in names or True]

    result = await asyncio.to_thread(
        poolengine_mgr.submit_tasks,
        [{"prompt": str(row.get("prompt") or ""), "meta": meta}], mode="append")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "入队失败")
    return {"ok": True, "queued": result.get("added") or 0,
            "asset_count": len(meta["asset_paths"]),
            "message": result.get("message") or ""}


# ===== 豆包降级（dola 引擎没出片的兜底）=====
# 引擎是纯 dola 协议的独立进程，不认识豆包库；降级放在面板进程里做，
# 用现成的 doubao_pool / store，互不干扰，还能单独开关。

async def _fb_create_task(task_id: str, *, model: str, prompt: str, ratio: str,
                          duration: int, channel: str) -> None:
    """降级器建任务行（关键字传参，避免误写 account 字段）。"""
    await asyncio.to_thread(store.create, task_id, model, prompt, ratio, duration,
                            channel=channel)


async def _fb_update_task(task_id: str, **fields) -> None:
    await asyncio.to_thread(store.update, task_id, **fields)


_FALLBACK = engine_fallback.DoubaoFallback(
    doubao, _fb_create_task, _fb_update_task,
    log=lambda m: print(m, flush=True))


def _fallback_enabled() -> bool:
    return os.environ.get("DOLA_ENGINE_FALLBACK", "1").strip().lower() not in ("0", "false", "no")


async def _run_doubao_fallback(reason: str = "") -> dict:
    """把引擎未出片的任务交给豆包（串行跑，放线程里避免冻事件循环）。"""
    if not _fallback_enabled():
        return {"skipped": True, "reason": "DOLA_ENGINE_FALLBACK=0 已关闭"}
    if _FALLBACK.running:
        return {"skipped": True, "reason": "降级任务进行中"}
    summary = await asyncio.to_thread(poolengine_mgr.read_summary)
    eff = _engine_effective_params()
    # 豆包侧模型串是**下划线**（与引擎的 seedance_v2.5 同口径，但档位更少）
    fb_model = os.environ.get("DOLA_FALLBACK_MODEL", "seedance_v2.0")
    fb_duration = min(10, int(eff.get("duration") or 10))
    fb_ratio = eff.get("ratio") or "16:9"
    if reason:
        print(f"[fallback] 触发降级（{reason}）", flush=True)
    return await asyncio.to_thread(
        _FALLBACK.run, summary, model=fb_model, ratio=fb_ratio,
        duration=fb_duration, max_items=int(os.environ.get("DOLA_FALLBACK_MAX", "20")))


@app.get("/api/admin/pool-engine/fallback")
async def admin_pool_engine_fallback_state(x_admin_key: str | None = Header(default=None)):
    """降级器状态（是否在跑、上一轮成功/失败）。"""
    _admin_auth(x_admin_key)
    st = _FALLBACK.state()
    st["enabled"] = _fallback_enabled()
    st["model"] = os.environ.get("DOLA_FALLBACK_MODEL", "seedance_v2.0")
    return st


@app.post("/api/admin/pool-engine/fallback")
async def admin_pool_engine_fallback_run(x_admin_key: str | None = Header(default=None)):
    """手动触发一次豆包降级（把引擎 summary 里 failed 的任务跑一遍）。"""
    _admin_auth(x_admin_key)
    res = await _run_doubao_fallback("手动触发")
    if res.get("skipped"):
        return {"ok": True, "skipped": True, "message": res.get("reason") or "无需降级"}
    return {"ok": True, "total": res.get("total"), "succeeded": res.get("ok"),
            "failed": res.get("failed"),
            "message": f"降级完成：成功 {res.get('ok',0)} / 失败 {res.get('failed',0)}"}


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
    """任务中心：可按渠道筛选（channel=dola|doubao，留空=全部）。

    ★ 数据源 = tasks.db **加** 引擎队列（run_pool 的 tasks.txt / queue_state.json）。
      引擎链路（批量出片 / 发起出片 / 创作台提交）不写 tasks.db —— 它有自己的
      队列与台账。只看 tasks.db 的话，新提交的任务要等**成片后**才出现在这里
      （_sync_engine_tasks_to_db 只落终态），期间用户在任务中心看不到任何东西，
      表现为「创建了任务但任务中心不刷新」。所以这里把引擎队列实时合并进来。
    """
    _admin_auth(x_admin_key)
    ch = (channel or "").strip().lower()
    rows = await asyncio.to_thread(
        store.recent_tasks, min(max(limit, 1), 200),
        None, ch if ch in ("dola", "doubao") else None)

    engine_rows = await asyncio.to_thread(_engine_queue_task_rows, ch)
    # 引擎已完成的任务在 tasks.db 里已有 eng_<tid>_<conv>.mp4 记录（带视频链接）——
    # 同一部片不要出现两行。
    # ★ 去重必须**按 conv 精确匹配**，不能按 tid 前缀：
    #   · tid 是队列行号（t001…），队列被清空/覆盖后会**重排复用**。上一轮跑完的
    #     eng_t001_38418355.mp4 还在库里，新一轮排队中的 eng_t001 是另一条任务，
    #     按 tid 前缀判重会把"刚提交、正在排队"的任务**误杀**——任务中心一片空白，
    #     用户以为提交失败（实测：点完「发起出片」列表里什么都没有）。
    #   · 只有"同一个 tid 且同一个 conv 已经成片入库"才算重复。排队/生成中的行
    #     没有 conv，永远不会被误判。
    existing = {str(r.get("id") or "") for r in rows}
    existing_convs = {c for x in existing for c in (_conv_from_eng_id(x) and [_conv_from_eng_id(x)]) or []}
    for er in engine_rows:
        tid = str(er.get("id") or "")
        conv = str(er.get("conversation_id") or "")
        # 终态（已成片）且 conv 能对上库内记录 → 同一条，跳过
        if conv and conv in existing_convs:
            continue
        rows.append(er)
    rows.sort(key=lambda r: float(r.get("created_at") or 0), reverse=True)
    return {"tasks": rows[:max(1, min(limit, 200))], "channel": ch or "all",
            "engine_rows": len(engine_rows)}


# 引擎队列状态 → 任务中心状态（任务中心的 status 语义见 loadTasks 的 sb 表）
_ENGINE_STATUS_MAP = {
    "pending": "queued",
    "flying": "processing",
    "accepted": "processing",
    "done": "completed",
    "failed": "failed",
    "cancelled": "failed",
}


def _conv_from_eng_id(task_id: str) -> str:
    """从库内引擎任务 id `eng_<tid>_<conv>.mp4` 里取出 conv 号。

    同一 conv 号在全库唯一对应一次会话（run_pool 收割时用它做文件名），
    所以它才是"这条任务已经成片入库"的可靠凭证；tid 会随队列重排复用，不能用作判重键。
    取不到（id 形如 eng_xxx.mp4 但中间没有 conv）时返回空串。
    """
    s = str(task_id or "")
    if not s.startswith(ENGINE_TASK_PREFIX):
        return ""
    stem = s[len(ENGINE_TASK_PREFIX):]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    parts = stem.split("_")
    # eng_<tid>_<conv>：tid 形如 t001（1 个 token），conv 是其后第一个纯数字 token
    if len(parts) >= 2 and parts[0].startswith("t") and parts[0][1:].isdigit() \
            and parts[1].isdigit():
        return parts[1]
    return ""


def _engine_queue_task_rows(channel: str = "") -> list[dict]:
    """把引擎队列快照转成任务中心可直接渲染的行（只读，不写库）。

    为什么不写 tasks.db：这些行是引擎的**活队列**，写进去会被看门狗当成
    「排队超时/孤儿任务」重派或判失败，反而搅乱引擎自己的队列调度
    （见 _sync_engine_tasks_to_db 的注释）。所以只在读取时合并展示。

    id 用 `eng_<tid>`（tid = t001 这种）。已完成的任务在 tasks.db 里的 id 是
    `eng_<文件名>`，调用方按 startswith 去重。
    """
    try:
        snap = poolengine_mgr.read_queue_state()
    except Exception as exc:  # noqa: BLE001
        print(f"[tasks] 引擎队列读取失败（已忽略）: {exc}", flush=True)
        return []
    rows_in = snap.get("rows") or []
    now = time.time()
    out: list[dict] = []
    for r in rows_in:
        tid = str(r.get("tid") or "")
        if not tid:
            continue
        row_ch = str(r.get("channel") or "dola")
        if channel and channel != row_ch:
            continue
        status = _ENGINE_STATUS_MAP.get(str(r.get("status") or "pending"), "queued")
        # created_at 必须稳定：前端 loadTasks 用 JSON 指纹做「数据没变就跳过重建」的
        # 短路，这里如果每次都用 time.time()，指纹永远在变 → 每 10 秒重建整表。
        # 引擎的 pending 行没有真实创建时间，取快照生成时间（够细，且同状态内稳定）；
        # 快照没有 generated_at（停机反推）时退化到 tasks.txt 的 mtime。
        created = _now_epoch(snap.get("generated_at"))
        if not created:
            try:
                created = pool_engine_service.TASKS_FILE.stat().st_mtime
            except OSError:
                created = now
        item = {
            "id": ENGINE_TASK_PREFIX + tid,
            "model": r.get("model") or "",
            "prompt": r.get("prompt") or "",
            "ratio": r.get("ratio") or "",
            "duration": r.get("duration") or None,
            "status": status,
            # 队列行的"创建时刻"是**没有真实来源**的：引擎的 pending/flying 行不带
            # 创建时间，只能借快照 generated_at（引擎每 3 秒重写一次 → 值一直在跳），
            # 或退化到 tasks.txt 的 mtime。之前把这个值当 created_at 交给前端，于是
            # 「已等 X 分」显示的是"tasks.txt 上次被写入距今多久"，与排队时长无关，
            # 而且快照一刷新就跳变。这里如实标记为不可信，由前端显示「—」。
            "created_at_trusted": False,
            "account": r.get("account") or "",
            "conversation_id": r.get("conv") or "",
            "channel": row_ch,
            "video_url": "",
            "api_key_name": "",
            "reference_images": "[]",
            "reference_thumbs": "[]",
            "attempted_accounts": "",
            "failure_code": "",
            "error": "",
            "eta_text": "",
            "started_at": None,
            "finished_at": None,
            "last_poll_at": 0,
            "client_concurrency_limit": 0,
            "created_at": created,
            "updated_at": created,
            "source": "engine-queue",   # 前端据此显示「引擎队列」来源标记
            "attempts": r.get("attempts") or 0,
            "asset_count": r.get("asset_count") or 0,
            "prio": bool(r.get("prio")),
        }
        if status == "failed":
            item["error"] = "引擎侧任务失败（详见运行状态日志）"
        out.append(item)
    return out


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


# ===== 查看视频：合并两个来源 =====
# 来源 A：引擎探测出片 out/mp4/（channel=dola，只读聚合）
# 来源 B：面板 / 测试生成 / API 出片（downloads/，走任务库）
# 合并成一张表，前端按 channel / 水印 筛选。

# 引擎成片同步进任务库时用的 id 前缀。作用有两个：
#   1) 让「任务中心」也能看到引擎出的片（引擎本身不碰 tasks.db）；
#   2) 便于「查看视频」去重 —— 同一部片不要既按 out/mp4 又按任务库出现两次。
ENGINE_TASK_PREFIX = "eng_"


def _sync_engine_tasks_to_db() -> int:
    """把 out/mp4 的引擎成片 upsert 进 tasks.db（终态 completed，看门狗不会动它）。

    只写**已成片**的记录：引擎队列里的 pending/flying 是临时态，写进任务库会被
    看门狗当成"孤儿任务"重派，反而把引擎的队列搅乱。所以这里只落终态。
    返回本次新增条数。

    ★ created_at 必须显式落**真实出片时刻**（台账 harvest 的落盘时刻，退化到文件
      mtime）。用默认的 time.time() 只能得到"入库这一刻" —— 一次同步写进来的十几条
      成片 created_at 完全相等（实测 11 条都是 1792167354.157），任务中心「创建时间」
      整列显示同一个 00:15，与真实出片时间（14:26~23:50）完全脱钩，用户看到的就是
      "时间跟视频对不上"。
    """
    rows = _engine_video_rows()
    added = 0
    for v in rows:
        name = str(v.get("id", "")).replace("eng:", "")
        if not name:
            continue
        tid = ENGINE_TASK_PREFIX + name
        started, finished = v.get("started_at"), v.get("finished_at")
        # 出片时刻：台账 harvest 落盘时刻优先，退化到 mp4 的 mtime
        born = float(v.get("created_at") or 0) or float(finished or 0) or time.time()
        try:
            if store.get(tid):
                # 已存在：只补可能后到的字段（例如台账后写的 account/提示词）
                store.update(tid, video_url=v.get("video_url"),
                             account=v.get("account") or None,
                             video_size=v.get("file_size"))
                _heal_engine_timestamps(tid, started, finished)
                _heal_engine_created_at(tid, born)
                continue
            store.create(tid, v.get("model") or "seedance_v2.5",
                         v.get("prompt") or "", v.get("ratio") or "16:9",
                         int(v.get("duration") or 30), account=v.get("account") or None,
                         channel="dola", created_at=born)
            store.update(tid, status="completed", video_url=v.get("video_url"),
                         video_size=v.get("file_size"),
                         conversation_id=v.get("conversation_id") or None,
                         started_at=started,
                         finished_at=finished)
            added += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[engine-sync] {tid} 写入任务库失败: {exc}", flush=True)
    return added


def _heal_engine_created_at(tid: str, born: float) -> None:
    """就地纠正引擎库行的 created_at（老行修复）。

    背景：这批行是 _sync_engine_tasks_to_db 早先写进去的，created_at 全是"同步那一
    刻"（十几条一模一样），任务中心「创建时间」列因此毫无信息量。台账里其实有真实
    落盘时刻，这里在每次同步时幂等地回填，用户不用手工删库重灌。

    判据：created_at 与 finished_at 明显不等（说明是"入库时刻"而不是出片时刻）且
    台账给了可信的 born 时，才改写。同批次同步写进去的行连 created_at 都相等，这里
    按"与 finished 不符"逐个纠正，不做整批特判。
    """
    if not born or born <= 0:
        return
    try:
        row = store.get(tid)
    except Exception:  # noqa: BLE001
        return
    if not row:
        return
    cur = float(row.get("created_at") or 0)
    if abs(cur - float(born)) < 1.0:
        return                      # 已经是对的（或在 1 秒误差内）：幂等返回
    fin = float(row.get("finished_at") or 0)
    # 只在"当前值明显不是出片时刻"时改写：cur 与 finished 不符才动手，
    # 避免把其它链路（面板出片）正常写入的 created_at 覆盖掉。
    if fin and abs(cur - fin) < 1.0:
        return
    try:
        store.update(tid, created_at=float(born))
    except Exception as exc:  # noqa: BLE001
        print(f"[engine-sync] {tid} 创建时刻修复失败: {exc}", flush=True)


def _heal_engine_timestamps(tid: str, started, finished) -> None:
    """就地纠正引擎库行的时刻（老行修复；台账认不出时刻时保持原样）。

    背景：这批行是 _sync_engine_tasks_to_db 早先写进去的，started_at 与 finished_at
    都被填成了文件 mtime（两值相等）→ 面板耗时恒为 0M。台账里其实有真实受理/成片
    时刻，这里在每次同步时幂等地把它们回填，用户不用手工删库重灌。

    只改「明显是错的」行：两值相等（同一 mtime）或为空，且台账给了可信时刻。
    已经是真实耗时的行不动，避免覆盖正常数据。
    """
    if not started or not finished:
        return
    try:
        row = store.get(tid)
    except Exception:  # noqa: BLE001
        return
    if not row:
        return
    cur_s, cur_f = row.get("started_at") or 0, row.get("finished_at") or 0
    same = bool(cur_s) and abs(float(cur_s) - float(cur_f)) < 0.001
    if not (same or not cur_s or not cur_f):
        return
    if abs(float(cur_s or 0) - float(started)) < 0.001 \
            and abs(float(cur_f or 0) - float(finished)) < 0.001:
        return
    try:
        store.update(tid, started_at=started, finished_at=finished)
    except Exception as exc:  # noqa: BLE001
        print(f"[engine-sync] {tid} 时刻修复失败: {exc}", flush=True)


def _engine_video_rows() -> list[dict]:
    """把引擎出片目录 + 台账合成面板能直接渲染的行。

    元数据优先级：ledger 的 harvest 记录（有 account / bytes / 无水印判定 / conv）
    → 退化到「文件名 + 大小 + mtime」。台账缺记录时不造假，如实留空。
    """
    rows: list[dict] = []
    mp4 = Path(pool_engine_service.MP4DIR)
    if not mp4.is_dir():
        return rows
    # 台账：文件名 → 权威元数据（含提示词与**真实时刻**）。
    # 提示词优先取台账（收割/受理时随任务一起写的），只有老台账没这个字段时，
    # 才回退到「按 tasks.txt 行号反查」——因为队列清空/覆盖后 tid 会重排
    # （t001 → t105 …），行号映射会张冠李戴。
    #
    # ★ 耗时也必须取台账：harvest 只写 local_path（文件名），而旧实现让
    #   _sync_engine_tasks_to_db 拿文件 mtime **同时**当 started_at 与 finished_at，
    #   两个字段永远相等 → 任务中心「耗时徽标」恒为 0M（用户 2026-10-17 实测反馈）。
    #   真实时刻在台账里成对存在：accepted（受理，带 conv + tstr）→ harvest（成片落盘）。
    #   配对键用 conv，**不能**用 tid：tid（t001）每轮重排复用，同一 tid 在台账里有多代记录。
    meta: dict[str, dict] = {}
    accepted_by_conv: dict[str, float] = {}
    try:
        for line in pool_engine_service.LEDGER.read_text(
                encoding="utf-8", errors="replace").splitlines()[-8000:]:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            kind = rec.get("kind")
            if kind == "accepted":
                conv = str(rec.get("conv") or "")
                if conv and conv not in accepted_by_conv:
                    accepted_by_conv[conv] = _now_epoch(rec.get("tstr"))
                continue
            if kind != "harvest":
                continue
            lp = rec.get("local_path") or ""
            if not lp:
                continue
            conv = str(rec.get("conv") or "")
            meta[Path(lp).name] = {
                "account": rec.get("account") or "",
                "conv": conv,
                "tid": rec.get("task") or "",
                "prompt": rec.get("prompt") or "",
                "unwatermarked": rec.get("unwatermarked"),
                # 渠道从台账取：豆包域成片也走这个目录，别一律标 dola
                "channel": str(rec.get("channel") or "dola"),
                # 出片参数也从台账取，供「查看视频」按真实值展示（见下方不取 params 的原因）
                "duration": rec.get("duration"),
                "ratio": rec.get("ratio") or "",
                "model": rec.get("model") or "",
                # 成片落盘时刻（harvest 的 tstr）
                "finished_at": _now_epoch(rec.get("tstr")),
            }
    except OSError:
        pass
    # 受理时刻按 conv 回填（harvest 记录本身不重复带 accepted 的 tstr）
    for m in meta.values():
        m["accepted_at"] = accepted_by_conv.get(m.get("conv") or "", 0.0)
    # ★ 这里**故意没有**「按 tasks.txt 行号反查提示词」的兜底映射。
    #   历史实现是 prompt_by_tid["t%03d" % n] = tasks.txt 第 n 行，用来给"老台账
    #   prompt 为空"的成片补一个提示词。但 tid 是**每轮重排复用**的（t001→t105…），
    #   队列一旦被清空/覆盖/换批，行号与 tid 就彻底脱钩 —— 实测同一部片在任务中心
    #   与视频库显示两个不同的提示词（t002_38418427 一边空、一边"汉服美女在赛博朋克
    #   的街道跑酷"，而它真实是"无人机缓缓飞过东京雨夜街道…"）。
    #   本文件写 _engine_queue_task_rows 时已明确「tid 会随队列重排复用，不能用作判重键」，
    #   同一理由在这里同样成立。台账认领不出提示词时**留空**，绝不猜。

    params = _engine_effective_params()   # noqa: F841  （保留：调用方/后续扩展可能用）
    for f in sorted(mp4.glob("*.mp4"),
                    key=lambda x: x.stat().st_mtime, reverse=True)[:500]:
        try:
            stt = f.stat()
        except OSError:
            continue
        m = meta.get(f.name) or {}
        tid = m.get("tid") or f.stem.split("_")[0]
        watermarked = f.stem.endswith("_WM")
        # 提示词：只认台账 harvest 记录（权威）。台账没有就留空，让前端显示 —。
        prompt = m.get("prompt") or ""
        # 渠道：台账认领的用台账；未认领（老成片）按 dola —— 面板只展示，不伪造
        channel = m.get("channel") or "dola"
        # 真实时刻三元组（台账有就用台账，缺则退回 mtime，绝不两个字段填同一个值）：
        #   accepted_at  受理 → started_at（起算点）
        #   finished_at  harvest 落盘 → 耗时徽标的终点
        started_at = m.get("accepted_at") or 0.0
        finished_at = m.get("finished_at") or 0.0
        if not finished_at:
            finished_at = stt.st_mtime
        if not started_at or started_at > finished_at:
            # 台账认不出受理时刻（bonus 片 / 老台账）→ 只给终点，起点留空由前端显示 —
            started_at = 0.0
        rows.append({
            "id": "eng:" + f.name,
            "source": "engine",
            # 列表排序仍用落盘时刻：没有台账时刻的老片也要有稳定序
            "created_at": finished_at,
            "started_at": started_at or None,
            "finished_at": finished_at,
            "channel": channel,
            # 时长/比例/模型**只认台账 harvest 记录**，不取全局 params：
            # params 是"当前引擎参数"（可随设置随时改），拿它给历史成片标注时长，
            # 会让同一批 30 秒片在某次改设置后集体变成新值（实测队列行显示 10 秒、
            # 库里行显示 30 秒）。台账没记就留空。
            "duration": m.get("duration") or None,
            "ratio": m.get("ratio") or None,
            "model": m.get("model") or None,
            "file_size": stt.st_size,
            "account": m.get("account") or "",
            "conversation_id": m.get("conv") or "",
            "prompt": prompt,
            "tid": tid,
            "watermarked": watermarked,
            "file_exists": True,
            "video_url": f"{config.PUBLIC_BASE}/engine-mp4/{f.name}",
        })
    return rows


@app.get("/api/admin/videos/all")
async def admin_videos_all(limit: int = 300,
                           x_admin_key: str | None = Header(default=None)):
    """查看视频：引擎出片（out/mp4）+ 面板出片（downloads）合并列表。

    筛选交给前端（数据量小、交互即时）；这里只负责把两个来源规范化成同一形状。
    """
    _admin_auth(x_admin_key)
    limit = min(max(limit, 1), 1000)

    # 先把引擎成片同步进任务库（幂等 upsert），这样「任务中心」也能看到引擎出的片。
    # 放在读列表之前：本次请求就能看到同步结果；失败不影响列表（只是任务中心少几条）。
    try:
        synced = await asyncio.to_thread(_sync_engine_tasks_to_db)
        if synced:
            print(f"[engine-sync] 同步 {synced} 条引擎成片进任务库", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[engine-sync] 同步失败（已忽略）: {exc}", flush=True)

    # ---- 面板来源：已完成任务 ----
    panel_rows: list[dict] = []
    try:
        rows = await asyncio.to_thread(store.recent_tasks, limit, None, None)
        for r in rows:
            if r.get("status") != "completed":
                continue
            item = dict(r)
            # 引擎成片的库记录与 out/mp4 是同一部片 → 只保留 out/mp4 那一条，
            # 否则「查看视频」里同一部片会出现两次（一次本地面板、一次引擎）。
            if str(item.get("id", "")).startswith(ENGINE_TASK_PREFIX):
                continue
            item["source"] = "panel"
            item["channel"] = item.get("channel") or "dola"
            if item.get("video_url"):
                local = Path(config.DOWNLOAD_DIR) / Path(item["video_url"]).name
                exists = local.exists()
                item["file_exists"] = exists
                if exists:
                    try:
                        item["file_size"] = os.path.getsize(local)
                    except OSError:
                        pass
            else:
                item["file_exists"] = False
            # 引擎不产水印标记，面板来源一律按「无水印」展示（面板链路本就取无水印源）
            item["watermarked"] = False
            panel_rows.append(item)
    except Exception as exc:  # noqa: BLE001
        print(f"[videos/all] 面板来源读取失败: {exc}", flush=True)

    engine_rows = await asyncio.to_thread(_engine_video_rows)
    out = engine_rows + panel_rows
    out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    out = out[:limit]
    return {
        "videos": out,
        "total": len(out),
        "engine_count": len(engine_rows),
        "panel_count": len(panel_rows),
    }


@app.delete("/api/admin/videos/engine/{name}")
async def admin_video_engine_delete(name: str,
                                    x_admin_key: str | None = Header(default=None)):
    """删除引擎成片（out/mp4/<name>）。只删文件，不碰任务库（引擎本就不写库）。"""
    _admin_auth(x_admin_key)
    safe = Path(name).name                      # 防目录穿越
    if not safe.endswith(".mp4") or safe != name:
        raise HTTPException(400, "非法文件名")
    target = Path(pool_engine_service.MP4DIR) / safe
    if not target.is_file():
        raise HTTPException(404, "视频文件不存在")
    try:
        await asyncio.to_thread(target.unlink)
    except OSError as exc:
        raise HTTPException(500, f"删除失败: {exc}") from exc
    return {"ok": True, "deleted": safe}


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


# ===== 素材库（创作台）=====
# 用户素材的长期资产库：上传 / 检索 / 打标 / 软删 / 产物入库 / 被引用查询。
# 数据层在 asset_store.py（独立 assets.db），本段只做 HTTP 适配与文件校验。
#
# ★ 为什么素材不放进 refs/：refs/ 是**临时物化产物**，任务终态会被
#   cleanup_local_references 删、服务启动会被 sweep_stale_references 清。
#   素材放 assets/ 且由 media.is_asset_reference 单独识别 → 永不被自动删。

_ASSET_KIND_BY_MIME = (
    ("image/", asset_store.KIND_IMAGE),
    ("video/", asset_store.KIND_VIDEO),
    ("audio/", asset_store.KIND_AUDIO),
)


def _asset_kind_of(filename: str, content_type: str = "") -> str:
    """按扩展名（优先）与 MIME 推断素材类别。"""
    ext = Path(str(filename or "")).suffix.lower()
    if ext in asset_store.IMAGE_EXTS:
        return asset_store.KIND_IMAGE
    if ext in asset_store.VIDEO_EXTS:
        return asset_store.KIND_VIDEO
    if ext in asset_store.AUDIO_EXTS:
        return asset_store.KIND_AUDIO
    for prefix, kind in _ASSET_KIND_BY_MIME:
        if str(content_type or "").lower().startswith(prefix):
            return kind
    return asset_store.KIND_IMAGE


def _asset_public(asset: dict) -> dict:
    """库行 → 前端可直接渲染的形状（URL 用 PUBLIC_BASE 拼，便于反代）。"""
    if not asset:
        return {}
    out = dict(asset)
    rel = str(out.get("rel_path") or "")
    if rel:
        out["url"] = f"{config.PUBLIC_BASE}/assets/{rel}"
    thumb = out.get("thumb")
    if thumb:
        out["thumb_url"] = f"{config.PUBLIC_BASE}/asset-thumbs/{Path(str(thumb)).name}"
    out["size_text"] = _fmt_bytes(out.get("bytes"))
    return out


def _fmt_bytes(value) -> str:
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


@app.get("/api/admin/assets")
async def admin_assets(kind: str = "", q: str = "", tag: str = "",
                       group: str = "", source: str = "", channel: str = "",
                       include_deleted: bool = False,
                       limit: int = 100, offset: int = 0,
                       x_admin_key: str | None = Header(default=None)):
    """素材列表（网格墙）。过滤在 SQL 层做。"""
    _admin_auth(x_admin_key)
    k = (kind or "").strip().lower()
    data = await asyncio.to_thread(
        asset_store.list_assets,
        kind=k if k in asset_store.KINDS else None,
        q=(q or "").strip() or None,
        tag=(tag or "").strip() or None,
        group_name=(group or "").strip() or None,
        source=(source or "").strip() or None,
        channel=(channel or "").strip() or None,
        include_deleted=bool(include_deleted),
        limit=limit, offset=offset,
    )
    data["assets"] = [_asset_public(a) for a in data.get("assets", [])]
    data["stats"] = await asyncio.to_thread(asset_store.stats)
    return data


@app.get("/api/admin/assets/stats")
async def admin_assets_stats(x_admin_key: str | None = Header(default=None)):
    """素材库概览（KPI 卡片）。"""
    _admin_auth(x_admin_key)
    return await asyncio.to_thread(asset_store.stats)


@app.get("/api/admin/assets/groups")
async def admin_assets_groups(x_admin_key: str | None = Header(default=None)):
    """已用分组与标签（供筛选下拉）。"""
    _admin_auth(x_admin_key)

    def _collect() -> dict:
        data = asset_store.list_assets(limit=500)
        groups, tags = set(), set()
        for item in data.get("assets", []):
            if item.get("group_name"):
                groups.add(item["group_name"])
            for t in item.get("tags") or []:
                tags.add(str(t))
        return {"groups": sorted(groups), "tags": sorted(tags)}

    return await asyncio.to_thread(_collect)


@app.post("/api/admin/assets/upload")
async def admin_assets_upload(request: Request,
                              x_admin_key: str | None = Header(default=None)):
    """上传素材（multipart，多文件）。

    校验对齐既有口径：扩展名白名单（server._save_uploaded_image 同款）、
    单文件 ≤ ASSET_UPLOAD_MAX_BYTES、单次 ≤ ASSET_UPLOAD_MAX_COUNT、总量配额。
    """
    _admin_auth(x_admin_key)
    form = await request.form()
    items = []
    for key in ("file", "files", "image", "images", "file[]", "files[]"):
        for item in form.getlist(key):
            if hasattr(item, "read"):
                items.append(item)
    if not items:
        raise HTTPException(400, "没有收到文件（字段名用 file / files）")
    if len(items) > config.ASSET_UPLOAD_MAX_COUNT:
        raise HTTPException(
            422, f"单次最多上传 {config.ASSET_UPLOAD_MAX_COUNT} 个文件"
            f"（收到 {len(items)}）")

    tags_raw = str(form.get("tags") or "").strip()
    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    group_name = str(form.get("group") or "").strip()

    saved, failed = [], []
    for item in items:
        filename = getattr(item, "filename", "") or ""
        try:
            data = await item.read()
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": filename, "error": f"读取失败: {exc}"})
            continue
        if not data:
            failed.append({"name": filename, "error": "空文件"})
            continue
        if len(data) > config.ASSET_UPLOAD_MAX_BYTES:
            failed.append({
                "name": filename,
                "error": "超过单文件上限 %.0f MB"
                         % (config.ASSET_UPLOAD_MAX_BYTES / 1048576)})
            continue
        kind = _asset_kind_of(filename, getattr(item, "content_type", ""))
        if kind == asset_store.KIND_IMAGE:
            # 图片额外做真实性校验（与 _save_uploaded_image 一致）
            try:
                from PIL import Image
                from io import BytesIO
                with Image.open(BytesIO(data)) as im:
                    im.verify()
            except Exception:
                failed.append({"name": filename, "error": "不是有效图片"})
                continue
        try:
            asset = await asyncio.to_thread(
                asset_store.ingest_bytes, data,
                filename=filename, kind=kind, name=filename,
                tags=tags, group_name=group_name)
            saved.append(_asset_public(asset))
        except ValueError as exc:
            failed.append({"name": filename, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": filename, "error": f"入库失败: {exc}"})
    return {"ok": bool(saved), "saved": saved, "failed": failed,
            "saved_count": len(saved), "failed_count": len(failed),
            "stats": await asyncio.to_thread(asset_store.stats)}


@app.get("/api/admin/assets/{asset_id}")
async def admin_asset_detail(asset_id: str,
                             x_admin_key: str | None = Header(default=None)):
    """素材详情：元数据 + 被哪些任务引用（"这条成片用了哪些素材"反向查询）。"""
    _admin_auth(x_admin_key)
    asset = await asyncio.to_thread(asset_store.get_asset, asset_id)
    if not asset:
        raise HTTPException(404, "素材不存在")
    refs = await asyncio.to_thread(asset_store.refs_for_asset, asset_id)
    out = _asset_public(asset)
    out["refs"] = refs
    try:
        out["file_exists"] = asset_store.abs_path(asset["rel_path"]).is_file()
    except ValueError:
        out["file_exists"] = False
    return out


class AssetPatchBody(BaseModel):
    name: str | None = None
    tags: list[str] | None = None
    group: str | None = None
    note: str | None = None


@app.patch("/api/admin/assets/{asset_id}")
async def admin_asset_patch(asset_id: str, body: AssetPatchBody,
                            x_admin_key: str | None = Header(default=None)):
    """改名 / 打标签 / 分组 / 备注。"""
    _admin_auth(x_admin_key)
    if not await asyncio.to_thread(asset_store.get_asset, asset_id):
        raise HTTPException(404, "素材不存在")
    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.tags is not None:
        fields["tags"] = [str(t).strip() for t in body.tags if str(t).strip()]
    if body.group is not None:
        fields["group_name"] = body.group.strip()
    if body.note is not None:
        fields["note"] = body.note
    asset = await asyncio.to_thread(asset_store.update_asset, asset_id, **fields)
    return {"ok": True, "asset": _asset_public(asset)}


@app.delete("/api/admin/assets/{asset_id}")
async def admin_asset_delete(asset_id: str, purge: bool = False,
                             x_admin_key: str | None = Header(default=None)):
    """删除素材。默认**软删**（文件保留，历史任务仍能显示"素材已删除"）。

    purge=1 时连文件一起删；若仍被任务引用则拒绝（避免抽走历史「再次生成」
    所依赖的素材）。
    """
    _admin_auth(x_admin_key)
    result = await asyncio.to_thread(asset_store.delete_asset, asset_id, purge=purge)
    if not result.get("ok"):
        if result.get("reason") == "not_found":
            raise HTTPException(404, "素材不存在")
        if result.get("reason") == "referenced":
            raise HTTPException(
                409, "该素材已被 %d 个任务引用，不能硬删除（可先软删除）"
                     % len(result.get("refs") or []))
        raise HTTPException(400, "删除失败")
    return result


class AssetBulkBody(BaseModel):
    ids: list[str] = Field(default_factory=list)
    tags: list[str] | None = None
    add_tags: list[str] | None = None
    group: str | None = None
    delete: bool = False
    purge: bool = False


@app.post("/api/admin/assets/bulk")
async def admin_assets_bulk(body: AssetBulkBody,
                            x_admin_key: str | None = Header(default=None)):
    """批量：追加标签 / 设置分组 / 批量删除。"""
    _admin_auth(x_admin_key)
    ids = [str(i).strip() for i in (body.ids or []) if str(i).strip()]
    if not ids:
        raise HTTPException(422, "ids 不能为空")

    if body.delete:
        purged, soft, blocked = 0, 0, []
        for aid in ids:
            res = await asyncio.to_thread(asset_store.delete_asset, aid,
                                          purge=bool(body.purge))
            if res.get("ok"):
                purged += 1 if body.purge else 0
                soft += 0 if body.purge else 1
            else:
                blocked.append(aid)
        return {"ok": True, "deleted": soft + purged, "purged": purged,
                "blocked": blocked,
                "stats": await asyncio.to_thread(asset_store.stats)}

    fields: dict[str, Any] = {}
    if body.add_tags:
        # 追加语义：逐个读现有标签再合并（素材量级下开销可忽略）
        added = 0
        for aid in ids:
            asset = await asyncio.to_thread(asset_store.get_asset, aid)
            if not asset:
                continue
            merged = list(dict.fromkeys(
                [str(t) for t in (asset.get("tags") or [])]
                + [str(t).strip() for t in body.add_tags if str(t).strip()]))
            await asyncio.to_thread(asset_store.update_asset, aid, tags=merged)
            added += 1
        return {"ok": True, "updated": added, "mode": "add_tags"}
    if body.tags is not None:
        fields["tags"] = [str(t).strip() for t in body.tags if str(t).strip()]
    if body.group is not None:
        fields["group_name"] = body.group.strip()
    if not fields:
        raise HTTPException(422, "没有要更新的字段")
    n = await asyncio.to_thread(asset_store.bulk_update, ids, **fields)
    return {"ok": True, "updated": n}


class AssetImportGeneratedBody(BaseModel):
    """把引擎/面板的成片登记为素材（创作台产物自动回流）。"""

    task_id: str
    path: str | None = None          # 不传则按 task_id 从两个产物目录里找
    channel: str = ""
    prompt: str = ""
    name: str | None = None
    tags: list[str] = Field(default_factory=list)


@app.post("/api/admin/assets/import-generated")
async def admin_assets_import_generated(body: AssetImportGeneratedBody,
                                        x_admin_key: str | None = Header(default=None)):
    """产物入库：把成片复制进 assets/out/ 换取长期保存。

    ★ 为什么要复制而不是索引：downloads/ 有保留期清理（_maintenance，默认 14 天）
      与体积上限（_prune_downloads），引擎 out/mp4/ 也会被清理。
      素材库的语义是长期资产，所以必须另存一份。
    """
    _admin_auth(x_admin_key)
    task_id = (body.task_id or "").strip()
    if not task_id:
        raise HTTPException(422, "task_id 不能为空")

    src = None
    if body.path:
        candidate = Path(body.path)
        # 只允许从既有的两个产物目录里取，防任意文件入库
        allowed_roots = [Path(config.DOWNLOAD_DIR).resolve(),
                         Path(pool_engine_service.MP4DIR).resolve()]
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if not any(_under(resolved, root) for root in allowed_roots):
            raise HTTPException(403, "path 只允许指向出片产物目录")
        if resolved.is_file():
            src = resolved
    if src is None:
        src = _find_generated_file(task_id)
    if src is None:
        raise HTTPException(404, f"找不到 {task_id} 的成片文件")

    # 已入库过就不重复复制（同一任务重复点"入库"是常态）
    existing = await asyncio.to_thread(_find_asset_by_task, task_id)
    if existing:
        return {"ok": True, "deduped": True, "asset": _asset_public(existing)}

    try:
        asset = await asyncio.to_thread(
            asset_store.ingest_file, src,
            source=asset_store.SOURCE_GENERATED,
            kind=asset_store.KIND_VIDEO,
            name=body.name or src.name,
            prompt=body.prompt or "",
            channel=body.channel or "",
            tags=body.tags or [],
            origin_task_id=task_id,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "deduped": False, "asset": _asset_public(asset)}


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _find_generated_file(task_id: str) -> Path | None:
    """在面板 downloads/ 与引擎 out/mp4/ 里按 task_id 找成片。

    引擎成片命名是 <tid>_<conv8>.mp4（可能带 _WM 后缀），面板任务则是
    video_url 里的文件名 —— 两种都按前缀匹配。
    """
    safe = Path(str(task_id)).name
    candidates: list[Path] = []
    for root in (Path(config.DOWNLOAD_DIR), Path(pool_engine_service.MP4DIR)):
        if not root.is_dir():
            continue
        candidates.extend(sorted(root.glob(f"{safe}*.mp4"),
                                 key=lambda p: p.stat().st_mtime, reverse=True))
    return candidates[0] if candidates else None


def _find_asset_by_task(task_id: str) -> dict | None:
    """按 origin_task_id 找已入库的产物素材（用于"重复入库"去重）。"""
    conn = asset_store._connect()
    try:
        row = conn.execute(
            "SELECT * FROM assets WHERE origin_task_id=? AND deleted_at IS NULL"
            " ORDER BY created_at DESC LIMIT 1", (str(task_id),)).fetchone()
        return asset_store._row(row)
    finally:
        conn.close()


# 面板单文件前端（放最后，保证上面的路由优先）
app.mount("/", StaticFiles(directory="web", html=True), name="web")
