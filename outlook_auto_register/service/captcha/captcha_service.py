from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional

import requests

from outlook_auto_register.config.constants import (
    ARKOSE_PUBLIC_KEY,
    CAPTCHA_RUN_API_BASE,
    CAPTCHA_RUN_API_BASE_GLOBAL,
    CAPTCHA_RUN_DEVELOPER_ID,
    PX_APP_ID,
)
from outlook_auto_register.service.resource.proxy.proxy_utils import parse_proxy, proxy_for_capsolver, proxy_for_captcha_run, timezone_for_country
from outlook_auto_register.service.risk.px_cookies import apply_captcha_run_token, captcha_run_has_token, normalize_px_solution

logger = logging.getLogger(__name__)


@dataclass
class CaptchaRunTask:
    """一次注册会话对应一个 captcha.run task（silent→press 共用 taskId）。"""

    task_id: str
    base: str
    variant: str
    silent_fetched: bool = False


# 这些密钥以「数据库为主」存储：环境变量为空时回落到 app_meta 的 setting:<KEY>。
# 让 CLI / Web / 引擎三方一致地从库里读，页面在库里存一次即可全栈生效。
_DB_BACKED_KEYS = {"CAPTCHA_RUN_API_KEY", "EZCAPTCHA_API_KEY", "CAPSOLVER_API_KEY"}


def _db_setting(key: str) -> str:
    try:
        from outlook_auto_register.dao.outlook_dao import get_setting

        return get_setting(key, "")
    except Exception:  # noqa: BLE001
        return ""


def _env(key: str, default: str = "") -> str:
    val = os.environ.get(key, "")
    if val:
        return val
    if key in _DB_BACKED_KEYS:
        db_val = _db_setting(key)
        if db_val:
            return db_val
    return os.environ.get(key, default)


def _ezcaptcha_base() -> str:
    return _env("EZCAPTCHA_API_BASE", "https://api.ez-captcha.com")


def _captcha_run_base() -> str:
    return _env("CAPTCHA_RUN_API_BASE", CAPTCHA_RUN_API_BASE)


def _ezcaptcha_proxy_fields(ctx: dict[str, Any]) -> dict[str, Any]:
    cfg = parse_proxy(ctx.get("proxy"))
    if not cfg:
        return {}
    return {
        "proxyType": "http",
        "proxyAddress": cfg.host,
        "proxyPort": cfg.port,
        "proxyLogin": cfg.username or "",
        "proxyPassword": cfg.password or "",
    }


def _poll_task(
    *,
    base: str,
    key: str,
    task_id: str,
    max_wait: int,
    result_key: str = "solution",
) -> Optional[dict[str, Any]]:
    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(3)
        try:
            resp = requests.post(
                f"{base}/getTaskResult",
                json={"clientKey": key, "taskId": task_id},
                timeout=30,
            )
            result = resp.json()
        except Exception as exc:
            logger.debug("轮询异常: %s", exc)
            continue

        status = result.get("status")
        if status == "ready":
            return result.get(result_key) or result.get("solution") or result
        if status in ("failed", "error") or result.get("errorId") == 1:
            logger.error("任务失败: %s", result.get("errorDescription") or result.get("message"))
            return None
    logger.error("任务超时 task_id=%s", task_id)
    return None


