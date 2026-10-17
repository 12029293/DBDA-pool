# -*- coding: utf-8 -*-
"""豆包域（www.doubao.com）· Seedance 2.5 **参考图生视频** 通道
================================================================
在已跑通的文生视频客户端 `doubao_cn_video.DoubaoVideoClient` 基础上，
补上「上传参考图 → 回填 attachment_block(10052) → 同一 /chat/completion 出片」。

已验证 / 待确认
----------------------------------------------------------------
- 文生视频：POST /chat/completion + ability_type=17 + model=seedance_v2.5  ✅ 已实测出片
- 图片上传：/alice/resource/prepare_upload → ImageX ApplyImageUpload → PUT TOS
  · 接口签名与 dola 域一致（豆包 bundle `an.d68aa88f8e.js` 同源）
  · `tenant_id="5"`；`scene_id` 豆包候选 **"5"**（旧 DoubaoCNClient），dola 实测 "4"
    → **首次请用抓包脚本 cap_seedance.py 确认**，再改 `SCENE_ID`
- 回填：messages[].content_block = [ block_type 10052 attachment_block(image) , block_type 10000 text_block ]
  → 与 `dola-pool/dola_client.py::_build_vision_body` 同形（dola 已跑通）

用法
----------------------------------------------------------------
    import asyncio
    from doubao_image_video import DoubaoImageVideoClient

    cli = DoubaoImageVideoClient(open("ck.txt", encoding="utf-8").read())
    url = asyncio.run(cli.generate_video_with_image(
        image="C:/path/ref.png",          # 本地路径 / http URL / data URI / 已有 tos uri
        prompt="基于参考图让主体轻轻转头",
        ratio="9:16", duration=5, model="seedance_v2.5"))
    print(url)
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
import uuid as uuid_lib

import aiohttp

# 复用已验证的豆包域客户端与公共参数
from doubao_cn_video import (
    DoubaoVideoClient, DoubaoLoginError, DoubaoThrottleError, CN_DOMAIN,
)
# 复用 CRC32（dola_client 内同族实现，已实测可用）
from dola_client import crc32_hex
# ★ 2026-10-13 换签名实现：CommitImageUpload 是 POST+JSON body，
#   需要 x-amz-content-sha256 参与签名（HAR 实测），dola_client.aws4_sign 只支持
#   GET 空 body，改用 protocol.dola_pure_api.sign_imagex（支持 POST+body、
#   include_payload_header 开关），不动 dola 域现网在用的 aws4_sign。
from protocol.dola_pure_api import sign_imagex

# ---------------- 可调常量（首次实拍后按真实值校准） ----------------
TENANT_ID = "5"
SCENE_ID = os.environ.get("DOUBAO_UPLOAD_SCENE_ID", "5")   # 豆包候选 "5"；dola 为 "4"
RESOURCE_TYPE = int(os.environ.get("DOUBAO_UPLOAD_RESOURCE_TYPE", "2"))

# ★ 2026-10-13 端点对齐网页版实测（HAR doubao.har 2026-10-12）：
#   ImageX 动作走豆包域反代 /top/v1（GET ?Action=ApplyImageUpload... /
#   POST ?Action=CommitImageUpload...），不是直连 upload_host。
#   host 动态取 CN_DOMAIN（跟随测试环境切换域名）。
IMAGEX_PROXY_PATH = "/top/v1"

# 附件块 / 附件类型（bundle 实证）
BLOCK_ATTACHMENT = 10052
ATTACHMENT_TYPE_IMAGE = 1


class DoubaoImageVideoClient(DoubaoVideoClient):
    """在文生视频客户端上增加「参考图生视频」能力"""

    # ---------- 1. 图片上传 ----------

    async def _fetch_image(self, image_input: str) -> bytes:
        if image_input.startswith("http"):
            async with aiohttp.ClientSession(**self._sess_kw()) as s:
                async with s.get(image_input, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    return await r.read()
        if os.path.isfile(image_input):
            with open(image_input, "rb") as f:
                return f.read()
        b64 = re.sub(r"^data:[^;]+;base64,", "", image_input)
        return base64.b64decode(b64)

    async def upload_image(self, image_input: str) -> str:
        """上传图片，返回 TOS StoreUri（即 image.uri）

        ★ 2026-10-13 对齐网页版 HAR 实测，改为四段式：
          prepare_upload → ApplyImageUpload(/top/v1 反代) → PUT TOS → CommitImageUpload
        第 ④ 步 Commit（POST + SessionKey + x-amz-content-sha256 签名）是网页版
        真实链路（HAR doubao.har:34070，返回 UriStatus=2000 + 正式 Uri），
        缺这步 TOS 端入库不生效，图生视频可能拿不到有效 uri。
        无 SessionKey 时兜底跳过 Commit（兼容旧行为）。
        """
        if image_input.startswith("tos-cn-i-"):
            return image_input
        image_buf = await self._fetch_image(image_input)

        # ① prepare_upload 申请上传凭证（不变）
        url = f"https://{CN_DOMAIN}/alice/resource/prepare_upload"
        headers = {**self._headers(), "agw-js-conv": "str"}
        body = {"tenant_id": TENANT_ID, "scene_id": SCENE_ID,
                "resource_type": RESOURCE_TYPE}
        async with aiohttp.ClientSession(**self._sess_kw()) as s:
            async with s.post(url, params=self._params(), json=body, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=15)) as r:
                p1 = await r.json(content_type=None)
        data = (p1.get("data") or p1) if isinstance(p1, dict) else {}
        auth = data.get("upload_auth_token") or {}
        if not auth:
            raise Exception("prepare_upload 失败: %s" % json.dumps(p1, ensure_ascii=False)[:200])
        service_id = data["service_id"]
        upload_host = data.get("upload_host", "")
        ak, sk, sts = auth["access_key"], auth["secret_key"], auth["session_token"]

        # ② ApplyImageUpload —— 走豆包域 /top/v1 反代（HAR 实测端点），AWS4 GET 签名
        apply_url = (
            f"https://{CN_DOMAIN}{IMAGEX_PROXY_PATH}"
            f"?Action=ApplyImageUpload&Version=2018-08-01"
            f"&ServiceId={service_id}&NeedFallback=true&UploadNum=1"
            f"&FileSize={len(image_buf)}&FileExtension=.png"
        )
        sig = sign_imagex("GET", apply_url, ak, sk, sts)
        async with aiohttp.ClientSession(**self._sess_kw()) as s:
            async with s.get(apply_url, headers=sig,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                p2 = await r.json(content_type=None)
        result = p2.get("Result") or {}
        if not result:
            raise Exception("ApplyImageUpload 失败: %s"
                            % json.dumps(p2, ensure_ascii=False)[:200])
        upload_addr = result["UploadAddress"]
        store_info = upload_addr["StoreInfos"][0]
        store_uri = store_info["StoreUri"]
        tos_host = upload_addr["UploadHosts"][0]
        session_key = upload_addr.get("SessionKey", "")

        # ③ PUT 到 TOS（不变：Authorization 用 TOS Auth，非 AWS4；content-crc32 8位小写）
        tos_url = f"https://{tos_host}/{store_uri}"
        tos_headers = {
            "Authorization": store_info["Auth"],
            "Content-Type": "application/octet-stream",
            "content-crc32": crc32_hex(image_buf),
            "Content-Disposition": 'attachment; filename="image.png"',
        }
        async with aiohttp.ClientSession(**self._sess_kw()) as s:
            async with s.put(tos_url, data=image_buf, headers=tos_headers,
                             timeout=aiohttp.ClientTimeout(total=60)) as r:
                await r.read()

        # ④ ★ 新增：CommitImageUpload（POST + SessionKey，AWS4 带 payload hash）
        #    HAR 实测：返回 Result.Results[0].UriStatus=2000 表示入库成功。
        if session_key:
            commit_url = (
                f"https://{CN_DOMAIN}{IMAGEX_PROXY_PATH}"
                f"?Action=CommitImageUpload&Version=2018-08-01&ServiceId={service_id}"
            )
            commit_body = json.dumps({"SessionKey": session_key},
                                     separators=(",", ":")).encode()
            csig = sign_imagex("POST", commit_url, ak, sk, sts,
                               body=commit_body, include_payload_header=True)
            chdrs = {**csig, "Content-Type": "application/json"}
            async with aiohttp.ClientSession(**self._sess_kw()) as s:
                async with s.post(commit_url, data=commit_body, headers=chdrs,
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
                    p4 = await r.json(content_type=None)
            results = ((p4.get("Result") or {}).get("Results") or [{}])[0]
            status = results.get("UriStatus")
            # UriStatus=2000 入库成功；异常值视为失败（放弃已上传的 uri，直接报错）
            if status is not None and status != 2000:
                raise Exception(
                    "CommitImageUpload 失败: UriStatus=%s %s"
                    % (status, json.dumps(p4, ensure_ascii=False)[:200]))
            # Commit 返回的正式 Uri（正常等于 store_uri；不一致时以它为准）
            return results.get("Uri") or store_uri

        # 兜底：Apply 响应没有 SessionKey 时退回旧行为（跳过 Commit）
        return store_uri

    # ---------- 2. 组装含参考图的消息 ----------

    @staticmethod
    def _image_block(uri: str) -> dict:
        """block_type=10052 图片附件块"""
        return {
            "block_type": BLOCK_ATTACHMENT,
            "content": {
                "attachment_block": {
                    "attachments": [{
                        "type": ATTACHMENT_TYPE_IMAGE,          # 1 = image
                        "identifier": str(uuid_lib.uuid4()),
                        "image": {
                            "name": "image.png",
                            "uri": uri,
                            "image_ori": {"url": "", "width": 1024, "height": 1024,
                                          "format": "", "url_formats": {}},
                            "image_thumb": {"url": "", "width": 0, "height": 0, "uri": uri},
                        },
                        "parse_state": 0,
                        "review_state": 1,
                        "upload_status": 1,
                        "progress": 100,
                        "src": "",
                    }],
                },
                "pc_event_block": "",
            },
            "block_id": str(uuid_lib.uuid4()),
            "parent_id": "",
            "meta_info": [],
            "append_fields": [],
        }

    # ---------- 3. 参考图出片 ----------

    async def generate_video_with_image(self, image: str, prompt: str,
                                        ratio: str = "9:16", duration: int = 5,
                                        model: str = "seedance_v2.5",
                                        timeout: int | None = None,
                                        on_poll=None) -> str:
        """上传参考图 + 出片，返回视频下载 URL。

        复用文生视频的 body 骨架，仅把 messages[0].content_block 换成
        [图片附件块, 文本块]（对齐浏览器输入引擎：附件在前、文本在后）。
        """
        if not self.is_valid:
            raise DoubaoLoginError("CK 缺少有效 sessionid")

        uri = await self.upload_image(image)

        body = self._build_video_body(prompt, ratio, duration, model)
        # ★ 2026-10-13：文本走 _video_prompt_text（带模型/比例/时长），
        #   对齐网页版「使用seedanceX.X生成，比例，N秒」格式；
        #   参考图引用前缀由文本块里的 attachment:// 链接承担（网页版同款）。
        video_prompt = self._video_prompt_text(prompt, ratio, duration, model)
        # 网页版带参考图时文本以「[文件名](attachment://uri?type=1) 」开头
        fname = os.path.basename(image).rstrip("/") or "image.png"
        att_ref = f"[{fname}](attachment://{uri}?type=1) "
        video_prompt = att_ref + video_prompt
        text_block = {
            "block_type": 10000,
            "content": {
                "text_block": {"text": video_prompt, "icon_url": "",
                               "icon_url_dark": "", "summary": ""},
                "pc_event_block": "",
            },
            "block_id": str(uuid_lib.uuid4()),
            "parent_id": "",
            "meta_info": [],
            "append_fields": [],
        }
        # 附件块在前、文本块在后（浏览器输入引擎即此顺序）
        body["messages"][0]["content_block"] = [self._image_block(uri), text_block]

        body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        headers = self._headers(local_conv_id=self._local_conv_id)
        params = self._params()
        url = f"https://{CN_DOMAIN}/chat/completion"

        conv_id = ""
        sse_err = ""
        srv_text = ""
        self.confirm_answered = False   # 「高消耗确认卡」每次生成重置
        self._answered_clarify = set()  # 消息块形态确认卡去重
        self.text_confirmed = False     # 纯文本确认是否已补发（仅受理成功才 True）
        self._pending_text_confirm = False
        self._text_confirm_err = ""
        self._confirm_send_count = 0    # 本次生成已发出的确认请求数（上限闸门）
        sse_text = ""                   # 流内文本缓冲（识别「参数确认」用）

        async def _on_sse_event(name: str, data) -> None:
            """★ 流内即时应答确认卡（2026-10-13 修复，与文生视频同款）。

            服务端下发确认卡后挂住 SSE 流等回应，等流读完再扫描的旧实现
            永远等不到——必须在事件到达时立即应答。

            ★ 2026-10-14 修正：纯文本参数确认（「…确认后我再开始生成视频。」）
            **不能在流内补发**——此刻原 SSE 流还没关闭，上游对同会话第二条
            /chat/completion 是**静默丢弃**（实测：会话里根本不出现该消息）。
            这里只标记，等 `_read_sse` 返回（本轮结束）后再发。
            """
            nonlocal conv_id, sse_text
            if not isinstance(data, dict):
                return
            for tc, clarify, raw in self._find_confirm_asks(data):
                if clarify in self._answered_clarify:
                    continue
                self._answered_clarify.add(clarify)
                if not await self._answer_confirm_generate(tc, clarify, raw):
                    self._answered_clarify.discard(clarify)  # 失败可重试
            sse_text = (sse_text + self._text_deltas(data))[-4000:]
            if self._needs_text_confirm(sse_text):
                self._pending_text_confirm = True
                self._poll_debug["param_confirm_seen_in_stream"] = True

        async with aiohttp.ClientSession(**self._sess_kw()) as s:
            async with s.post(url, params=params, json=body, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=120)) as resp:
                events = await self._read_sse(resp, on_event=_on_sse_event)
        for name, data in events:
            if name == "SSE_ACK":
                ack = data.get("ack_client_meta") or {}
                conv_id = ack.get("conversation_id", "")
                if ack.get("conversation_type"):
                    self._conv_type = ack["conversation_type"]
            # 兜底：流内即时应答已覆盖常规路径，这里再扫一遍残留确认卡
            for ask in (self._find_confirm_asks(data)
                        if isinstance(data, dict) else []):
                if ask[1] not in self._answered_clarify:
                    self._answered_clarify.add(ask[1])
                    if await self._answer_confirm_generate(ask[0], ask[1], ask[2]):
                        continue
                    self._answered_clarify.discard(ask[1])
            if name == "gateway-error":
                msg = json.dumps(data, ensure_ascii=False)
                if "710012000" in msg or "Invalid User ID" in msg:
                    raise DoubaoLoginError(f"豆包域登录态无效: {msg[:120]}")
                raise Exception("受理失败: %s" % msg[:160])
            if name == "STREAM_ERROR":
                code = data.get("error_code")
                msg = data.get("error_msg") or ""
                if code in (710022002, 710022004):
                    # 上游限流 ≠ 登录失效（见 doubao_cn_video.DoubaoThrottleError）
                    raise DoubaoThrottleError(f"豆包域限流({code}): {msg}")
                sse_err = f"[{code}] {msg}"
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

        if not conv_id or conv_id == "0":
            raise Exception(
                "未建立会话(conversation_id=%r)，检查 attachment_block / model。%s %s"
                % (conv_id or "", sse_err, srv_text[:160]))
        self.last_conversation_id = conv_id

        # ★ 兜底（**主路径**）：本轮 SSE 已关闭，此刻补发才被上游受理
        #   （流未关闭时发的会被静默丢弃，2026-10-14 实测）。不确认就不排产。
        stream_text = sse_text
        for _n, _d in events:
            if isinstance(_d, dict):
                stream_text += self._text_deltas(_d)
        stream_text = (stream_text + " " + srv_text)[-4000:]
        if self._needs_text_confirm(stream_text):
            self._pending_text_confirm = True
        if self._pending_text_confirm and not self.text_confirmed:
            await self._send_text_confirmation(conv_id)

        return await self._poll_video(conv_id, timeout,
                                      conversation_type=self._conv_type,
                                      on_poll=on_poll)


__all__ = ["DoubaoImageVideoClient", "TENANT_ID", "SCENE_ID", "RESOURCE_TYPE"]
