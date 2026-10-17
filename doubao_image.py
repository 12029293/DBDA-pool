# -*- coding: utf-8 -*-
"""豆包域（www.doubao.com）· Seedream 文生图通道
================================================================
与 ``doubao_cn_video``（Seedance 文生视频）**平行**的第二条生成能力：
同一个账号池、同一套 Cookie、同样的 ``/chat/completion`` 网关，
只有 ``content_type`` / ``skill`` 与**结果解析方式**不同。

协议事实（来自参考实现 open-ai-canvas 的真机抓包结论 + 本仓库实测对照）
----------------------------------------------------------------
1. 路径与视频一致：``POST https://www.doubao.com/chat/completion``
   （不带 ``/samantha`` 前缀；带前缀是网关层 ``unexpected end of JSON input``）。
2. 触发方式**不是** ``chat_ability.ability_type``（那是 dola 域的口径），
   而是消息体上的 **``content_type = 2009`` + ``skill`` 块**：

   .. code-block:: json

      {"messages":[{"content":"<JSON字符串>","content_type":2009,
                    "skill":{"skill_type":3,"skill_id":"3",...}}],
       "completion_option":{"action_bar_skill_id":3, ...}}

   其中 ``content`` 是**二次序列化的 JSON 字符串**，形如
   ``{"text": ..., "model": ..., "ratio": ..., "template_type":"placeholder",
      "use_creation": false}``。

3. ★ **比例/模型豆包只认正文语义**：结构化 ``ratio`` / ``model`` 字段写了也不一定
   生效，必须把比例以方位词（横屏/竖屏/方形）写进 ``text`` 正文。与视频同结论。
4. ★ 结果不走 ``/im/chain/single`` 轮询（图是「同步返回」的），而是直接在
   SSE 流里下发：
     · ``content_type 2010`` 的 ``content.data[]``（``image_ori_raw`` / ``image_ori`` …）
     · ``content_type 2074`` 的 ``content.creations[].image``
     · 补丁流（``patch_op`` / ``creation_block``）里任意层级的 ``creations[]``
   因此**图不需要轮询**，读完整条 SSE 即可判定成败。

5. ★ 无水印：SSE 下发的所有变体水印都**烧录在像素里**（``cthumb_wm1`` /
   ``cpreview_wm1`` / ``cdld_wm3``），改 URL 参数无效。真无水印原片要凭裸 key
   （``tos-cn-i-xxx/rc_gen_image/<hash>.jpeg``）调
   ``POST /alice/message/get_file_url {uris:[key], type:"image"}``
   换签名直链（返回 ``~tplv-*-image-qvalue`` 模板）。
   ``image_ori_raw`` 本身通常就是干净原图，优先取它，换链只作兜底。

模型名（豆包网页版 Seedream 档位）
----------------------------------------------------------------
对外统一键 → 正文里的 ``model`` 文案：

    seedream-4.0      → Seedream 4.0
    seedream-4.5      → Seedream 4.5     （默认）
    seedream-5.0-pro  → Seedream 5.0 Pro
    seedream-5.0-flash→ Seedream 5.0 Flash

用法
----------------------------------------------------------------
    import asyncio
    from doubao_image import DoubaoImageClient

    cli = DoubaoImageClient(open("ck.txt", encoding="utf-8").read())
    urls = asyncio.run(cli.generate_image("一只橘猫，电影感", ratio="16:9",
                                          model="seedream-4.5"))
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid as uuid_lib

import aiohttp

# 复用视频通道的请求构造（headers / params / session / SSE 读取 / 异常类型）
from doubao_cn_video import (
    CN_DOMAIN,
    DoubaoLoginError,
    DoubaoThrottleError,
    DoubaoCreditError,
    DoubaoVideoClient,
)

# ---------------------------------------------------------------- 协议常量

# 文生图的内容类型与技能号（与视频的 2020 / 17 相对）。
IMAGE_CONTENT_TYPE = 2009
IMAGE_SKILL_TYPE = 3
IMAGE_SKILL_ID = "3"

# 生成结果的块类型
CREATION_BLOCK_TYPE = 2074
IMAGE_DATA_CONTENT_TYPE = 2010

# 无水印换链接口
GET_FILE_URL_PATH = "/alice/message/get_file_url"

MODEL_TEXT_LABEL = {
    "seedream-4.0": "Seedream 4.0",
    "seedream-4.5": "Seedream 4.5",
    "seedream-5.0-pro": "Seedream 5.0 Pro",
    "seedream-5.0-flash": "Seedream 5.0 Flash",
}
DEFAULT_IMAGE_MODEL = "seedream-4.5"
IMAGE_MODELS = ("seedream-4.0", "seedream-4.5",
                "seedream-5.0-pro", "seedream-5.0-flash")

# ★ 图片比例比视频多 3:2 / 2:3 / 4:5 / 5:4。
#   证据：视频侧的比例档位来自网页版真实配置
#   （`extensions/dola30/doubao-skill-pack-response.json` 的 ratio.options 只有
#     1:1 / 3:4 / 4:3 / 9:16 / 16:9 / 21:9 —— **没有** 3:2 系）；
#   而参考实现（open-ai-canvas）的豆包**图片**比例吸附表是
#     1:1,16:9,9:16,4:3,3:4,3:2,2:3,21:9,4:5,5:4 —— 图片侧确实更宽。
IMAGE_RATIOS = ("21:9", "16:9", "3:2", "4:3", "1:1",
                "4:5", "5:4", "3:4", "2:3", "9:16")

# 图片 URL 提取
_IMAGE_URL_RE = re.compile(r"https?://[^\s)\"']+")
_IMAGE_LIKE_RE = re.compile(r"(?i)(\.png|\.jpe?g|\.webp|byteimg|imagex|tos-cn)")
_TPL_WATERMARK_RE = re.compile(r"~tplv-[^.?&]*watermark[^.?&]*")
_LOGO_PARAM_RE = re.compile(r"[&?]logo=[^&]*")
_IMAGE_KEY_RE = re.compile(
    r"(tos-cn-i-[a-z0-9]+/[A-Za-z0-9_/-]+\.(?:jpeg|jpg|png|webp))")

# 变体优先级：image_ori_raw 通常已是无水印原图
_VARIANT_KEYS = ("image_ori_raw", "image_ori", "image_raw",
                 "image_preview", "image_thumb")


def resolve_image_model(model: str | None) -> str:
    """对外模型键 → 豆包正文里的模型文案（未知回落默认档）。

    ★ 顺序要紧：``seedream-5.0-pro`` / ``-flash`` 归一后都含 "5.0"，
    若先判 "5.0" 会把 pro/flash 的档位信息吃掉，所以先判后缀。
    """
    raw = (model or "").strip().lower()
    if raw in MODEL_TEXT_LABEL:
        return MODEL_TEXT_LABEL[raw]
    flat = raw.replace("_", "-").replace(" ", "")
    if "pro" in flat:
        return MODEL_TEXT_LABEL["seedream-5.0-pro"]
    if "flash" in flat or "fast" in flat:
        return MODEL_TEXT_LABEL["seedream-5.0-flash"]
    if "4.5" in flat or "45" in flat:
        return MODEL_TEXT_LABEL["seedream-4.5"]
    if "4.0" in flat or "40" in flat:
        return MODEL_TEXT_LABEL["seedream-4.0"]
    if "5.0" in flat or "50" in flat:
        return MODEL_TEXT_LABEL["seedream-5.0-pro"]
    return MODEL_TEXT_LABEL[DEFAULT_IMAGE_MODEL]


def normalize_image_ratio(value: str | None) -> str:
    """把像素尺寸（``1824x1024``）或任意写法归一成标准比例（``16:9``）。

    豆包正文只认 ``W:H`` 这类语义；面板若存了像素（部分协议会），
    这里按最接近的标准比例吸附（2% 容差，与参考实现同口径）。
    """
    v = (value or "").strip().lower().replace("×", "x")
    if not v or v == "auto":
        return "1:1"
    if ":" in v:
        return v
    parts = v.split("x")
    if len(parts) != 2:
        return "1:1"
    try:
        w, h = int(parts[0].strip()), int(parts[1].strip())
    except (TypeError, ValueError):
        return "1:1"
    if w <= 0 or h <= 0:
        return "1:1"
    standards = [(1, 1), (16, 9), (9, 16), (4, 3), (3, 4),
                 (3, 2), (2, 3), (21, 9), (4, 5), (5, 4)]
    best, best_diff = "", 0.02
    for sw, sh in standards:
        diff = abs((w * sh) / (h * sw) - 1)
        if diff < best_diff:
            best, best_diff = "%d:%d" % (sw, sh), diff
    if best:
        return best
    a, b = w, h
    while b:
        a, b = b, a % b
    a = a or 1
    return "%d:%d" % (w // a, h // a)


def ratio_spec_text(ratio: str) -> str:
    """比例 → 方位词描述（豆包只认正文语义，不认结构化字段）。"""
    v = (ratio or "").strip()
    if not v or v == "auto":
        return ""
    parts = v.split(":", 1)
    if len(parts) != 2:
        return "比例 " + v
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        return "比例 " + v
    if w == 0 or h == 0:
        return "比例 " + v
    orient = "方形" if w == h else ("横屏" if w > h else "竖屏")
    return "%s %s" % (orient, v)


def clean_image_url(url: str) -> str:
    """清掉 URL 上的水印模板痕迹（只作用于回退变体）。"""
    if not url:
        return url
    if "image_dld_watermark" in url:
        return url.replace("image_dld_watermark", "image_pre_watermark")
    u = _TPL_WATERMARK_RE.sub("", url)
    if "logo=" in u:
        u = _LOGO_PARAM_RE.sub("", u)
        u = u.replace("?&", "?").replace("&&", "&")
        if u.endswith("?") or u.endswith("&"):
            u = u[:-1]
    return u


def _find_url_in_data(node, *hints) -> str:
    """递归在任意结构里找首个含 hint 的 http URL（兜底用）。"""
    if isinstance(node, dict):
        for v in node.values():
            found = _find_url_in_data(v, *hints)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_url_in_data(v, *hints)
            if found:
                return found
    elif isinstance(node, str):
        if node.startswith("http") and any(h in node for h in hints):
            return node
    return ""


def _harvest_creations(node, pick, depth: int = 0) -> None:
    """递归收集任意层级 ``creations[].image``（补丁流 / creation_block）。"""
    if depth > 24:
        return
    if isinstance(node, dict):
        creations = node.get("creations")
        if isinstance(creations, list):
            for item in creations:
                if isinstance(item, dict):
                    img = item.get("image")
                    if isinstance(img, dict):
                        pick(img, str(img.get("key") or ""))
        for v in node.values():
            _harvest_creations(v, pick, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _harvest_creations(v, pick, depth + 1)


def collect_images(events: list) -> list[str]:
    """从 SSE 事件流里提取全部生成图（按变体优先级 + 去重）。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(raw: str, key: str = "") -> None:
        if not isinstance(raw, str) or not raw.startswith("http"):
            return
        u = raw.replace("\\u0026", "&").replace("&amp;", "&")
        if not _IMAGE_LIKE_RE.search(u):
            return
        u = clean_image_url(u)
        dedupe = key or (u.split("?")[0] if "?" in u else u)
        if dedupe in seen or u in seen:
            return
        seen.add(dedupe)
        seen.add(u)
        out.append(u)

    def pick(item, key: str = "") -> None:
        for k in _VARIANT_KEYS:
            v = item.get(k)
            if isinstance(v, dict) and isinstance(v.get("url"), str):
                add(v["url"], key)
        if isinstance(item.get("url"), str):
            add(item["url"], key)

    for _name, data in events:
        if not isinstance(data, dict):
            continue
        # 补丁流兜底：creations 可能嵌在任意层
        _harvest_creations(data, pick)
        msg = data
        content = msg.get("content")
        cm = content if isinstance(content, dict) else None
        if cm is None and isinstance(content, str):
            try:
                cm = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                cm = None
        if isinstance(cm, dict):
            ct = msg.get("content_type")
            if ct == IMAGE_DATA_CONTENT_TYPE:
                for it in (cm.get("data") or []):
                    if isinstance(it, dict):
                        pick(it, str(it.get("key") or ""))
            elif ct == CREATION_BLOCK_TYPE:
                for c in (cm.get("creations") or []):
                    if isinstance(c, dict):
                        img = c.get("image")
                        if isinstance(img, dict):
                            pick(img, str(img.get("key") or ""))
                        if isinstance(c.get("url"), str):
                            add(c["url"])
            text = cm.get("text")
            if isinstance(text, str):
                for u in _IMAGE_URL_RE.findall(text):
                    add(u)
        elif isinstance(content, str):
            for u in _IMAGE_URL_RE.findall(content):
                add(u)
    return out


