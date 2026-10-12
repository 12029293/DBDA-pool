
"""Web job runtime and shared helpers."""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from outlook_auto_register.service.account import account_store, enable_imap, graph_mail, mail_reader
from outlook_auto_register.service.account.account_persist import merge_account_row
from outlook_auto_register.service.account.keepalive_service import keepalive_one
from outlook_auto_register.service.registration import post_register_service as post_register
from outlook_auto_register.service.registration import register_service as reg_module
from outlook_auto_register.service.registration.register_service import register_one, save_account
from outlook_auto_register.service.registration.batch_service import register_batch_iter
from outlook_auto_register.service.resource.proxy import proxy_pool
from outlook_auto_register.service.resource.recovery import cf_domain_mail, external_recovery_pool as ext_recovery_pool
from outlook_auto_register.service.web import rescue_adapter
from outlook_auto_register.config import constants as reg_constants
from outlook_auto_register.dao import outlook_dao as app_db
from outlook_auto_register.model.entity.register_models import RegisterResult
from outlook_auto_register.model.dto.web_requests import RegisterRequest

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parents[2]
ACCOUNTS_DIR = PROJECT_DIR / "accounts"
STATIC_DIR = PROJECT_DIR / "frontend" / "static"
META_FILE = ACCOUNTS_DIR / "webapp_meta.json"
JOBS_FILE = ACCOUNTS_DIR / "webapp_jobs.json"
COMBO_FILE = ACCOUNTS_DIR / "accounts.txt"
DUAL_FILE = ACCOUNTS_DIR / "accounts_dual.txt"
RECOVERY_FILE = ACCOUNTS_DIR / "accounts_recovery.txt"

MAIL_CLIENT_ID = os.environ.get("MAIL_CLIENT_ID", "9e5f94bc-e8a4-4e73-b8be-63364c29d753")
_scope_map = getattr(reg_constants, "MAIL_SCOPE_BY_MODE", None) or getattr(reg_constants, "_MAIL_SCOPE_BY_MODE", {})
_ENGINE_MODES = list(_scope_map.keys()) or ["graph", "outlook_rest", "imap"]
DEFAULT_TOKEN_MODE = getattr(reg_constants, "MAIL_TOKEN_MODE", "graph")
DUAL_READY = "dual" in _scope_map
TOKEN_MODES = _ENGINE_MODES + (["dual"] if "dual" not in _ENGINE_MODES else [])
PRODUCT_MODES = [
    {"id": "graph", "label": "Graph 四段式", "export": "graph", "hint": ""},
    {"id": "graph_recovery", "label": "Graph 六段式（推荐）", "export": "recovery", "hint": ""},
]
EXECUTION_ROUTES = [
    {"id": "protocol", "label": "纯协议（当前可用）", "ready": True, "description": "Fluent Web API + PX solver，不依赖浏览器窗口。"},
    {"id": "roxy", "label": "Roxy 指纹浏览器", "ready": False, "description": "共享注册参数/代理/收码/token 管线；浏览器注册 adapter 尚未接入。"},
    {"id": "bitbrowser", "label": "比特浏览器", "ready": False, "description": "共享注册参数/代理/收码/token 管线；BitBrowser adapter 尚未接入。"},
]
EXPORT_FORMATS = ["graph", "recovery", "dual"]
BATCH_READY = True
KEEPALIVE_READY = True
RESCUE_READY = rescue_adapter.RESCUE_READY

_save_lock = threading.Lock()
_meta_lock = threading.Lock()

