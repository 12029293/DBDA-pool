# -*- coding: utf-8 -*-
"""注册 CK 补全（ck_enrich.py）——把纯协议注册产物补齐到「网页授权样本」的字段集。

样本 = C:\\Users\\Administrator\\Desktop\\Cookies_01.txt（26 项，网页 OAuth 登录产出）。
纯协议注册产物是 16~17 项，差的 10 项按**可得性**分三档：

  real   —— 服务端真下发，一次 GET 就能拿（本模块实测通过）：
            passport_csrf_token_wap_state  <- GET /passport/web/auth/login_only/
            passport_csrf_token            <- GET /passport/safe/csrf_token/（刷新）
  derive —— 前端 JS 常量 / 由已有字段推导，无需联网：
            flow_user_country（= store-country-code 大写）
            dbx-web-theme=light、conversation_list_v2_group_mode=none、i18next=en
            _ga / _ga_5MR93B9JT5（GA4 结构，按时间戳生成）
  synth  —— 服务端签名项，协议侧**实测不可得**：
            · passport_auth_status(_ss)  网页登录成功才下发；
              重放 /passport/web/auth/authorize/ 恒返 1902（补 Referer/Origin/wap_state 亦无效）
            · flow_cur_user_sec_id / flow_multi_user_sec_info
              flow-web SDK 生成（post /passport/doubao/security_token_beat/ 需 SDK 签名，返 2035）
            → 无密钥不可复现，只能按同格式本地合成。

模式（环境变量 CK_ENRICH）：
  match（默认）—— 全字段对齐样本：real + derive + synth（含 flow_* 占位）
  safe        —— 只写真字段：real + derive，丢弃 synth 四项
  off         —— 完全不补

对外入口只有一个 enrich()，**永不抛异常**：联网失败自动降级为只做 derive。

⚠️ synth 项是「长得像」而非真凭据。服务端若校验收到的值，错误值与缺失值表现相同
   （都不会比现在更差），但不会因此获得任何权限。要绝对干净就设 CK_ENRICH=safe。
"""
from __future__ import annotations

import base64
import hashlib
import os
import random
import time

GA_MEASUREMENT_ID = "5MR93B9JT5"          # 样本 _ga_5MR93B9JT5 的属性号
AID = "495671"
PLATFORM_APP_ID = "2085"

# 真实下发端（样本来源，实测：login_only 一个 GET 即下发 wap_state）
LOGIN_ONLY = ("/passport/web/auth/login_only/?device_platform=web&os_type=1"
              "&terminal_type=2&aid=%s&account_sdk_source=web&language=en"
              "&passport_jssdk_version=2.0.1-verify-center.1&platform_app_id=%s"
              % (AID, PLATFORM_APP_ID))
CSRF = "/passport/safe/csrf_token/?aid=%s" % AID

# 前端 JS 写死的 UI cookie（样本值）
UI_CONST = {
    "dbx-web-theme": "light",
    "conversation_list_v2_group_mode": "none",
    "i18next": "en",
}

# 样本字段顺序（补全项一律追加到尾部，保持与样本一致的相对顺序）
TAIL_ORDER = [
    "flow_user_country",
    "conversation_list_v2_group_mode",
    "dbx-web-theme",
    "i18next",
    "passport_auth_status",
    "passport_auth_status_ss",
    "passport_csrf_token_wap_state",
    "flow_cur_user_sec_id",
    "flow_multi_user_sec_info",
    "_ga",
    "_ga_%s" % GA_MEASUREMENT_ID,
]

SYNTH_KEYS = ("passport_auth_status", "passport_auth_status_ss",
              "flow_cur_user_sec_id", "flow_multi_user_sec_info")


def active_mode() -> str:
    m = (os.environ.get("CK_ENRICH") or "match").strip().lower()
    return m if m in ("off", "safe", "match") else "match"


# ------------------------------------------------------------------ 输入归一
def _as_pairs(cookies) -> list[tuple[str, str]]:
    """dict / list[(k,v)] / list[{name,value}] / Cookie 头文本 → [(k,v)]（保序去重）。"""
    pairs: list[tuple[str, str]] = []
    if cookies is None:
        return pairs
    if isinstance(cookies, dict):
        pairs = [(str(k), "" if v is None else str(v)) for k, v in cookies.items()]
    elif isinstance(cookies, str):
        for part in cookies.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                pairs.append((k.strip(), v.strip()))
    elif isinstance(cookies, (list, tuple)):
        for it in cookies:
            if isinstance(it, dict):
                nm = str(it.get("name") or "").strip()
                if nm:
                    pairs.append((nm, str(it.get("value") or "")))
            elif isinstance(it, (list, tuple)) and len(it) >= 2:
                pairs.append((str(it[0]).strip(), str(it[1])))
    seen, out = set(), []
    for k, v in pairs:
        if k and k not in seen:
            seen.add(k)
            out.append((k, v))
    return out


# ------------------------------------------------------------------ 真实抓取
def _snap(cli) -> dict:
    d = {c.name: c.value for c in cli.cookiejar}
    d.update(getattr(cli, "_cookies", {}) or {})
    return d


def _fetch(cookie_header: str, proxy, timeout: float) -> dict:
    """用账号自己的登录态打真实端点，返回新增/变化的 cookie。失败返回 {}。"""
    import dola_probe as dp
    out: dict = {}
    try:
        cli = dp.Client(proxy=proxy, timeout=timeout)
    except Exception:
        return out
    hdr = {"Cookie": cookie_header}
    for path in (LOGIN_ONLY, CSRF):
        before = _snap(cli)
        try:
            cli.get(dp.BASE_HOST + path, headers=hdr)
        except Exception:
            continue
        after = _snap(cli)
        for k, v in after.items():
            if k not in before or str(before.get(k)) != str(v):
                if v:
                    out[k] = v
    return out


