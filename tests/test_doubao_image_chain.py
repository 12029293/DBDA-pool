# -*- coding: utf-8 -*-
"""豆包「参考图上传 → 出片」全链路本地夹具测试。

为什么需要这个夹具：本机没有有效的豆包登录态（无真实账号），
`prepare_upload` 只能探到 `710012001 login invalid`。为了真验证
「测试生成支持上传参考图并走豆包图片上传接口」这条需求，这里起一个
**本地 aiohttp 服务**按豆包真实协议应答，把 DoubaoImageVideoClient /
DoubaoPool 的请求逐跳抓下来断言：

  ① POST /alice/user/get_account_info      → 登录态有效
  ② POST /alice/resource/prepare_upload    → 断言 tenant_id/scene_id/resource_type
  ③ GET  ApplyImageUpload                  → 断言 AWS4 签名头存在、参数齐全
  ④ PUT  https://<upload_host>/<StoreUri>  → 断言 crc32 与字节完全一致
  ⑤ POST /chat/completion (SSE)            → 断言 content_block 是
                                             [10052 图片附件块, 10000 文本块]，
                                             且 attachment_block.image.uri == StoreUri
  ⑥ POST /im/chain/single                  → 返 block_type=2074/type=2/download_url
  ⑦ 下载 + 记账                            → 断言 used_today += 1
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pr = types.ModuleType("patchright")
pa = types.ModuleType("patchright.async_api")
pa.async_playwright = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stub"))
pr.async_api = pa
sys.modules.setdefault("patchright", pr)
sys.modules.setdefault("patchright.async_api", pa)

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import doubao_cn_video  # noqa: E402
import doubao_image_video  # noqa: E402
import doubao_pool  # noqa: E402
import config  # noqa: E402
from doubao_pool import DoubaoAccountStore, DoubaoPool  # noqa: E402

CK = ("sessionid=" + "e" * 32 + "; sessionid_ss=" + "e" * 32
      + "; ttwid=tt; has_biz_token=false")

STORE_URI = "tos-cn-v-abc123/ref-image-0001"
TOS_AUTH = "upload-auth-token-xyz"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"payload" * 64


class Fixture:
    """记录夹具收到的每一跳请求，供测试断言。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail_prepare = False

    def rec(self, name: str, **kw) -> None:
        self.calls.append({"name": name, **kw})

    def get(self, name: str) -> dict | None:
        return next((c for c in self.calls if c["name"] == name), None)

    def all(self, name: str) -> list[dict]:
        return [c for c in self.calls if c["name"] == name]


