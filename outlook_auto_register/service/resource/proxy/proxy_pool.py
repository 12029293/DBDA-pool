"""代理池：SQLite 持久化（库文件与账号共用 outlook.db）。"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from outlook_auto_register.dao import outlook_dao as db
from outlook_auto_register.service.resource.proxy.proxy_utils import expand_proxy_template, has_sid_template, infer_country_from_template, preflight_proxy, random_sid

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_rr_lock = threading.Lock()
_rr_cursor = 0

DEFAULT_SETTINGS: dict[str, Any] = {
    "strategy": "round_robin",  # round_robin | least_used | random
    "require_healthy": False,
    "sticky_per_account": True,
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def pool_file() -> Path:
    return db.db_path()


def storage_backend() -> str:
    return db.storage_backend()


def _with_db() -> sqlite3.Connection:
    db.ensure_initialized()
    conn = db.connect()
    for key, val in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO proxy_settings(key, value) VALUES (?, ?)",
            (key, json.dumps(val)),
        )
    conn.commit()
    return conn


def _get_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM proxy_settings"):
        try:
            settings[row["key"]] = json.loads(row["value"])
        except Exception:  # noqa: BLE001
            settings[row["key"]] = row["value"]
    return settings


def _set_settings(conn: sqlite3.Connection, settings: dict[str, Any]) -> None:
    for key, val in settings.items():
        conn.execute(
            "INSERT INTO proxy_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(val)),
        )


def _row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "label": row["label"],
        "provider": row["provider"] or "",
        "country": row["country"] or "",
        "template": row["template"],
        "enabled": bool(row["enabled"]),
        "status": row["status"] or "unknown",
        "last_check_at": row["last_check_at"] or "",
        "last_check_msg": row["last_check_msg"] or "",
        "exit_ip": row["exit_ip"] or "",
        "stats": {
            "assigned": int(row["assigned_count"] or 0),
            "success": int(row["success_count"] or 0),
            "fail": int(row["fail_count"] or 0),
            "checks": int(row["check_count"] or 0),
        },
        "created_at": row["created_at"] or "",
        "sort_order": int(row["sort_order"] or 0),
    }


def ensure_templates(
    templates: list[str],
    *,
    text: Optional[str] = None,
    provider: str = "web",
    country: str = "",
) -> dict[str, Any]:
    """将代理模板写入 SQLite（已存在相同 template 则跳过）。"""
    lines = list(templates or [])
    if text:
        for line in text.replace("\r", "").split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    lines = [t.strip() for t in lines if (t or "").strip()]
    if not lines:
        return {"added": 0, "existing": 0, "ids": []}
    provider = (provider or "web").strip() or "web"
    default_country = (country or "").strip().upper()[:6]
    added_ids: list[str] = []
    existing_n = 0
    with _lock:
        conn = _with_db()
        try:
            known = {r["template"] for r in conn.execute("SELECT template FROM proxies")}
            to_add = [t for t in lines if t not in known]
            existing_n = len(lines) - len(to_add)
            if to_add:
                max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) AS m FROM proxies").fetchone()["m"]
                for i, template in enumerate(to_add):
                    cc = default_country or infer_country_from_template(template)
                    pid = uuid.uuid4().hex[:12]
                    conn.execute(
                        """INSERT INTO proxies(
                            id, label, provider, country, template, enabled, status, last_check_at, last_check_msg,
                            exit_ip, assigned_count, success_count, fail_count, check_count, created_at, sort_order
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            pid, _auto_label(template), provider, cc, template, 1, "unknown",
                            "", "", "", 0, 0, 0, 0, _now_iso(), int(max_order) + 1 + i,
                        ),
                    )
                    added_ids.append(pid)
                conn.commit()
        finally:
            conn.close()
    return {"added": len(added_ids), "existing": existing_n, "ids": added_ids}


def load_store() -> dict[str, Any]:
    """兼容旧调用：组装 dict 视图（大量代理时优先用 SQL 直查 API）。"""
    with _lock:
        conn = _with_db()
        try:
            settings = _get_settings(conn)
            proxies = [
                _row_to_entry(r)
                for r in conn.execute("SELECT * FROM proxies ORDER BY sort_order, created_at")
            ]
            bindings: dict[str, Any] = {}
            for r in conn.execute("SELECT * FROM proxy_bindings"):
                bindings[r["email"]] = {
                    "proxy_id": r["proxy_id"],
                    "resolved": r["resolved"],
                    "assigned_at": r["assigned_at"],
                    "purpose": r["purpose"],
                }
            return {"version": 2, "settings": settings, "proxies": proxies, "bindings": bindings}
        finally:
            conn.close()