# ------------------------------------------------------------------ 本地合成
def _ga_rand() -> str:
    return "%010d" % random.randint(10 ** 9, 10 ** 10 - 1)


def _synth_auth_status(pairs: list[tuple[str, str]]) -> str:
    """<32 位 hex>%2C。用账号自身 sessionid/user_id 派生，保证同号稳定可复现。"""
    seed = ""
    for k, v in pairs:
        if k in ("sessionid", "sessionid_ss", "uid_tt", "sid_tt"):
            seed += v
    if not seed:
        seed = "%s|%s" % (_ga_rand(), time.time())
    return hashlib.md5(seed.encode("utf-8", "replace")).hexdigest() + "%2C"


def _synth_flow_sec_id(seed: str, nbytes: int) -> str:
    """base64 占位（加密 blob 无密钥不可复现；仅保持长度/编码形态一致）。"""
    raw = hashlib.sha512(seed.encode("utf-8", "replace")).digest()
    while len(raw) < nbytes:
        raw += hashlib.sha512(raw).digest()
    return base64.b64encode(raw[:nbytes]).decode("ascii")


# ------------------------------------------------------------------ 主入口
def enrich(cookies, *, proxy=None, timeout: float = 15.0, fetch: bool = True,
           mode: str | None = None, log=None) -> dict:
    """把 CK 补齐到样本字段集，返回**新的有序 dict**。永不抛异常。

    cookies 可为 dict / [(k,v)] / [{name,value}] / Cookie 头文本。
    fetch=False 时跳过联网（只做 derive + synth），适合离线导出场景。
    """
    def _log(msg):
        if callable(log):
            try:
                log("hl", msg)
            except Exception:
                pass

    m = (mode or active_mode()).strip().lower()
    pairs = _as_pairs(cookies)
    if m == "off" or not pairs:
        return dict(pairs)

    have = {k for k, _ in pairs}
    header = "; ".join("%s=%s" % (k, v) for k, v in pairs)

    add: dict[str, str] = {}

    # 1) real：真端点抓取（只补缺失项，不覆盖账号已有值）
    if fetch:
        got = _fetch(header, proxy, timeout)
        for k in ("passport_csrf_token_wap_state",):
            if got.get(k) and k not in have:
                add[k] = str(got[k])
                _log("CK 补全[real] %s (%d 字符)" % (k, len(str(got[k]))))
        if got.get("passport_csrf_token") and "passport_csrf_token" not in have:
            add["passport_csrf_token"] = str(got["passport_csrf_token"])

    # 2) derive：由已有字段推导
    derive: dict[str, str] = {}
    if "flow_user_country" not in have:
        cc = ""
        for k, v in pairs:
            if k == "store-country-code" and v:
                cc = v
        if cc:
            derive["flow_user_country"] = cc.strip().upper()
    for k, v in UI_CONST.items():
        if k not in have:
            derive[k] = v
    now = int(time.time())
    if "_ga" not in have:
        derive["_ga"] = "GA1.1.%s.%d" % (_ga_rand(), now)
    ga_key = "_ga_%s" % GA_MEASUREMENT_ID
    if ga_key not in have:
        derive[ga_key] = "GS2.1.s%d$o1$g0$t%d$j%d$l0$h0" % (now, now, now % 60)
    add.update(derive)
    if derive:
        _log("CK 补全[derive] %s" % ", ".join(sorted(derive)))

    # 3) synth：服务端签名项（safe 模式丢弃）
    if m == "match":
        if "passport_auth_status" not in have:
            v = _synth_auth_status(pairs)
            add["passport_auth_status"] = v
            add["passport_auth_status_ss"] = v
            _log("CK 补全[synth] passport_auth_status=%s（本地派生，非真凭据）" % v)
        if "flow_cur_user_sec_id" not in have:
            seed = header or _ga_rand()
            cur = _synth_flow_sec_id(seed + "|cur", 78)
            mul = _synth_flow_sec_id(seed + "|multi", 99)
            add["flow_cur_user_sec_id"] = cur
            add["flow_multi_user_sec_info"] = mul
            _log("CK 补全[synth] flow_cur_user_sec_id/flow_multi_user_sec_info（占位，加密不可复现）")

    # 合并：原字段保序在前，补全项按 TAIL_ORDER 追加在尾
    merged: dict[str, str] = dict(pairs)
    for k in TAIL_ORDER:
        if k in add and k not in merged:
            merged[k] = add[k]
    for k, v in add.items():          # TAIL_ORDER 未列到的（如 passport_csrf_token 刷新）兜底
        if k not in merged:
            merged[k] = v
    return merged


def enrich_header(text: str, **kw) -> str:
    """Cookie 头文本进 → Cookie 头文本出。"""
    return "; ".join("%s=%s" % (k, v) for k, v in enrich(text, **kw).items())


def enrich_cookie_list(cookies: list, **kw) -> list:
    """[{name,value,domain,path}] 形态：补全后保持逐条结构（domain 沿用样本口径）。"""
    pairs = _as_pairs(cookies)
    dom = ".dola.com"
    for c in cookies or []:
        if isinstance(c, dict) and c.get("domain"):
            dom = str(c["domain"])
            break
    merged = enrich(pairs, **kw)
    return [{"name": k, "value": v, "domain": dom, "path": "/"} for k, v in merged.items()]