def build_app(fx: Fixture) -> web.Application:
    app = web.Application()

    async def account_info(request):
        fx.rec("account_info", cookie=request.headers.get("Cookie", ""))
        return web.json_response({"code": 0, "data": {
            "user_id": "12345",
            "base_account_info": {"mobile": "+86 138****0000", "name": "测试号"},
            "connect_account_info": [{"platform": "aweme_v2"}],
        }})

    async def prepare_upload(request):
        if fx.fail_prepare:
            fx.rec("prepare_upload", query=dict(request.query), body={})
            return web.json_response({"code": 710012001, "message": "login invalid"})
        body = await request.json()
        fx.rec("prepare_upload", query=dict(request.query), body=body)
        return web.json_response({
            "code": 0,
            "data": {
                "service_id": "svc-1",
                "upload_host": f"127.0.0.1:{request.url.port}",
                "upload_auth_token": {"access_key": "AKTEST", "secret_key": "SKTEST",
                                      "session_token": "STSTEST"},
            },
        })

    async def apply_upload(request):
        fx.rec("ApplyImageUpload", query=dict(request.query),
               auth=request.headers.get("Authorization", ""),
               xdate=request.headers.get("X-Amz-Date", ""))
        return web.json_response({"Result": {"UploadAddress": {
            "StoreInfos": [{"StoreUri": STORE_URI, "Auth": TOS_AUTH}],
            "UploadHosts": [f"127.0.0.1:{request.url.port}"],
        }}})

    async def tos_put(request):
        data = await request.read()
        fx.rec("tos_put", path=request.path, data=data,
               auth=request.headers.get("Authorization", ""),
               crc32=request.headers.get("content-crc32", ""),
               ctype=request.headers.get("Content-Type", ""))
        return web.json_response({"code": 200})

    async def completion(request):
        raw = await request.read()
        body = json.loads(raw.decode())
        fx.rec("completion", query=dict(request.query), body=body)
        # 按豆包真实 SSE 形态回一个 SSE_ACK
        ack = {"ack_client_meta": {"conversation_id": "98765432101234567",
                                   "conversation_type": 3}}
        sse = ("event: SSE_ACK\ndata: " + json.dumps(ack) + "\n\n"
               "event: SSE_REPLY_END\ndata: {}\n\n")
        return web.Response(text=sse, content_type="text/event-stream")

    async def chain_single(request):
        body = await request.json()
        fx.rec("chain_single", query=dict(request.query), body=body,
               ctype=request.headers.get("content-type", ""),
               agw=request.headers.get("agw-js-conv", ""))
        content = json.dumps([{
            "block_type": 2074,
            "content": {"creation_block": {"creations": [{
                "type": 2,
                # ★ 必须是绝对 URL：`_poll_video` 只认 http(s) 开头的 download_url
                #   （豆包真实返回的就是 https://v26-vdl.doubao.com/... 绝对地址）
                "video": {"download_url":
                          f"http://127.0.0.1:{request.url.port}/_video/out.mp4"}}]}},
        }])
        payload = {"status_code": 0, "downlink_body": {
            "pull_singe_chain_downlink_body": {"messages": [{"content": content}]}}}
        return web.json_response(payload)

    async def out_video(request):
        return web.Response(body=MP4, content_type="video/mp4")

    app.router.add_post("/alice/user/get_account_info", account_info)
    app.router.add_post("/alice/resource/prepare_upload", prepare_upload)
    app.router.add_get("/", apply_upload)
    app.router.add_put("/{tail:.*}", tos_put)
    app.router.add_post("/chat/completion", completion)
    app.router.add_post("/im/chain/single", chain_single)
    app.router.add_get("/_video/out.mp4", out_video)
    return app


class _Ctx:
    def __init__(self):
        self.fx = Fixture()
        self.runner = None
        self.port = 0

    async def __aenter__(self):
        self.runner = web.AppRunner(build_app(self.fx))
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


def _point_clients_at(port: int) -> None:
    """把豆包域的两个模块的域名常量改到本地夹具。"""
    base = f"127.0.0.1:{port}"
    doubao_cn_video.CN_DOMAIN = base
    doubao_image_video.CN_DOMAIN = base


def _prepare_env(monkeypatch, tmp_path) -> None:
    """出片下载目录挪到 tmp，并让下载走直连（夹具在 127.0.0.1，不该吃 SOCKS）。"""
    dl = tmp_path / "downloads"
    dl.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", str(dl))
    monkeypatch.setattr(config, "DOWNLOAD_DIRECT", True)
    monkeypatch.setattr(config, "PUBLIC_BASE", "http://127.0.0.1:8002")