def _env_bool(key: str, default: bool = False) -> bool:
    val = _env(key, "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


def build_captcha_run_payload_legacy(ctx: dict[str, Any]) -> dict[str, Any]:
    """项目旧版 payload（含 websiteURL / cookies / _px*）。"""
    payload: dict[str, Any] = {
        "captchaType": "PxCaptcha2",
        "uaid": ctx.get("uaid", ""),
        "uuid": ctx.get("uuid", ""),
        "vid": ctx.get("vid", "") or ctx.get("pxvid", ""),
        "appId": ctx.get("app_id", PX_APP_ID),
        "websiteURL": ctx.get("page_url", "https://signup.live.com/"),
        "challengeUrl": ctx.get("challenge_url", ""),
        "userAgent": ctx.get("user_agent", ""),
        "cookies": ctx.get("cookies_header", ""),
        "_px3": ctx.get("px3", ""),
        "_pxde": ctx.get("pxde", ""),
        "_pxvid": ctx.get("pxvid", "") or ctx.get("vid", ""),
        "pxcts": ctx.get("pxcts", ""),
    }
    if ctx.get("proxy"):
        payload["proxy"] = ctx["proxy"]
    return payload


def build_captcha_run_payload_official(ctx: dict[str, Any]) -> dict[str, Any]:
    """PxCaptcha2 官方文档 POST /v2/tasks/ 请求体（Confluence PxCaptcha2）。"""
    cfg = parse_proxy(ctx.get("proxy"))
    allow_no_proxy = _env("CAPTCHA_RUN_PROXY_MODE", "").strip().lower() in (
        "never", "0", "off", "false", "noproxy",
    )
    if not cfg and not allow_no_proxy:
        raise RuntimeError("captcha.run 官方文档：host/port/login/password 代理必填")
    country = (ctx.get("country") or "US").strip().upper()
    ua = (_env("CAPTCHA_RUN_USER_AGENT") or ctx.get("user_agent") or "Win").strip()
    payload: dict[str, Any] = {
        "captchaType": "PxCaptcha2",
        "uaid": ctx.get("uaid", ""),
        "uuid": ctx.get("uuid", "") or "",
        "vid": (ctx.get("vid") or ctx.get("pxvid") or ""),
        "userAgent": ua,
        "country": country,
        "timezone": ctx.get("timezone") or timezone_for_country(country),
    }
    if cfg:
        payload.update({
            "host": cfg.host,
            "port": cfg.port,
            "login": cfg.username or "",
            "password": cfg.password or "",
        })
    dev = _env("CAPTCHA_RUN_DEVELOPER_ID") or CAPTCHA_RUN_DEVELOPER_ID
    if dev and _env_bool("CAPTCHA_RUN_INCLUDE_DEVELOPER", True):
        payload["developer"] = dev
    return payload


def build_captcha_run_payload_exe(ctx: dict[str, Any]) -> dict[str, Any]:
    """兼容旧脚本名；等同官方文档 payload。"""
    return build_captcha_run_payload_official(ctx)


def _captcha_run_include_proxy(phase: str) -> bool:
    mode = _env("CAPTCHA_RUN_PROXY_MODE", "").strip().lower()
    if mode in ("never", "0", "off", "false"):
        return False
    if mode in ("always", "1", "on", "true"):
        return True
    # 默认：silent 不传代理（平台 worker 更快出 token）；press 传会话代理
    return phase == "press"


def _strip_captcha_run_proxy(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    for key in ("host", "login", "password", "port", "proxy"):
        out.pop(key, None)
    return out


def build_captcha_run_payload(
    ctx: dict[str, Any],
    *,
    phase: str = "silent",
    include_proxy: Optional[bool] = None,
) -> dict[str, Any]:
    """captcha.run POST body。

    silent：exe 扁平模板（对齐 26.7.11）
    press：exe 模板 + 会话 cookie/_px*（打码端需要绑定当前 challenge 会话）
    """
    if include_proxy is None:
        include_proxy = _captcha_run_include_proxy(phase)

    if _env_bool("CAPTCHA_RUN_LEGACY_PAYLOAD"):
        payload = build_captcha_run_payload_legacy(ctx)
        if not include_proxy:
            payload.pop("proxy", None)
        return payload

    payload = build_captcha_run_payload_exe(ctx)
    if not include_proxy:
        payload = _strip_captcha_run_proxy(payload)

    if phase != "press":
        return payload

    for key, ctx_key in (
        ("websiteURL", "page_url"),
        ("challengeUrl", "challenge_url"),
        ("appId", "app_id"),
        ("cookies", "cookies_header"),
        ("_px3", "px3"),
        ("_pxde", "pxde"),
        ("_pxvid", "pxvid"),
        ("pxcts", "pxcts"),
    ):
        val = ctx.get(ctx_key, "")
        if val:
            payload[key] = val
    return payload


def captcha_run_payload_variants(ctx: dict[str, Any], *, phase: str) -> list[tuple[str, dict[str, Any]]]:
    """纯协议：按优先级尝试多种 POST 体。

    press 必须带会话代理（官方文档必填；无代理成功的 token 与微软会话 IP 不一致，会扣费但不认）。
    silent 可无代理（更快）。
    """
    variants: list[tuple[str, dict[str, Any]]] = []
    if phase == "silent":
        if ctx.get("proxy") and _env("CAPTCHA_RUN_PROXY_MODE", "").strip().lower() in (
            "always", "1", "on", "true",
        ):
            variants.append(("exe-proxy", build_captcha_run_payload_exe(ctx)))
        else:
            variants.append(("exe-noproxy", build_captcha_run_payload_exe({**ctx, "proxy": None})))
            if ctx.get("proxy"):
                variants.append(("exe-proxy", build_captcha_run_payload_exe(ctx)))
        variants.append(("legacy", build_captcha_run_payload_legacy(ctx)))
    else:
        if not ctx.get("proxy"):
            raise RuntimeError("press 打码必须传会话代理（与注册同一代理），否则 token 与微软 IP 不一致")
        # exe-exact：与 exe / 官方文档一致（扁平代理字段 + uuid/vid）
        variants.append(("exe-exact", build_captcha_run_payload_exe(ctx)))
        if _env_bool("CAPTCHA_RUN_PRESS_ALLOW_NOPROXY"):
            variants.append(("exe-noproxy", build_captcha_run_payload_exe({**ctx, "proxy": None})))
        if _env_bool("CAPTCHA_RUN_PRESS_FULL"):
            variants.append(("press-full", build_captcha_run_payload(ctx, phase=phase, include_proxy=True)))
        legacy = build_captcha_run_payload_legacy(ctx)
        # legacy 也可能带 proxy 字段；保留作兜底，但仍优先有代理的 exe-exact
        variants.append(("legacy", legacy))
    # 去重
    seen: set[str] = set()
    unique: list[tuple[str, dict[str, Any]]] = []
    for name, body in variants:
        key = str(sorted(body.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append((name, body))
    return unique


def _log_captcha_run_poll(result: dict[str, Any], *, mode: str) -> None:
    if not _env_bool("CAPTCHA_RUN_DEBUG"):
        return
    resp = result.get("response") if isinstance(result.get("response"), dict) else {}
    logger.info(
        "captcha.run poll mode=%s status=%s press=%s silent=%s reqPress=%s reqSilent=%s",
        mode,
        result.get("status"),
        bool(resp.get("pressToken")),
        bool(resp.get("silentToken")),
        resp.get("requestedPress"),
        resp.get("requestedSilent"),
    )


def _extract_captcha_run_px(result: dict[str, Any], *, mode: str) -> Optional[dict[str, str]]:
    """captcha.run 在 status=Working 时也可能已写入 response.silentToken/pressToken。"""
    resp = result.get("response") if isinstance(result.get("response"), dict) else {}
    status = (result.get("status") or "").lower()

    if mode == "press":
        if resp.get("pressToken"):
            px = normalize_px_solution({"response": {"pressToken": resp["pressToken"]}})
            if px.get("px3"):
                logger.info("captcha.run 使用 pressToken")
                return px
        if status in ("working", "pending", "processing", "queued", ""):
            return None
        if status in ("success", "fail", "failed", "error"):
            return None
        return None

    if not captcha_run_has_token(result):
        return None
    px = normalize_px_solution(result)
    return px if px.get("px3") else None


def _captcha_run_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_env('CAPTCHA_RUN_API_KEY')}",
        "Content-Type": "application/json",
    }


def create_captcha_run_task(ctx: dict[str, Any]) -> Optional[CaptchaRunTask]:
    """官方流程：POST /v2/tasks/ 一次；后续 GET ?captchaType=silent|press 共用 taskId。

    [dola-pool patch 2026-10-10] captcha.run 的 PxCaptcha2 端点实测有 ~30% 概率
    返回 403「验证码类型 PxCaptcha2 维护中」或 30s 读超时（瞬时限流/抖动）。
    原版每个 base 只试一次（两个 base 共 2 次）→ 约一半概率直接判死，
    表现为「纯协议打码失败 phase=silent」。
    这里改为多轮重试（CAPTCHA_RUN_CREATE_RETRIES，默认 5 轮，每轮两个 base）。
    注：重新 vendor 上游代码时务必保留本补丁。
    """
    if not _env("CAPTCHA_RUN_API_KEY"):
        return None

    payload = build_captcha_run_payload_official(ctx)
    headers = _captcha_run_headers()
    variant = "official-proxy"

    if _env_bool("CAPTCHA_RUN_DEBUG"):
        import json
        logger.info("captcha.run 建 task (%s): %s", variant, json.dumps(payload, ensure_ascii=False))

    try:
        rounds = max(1, int((_env("CAPTCHA_RUN_CREATE_RETRIES") or "5").strip() or "5"))
    except Exception:  # noqa: BLE001
        rounds = 5

    last = ""
    for rnd in range(1, rounds + 1):
        for base in (_captcha_run_base(), CAPTCHA_RUN_API_BASE_GLOBAL):
            try:
                create = requests.post(
                    f"{base}/v2/tasks/",
                    json=payload,
                    headers=headers,
                    timeout=30,
                )
                if create.status_code >= 400:
                    last = "HTTP %s %s" % (create.status_code, create.text[:120])
                    logger.warning(
                        "captcha.run 建 task 失败 %s status=%s body=%s（第 %s/%s 轮）",
                        base, create.status_code, create.text[:200], rnd, rounds,
                    )
                    continue
                data = create.json()
                task_id = data.get("taskId") or data.get("id") or data.get("task_id")
                if not task_id:
                    last = "无 taskId: %s" % data
                    logger.warning("captcha.run 建 task 无 taskId: %s", data)
                    continue
                logger.info("captcha.run 已建 task %s base=%s（第 %s 轮）", task_id, base, rnd)
                return CaptchaRunTask(task_id=task_id, base=base, variant=variant)
            except Exception as exc:  # noqa: BLE001
                last = repr(exc)[:160]
                logger.error("captcha.run 建 task 异常(%s): %s", base, exc)
        if rnd < rounds:
            time.sleep(2.0)
    logger.error("captcha.run 建 task 全部失败（%s 轮）: %s", rounds, last)
    return None


def poll_captcha_run_token(
    task: CaptchaRunTask,
    mode: Literal["silent", "press"],
    *,
    max_wait: Optional[int] = None,
    warmup_silent_before_press: bool = False,
) -> Optional[dict[str, str]]:
    """GET /v2/tasks/{id}?captchaType=silent|press；不新建 task。"""
    if mode == "press":
        default_wait = int(_env("CAPTCHA_RUN_PRESS_WAIT", "180") or 180)
    else:
        default_wait = int(_env("CAPTCHA_RUN_SILENT_WAIT", "90") or 90)
    max_wait = max_wait or default_wait

    headers = _captcha_run_headers()
    poll_interval = 2.0 if mode == "silent" else 3.0

    if mode == "press" and warmup_silent_before_press and not task.silent_fetched:
        poll_captcha_run_token(task, "silent", max_wait=min(max_wait, 90))

    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(poll_interval)
        try:
            poll = requests.get(
                f"{task.base}/v2/tasks/{task.task_id}?captchaType={mode}",
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            logger.debug("captcha.run 轮询异常 mode=%s: %s", mode, exc)
            continue
        if poll.status_code == 404:
            logger.debug("captcha.run press 轮询 404，任务可能仍在排队 task=%s", task.task_id)
            continue
        if poll.status_code >= 400:
            time.sleep(2)
            continue
        result = poll.json()
        _log_captcha_run_poll(result, mode=mode)
        px = _extract_captcha_run_px(result, mode=mode)
        if px:
            if mode == "silent":
                task.silent_fetched = True
            logger.info(
                "captcha.run task=%s mode=%s variant=%s px3=%s",
                task.task_id, mode, task.variant, bool(px.get("px3")),
            )
            return px
        status = (result.get("status") or "").lower()
        if status in ("fail", "failed", "error"):
            resp = result.get("response") if isinstance(result.get("response"), dict) else {}
            reason = result.get("reason") or resp.get("reason") or "?"
            ip = result.get("ip") or resp.get("ip") or "?"
            req_silent = resp.get("requestedSilent")
            req_press = resp.get("requestedPress")
            logger.error(
                "captcha.run task=%s mode=%s 失败 status=%s reason=%s ip=%s "
                "reqSilent=%s reqPress=%s",
                task.task_id, mode, result.get("status"), reason, ip,
                req_silent, req_press,
            )
            # silent 都解不出 + reqPress=false + 快速失败 ≈ 代理连不通微软 PX
            if reason == "UNKNOWN" and not resp.get("silentToken"):
                logger.error(
                    "captcha.run 连 silent 都未解出：极可能是会话代理失效/被封，"
                    "worker 无法通过该代理连微软 PX。请更换有效代理。",
                )
            return None
    logger.warning("captcha.run task=%s mode=%s 超时", task.task_id, mode)
    return None


def solve_perimeterx_captcha_run(
    ctx: dict[str, Any],
    *,
    mode: Literal["press", "silent"] = "press",
    max_wait: int = 120,
    task: Optional[CaptchaRunTask] = None,
) -> Optional[dict[str, str]]:
    """
    captcha.run PxCaptcha2 官方流程：
      POST /v2/tasks/ 一次
      GET  ?captchaType=silent  → verify#1
      GET  ?captchaType=press   → verify#2（须先完成 silent）
    """
    if not _env("CAPTCHA_RUN_API_KEY"):
        return None

    if mode == "press":
        max_wait = max(max_wait, int(_env("CAPTCHA_RUN_PRESS_WAIT", "180") or 180))
    else:
        max_wait = max(max_wait, int(_env("CAPTCHA_RUN_SILENT_WAIT", "90") or 90))

    if task is None:
        task = create_captcha_run_task(ctx)
    if not task:
        return None

    if mode == "silent":
        return poll_captcha_run_token(task, "silent", max_wait=max_wait)

    if not task.silent_fetched:
        poll_captcha_run_token(task, "silent", max_wait=min(max_wait, 90))
    return poll_captcha_run_token(task, "press", max_wait=max_wait, warmup_silent_before_press=False)


def solve_perimeterx_ezcaptcha(ctx: dict[str, Any], max_wait: int = 120) -> Optional[dict[str, str]]:
    if not _env("EZCAPTCHA_API_KEY"):
        return None
    try:
        task: dict[str, Any] = {
            "type": "PerimeterX",
            "websiteURL": ctx.get("page_url", "https://signup.live.com/"),
            "websiteKey": ctx.get("app_id", PX_APP_ID),
        }
        task.update(_ezcaptcha_proxy_fields(ctx))
        if ctx.get("pxvid"):
            task["vid"] = ctx["pxvid"]
        if ctx.get("pxde"):
            task["pxde"] = ctx["pxde"]
        if ctx.get("pxcts"):
            task["pxcts"] = ctx["pxcts"]

        resp = requests.post(
            f"{_ezcaptcha_base()}/createTask",
            json={"clientKey": _env("EZCAPTCHA_API_KEY"), "task": task},
            timeout=30,
        )
        data = resp.json()
        if data.get("errorId", 1) != 0:
            logger.error("EzCaptcha 创建失败: %s", data.get("errorDescription", data))
            return None
        solution = _poll_task(base=_ezcaptcha_base(), key=_env("EZCAPTCHA_API_KEY"), task_id=data["taskId"], max_wait=max_wait)
        return normalize_px_solution(solution) if solution else None
    except Exception as exc:
        logger.error("EzCaptcha 异常: %s", exc)
        return None


def solve_perimeterx_ezcaptcha_invisible(ctx: dict[str, Any], max_wait: int = 90) -> Optional[dict[str, str]]:
    """EzCaptcha PxInvisibleCaptcha — 对应 exe silent 模式。"""
    if not _env("EZCAPTCHA_API_KEY"):
        return None
    try:
        task = {
            "type": "PxInvisibleCaptcha",
            "websiteURL": ctx.get("page_url", "https://signup.live.com/"),
            "websiteKey": ctx.get("app_id", PX_APP_ID),
        }
        task.update(_ezcaptcha_proxy_fields(ctx))
        resp = requests.post(
            f"{_ezcaptcha_base()}/createTask",
            json={"clientKey": _env("EZCAPTCHA_API_KEY"), "task": task},
            timeout=30,
        )
        data = resp.json()
        if data.get("errorId", 1) != 0:
            return None
        solution = _poll_task(base=_ezcaptcha_base(), key=_env("EZCAPTCHA_API_KEY"), task_id=data["taskId"], max_wait=max_wait)
        return normalize_px_solution(solution) if solution else None
    except Exception as exc:
        logger.error("EzCaptcha invisible 异常: %s", exc)
        return None


def solve_perimeterx_capsolver(ctx: dict[str, Any], max_wait: int = 120) -> Optional[dict[str, str]]:
    if not _env("CAPSOLVER_API_KEY"):
        return None
    try:
        task: dict[str, Any] = {
            "type": "AntiPerimeterXTaskProxyless",
            "websiteURL": ctx.get("page_url", "https://signup.live.com/"),
            "userAgent": ctx.get("user_agent", ""),
            "_pxvid": ctx.get("pxvid", ""),
            "_pxde": ctx.get("pxde", ""),
            "pxcts": ctx.get("pxcts", ""),
        }
        proxy_payload = proxy_for_capsolver(ctx.get("proxy"))
        if proxy_payload:
            task.update(proxy_payload)
            task["type"] = "AntiPerimeterXTask"

        resp = requests.post(
            "https://api.capsolver.com/createTask",
            json={"clientKey": _env("CAPSOLVER_API_KEY"), "task": task},
            timeout=30,
        )
        data = resp.json()
        if data.get("errorId", 1) != 0:
            logger.error("CapSolver 创建失败: %s", data.get("errorDescription"))
            return None
        solution = _poll_task(base="https://api.capsolver.com", key=_env("CAPSOLVER_API_KEY"), task_id=data["taskId"], max_wait=max_wait)
        return normalize_px_solution(solution) if solution else None
    except Exception as exc:
        logger.error("CapSolver 异常: %s", exc)
        return None


def solve_image_to_text(image_b64: str, *, module: str = "") -> str:
    """登录页「输入你看到的字符」文本图形验证码 → OCR 文本（CapSolver ImageToText）。

    MSA login 在风控/headless 时会插入 GetHIP 文本验证码，浏览器路径需 OCR 破解。
    """
    key = _env("CAPSOLVER_API_KEY")
    if not (key and image_b64):
        return ""
    try:
        task: dict[str, Any] = {"type": "ImageToTextTask", "body": image_b64}
        if module:
            task["module"] = module
        resp = requests.post(
            "https://api.capsolver.com/createTask",
            json={"clientKey": key, "task": task},
            timeout=30,
        )
        data = resp.json()
        if data.get("errorId", 1) != 0:
            logger.error("CapSolver ImageToText 失败: %s", data.get("errorDescription"))
            return ""
        # ImageToText 多为同步返回
        sol = data.get("solution") or {}
        text = sol.get("text", "")
        if text:
            return text.strip()
        task_id = data.get("taskId")
        if task_id:
            got = _poll_task(base="https://api.capsolver.com", key=key, task_id=task_id, max_wait=60)
            if isinstance(got, dict):
                return (got.get("text") or "").strip()
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.error("CapSolver ImageToText 异常: %s", exc)
        return ""


def solve_arkose_ezcaptcha(ctx: dict[str, Any], max_wait: int = 120) -> Optional[str]:
    """reg-factory 同款 EzCaptcha FunCaptcha。"""
    if not _env("EZCAPTCHA_API_KEY"):
        return None
    try:
        task: dict[str, Any] = {
            "type": "FunCaptchaTask",
            "websiteURL": ctx.get("page_url", "https://signup.live.com/"),
            "websitePublicKey": ARKOSE_PUBLIC_KEY,
        }
        task.update(_ezcaptcha_proxy_fields(ctx))
        resp = requests.post(
            f"{_ezcaptcha_base()}/createTask",
            json={"clientKey": _env("EZCAPTCHA_API_KEY"), "task": task},
            timeout=30,
        )
        data = resp.json()
        if data.get("errorId", 1) != 0:
            logger.error("EzCaptcha Arkose 创建失败: %s", data.get("errorDescription", data))
            return None
        solution = _poll_task(
            base=_ezcaptcha_base(),
            key=_env("EZCAPTCHA_API_KEY"),
            task_id=data["taskId"],
            max_wait=max_wait,
        )
        if solution:
            return solution.get("token", "")
        return None
    except Exception as exc:
        logger.error("EzCaptcha Arkose 异常: %s", exc)
        return None


def solve_arkose(ctx: dict[str, Any], max_wait: int = 120) -> Optional[str]:
    for name, fn in (
        ("CapSolver Arkose", lambda: solve_arkose_capsolver(ctx, max_wait=max_wait)),
        ("EzCaptcha Arkose", lambda: solve_arkose_ezcaptcha(ctx, max_wait=max_wait)),
    ):
        token = fn()
        if token:
            logger.info("%s 成功", name)
            return token
    return None


def solve_arkose_capsolver(ctx: dict[str, Any], max_wait: int = 120) -> Optional[str]:
    if not _env("CAPSOLVER_API_KEY"):
        return None
    try:
        task: dict[str, Any] = {
            "type": "FunCaptchaTaskProxyLess",
            "websiteURL": ctx.get("page_url", "https://signup.live.com/"),
            "websitePublicKey": ARKOSE_PUBLIC_KEY,
        }
        proxy_payload = proxy_for_capsolver(ctx.get("proxy"))
        if proxy_payload:
            task.update(proxy_payload)
            task["type"] = "FunCaptchaTask"

        resp = requests.post(
            "https://api.capsolver.com/createTask",
            json={"clientKey": _env("CAPSOLVER_API_KEY"), "task": task},
            timeout=30,
        )
        data = resp.json()
        if data.get("errorId", 1) != 0:
            return None
        solution = _poll_task(base="https://api.capsolver.com", key=_env("CAPSOLVER_API_KEY"), task_id=data["taskId"], max_wait=max_wait)
        if solution:
            return solution.get("token", "")
        return None
    except Exception as exc:
        logger.error("Arkose CapSolver 异常: %s", exc)
        return None


def solve_perimeterx(
    ctx: dict[str, Any],
    *,
    prefer_mode: Literal["press", "silent", "auto"] = "auto",
) -> Optional[dict[str, str]]:
    """
    按 exe 优先级解 PX（纯协议，不启动浏览器）。

    silent 阶段（verify #1 前）：
      captcha.run silent → EzCaptcha invisible → CapSolver

    press 阶段（verify #2 前）：
      captcha.run press → EzCaptcha PerimeterX → CapSolver
    """
    if prefer_mode == "silent":
        chain = [
            ("captcha.run silent", lambda: solve_perimeterx_captcha_run(ctx, mode="silent")),
            ("EzCaptcha invisible", lambda: solve_perimeterx_ezcaptcha_invisible(ctx)),
            ("CapSolver", lambda: solve_perimeterx_capsolver(ctx)),
        ]
    elif prefer_mode == "press":
        fallback = _env("PX_PRESS_FALLBACK").strip().lower()
        if fallback in {"ez", "ezcaptcha"}:
            chain = [
                ("EzCaptcha press (PX_PRESS_FALLBACK)", lambda: solve_perimeterx_ezcaptcha(ctx)),
                ("CapSolver", lambda: solve_perimeterx_capsolver(ctx)),
            ]
        elif fallback in {"capsolver", "cap"}:
            chain = [
                ("CapSolver (PX_PRESS_FALLBACK)", lambda: solve_perimeterx_capsolver(ctx)),
                ("EzCaptcha press", lambda: solve_perimeterx_ezcaptcha(ctx)),
            ]
        else:
            chain = [
                ("captcha.run press", lambda: solve_perimeterx_captcha_run(ctx, mode="press")),
                ("EzCaptcha press", lambda: solve_perimeterx_ezcaptcha(ctx)),
                ("CapSolver", lambda: solve_perimeterx_capsolver(ctx)),
            ]
    else:
        chain = [
            ("captcha.run press", lambda: solve_perimeterx_captcha_run(ctx, mode="press")),
            ("captcha.run silent", lambda: solve_perimeterx_captcha_run(ctx, mode="silent")),
            ("CapSolver", lambda: solve_perimeterx_capsolver(ctx)),
            ("EzCaptcha press", lambda: solve_perimeterx_ezcaptcha(ctx)),
            ("EzCaptcha invisible", lambda: solve_perimeterx_ezcaptcha_invisible(ctx)),
        ]

    for name, fn in chain:
        sol = fn()
        if sol and sol.get("px3"):
            logger.info("%s 成功", name)
            return sol
        logger.warning("%s 未返回 px3", name)

    return None


def get_captcha_run_balance() -> Optional[float]:
    if not _env("CAPTCHA_RUN_API_KEY"):
        return None
    try:
        resp = requests.get(
            f"{_captcha_run_base()}/v2/users/self/wallet",
            headers={"Authorization": f"Bearer {_env('CAPTCHA_RUN_API_KEY')}"},
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get("balance") or data.get("wallet", {}).get("balance")
    except Exception:
        return None
