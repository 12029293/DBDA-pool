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

# ★ 2026-10-13：模型 → 提示词里的自然语言标签。
#   HAR 实证（doubao2.har）：网页版提示词原文带「使用seedance2.5生成，1:1 , 15秒」，
#   即**模型、比例、时长都写在文本里**由服务端 Agent 解析；
#   `chat_ability.ability_param` 只是我们额外附带的兜底结构。
#   只写 ability_param、文本不带时长 → Agent 不知道时长，会反问确认（实测踩坑）。
MODEL_TEXT_LABEL = {
    "seedance_v2.5": "Seedance 2.5",
    "seedance_v2.0": "Seedance 2.0",
    "seedance_v2.0_std": "Seedance 2.0 标准版",
    "seedance_v2.0_mini": "Seedance 2.0 Mini",
}

# ★ 2026-10-13 新增：「高消耗生成确认卡」上报端点（HAR doubao2.har 实测）。
#   服务端在某些账号/参数组合下会在 SSE 里下发 InteractionAsk 确认卡
#   （"视频生成为高消耗生成，是否按上述方案开始生成？"），
#   不回应则任务停在等待确认状态，轮询只能等到超时。
#   回应端点：POST /alice/office/tool_local/upload_ask_human_result
ASK_HUMAN_PATH = "/alice/office/tool_local/upload_ask_human_result"
# 注意：host 必须**调用时**拼（跟随 CN_DOMAIN 变化，测试夹具要能改域名）
ASK_HUMAN_URL = f"https://{{host}}{ASK_HUMAN_PATH}"

# ★ 2026-10-13（HAR doubao2.har 实测定位）：确认卡是**消息块**，不是 SSE 事件。
#   前端源码原句：`if (10080 !== c.block_type && 10082 !== c.block_type) return;
#                 let u = c.content?.interaction_ask_block; tool_call_id = u?.clarify_id`
#   ⇒ 确认卡以 block_type=10080/10082（BLOCK_INTERACTION_ASK）形式挂在消息链里，
#     由 /im/chain/* 拉取，**不走 /chat/completion 的 SSE**。因此自动应答必须
#     在 `_poll_video` 扫消息块时做（那里本来就在扫 block_type=2074 取视频）。
INTERACTION_ASK_BLOCK_TYPES = (10080, 10082)

# 纯文本参数确认（App 端「视频生成参数确认 … 确认后我再开始生成视频。」）
TEXT_CONFIRM_PATTERNS = (
    "确认后我再开始生成",
    "确认后开始生成",
    "视频生成参数确认",
    "请确认后",
    "是否开始生成",
    "确认无误后",
)
# 自动应答用的用户侧文本（纯文本确认路径）
TEXT_CONFIRM_REPLY = "确认，请按上述参数开始生成视频"

# ★ 2026-10-14：出片轮询超时（秒）。
#   上游对 30 秒片的原话：「本次使用 Seedance 2.5 生成，**预计等待 15 分钟**」
#   （SSE 实测原文，见交接文档）。旧的 300 秒必然在出片前超时，
#   即使确认卡全部答对也拿不到片。默认 900 秒，可用 DOUBAO_VIDEO_TIMEOUT 覆盖。
POLL_TIMEOUT_SEC = int(os.environ.get("DOUBAO_VIDEO_TIMEOUT", "900"))

# ★ 2026-10-14：轮询期「补发确认」的重试间隔（按轮询轮数计）。
#   补发确认可能被上游偶发拒绝（见 `_confirm_response_ok`），旧实现只发一次，
#   失败就永久失去机会。现在每 CONFIRM_RETRY_POLLS 轮重试一次；
#   默认 6 轮 ≈ 30 秒（poll_interval 默认 5s），避免把会话刷满。
CONFIRM_RETRY_POLLS = int(os.environ.get("DOUBAO_CONFIRM_RETRY_POLLS", "6"))

# ★ 2026-10-14：单次生成里「补发确认」的**总发送次数上限**。
#   重试是必要的（上游会偶发丢弃），但必须有闸门——否则上游持续拒绝时
#   会把会话刷满（每次 3 个内联重试 × 每 6 轮一次 × 900 秒）。
#   正常情况下第 1 次就受理，6 次足够；超限后放弃并留痕。
CONFIRM_MAX_SENDS = int(os.environ.get("DOUBAO_CONFIRM_MAX_SENDS", "6"))

# ★ 2026-10-13（真号实测）：图生视频会下发 **button_block(10103)** 确认卡
#   （scene=creation_portrait_video_auth_confirm 素材授权 /
#     gen_video_by_downgraded_model 降级模型），点击协议走客户端 im 通道，
#   **纯 HTTP 无法回传**（HAR/前端 bundle 均无 web 提交端点；纯文本确认无效）。
#   轮询检测到它时提前抛错（可识别、面板可提示用户去 App 点一下），
#   不再傻等整个 timeout。
BUTTON_BLOCK_TYPE = 10103
BUTTON_NEEDS_APP_SCENE_PREFIX = (
    "creation_portrait_video_auth_confirm",   # 素材授权确认
    "gen_video_by_downgraded_model",          # 降级模型确认
    "creation_ref_video_exceed_confirm",
    "creation_paid_confirm_video",
    "create_avatar_with_reply_blocked_creation",
)