def _install_http_shim(monkeypatch) -> None:
    """把客户端里的 https:// 改写成 http://，好让本地夹具（明文 HTTP）能接住请求。

    被测代码按真实协议构造 https:// URL —— 这一点本身就是断言目标之一，
    所以这里不改被测代码，只在 aiohttp 这一层做 scheme 降级。
    """
    real_request = aiohttp.ClientSession.request
    real_get = aiohttp.ClientSession.get
    real_put = aiohttp.ClientSession.put
    real_post = aiohttp.ClientSession.post

    def down(url: str) -> str:
        if isinstance(url, str) and url.startswith("https://127.0.0.1:"):
            return "http://" + url[len("https://"):]
        return url

    # ★ 注意：ClientSession.get/post/put 是**同步**方法，返回可以被 `async with`
    #   使用的上下文管理器（_RequestContextManager），不能包成 async def。
    def request(self, method, url, *a, **kw):
        return real_request(self, method, down(url), *a, **kw)

    def get(self, url, *a, **kw):
        return real_get(self, down(url), *a, **kw)

    def put(self, url, *a, **kw):
        return real_put(self, down(url), *a, **kw)

    def post(self, url, *a, **kw):
        return real_post(self, down(url), *a, **kw)

    monkeypatch.setattr(aiohttp.ClientSession, "request", request)
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)
    monkeypatch.setattr(aiohttp.ClientSession, "put", put)
    monkeypatch.setattr(aiohttp.ClientSession, "post", post)


def test_reference_image_upload_then_generate_full_chain(tmp_path, monkeypatch):
    """核心：参考图 → TOS 上传 → attachment_block → 出片 → 下载 → 记账。"""

    async def scenario():
        async with _Ctx() as ctx:
            _point_clients_at(ctx.port)
            _install_http_shim(monkeypatch)
            _prepare_env(monkeypatch, tmp_path)
            monkeypatch.setattr(doubao_pool, "UPLOAD_DIR", tmp_path / "up")

            st = DoubaoAccountStore(tmp_path / "dbao")
            pool = DoubaoPool(store=st, proxy="", daily_limit=2)
            pool.add("db001", CK, {"login_ok": True})

            ref = tmp_path / "ref.png"
            from PIL import Image

            Image.new("RGB", (16, 16), (7, 8, 9)).save(ref, format="PNG")

            res = await pool.test_generate(
                "db001", "一只猫在跑", ratio="16:9", duration=5,
                model="seedance-2.0", image_paths=[str(ref)])

            assert res["ok"] is True, res
            assert Path(res["local_path"]).is_file()
            assert Path(res["local_path"]).read_bytes() == MP4
            assert res["video_url"].endswith(".mp4")
            # 真实 conversation_id（不是 local_ 占位）应落到任务记录里
            assert res["conversation_id"] == "98765432101234567", res["conversation_id"]

            fx = ctx.fx
            # ② prepare_upload 参数
            p1 = fx.get("prepare_upload")
            assert p1 is not None, fx.calls
            assert p1["body"] == {"tenant_id": "5", "scene_id": "5",
                                  "resource_type": 2}, p1["body"]
            assert p1["query"]["aid"] == "497858"
            assert p1["query"]["device_platform"] == "web"

            # ③ ApplyImageUpload 带 AWS4 签名与必要参数
            p2 = fx.get("ApplyImageUpload")
            assert p2 is not None, fx.calls
            assert p2["query"]["Action"] == "ApplyImageUpload"
            assert p2["query"]["ServiceId"] == "svc-1"
            assert p2["query"]["UploadNum"] == "1"
            assert p2["query"]["FileSize"] == str(ref.stat().st_size)
            assert "AWS4-HMAC-SHA256" in p2["auth"], p2["auth"][:60]
            assert p2["xdate"], "缺少 X-Amz-Date"

            # ④ PUT TOS：字节一致 + crc32 头
            p3 = fx.get("tos_put")
            assert p3 is not None, fx.calls
            assert p3["path"] == "/" + STORE_URI
            assert p3["data"] == ref.read_bytes()
            assert p3["auth"] == TOS_AUTH
            assert len(p3["crc32"]) == 8, p3["crc32"]

            # ⑤ /chat/completion 的 content_block 结构
            p4 = fx.get("completion")
            assert p4 is not None, fx.calls
            assert p4["query"]["aid"] == "497858"
            blocks = p4["body"]["messages"][0]["content_block"]
            assert [b["block_type"] for b in blocks] == [10052, 10000], \
                [b["block_type"] for b in blocks]
            att = blocks[0]["content"]["attachment_block"]["attachments"][0]
            assert att["type"] == 1
            assert att["image"]["uri"] == STORE_URI
            assert att["upload_status"] == 1
            # 关键：body 里必须带豆包域 bot_id，否则会话建不起来
            assert p4["body"]["client_meta"]["bot_id"] == "7338286299411103781"
            assert p4["body"]["chat_ability"]["ability_type"] == 17
            ap = json.loads(p4["body"]["chat_ability"]["ability_param"])
            assert ap == {"ratio": "16:9", "model": "seedance_v2.0", "duration": 5}, ap
            assert blocks[1]["content"]["text_block"]["text"].startswith("生成影片：")

            # ⑥ 轮询：必须是 uplink_body 包装 + im 版头
            p5 = fx.get("chain_single")
            assert p5 is not None, fx.calls
            assert p5["ctype"] == "application/json; encoding=utf-8", p5["ctype"]
            assert p5["agw"] == "str", p5["agw"]
            assert p5["body"]["cmd"] == 3100
            up = p5["body"]["uplink_body"]["pull_singe_chain_uplink_body"]
            assert up["conversation_id"] == "98765432101234567"
            assert up["conversation_type"] == 3

            # ⑦ 下载 + 记账
            meta = st.roll_day(st.read_meta("db001"))
            assert meta["used_today"] == 1, meta

    asyncio.run(scenario())


