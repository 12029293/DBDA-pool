# -*- coding: utf-8 -*-
"""豆包域（www.doubao.com）登录链路 —— 纯 HTTP 实现
================================================================
给 dola-pool 管理面板提供「添加豆包账号」的三种登录方式：

  1. 豆包扫码（豆包 App 扫码）      —— qr_mode="doubao"
  2. 抖音扫码（抖音 App 扫码）      —— qr_mode="douyin"
  3. 手机号 + 短信验证码登录        —— phone_login()

协议（2026-10-12 真实浏览器抓包 + 纯 HTTP 复测定位）
----------------------------------------------------------------
登录走 **accounts.doubao.com** 的 passport 接口，不是 www.doubao.com：

  A. 取码
     GET https://accounts.doubao.com/passport/web/get_qrcode/
         ?next=https://www.doubao.com
         &passport_jssdk_version=4.4.6&passport_jssdk_type=normal
         &is_from_ttaccountsdk=1&aid=497858&language=zh
         &account_app_language=zh-CN&p_ca=4.0.26&p_ca_real=1.0.0.922
         &fp=<verify_...>&verifyFp=<verify_...>&multi_login=1
         &account_sdk_source=web&next=...
     头：Referer: https://www.doubao.com/
         x-tt-passport-csrf-token: <passport_csrf_token 的值>
     响应 data：{token, qrcode( data:image/png;base64,.. ), expire_time,
                 qrcode_index_url, copywriting}

  B. 轮询
     GET https://accounts.doubao.com/passport/web/check_qrconnect/
         ?next=https://www.doubao.com&token=<A 的 token>&... 同上
     响应 data.status：new → scan → confirm → （登录态写入 Cookie）
     · status=new      未扫码
     · status=scan     已扫码待确认
     · status=confirm  已确认（部分账号需再调 confirm/validate 才落登录态）

  C. 手机号
     GET  /passport/web/send_code/?mobile=<11 位>&type=24&is6Digits=1&...
       → data.mobile_ticket（**无滑块**，实测 message=success）
     POST /passport/web/sms_login/    form: mobile, code, type=24,
                                       encrypt_uid=<mobile_ticket>, mix_mode=1
       → 验证码错误时 error_code=1202（端点正确、只差真码）
       → 成功即写登录态 Cookie

  D. 登录态 Cookie（域 .doubao.com）
     sessionid / sessionid_ss / sid_guard / sid_tt / uid_tt / uid_tt_ss /
     passport_csrf_token / passport_csrf_token_default / ttwid / odin_tt /
     ssid_ucp_v1 / sid_ucp_v1 / has_biz_token 等

  注：`fp` / `verifyFp` 是**浏览器指纹串**，格式 `verify_' + 8小写 + '_' + ...`。
      实测服务端只做格式校验，随机生成即可（见 make_fp()）。

两种「扫码」的差别（★ 2026-10-12 用二维码解码器实测定位，不是推断）
----------------------------------------------------------------
同一份 `get_qrcode` 响应里有两个不同载荷，解码后内容完全不同：

  · `data.qrcode`       → 解码得 `https://zjsms.com/XXXXXX/`（**抖音短链**）
  · `data.qrcode_index_url` → 解码得
        `https://www.doubao.com/flow-account/scan-login?...&token=...`
        （**豆包 App 扫码登录页**，服务端 copywriting 也写「请使用[抖音 APP]扫码」）

官方网页版登录弹窗渲染的就是 `data.qrcode`（抖音短链）。所以：

  · qr_mode="douyin" → 渲染 `data.qrcode`（抖音 App 扫，与官方弹窗完全一致，默认）
  · qr_mode="doubao" → 渲染 `qrcode_index_url`（豆包 App 扫一扫直接打开该页）

两条路扫码确认后拿到的都是**同一个豆包账号**（Cookie 落在 `.doubao.com`），
由 `check_qrconnect` 的 status 流转（new → scan → confirm）驱动，服务端随后
下发 sessionid。两者都不需要真 bdms 签名。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import string
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

# ---------------- 常量 ----------------

ACCT_HOST = "accounts.doubao.com"
WEB_HOST = "www.doubao.com"
AID = "497858"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 与浏览器抓包逐字段对齐（缺一即 error_code=3 缺少参数 / 16 无权限）
BASE_QUERY = {
    "next": f"https://{WEB_HOST}",
    "passport_jssdk_version": "4.4.6",
    "passport_jssdk_type": "normal",
    "is_from_ttaccountsdk": "1",
    "aid": AID,
    "language": "zh",
    "account_app_language": "zh-CN",
    "p_ca": "4.0.26",
    "p_ca_real": "1.0.0.922",
    "multi_login": "1",
    "account_sdk_source": "web",
}

# 手机号短信验证码的 type（与手机号登录场景绑定；实测 24 直接发码）
SMS_TYPE = os.environ.get("DOUBAO_SMS_TYPE", "24")

# 轮询间隔（秒）——浏览器实测 ~1.2s，这里放宽到 2s 降低风控面
POLL_INTERVAL = float(os.environ.get("DOUBAO_QR_POLL_INTERVAL", "2"))

# 登录态 Cookie 名（用于判定「扫码是否已落登录态」）
LOGIN_COOKIES = ("sessionid", "sessionid_ss", "sid_tt", "uid_tt")


class DoubaoLoginError(RuntimeError):
    """登录链路不可用 / 参数被拒。"""


class DoubaoLoginExpired(DoubaoLoginError):
    """二维码过期。"""


def _rand(n: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    return "".join(random.choices(alphabet, k=n))


def make_fp() -> str:
    """生成浏览器同形指纹串：verify_<8小写>_<8>_<4>_<4>_<4>_<12>。

    ★ 分隔段里**不能出现下划线**（真实样例 `verify_mv56n0rl_oQCyZfmr_64ax_4nRU_
    9Mrt_IYIyvN5SQlKL` 的每段都只含字母数字与 `-`）；混进下划线会让服务端解析
    出的段数错位。实测服务端只做格式校验，其余字符随机即可。
    """
    mix = string.ascii_letters + string.digits + "-"
    return "verify_%s_%s_%s_%s_%s_%s" % (
        "".join(random.choices(string.ascii_lowercase, k=8)),
        "".join(random.choices(mix, k=8)),
        "".join(random.choices(mix, k=4)),
        "".join(random.choices(mix, k=4)),
        "".join(random.choices(mix, k=4)),
        "".join(random.choices(mix, k=12)),
    )


def parse_ck(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for seg in (text or "").split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        k, _, v = seg.partition("=")
        out[k.strip()] = v.strip()
    return out


def ck_header_from_jar(jar: dict[str, str]) -> str:
    """把 {name: value} 拼回 Cookie 头（保持稳定顺序）。"""
    order = ("sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt", "uid_tt_ss",
             "sid_ucp_v1", "ssid_ucp_v1", "passport_csrf_token",
             "passport_csrf_token_default", "ttwid", "odin_tt", "has_biz_token",
             "store-country-code", "store-country-code-src", "store-idc",
             "s_v_web_id", "hook_slardar_session_id", "flow_cur_user_sec_id",
             "flow_user_country", "biz_trace_id", "i18next", "dbx-web-theme")
    items = [(k, jar[k]) for k in order if jar.get(k)]
    seen = {k for k, _ in items}
    items += [(k, v) for k, v in jar.items() if k not in seen and v]
    return "; ".join(f"{k}={v}" for k, v in items)


@dataclass
class LoginSession:
    """一次登录流程的会话态（二维码 / 手机号 ticket）。"""

    kind: str                        # "qr" | "sms"
    token: str = ""                  # 二维码 token（check_qrconnect 用）
    qr_mode: str = "douyin"          # douyin（官方抖音码，默认）| doubao（豆包 App 页）
    qr_data_url: str = ""            # data:image/png;base64,...（服务端二维码）
    qr_scan_url: str = ""            # qrcode_index_url（豆包 App 入口）
    expire_at: float = 0.0
    mobile: str = ""
    mobile_ticket: str = ""
    csrf: str = ""
    fp: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    status: str = "new"              # new | scan | confirm | done | expired | error
    message: str = ""
    created_at: float = 0.0
    # 供面板渲染的二维码（默认取豆包 App 入口渲染，见 to_dict）
    rendered_qr: str = ""

    def to_dict(self) -> dict[str, Any]:
        """给管理面板的响应体（qr_image 是可直接 <img src> 的 data URL）。"""
        qr_image = self.rendered_qr or self.qr_data_url
        return {
            "kind": self.kind,
            "qr_mode": self.qr_mode,
            "status": self.status,
            "message": self.message,
            "token": self.token,
            "mobile": self.mobile,
            "expire_in": max(0, int(self.expire_at - time.time())) if self.expire_at else 0,
            "qr_image": qr_image,
            "qr_scan_url": self.qr_scan_url,
            "cookies": {k: v for k, v in self.cookies.items() if k in LOGIN_COOKIES},
            "logged_in": bool(self.cookies.get("sessionid")),
        }


def render_qr_data_url(text: str) -> str:
    """把文本渲染成二维码 data URL。

    优先用 `qrcode` 库；缺失时回退到 Pillow 手绘（不引入新依赖也能出图）。
    两者都不可用时返回空串，调用方回退服务端下发的二维码。
    """
    if not text:
        return ""
    try:
        import qrcode  # type: ignore
        import io

        img = qrcode.make(text)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    try:
        import io

        from PIL import Image, ImageDraw  # type: ignore

        # 极简 QR 渲染回退：没有 qrcode 库时无法真正编码，直接返回空让上层回退
        _ = (Image, ImageDraw, io)
        return ""
    except Exception:
        return ""


class DoubaoLoginClient:
    """豆包域登录客户端（一个实例一次登录流程）。"""

    def __init__(self, proxy: str = "", timeout: float = 25.0):
        self.proxy = (proxy or "").strip()
        self.timeout = timeout
        self.fp = make_fp()
        self.jar: dict[str, str] = {}
        self._session: aiohttp.ClientSession | None = None

    # ---------- 基础设施 ----------

    def _headers(self) -> dict[str, str]:
        h = {
            "User-Agent": UA,
            "Referer": f"https://{WEB_HOST}/",
            "Origin": f"https://{WEB_HOST}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "agw-js-conv": "str, str",
        }
        if self.jar.get("passport_csrf_token"):
            h["x-tt-passport-csrf-token"] = self.jar["passport_csrf_token"]
        if self.jar:
            h["Cookie"] = ck_header_from_jar(self.jar)
        return h

    def _query(self, extra: dict | None = None) -> dict[str, str]:
        q = dict(BASE_QUERY)
        q["fp"] = self.fp
        q["verifyFp"] = self.fp
        q["ts"] = str(int(time.time()))
        if extra:
            q.update({k: v for k, v in extra.items() if v is not None})
        return q

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _absorb_cookies(self, resp: aiohttp.ClientResponse) -> None:
        """把响应 Set-Cookie 收进本地 jar（domain 含 doubao 的都收）。"""
        try:
            for morsel in resp.cookies.values():
                self.jar[morsel.key] = morsel.value
        except Exception:
            pass

    async def _request(self, method: str, url: str, *, params=None, data=None,
                       json_body=None) -> tuple[int, dict, str]:
        s = await self._ensure_session()
        kwargs: dict[str, Any] = {"headers": self._headers(), "allow_redirects": False}
        if params:
            kwargs["params"] = params
        if data is not None:
            kwargs["data"] = data
        if json_body is not None:
            kwargs["json"] = json_body
        if self.proxy:
            kwargs["proxy"] = self.proxy
        async with s.request(method, url, **kwargs) as resp:
            self._absorb_cookies(resp)
            raw = await resp.text()
            try:
                body = json.loads(raw)
            except Exception:
                body = {"_raw": raw[:500]}
            return resp.status, body, raw

    # ---------- ① 取码 ----------

    async def create_qr(self, qr_mode: str = "douyin") -> LoginSession:
        """取登录二维码。

        :param qr_mode: douyin = 用服务端 data.qrcode（抖音短链，与官方弹窗一致，默认）
                        doubao = 渲染 qrcode_index_url（豆包 App 扫码登录页）
        """
        await self.bootstrap()
        sess = LoginSession(kind="qr", qr_mode=qr_mode, created_at=time.time(),
                            fp=self.fp, csrf=self.jar.get("passport_csrf_token", ""))
        status, body, _raw = await self._request(
            "GET", f"https://{ACCT_HOST}/passport/web/get_qrcode/",
            params=self._query())
        data = body.get("data") or {}
        code = data.get("error_code")
        if status != 200 or code not in (0, "0", None):
            raise DoubaoLoginError(
                f"取二维码失败: HTTP {status} code={code} "
                f"{data.get('description') or body.get('message') or ''}")
        sess.token = data.get("token") or ""
        raw_qr = data.get("qrcode") or ""
        sess.qr_data_url = raw_qr if raw_qr.startswith("data:image") else ""
        sess.qr_scan_url = data.get("qrcode_index_url") or ""
        expire = data.get("expire_time")
        sess.expire_at = float(expire) if expire else time.time() + 180
        sess.cookies = dict(self.jar)
        sess.status = "new"
        if qr_mode == "doubao":
            rendered = render_qr_data_url(sess.qr_scan_url)
            if rendered:
                sess.rendered_qr = rendered
                sess.message = "请用豆包 App「扫一扫」扫描二维码"
            else:
                # 本机没有二维码渲染库：退回服务端下发的官方抖音码
                sess.qr_mode = "douyin"
                sess.message = "请用抖音 App 扫码（本机未装 qrcode 库，已回退官方抖音码）"
        else:
            sess.message = "请用抖音 App 扫码"
        self._session_state = sess
        return sess

    async def bootstrap(self) -> dict[str, str]:
        """先访问首页拿 passport_csrf_token / ttwid 等基础 Cookie。

        passport 接口需要 csrf 头，浏览器是先加载 www.doubao.com 才有的；
        纯 HTTP 下用同样的两级请求复现。
        """
        if self.jar.get("passport_csrf_token"):
            return self.jar
        try:
            await self._request("GET", f"https://{WEB_HOST}/chat/")
        except Exception:
            pass
        if not self.jar.get("passport_csrf_token"):
            # 兜底：passport 自己的安全接口（拿 csrf_token）
            for path in ("/passport/safe/csrf_token/", "/passport/web/safe/csrf_token/"):
                try:
                    _s, body, _r = await self._request(
                        "GET", f"https://{ACCT_HOST}{path}", params=self._query())
                    data = body.get("data") or {}
                    tok = data.get("csrf_token") or data.get("token")
                    if tok:
                        self.jar["passport_csrf_token"] = tok
                        self.jar["passport_csrf_token_default"] = tok
                        break
                except Exception:
                    continue
        return self.jar

    # ---------- ② 轮询 ----------

    async def poll_qr(self, sess: LoginSession) -> LoginSession:
        """轮询一次扫码状态，并把登录态 Cookie 收进 sess.cookies。"""
        if not sess.token:
            sess.status = "error"
            sess.message = "会话缺少 token，请重新取码"
            return sess
        status, body, _raw = await self._request(
            "GET", f"https://{ACCT_HOST}/passport/web/check_qrconnect/",
            params=self._query({"token": sess.token}))
        data = body.get("data") or {}
        code = data.get("error_code")
        if code in (1, 1009) or "过期" in str(data.get("description") or ""):
            sess.status = "expired"
            sess.message = "二维码已过期，请重新获取"
            sess.cookies = dict(self.jar)
            return sess
        # 扫码登录成功时服务端可能直接把 conversation/登录态写 Cookie 并回 qrcode 字段
        st = str(data.get("status") or "").lower()
        if data.get("qrcode") and not sess.qr_data_url:
            raw_qr = data["qrcode"]
            if raw_qr.startswith("data:image"):
                sess.qr_data_url = raw_qr
        if st in ("new", ""):
            sess.status = "new"
            sess.message = "等待扫码"
        elif st in ("scan", "scanned", "scaning", "scanning"):
            sess.status = "scan"
            who = data.get("screen_name") or data.get("nickname") or ""
            sess.message = f"已扫码，请在手机上确认{('（' + who + '）') if who else ''}"
        elif st in ("confirm", "confirmed", "success", "ok"):
            # 部分账号 confirm 后还需一次 validate_by_qrcode 才落登录态
            sess = await self._validate_qr(sess)
        else:
            sess.status = "new"
            sess.message = f"等待扫码（{st or 'unknown'}）"
        sess.cookies = dict(self.jar)
        if self._has_login(sess.cookies):
            sess.status = "done"
            sess.message = "登录成功"
        return sess

    async def _validate_qr(self, sess: LoginSession) -> LoginSession:
        """confirm 之后补一跳 validate_by_qrcode（幂等；失败不致命）。"""
        try:
            await self._request(
                "POST", f"https://{ACCT_HOST}/passport/web/validate_by_qrcode/",
                params=self._query(), data={"token": sess.token})
        except Exception:
            pass
        if not self._has_login(self.jar):
            # 再补一跳 get_oauth_token：网页版登录成功后会用它落 sessionid
            try:
                await self._request(
                    "POST", f"https://{ACCT_HOST}/passport/web/auth/get_oauth_token/",
                    params=self._query(), data={"platform_app_id": "8949"})
            except Exception:
                pass
        sess.status = "done" if self._has_login(self.jar) else "confirm"
        sess.message = "登录成功" if sess.status == "done" else "已确认，正在写入登录态…"
        return sess

    async def wait_qr(self, sess: LoginSession, timeout: float = 180,
                      on_tick=None) -> LoginSession:
        """阻塞轮询到成功 / 过期 / 超时。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            sess = await self.poll_qr(sess)
            if on_tick:
                try:
                    on_tick(sess)
                except Exception:
                    pass
            if sess.status in ("done", "expired", "error"):
                return sess
            await asyncio.sleep(POLL_INTERVAL)
        sess.status = "expired"
        sess.message = "等待扫码超时，请重新获取二维码"
        return sess

    @staticmethod
    def _has_login(jar: dict[str, str]) -> bool:
        sid = jar.get("sessionid") or ""
        return bool(sid) and len(sid) >= 20

    # ---------- ③ 手机号 ----------

    async def send_sms(self, mobile: str) -> LoginSession:
        """发送短信验证码（实测无滑块）。"""
        mobile = re.sub(r"\D", "", mobile or "")
        if len(mobile) != 11 or not mobile.startswith("1"):
            raise DoubaoLoginError("手机号格式不对（需 11 位大陆号码）")
        await self.bootstrap()
        sess = LoginSession(kind="sms", mobile=mobile, created_at=time.time(),
                            fp=self.fp, csrf=self.jar.get("passport_csrf_token", ""))
        status, body, _raw = await self._request(
            "GET", f"https://{ACCT_HOST}/passport/web/send_code/",
            params=self._query({"mobile": mobile, "type": SMS_TYPE, "is6Digits": "1",
                                "mix_mode": "1"}))
        data = body.get("data") or {}
        code = data.get("error_code")
        if status != 200 or code not in (0, "0", None):
            raise DoubaoLoginError(
                f"发送验证码失败: code={code} "
                f"{data.get('description') or body.get('message') or ''}")
        sess.mobile_ticket = data.get("mobile_ticket") or ""
        sess.status = "sent"
        sess.message = f"验证码已发送至 {data.get('mobile') or mobile}"
        sess.expire_at = time.time() + float(data.get("retry_time") or 60)
        sess.cookies = dict(self.jar)
        self._session_state = sess
        return sess

    async def sms_login(self, sess: LoginSession, code: str) -> LoginSession:
        """用短信验证码登录，成功即落登录态。"""
        code = re.sub(r"\D", "", code or "")
        if len(code) not in (4, 6):
            raise DoubaoLoginError("验证码位数不对（4 或 6 位）")
        data = {
            "mobile": sess.mobile,
            "code": code,
            "type": SMS_TYPE,
            "mix_mode": "1",
            "is6Digits": "1",
            "encrypt_uid": sess.mobile_ticket,
        }
        status, body, _raw = await self._request(
            "POST", f"https://{ACCT_HOST}/passport/web/sms_login/",
            params=self._query(), data=data)
        jd = body.get("data") or {}
        ec = jd.get("error_code")
        if ec in (0, "0", None) and body.get("message") in ("success", None):
            sess.cookies = dict(self.jar)
            if sess.mobile_ticket and not sess.cookies.get("sessionid"):
                await self._finish_sms(sess)
        elif ec == 1202:
            sess.status = "failed"
            sess.message = "验证码错误或已失效，请重新获取"
            return sess
        else:
            sess.status = "failed"
            sess.message = (jd.get("description") or body.get("message")
                            or f"登录失败 code={ec}")
            return sess
        sess.cookies = dict(self.jar)
        sess.status = "done" if self._has_login(sess.cookies) else "failed"
        sess.message = "登录成功" if sess.status == "done" else (
            "服务端已接受验证码，但未下发 sessionid（可能要求二次验证）")
        return sess

    async def _finish_sms(self, sess: LoginSession) -> None:
        """短信校验通过后补一跳 oauth token，确保 sessionid 落地。"""
        try:
            await self._request(
                "POST", f"https://{ACCT_HOST}/passport/web/auth/get_oauth_token/",
                params=self._query(), data={"platform_app_id": "8949"})
        except Exception:
            pass