def _proxy_url(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        from outlook_auto_register.service.resource.proxy.proxy_utils import parse_proxy

        cfg = parse_proxy(raw)
        return cfg.url if cfg else ""
    except Exception:  # noqa: BLE001
        return ""


_SETTINGS_KEYS = (
    "CAPTCHA_RUN_API_KEY",
    "EZCAPTCHA_API_KEY",
    "CAPSOLVER_API_KEY",
    "OFFCAPTCHA_API_KEY",
    "OFFCAPTCHA_SOFT_ID",
    "DEFAULT_CAPTCHA_PROVIDER",
)

CAPTCHA_PROVIDER_CATALOG: list[dict[str, str]] = [
    {
        "id": "offcaptcha",
        "label": "OffCaptcha",
        "key_setting": "OFFCAPTCHA_API_KEY",
        "hint": "PX invisible + press（推荐）",
    },
    {
        "id": "captcha_run",
        "label": "captcha.run",
        "key_setting": "CAPTCHA_RUN_API_KEY",
        "hint": "silent → press 单 task",
    },
    {
        "id": "ezcaptcha",
        "label": "EzCaptcha",
        "key_setting": "EZCAPTCHA_API_KEY",
        "hint": "PerimeterX / PxInvisible",
    },
    {
        "id": "capsolver",
        "label": "CapSolver",
        "key_setting": "CAPSOLVER_API_KEY",
        "hint": "AntiPerimeterX",
    },
    {
        "id": "local",
        "label": "本地 SwiftShader",
        "key_setting": "",
        "hint": "无第三方 Key，需本机浏览器环境",
    },
]


def _captcha_provider_meta(provider_id: str) -> Optional[dict[str, str]]:
    pid = (provider_id or "").strip().lower()
    for row in CAPTCHA_PROVIDER_CATALOG:
        if row["id"] == pid:
            return row
    return None


def _captcha_provider_configured(provider_id: str) -> bool:
    meta = _captcha_provider_meta(provider_id)
    if not meta:
        return False
    if not meta.get("key_setting"):
        return True
    key = (os.environ.get(meta["key_setting"]) or app_db.get_setting(meta["key_setting"]) or "").strip()
    return bool(key)


def _resolve_captcha_provider(p: dict[str, Any]) -> str:
    raw = p.get("captcha_provider")
    if raw:
        return str(raw).strip().lower()
    return (
        app_db.get_setting("DEFAULT_CAPTCHA_PROVIDER")
        or "offcaptcha"
    ).strip().lower() or "offcaptcha"


def _parse_proxy_selection(p: dict[str, Any]) -> tuple[bool, Optional[str]]:
    """返回 (use_pool, provider_filter)。"""
    sel = str(p.get("proxy_selection") or "").strip()
    if not sel:
        if p.get("use_proxy_pool"):
            return True, (p.get("proxy_provider") or None)
        proxy = (p.get("proxy") or "").strip()
        return (False, None) if proxy else (True, None)
    if sel == "auto":
        return True, None
    if sel.startswith("provider:"):
        prov = sel.split(":", 1)[1].strip()
        return True, prov or None
    return False, None


def _build_register_proxy_plan(p: dict[str, Any], count: int) -> tuple[list[str], dict[str, Any]]:
    """注册任务：代理池（SQLite）按选择分配；兼容旧版 use_proxy_pool + 文本框。"""
    use_pool, provider_filter = _parse_proxy_selection(p)
    if use_pool:
        plan, meta = proxy_pool.plan_for_batch(count, provider=provider_filter)
        if len(plan) < count:
            fallback = (p.get("proxy") or "").strip()
            if fallback:
                from outlook_auto_register.service.resource.proxy.proxy_utils import expand_proxy_unique

                extra = expand_proxy_unique(fallback, count - len(plan))
                plan = plan + list(extra)
                meta["fallback_used"] = True
        if not plan:
            hint = "代理池无可用条目"
            if provider_filter:
                hint += f"（代理商 {provider_filter}）"
            hint += "。请先在「代理池」页添加并启用代理。"
            raise ValueError(hint)
        meta["proxy_selection"] = p.get("proxy_selection") or "auto"
        if provider_filter:
            meta["proxy_provider"] = provider_filter
        return plan, meta
    proxy = (p.get("proxy") or "").strip()
    if not proxy:
        raise ValueError("请选择代理池分配方式，或在「代理池」页添加条目。")
    from outlook_auto_register.service.registration.batch_service import _plan_proxies

    return [x or "" for x in _plan_proxies(proxy, count)], {"source": "manual"}


def _after_register_proxy(
    email: str,
    assignments: list[dict[str, Any]],
    index: int,
    *,
    success: bool,
    reg_country: str = "US",
    error: str = "",
) -> None:
    if index >= len(assignments):
        return
    a = assignments[index]
    pid = a.get("proxy_id") or ""
    resolved = a.get("resolved") or ""
    if success and email and pid and resolved:
        proxy_pool.bind_account(email, pid, resolved, purpose="register")
    if pid:
        proxy_pool.record_result(
            pid,
            success=success,
            reg_country=reg_country,
            purpose="register",
            email=email if success else "",
            error=error if not success else "",
        )


def _apply_token_mode(mode: str) -> str:
    """按选择切换邮件令牌 scope，返回产出格式 mode（graph_recovery 不降级为 graph）。"""
    normalize = getattr(reg_constants, "normalize_token_mode", lambda m: (m or "graph").strip().lower())
    mode = normalize(mode)
    scope_map = getattr(reg_constants, "MAIL_SCOPE_BY_MODE", None) or getattr(
        reg_constants, "_MAIL_SCOPE_BY_MODE", {}
    )
    is_recovery = getattr(reg_constants, "is_recovery_mode", lambda m: m in ("recovery", "login_exe"))
    is_graph_recovery = getattr(reg_constants, "is_graph_recovery_mode", lambda m: m == "graph_recovery")

    dual = False
    if is_graph_recovery(mode):
        effective = "graph_recovery"
        scope = scope_map.get("graph")
    elif mode == "dual" and not DUAL_READY:
        effective = "graph"
        scope = scope_map.get("graph")
    else:
        scope = scope_map.get(mode)
        effective = mode
        if not scope:
            effective = "graph"
            scope = scope_map.get("graph")
        dual = effective == "dual"
        if is_recovery(effective):
            dual = False
            scope = scope or scope_map.get("login_exe") or scope_map.get("imap")

    reg_constants.DUAL_TOKEN = dual
    post_register.DUAL_TOKEN = dual
    reg_module.DUAL_TOKEN = dual
    oauth_mode = "graph" if is_graph_recovery(mode) else effective
    if hasattr(reg_constants, "MAIL_TOKEN_MODE"):
        reg_constants.MAIL_TOKEN_MODE = oauth_mode
    if scope:
        os.environ["OUTLOOK_MAIL_TOKEN_MODE"] = oauth_mode
        post_register.MAIL_SCOPE = scope
        reg_constants.MAIL_SCOPE = scope
    return effective


def _job_combo(result: RegisterResult, mode: str) -> str:
    if hasattr(result, "product_combo"):
        return result.product_combo(mode)
    return result.to_combo()


# ---------------------------------------------------------------------------
# 账号元数据（备注 / 标签 / 测活缓存）——存 accounts/webapp_meta.json
# ---------------------------------------------------------------------------


def _load_meta() -> dict[str, Any]:
    """遗留兼容：元数据已迁入 SQLite account_meta。"""
    return {}


def _save_meta(meta: dict[str, Any]) -> None:
    del meta


def _update_meta(email: str, patch: dict[str, Any]) -> None:
    account_store.update_meta(email, patch)


_FN_TS = re.compile(r"_(\d{8})_(\d{6})\.json$")
_BATCH_LABEL_SAFE = re.compile(r"[^\w\-.@+]+")


def _domain_slug(domain: str) -> str:
    d = (domain or "@outlook.com").strip().lstrip("@").split(".")[0]
    return (d or "outlook").lower()


def _token_mode_slug(mode: str) -> str:
    m = (mode or "graph").strip().lower()
    return {
        "graph": "G4",
        "graph_recovery": "G6",
        "recovery": "IMAP6",
        "login_exe": "IMAP6",
        "dual": "DUAL",
        "outlook_rest": "REST",
        "imap": "IMAP",
    }.get(m, m[:8].upper())


def _sanitize_batch_label(raw: str) -> str:
    s = _BATCH_LABEL_SAFE.sub("-", (raw or "").strip())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:48]


def _existing_batch_labels(*, jobs: Optional[dict[str, Any]] = None) -> set[str]:
    labels: set[str] = set()
    for rec in _load_jobs_store():
        lb = _sanitize_batch_label(str(rec.get("batch_label") or ""))
        if lb:
            labels.add(lb)
    src = jobs if jobs is not None else _jobs
    if jobs is None:
        with _jobs_lock:
            for j in src.values():
                lb = _sanitize_batch_label(str(getattr(j, "batch_label", "") or ""))
                if lb:
                    labels.add(lb)
    else:
        for j in src.values():
            lb = _sanitize_batch_label(str(getattr(j, "batch_label", "") or ""))
            if lb:
                labels.add(lb)
    return labels