def test_generate_without_image_uses_text_block_only(tmp_path, monkeypatch):
    """无参考图时不应出现 10052 附件块，也不应调到上传接口。"""

    async def scenario():
        async with _Ctx() as ctx:
            _point_clients_at(ctx.port)
            _install_http_shim(monkeypatch)
            _prepare_env(monkeypatch, tmp_path)
            st = DoubaoAccountStore(tmp_path / "dbao2")
            pool = DoubaoPool(store=st, proxy="", daily_limit=2)
            pool.add("db001", CK, {"login_ok": True})

            res = await pool.test_generate("db001", "一只狗", ratio="9:16",
                                           duration=10, model="seedance-2.5")
            assert res["ok"] is True, res
            fx = ctx.fx
            assert fx.get("prepare_upload") is None, "无参考图不该调上传"
            assert fx.get("tos_put") is None
            blocks = fx.get("completion")["body"]["messages"][0]["content_block"]
            assert [b["block_type"] for b in blocks] == [10000]
            ap = json.loads(
                fx.get("completion")["body"]["chat_ability"]["ability_param"])
            assert ap == {"ratio": "9:16", "model": "seedance_v2.5", "duration": 10}, ap
            assert st.read_meta("db001")["used_today"] == 1

    asyncio.run(scenario())


def test_upload_failure_does_not_charge_quota(tmp_path, monkeypatch):
    """上传失败要如实报错，且**不能**记账（避免白扣额度）。"""

    async def scenario():
        async with _Ctx() as ctx:
            _point_clients_at(ctx.port)
            _install_http_shim(monkeypatch)
            _prepare_env(monkeypatch, tmp_path)
            st = DoubaoAccountStore(tmp_path / "dbao3")
            pool = DoubaoPool(store=st, proxy="", daily_limit=2)
            pool.add("db001", CK, {"login_ok": True})
            ctx.fx.fail_prepare = True

            ref = tmp_path / "ref.png"
            from PIL import Image

            Image.new("RGB", (8, 8), (1, 2, 3)).save(ref, format="PNG")

            res = await pool.test_generate("db001", "x", image_paths=[str(ref)])
            assert res["ok"] is False, res
            assert "prepare_upload" in res["error"] or "710012001" in res["error"], res
            assert st.roll_day(st.read_meta("db001"))["used_today"] == 0

    asyncio.run(scenario())