def save_store(store: dict[str, Any]) -> None:
    """兼容旧调用：整包写回（Web 管理请走增删改 API，勿依赖此函数）。"""
    with _lock:
        conn = _with_db()
        try:
            _set_settings(conn, store.get("settings") or {})
            conn.execute("DELETE FROM proxies")
            conn.execute("DELETE FROM proxy_bindings")
            for i, p in enumerate(store.get("proxies") or []):
                stats = p.get("stats") or {}
                conn.execute(
                    """INSERT INTO proxies(
                        id, label, provider, country, template, enabled, status, last_check_at, last_check_msg,
                        exit_ip, assigned_count, success_count, fail_count, check_count, created_at, sort_order
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        p.get("id") or uuid.uuid4().hex[:12],
                        p.get("label") or "",
                        p.get("provider") or "",
                        p.get("country") or "",
                        p.get("template") or "",
                        1 if p.get("enabled", True) else 0,
                        p.get("status") or "unknown",
                        p.get("last_check_at") or "",
                        p.get("last_check_msg") or "",
                        p.get("exit_ip") or "",
                        int(stats.get("assigned") or 0),
                        int(stats.get("success") or 0),
                        int(stats.get("fail") or 0),
                        int(stats.get("checks") or 0),
                        p.get("created_at") or _now_iso(),
                        i,
                    ),
                )
            for email, b in (store.get("bindings") or {}).items():
                conn.execute(
                    """INSERT OR REPLACE INTO proxy_bindings(email, proxy_id, resolved, assigned_at, purpose)
                       VALUES (?,?,?,?,?)""",
                    (
                        email.strip().lower(),
                        b.get("proxy_id") or "",
                        b.get("resolved") or "",
                        b.get("assigned_at") or _now_iso(),
                        b.get("purpose") or "register",
                    ),
                )
            conn.commit()
        finally:
            conn.close()


def _auto_label(template: str) -> str:
    m = re.match(r"^([^:]+):(\d+)", template)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    if "://" in template:
        from urllib.parse import urlparse

        u = urlparse(template)
        return f"{u.hostname or 'proxy'}:{u.port or 80}"
    return template[:24] or "proxy"


def mask_template(template: str) -> str:
    t = (template or "").strip()
    if not t:
        return ""
    m = re.match(r"^([^:]+):(\d+):([^:]+):(.+)$", t)
    if m and "://" not in t:
        host, port, user, _pwd = m.groups()
        return f"{host}:{port}:{user}:***"
    if "@" in t and "://" in t:
        from urllib.parse import urlparse

        u = urlparse(t)
        user = u.username or ""
        host = u.hostname or ""
        port = u.port or ""
        auth = f"{user}:***@" if user else ""
        return f"{u.scheme}://{auth}{host}:{port}"
    return t[:48] + ("…" if len(t) > 48 else "")


def entry_by_id(store: dict[str, Any], proxy_id: str) -> Optional[dict[str, Any]]:
    with _lock:
        conn = _with_db()
        try:
            row = conn.execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()
            return _row_to_entry(row) if row else None
        finally:
            conn.close()


def list_providers() -> list[dict[str, Any]]:
    with _lock:
        conn = _with_db()
        try:
            rows = conn.execute(
                """SELECT provider AS name, COUNT(*) AS total,
                          SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled,
                          SUM(CASE WHEN status='ok' AND enabled=1 THEN 1 ELSE 0 END) AS ok
                   FROM proxies
                   WHERE provider != ''
                   GROUP BY provider
                   ORDER BY provider"""
            ).fetchall()
            return [{"name": r["name"], "total": r["total"], "enabled": r["enabled"], "ok": r["ok"]} for r in rows]
        finally:
            conn.close()


def list_entries(
    store: Optional[dict[str, Any]] = None,
    *,
    for_api: bool = False,
    provider: Optional[str] = None,
    limit: int = 5000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    del store  # SQLite 直查，忽略内存 store
    limit = max(1, min(int(limit), 20000))
    offset = max(0, int(offset))
    with _lock:
        conn = _with_db()
        try:
            sql = "SELECT * FROM proxies"
            params: list[Any] = []
            if provider:
                sql += " WHERE provider=?"
                params.append(provider)
            sql += " ORDER BY provider, sort_order, created_at LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            out: list[dict[str, Any]] = []
            for row in conn.execute(sql, params):
                ent = _row_to_entry(row)
                ent["has_sid"] = has_sid_template(ent.get("template"))
                ent["template_masked"] = mask_template(ent.get("template") or "")
                if for_api:
                    ent.pop("template", None)
                out.append(ent)
            return out
        finally:
            conn.close()


def pool_stats(store: Optional[dict[str, Any]] = None, *, provider: Optional[str] = None) -> dict[str, int]:
    del store
    with _lock:
        conn = _with_db()
        try:
            where = ""
            params: list[Any] = []
            if provider:
                where = " WHERE provider=?"
                params.append(provider)
            total = conn.execute(f"SELECT COUNT(*) AS c FROM proxies{where}", params).fetchone()["c"]
            enabled = conn.execute(
                f"SELECT COUNT(*) AS c FROM proxies{where}{' AND' if where else ' WHERE'} enabled=1",
                params,
            ).fetchone()["c"]
            ok = conn.execute(
                f"SELECT COUNT(*) AS c FROM proxies{where}{' AND' if where else ' WHERE'} enabled=1 AND status='ok'",
                params,
            ).fetchone()["c"]
            dead = conn.execute(
                f"SELECT COUNT(*) AS c FROM proxies{where}{' AND' if where else ' WHERE'} enabled=1 AND status='dead'",
                params,
            ).fetchone()["c"]
            unknown = max(0, int(enabled) - int(ok) - int(dead))
            bindings = conn.execute("SELECT COUNT(*) AS c FROM proxy_bindings").fetchone()["c"]
            providers = conn.execute(
                "SELECT COUNT(DISTINCT provider) AS c FROM proxies WHERE provider != ''"
            ).fetchone()["c"]
            return {
                "total": int(total),
                "enabled": int(enabled),
                "ok": int(ok),
                "dead": int(dead),
                "unknown": int(unknown),
                "bindings": int(bindings),
                "providers": int(providers),
            }
        finally:
            conn.close()


def _eligible_entries(conn: sqlite3.Connection, settings: dict[str, Any], *, provider: Optional[str] = None) -> list[dict[str, Any]]:
    require_ok = bool(settings.get("require_healthy"))
    sql = "SELECT * FROM proxies WHERE enabled=1"
    params: list[Any] = []
    if require_ok:
        sql += " AND status='ok'"
    else:
        sql += " AND status != 'dead'"
    if provider:
        sql += " AND provider=?"
        params.append(provider)
    sql += " ORDER BY sort_order, created_at"
    return [_row_to_entry(r) for r in conn.execute(sql, params)]


def resolve_template(template: str) -> str:
    template = (template or "").strip()
    if not template:
        return ""
    if "{sid}" in template:
        return template.replace("{sid}", random_sid())
    return template


def _pick_entry(
    conn: sqlite3.Connection,
    settings: dict[str, Any],
    *,
    index: int = 0,
    provider: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    global _rr_cursor
    pool = _eligible_entries(conn, settings, provider=provider)
    if not pool:
        return None
    strategy = settings.get("strategy") or "round_robin"
    if strategy == "random":
        return random.choice(pool)
    if strategy == "least_used":
        return min(pool, key=lambda p: int((p.get("stats") or {}).get("assigned") or 0))
    with _rr_lock:
        idx = (_rr_cursor + index) % len(pool)
        if index == 0:
            _rr_cursor = (_rr_cursor + 1) % len(pool)
    return pool[idx]


def _bump_assigned(conn: sqlite3.Connection, proxy_id: str) -> None:
    conn.execute(
        "UPDATE proxies SET assigned_count = assigned_count + 1 WHERE id=?",
        (proxy_id,),
    )


def plan_for_batch(
    count: int,
    *,
    emails: Optional[list[str]] = None,
    provider: Optional[str] = None,
) -> tuple[list[str], dict[str, Any]]:
    count = max(1, int(count))
    plan: list[str] = []
    assignments: list[dict[str, Any]] = []
    settings: dict[str, Any] = dict(DEFAULT_SETTINGS)

    with _lock:
        conn = _with_db()
        try:
            settings = _get_settings(conn)
            sticky = bool(settings.get("sticky_per_account", True))

            for i in range(count):
                email = (emails[i] if emails and i < len(emails) else "") or ""
                email = email.strip().lower()
                resolved = ""
                proxy_id = ""
                entry = None
                template = ""

                if sticky and email:
                    row = conn.execute(
                        "SELECT * FROM proxy_bindings WHERE email=?", (email,)
                    ).fetchone()
                    if row and row["resolved"]:
                        resolved = row["resolved"]
                        proxy_id = row["proxy_id"]

                if not resolved:
                    entry = _pick_entry(conn, settings, index=i, provider=provider)
                    if not entry:
                        break
                    proxy_id = entry["id"]
                    template = entry.get("template") or ""
                    resolved = resolve_template(template)
                    _bump_assigned(conn, proxy_id)
                elif proxy_id:
                    prow = conn.execute(
                        "SELECT template FROM proxies WHERE id=?", (proxy_id,)
                    ).fetchone()
                    template = (prow["template"] if prow else "") or ""

                plan.append(resolved)
                assignments.append({
                    "index": i,
                    "email": email,
                    "proxy_id": proxy_id,
                    "resolved": resolved,
                    "template": template,
                })

            if assignments and any(a.get("proxy_id") for a in assignments):
                conn.commit()
        finally:
            conn.close()

    return plan, {
        "source": "pool" if plan else "empty",
        "strategy": settings.get("strategy") if plan else None,
        "assignments": assignments,
        "planned": len(plan),
        "requested": count,
    }


def resolve_for_email(
    email: str,
    *,
    fallback: str = "",
    provider: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    email = (email or "").strip().lower()
    meta: dict[str, Any] = {"email": email, "source": "pool"}

    with _lock:
        conn = _with_db()
        try:
            settings = _get_settings(conn)
            sticky = bool(settings.get("sticky_per_account", True))

            if sticky and email:
                row = conn.execute(
                    "SELECT * FROM proxy_bindings WHERE email=?", (email,)
                ).fetchone()
                if row and row["resolved"]:
                    meta.update({"proxy_id": row["proxy_id"], "source": "binding"})
                    return row["resolved"], meta

            entry = _pick_entry(conn, settings, provider=provider)
            if entry:
                resolved = resolve_template(entry.get("template") or "")
                if email:
                    conn.execute(
                        """INSERT OR REPLACE INTO proxy_bindings(email, proxy_id, resolved, assigned_at, purpose)
                           VALUES (?,?,?,?,?)""",
                        (email, entry["id"], resolved, _now_iso(), "rescue"),
                    )
                _bump_assigned(conn, entry["id"])
                conn.commit()
                meta.update({"proxy_id": entry["id"], "source": "pool"})
                return resolved, meta
        finally:
            conn.close()

    fb = (fallback or "").strip()
    if fb:
        expanded = expand_proxy_template(fb, count=1)
        resolved = expanded[0] if expanded else fb
        meta["source"] = "fallback"
        return resolved, meta
    return "", meta


def bind_account(
    email: str,
    proxy_id: str,
    resolved: str,
    *,
    purpose: str = "register",
    store: Optional[dict[str, Any]] = None,
) -> None:
    del store
    email = (email or "").strip().lower()
    if not email or not resolved:
        return
    with _lock:
        conn = _with_db()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO proxy_bindings(email, proxy_id, resolved, assigned_at, purpose)
                   VALUES (?,?,?,?,?)""",
                (email, proxy_id, resolved, _now_iso(), purpose),
            )
            conn.commit()
        finally:
            conn.close()


