# -*- coding: utf-8 -*-
"""Cookie 导出（cookie_export.py）。

把账号目录里的 ``accounts/<name>/cookie_state.json`` 还原成可直接复用/再导入的格式。
**目标是 dola-pool 2.1.4 能直接吃**，所以四种格式都对齐 2.1.4 的三条导入路：

  json     —— Cookie-Editor 风格的 JSON 数组 ``[{name,value,domain,path,...}]``
              2.1.4「添加账号 → Cookie 导入」弹窗直接粘贴 ✅
              2.1.4「批量添加 → Cookie JSON 文件」✅（前端认 Array）
              2.1.4「批量添加 → Cookie 头文本 / Cookie JSON」✅
  bundle   —— json 数组 + 账号元信息（name/email/user_id/cookie_header/cookies_list/storage）
              同时带 ``cookies`` 与 ``cookies_list`` 两个键，2.1.4 三条路全认 ✅
              （旧版只有 cookies_list，单账号弹窗会因缺 ``cookies`` 报「cookie 缺少 name/value 字段」）
  header   —— Cookie 头文本 ``k=v; k=v``，走 2.1.4「批量添加 → Cookie 头文本」，也是抓包/HTTP 工具通用格式
  netscape —— Netscape cookies.txt（curl -b / yt-dlp / 多数扩展都认），2.1.4 不认，仅供外部工具

2.1.4 的解析口径（cookie_login.normalize_cookies / cookie_import.parse_cookie_json_text）：
顶层数组，或含 ``cookies`` / ``cookie`` / ``cookies_list`` 数组的对象；每条至少 name+value，
``domain`` 必须含 dola.com（否则被当第三方 cookie 丢掉）。

纯函数层：只读文件、拼字符串，不碰浏览器/HTTP，可直接单测。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

# json 放第一位 = 默认格式：2.1.4 单账号弹窗唯一能直接粘贴的就是它
FORMATS = ("json", "bundle", "header", "netscape")
DEFAULT_FORMAT = "json"
# 能被 2.1.4「添加账号 → Cookie 导入」弹窗直接粘贴的格式（必须是合法 JSON）
PANEL_IMPORTABLE = ("json", "bundle")

_MIME = {
    "header": "text/plain; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "netscape": "text/plain; charset=utf-8",
    "bundle": "application/json; charset=utf-8",
}
_SUFFIX = {
    "header": "_cookie.txt",
    "json": "_cookie_editor.json",
    "netscape": "_cookies_netscape.txt",
    "bundle": "_ck_bundle.json",
}


def normalize_format(fmt: str | None) -> str:
    """未知/空格式一律回落默认（json，2.1.4 弹窗能直接粘贴的那个）。"""
    f = (fmt or "").strip().lower()
    return f if f in FORMATS else DEFAULT_FORMAT


def mime_for(fmt: str) -> str:
    return _MIME.get(normalize_format(fmt), _MIME[DEFAULT_FORMAT])


def filename_for(name: str, fmt: str) -> str:
    return "%s%s" % (name, _SUFFIX.get(normalize_format(fmt), _SUFFIX[DEFAULT_FORMAT]))


# --------------------------------------------------------------- 读
def cookies_of(state: dict) -> list[dict]:
    """cookie_state.json → [{name,value,domain,path}]（两种落盘结构都兼容）。"""
    if not isinstance(state, dict):
        return []
    items = state.get("cookies_list")
    if not isinstance(items, list) or not items:
        raw = state.get("cookies")
        items = [dict(v, name=k) for k, v in raw.items()] if isinstance(raw, dict) else []
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        nm = str(it.get("name") or "").strip()
        if not nm:
            continue
        out.append({
            "name": nm,
            "value": str(it.get("value") or ""),
            "domain": str(it.get("domain") or "").strip() or ".dola.com",
            "path": str(it.get("path") or "").strip() or "/",
        })
    return out


def load_account(accounts_dir, name: str) -> Optional[dict]:
    """读账号的 CK + web storage + 元信息；没有可导出来源返回 None。

    首选 cookie_state.json（规范化落盘，带域名/storage/邮箱），回落 cookie.txt（原始 Cookie 头）。
    storage 是 2.1.4 恢复 localStorage/sessionStorage/indexedDB 用的，别丢。
    """
    base = Path(accounts_dir) / name
    state_file = base / "cookie_state.json"
    if state_file.is_file():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = None
        if isinstance(state, dict):
            cks = cookies_of(state)
            if cks:
                storage = state.get("storage")
                return {
                    "cookies": cks,
                    "storage": storage if isinstance(storage, dict) else {},
                    "email": str(state.get("email") or ""),
                    "user_id": str(state.get("user_id") or ""),
                }
    txt_file = base / "cookie.txt"
    if txt_file.is_file():
        from cookie_import import parse_cookie_header_line
        try:
            raw = txt_file.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            raw = ""
        cks = parse_cookie_header_line(raw)
        if cks:
            return {"cookies": cks, "storage": {}, "email": "", "user_id": ""}
    return None


def load_cookies(accounts_dir, name: str) -> Optional[list[dict]]:
    """只取 cookie 列表（兼容旧调用）；没有可导出来源返回 None。"""
    acc = load_account(accounts_dir, name)
    return acc["cookies"] if acc else None


# --------------------------------------------------------------- 渲染
def to_header(cookies: list[dict]) -> str:
    return "; ".join("%s=%s" % (c["name"], c.get("value", "")) for c in cookies)


def editor_item(c: dict) -> dict:
    """单条 cookie → Cookie-Editor 风格对象（2.1.4 的 to_playwright_cookies 也认这套）。"""
    return {
        "domain": c.get("domain") or ".dola.com",
        "name": c["name"],
        "value": c.get("value", ""),
        "path": c.get("path") or "/",
        "secure": True,
        "httpOnly": False,
        "sameSite": "no_restriction",   # 2.1.4 映射成 SameSite=None（配 secure=True 合法）
        "session": True,
    }


def to_editor_json(cookies: list[dict]) -> str:
    """Cookie-Editor / EditThisCookie 扩展与 2.1.4 弹窗都能直接吃的 JSON 数组。"""
    return json.dumps([editor_item(c) for c in cookies], ensure_ascii=False, indent=2)


def to_netscape(cookies: list[dict]) -> str:
    """Netscape cookies.txt：domain / includeSubdomains / path / secure / expiry / name / value。"""
    lines = ["# Netscape HTTP Cookie File",
             "# 由 dola-pool 面板导出；curl -b 本文件即可带登录态",
             ""]
    for c in cookies:
        dom = (c.get("domain") or ".dola.com").strip()
        sub = dom.startswith(".")
        lines.append("\t".join([dom, "TRUE" if sub else "FALSE",
                                c.get("path") or "/", "TRUE", "0",
                                c["name"], c.get("value", "")]))
    return "\n".join(lines) + "\n"


def to_bundle(name: str, cookies: list[dict], *, storage: Optional[dict] = None,
              email: str = "", user_id: str = "", password: str = "") -> str:
    """完整 JSON：既是备份，也是 2.1.4 三条导入路通吃的包。

    ``cookies`` 与 ``cookies_list`` 都写（内容一致）：2.1.4 单账号弹窗/文件模式读 ``cookies``，
    批量文本模式读 ``cookies_list``/``cookies``；``storage`` 供浏览器路径恢复 web storage。
    """
    return json.dumps({
        "name": name,
        "email": email or "",
        "user_id": user_id or "",
        "password": password or "",
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cookie_count": len(cookies),
        "cookie_header": to_header(cookies),
        "cookies": [editor_item(c) for c in cookies],
        "cookies_list": cookies,
        "storage": storage or {},
    }, ensure_ascii=False, indent=2)


def render(fmt: str, name: str, cookies: list[dict], *, storage: Optional[dict] = None,
           email: str = "", user_id: str = "", password: str = "") -> str:
    f = normalize_format(fmt)
    if f == "json":
        return to_editor_json(cookies)
    if f == "netscape":
        return to_netscape(cookies)
    if f == "bundle":
        return to_bundle(name, cookies, storage=storage, email=email,
                         user_id=user_id, password=password)
    return to_header(cookies)


def sessionid_short(cookies: list[dict], n: int = 8) -> str:
    """取 sessionid 前 n 位做「是不是同一个号」的肉眼校验。"""
    for c in cookies:
        if c["name"] == "sessionid" and c.get("value"):
            return c["value"][:n]
    return ""


def _merge_cookies(cookies: list, add: dict) -> list:
    """把补真得到的字段并进 CK 列表（同名覆盖，新名追加），不改动其它字段。"""
    out = [dict(c) for c in cookies]
    idx = {c.get("name"): i for i, c in enumerate(out)}
    for k, v in (add or {}).items():
        if not v:
            continue
        if k in idx:
            out[idx[k]]["value"] = v
        else:
            out.append({"name": k, "value": v, "domain": ".dola.com", "path": "/"})
    return out


def export_account(accounts_dir, name: str, *, email: str = "", user_id: str = "",
                   password: str = "", enrich: bool = True, proxy=None,
                   browser_touch: bool | None = None) -> Optional[dict]:
    """单个账号的导出视图；无 CK 可导返回 None。

    一次性把四种格式都算好返回，前端切换格式不用再打一次接口。
    email/user_id 优先用调用方给的（账号池元数据），其次回落 state 里自带的。

    ``enrich=True`` 时把 CK 补齐到「网页授权样本」字段集（ck_enrich.py）。
    ``proxy`` 给了且可用才联网抓 wap_state，否则只做本地派生（离线安全）。

    ``browser_touch``（默认跟随 CK_BROWSER_TOUCH）用账号登录态跑一次真实页面，
    把 flow_cur_user_sec_id / s_v_web_id 换成浏览器真值；**已有真值则自动跳过**，
    所以新注册的号导出时不会再花这一次开销。
    """
    acc = load_account(accounts_dir, name)
    if acc is None:
        return None
    cookies = acc["cookies"]
    if enrich:
        try:
            from ck_enrich import enrich_cookie_list, active_mode
            if active_mode() != "off":
                cookies = enrich_cookie_list(cookies, proxy=proxy, timeout=15.0,
                                             fetch=bool(proxy))
        except Exception:  # 补全失败退回原始字段，不影响导出
            pass
    try:  # 浏览器补真（真值优先；失败/不可用一律退回现有字段）
        import ck_browser
        if browser_touch is None:
            browser_touch = ck_browser.touch_enabled()
        kv = {c["name"]: c.get("value") or "" for c in cookies}
        if browser_touch and not ck_browser.looks_real(kv):
            got = ck_browser.harvest(kv, proxy=proxy or "", timeout=90.0)
            cookies = _merge_cookies(cookies, got)
    except Exception:
        pass
    email = email or acc["email"]
    user_id = user_id or acc["user_id"]
    return {
        "name": name,
        "email": email or "",
        "user_id": user_id or "",
        "count": len(cookies),
        "sessionid": sessionid_short(cookies),
        "storage_keys": sorted(k for k in (acc["storage"] or {}) if k != "cookie"),
        "panel_importable": list(PANEL_IMPORTABLE),
        "formats": {f: render(f, name, cookies, storage=acc["storage"], email=email,
                              user_id=user_id, password=password) for f in FORMATS},
        "filenames": {f: filename_for(name, f) for f in FORMATS},
    }