def _make_batch_label(params: dict[str, Any], batch_no: int, *, jobs: Optional[dict[str, Any]] = None) -> str:
    """生成有业务含义的批次名；用户可传 batch_label 覆盖。"""
    custom = _sanitize_batch_label(str(params.get("batch_label") or ""))
    if custom:
        return custom
    country = (params.get("country") or "US").strip().upper()[:6]
    dom = _domain_slug(str(params.get("domain") or "@outlook.com"))
    count = max(1, int(params.get("count") or 1))
    mode = _token_mode_slug(str(params.get("token_mode") or "graph"))
    prefix = _sanitize_batch_label(str(params.get("prefix") or ""))
    date_part = datetime.now().strftime("%m%d")
    parts = [date_part, country, dom]
    if prefix:
        parts.append(prefix[:12])
    parts.extend([f"{count}x", mode])
    label = "-".join(p for p in parts if p)
    if label in _existing_batch_labels(jobs=jobs):
        label = f"{label}-#{batch_no}"
    return label[:48]


def _parse_dt(raw: str) -> Optional[datetime]:
    s = (raw or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _dt_iso(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt else ""


def _infer_created_at(fp: Path, data: dict[str, Any]) -> str:
    existing = _parse_dt(str(data.get("created_at") or ""))
    if existing:
        return _dt_iso(existing)
    m = _FN_TS.search(fp.name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(fp.stat().st_mtime).isoformat()
    except OSError:
        return ""


def _best_updated_at(data: dict[str, Any], meta_entry: dict[str, Any], fp: Optional[Path] = None) -> str:
    cands: list[datetime] = []
    for raw in (
        data.get("updated_at"),
        data.get("rescued_at"),
        data.get("last_alive_at"),
        (meta_entry or {}).get("updated_at"),
        ((meta_entry or {}).get("verify") or {}).get("checked_at"),
    ):
        dt = _parse_dt(str(raw or ""))
        if dt:
            cands.append(dt.replace(tzinfo=None) if dt.tzinfo else dt)
    if fp is not None:
        try:
            cands.append(datetime.fromtimestamp(fp.stat().st_mtime))
        except OSError:
            pass
    if not cands:
        created = _parse_dt(str(data.get("created_at") or ""))
        return _dt_iso(created.replace(tzinfo=None) if created and created.tzinfo else created)
    return max(cands).isoformat()


def _best_last_alive(data: dict[str, Any], meta_entry: dict[str, Any]) -> str:
    verify = (meta_entry or {}).get("verify") or {}
    if verify.get("checked_at"):
        return str(verify["checked_at"])
    for key in ("last_alive_at", "rescued_at"):
        if data.get(key):
            return str(data[key])
    return ""


def _survival_end_at(row: dict[str, Any], meta_entry: dict[str, Any]) -> Optional[datetime]:
    """存活统计终点：首次测活/保活确认（活或死）的时刻，不用「当前时间」。"""
    verify = (meta_entry or {}).get("verify")
    if isinstance(verify, dict) and verify.get("checked_at"):
        return _parse_dt(str(verify["checked_at"]))
    return None


def _compute_alive_seconds(created_raw: str, end_dt: Optional[datetime]) -> Optional[int]:
    created = _parse_dt(created_raw)
    if not created or not end_dt:
        return None
    created_naive = created.replace(tzinfo=None) if created.tzinfo else created
    end_naive = end_dt.replace(tzinfo=None) if end_dt.tzinfo else end_dt
    if end_naive < created_naive:
        return None
    return max(0, int((end_naive - created_naive).total_seconds()))


def _patch_account_json(email: str, patch: dict[str, Any]) -> None:
    account_store.patch_account(email, patch)


# ---------------------------------------------------------------------------
# 任务 / 日志基础设施
# ---------------------------------------------------------------------------

_jobs: dict[str, "Job"] = {}
_jobs_lock = threading.Lock()
_jobs_file_lock = threading.Lock()
_thread_job: dict[int, str] = {}  # 线程 ident -> job_id，日志归属
_active_batch_job: Optional[str] = None  # 引擎批量运行时：其内部线程池日志归属到此任务


class Job:
    def __init__(self, job_id: str, params: dict[str, Any]):
        self.id = job_id
        self.params = params
        self.status = "running"
        self.created_at = datetime.now().isoformat()
        self.count = int(params.get("count", 1))
        self.concurrency = int(params.get("concurrency", 1) or 1)
        self.batch_no = int(params.get("batch_no") or 0)
        self.batch_label = str(params.get("batch_label") or (f"B{self.batch_no}" if self.batch_no else ""))
        self.accounts: list[dict[str, Any]] = [
            {
                "index": i + 1,
                "status": "等待中",
                "email": "",
                "password": "",
                "client_id": "",
                "refresh_token": "",
                "combo": "",
                "combo_dual": "",
                "recovery_email": "",
                "recovery_password": "",
                "login_token": False,
                "error": "",
                "saved_path": "",
            }
            for i in range(self.count)
        ]
        self.logs: list[dict[str, Any]] = []
        self.batch_summary: Optional[dict[str, Any]] = None
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._lock = threading.Lock()
        self._cancelled = False
        self._cancelled = False

    def emit(self, event: dict[str, Any]) -> None:
        self._queue.put(event)

    def push_log(self, level: str, msg: str) -> None:
        rec = {"ts": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg}
        with self._lock:
            self.logs.append(rec)
            if len(self.logs) > 2000:
                self.logs = self.logs[-2000:]
        self.emit({"type": "log", **rec})

    def update_account(self, index0: int, **fields: Any) -> None:
        with self._lock:
            self.accounts[index0].update(fields)
            snapshot = dict(self.accounts[index0])
        self.emit({"type": "account", "account": snapshot})

    def counts(self) -> tuple[int, int]:
        ok = sum(1 for a in self.accounts if str(a["status"]).startswith("成功"))
        fail = sum(1 for a in self.accounts if a["status"] == "失败")
        return ok, fail

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ok, fail = self.counts()
            return {
                "id": self.id,
                "batch_no": self.batch_no,
                "batch_label": self.batch_label,
                "status": self.status,
                "created_at": self.created_at,
                "count": self.count,
                "concurrency": self.concurrency,
                "dry_run": bool(self.params.get("dry_run")),
                "token_mode": self.params.get("token_mode"),
                "ok_count": ok,
                "fail_count": fail,
                "params": _mask_params(self.params),
                "accounts": [dict(a) for a in self.accounts],
                "logs": list(self.logs[-500:]),
                "batch_summary": self.batch_summary,
            }

    def summary(self) -> dict[str, Any]:
        ok, fail = self.counts()
        return {
            "id": self.id,
            "batch_no": self.batch_no,
            "batch_label": self.batch_label,
            "status": self.status,
            "created_at": self.created_at,
            "count": self.count,
            "concurrency": self.concurrency,
            "dry_run": bool(self.params.get("dry_run")),
            "token_mode": self.params.get("token_mode"),
            "ok_count": ok,
            "fail_count": fail,
        }


class _JobLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        if not (record.name == "service" or record.name.startswith("service.")):
            return
        job_id = _thread_job.get(threading.get_ident()) or _active_batch_job
        if not job_id:
            return
        job = _jobs.get(job_id)
        if not job:
            return
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        job.push_log(record.levelname, msg)


def _install_log_handler() -> None:
    handler = _JobLogHandler()
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    if not any(isinstance(h, _JobLogHandler) for h in root.handlers):
        root.addHandler(handler)
    pkg_logger = logging.getLogger("service")
    pkg_logger.setLevel(logging.INFO)


_install_log_handler()


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return secret[:4] + "***" + secret[-4:]


def _mask_params(params: dict[str, Any]) -> dict[str, Any]:
    out = dict(params)
    if out.get("captcha_key"):
        out["captcha_key"] = _mask(out["captcha_key"])
    return out


def _load_jobs_store() -> list[dict[str, Any]]:
    return account_store.list_jobs()


def _job_record(job: "Job") -> dict[str, Any]:
    ok, fail = job.counts()
    emails = [
        a.get("email")
        for a in job.accounts
        if a.get("email") and str(a.get("status") or "").startswith("成功")
    ]
    return {
        "id": job.id,
        "batch_no": job.batch_no,
        "batch_label": job.batch_label,
        "created_at": job.created_at,
        "status": job.status,
        "count": job.count,
        "concurrency": job.concurrency,
        "token_mode": job.params.get("token_mode"),
        "dry_run": bool(job.params.get("dry_run")),
        "ok_count": ok,
        "fail_count": fail,
        "emails": emails,
        "params": _mask_params(job.params),
    }


def _persist_job(job: "Job") -> None:
    if job.params.get("dry_run"):
        return
    account_store.save_job(_job_record(job))


def _finish_job(job: "Job", status: str, log_msg: str = "") -> None:
    job.status = status
    if log_msg:
        job.push_log("WARNING" if status in {"cancelled", "error"} else "INFO", log_msg)
    with job._lock:
        for acct in job.accounts:
            if acct.get("status") in {"等待中", "进行中"}:
                acct["status"] = "失败"
                if status == "cancelled" and not acct.get("error"):
                    acct["error"] = "任务已取消"
    try:
        _persist_job(job)
    except Exception:  # noqa: BLE001
        pass
    job.emit({"type": "done", "status": job.status})


def _cancel_job(job: "Job", reason: str = "用户取消") -> None:
    if job.status != "running":
        return
    job._cancelled = True
    _finish_job(job, "cancelled", f"任务已取消：{reason}")


def _reconcile_stale_jobs_on_startup() -> None:
    """服务重启后，内存无任务但 DB 仍标 running 的批次改为 error。"""
    try:
        live_ids = set(_jobs.keys())
        for rec in _load_jobs_store():
            if rec.get("status") != "running" or rec.get("id") in live_ids:
                continue
            rec["status"] = "error"
            rec["fail_count"] = max(int(rec.get("fail_count") or 0), int(rec.get("count") or 1))
            account_store.save_job(rec)
    except Exception:  # noqa: BLE001
        pass



def _batch_index() -> dict[str, dict[str, Any]]:
    out = account_store.batch_index()
    with _jobs_lock:
        jobs = list(_jobs.values())
    for j in jobs:
        info = {"batch_id": j.id, "batch_no": j.batch_no, "batch_label": j.batch_label}
        for a in j.accounts:
            em = a.get("email") or ""
            if em and str(a.get("status") or "").startswith("成功"):
                out[em] = info
    return out


# ---------------------------------------------------------------------------
# 注册执行（并发 + register_batch 特性探测）
# ---------------------------------------------------------------------------


def _run_dry(job: Job) -> None:
    steps = [
        "代理预检通过: (模拟) 出口=203.0.113.7",
        "选用邮箱: 模拟随机前缀@outlook.com",
        "risk/initialize → humanSensorUrl 预加载",
        "captcha.run silent → press（模拟通过）",
        "risk/verify #2 challengeSolution 提交",
        "CreateAccount 成功",
        "oauth20_authorize.srf slt 登录（模拟）",
    ]
    mode = job.params.get("token_mode") or DEFAULT_TOKEN_MODE
    job.push_log("INFO", f"产出格式: {mode}（干跑，仅演示）｜并发度 {job.concurrency}")

    def one(i: int) -> None:
        job.update_account(i, status="进行中")
        job.push_log("INFO", f"[#{i+1}] 干跑开始（不消耗真实资源）")
        for s in steps:
            job.push_log("INFO", f"[#{i+1}] {s}")
            time.sleep(0.08)
        email = f"dryrun{i+1}_{uuid.uuid4().hex[:6]}@outlook.com"
        pwd = f"DryRunPwd{i+1}!"
        if mode in ("graph_recovery", "login_exe", "recovery"):
            rec = f"rec{i+1}_{uuid.uuid4().hex[:6]}@your-cf-domain.com" if mode == "graph_recovery" else f"rec{i+1}_{uuid.uuid4().hex[:6]}@your-recovery-host.com"
            rec_pwd = "cf_domain" if mode == "graph_recovery" else "DryRunRecPwd"
            combo = f"{email}----{pwd}----{MAIL_CLIENT_ID}--------{rec}----{rec_pwd}"
            label = "Graph 六段式" if mode == "graph_recovery" else "login.exe 六段式(IMAP)"
            job.push_log("INFO", f"[#{i+1}] 干跑产出 {label}（恢复邮箱，非 dual）")
        else:
            combo = f"{email}----{pwd}----{MAIL_CLIENT_ID}----"
        job.update_account(
            i, status="成功(干跑)", email=email, password=pwd,
            client_id=MAIL_CLIENT_ID, refresh_token="", combo=combo, error="",
        )
        job.push_log("INFO", f"[#{i+1}] 干跑完成（未写盘、未消耗额度）")

    conc = max(1, min(job.concurrency, job.count))
    with ThreadPoolExecutor(max_workers=conc) as ex:
        for f in as_completed([ex.submit(one, i) for i in range(job.count)]):
            f.result()


def _do_register_one(job: Job, i: int, p: dict[str, Any]) -> None:
    """单个注册（线程池 worker 内执行），日志归属到本任务。"""
    _thread_job[threading.get_ident()] = job.id
    job.update_account(i, status="进行中")
    ph = _proxy_line_for_index(p, i)
    if ph:
        job.push_log("INFO", f"[#{i + 1}] 开始注册（代理 {ph}）")
    else:
        job.push_log("INFO", f"[#{i + 1}] 开始注册")
    plan = p.get("proxy_plan") or []
    one_proxy = plan[i] if i < len(plan) else (p.get("proxy") or None)
    assignments = p.get("proxy_assignments") or []
    try:
        try:
            result: RegisterResult = register_one(
                email_prefix=(p.get("prefix") or None),
                email_domain=p.get("domain") or "@outlook.com",
                country=p.get("country") or "US",
                proxy=one_proxy or None,
                px_mode=p.get("px_mode") or "solver",
                skip_post_login=bool(p.get("skip_login")),
                fetch_mail_token=not bool(p.get("no_mail_token")),
            )
        except Exception as exc:  # noqa: BLE001
            job.update_account(i, status="失败", error=str(exc))
            job.push_log("ERROR", f"[#{i+1}] 异常: {exc}")
            _after_register_proxy(
                "", assignments, i, success=False,
                reg_country=p.get("country") or "US", error=str(exc),
            )
            return
    finally:
        _thread_job.pop(threading.get_ident(), None)

    if result.success:
        saved = ""
        try:
            with _save_lock:
                saved = save_account(
                    result, str(ACCOUNTS_DIR),
                    batch_id=job.id, batch_no=job.batch_no, batch_label=job.batch_label,
                )
        except Exception as exc:  # noqa: BLE001
            job.push_log("ERROR", f"[#{i+1}] 保存失败: {exc}")
        mode = p.get("token_mode") or DEFAULT_TOKEN_MODE
        combo = _job_combo(result, mode)
        job.update_account(
            i, status="成功", email=result.email, password=result.password,
            client_id=result.client_id, refresh_token=result.refresh_token or "",
            recovery_email=result.recovery_email or "",
            recovery_password=result.recovery_password or "",
            combo=combo,
            combo_dual=result.to_combo(dual=True) if result.login_refresh_token else "",
            combo_recovery=result.to_combo(recovery=True) if result.recovery_email else "",
            error="", saved_path=saved,
        )
        tip = "已取 refresh_token" if result.refresh_token else "无 refresh_token"
        if result.recovery_email:
            tip += "，已绑恢复邮箱"
        job.push_log("INFO", f"[#{i+1}] 注册成功 {result.email}（{tip}）")
        _after_register_proxy(
            result.email or "", assignments, i, success=True,
            reg_country=p.get("country") or "US",
        )
    else:
        job.update_account(i, status="失败", error=result.error)
        job.push_log("ERROR", f"[#{i+1}] 注册失败: {result.error}")
        _after_register_proxy(
            "", assignments, i, success=False,
            reg_country=p.get("country") or "US", error=result.error or "",
        )


def _proxy_line_for_index(p: dict[str, Any], idx: int) -> str:
    plan = p.get("proxy_plan") or []
    tpl = plan[idx] if idx < len(plan) else (p.get("proxy") or "")
    if not tpl:
        return ""
    try:
        return proxy_pool.mask_template(str(tpl))
    except Exception:  # noqa: BLE001
        return str(tpl)[:72]


def _run_batch_iter(job: Job, p: dict[str, Any]) -> bool:
    """用引擎 register_batch_iter 驱动 SSE。

    引擎内部已 save_account，网页不再重复保存。返回 True 表示已由本函数处理（成功
    或已消费部分事件不宜重跑），False 表示未开跑可安全回退线程池。
    """
    if not BATCH_READY or register_batch_iter is None:
        return False
    global _active_batch_job
    assignments = p.get("proxy_assignments") or []
    proxy_templates = [
        (a.get("template") or "").strip() or None for a in assignments
    ]
    if len(proxy_templates) < job.count:
        proxy_templates += [None] * (job.count - len(proxy_templates))
    kwargs = dict(
        concurrency=job.concurrency,
        email_prefix=(p.get("prefix") or None),
        email_domain=p.get("domain") or "@outlook.com",
        country=p.get("country") or "US",
        proxy=(p.get("proxy") or None),
        proxy_plan=p.get("proxy_plan"),
        proxy_templates=proxy_templates,
        px_mode=p.get("px_mode") or "solver",
        skip_post_login=bool(p.get("skip_login")),
        fetch_mail_token=not bool(p.get("no_mail_token")),
        output_dir=str(ACCOUNTS_DIR),
        batch_id=job.id,
        batch_no=job.batch_no,
        batch_label=job.batch_label,
        jitter_min=p.get("jitter_min"),
        jitter_max=p.get("jitter_max"),
    )
    _active_batch_job = job.id  # 引擎线程池内 register_one 的日志归属到本任务
    consumed = False
    try:
        for ev in register_batch_iter(job.count, **kwargs):
            etype = ev.get("type")
            if etype == "start":
                jitter = ev.get("jitter") or []
                jtxt = (
                    f"{jitter[0]}–{jitter[1]}秒"
                    if len(jitter) >= 2 and float(jitter[1] or 0) > 0
                    else "无"
                )
                job.push_log(
                    "INFO",
                    f"引擎批量启动：共 {ev.get('total')} 个，并发 {ev.get('concurrency')}，"
                    f"相邻启动错峰 {jtxt}",
                )
                if ev.get("proxy_unique"):
                    job.push_log("INFO", "防封·一号一 IP：每号独立 {sid} 会话（出口 IP 不同）")
                elif not ev.get("proxy_has_sid") and (p.get("proxy") or p.get("proxy_plan")):
                    job.push_log(
                        "WARNING",
                        "代理未含 {sid}：全批可能共用同一出口 IP，建议改用带 {sid} 的模板",
                    )
            elif etype == "account_start":
                consumed = True
                idx = int(ev.get("index", 0))
                if idx >= job.count:
                    continue
                job.update_account(idx, status="进行中")
                ph = _proxy_line_for_index(p, idx)
                if ph:
                    job.push_log("INFO", f"[#{idx + 1}] 开始注册（代理 {ph}）")
                else:
                    job.push_log("INFO", f"[#{idx + 1}] 开始注册")
            elif etype == "result":
                consumed = True
                idx = int(ev.get("index", 0))
                if idx >= job.count:
                    continue
                i = idx
                combo = ev.get("combo") or ""
                rec_combo = ev.get("combo_recovery") or ""
                if p.get("token_mode") in ("login_exe", "recovery", "graph_recovery") and rec_combo:
                    combo = rec_combo
                if combo:
                    _e, pwd, cid, rt = _split_combo(combo)
                else:
                    pwd = cid = rt = ""
                if ev.get("success"):
                    job.update_account(
                        i, status="成功", email=ev.get("email") or "",
                        password=pwd, client_id=cid, refresh_token=rt,
                        recovery_email=ev.get("recovery_email") or "",
                        recovery_password=ev.get("recovery_password") or "",
                        combo=combo, combo_dual=ev.get("combo_dual") or "",
                        combo_recovery=rec_combo,
                        login_token=bool(ev.get("login_token_present")),
                        error="", saved_path="(引擎已保存)",
                    )
                    _after_register_proxy(
                        ev.get("email") or "", assignments, idx, success=True,
                        reg_country=p.get("country") or "US",
                    )
                    dual_tip = "，含双令牌" if ev.get("login_token_present") else ""
                    job.push_log(
                        "INFO",
                        f"[#{i+1}] 成功 {ev.get('email')}（{ev.get('elapsed')}s{dual_tip}）",
                    )
                else:
                    job.update_account(
                        i, status="失败", email=ev.get("email") or "",
                        error=ev.get("error") or "",
                    )
                    _after_register_proxy(
                        ev.get("email") or "", assignments, idx, success=False,
                        reg_country=p.get("country") or "US",
                        error=ev.get("error") or "",
                    )
                    job.push_log("ERROR", f"[#{i+1}] 失败：{ev.get('error')}")
            elif etype == "done":
                job.batch_summary = {
                    "total": ev.get("total"), "ok": ev.get("ok"),
                    "failed": ev.get("failed"), "elapsed": ev.get("elapsed"),
                    "avg_per_account": ev.get("avg_per_account"),
                    "avg_stage_timings": ev.get("avg_stage_timings") or {},
                }
                job.emit({"type": "summary", "summary": job.batch_summary})
                stages = job.batch_summary["avg_stage_timings"]
                top = sorted(stages.items(), key=lambda kv: kv[1], reverse=True)[:3]
                top_txt = "，".join(f"{k}={v}s" for k, v in top) or "无"
                job.push_log(
                    "INFO",
                    f"本批完成：成功 {ev.get('ok')}/{ev.get('total')}，本批耗时 "
                    f"{ev.get('elapsed')}s，单号均耗 {ev.get('avg_per_account')}s，"
                    f"阶段大头：{top_txt}",
                )
        return True
    except Exception as exc:  # noqa: BLE001
        job.push_log("WARNING", f"引擎 register_batch_iter 执行异常: {exc}")
        return consumed  # 已消费部分事件则不重跑，避免重复真实注册
    finally:
        _active_batch_job = None


def _apply_captcha_runtime(p: dict[str, Any]) -> str:
    """按所选打码平台注入环境变量，返回 px_mode。"""
    provider = _resolve_captcha_provider(p)
    p["captcha_provider"] = provider
    meta = _captcha_provider_meta(provider)
    if meta and meta.get("key_setting"):
        db_key = (app_db.get_setting(meta["key_setting"]) or "").strip()
        if db_key:
            os.environ[meta["key_setting"]] = db_key
    # 兼容旧版注册页直接传 captcha_key（仅 captcha.run）
    legacy_key = (p.get("captcha_key") or "").strip()
    if legacy_key:
        os.environ["CAPTCHA_RUN_API_KEY"] = legacy_key
        app_db.set_setting("CAPTCHA_RUN_API_KEY", legacy_key)
        if not p.get("captcha_provider"):
            provider = "captcha_run"
            p["captcha_provider"] = provider

    if provider == "offcaptcha":
        os.environ["PX_SOLVER"] = "offcaptcha"
        os.environ.setdefault("OFFCAPTCHA_APPLY_UA", "1")
        os.environ.pop("PX_PRESS_FALLBACK", None)
        px_mode = "offcaptcha"
    elif provider == "local":
        os.environ["PX_SOLVER"] = "swiftshader"
        os.environ.pop("PX_PRESS_FALLBACK", None)
        px_mode = "local"
    elif provider == "ezcaptcha":
        os.environ.pop("PX_SOLVER", None)
        os.environ["PX_PRESS_FALLBACK"] = "ezcaptcha"
        px_mode = "solver"
    elif provider == "capsolver":
        os.environ.pop("PX_SOLVER", None)
        os.environ["PX_PRESS_FALLBACK"] = "capsolver"
        px_mode = "solver"
    else:
        os.environ.pop("PX_SOLVER", None)
        os.environ.pop("PX_PRESS_FALLBACK", None)
        px_mode = "solver"
    p["px_mode"] = px_mode
    return provider, px_mode, (meta or {}).get("label") or provider


def _run_real(job: Job) -> None:
    p = job.params
    provider, px_mode, provider_label = _apply_captcha_runtime(p)
    job.push_log("INFO", f"打码平台: {provider_label}（px_mode={px_mode}）")

    job.push_log("INFO", "正在规划代理…")
    try:
        plan, pmeta = _build_register_proxy_plan(p, job.count)
    except ValueError as exc:
        job.push_log("ERROR", str(exc))
        raise
    p["proxy_plan"] = plan
    pmeta_assignments = pmeta.get("assignments") or []
    if not pmeta_assignments:
        manual_tpl = (p.get("proxy") or "").strip()
        pmeta_assignments = [
            {
                "index": i,
                "template": manual_tpl,
                "resolved": plan[i] if i < len(plan) else "",
            }
            for i in range(job.count)
        ]
    p["proxy_assignments"] = pmeta_assignments
    if p.get("use_proxy_pool"):
        job.push_log(
            "INFO",
            f"代理池：已规划 {len(plan)} 条（策略 {pmeta.get('strategy') or '—'}）",
        )
    retries = max(1, int((os.environ.get("REG_PROXY_RETRIES") or "3").strip() or "3"))
    job.push_log("INFO", f"PX/代理失败自动重试：最多 {retries} 次（REG_PROXY_RETRIES）")

    requested_mode = p.get("token_mode") or DEFAULT_TOKEN_MODE
    effective = _apply_token_mode(requested_mode)
    if effective != requested_mode:
        job.push_log("WARNING", f"产出格式 {requested_mode} 不可用，已降级为 {effective}")
    fmt_label = {"graph": "Graph 四段", "graph_recovery": "Graph 六段", "dual": "双令牌六段"}.get(
        effective, effective
    )
    job.push_log("INFO", f"产出格式: {fmt_label}｜并发度 {job.concurrency}")

    # 优先引擎生成器；不可用则线程池兜底
    if _run_batch_iter(job, p):
        return
    job.push_log("INFO", "register_batch_iter 不可用，使用线程池兜底并发 register_one")
    conc = max(1, min(job.concurrency, job.count))
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = [ex.submit(_do_register_one, job, i, p) for i in range(job.count)]
        for f in as_completed(futs):
            f.result()


def _job_worker(job_id: str) -> None:
    job = _jobs[job_id]
    _thread_job[threading.get_ident()] = job_id
    job.push_log(
        "INFO",
        f"批次 {job.batch_label} 开始执行（{job.count} 个，并发 {job.concurrency}）",
    )
    try:
        if job.params.get("dry_run"):
            _run_dry(job)
        else:
            _run_real(job)
        job.status = "done"
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.push_log("ERROR", f"任务异常终止: {exc}")
    finally:
        _thread_job.pop(threading.get_ident(), None)
        try:
            _persist_job(job)
        except Exception:  # noqa: BLE001
            pass
        job.emit({"type": "done", "status": job.status})
        job.push_log("INFO", "任务结束。")


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

def _startup_log() -> None:
    try:
        app_db.ensure_initialized(ACCOUNTS_DIR)
        st = app_db.db_status()
        logger.info(
            "SQLite 已就绪: %s（账号 %s · 代理 %s）",
            st.get("path"),
            st.get("accounts", 0),
            st.get("proxies", 0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("SQLite 初始化失败: %s", exc)
    if cf_domain_mail.cf_domain_backend_active() and cf_domain_mail.cf_configured():
        logging.getLogger(__name__).info(
            "proofs 收码后端: CF 域名 %s", cf_domain_mail.load_config().domain
        )
    elif not ext_recovery_pool.external_pool_enabled():
        logging.getLogger(__name__).warning(
            "恢复邮箱未配置：请设置 OUTLOOK_RECOVERY_BACKEND=cf_domain（your-cf-domain.com）"
            "或 OUTLOOK_EXTERNAL_RECOVERY_POOL_FILE + OUTLOOK_RECOVERY_IMAP_HOST"
        )
    _reconcile_stale_jobs_on_startup()

def _setting_status(key: str) -> dict[str, Any]:
    val = (os.environ.get(key) or app_db.get_setting(key) or "").strip()
    return {"set": bool(val), "masked": _mask(val), "source": "env" if os.environ.get(key) else ("db" if val else "")}


def _register_options_payload() -> dict[str, Any]:
    captcha_items: list[dict[str, Any]] = []
    for row in CAPTCHA_PROVIDER_CATALOG:
        st = _setting_status(row["key_setting"]) if row.get("key_setting") else {"set": True, "masked": "", "source": "builtin"}
        captcha_items.append(
            {
                "id": row["id"],
                "label": row["label"],
                "hint": row.get("hint") or "",
                "configured": _captcha_provider_configured(row["id"]),
                "requires_key": bool(row.get("key_setting")),
                **st,
            }
        )
    default_provider = (
        app_db.get_setting("DEFAULT_CAPTCHA_PROVIDER")
        or next((x["id"] for x in captcha_items if x["configured"]), "offcaptcha")
    )
    pool_stats = proxy_pool.pool_stats()
    proxy_options: list[dict[str, Any]] = [
        {
            "value": "auto",
            "label": f"自动（代理池 · {pool_stats.get('enabled', 0)} 条可用）",
            "count": pool_stats.get("enabled", 0),
        }
    ]
    for prov in proxy_pool.list_providers():
        name = prov.get("name") or ""
        enabled = int(prov.get("enabled") or 0)
        if not name:
            continue
        proxy_options.append(
            {
                "value": f"provider:{name}",
                "label": f"{name}（{enabled} 条可用）",
                "count": enabled,
                "provider": name,
            }
        )
    return {
        "ok": True,
        "captcha": {
            "items": captcha_items,
            "default": default_provider,
        },
        "proxy": {
            "options": proxy_options,
            "stats": pool_stats,
        },
    }

def _split_combo(combo: str) -> tuple[str, str, str, str]:
    """email----password----client_id----refresh_token → (email, pwd, cid, rt)。"""
    parts = combo.strip().split("----")
    if len(parts) < 4:
        return "", "", "", ""
    email = parts[0]
    pwd = parts[1]
    rt = next((p for p in parts if p.startswith("M.C")), "") or (parts[3] if len(parts) > 3 else "")
    cid = next((p for p in parts if len(p) == 36 and p.count("-") == 4), "") or parts[2]
    return email, pwd, cid, rt


def _apply_batch(row: dict[str, Any], data: dict[str, Any], index: dict[str, dict[str, Any]]) -> None:
    email = str(row.get("email") or "")
    hit = index.get(email) or {}
    batch_id = data.get("batch_id") or hit.get("batch_id") or ""
    batch_no = data.get("batch_no") if data.get("batch_no") not in (None, "") else hit.get("batch_no")
    batch_label = data.get("batch_label") or hit.get("batch_label") or (
        f"B{batch_no}" if batch_no else ""
    )
    row["batch_id"] = batch_id
    row["batch_no"] = batch_no
    row["batch_label"] = batch_label


def _load_accounts() -> list[dict[str, Any]]:
    """账号池列表：SQLite 为唯一数据源。"""
    app_db.ensure_initialized(ACCOUNTS_DIR)
    rows = account_store.list_accounts()
    batch_map = _batch_index()
    for row in rows:
        m = {"verify": row.get("verify")}
        _apply_batch(row, {
            "batch_id": row.get("batch_id"),
            "batch_no": row.get("batch_no"),
            "batch_label": row.get("batch_label"),
        }, batch_map)
        if not row.get("updated_at"):
            row["updated_at"] = _best_updated_at(row, m)
        if not row.get("last_alive_at"):
            row["last_alive_at"] = _best_last_alive(row, m)
        end_dt = _survival_end_at(row, m)
        row["survival_end_at"] = _dt_iso(end_dt) if end_dt else ""
        row["alive_seconds"] = _compute_alive_seconds(str(row.get("created_at") or ""), end_dt)
    return rows


def _is_graph_readable(row: dict[str, Any]) -> bool:
    v = row.get("verify") or {}
    return bool(v.get("ok") or v.get("graph"))


def _compute_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    today = datetime.now().strftime("%Y-%m-%d")
    total = len(rows)
    with_token = sum(1 for r in rows if r.get("has_token"))
    usable = dead = untested = 0
    today_new = 0
    for r in rows:
        v = r.get("verify")
        if v is None:
            untested += 1
        elif _is_graph_readable(r):
            usable += 1
        elif r.get("batch_label") or r.get("batch_no"):
            dead += 1
        # 无批次旧号的失败测活：概览不计入失活/未测，避免数字膨胀
        if r.get("created_at", "").startswith(today):
            today_new += 1
    return {
        "total": total,
        "with_token": with_token,
        "usable": usable,
        "dead": dead,
        "untested": untested,
        "today_new": today_new,
        "recovery_pool_configured": ext_recovery_pool.external_pool_enabled(),
    }


def _format_combo(row: dict[str, Any], fmt: str) -> str:
    if fmt == "dual":
        return row.get("combo_dual") or row.get("combo", "")
    if fmt == "recovery":
        if row.get("combo_recovery"):
            return row["combo_recovery"]
        rec_email = row.get("recovery_email", "")
        rec_pwd = row.get("recovery_password", "")
        if rec_email and rec_pwd and row.get("combo"):
            parts = row["combo"].split("----")
            if len(parts) >= 4:
                return "----".join(parts[:4] + [rec_email, rec_pwd])
        return ""
    return row.get("combo", "")
def _verify_one(email: str, refresh_token: str, proxy_url: str, test_imap: bool) -> dict[str, Any]:
    if not refresh_token:
        return {"ok": False, "email": email, "usable": [], "message": "缺少 refresh_token"}

    def _probe(via: str) -> dict[str, Any]:
        return graph_mail.probe_token(email or "unknown", refresh_token, proxy_url=via)

    probe = _probe(proxy_url)
    used_proxy = proxy_url
    if probe.get("transient") and proxy_url:
        logger.warning(
            "测活经代理 %s 网络失败，回退直连重试（refresh 换票无需注册代理）",
            proxy_url.split("@")[-1][:40] if "@" in proxy_url else proxy_url[:40],
        )
        probe = _probe("")
        used_proxy = ""
    if probe.get("transient"):
        detail = probe.get("detail", {})
        return {
            "ok": False,
            "transient": True,
            "unable": True,
            "email": email,
            "usable": [],
            "granted_scope": detail.get("granted_scope", ""),
            "refresh_error": detail.get("refresh", ""),
            "graph": {"status": detail.get("graph"), "ok": False},
            "outlook_rest": {"status": detail.get("outlook_rest"), "ok": False},
            "imap": {"tested": False},
            "summary": "测活暂不可用（网络/SSL），未改账号状态",
            "message": detail.get("refresh", "network"),
            "verify_via": "direct" if not used_proxy else "proxy",
        }
    detail = probe.get("detail", {})
    usable = list(probe.get("usable", []))
    graph_status = detail.get("graph")
    rest_status = detail.get("outlook_rest")
    res: dict[str, Any] = {
        "email": email,
        "granted_scope": detail.get("granted_scope", ""),
        "refresh_error": detail.get("refresh", ""),
        "graph": {"status": graph_status, "ok": graph_status == 200},
        "outlook_rest": {"status": rest_status, "ok": rest_status == 200},
        "imap": {"tested": False},
        "usable": usable,
        "verify_via": "direct" if not used_proxy else "proxy",
    }
    if proxy_url and not used_proxy:
        res["proxy_fallback"] = True
    if test_imap and email:
        im = enable_imap.imap_login_test(email, refresh_token, proxy_url=used_proxy)
        res["imap"] = {
            "tested": True,
            "ok": bool(im.get("ok")),
            "stage": im.get("stage", ""),
            "detail": str(im.get("detail", ""))[:200],
            "message_count": im.get("message_count"),
        }
        if im.get("ok"):
            usable.append("imap")
    res["ok"] = bool(usable)
    if "graph" in usable:
        res["summary"] = "✅ 可用：Graph 令牌可读信（推荐）"
    elif "outlook_rest" in usable:
        res["summary"] = "✅ 可用：Outlook REST 令牌可读信"
    elif res["imap"].get("ok"):
        res["summary"] = "⚠️ 仅 IMAP 可用（老号）"
    else:
        res["summary"] = "❌ 不可用：graph/outlook_rest/imap 均未通过"
    return res


def _cache_verify(res: dict[str, Any]) -> None:
    email = res.get("email")
    if not email:
        return
    account_store.cache_verify(email, res)


# ---------------------------------------------------------------------------
# IMAP / 保活（占位，如实反馈）
# ---------------------------------------------------------------------------


def _account_json_path_for(email: str) -> Optional[Path]:
    if not ACCOUNTS_DIR.exists():
        return None
    skip = {META_FILE.name, JOBS_FILE.name}
    for fp in ACCOUNTS_DIR.glob("*.json"):
        if fp.name in skip:
            continue
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and data.get("email") == email:
            return fp
    return None


def _writeback_keepalive(email: str, new_line: str) -> None:
    """把轮换后的新行回写 账号 json / accounts.txt / accounts_dual.txt（在 _save_lock 内调用）。"""
    parts = new_line.split("----")
    if len(parts) < 4:
        return
    graph4 = "----".join(parts[:4])
    new_rt = parts[3]
    is_dual = len(parts) >= 6
    # 账号 json：更新 graph refresh_token / combo，六段时同步 combo_dual / login_*
    fp = _account_json_path_for(email)
    if fp:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            data["refresh_token"] = new_rt
            data["combo"] = graph4
            if is_dual:
                data["combo_dual"] = new_line
                data["login_client_id"] = parts[4]
                data["login_refresh_token"] = parts[5]
            now = datetime.now().isoformat()
            data["updated_at"] = now
            data["last_alive_at"] = now
            fp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    # accounts.txt（四段）
    if COMBO_FILE.exists():
        changed = False
        kept: list[str] = []
        for l in COMBO_FILE.read_text(encoding="utf-8").splitlines():
            e = l.split("----")[0].strip() if "----" in l else ""
            if e == email and not l.strip().startswith("#"):
                kept.append(graph4)
                changed = True
            else:
                kept.append(l)
        if changed:
            COMBO_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
    # accounts_dual.txt（六段）
    if is_dual and DUAL_FILE.exists():
        changed = False
        kept = []
        for l in DUAL_FILE.read_text(encoding="utf-8").splitlines():
            e = l.split("----")[0].strip() if "----" in l else ""
            if e == email and not l.strip().startswith("#"):
                kept.append(new_line)
                changed = True
            else:
                kept.append(l)
        if changed:
            DUAL_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