def unbind_accounts(emails: list[str]) -> int:
    emails = [e.strip().lower() for e in emails if (e or "").strip()]
    if not emails:
        return 0
    with _lock:
        conn = _with_db()
        try:
            n = 0
            for e in emails:
                cur = conn.execute("DELETE FROM proxy_bindings WHERE email=?", (e,))
                n += cur.rowcount
            conn.commit()
            return n
        finally:
            conn.close()


def record_result(
    proxy_id: str,
    *,
    success: bool,
    reg_country: str = "",
    purpose: str = "register",
    email: str = "",
    error: str = "",
) -> None:
    if not proxy_id:
        return
    col = "success_count" if success else "fail_count"
    reg_cc = (reg_country or "").strip().upper()[:6]
    purpose = (purpose or "register").strip() or "register"
    email = (email or "").strip().lower()
    err = (error or "").strip()[:500]
    with _lock:
        conn = _with_db()
        try:
            row = conn.execute(
                "SELECT provider, country FROM proxies WHERE id=?", (proxy_id,)
            ).fetchone()
            provider = (row["provider"] or "") if row else ""
            country = (row["country"] or "") if row else ""
            conn.execute(f"UPDATE proxies SET {col} = {col} + 1 WHERE id=?", (proxy_id,))
            conn.execute(
                """INSERT INTO proxy_events(
                    proxy_id, provider, country, reg_country, purpose, success, email, error, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    proxy_id,
                    provider,
                    country,
                    reg_cc,
                    purpose,
                    1 if success else 0,
                    email,
                    err,
                    _now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def check_entry(entry: dict[str, Any], *, timeout: int = 15, conn: Optional[sqlite3.Connection] = None) -> dict[str, Any]:
    template = (entry.get("template") or "").strip()
    probe = resolve_template(template) if "{sid}" in template else template
    ok, msg = preflight_proxy(probe or None, timeout=timeout)
    status = "ok" if ok else "dead"
    exit_ip = entry.get("exit_ip") or ""
    if ok and "出口=" in msg:
        ip_part = msg.split("出口=", 1)[1].split(" ", 1)[0].strip()
        if ip_part and ip_part != "ok":
            exit_ip = ip_part
    checked_at = _now_iso()
    own_conn = conn is None
    if own_conn:
        conn = _with_db()
    try:
        conn.execute(
            """UPDATE proxies SET status=?, last_check_at=?, last_check_msg=?, exit_ip=?,
               check_count = check_count + 1 WHERE id=?""",
            (status, checked_at, msg, exit_ip, entry.get("id")),
        )
        if own_conn:
            conn.commit()
    finally:
        if own_conn and conn:
            conn.close()
    return {
        "id": entry.get("id"),
        "ok": ok,
        "status": status,
        "message": msg,
        "exit_ip": exit_ip,
    }


def add_proxies(
    templates: list[str],
    *,
    label: str = "",
    provider: str = "",
    country: str = "",
    _conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    templates = [t.strip() for t in templates if (t or "").strip()]
    if not templates:
        return []
    provider = (provider or "").strip()
    default_country = (country or "").strip().upper()[:6]
    own = _conn is None
    conn = _conn or _with_db()
    created: list[dict[str, Any]] = []
    try:
        max_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) AS m FROM proxies").fetchone()["m"]
        for i, template in enumerate(templates):
            lbl = label if len(templates) == 1 and label else ""
            if not lbl and len(templates) > 1:
                lbl = f"{_auto_label(template)}-{i + 1}"
            cc = default_country or infer_country_from_template(template)
            ent = {
                "id": uuid.uuid4().hex[:12],
                "label": lbl or _auto_label(template),
                "provider": provider,
                "country": cc,
                "template": template,
                "enabled": True,
                "status": "unknown",
                "last_check_at": "",
                "last_check_msg": "",
                "exit_ip": "",
                "stats": {"assigned": 0, "success": 0, "fail": 0, "checks": 0},
                "created_at": _now_iso(),
                "sort_order": int(max_order) + 1 + i,
            }
            conn.execute(
                """INSERT INTO proxies(
                    id, label, provider, country, template, enabled, status, last_check_at, last_check_msg,
                    exit_ip, assigned_count, success_count, fail_count, check_count, created_at, sort_order
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ent["id"], ent["label"], ent["provider"], ent["country"], ent["template"], 1, "unknown",
                    "", "", "", 0, 0, 0, 0, ent["created_at"], ent["sort_order"],
                ),
            )
            created.append(ent)
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()
    return created


