"""offcaptcha.com PerimeterX 打码客户端（Microsoft Outlook 注册）。

文档：https://offcaptcha.com/docs
官方示例任务类型：
  - PXCaptchaInvisible     silent / 指纹（verify #1）
  - PXCaptchaPressAndHold  按住（verify #2）

鉴权：Authorization: Bearer <OFFCAPTCHA_API_KEY>
建任务：POST /v1/tasks/  （body 含 task + softID，默认 OFFCAPTCHA_SOFT_ID）
轮询：  GET  /v1/tasks/{task_id}  直到 status=ready
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import requests

from outlook_auto_register.config.constants import (
    LOGIN_MS_BASE,
    OFFCAPTCHA_API_BASE,
    OFFCAPTCHA_SOFT_ID,
    PX_APP_ID,
    RISK_VERIFY_PATH,
)
from outlook_auto_register.service.resource.proxy.proxy_utils import parse_proxy

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2.0
_INVISIBLE_TIMEOUT = 60
_PRESS_TIMEOUT = 150


def _db_setting(key: str) -> str:
    try:
        from outlook_auto_register.dao.outlook_dao import get_setting

        return get_setting(key, "")
    except Exception:  # noqa: BLE001
        return ""


def api_key() -> str:
    return (
        os.environ.get("OFFCAPTCHA_API_KEY")
        or os.environ.get("OFFCAPTCHA_KEY")
        or _db_setting("OFFCAPTCHA_API_KEY")
        or ""
    ).strip()


def api_base() -> str:
    return (os.environ.get("OFFCAPTCHA_API_BASE") or OFFCAPTCHA_API_BASE).rstrip("/")


def soft_id() -> str:
    return (
        os.environ.get("OFFCAPTCHA_SOFT_ID")
        or os.environ.get("OFFCAPTCHA_SOFTID")
        or OFFCAPTCHA_SOFT_ID
    ).strip()


def _headers() -> dict[str, str]:
    key = api_key()
    if not key:
        raise RuntimeError("未配置 OFFCAPTCHA_API_KEY（或 OFFCAPTCHA_KEY）")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _proxy_url(raw: Optional[str]) -> str:
    cfg = parse_proxy(raw)
    return cfg.url if cfg else (raw or "")


def _task_id_from(data: dict[str, Any]) -> str:
    for key in ("taskId", "task_id", "id"):
        val = data.get(key)
        if val:
            return str(val)
    task = data.get("task")
    if isinstance(task, dict):
        for key in ("id", "taskId"):
            if task.get(key):
                return str(task[key])
    return ""


def _solution_from(data: dict[str, Any]) -> dict[str, Any]:
    sol = data.get("solution")
    if isinstance(sol, dict):
        return sol
    result = data.get("result")
    if isinstance(result, dict):
        inner = result.get("solution")
        return inner if isinstance(inner, dict) else result
    return {}


def _status_of(data: dict[str, Any]) -> str:
    return str(data.get("status") or data.get("state") or "").strip().lower()


def _cookie_values(raw: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    if isinstance(raw, dict):
        for name, value in raw.items():
            if name and value is not None:
                values[str(name)] = str(value)
        return values
    if not isinstance(raw, list):
        return values
    for item in raw:
        record: Any = item
        if isinstance(item, str):
            try:
                record = json.loads(item)
            except json.JSONDecodeError:
                continue
        if isinstance(record, dict) and record.get("name") and record.get("value") is not None:
            values[str(record["name"])] = str(record["value"])
    return values


def _px_from_solution(sol: dict[str, Any]) -> dict[str, str]:
    cookies = _cookie_values(sol.get("cookies"))
    px3 = str(sol.get("px3") or sol.get("_px3") or cookies.get("_px3") or "")
    pxde = str(sol.get("pxde") or sol.get("_pxde") or cookies.get("_pxde") or "")
    pxvid = str(
        sol.get("pxvid")
        or sol.get("_pxvid")
        or sol.get("vid")
        or cookies.get("_pxvid")
        or ""
    )
    pxcts = str(sol.get("pxcts") or cookies.get("pxcts") or "")
    return {"px3": px3, "pxde": pxde, "pxvid": pxvid, "pxcts": pxcts}


def build_invisible_payload(
    *,
    website_url: str,
    session_id: str,
    fpt_url: str,
    proxy: Optional[str],
    website_key: str = PX_APP_ID,
    user_agent: str = "",
) -> dict[str, Any]:
    """对齐官方 PXCaptchaInvisible：PXTask 字段 + data{sessionId,fptURL,iframeURL}。"""
    task: dict[str, Any] = {
        "type": "PXCaptchaInvisible",
        "websiteURL": website_url,
        "websiteKey": website_key,
        "data": {
            "sessionId": session_id,
            "fptURL": fpt_url,
            "iframeURL": fpt_url,
            "keepSession": True,
        },
    }
    proxy_url = _proxy_url(proxy)
    if proxy_url:
        task["proxy"] = proxy_url
    if user_agent:
        task["userAgent"] = user_agent
    return {"task": task}


def build_press_payload(
    *,
    website_url: str,
    session_id: str,
    target_url: str,
    uuid: str,
    vid: str,
    proxy: Optional[str],
    website_key: str = PX_APP_ID,
    user_agent: str = "",
    iframe_url: str = "",
) -> dict[str, Any]:
    """对齐官方 PXCaptchaPressAndHold：PXTask 字段 + data{sessionId,targetURL,uuid,vid}。"""
    data: dict[str, Any] = {
        "sessionId": session_id,
        "targetURL": target_url,
        "uuid": uuid,
        "vid": vid,
    }
    if iframe_url:
        data["iframeURL"] = iframe_url
    task: dict[str, Any] = {
        "type": "PXCaptchaPressAndHold",
        "websiteURL": website_url,
        "websiteKey": website_key,
        "data": data,
    }
    proxy_url = _proxy_url(proxy)
    if proxy_url:
        task["proxy"] = proxy_url
    if user_agent:
        task["userAgent"] = user_agent
    return {"task": task}


def create_task(payload: dict[str, Any]) -> str:
    body = dict(payload)
    sid = soft_id()
    if sid and "softID" not in body and "softId" not in body:
        body["softID"] = sid
    url = f"{api_base()}/tasks/"
    ttype = (body.get("task") or {}).get("type", "?")
    logger.info("offcaptcha 建任务 type=%s url=%s softID=%s", ttype, url, sid or "-")
    resp = requests.post(url, headers=_headers(), json=body, timeout=45)
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:400]}
    if resp.status_code >= 400:
        raise RuntimeError(
            f"offcaptcha 建任务失败 HTTP {resp.status_code}: {data}"
        )
    task_id = _task_id_from(data)
    if not task_id:
        raise RuntimeError(f"offcaptcha 建任务无 taskId: {data}")
    logger.info("offcaptcha 已建任务 %s type=%s", task_id, ttype)
    return task_id


def poll_task(task_id: str, *, timeout_s: int) -> dict[str, Any]:
    url = f"{api_base()}/tasks/{task_id}"
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        resp = requests.get(url, headers=_headers(), timeout=30)
        try:
            last = resp.json()
        except ValueError:
            last = {"raw": resp.text[:400], "http": resp.status_code}
        status = _status_of(last)
        logger.info("offcaptcha 轮询 task=%s status=%s http=%s", task_id, status or "?", resp.status_code)
        err_id = last.get("errorId")
        if err_id not in (None, 0, "0"):
            raise RuntimeError(
                f"offcaptcha 任务失败 task={task_id} errorId={err_id} "
                f"{last.get('errorCode')} {last.get('errorDescription')}"
            )
        if status in {"ready", "completed", "success", "solved"}:
            return last
        if status in {"failed", "error", "unsolvable"}:
            raise RuntimeError(f"offcaptcha 任务失败 task={task_id}: {last}")
        if resp.status_code >= 400 and status not in {"processing", "working", "pending", "idle"}:
            raise RuntimeError(f"offcaptcha 轮询 HTTP {resp.status_code}: {last}")
        time.sleep(_POLL_INTERVAL)
    raise RuntimeError(f"offcaptcha 轮询超时 task={task_id} last={last}")


def solve_invisible(
    *,
    website_url: str,
    session_id: str,
    fpt_url: str,
    proxy: Optional[str],
    website_key: str = PX_APP_ID,
    user_agent: str = "",
) -> dict[str, Any]:
    payload = build_invisible_payload(
        website_url=website_url,
        session_id=session_id,
        fpt_url=fpt_url,
        proxy=proxy,
        website_key=website_key,
        user_agent=user_agent,
    )
    task_id = create_task(payload)
    raw = poll_task(task_id, timeout_s=_INVISIBLE_TIMEOUT)
    sol = _solution_from(raw)
    px = _px_from_solution(sol)
    if not px.get("px3"):
        raise RuntimeError(f"offcaptcha invisible 无 px3: {raw}")
    px["cookies"] = sol.get("cookies") or {}
    px["userAgent"] = str(sol.get("userAgent") or sol.get("user_agent") or "")
    px["raw_status"] = _status_of(raw)
    return px


def solve_press(
    *,
    website_url: str,
    session_id: str,
    target_url: str,
    uuid: str,
    vid: str,
    proxy: Optional[str],
    website_key: str = PX_APP_ID,
    user_agent: str = "",
    iframe_url: str = "",
) -> dict[str, str]:
    payload = build_press_payload(
        website_url=website_url,
        session_id=session_id,
        target_url=target_url,
        uuid=uuid,
        vid=vid,
        proxy=proxy,
        website_key=website_key,
        user_agent=user_agent,
        iframe_url=iframe_url,
    )
    task_id = create_task(payload)
    raw = poll_task(task_id, timeout_s=_PRESS_TIMEOUT)
    px = _px_from_solution(_solution_from(raw))
    if not px.get("px3"):
        raise RuntimeError(f"offcaptcha press 无 px3: {raw}")
    return px


def ping() -> dict[str, Any]:
    """探测 key 是否可用（余额接口因文档版本可能 404，仍返回 HTTP 细节）。"""
    key = api_key()
    if not key:
        return {"ok": False, "error": "missing_key"}
    headers = _headers()
    out: dict[str, Any] = {"ok": False, "base": api_base()}
    for path in ("/balance", "/users/me", "/tasks/"):
        url = f"{api_base()}{path}"
        try:
            if path == "/tasks/":
                resp = requests.get(url, headers=headers, timeout=20)
            else:
                resp = requests.get(url, headers=headers, timeout=20)
        except requests.RequestException as exc:
            out["error"] = str(exc)
            continue
        body = ""
        try:
            parsed = resp.json()
            body = parsed
        except ValueError:
            body = resp.text[:200]
        out[path] = {"http": resp.status_code, "body": body}
        if resp.status_code < 500:
            out["ok"] = resp.status_code != 401
    return out


def default_press_target() -> str:
    return f"{LOGIN_MS_BASE}{RISK_VERIFY_PATH}"
