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
# 复用 AWS4 签名 / CRC32（dola_client 内同族实现，已实测可用）
from dola_client import aws4_sign, crc32_hex

# ---------------- 可调常量（首次实拍后按真实值校准） ----------------
TENANT_ID = "5"
SCENE_ID = os.environ.get("DOUBAO_UPLOAD_SCENE_ID", "5")   # 豆包候选 "5"；dola 为 "4"
RESOURCE_TYPE = int(os.environ.get("DOUBAO_UPLOAD_RESOURCE_TYPE", "2"))

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

        三段式：prepare_upload → ApplyImageUpload → PUT TOS
        """
        if image_input.startswith("tos-cn-i-"):
            return image_input
        image_buf = await self._fetch_image(image_input)

        # ① prepare_upload 申请上传凭证
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
        upload_host = data["upload_host"]
        ak, sk, sts = auth["access_key"], auth["secret_key"], auth["session_token"]

        # ② ApplyImageUpload（AWS4 签名）
        apply_url = (
            f"https://{upload_host}/?Action=ApplyImageUpload&Version=2018-08-01"
            f"&ServiceId={service_id}&NeedFallback=true&UploadNum=1"
            f"&FileSize={len(image_buf)}&FileExtension=.png"
        )
        sig = aws4_sign("GET", apply_url, ak, sk, sts)
        async with aiohttp.ClientSession(**self._sess_kw()) as s:
            async with s.get(apply_url, headers=sig,
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                p2 = await r.json(content_type=None)
        store_info = p2["Result"]["UploadAddress"]["StoreInfos"][0]
        store_uri = store_info["StoreUri"]
        tos_host = p2["Result"]["UploadAddress"]["UploadHosts"][0]

        # ③ PUT 到 TOS
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
                                        timeout: int = 300) -> str:
        """上传参考图 + 出片，返回视频下载 URL。

        复用文生视频的 body 骨架，仅把 messages[0].content_block 换成
        [图片附件块, 文本块]（对齐浏览器输入引擎：附件在前、文本在后）。
        """
        if not self.is_valid:
            raise DoubaoLoginError("CK 缺少有效 sessionid")

        uri = await self.upload_image(image)

        body = self._build_video_body(prompt, ratio, duration, model)
        video_prompt = f"生成影片：{prompt}，{ratio}"
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
        async with aiohttp.ClientSession(**self._sess_kw()) as s:
            async with s.post(url, params=params, json=body, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=120)) as resp:
                events = await self._read_sse(resp)
        for name, data in events:
            if name == "SSE_ACK":
                ack = data.get("ack_client_meta") or {}
                conv_id = ack.get("conversation_id", "")
                if ack.get("conversation_type"):
                    self._conv_type = ack["conversation_type"]
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

        return await self._poll_video(conv_id, timeout,
                                      conversation_type=self._conv_type)


__all__ = ["DoubaoImageVideoClient", "TENANT_ID", "SCENE_ID", "RESOURCE_TYPE"]