def update_proxy(proxy_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    with _lock:
        conn = _with_db()
        try:
            row = conn.execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()
            if not row:
                return None
            sets: list[str] = []
            params: list[Any] = []
            for k in ("label", "template", "provider", "country"):
                if k in fields and fields[k] is not None:
                    sets.append(f"{k}=?")
                    val = fields[k]
                    if k == "country":
                        val = (val or "").strip().upper()[:6]
                    params.append(val)
            if "template" in fields and fields["template"] is not None and "country" not in fields:
                cc = infer_country_from_template(fields["template"] or "")
                if cc:
                    sets.append("country=?")
                    params.append(cc)
            if "enabled" in fields and fields["enabled"] is not None:
                sets.append("enabled=?")
                params.append(1 if fields["enabled"] else 0)
            if sets:
                params.append(proxy_id)
                conn.execute(f"UPDATE proxies SET {', '.join(sets)} WHERE id=?", params)
                conn.commit()
            row = conn.execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()
            return _row_to_entry(row) if row else None
        finally:
            conn.close()


def delete_proxies(proxy_ids: list[str]) -> int:
    ids = [i for i in (proxy_ids or []) if i]
    if not ids:
        return 0
    with _lock:
        conn = _with_db()
        try:
            before = conn.execute("SELECT COUNT(*) AS c FROM proxies").fetchone()["c"]
            placeholders = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM proxies WHERE id IN ({placeholders})", ids)
            conn.execute(
                f"DELETE FROM proxy_bindings WHERE proxy_id IN ({placeholders})",
                ids,
            )
            conn.commit()
            after = conn.execute("SELECT COUNT(*) AS c FROM proxies").fetchone()["c"]
            return int(before) - int(after)
        finally:
            conn.close()


def update_settings(**fields: Any) -> dict[str, Any]:
    with _lock:
        conn = _with_db()
        try:
            settings = _get_settings(conn)
            for k in ("strategy", "require_healthy", "sticky_per_account"):
                if k in fields and fields[k] is not None:
                    settings[k] = fields[k]
            _set_settings(conn, settings)
            conn.commit()
            return dict(settings)
        finally:
            conn.close()


def check_proxies(proxy_ids: Optional[list[str]] = None, *, timeout: int = 15) -> list[dict[str, Any]]:
    with _lock:
        conn = _with_db()
        try:
            if proxy_ids:
                placeholders = ",".join("?" * len(proxy_ids))
                rows = conn.execute(
                    f"SELECT * FROM proxies WHERE id IN ({placeholders})",
                    list(proxy_ids),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM proxies ORDER BY sort_order, created_at").fetchall()
            results = [check_entry(_row_to_entry(r), timeout=timeout, conn=conn) for r in rows]
            conn.commit()
            return results
        finally:
            conn.close()


def _rate_row(total: int, ok: int) -> dict[str, Any]:
    total = int(total or 0)
    ok = int(ok or 0)
    fail = max(0, total - ok)
    rate = round(100.0 * ok / total, 1) if total else None
    return {"total": total, "success": ok, "fail": fail, "rate": rate}


def proxy_analytics(
    *,
    provider: Optional[str] = None,
    country: Optional[str] = None,
    reg_country: Optional[str] = None,
    days: int = 0,
) -> dict[str, Any]:
    """按代理商 / 代理国家 / 注册国家聚合成功率。"""
    where = "WHERE 1=1"
    params: list[Any] = []
    if provider:
        where += " AND provider=?"
        params.append(provider)
    if country:
        where += " AND country=?"
        params.append(country.strip().upper()[:6])
    if reg_country:
        where += " AND reg_country=?"
        params.append(reg_country.strip().upper()[:6])
    if days and days > 0:
        from datetime import datetime, timedelta

        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        where += " AND created_at >= ?"
        params.append(cutoff)

    def _agg(sql: str, p: list[Any]) -> list[dict[str, Any]]:
        rows = []
        for r in conn.execute(sql, p):
            item = dict(r)
            stats = _rate_row(item.pop("total"), item.pop("ok"))
            item.update(stats)
            rows.append(item)
        return rows

    with _lock:
        conn = _with_db()
        try:
            by_provider = _agg(
                f"""SELECT provider AS name,
                           COUNT(*) AS total,
                           SUM(success) AS ok
                    FROM proxy_events {where}
                    GROUP BY provider
                    HAVING provider != ''
                    ORDER BY total DESC""",
                list(params),
            )
            by_provider_country = _agg(
                f"""SELECT provider, country,
                           COUNT(*) AS total,
                           SUM(success) AS ok
                    FROM proxy_events {where}
                    GROUP BY provider, country
                    HAVING provider != ''
                    ORDER BY provider, country""",
                list(params),
            )
            by_provider_reg_country = _agg(
                f"""SELECT provider, reg_country,
                           COUNT(*) AS total,
                           SUM(success) AS ok
                    FROM proxy_events {where}
                    GROUP BY provider, reg_country
                    HAVING provider != '' AND reg_country != ''
                    ORDER BY provider, reg_country""",
                list(params),
            )
            by_provider_both = _agg(
                f"""SELECT provider, country, reg_country,
                           COUNT(*) AS total,
                           SUM(success) AS ok
                    FROM proxy_events {where}
                    GROUP BY provider, country, reg_country
                    HAVING provider != ''
                    ORDER BY provider, country, reg_country""",
                list(params),
            )
            # 行级累计（proxy 表 counters，含无事件的历史）
            proxy_rows = []
            sql2 = """SELECT provider, country,
                             SUM(success_count) AS success,
                             SUM(fail_count) AS fail,
                             SUM(success_count + fail_count) AS total
                      FROM proxies"""
            p2: list[Any] = []
            if provider:
                sql2 += " WHERE provider=?"
                p2.append(provider)
            sql2 += " GROUP BY provider, country HAVING provider != '' ORDER BY provider, country"
            for r in conn.execute(sql2, p2):
                stats = _rate_row(r["total"], r["success"])
                proxy_rows.append({
                    "provider": r["provider"] or "",
                    "country": r["country"] or "",
                    **stats,
                })
            event_total = conn.execute(
                f"SELECT COUNT(*) AS c FROM proxy_events {where}", params
            ).fetchone()["c"]
            return {
                "days": int(days or 0),
                "event_count": int(event_total or 0),
                "by_provider": by_provider,
                "by_provider_country": by_provider_country,
                "by_provider_reg_country": by_provider_reg_country,
                "by_provider_country_reg_country": by_provider_both,
                "proxy_counters": proxy_rows,
            }
        finally:
            conn.close()


def _timeseries_filters(
    *,
    provider: Optional[str] = None,
    country: Optional[str] = None,
    reg_country: Optional[str] = None,
    start_iso: str,
) -> tuple[str, list[Any]]:
    where = "WHERE substr(created_at, 1, 10) >= ?"
    params: list[Any] = [start_iso]
    if provider:
        where += " AND provider=?"
        params.append(provider)
    if country:
        where += " AND country=?"
        params.append(country.strip().upper()[:6])
    if reg_country:
        where += " AND reg_country=?"
        params.append(reg_country.strip().upper()[:6])
    return where, params


def _fill_daily_points(
    start,
    end,
    day_map: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    from datetime import timedelta

    pts: list[dict[str, Any]] = []
    cur = start
    while cur <= end:
        key = cur.isoformat()
        pts.append(day_map.get(key) or {"date": key, "total": 0, "success": 0, "fail": 0, "rate": None})
        cur += timedelta(days=1)
    return pts


def proxy_analytics_timeseries(
    *,
    provider: Optional[str] = None,
    country: Optional[str] = None,
    reg_country: Optional[str] = None,
    days: int = 30,
    group_by: str = "provider",
) -> dict[str, Any]:
    """按日聚合成功率；lines 供多曲线对比，series 为总量柱形。"""
    days = max(1, min(int(days or 30), 365))
    group_by = (group_by or "provider").strip().lower()
    if group_by not in ("overview", "provider", "country", "provider_country"):
        group_by = "provider"
    from datetime import datetime, timedelta

    start = (datetime.now() - timedelta(days=days - 1)).date()
    end = datetime.now().date()
    where, params = _timeseries_filters(
        provider=provider,
        country=country,
        reg_country=reg_country,
        start_iso=start.isoformat(),
    )

    with _lock:
        conn = _with_db()
        try:
            rows = conn.execute(
                f"""SELECT substr(created_at, 1, 10) AS day,
                           COUNT(*) AS total,
                           SUM(success) AS ok
                    FROM proxy_events {where}
                    GROUP BY day
                    ORDER BY day""",
                params,
            ).fetchall()
            if group_by == "provider":
                dim_sql = f"""SELECT substr(created_at, 1, 10) AS day, provider AS dim_a, '' AS dim_b,
                                     COUNT(*) AS total, SUM(success) AS ok
                              FROM proxy_events {where} AND provider != ''
                              GROUP BY day, provider ORDER BY provider, day"""
            elif group_by == "country":
                dim_sql = f"""SELECT substr(created_at, 1, 10) AS day, country AS dim_a, '' AS dim_b,
                                     COUNT(*) AS total, SUM(success) AS ok
                              FROM proxy_events {where} AND country != ''
                              GROUP BY day, country ORDER BY country, day"""
            elif group_by == "provider_country":
                dim_sql = f"""SELECT substr(created_at, 1, 10) AS day, provider AS dim_a, country AS dim_b,
                                     COUNT(*) AS total, SUM(success) AS ok
                              FROM proxy_events {where} AND provider != ''
                              GROUP BY day, provider, country
                              ORDER BY provider, country, day"""
            else:
                dim_sql = ""
            dim_rows = conn.execute(dim_sql, params).fetchall() if dim_sql else []
            filter_opts = {
                "providers": [
                    r["name"]
                    for r in conn.execute(
                        "SELECT DISTINCT provider AS name FROM proxy_events WHERE provider != '' ORDER BY provider"
                    ).fetchall()
                ],
                "countries": [
                    r["name"]
                    for r in conn.execute(
                        "SELECT DISTINCT country AS name FROM proxy_events WHERE country != '' ORDER BY country"
                    ).fetchall()
                ],
                "reg_countries": [
                    r["name"]
                    for r in conn.execute(
                        "SELECT DISTINCT reg_country AS name FROM proxy_events WHERE reg_country != '' ORDER BY reg_country"
                    ).fetchall()
                ],
            }
        finally:
            conn.close()

    day_map: dict[str, dict[str, Any]] = {}
    for r in rows:
        day_map[r["day"]] = {"date": r["day"], **_rate_row(r["total"], r["ok"])}
    series = _fill_daily_points(start, end, day_map)

    dim_map: dict[str, dict[str, dict[str, Any]]] = {}
    for r in dim_rows:
        a = (r["dim_a"] or "").strip()
        b = (r["dim_b"] or "").strip()
        if group_by == "provider":
            key, label = a, a
        elif group_by == "country":
            key, label = a, a
        else:
            key, label = f"{a}|{b}", f"{a} · {b}" if b else a
        if not key:
            continue
        dim_map.setdefault(key, {"label": label, "provider": a if group_by != "country" else "", "country": b if group_by == "provider_country" else (a if group_by == "country" else ""), "days": {}})["days"][r["day"]] = {
            "date": r["day"],
            **_rate_row(r["total"], r["ok"]),
        }

    lines: list[dict[str, Any]] = []
    if group_by == "overview":
        lines.append({
            "id": "overall",
            "label": "总览",
            "provider": "",
            "country": "",
            "points": series,
        })
    else:
        for key, meta in sorted(dim_map.items(), key=lambda kv: kv[1]["label"]):
            lines.append({
                "id": key,
                "label": meta["label"],
                "provider": meta.get("provider") or "",
                "country": meta.get("country") or "",
                "points": _fill_daily_points(start, end, meta["days"]),
            })

    total_events = sum(p["total"] for p in series)
    total_ok = sum(p["success"] for p in series)
    return {
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "group_by": group_by,
        "series": series,
        "lines": lines,
        "by_provider": {ln["id"]: ln["points"] for ln in lines if ln.get("provider") and not ln.get("country")},
        "filters": filter_opts,
        "overall": _rate_row(total_events, total_ok),
    }


def backfill_proxy_countries(*, force: bool = False) -> dict[str, int]:
    """从模板推断并回填 proxies.country（force=True 时覆盖已有值）。"""
    updated = 0
    skipped = 0
    still_empty = 0
    with _lock:
        conn = _with_db()
        try:
            sql = "SELECT id, template, country FROM proxies"
            if not force:
                sql += " WHERE country='' OR country IS NULL"
            for row in conn.execute(sql):
                cc = infer_country_from_template(row["template"] or "")
                cur = (row["country"] or "").strip().upper()
                if not cc:
                    if not cur:
                        still_empty += 1
                    else:
                        skipped += 1
                    continue
                if not force and cur:
                    skipped += 1
                    continue
                if force and cur == cc:
                    skipped += 1
                    continue
                conn.execute("UPDATE proxies SET country=? WHERE id=?", (cc, row["id"]))
                updated += 1
            conn.commit()
        finally:
            conn.close()
    return {"updated": updated, "skipped": skipped, "still_empty": still_empty}


def bindings_for_api(
    store: Optional[dict[str, Any]] = None,
    *,
    limit: int = 2000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    del store
    limit = max(1, min(int(limit), 10000))
    offset = max(0, int(offset))
    with _lock:
        conn = _with_db()
        try:
            rows = conn.execute(
                """SELECT b.*, p.label AS proxy_label
                   FROM proxy_bindings b
                   LEFT JOIN proxies p ON p.id = b.proxy_id
                   ORDER BY b.assigned_at DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
            return [
                {
                    "email": r["email"],
                    "proxy_id": r["proxy_id"],
                    "proxy_label": r["proxy_label"] or "",
                    "resolved_masked": mask_template(r["resolved"] or ""),
                    "assigned_at": r["assigned_at"],
                    "purpose": r["purpose"],
                }
                for r in rows
            ]
        finally:
            conn.close()