class DoubaoButtonConfirmError(Exception):
    """图生视频遇到「确认生成」按钮卡（10103），需人工在豆包 App 点击。"""
    pass


# ★ 2026-10-14（真号实测）：「付费额度」确认卡是 10103 里的一类**独立**情形——
#   账号**今日免费额度已用完**时，上游会先回文本
#     「今日视频生成免费额度已用完，本次将消耗付费额度。是否继续生成？」
#   再挂一张 10103 按钮卡（scene=gen_video_by_downgraded_model）。
#   它与「素材授权」卡的区别很关键：
#     · 素材授权 → 账号级一次性，点完永久生效（点一次即可，属"人工一次性"）
#     · 付费确认 → **每次免费额度耗尽后都会重现**，且只有客户端能点
#   ⇒ 号池的正确处置不是「等人点一下」，而是**把这个号暂时挪出调度**，
#     换一个仍有免费额度的号出片（多号池的核心价值）。
BUTTON_PAID_SCENES = (
    "gen_video_by_downgraded_model",     # 付费/降级确认
    "creation_paid_confirm_video",
)
PAID_QUOTA_TEXT_HINTS = ("免费额度已用完", "消耗付费额度", "是否继续生成")


class DoubaoPaidQuotaError(DoubaoButtonConfirmError):
    """付费额度确认卡：今日免费额度已用完，需在豆包 App/网页点「确认生成」同意付费。

    继承 `DoubaoButtonConfirmError`（保持既有 except 兼容），但池层会**单独归类**为
    `paid` 并把该账号临时挪出调度（见 `doubao_pool.PAID_QUOTA_COOLDOWN_SEC`）。
    """
    def __init__(self, message: str, scene: str = ""):
        super().__init__(message)
        self.scene = scene

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
        self._text_confirm_err = ""
        # ★ 2026-10-14：流内只**标记**「需要补发确认」，不立刻发请求——
        #   流未关闭时发的确认会被上游静默丢弃（详见 _send_text_confirmation）。
        self._pending_text_confirm = False
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

    @staticmethod
    def _video_prompt_text(prompt: str, ratio: str, duration: int,
                           model: str) -> str:
        """★ 组装视频提示词（2026-10-13 对齐网页版实测格式）。

        HAR 原文（doubao2.har）：
          「[图](attachment://…) 图片中的小猫…使用seedance2.5生成，1:1 , 15秒
           [Seedance 2.5](skill://seedance-25?type=2&id=…)」

        ⇒ **模型、比例、时长都写进文本**，服务端 Agent 靠自然语言解析；
          ability_param 只是兜底。旧实现只写「生成影片：{prompt}，{ratio}」
          不带时长 → Agent 不知道要几秒，会先反问参数确认（实测踩坑：
          App 上出现「视频生成参数确认…15秒」却等用户手点）。
        """
        label = MODEL_TEXT_LABEL.get(model, "Seedance 2.5")
        return (f"生成影片：{prompt}，使用{label}生成，"
                f"{ratio}，{int(duration)}秒")

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
        video_prompt = self._video_prompt_text(prompt, ratio, duration, model)
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

    async def _read_sse(self, resp: aiohttp.ClientResponse,
                        on_event=None) -> list:
        """读取 SSE 流，返回 [(event_name, data_dict), ...]

        ★ 2026-10-11 容错修复：豆包域 CDN（volc-dcdn）的 SSE 流偶发
          `ClientPayloadError: Not enough data to satisfy transfer length header`
          （chunked 尾段被 CDN 提前关闭）。原实现用 `async for chunk in resp.content`
          会把该异常抛出，导致**明明已拿到 SSE_ACK + 真实 conversation_id 的会话
          被整段丢弃**（表现为「提交即失败」，实际受理成功）。
          改为 `iter_any()` + 捕获异常，保留已读事件——出片受理判定只需
          SSE_ACK 里的 conversation_id，后续凭 conv_id 轮询即可。

        ★ 2026-10-13 二次修复（确认卡即时应答）：新增 `on_event(name, data)` 回调，
          **每解析出一个事件立即回调**。原因：服务端下发「高消耗生成确认卡」后会
          **挂住 SSE 流等待回应**——等整条流读完再扫描确认卡的旧实现永远等不到
          流尾（120s 读超时先到），确认卡根本没机会被应答。回调里抛异常不影响
          主流程（确认应答失败时走轮询兜底）。
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
                    if on_event is not None:
                        try:
                            await on_event(name, data)
                        except Exception:
                            pass  # 确认卡应答失败不阻断读流（轮询兜底）
        except Exception as exc:  # 连接被 CDN 截断：保留已读事件
            self._read_sse_warn = "%s: %s" % (type(exc).__name__, str(exc)[:120])
        return events

    # ---------- 「高消耗生成确认卡」自动应答（2026-10-13 新增 / 2026-10-13 二次修复） ----------
    #
    # ★ 二次修复背景（真号实测：App 显示提示词已发出、任务一直卡「生成中」）：
    #   1) 旧实现在**整条 SSE 读完后**才扫描确认卡——但服务端下发确认卡后会
    #      挂住流等回应，`_read_sse` 读不到流尾，扫描代码根本执行不到，
    #      最终把 120s 读超时/300s 轮询超时耗光（meta 里留下「超时未出片」）。
    #      ⇒ 修复：`_read_sse` 增加 on_event 回调，**每解析出一个事件立即应答**。
    #   2) 旧实现只认 question_id="confirm_generate" 且回**写死的**回显结构；
    #      服务端实际会下发多种确认问题（参数确认/高消耗确认/素材授权…），
    #      写死结构会答非所问被静默拒绝。
    #      ⇒ 修复：递归识别任意含 clarify_id+questions 的确认块，**原样回传**
    #        服务端下发的 block，只注入 answer（选第一个非取消选项）+ status=2。
    #   3) 旧实现应答失败仅吞异常、clarify 已记账永不重试。
    #      ⇒ 修复：应答校验响应体，失败原地重试 3 次；超时报错里带上诊断。

    @staticmethod
    def _collect_ask_blocks(node, out: list, depth: int = 0) -> None:
        """递归收集任意形态的「确认问答块」（含 clarify_id + questions 的 dict）。

        确认块可能出现在：
          · SSE 事件 data 里（ask_human_result.InteractionAsk.interaction_ask_block_json
            —— 二次序列化 JSON 字符串）；
          · 消息块 content.interaction_ask_block（dict，/im/chain 轮询）。
        depth 限制防止异常深嵌套（消息链理论上不深，防御性兜底）。
        """
        if depth > 12:
            return
        if isinstance(node, dict):
            if "clarify_id" in node and isinstance(node.get("questions"), list):
                out.append(node)
            for v in node.values():
                DoubaoVideoClient._collect_ask_blocks(v, out, depth + 1)
        elif isinstance(node, list):
            for v in node:
                DoubaoVideoClient._collect_ask_blocks(v, out, depth + 1)

    @staticmethod
    def _find_confirm_asks(data) -> list[tuple[str, str, str]]:
        """从 SSE 事件 / 消息数据里提取全部确认卡。

        返回 [(tool_call_id, clarify_id, 原始block_json), ...]（按 clarify_id 去重）。
        原始 block_json 供应答时**原样回传**（只注入 answer），避免答非所问。
        tool_call_id：优先取块外层包裹的 tool_call_id，否则回落 clarify_id
        （前端源码：`tool_call_id = u?.clarify_id ?? toolCallIds[0]`）。
        """
        if not isinstance(data, dict):
            return []
        asks: list[tuple[str, str, str]] = []
        seen: set[str] = set()

        def emit(block: dict, outer_tool_call_id: str = "") -> None:
            clarify = str(block.get("clarify_id") or "")
            if not clarify or clarify in seen:
                return
            seen.add(clarify)
            tc = str(outer_tool_call_id or block.get("tool_call_id") or clarify)
            try:
                raw = json.dumps(block, ensure_ascii=False)
            except Exception:
                raw = ""
            asks.append((tc, clarify, raw))

        # 形态 A：SSE 事件 data.ask_human_result.InteractionAsk.interaction_ask_block_json
        ahr = data.get("ask_human_result")
        if isinstance(ahr, dict):
            tc = str(ahr.get("tool_call_id") or "")
            ia = ahr.get("InteractionAsk") or {}
            if isinstance(ia, dict):
                blk = ia.get("interaction_ask_block_json")
                if isinstance(blk, str) and blk.strip():
                    try:
                        inner = json.loads(blk)
                    except Exception:
                        m = re.search(r'"clarify_id"\s*:\s*"([^"]+)"', blk)
                        if m:  # 内层 JSON 损坏时至少保住 clarify_id
                            emit({"clarify_id": m.group(1),
                                  "questions": [1]}, tc)
                    else:
                        if isinstance(inner, dict):
                            emit(inner, tc)
                elif isinstance(ia.get("questions"), list):
                    emit(ia, tc)
        # 形态 B：扁平 data.tool_call_id + data.interaction_ask_block_json
        if not asks:
            blk = data.get("interaction_ask_block_json")
            if isinstance(blk, str) and blk.strip():
                try:
                    inner = json.loads(blk)
                except Exception:
                    inner = None
                if isinstance(inner, dict):
                    emit(inner, str(data.get("tool_call_id") or ""))
        # 形态 C：dict 递归（消息块 content.interaction_ask_block 等任意位置）
        if not asks:
            blocks: list[dict] = []
            DoubaoVideoClient._collect_ask_blocks(data, blocks)
            for b in blocks:
                emit(b, str(data.get("tool_call_id") or ""))
        return asks

    @classmethod
    def _find_confirm_ask(cls, data) -> tuple[str, str] | None:
        """兼容旧签名：返回第一张确认卡的 (tool_call_id, clarify_id) 或 None。"""
        asks = cls._find_confirm_asks(data)
        return (asks[0][0], asks[0][1]) if asks else None

    async def _answer_confirm_generate(self, tool_call_id: str, clarify_id: str,
                                       ask_block_json: str = "") -> bool:
        """回应「视频生成为高消耗生成，是否按上述方案开始生成？」确认卡。

        HAR 实测（doubao2.har:139632）：
          - 端点 POST /alice/office/tool_local/upload_ask_human_result
          - interaction_ask_block_json 是**二次序列化的 JSON 字符串**
          - 关键：answer.status=2 + selected_option_ids=["generate"]，顶层 status=2
        ★ 2026-10-13 修复：优先**原样回传**服务端下发的 block（ask_block_json），
          只注入 answer（每个问题选第一个非取消选项）与顶层 status=2；
          服务端下发的 question_id/option_id 可能随场景变化（confirm_generate
          只是其中一种），写死会答非所问被静默拒绝。
          拿不到原始 block 时才退回 confirm_generate 硬编码结构（兼容旧抓包形态）。
          返回 True/False 表示服务端是否接受；失败原地重试 3 次。
        """
        inner: dict | None = None
        if ask_block_json:
            try:
                parsed = json.loads(ask_block_json)
                if isinstance(parsed, dict) and isinstance(parsed.get("questions"), list):
                    inner = parsed
            except Exception:
                inner = None
        if inner is None:
            # 兜底：HAR doubao2.har 的 confirm_generate 形态
            inner = {
                "clarify_id": clarify_id,
                "questions": [{
                    "question_id": "confirm_generate",
                    "type": 1,
                    "title": "视频生成为高消耗生成，是否按上述方案开始生成？",
                    "required": True,
                    "options": [
                        {"label": "按要求生成",
                         "description": "视频生成为高消耗生成，确认后立即提交生成任务",
                         "option_id": "generate"},
                        {"label": "暂不生成",
                         "description": "暂不生成，可调整方案后再确认",
                         "option_id": "cancel"},
                    ],
                    "question_capability": {"allow_text": True,
                                            "text_placeholder": "自定义描述..."},
                }],
            }
        # 注入 clarify_id（服务端可能省略）与每个问题的 answer
        if not str(inner.get("clarify_id") or ""):
            inner["clarify_id"] = clarify_id
        for q in inner.get("questions") or []:
            if not isinstance(q, dict):
                continue
            opts = [o for o in (q.get("options") or []) if isinstance(o, dict)]
            pick = next((o.get("option_id") for o in opts
                         if str(o.get("option_id") or "") not in ("cancel", "no")),
                        (opts[0].get("option_id") if opts else "generate"))
            q["answer"] = {"status": 2,
                           "selected_option_ids": [pick or "generate"],
                           "question_id": q.get("question_id") or ""}
        inner["status"] = 2
        body = {
            "ask_human_result": {
                "tool_call_id": tool_call_id,
                "InteractionAsk": {
                    "interaction_ask_block_json": json.dumps(inner, ensure_ascii=False),
                },
            }
        }
        headers = {**self._headers(), "agw-js-conv": "str"}
        last_err = ""
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(**self._sess_kw()) as s:
                    async with s.post(
                            f"https://{CN_DOMAIN}{ASK_HUMAN_PATH}",
                            params=self._params(), json=body, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=15)) as r:
                        txt = await r.text()
                # ★ 校验服务端是否真的接受（旧实现只发不验，被拒绝也当成功）
                detail = txt[:160].replace("\n", " ")
                try:
                    j = json.loads(txt)
                    code = j.get("code") if isinstance(j, dict) else None
                except Exception:
                    code = 0 if r.status == 200 else r.status
                if r.status == 200 and code in (0, None):
                    self.confirm_answered = True
                    self._poll_debug["answered_clarify"] = clarify_id
                    return True
                last_err = f"HTTP {r.status} code={code} {detail}"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"[:160]
            if attempt < 2:
                await asyncio.sleep(1.5)
        # ★ 失败必须留痕：允许后续轮询阶段对重发的卡再次应答（不记 answered）
        self._confirm_answer_err = last_err
        self._poll_debug["confirm_answer_err"] = last_err
        return False

    # ---------- 确认卡：消息块形态 + 纯文本形态（2026-10-13 新增） ----------

    async def _handle_interaction_ask_block(self, block: dict) -> bool:
        """扫消息链里的确认卡块并应答；命中返回 True。

        ★ HAR 实证：确认卡 = block_type 10080/10082 的消息块，
          clarify_id 在 content.interaction_ask_block 里（不是 SSE 事件）。
          同一个 clarify_id 只应答一次（避免每轮轮询重复提交）。
        ★ 2026-10-13：应答改为**原样回传** interaction_ask_block（只注入
          answer），成功才记 answered（失败时下一轮轮询可重试）。
        """
        if not isinstance(block, dict):
            return False
        if block.get("block_type") not in INTERACTION_ASK_BLOCK_TYPES:
            return False
        iab = ((block.get("content") or {}).get("interaction_ask_block") or {})
        if not isinstance(iab, dict):
            return False
        clarify_id = str(iab.get("clarify_id") or "")
        if not clarify_id:
            return False
        # 状态：1=Unanswered 处理中才应答；2=Finish 已答过，跳过
        status = iab.get("status")
        try:
            status_i = int(status) if status is not None else None
        except (TypeError, ValueError):
            status_i = None
        if status_i is not None and status_i not in (0, 1):
            return False
        if clarify_id in self._answered_clarify:
            return False
        # tool_call_id 通常等于 clarify_id（前端源码：u?.clarify_id ?? toolCallIds[0]）
        tool_call_id = str(iab.get("tool_call_id") or clarify_id)
        try:
            raw = json.dumps(iab, ensure_ascii=False)
        except Exception:
            raw = ""
        self._answered_clarify.add(clarify_id)   # 先记账防同轮重复提交
        ok = await self._answer_confirm_generate(tool_call_id, clarify_id, raw)
        if not ok:
            self._answered_clarify.discard(clarify_id)  # 失败允许下轮重试
            return False
        self._poll_debug["answered_clarify"] = clarify_id
        return True

    @staticmethod
    def _needs_text_confirm(text: str) -> bool:
        """App 端纯文本参数确认（「…确认后我再开始生成视频。」）"""
        if not text:
            return False
        return any(p in text for p in TEXT_CONFIRM_PATTERNS)

    @staticmethod
    def _confirm_response_ok(events: list, conversation_id: str) -> tuple:
        """判定「补发确认」是否被上游**真正受理**（返回 (ok, reason)）。

        ★ 2026-10-14 新增。背景（真号实测）：
          旧实现发完确认请求后**不看响应**，直接 `self.text_confirmed = True`。
          而真实情况是：如果这条确认是在**原请求的 SSE 流还没关闭时**发出的，
          上游会**静默丢弃**它——不报错、不落库、不排产。于是：

              流内发送 → 被丢弃 → 却已置 text_confirmed=True
                       → 流尾兜底 / 轮询兜底全部被 `not self.text_confirmed` 跳过
                       → 必定「超时未出片」

          证据（只读拉会话 `_diag_conv_inspect.py`）：
            · 失败会话 38447095680162818 只有 2 条消息（用户提示词 + 助手参数确认），
              **我补发的「确认」根本不在会话里**；
            · 对照手工补发（等流关闭后再发，见 _diag_confirm_db001.log）：
              HTTP 200 + 91 个 SSE 事件（含 SSE_ACK）→ 会话 2→4 条 → 开始排产出片。

        受理特征（对齐成功样本）：
          · SSE_ACK（ack_client_meta.conversation_id 与本会话一致）——最强信号；
          · 或出现任意「生产性」事件（FULL_MSG_NOTIFY / STREAM_MSG_NOTIFY /
            DOWNLINK_CMD / CHUNK_DELTA）。
        失败特征：
          · gateway-error / STREAM_ERROR（含限流码）→ 直接判失败；
          · 只有 SSE_HEARTBEAT 或**一个事件都没有** → 典型的「被静默丢弃」。
        """
        ack_conv = ""
        productive = 0
        heartbeats = 0
        for name, data in (events or []):
            if name in ("gateway-error", "STREAM_ERROR"):
                msg = json.dumps(data, ensure_ascii=False) if data else ""
                return False, "上游拒绝: %s" % msg[:140]
            if not isinstance(data, dict):
                continue
            if name == "SSE_ACK":
                ack_conv = ((data.get("ack_client_meta") or {})
                            .get("conversation_id") or "")
                productive += 1
            elif name == "SSE_HEARTBEAT":
                heartbeats += 1
            elif name in ("FULL_MSG_NOTIFY", "STREAM_MSG_NOTIFY",
                          "DOWNLINK_CMD", "STREAM_CHUNK", "CHUNK_DELTA"):
                productive += 1
        if ack_conv and conversation_id and ack_conv != conversation_id:
            return False, "SSE_ACK 会话号不一致(%s≠%s)" % (ack_conv, conversation_id)
        if ack_conv:
            return True, ""
        if productive:
            return True, ""
        if heartbeats:
            return False, "只有心跳无受理事件（疑似被静默丢弃）"
        return False, "无任何 SSE 事件（疑似被静默丢弃）"

    async def _send_text_confirmation(self, conversation_id: str,
                                      attempts: int = 3) -> bool:
        """在**同一会话**里补发一条「确认」文本，推进被卡住的生成。

        与 InteractionAsk 应答不同：这是用户在对话里回一句话。
        复用出片 body 骨架 → 改成同会话续聊（conversation_id 带上、
        need_create_conversation=False）并去掉 chat_ability（纯文本）。

        ★★ 调用时机是**成败关键**（2026-10-14 实测）：
          必须在**原请求的 SSE 流已经关闭之后**再发。网页版的真实时序就是
          「助手回完『请先确认参数』→ 本轮结束 → 用户再发一条消息」。
          在流未关闭时发 → 上游静默丢弃（见 `_confirm_response_ok` 文档）。
          故 `generate_video` 只允许在 `_read_sse` 返回后 / `_poll_video` 轮询期调用。

        ★ 响应校验：只有 `_confirm_response_ok` 判为受理才置 `text_confirmed=True`；
          否则保持 False 并留痕，允许流尾/轮询阶段重试。失败原地重试 `attempts` 次。
        """
        if not conversation_id or conversation_id == "0":
            self._poll_debug["text_confirm_err"] = "无有效 conversation_id"
            return False
        if not hasattr(self, "_confirm_send_count"):
            self._confirm_send_count = 0
        if self._confirm_send_count >= CONFIRM_MAX_SENDS:
            self._poll_debug["text_confirm_err"] = (
                "已达发送上限 %d 次，停止重试" % CONFIRM_MAX_SENDS)
            return False
        last_err = ""
        for attempt in range(1, max(1, int(attempts)) + 1):
            if self._confirm_send_count >= CONFIRM_MAX_SENDS:
                break
            self._confirm_send_count += 1
            try:
                body = self._build_video_body(TEXT_CONFIRM_REPLY, "16:9", 5,
                                              MODEL_SEEDANCE)
                body.pop("chat_ability", None)
                body["client_meta"]["conversation_id"] = conversation_id
                body["option"]["need_create_conversation"] = False
                body["option"]["conversation_init_option"] = {}
                body["option"]["conversation_init_ext"] = {}
                body["messages"][0]["content_block"][0]["content"]["text_block"]["text"] \
                    = TEXT_CONFIRM_REPLY
                headers = self._headers(local_conv_id=self._local_conv_id)
                async with aiohttp.ClientSession(**self._sess_kw()) as s:
                    async with s.post(f"https://{CN_DOMAIN}/chat/completion",
                                      params=self._params(), json=body,
                                      headers=headers,
                                      timeout=aiohttp.ClientTimeout(total=60)) as r:
                        http_status = r.status
                        events = await self._read_sse(r)
                ok, reason = self._confirm_response_ok(events, conversation_id)
                self._poll_debug["text_confirm_http"] = http_status
                self._poll_debug["text_confirm_events"] = len(events or [])
                if ok:
                    self.text_confirmed = True
                    self._poll_debug["text_confirm"] = (
                        "已补发「%s」http=%s events=%d 第%d次"
                        % (TEXT_CONFIRM_REPLY, http_status, len(events or []),
                           attempt))
                    return True
                last_err = reason
            except Exception as exc:
                last_err = "%s: %s" % (type(exc).__name__, str(exc)[:120])
            self._poll_debug["text_confirm_err"] = (
                "attempt%d 未受理: %s" % (attempt, last_err))
            if attempt < attempts:
                await asyncio.sleep(2.0)
        self._text_confirm_err = last_err
        self._poll_debug["text_confirm_err"] = (
            "全部%d次未受理: %s" % (attempts, last_err))
        return False

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
                             timeout: int = None, on_poll=None) -> str:
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
        self.confirm_answered = False   # 「高消耗确认卡」每次生成重置
        self._answered_clarify = set()  # 已应答的 clarify_id（消息块形态去重）
        self.text_confirmed = False     # 纯文本确认是否已补发（仅受理成功才置 True）
        self._pending_text_confirm = False  # 已见到「参数确认」提示，待补发
        self._text_confirm_err = ""
        self._confirm_send_count = 0        # 本次生成已发出的确认请求数
        aio_kw = {"params": params} if params else {}
        sse_text = ""   # 流内累积的助手文本（用于即时识别「参数确认」）

        async def _on_sse_event(name: str, data) -> None:
            """★ 流内即时应答确认卡（2026-10-13 修复）。

            服务端下发确认卡后挂住 SSE 流等回应——旧实现等流读完才扫描，
            永远等不到（120s 读超时先到），任务卡「生成中」直至轮询超时。
            现在每个事件到达时立刻识别并应答，应答成功后服务端恢复推流。

            ★ 2026-10-14 追加：**纯文本参数确认**也由本回调识别，但**只标记不发送**。
            实测（真号 30 秒片）助手先回一段纯文本：
              「…**视频生成参数确认** 模型/时长/比例… **确认后我再开始生成视频。**」
            服务端发完这段就结束本轮、等用户回话。
              · 若在本回调里立刻补发「确认」→ 原流还没关闭 → 上游静默丢弃
                （2026-10-14 定位的真根因，详见 `_confirm_response_ok`）；
              · 正确时序：本轮结束（`_read_sse` 返回）后再发，见下方流尾兜底。
            """
            nonlocal conv_id, sse_text
            if not isinstance(data, dict):
                return
            asks = self._find_confirm_asks(data)
            for tc, clarify, raw in asks:
                if clarify in self._answered_clarify:
                    continue
                self._answered_clarify.add(clarify)   # 先记账防重复应答
                ok = await self._answer_confirm_generate(tc, clarify, raw)
                if not ok:
                    # 应答失败：允许轮询阶段对重发的卡再答（从去重集合移除）
                    self._answered_clarify.discard(clarify)
            # —— 纯文本参数确认：流内**只标记**，绝不在此发请求 ——
            #    ★ 2026-10-14 修复：原实现在这里直接 `_send_text_confirmation`，
            #      但此刻原 SSE 流**尚未关闭**，上游对「同一会话并发第二条
            #      /chat/completion」是**静默丢弃**（不报错、不落库）。
            #      更致命的是旧实现发完就无条件 `text_confirmed=True`，
            #      使流尾兜底/轮询兜底全部被跳过 → 必然超时未出片。
            #      实测对照见 `_confirm_response_ok` 文档与 _diag_conv_inspect.py。
            sse_text = (sse_text + self._text_deltas(data))[-4000:]
            if self._needs_text_confirm(sse_text):
                self._pending_text_confirm = True
                self._poll_debug["param_confirm_seen_in_stream"] = True

        async with aiohttp.ClientSession(**self._sess_kw()) as s:
            async with s.post(url, json=body, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=120),
                              **aio_kw) as resp:
                _t_stream = time.time()
                events = await self._read_sse(resp, on_event=_on_sse_event)
                # ★ 首轮流耗时：确认请求必须等它结束才发（见 _send_text_confirmation）
                self._poll_debug["stream_sec"] = round(time.time() - _t_stream, 2)

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
            # 兜底：流内即时应答已覆盖常规路径；此处再扫一遍残留在
            # events 里的确认卡（如应答曾失败、服务端重发后已可再答）。
            for ask in (self._find_confirm_asks(data)
                        if isinstance(data, dict) else []):
                if ask[1] not in self._answered_clarify:
                    self._answered_clarify.add(ask[1])
                    if await self._answer_confirm_generate(ask[0], ask[1], ask[2]):
                        continue
                    self._answered_clarify.discard(ask[1])
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

        # ★ 兜底（**这是主路径**）：本轮 SSE 已关闭。注意必须在此刻才发——
        #   流未关闭时发的确认会被上游静默丢弃（2026-10-14 实测根因）。
        #   不确认，上游就一直不排产，最后必然超时未出片。
        stream_text = sse_text
        for _n, _d in events:
            if isinstance(_d, dict):
                stream_text += self._text_deltas(_d)
        stream_text = (stream_text + " " + srv_text)[-4000:]
        if self._needs_text_confirm(stream_text):
            self._pending_text_confirm = True
        self._poll_debug["stream_text_len"] = len(stream_text)
        if self._pending_text_confirm and not self.text_confirmed:
            await self._send_text_confirmation(conv_id)
        self._poll_debug["confirmed_after_stream"] = self.text_confirmed

        return await self._poll_video(conv_id, timeout,
                                      conversation_type=self._conv_type,
                                      on_poll=on_poll)

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

    @staticmethod
    def _message_blocks(msg: dict) -> list:
        """★ 2026-10-14 修复：取一条消息的内容块，**两种消息体格式都认**。

        上游把消息体换成了 `content_block`（实测 `message_body_version="1"`、
        ext 里 `use_content_block="1"`）：

            {"content_type": 9999, "content": "",              ← 旧实现只读这里
             "content_block": [{"block_type": 10000, ...}]}   ← 真实内容在这里

        旧实现（`_poll_video`）只读 `msg["content"]`：新格式下它是**空字符串**，
        `json.loads("")` 抛错 → `continue` → **整轮消息一个块都没扫**。
        后果（2026-10-13/14 两条失败任务的真正根因）：
          · 视频块 2074 永远扫不到 → 必然「超时未出片」；
          · 纯文本参数确认（「…确认后我再开始生成视频」）扫不到 → 一直不确认；
          · 10103 按钮卡扫不到 → 连「需人工确认」都提示不出来；
          · 诊断只剩 last_downlink_keys（消息明明有 2~4 条）。

        兼容策略：优先 `content_block`（新版 list）；缺失时回落 `content`
        （旧版是**二次序列化的 JSON 字符串**，也可能直接是 list）。
        """
        if not isinstance(msg, dict):
            return []
        blocks = msg.get("content_block")
        if isinstance(blocks, list):
            return [b for b in blocks if isinstance(b, dict)]
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            try:
                content = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                return []
        if isinstance(content, list):
            return [b for b in content if isinstance(b, dict)]
        return []

    @staticmethod
    def _text_deltas(data) -> str:
        """从任意 SSE 事件里抽文本增量（用于流内识别纯文本参数确认）。

        覆盖形态：`CHUNK_DELTA{"text":…}` / `STREAM_CHUNK.patch_op[].patch_value`
        里的 `text_block.text` / `STREAM_MSG_NOTIFY.content.content_block[].text`。
        只认 `text` 字段（不认 `content`），避免把整段 JSON 拼进缓冲区。
        """
        out: list[str] = []

        def walk(node, depth: int = 0) -> None:
            if depth > 14:
                return
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "text" and isinstance(v, str):
                        out.append(v)
                    else:
                        walk(v, depth + 1)
            elif isinstance(node, list):
                for v in node:
                    walk(v, depth + 1)

        walk(data)
        return "".join(out)

    async def _poll_video(self, conversation_id: str, timeout: int = None,
                          conversation_type: int = 3, on_poll=None) -> str:
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
          downlink_body.pull_singe_chain_downlink_body.messages[]
            → `content_block`（新版）或 `content`（旧版 JSON 字符串）
            → block_type == 2074（creation_block）
            → creations[].video.download_url
        实测成功样本：384x682 / 5.057s / 851,428 字节 MP4。
        """
        timeout = int(timeout or POLL_TIMEOUT_SEC)
        start = time.time()
        # 兜底初始化（image 客户端等直接调用 _poll_video 时也要有这些状态）
        if not hasattr(self, "_answered_clarify"):
            self._answered_clarify = set()
        if not hasattr(self, "text_confirmed"):
            self.text_confirmed = False
        if not hasattr(self, "_text_confirm_err"):
            self._text_confirm_err = ""
        if not hasattr(self, "_pending_text_confirm"):
            self._pending_text_confirm = False
        polls = 0
        # 轮询期定期重试补发确认：本轮生成从「没发过」开始计
        self._last_confirm_poll = -10 ** 9
        url = f"https://{CN_DOMAIN}/im/chain/single"
        async with aiohttp.ClientSession(**self._sess_kw()) as s:
            while time.time() - start < timeout:
                await asyncio.sleep(float(getattr(self, "poll_interval", 5)))
                polls += 1
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
                # ★ 心跳：出片要等 5~15 分钟，任务库必须看到「还在轮询」，
                #   否则看门狗（processing 且超过 STALE_TASK_SECONDS 无更新）
                #   会把还在正常生成的任务当僵尸重派，白白重复出片+重复扣额度。
                if on_poll is not None:
                    try:
                        on_poll(time.time())
                    except Exception:
                        pass
                self._poll_debug["last_downlink_keys"] = list(dl.keys())
                block_seen: dict[int, int] = {}
                for msg in self._extract_messages(dl):
                    # ★ 2026-10-14：块统一从 `content_block`（新版）/`content`（旧版）
                    #   取，见 `_message_blocks`。旧实现只读 content（新版恒为空串）
                    #   → 一个块都扫不到，必超时。
                    for block in self._message_blocks(msg):
                        bt = block.get("block_type")
                        if isinstance(bt, int):
                            block_seen[bt] = block_seen.get(bt, 0) + 1
                        text = ((block.get("content") or {})
                                .get("text_block", {}).get("text", ""))
                        if text:
                            self._poll_debug["last_text"] = text[:120]
                        # ★ 2026-10-14：记住「免费额度已用完」原文——它和 10103
                        #   按钮卡在同一轮消息里，但**块顺序不保证**，先记下来，
                        #   按钮分支再取用，避免依赖先后次序。
                        if text and any(h in text for h in PAID_QUOTA_TEXT_HINTS):
                            self._poll_debug["paid_quota_text"] = text[:140]
                        if text and CREDIT_FAIL_PATTERN.search(text):
                            raise DoubaoCreditError("额度不足: %s" % text[:60])
                        # ★ 2026-10-13：确认卡（消息块形态，block_type 10080/10082）
                        #   命中即自动应答，然后继续本轮扫描（同一轮里可能已有视频块）。
                        if await self._handle_interaction_ask_block(block):
                            continue
                        # ★ 纯文本确认（App 端「…确认后我再开始生成视频。」）
                        #   2026-10-14：这里**只标记**，补发统一放到本轮消息扫完之后
                        #   （见下方），避免 `continue` 跳过同一轮里的 2074 视频块。
                        if text and self._needs_text_confirm(text):
                            self._pending_text_confirm = True
                        # ★ 2026-10-13：button_block(10103) 授权/降级确认卡——
                        #   HTTP 无法代点（协议在客户端 im 通道），标记并提前失败。
                        if bt == BUTTON_BLOCK_TYPE:
                            bb = (block.get("content") or {}).get("button_block") or {}
                            scene = str(bb.get("scene") or "")
                            if scene.startswith(BUTTON_NEEDS_APP_SCENE_PREFIX):
                                self._poll_debug["stuck_button"] = scene
                                quota_hint = str(
                                    self._poll_debug.get("paid_quota_text") or "")
                                if scene in BUTTON_PAID_SCENES:
                                    # 付费额度确认：换号可解（多号池）→ 单独归类
                                    raise DoubaoPaidQuotaError(
                                        "今日免费额度已用完，需在豆包 App/网页点击"
                                        "「确认生成」同意消耗付费额度（scene=%s）"
                                        "%s" % (scene,
                                                ("；上游原文：" + quota_hint[:80])
                                                if quota_hint else ""),
                                        scene=scene)
                                raise DoubaoButtonConfirmError(
                                    "豆包下发「确认生成」按钮卡（scene=%s），"
                                    "请在豆包 App/网页里打开该会话点击确认后重试；"
                                    "该确认操作仅客户端可完成" % scene)
                        if bt != 2074:
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
                if block_seen:
                    # 轮询诊断：这一轮到底扫到了哪些块（区分「上游还在生成」
                    # 与「扫不到块」——后者正是 2026-10-14 之前的真故障）
                    self._poll_debug["blocks"] = dict(sorted(block_seen.items()))
                # ★ 2026-10-14：本轮扫完后，定期重试补发「确认」。
                #   旧实现只发一次，一旦上游静默丢弃就永久失去机会 → 必然超时；
                #   现在按 CONFIRM_RETRY_POLLS 轮间隔重试，且**每次校验响应**，
                #   只有真正受理才置 text_confirmed=True 停止重试。
                if (self._pending_text_confirm and not self.text_confirmed
                        and polls - self._last_confirm_poll
                        >= CONFIRM_RETRY_POLLS):
                    self._last_confirm_poll = polls
                    self._poll_debug["confirm_retry_poll"] = polls
                    await self._send_text_confirmation(conversation_id)
        # ★ 超时必须带诊断：确认卡是否已答/应答失败原因/最后轮询状态，
        #   否则「卡生成中超时」无法区分是限流、确认卡未答还是上游真的慢。
        self._poll_debug["text_confirmed"] = self.text_confirmed
        self._poll_debug["polls"] = polls
        self._poll_debug["elapsed"] = int(time.time() - start)
        dbg = " ".join(f"{k}={v}" for k, v in (self._poll_debug or {}).items())
        if getattr(self, "_confirm_answer_err", ""):
            dbg += f" confirm_err={self._confirm_answer_err}"
        if getattr(self, "_text_confirm_err", ""):
            dbg += f" text_confirm_err={self._text_confirm_err}"
        raise Exception("豆包域视频生成超时未出片 %s" % (dbg or "（无轮询诊断）"))


# ---------------- 账号池 ----------------

class DoubaoVideoPool:
    """豆包域账号池：轮询 + 失败换号 + 额度检测"""

    def __init__(self, cks: list, proxy: str = None,
                 video_timeout: int = None, max_retry: int = 2):
        self.clients = []
        for ck in cks:
            c = DoubaoVideoClient(ck, proxy=proxy)
            if c.is_valid:
                self.clients.append(c)
        if not self.clients:
            raise ValueError("豆包域账号池为空（所有 CK 均无有效 sessionid）")
        self.video_timeout = int(video_timeout or POLL_TIMEOUT_SEC)
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