def collect_reply_text(events: list) -> str:
    """把助手回复的纯文本拼起来（无图时用于说明被拒原因）。"""
    chunks: list[str] = []

    def add(node) -> None:
        if isinstance(node, dict):
            tb = node.get("text_block")
            if isinstance(tb, dict) and isinstance(tb.get("text"), str):
                chunks.append(tb["text"])
            for v in node.values():
                add(v)
        elif isinstance(node, list):
            for v in node:
                add(v)

    for name, data in events:
        if name in ("SSE_HEARTBEAT", "SSE_REPLY_END"):
            continue
        add(data)
    text = "".join(chunks)
    # 剥掉偶发的 "{}" JSON 噪声
    return text.replace("{}", "").strip()


class DoubaoImageRejectedError(Exception):
    """**提示词级**拒绝：模型回了文本但没出图（内容风控/版权/真人等）。

    ★ 与"空响应"必须分开，否则号池会做错事：
      · 本类 = 上游明确回复了拒绝理由 → 换号也一样被拒（提示词的问题），
        池层应当**直接判失败**，不要把整池号白跑一遍；
      · 空响应（普通 Exception）= 可能是瞬时限流/CDN 截断 → 应当换号重试。
    """


class DoubaoImageClient(DoubaoVideoClient):
    """文生图客户端：复用视频客户端的请求构造，换 content_type/skill 与结果解析。"""

    # ---------- 请求体 ----------

    def _build_image_body(self, prompt: str, ratio: str, model: str,
                          style: str = "") -> dict:
        """构建文生图请求体。

        ★★ 2026-10-16 实测校正（真号 db002 逐字段二分，见 `_probe_img*.py`）：
        参考实现（open-ai-canvas）那份**精简版 ``completion_option`` 在本站点不可用**——
        照搬会拿到 ``710020202 common invalid param``（请求体校验失败）；
        换成参考实现里更短的 option 会变成 ``710020702 system error``。
        而**沿用本仓库视频通道那份完整 option/ext**、只把消息换成图片消息，
        校验即可通过（错误码回到 ``710022002`` = 限流，属"已过校验"）。

        结论（本方法就是这么实现的）：
          1. 以 `_build_video_body()` 产出的**完整信封**为底（option/ext/client_meta
             与视频链路完全一致，天然跟着视频通道一起演进）；
          2. 把 ``messages`` 换成**图片消息**：``content_type=2009`` + ``skill`` 块，
             ``content`` 是二次序列化的 JSON 字符串；
          3. **删掉 ``chat_ability``** —— 实测：带视频的 ``ability_type=17`` 或
             完全不带的差异都让校验通过，但图片语义下不该带视频能力声明。
        """
        # ① 复用视频信封（这一步同时把 self._local_conv_id 设好）。
        #    prompt/ratio/model 只是给信封里那个稍后会被整体替换掉的消息占位。
        body = self._build_video_body(prompt, ratio, 5, "seedance_v2.0")
        # ② 换成图片消息
        ratio = normalize_image_ratio(ratio)
        model_label = resolve_image_model(model)
        text_data = prompt
        spec = ratio_spec_text(ratio)
        if spec:
            text_data = "%s，%s" % (prompt, spec)
        if style and style != "默认":
            text_data = "%s\n风格：%s" % (text_data, style)

        content_data = {
            "text": text_data,
            "model": model_label,
            "template_type": "placeholder",
            "use_creation": False,
            "ratio": ratio,
        }
        body["messages"] = [{
            "content": json.dumps(content_data, ensure_ascii=False,
                                  separators=(",", ":")),
            "content_type": IMAGE_CONTENT_TYPE,
            "attachments": [],
            "references": [],
            "skill": {
                "skill_type": IMAGE_SKILL_TYPE,
                "skill_type_no_default": IMAGE_SKILL_TYPE,
                "skill_id": IMAGE_SKILL_ID,
                "skill_id_no_default": IMAGE_SKILL_ID,
            },
        }]
        # ③ 图片语义不带视频能力声明
        body.pop("chat_ability", None)
        return body

    # ---------- 无水印换链 ----------

    async def upgrade_no_watermark(self, urls: list[str]) -> list[str]:
        """把带水印模板的 URL 换成无水印签名直链（失败原样保留）。"""
        if not urls or not self.raw_ck:
            return urls
        out: list[str] = []
        upgraded = 0
        cache: dict[str, str] = {}
        for u in urls:
            m = _IMAGE_KEY_RE.search(u or "")
            best = ""
            if m:
                key = m.group(1)
                if key in cache:
                    best = cache[key]
                else:
                    best = await self._fetch_signed_image(key)
                    cache[key] = best
            if best.startswith("http"):
                out.append(best)
                upgraded += 1
            else:
                out.append(u)
        return out if upgraded else urls

    async def _fetch_signed_image(self, key: str) -> str:
        """凭裸 key 调 get_file_url 换无水印签名直链。"""
        url = f"https://{CN_DOMAIN}{GET_FILE_URL_PATH}"
        body = {"uris": [key], "type": "image", "expire_second": 3600}
        try:
            async with aiohttp.ClientSession(**self._sess_kw()) as s:
                async with s.post(
                    url, params=self._params(), json=body,
                    headers={**self._headers(), "agw-js-conv": "str, str"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    j = await r.json(content_type=None)
        except Exception:
            return ""
        data = (j or {}).get("data") or {}
        for item in (data.get("file_urls") or []):
            for field in ("main_url", "back_url"):
                v = str(item.get(field) or "")
                if v.startswith("http"):
                    return v
        return ""

    # ---------- 生成 ----------

    async def generate_image(self, prompt: str, ratio: str = "1:1",
                             model: str = DEFAULT_IMAGE_MODEL,
                             style: str = "", timeout: int = None,
                             no_watermark: bool = True) -> dict:
        """文生图：返回 ``{"images": [...], "conversation_id": str, "text": str}``。

        与视频不同，**结果在 SSE 流里直接下发**，不需要轮询会话链。
        """
        if not self.is_valid:
            raise DoubaoLoginError("CK 缺少有效 sessionid")
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt 不能为空")

        body = self._build_image_body(prompt, ratio, model, style)
        headers = self._headers(local_conv_id=self._local_conv_id)
        url = f"https://{CN_DOMAIN}/chat/completion"
        # 文生图同样**不加 a_bogus/msToken**（加了就 710022002，见模块 docstring）
        timeout = int(timeout or os.environ.get("DOUBAO_IMAGE_TIMEOUT", "300"))

        conv_id = ""
        sse_err = ""
        srv_text = ""
        try:
            async with aiohttp.ClientSession(**self._sess_kw()) as s:
                async with s.post(url, json=body, headers=headers,
                                  params=self._params(),
                                  timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    events = await self._read_sse(resp)
        except asyncio.TimeoutError as exc:  # 读流超时：保留不了事件，按失败
            raise Exception("豆包生图等待超时（%ds）" % timeout) from exc

        for name, data in events:
            if name == "gateway-error":
                msg = json.dumps(data, ensure_ascii=False)
                if "710012000" in msg or "Invalid User ID" in msg:
                    raise DoubaoLoginError("豆包域登录态无效: %s" % msg[:120])
                raise Exception("豆包生图受理失败: %s" % msg[:160])
            if name == "STREAM_ERROR":
                code = data.get("error_code")
                msg = data.get("error_msg") or ""
                if code in (710022002, 710022004):
                    raise DoubaoThrottleError("豆包域限流(%s): %s" % (code, msg))
                sse_err = "[%s] %s" % (code, msg)
            if name == "SSE_ACK":
                conv_id = (data.get("ack_client_meta") or {}).get(
                    "conversation_id", "") or conv_id
            if name == "STREAM_MSG_NOTIFY":
                try:
                    for b in ((data.get("content") or {}).get("content_block") or []):
                        t = ((b.get("content") or {}).get("text_block") or {}).get("text")
                        if t:
                            srv_text = t
                except Exception:
                    pass
            if not conv_id and isinstance(data, dict):
                conv_id = data.get("conversation_id") or conv_id

        if conv_id:
            self.last_conversation_id = conv_id

        images = collect_images(events)
        text = collect_reply_text(events) or srv_text

        # 额度类终态：判成 CreditError 让池层换号（不要傻重试）
        if not images and text and re.search(
                r"免费额度已用完|额度已用完|额度用尽|积分不足|余额不足|已达上限",
                text):
            raise DoubaoCreditError("额度不足: %s" % text[:80])

        if not images:
            hint = (text or sse_err or "").strip()
            if hint:
                # ★ 上游**明确回复了**拒绝理由 → 提示词级判定，换号无效。
                #   用专门的异常类型承载（不要靠"消息里含某个词"来判 —— 
                #   本类的提示文案里就带着"风控/拒绝"字样，字符串匹配会恒真）。
                raise DoubaoImageRejectedError(
                    "豆包拒绝生成或未返回图片：%.200s"
                    "（若提示词含版权角色/真人等敏感内容会被上游风控拒绝，"
                    "请调整提示词后重试）" % hint)
            raise Exception(
                "豆包生图未返回图片（空响应，sse=%s），可能被风控或 CDN 截断，"
                "请换号重试" % (sse_err or "无"))

        if no_watermark:
            try:
                images = await self.upgrade_no_watermark(images)
            except Exception:
                pass   # 换链失败不影响出图，保留带水印 URL

        return {"images": images, "conversation_id": conv_id, "text": text}
