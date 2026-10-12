"""
豆包域（www.doubao.com）视频生成通道 —— 独立模块
================================================================
给 dola-pool 增加第二条出片通道：豆包域账号池 + 豆包域视频接口。

背景（2026-10-08 实测结论）
----------------------------------------------------------------
1. dola.com 与 doubao.com 是两套完全独立的登录体系：
   - aid：dola=495671 / doubao=497858
   - sessionid 不跨域：dola 的 sessionid 打豆包 passport 返回
     `error_code=1 会话过期，请重新登录`（实测）。
   - 反之豆包 CK 打 dola 池接口同样无效。
   ⇒ 账号池必须按域分开管理，不能混用。

2. 豆包域视频接口的正确路径是 **`POST https://www.doubao.com/chat/completion`**
   （**不带 `/samantha` 前缀**）：
   - `/samantha/chat/completion` → `gateway-error: unexpected end of JSON input`
     （网关层就挂了，根本没进业务）
   - `/chat/completion`        → 进业务逻辑，返回 SSE_ACK + conversation_id ✅
   路径来源：豆包网页版主 bundle `chat.8a2964c561.js` 提取 + 实测验证。

3. ★★ 两个真正的卡点（2026-10-08 定位，比"路径/签名"更关键）：

   a) **bot_id 必须用豆包域自己的值**
      豆包域 /chat 默认 bot = **7338286299411103781**
      （来源：让浏览器真实发一条消息抓到的原生请求，见 抓包/browser_send.py）

      传 dola 域的 7339470689562525703 会怎样：
        SSE_ACK 里 conversation_id="0"
        → STREAM_MSG_NOTIFY 回 "Something went wrong. Please try again later."
        → 会话建不起来，后面白等轮询。

   b) **710022002「当前服务访问频繁」是账号/IP 级限流，不是接口问题**
      证据链：
        1) 连**纯文本对话**（ability_type=0、内容"你好"）也返回 710022002；
        2) 浏览器里**真实 UI 操作发送**同样 710022002（browser_send.py 实测）；
        3) 参数矩阵（同 CK 同路径）：裸参数 → SSE_ACK 能建会话；
           带伪 a_bogus/msToken → 710022002。
      ⇒ 签名只决定"能不能进业务"，**进了业务之后照样限流**。
      触发条件：本机 1 小时内集中打了 20+ 次。
      解法：冷却 ≥10 分钟，或换出口 IP（换网络最直接）。
      ★ 2026-10-12 修正：池层现在把 710022002/710022004 归类为
      **DoubaoThrottleError（限流）** —— 只做短冷却、**不标登录失效**。
      先前误当 DoubaoLoginError，导致好号被标「风控」并锁 30 分钟
      （实测同一时刻登录探针仍正常返回手机号，证明账号是好的）。

4. 真实请求模板来源（browser_send.py 抓到的网页版原生请求）
   query：aid/device_id/device_platform/doubao_device_platform/doubao_pc_version/
          fp/language/pc_version/pkg_type/real_aid/region/samantha_web/sys_region/
          tea_uuid/tz_name/use-olympus-account/version_code/web_id/
          web_platform/web_tab_id (+msToken+a_bogus)
          注意 tea_uuid == web_id
   headers：x-flow-trace=04-<32hex>-<16hex>-01 / referer=/chat/local_<16位> /
            agw-js-conv: "str, str" / last-event-id: undefined
   body：client_meta(含 local_permissions) / messages / option(42 键) /
         [chat_ability] / user_context / ext(7 键，**不含 fp**)
   local_conversation_id 形如 local_<16 位数字>

5. 提示词必须以「生成影片：」开头，否则会被降级为文生图（dola 域同样规律）。

6. 豆包域真签名器（备选，默认关）见 `doubao_signer.py`
   （脚本 protocol/js/bdms_sign_doubao.js，SDK protocol/js/webmssdk_doubao.js
    321,966 字节，来自 lf-flow-web-cdn.doubao.com，
    pageId=26930 / aid=497858 / ic=13 / ddrt=13）。
   调用 frontierSign(url, body, method) → {"X-Bogus": "…"} 写进 query 的 a_bogus。
   开：DOUBAO_USE_SIGN=1。实测真签名能绕过 710022002，但会撞 710022004
   （subtype=semantic_reasoning，VM 缺浏览器行为熵），故默认关闭。

用法
----------------------------------------------------------------
    from doubao_cn_video import DoubaoVideoClient

    cli = DoubaoVideoClient(ck_text)          # ck_text = 豆包域完整 Cookie 头
    ok = await cli.check_login()              # 先验登录态
    url = await cli.generate_video("一只猫在跑", ratio="9:16", duration=5)
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
import uuid

import aiohttp

# ★ 豆包域真签名器（webmssdk frontierSign）。缺失时自动降级为裸参数模式。
try:
    from doubao_signer import sign_url, signer_available  # noqa: E402
    HAVE_SIGNER = True
except Exception:  # pragma: no cover
    sign_url = None  # type: ignore

    def signer_available() -> bool:  # type: ignore
        return False

    HAVE_SIGNER = False

# ---------------- 常量 ----------------

CN_DOMAIN = "www.doubao.com"
CN_AID = "497858"                 # 豆包工作 / 豆包主站 aid
CN_PC_VERSION = "3.39.2"          # ★ 实测网页版真实版本（原 2.44.0 系臆测）
CN_VERSION_CODE = "20800"

# ★★ 豆包域 /chat 默认 bot_id
#   实测来源：browser_send.py 让浏览器真实发一条消息，抓到的页面原生请求
#     bot_id = 7338286299411103781
#   注意：**绝对不能沿用 dola 域的 7339470689562525703**。
#   实测传错 bot_id 的后果：SSE_ACK 里 conversation_id="0"，
#   紧跟 STREAM_MSG_NOTIFY 回 "Something went wrong. Please try again later."
#   （即"请求结构被接受、会话建不起来"）。
CN_BOT_ID = "7338286299411103781"

# 豆包域出片用的模型名（对齐 dola 域 seedance 系列）
MODEL_SEEDANCE = "seedance_v2.0"

CN_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Origin": f"https://{CN_DOMAIN}",
    "Pragma": "no-cache",
    "Priority": "u=1, i",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# 额度不足关键词（与 dola_client 保持一致）
CREDIT_FAIL_PATTERN = re.compile(
    r"無法生成|无法生成|不能生成|无法完成|無法完成|"
    r"余额不足|餘額不足|额度不足|額度不足|额度耗尽|額度耗盡|"
    r"insufficient|unable to (generate|create)",
    re.IGNORECASE,
)


# ---------------- 工具 ----------------

def _rand_digits(n: int) -> str:
    return "".join(random.choices(string.digits, k=n))


def _rand_hex(n: int) -> str:
    return "".join(random.choices("0123456789abcdef", k=n))


def _rand_mix(n: int) -> str:
    return "".join(random.choices(
        string.ascii_letters + string.digits + "_-", k=n))


def _fake_ms_token() -> str:
    """（已废弃）豆包域一旦带 msToken 就被判 710022002，保留仅为向后兼容。"""
    return base64.urlsafe_b64encode(os.urandom(96)).rstrip(b"=").decode()


def _fake_a_bogus() -> str:
    """（已废弃）豆包域一旦带 a_bogus（含伪签名）就触发风控，保留仅为向后兼容。"""
    p1 = "".join(random.choices(string.ascii_letters + string.digits, k=34))
    p2 = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    return f"mf-{p1}-{p2}"


def _fake_fp() -> str:
    """豆包域 fp 指纹占位。

    ★ 实测格式（browser_send 抓到网页版真实值）：
        verify_muykqgmj_1gzRN0Mv_u9Z2_4o2A_831j_E5errwKLnH6F
      结构 = verify_ + 8小写 + _ + 8混合 + _ + 4 + _ + 4 + _ + 4 + _ + 21
    注意：真实请求里 fp **只出现在 query**，body.ext 里不含 fp。
    """
    return "verify_%s_%s_%s_%s_%s_%s" % (
        "".join(random.choices(string.ascii_lowercase, k=8)),
        _rand_mix(8), _rand_mix(4), _rand_mix(4),
        _rand_mix(4), _rand_mix(21),
    )


def parse_ck(text: str) -> dict:
    """把 Cookie 头文本解析成 {name: value}"""
    out = {}
    for seg in (text or "").split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        k, _, v = seg.partition("=")
        out[k.strip()] = v.strip()
    return out


def ck_domain(text: str) -> str:
    """判定 CK 属于哪个域：dola / doubao / unknown

    判据（实测）：
    - dola 域 CK 必带 msToken（且常带 s_v_web_id）
    - doubao 域 CK 必带 has_biz_token 或 doubao.com，无 msToken
    """
    low = (text or "").lower()
    if "mstoken=" in low or "s_v_web_id=" in low:
        return "dola"
    if "doubao.com" in low or "has_biz_token" in low:
        return "doubao"
    if "ttwid=" in low and "sessionid=" in low:
        return "doubao"
    return "unknown"


# ---------------- 客户端 ----------------

class DoubaoLoginError(Exception):
    """豆包域登录态无效（需换号）

    ★ 只用于**真登录失效**：710012000 `Invalid User ID` / 会话过期。
    `710022002 当前服务访问频繁` 属上游限流（登录探针照常可用），
    抛 `DoubaoThrottleError`，不要混进来 —— 混进来会把好号误判成风控。
    """


class DoubaoThrottleError(Exception):
    """上游限流 / 访问频繁（710022002 / 710022004）。

    与登录失效的区别（2026-10-12 实测）：限流只打对话/出片通道，
    同一时刻 `/alice/user/get_account_info` 仍正常返回手机号。
    ⇒ 处理方式：短冷却后重试或换号，**绝不标登录失效**。
    """


class DoubaoCreditError(Exception):
    """额度不足（需换号，不重试）"""


class DoubaoVideoClient:
    """豆包域（doubao.com）视频生成客户端，单个 CK 一个实例

    与 dola 域 DolaClient 的关键差异：
    - 域是 www.doubao.com，aid=497858
    - ability_type=17 走 /chat/completion（不再是 /im/chain 主链路）
    - 轮询同样走 /im/chain/single，cmd=3100
    """

    def __init__(self, ck_text: str, proxy: str = None):
        """
        :param ck_text: 豆包域完整 Cookie 头（含 sessionid=）
        :param proxy:  可选代理，形如 http://... 或 socks5://...
        """
        self.raw_ck = (ck_text or "").strip()
        self.cookies = parse_ck(self.raw_ck)
        self.sessionid = self.cookies.get("sessionid") or ""
        self.domain = ck_domain(self.raw_ck)
        self.proxy = proxy
        self.device_id = "7" + _rand_digits(18)
        self.web_id = "7" + _rand_digits(18)
        self.fp = _fake_fp()
        self._last_sign_err = ""
        self._read_sse_warn = ""
        self._poll_debug = {}
        self._local_conv_id = ""
        self._conv_type = 3          # 创作会话类型（SSE_ACK 回填）
        # ★ 上游真实会话 ID（SSE_ACK 回填）。注意不是 `_local_conv_id`
        #   —— 后者是发请求时随机生成的 `local_<16位数字>` 占位，只用于 referer，
        #   拿它当 conversation_id 会让任务记录无法用于续轮询/排障。
        self.last_conversation_id = ""

    @property
    def is_valid(self) -> bool:
        return bool(self.sessionid) and len(self.sessionid) >= 20

    # ---------- 内部请求构造 ----------

    def _headers(self, referer: str = None, local_conv_id: str = "",
                 im: bool = False) -> dict:
        """请求头（对齐浏览器抓到的真实网页版请求头）

        ★ 两套头，不能混（实测踩过坑）：
          /chat/completion（对话/出片受理）
              content-type: application/json
              agw-js-conv : "str, str"
          /im/chain/*（拉消息/轮询结果）
              content-type: application/json; encoding=utf-8   ← 少了 encoding
                            → 712012002「不支持编码类型」
              agw-js-conv : "str"                              ← 只有 str
              accept      : application/json, text/plain, */*
        真实头清单（公共）：
            x-flow-trace: 04-<32hex>-<16hex>-01
            referer: https://www.doubao.com/chat/<conversation_id>
            last-event-id: undefined      ← 字面量字符串 "undefined"
        """
        h = {
            **CN_HEADERS,
            "Cookie": self.raw_ck,
            "X-Flow-Trace": f"04-{_rand_hex(32)}-{_rand_hex(16)}-01",
            "last-event-id": "undefined",
            "Referer": referer or f"https://{CN_DOMAIN}/chat/{local_conv_id}",
        }
        if im:
            h["content-type"] = "application/json; encoding=utf-8"
            h["agw-js-conv"] = "str"
            h["accept"] = "application/json, text/plain, */*"
        else:
            h["agw-js-conv"] = "str, str"
        return h

    def _params(self, extra: dict = None) -> dict:
        """query 参数（对齐 browser_send 抓到的真实网页版 query）

        真实 query 全量（2026-10-08 抓包）：
            aid=497858 & device_id=… & device_platform=web &
            doubao_device_platform=web & doubao_pc_version=3.39.2 &
            fp=verify_… & language=zh & pc_version=3.39.2 &
            pkg_type=release_version & real_aid=497858 & region=HK &
            samantha_web=1 & sys_region=HK & tea_uuid=… &
            tz_name=Asia/Shanghai & use-olympus-account=1 &
            version_code=20800 & web_id=… & web_platform=browser &
            web_tab_id=… & msToken=… & a_bogus=…
        注意：tea_uuid 与 web_id 实测**取值相同**。
        """
        p = {
            "aid": CN_AID,
            "device_id": self.device_id,
            "device_platform": "web",
            "doubao_device_platform": "web",
            "doubao_pc_version": CN_PC_VERSION,
            "fp": self.fp,
            "language": "zh",
            "pc_version": CN_PC_VERSION,
            "pkg_type": "release_version",
            "real_aid": CN_AID,
            "region": os.environ.get("DOUBAO_REGION", "CN"),
            "samantha_web": 1,
            "sys_region": os.environ.get("DOUBAO_REGION", "CN"),
            "tea_uuid": self.web_id,
            "tz_name": "Asia/Shanghai",
            "use-olympus-account": 1,
            "version_code": CN_VERSION_CODE,
            "web_id": self.web_id,
            "web_platform": "browser",
            "web_tab_id": str(uuid.uuid4()),
        }
        if extra:
            p.update(extra)
        return p

    def _sess_kw(self) -> dict:
        kw = {"trust_env": True}
        if self.proxy:
            kw["proxy"] = self.proxy
        return kw

    # ---------- 登录态校验 ----------

    async def check_login(self) -> bool:
        """校验豆包域登录态是否有效

        ★ 2026-10-08 修复：原实现打 `/passport/web/account/info/`，
          对**扫码得到的 CK 返回 error_code=1011「用户不存在」**，
          导致有效 CK 被误判为无效（扫码用户明明已登录、会员已开通）。
          改用 `/alice/user/get_account_info`——扫码 CK 实测返回
          `code:0` + `base_account_info.mobile` + `connect_account_info`
          （platform=aweme_v2），是网页版真正的登录态探针。
        """
        url = f"https://{CN_DOMAIN}/alice/user/get_account_info"
        try:
            async with aiohttp.ClientSession(**self._sess_kw()) as s:
                async with s.post(
                    url,
                    params=self._params(),
                    json={},
                    headers={**self._headers(), "agw-js-conv": "str, str"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    j = await r.json(content_type=None)
        except Exception:
            return False
        if not isinstance(j, dict) or j.get("code") not in (0, None):
            return False
        data = j.get("data") or {}
        return bool(data.get("base_account_info")
                    or data.get("connect_account_info")
                    or data.get("user_id"))

    async def account_info(self) -> dict:
        """返回账号信息（失败返回 {}）"""
        url = f"https://{CN_DOMAIN}/alice/user/get_account_info"
        try:
            async with aiohttp.ClientSession(**self._sess_kw()) as s:
                async with s.post(
                    url, params=self._params(), json={},
                    headers={**self._headers(), "agw-js-conv": "str, str"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    j = await r.json(content_type=None)
            return (j.get("data") or {}) if isinstance(j, dict) else {}
        except Exception:
            return {}

    # ---------- 视频生成 ----------

    def _build_video_body(self, prompt: str, ratio: str, duration: int, model: str,
                          mention_skill: str = "") -> dict:
        """构建视频请求体（**结构逐字段对齐 browser_send 抓到的真实网页版请求**）

        真实请求顶层键：client_meta / messages / option / user_context / ext
        （纯文本对话时**没有 chat_ability**；视频时用它指定 ability_type=17）

        关键字段（实测，写错会导致 conversation_id="0" + Something went wrong）：
          client_meta.bot_id          = 7338286299411103781   ★ 豆包域专用
          client_meta.local_conv_id   = local_<16位数字>
          client_meta.local_permissions = 3 条定位权限
          option.agent_mode           = 2
          option.conversation_init_ext = {model_item_key, reasoning_effort, mode_id}
          option.support_lazy_fetch_stream = true
          option.is_old_user          = true
          option.aggregate_params     = {mention_skill_list, …}
          option.conversation_mode    = 1
          ext.agent_mode              = "2"；ext.is_finish = "1"
          ext 里**不含 fp**（fp 只放 query）

        视频触发方式（待真实出片抓包最终确认）：
          网页版「视频生成/Seedance」技能是通过 `option.aggregate_params.mention_skill_list`
          （一个 JSON 字符串数组）挂上去的；这里同时保留 chat_ability.ability_type=17
          作为 dola 域同款兜底。
        """
        now_ms = int(time.time() * 1000)
        now_sec = now_ms // 1000
        local_conv_id = "local_" + _rand_digits(16)
        self._local_conv_id = local_conv_id
        # ★ 必须加「生成影片：」前缀，否则降级为文生图
        video_prompt = f"生成影片：{prompt}，{ratio}"
        skill_list = json.dumps(
            ([{"skill_id": mention_skill}] if mention_skill else []),
            ensure_ascii=False, separators=(",", ":"))
        return {
            "client_meta": {
                "local_conversation_id": local_conv_id,
                "conversation_id": "",
                "bot_id": CN_BOT_ID,
                "last_section_id": "",
                "last_message_index": None,
                "local_permissions": [
                    {"permission_name": "ACCESS_COARSE_LOCATION", "status": 3},
                    {"permission_name": "ACCESS_FINE_LOCATION", "status": 3},
                    {"permission_name": "ACCESS_BACKGROUND_LOCATION", "status": 3},
                ],
            },
            "messages": [{
                "local_message_id": str(uuid.uuid4()),
                "content_block": [{
                    "block_type": 10000,
                    "content": {
                        "text_block": {"text": video_prompt, "icon_url": "",
                                       "icon_url_dark": "", "summary": ""},
                        "pc_event_block": "",
                    },
                    "block_id": str(uuid.uuid4()),
                    "parent_id": "",
                    "meta_info": [],
                    "append_fields": [],
                }],
                "message_status": 0,
            }],
            "option": {
                "send_message_scene": "",
                "create_time_ms": now_ms,
                "collect_id": "",
                "is_audio": False,
                "answer_with_suggest": False,
                "agent_mode": 2,
                "tts_switch": False,
                "need_deep_think": 0,
                "click_clear_context": False,
                "from_suggest": False,
                "is_regen": False,
                "is_replace": False,
                "is_from_click_option": False,
                "is_from_click_softlink": False,
                "disable_sse_cache": False,
                "select_text_action": "",
                "is_select_text": False,
                "resend_for_regen": False,
                "scene_type": 0,
                "unique_key": str(uuid.uuid4()),
                "start_seq": 0,
                "need_create_conversation": True,
                "conversation_init_option": {"need_ack_conversation": True},
                "conversation_init_ext": {"model_item_key": "0",
                                          "reasoning_effort": "3", "mode_id": "1"},
                "regen_query_id": [],
                "edit_query_id": [],
                "regen_instruction": "",
                "no_replace_for_regen": False,
                "message_from": 0,
                "shared_app_name": "",
                "shared_app_id": "",
                "sse_recv_event_options": {"support_chunk_delta": True},
                "support_lazy_fetch_stream": True,
                "is_ai_playground": False,
                "is_old_user": True,
                "recovery_option": {"is_recovery": False, "req_create_time_sec": now_sec,
                                    "append_sse_event_scene": 0},
                "message_storage_type": 0,
                "related_deleted_message_ids": {},
                "connector_info_list": [],
                "model_config": {"model_item_key": "0", "model_extra_params": {},
                                 "reasoning_effort": 3},
                "aggregate_params": {
                    "mention_skill_list": skill_list,
                    "mention_plugin_list": "[]",
                    "mention_ext": "[{}]",
                    "conversation_mode": "1",
                    "mode_id": "1",
                    "model_item_key": "0",
                    "agent_mode": "2",
                    "reasoning_effort": "3",
                    "provider_id": "",
                },
                "conversation_mode": 1,
            },
            "chat_ability": {
                "ability_type": 17,
                "ability_param": json.dumps({
                    "ratio": ratio,
                    "model": model,
                    "duration": duration,
                }),
            },
            "user_context": [],
            "ext": {
                "agent_mode": "2",
                "use_deep_think": "0",
                "sub_conv_firstmet_type": "1",
                "collection_id": "",
                "is_finish": "1",
                "conversation_init_option": '{"need_ack_conversation":true}',
                "commerce_credit_config_enable": "0",
            },
        }

    async def _read_sse(self, resp: aiohttp.ClientResponse) -> list:
        """读取 SSE 流，返回 [(event_name, data_dict), ...]

        ★ 2026-10-11 容错修复：豆包域 CDN（volc-dcdn）的 SSE 流偶发
          `ClientPayloadError: Not enough data to satisfy transfer length header`
          （chunked 尾段被 CDN 提前关闭）。原实现用 `async for chunk in resp.content`
          会把该异常抛出，导致**明明已拿到 SSE_ACK + 真实 conversation_id 的会话
          被整段丢弃**（表现为「提交即失败」，实际受理成功）。
          改为 `iter_any()` + 捕获异常，保留已读事件——出片受理判定只需
          SSE_ACK 里的 conversation_id，后续凭 conv_id 轮询即可。
        """
        events = []
        buf = ""
        try:
            async for chunk in resp.content.iter_any():
                if not chunk:
                    continue
                buf += chunk.decode("utf-8", errors="replace")
                while "\n\n" in buf:
                    raw, buf = buf.split("\n\n", 1)
                    name, dlines = "", []
                    for line in raw.split("\n"):
                        if line.startswith("event:"):
                            name = line[6:].strip()
                        elif line.startswith("data:"):
                            dlines.append(line[5:].strip())
                    ds = "\n".join(dlines)
                    try:
                        data = json.loads(ds) if ds else {}
                    except json.JSONDecodeError:
                        data = {"_raw": ds}
                    events.append((name, data))
        except Exception as exc:  # 连接被 CDN 截断：保留已读事件
            self._read_sse_warn = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        return events

    async def probe_text(self, text: str = "你好", timeout: int = 40) -> dict:
        """发一句**纯文本**判限流（对齐 USAGE.md 的「你好探测」）。

        为什么用纯文本：
          · 与出片共用同一条 `/chat/completion` 限流通道 → 结论可直接代表出片
          · 不消耗视频额度、不建视频会话，是最便宜的探针

        返回 {ok, status, reason}
          status: ok（已受理）| throttled（710022002/710022004）
                  | login_invalid（710012001）| error
        """
        if not self.is_valid:
            return {"ok": False, "status": "login_invalid",
                    "reason": "CK 缺少 sessionid"}
        body = self._build_video_body(text, "16:9", 5, MODEL_SEEDANCE)
        # 纯文本对话：真实网页版不带 chat_ability
        body.pop("chat_ability", None)
        try:
            async with aiohttp.ClientSession(**self._sess_kw()) as s:
                async with s.post(f"https://{CN_DOMAIN}/chat/completion",
                                  params=self._params(), json=body,
                                  headers=self._headers(
                                      local_conv_id=self._local_conv_id),
                                  timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                    raw = await r.read()
        except Exception as exc:
            return {"ok": False, "status": "error",
                    "reason": f"{type(exc).__name__}: {exc}"[:180]}
        text_body = raw.decode("utf-8", "replace")
        # 网关/业务错误可能是纯 JSON（非 SSE）
        if "710022002" in text_body or "710022004" in text_body:
            return {"ok": False, "status": "throttled",
                    "reason": "当前服务访问频繁（710022002）"}
        if "710012001" in text_body or "Invalid User ID" in text_body:
            return {"ok": False, "status": "login_invalid",
                    "reason": "登录已过期（710012001）"}
        if "SSE_ACK" in text_body:
            return {"ok": True, "status": "ok", "reason": "已受理（未被限流）"}
        return {"ok": False, "status": "error",
                "reason": text_body[:180].replace("\n", " ")}

    async def generate_video(self, prompt: str, ratio: str = "9:16",
                             duration: int = 5, model: str = MODEL_SEEDANCE,
                             timeout: int = 300) -> str:
        """豆包域出片，返回视频下载 URL

        流程：POST /chat/completion(ability_type=17)
              → SSE 取 conversation_id
              → 轮询 /im/chain/single(cmd=3100) 取 download_url

        ★ 2026-10-08 关键修复（三处，缺一不可）：
          1) 路径：/samantha/chat/completion → /chat/completion
             （前者网关层直接 `unexpected end of JSON input`）
          2) 签名：加**真** a_bogus（doubao 版 webmssdk frontierSign）。
             伪签名 mf-xxx → 710022002；无签名 → 能建会话但被风控限流；
             真签名 → 正常过风控。
             ★ 只签 a_bogus，**不要加 msToken**（加了就 710022002）。
          3) 校验：check_login 改打 /alice/user/get_account_info。
        """
        if not self.is_valid:
            raise DoubaoLoginError("CK 缺少有效 sessionid")

        body = self._build_video_body(prompt, ratio, duration, model)
        body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        # referer 必须带 local_conversation_id（真实网页版如此）
        headers = self._headers(local_conv_id=self._local_conv_id)
        params = self._params()

        # ★ 豆包域**不要加 a_bogus 签名**（2026-10-08 实测结论，与 dola 域相反）：
        #   - 带真 a_bogus  → 710022004 rate limited，
        #                     decision.type=verify / subtype=semantic_reasoning /
        #                     verify_scene=doubao_message_web
        #                     （脱离真实浏览器的 VM 环境缺行为熵，被判语义风控）
        #   - 带伪 a_bogus  → 710022002 服务访问频繁
        #   - 裸参数（不带签名）→ ✅ SSE_ACK + 真 conversation_id，正常受理
        #   故此处直接走裸参数。签名能力保留在 doubao_signer.py，
        #   由环境变量 DOUBAO_USE_SIGN=1 开启（默认关）。
        url = f"https://{CN_DOMAIN}/chat/completion"
        if os.environ.get("DOUBAO_USE_SIGN") == "1" and HAVE_SIGNER:
            try:
                q = "&".join(f"{k}={v}" for k, v in params.items())
                signed = sign_url(f"{url}?{q}", method="POST", headers=headers,
                                  body=body_json, cookies=self.cookies)
                if "a_bogus=" in signed:
                    url, params = signed, None
            except Exception as e:
                self._last_sign_err = str(e)[:200]

        conv_id = ""
        sse_err = None
        srv_text = ""
        aio_kw = {"params": params} if params else {}
        async with aiohttp.ClientSession(**self._sess_kw()) as s:
            async with s.post(url, json=body, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=120),
                              **aio_kw) as resp:
                events = await self._read_sse(resp)

        for name, data in events:
            if name == "gateway-error":
                msg = json.dumps(data, ensure_ascii=False)
                if "710012000" in msg or "Invalid User ID" in msg:
                    raise DoubaoLoginError(f"豆包域登录态无效: {msg[:120]}")
                raise Exception(f"豆包域受理失败: {msg[:160]}")
            if name == "STREAM_ERROR":
                code = data.get("error_code")
                msg = data.get("error_msg") or ""
                if code in (710022002, 710022004):
                    # 上游限流（不是登录失效！）：抛专门的异常，让池层只做短冷却。
                    raise DoubaoThrottleError(f"豆包域限流({code}): {msg}")
                sse_err = f"[{code}] {msg}"
            if name == "SSE_ACK":
                ack = data.get("ack_client_meta") or {}
                conv_id = ack.get("conversation_id", "")
                # 创作会话 conversation_type=3，轮询时要带上
                if ack.get("conversation_type"):
                    self._conv_type = ack["conversation_type"]
            if name == "STREAM_MSG_NOTIFY":
                # 抓服务端回吐的文本，便于判断「Something went wrong」等软失败
                try:
                    blocks = ((data.get("content") or {}).get("content_block") or [])
                    for b in blocks:
                        t = ((b.get("content") or {}).get("text_block") or {}).get("text")
                        if t:
                            srv_text = t
                except Exception:
                    pass
            if not conv_id and isinstance(data, dict):
                conv_id = data.get("conversation_id") or conv_id

        # ★ 关键：conversation_id="0" 表示**会话没建起来**（bot_id 错 / 字段缺失 /
        #   被风控降级），不是有效会话，必须当失败处理，否则会白等轮询几百秒。
        if not conv_id or conv_id == "0":
            raise Exception(
                "豆包域未建立会话(conversation_id=%r) %s %s"
                % (conv_id or "", sse_err or "", srv_text[:200]))
        self.last_conversation_id = conv_id

        return await self._poll_video(conv_id, timeout,
                                      conversation_type=self._conv_type)

    @staticmethod
    def _extract_messages(dl: dict) -> list:
        """从 downlink_body 提取 messages，兼容多种字段名。

        已知/可能字段名：
          - pull_singe_chain_downlink_body（dola 域拼写，豆包域可能不同）
          - pull_single_chain_downlink_body
          - pull_recent_conv_chain_downlink_body（bundle 里实际出现过的）
        """
        if not isinstance(dl, dict):
            return []
        for key in ("pull_singe_chain_downlink_body",
                    "pull_single_chain_downlink_body",
                    "pull_recent_conv_chain_downlink_body",
                    "pull_chain_downlink_body"):
            v = dl.get(key)
            if isinstance(v, dict) and isinstance(v.get("messages"), list):
                return v["messages"]
        # 兜底：任意含 messages 的子结构
        for v in dl.values():
            if isinstance(v, dict) and isinstance(v.get("messages"), list):
                return v["messages"]
        return []

    async def _poll_video(self, conversation_id: str, timeout: int = 300,
                          conversation_type: int = 3) -> str:
        """轮询 /im/chain/single（cmd=3100）取视频下载地址

        ★★ 三个必须对齐的点（2026-10-08 全部实测定位，缺一即失败）：

          1) **body 必须包 `uplink_body`**
                 {"cmd":3100,
                  "uplink_body":{"pull_singe_chain_uplink_body":{...}},
                  "sequence_id":"<uuid>","channel":2,"version":"1"}
             扁平 body（cmd/conv_id 直接放顶层）→ `712012002 不支持编码类型`

          2) **headers 必须用 im 版**（见 `_headers(im=True)`）
                 content-type: application/json; encoding=utf-8
                 agw-js-conv : str
             少了 `; encoding=utf-8` 同样 → 712012002

          3) `anchor_index` 用 9007199254740991（JS MAX_SAFE_INTEGER），
             `direction: 1`（朝最新消息方向翻），`conversation_type: 3`（创作会话）

        响应解析：
          downlink_body.pull_singe_chain_downlink_body.messages[].content
            （content 是 **JSON 字符串**，需二次 loads）
            → block_type == 2074（creation_block）
            → creations[].video.download_url
        实测成功样本：384x682 / 5.057s / 851,428 字节 MP4。
        """
        start = time.time()
        url = f"https://{CN_DOMAIN}/im/chain/single"
        async with aiohttp.ClientSession(**self._sess_kw()) as s:
            while time.time() - start < timeout:
                await asyncio.sleep(5)
                body = {
                    "cmd": 3100,
                    "uplink_body": {
                        "pull_singe_chain_uplink_body": {
                            "conversation_id": conversation_id,
                            "anchor_index": 9007199254740991,
                            "conversation_type": conversation_type,
                            "direction": 1,
                            "limit": 20,
                            "ext": {},
                            "filter": {"index_list": []},
                            "evaluate_ab_params": "",
                            "evaluate_common_params": "",
                            "option": {"lazy_load_strategy": 0},
                        }},
                    "sequence_id": str(uuid.uuid4()),
                    "channel": 2,
                    "version": "1",
                }
                headers = self._headers(local_conv_id=self._local_conv_id, im=True)
                try:
                    async with s.post(url, params=self._params(), json=body,
                                      headers=headers,
                                      timeout=aiohttp.ClientTimeout(total=30)) as r:
                        data = await r.json(content_type=None)
                except Exception:
                    continue

                if isinstance(data, dict) and data.get("status_code") not in (0, None):
                    self._poll_debug["last_status"] = (
                        data.get("status_code"), data.get("status_desc"))
                    continue

                dl = (data or {}).get("downlink_body") or {}
                self._poll_debug["last_downlink_keys"] = list(dl.keys())
                for msg in self._extract_messages(dl):
                    content = msg.get("content")
                    if isinstance(content, str):
                        try:
                            content = json.loads(content)
                        except json.JSONDecodeError:
                            continue
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        text = ((block.get("content") or {})
                                .get("text_block", {}).get("text", ""))
                        if text and CREDIT_FAIL_PATTERN.search(text):
                            raise DoubaoCreditError("额度不足: %s" % text[:60])
                        if block.get("block_type") != 2074:
                            continue
                        creations = ((block.get("content") or {})
                                     .get("creation_block", {}).get("creations") or [])
                        for cre in creations:
                            if cre.get("type") != 2:
                                continue
                            v = cre.get("video") or {}
                            u = (v.get("download_url") or v.get("url")
                                 or v.get("main_url") or "")
                            if u.startswith("http"):
                                return u
        raise Exception("豆包域视频生成超时未出片")


# ---------------- 账号池 ----------------

class DoubaoVideoPool:
    """豆包域账号池：轮询 + 失败换号 + 额度检测"""

    def __init__(self, cks: list, proxy: str = None,
                 video_timeout: int = 300, max_retry: int = 2):
        self.clients = []
        for ck in cks:
            c = DoubaoVideoClient(ck, proxy=proxy)
            if c.is_valid:
                self.clients.append(c)
        if not self.clients:
            raise ValueError("豆包域账号池为空（所有 CK 均无有效 sessionid）")
        self.video_timeout = video_timeout
        self.max_retry = max_retry
        random.shuffle(self.clients)

    @classmethod
    def from_dir(cls, ck_dir: str, proxy: str = None, **kw) -> "DoubaoVideoPool":
        """从目录读所有 *.txt（一行一个 CK）构建池"""
        cks = []
        for name in sorted(os.listdir(ck_dir)):
            if not name.endswith(".txt"):
                continue
            path = os.path.join(ck_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and "sessionid=" in line:
                            cks.append(line)
            except Exception:
                continue
        return cls(cks, proxy=proxy, **kw)

    @classmethod
    def auto_discover(cls, extra_dirs: list = None, proxy: str = None,
                      **kw) -> "DoubaoVideoPool":
        """自动发现豆包 CK 目录并建池

        搜索顺序（先找到的优先，全部合并）：
        1. 参数 extra_dirs
        2. 环境变量 DOLA_DOUBAO_CK_DIR
        3. 纯算注册机的落盘目录（默认位置）
        4. 本目录下 doubao_ck/

        这样纯算注册机扫码收集的 CK 会自动被 dola-pool 用上，
        无需手工搬运。
        """
        cands = list(extra_dirs or [])
        env = os.environ.get("DOLA_DOUBAO_CK_DIR")
        if env:
            cands.append(env)
        cands += [
            r"C:\Users\Administrator\Desktop\纯算注册\doubao_ck",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "doubao_ck"),
        ]

        seen, cks = set(), []
        for d in cands:
            if not d or not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if not name.endswith(".txt"):
                    continue
                try:
                    with open(os.path.join(d, name), encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and "sessionid=" in line and line not in seen:
                                seen.add(line)
                                cks.append(line)
                except Exception:
                    continue
        return cls(cks, proxy=proxy, **kw)

    async def generate_video(self, prompt: str, ratio: str = "9:16", duration: int = 5,
                             model: str = MODEL_SEEDANCE) -> str:
        """轮询出片：失败自动换号，额度不足直接跳过该号

        :param model: 模型名，默认 seedance_v2.0；可传 seedance_v2.5 等
        """
        last_err = None
        tried = 0
        for cli in self.clients:
            if tried >= self.max_retry + len(self.clients):
                break
            tried += 1
            try:
                return await cli.generate_video(prompt, ratio, duration,
                                                model=model,
                                                timeout=self.video_timeout)
            except DoubaoCreditError as e:
                last_err = e
                continue      # 额度不足：换号
            except DoubaoLoginError as e:
                last_err = e
                continue      # 登录失效：换号
            except Exception as e:
                last_err = e
                continue
        raise Exception(f"豆包域所有账号均失败: {last_err}")


__all__ = [
    "DoubaoVideoClient", "DoubaoVideoPool",
    "DoubaoLoginError", "DoubaoCreditError",
    "parse_ck", "ck_domain",
    "CN_DOMAIN", "CN_AID", "MODEL_SEEDANCE",
]
