"""Account keepalive: refresh token rotation + lightweight mail probe."""
from __future__ import annotations

import requests

from outlook_auto_register.service.account.graph_mail import (
    GRAPH_BASE,
    OUTLOOK_REST_BASE,
    _resource_of,
    refresh_token_for,
)


def _is_cid(p: str) -> bool:
    return len(p) == 36 and p.count("-") == 4


def parse_combo(line: str):
    parts = line.split("----")
    if len(parts) < 4:
        return None
    return parts, 3


def keepalive_one(line: str, proxy_url: str = "") -> dict:
    line = line.strip()
    if not line or line.startswith("#"):
        return {"skip": True, "line": line}
    parsed = parse_combo(line)
    if not parsed:
        return {"ok": False, "line": line, "detail": "非法格式"}
    parts, rt_idx = parsed
    email = parts[0]
    client_id = parts[2] if _is_cid(parts[2]) else ""
    rt = parts[rt_idx]

    if client_id:
        data = refresh_token_for(rt, "", client_id=client_id, proxy_url=proxy_url)
    else:
        data = refresh_token_for(rt, "", proxy_url=proxy_url)
    at = data.get("access_token", "")
    if not at:
        return {
            "ok": False,
            "email": email,
            "line": line,
            "detail": str(data.get("error_description", data.get("error")))[:80],
        }

    resource = _resource_of(data.get("scope", ""))
    if resource == "outlook":
        me_url = OUTLOOK_REST_BASE + "/me"
        msg_url = OUTLOOK_REST_BASE + "/me/messages?$top=1&$select=Subject"
    else:
        me_url = GRAPH_BASE + "/me?$select=userPrincipalName"
        msg_url = GRAPH_BASE + "/me/messages?$top=1&$select=subject"
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    prof_ok = msg_ok = False
    try:
        r = requests.get(me_url, headers={"Authorization": "Bearer " + at}, proxies=proxies, timeout=30)
        prof_ok = r.status_code == 200
        r2 = requests.get(msg_url, headers={"Authorization": "Bearer " + at}, proxies=proxies, timeout=30)
        msg_ok = r2.status_code == 200
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "email": email, "line": line, "detail": f"读信异常:{exc}"[:80]}

    new_rt = data.get("refresh_token", "") or rt
    rotated = new_rt != rt
    new_parts = list(parts)
    new_parts[rt_idx] = new_rt
    return {
        "ok": prof_ok and msg_ok,
        "email": email,
        "profile": prof_ok,
        "message": msg_ok,
        "rotated": rotated,
        "new_line": "----".join(new_parts),
    }