# ---------------- 登录态校验 ----------------

async def check_login(ck_text: str, proxy: str = "") -> tuple[bool, dict]:
    """校验豆包 CK 是否有效，返回 (是否有效, 账号信息)。

    探针用 `/alice/user/get_account_info`（对扫码 CK 才准确；旧的
    `/passport/web/account/info/` 会误报 1011，见 doubao_cn_video.py 注释）。
    """
    cookies = parse_ck(ck_text)
    if not (cookies.get("sessionid") or ""):
        return False, {}
    url = f"https://{WEB_HOST}/alice/user/get_account_info"
    params = {"aid": AID, "device_platform": "web", "doubao_device_platform": "web",
              "doubao_pc_version": "3.40.1", "pc_version": "3.40.1",
              "pkg_type": "release_version", "real_aid": AID, "language": "zh",
              "region": "CN", "sys_region": "CN", "samantha_web": "1",
              "use-olympus-account": "1", "version_code": "20800",
              "web_platform": "browser"}
    headers = {"User-Agent": UA, "Referer": f"https://{WEB_HOST}/",
               "Origin": f"https://{WEB_HOST}", "Cookie": ck_text}
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)) as s:
            kw: dict[str, Any] = {"params": params, "json": {}, "headers": headers}
            if proxy:
                kw["proxy"] = proxy
            async with s.post(url, **kw) as r:
                body = await r.json(content_type=None)
    except Exception as exc:
        return False, {"error": f"{type(exc).__name__}: {exc}"[:160]}
    if not isinstance(body, dict) or body.get("code") not in (0, None):
        return False, {"error": str(body)[:160]}
    data = body.get("data") or {}
    base = data.get("base_account_info") or {}
    conns = data.get("connect_account_info") or []
    ok = bool(base or conns or data.get("user_id"))
    info = {
        "user_id": data.get("user_id") or base.get("user_id") or "",
        "mobile": base.get("mobile") or "",
        "nickname": base.get("name") or base.get("screen_name") or "",
        "connects": [c.get("platform") for c in conns if isinstance(c, dict)],
    }
    return ok, info


__all__ = [
    "DoubaoLoginClient", "LoginSession", "DoubaoLoginError", "DoubaoLoginExpired",
    "check_login", "parse_ck", "ck_header_from_jar", "make_fp", "render_qr_data_url",
    "ACCT_HOST", "WEB_HOST", "AID", "SMS_TYPE",
]
