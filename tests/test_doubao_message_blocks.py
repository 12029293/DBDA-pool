# -*- coding: utf-8 -*-
"""回归测试：豆包消息体新格式（`content_block`）+ 纯文本「参数确认」自动应答。

背景（2026-10-14 真号实测定位）：
上游把消息体换成了 `content_block`（`message_body_version="1"`、
ext 里 `use_content_block="1"`），`content` 变成**空字符串**。而
`_poll_video` 旧实现只读 `msg["content"]` → `json.loads("")` 抛错 →
**整轮消息一个块都没扫**，于是：
  · 视频块 2074 永远扫不到 → 任务必然「豆包域视频生成超时未出片」；
  · 助手先回的纯文本「**视频生成参数确认**…**确认后我再开始生成视频。**」
    扫不到 → 从不补发「确认」→ 上游一直不排产；
  · 失败诊断里只剩 `last_downlink_keys=['pull_singe_chain_downlink_body']`
    （消息明明有 2~4 条），完全看不出真因。

本文件用本地 aiohttp 夹具按**新版消息体**应答，逐条锁住：
  ① 新版 `content_block` 里的 2074 视频块能被识别并返回 download_url；
  ② 新版里的纯文本参数确认能被识别，并在**同一会话**补发确认文本；
  ③ 旧版 `content` JSON 字符串仍然兼容（不回归）；
  ④ 超时诊断里能看到扫到的块类型（blocks=…），不再是空白诊断。
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

# 与既有豆包夹具同款：测试环境没有 patchright，打桩掉
pr = types.ModuleType("patchright")
pa = types.ModuleType("patchright.async_api")
pa.async_playwright = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stub"))
pr.async_api = pa
sys.modules.setdefault("patchright", pr)
sys.modules.setdefault("patchright.async_api", pa)

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import doubao_cn_video  # noqa: E402
from doubao_cn_video import (  # noqa: E402
    TEXT_CONFIRM_REPLY, DoubaoButtonConfirmError, DoubaoPaidQuotaError,
    DoubaoVideoClient,
)

CK = ("sessionid=" + "a" * 32 + "; sessionid_ss=" + "a" * 32
      + "; ttwid=tt; has_biz_token=false")

CONV = "38447183010127618"

# 补发确认请求的两种响应形态
SSE_ACK = ("event: SSE_HEARTBEAT\ndata: {}\n\n"
           "event: SSE_ACK\ndata: "
           + json.dumps({"ack_client_meta": {"conversation_id": CONV,
                                             "conversation_type": 3}})
           + "\n\nevent: FULL_MSG_NOTIFY\ndata: {}\n\n")
SSE_HEARTBEAT_ONLY = "event: SSE_HEARTBEAT\ndata: {}\n\n"
SSE_GATEWAY_ERROR = ('event: gateway-error\ndata: {"error_code": 710022002, '
                     '"error_msg": "rate limited"}\n\n')

# 真号实测原文（节选）：助手先回参数确认，服务端等用户回话
PARAM_CONFIRM_TEXT = (
    "我将为你生成一只橘猫在草地上追逐蝴蝶的自然电影感视频，请先确认以下参数：\n\n"
    "**视频生成参数确认**\n\n- **模型：** Seedance 2.5\n- **时长：** 30 秒\n"
    "- **比例：** 16:9 横屏\n\n确认后我再开始生成视频。"
)


def _text_block(text: str) -> dict:
    return {"block_type": 10000,
            "content": {"text_block": {"text": text}, "pc_event_block": ""}}


def _video_block(url: str) -> dict:
    return {"block_type": 2074,
            "content": {"creation_block": {"creations": [
                {"type": 2, "video": {"download_url": url}}]}}}


def _button_block(scene: str) -> dict:
    """10103「确认生成」按钮卡（真号实测形态，含 creation_btn_rely_info）。"""
    return {"block_type": 10103, "block_id": "40437601338474498",
            "content": {"pc_event_block": "", "button_block": {
                "display_text": "确认生成", "command": 3, "scheme_url": "",
                "scene": scene, "invisible": False, "icon_url": "",
                "dark_icon_url": "", "message_text": "",
                "extra": {"creation_btn_rely_info":
                          '{"raw_req_msg_id":58493612262762498,'
                          '"scene":"%s","attach_message_ids":[58493612262762498]}'
                          % scene}}}}


PAID_QUOTA_TEXT = ("今日视频生成免费额度已用完，本次将消耗付费额度。是否继续生成？")


class Fixture:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.messages: list[dict] = []      # 新版：message["content_block"]
        self.legacy_messages: list[dict] = []   # 旧版：message["content"] JSON 串
        # ★ 2026-10-14 新增：模拟「原请求的 SSE 流还没关闭」
        self.stream_open = False
        self.violations: list[str] = []
        # 补发确认请求的响应：默认 SSE_ACK（受理）；可改成只回心跳（被静默丢弃）
        self.confirm_sse = SSE_ACK

    def rec(self, name: str, **kw) -> None:
        self.calls.append({"name": name, **kw})

    def all(self, name: str) -> list[dict]:
        return [c for c in self.calls if c["name"] == name]


def build_app(fx: Fixture) -> web.Application:
    app = web.Application()

    async def completion(request):
        raw = await request.read()
        body = json.loads(raw.decode())
        # 「补发确认」= 带 client_meta.conversation_id 的续聊请求
        is_confirm = bool((body.get("client_meta") or {}).get("conversation_id"))
        if is_confirm and fx.stream_open:
            # ★ 真根因回归点：流未关闭时发的确认会被上游静默丢弃
            fx.violations.append("confirm-while-stream-open")
        fx.rec("completion", body=body)

        if is_confirm:
            return web.Response(text=fx.confirm_sse,
                                content_type="text/event-stream")

        # 首次受理：推一段「参数确认」文本，并**保持流打开**一小段时间，
        # 模拟服务端「发完这段就结束本轮」之前客户端还在流里的窗口。
        resp = web.StreamResponse(
            headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        fx.stream_open = True
        try:
            await resp.write(("event: SSE_ACK\ndata: "
                              + json.dumps({"ack_client_meta": {
                                  "conversation_id": CONV,
                                  "local_conversation_id": "local_1",
                                  "conversation_type": 3}})
                              + "\n\n").encode())
            await resp.write(('event: CHUNK_DELTA\ndata: '
                              + json.dumps({"text": PARAM_CONFIRM_TEXT},
                                           ensure_ascii=False)
                              + "\n\n").encode())
            await asyncio.sleep(0.15)      # 客户端此时仍在读流
        finally:
            fx.stream_open = False
        await resp.write_eof()
        return resp

    async def chain_single(request):
        body = await request.json()
        fx.rec("chain_single", body=body,
               ctype=request.headers.get("content-type", ""))
        msgs = list(fx.messages)
        msgs += [{"content": json.dumps(m["content_block"])}
                 for m in fx.legacy_messages]
        payload = {"status_code": 0, "downlink_body": {
            "pull_singe_chain_downlink_body": {"messages": msgs}}}
        return web.json_response(payload)

    app.router.add_post("/chat/completion", completion)
    app.router.add_post("/im/chain/single", chain_single)
    return app


class _Ctx:
    def __init__(self, fx: Fixture):
        self.fx = fx
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


def _install_http_shim(monkeypatch) -> None:
    real_request = aiohttp.ClientSession.request
    real_post = aiohttp.ClientSession.post

    def down(url: str) -> str:
        if isinstance(url, str) and url.startswith("https://127.0.0.1:"):
            return "http://" + url[len("https://"):]
        return url

    def request(self, method, url, *a, **kw):
        return real_request(self, method, down(url), *a, **kw)

    def post(self, url, *a, **kw):
        return real_post(self, down(url), *a, **kw)

    monkeypatch.setattr(aiohttp.ClientSession, "request", request)
    monkeypatch.setattr(aiohttp.ClientSession, "post", post)


def _client(port: int) -> DoubaoVideoClient:
    doubao_cn_video.CN_DOMAIN = f"127.0.0.1:{port}"
    cli = DoubaoVideoClient(CK)
    cli.poll_interval = 0.01     # 夹具里不必真等 5 秒
    return cli


# ---------------- ① 纯函数：块/文本提取 ----------------

def test_message_blocks_prefers_content_block():
    """新版：块在 content_block（content 是空串）——旧实现就是在这一步全丢的。"""
    msg = {"content": "", "content_block": [_text_block("你好")]}
    blocks = DoubaoVideoClient._message_blocks(msg)
    assert len(blocks) == 1 and blocks[0]["block_type"] == 10000


def test_message_blocks_supports_legacy_json_string():
    """旧版：content 是二次序列化的 JSON 字符串，仍然兼容（不回归）。"""
    msg = {"content": json.dumps([_text_block("旧格式")])}
    blocks = DoubaoVideoClient._message_blocks(msg)
    assert len(blocks) == 1


def test_message_blocks_handles_garbage():
    assert DoubaoVideoClient._message_blocks({"content": ""}) == []
    assert DoubaoVideoClient._message_blocks({"content": "{not json"}) == []
    assert DoubaoVideoClient._message_blocks({}) == []
    assert DoubaoVideoClient._message_blocks(None) == []


def test_text_deltas_collects_chunk_delta_and_patch_op():
    assert DoubaoVideoClient._text_deltas({"text": "确认后"}) == "确认后"
    patch = {"patch_op": [{"patch_value": {"content_block": [
        _text_block("我再开始生成视频。")]}}]}
    assert DoubaoVideoClient._text_deltas(patch) == "我再开始生成视频。"
    # 不认 content（避免把整段 JSON 拼进缓冲）
    assert DoubaoVideoClient._text_deltas({"content": "不许进缓冲"}) == ""


def test_needs_text_confirm_matches_real_reply():
    assert DoubaoVideoClient._needs_text_confirm(PARAM_CONFIRM_TEXT)
    assert not DoubaoVideoClient._needs_text_confirm("视频已经生成好了")


# ---------------- ② 轮询：新版格式必须能拿到视频 ----------------

def test_poll_video_finds_creation_block_in_new_format(tmp_path, monkeypatch):
    """核心回归：消息体是新版 content_block 时，2074 视频块必须被扫到。

    同时覆盖「没出片时会在同一会话补发确认」的语义——但**出片优先**：
    一旦扫到 2074 就立刻返回 URL，不再多补确认（2026-10-14 调整）。
    """
    fx = Fixture()
    fx.messages = [{"content": "", "content_block": [_text_block(
        PARAM_CONFIRM_TEXT)]}]

    async def scenario():
        async with _Ctx(fx) as ctx:
            _install_http_shim(monkeypatch)
            cli = _client(ctx.port)
            monkeypatch.setattr(doubao_cn_video, "CN_DOMAIN", f"127.0.0.1:{ctx.port}")

            # 阶段一：只有「参数确认」文本、没有视频 → 必须补发确认，然后超时
            cli.poll_interval = 0.05
            with pytest.raises(Exception):
                await cli._poll_video(CONV, timeout=1, conversation_type=3)
            convs = [c["body"]["client_meta"]["conversation_id"]
                     for c in fx.all("completion")]
            assert CONV in convs, f"未在会话内补发确认，completion 调用={convs}"
            sent = [c["body"]["messages"][0]["content_block"][0]
                    ["content"]["text_block"]["text"]
                    for c in fx.all("completion")]
            assert TEXT_CONFIRM_REPLY in sent
            assert cli.text_confirmed is True

            # 阶段二：出现新版 content_block 里的 2074 视频块 → 必须被扫到
            fx.messages = [{"content": "", "content_block": [
                _video_block("http://x/out.mp4")]}]
            url = await cli._poll_video(CONV, timeout=5, conversation_type=3)
            assert url == "http://x/out.mp4"
            # 轮询请求本身仍须带 im 版 content-type（少了 → 712012002）
            assert fx.all("chain_single")[0]["ctype"] == \
                "application/json; encoding=utf-8"

    asyncio.run(scenario())


def test_poll_video_supports_legacy_content(tmp_path, monkeypatch):
    """旧格式（content=JSON 串）也必须继续可用。"""
    fx = Fixture()
    fx.legacy_messages = [{"content_block": [_video_block("http://x/legacy.mp4")]}]

    async def scenario():
        async with _Ctx(fx) as ctx:
            _install_http_shim(monkeypatch)
            cli = _client(ctx.port)
            url = await cli._poll_video(CONV, timeout=5, conversation_type=3)
            assert url == "http://x/legacy.mp4"

    asyncio.run(scenario())


def test_poll_timeout_diag_reports_blocks(tmp_path, monkeypatch):
    """超时诊断必须能看出「扫到了哪些块」，而不是只剩 last_downlink_keys。"""
    fx = Fixture()
    fx.messages = [{"content": "", "content_block": [
        _text_block("视频正在生成中，请稍候"), {"block_type": 10040, "content": {}}]}]

    async def scenario():
        async with _Ctx(fx) as ctx:
            _install_http_shim(monkeypatch)
            cli = _client(ctx.port)
            cli.poll_interval = 0.1
            with pytest.raises(Exception) as ei:
                await cli._poll_video(CONV, timeout=1.0, conversation_type=3)
            msg = str(ei.value)
            assert "超时未出片" in msg
            assert "blocks=" in msg, f"诊断缺少块统计: {msg}"
            assert "10040" in msg
            assert "text_confirmed=" in msg

    asyncio.run(scenario())


def test_poll_timeout_default_uses_env(monkeypatch):
    """默认超时来自 DOUBAO_VIDEO_TIMEOUT（上游自述 30 秒片要等 15 分钟）。"""
    assert doubao_cn_video.POLL_TIMEOUT_SEC >= 600


# ---------------- ③ 补发「确认」的时序与响应校验（2026-10-14 真根因） ----------------
#
# 真号实测对照：
#   · 在原请求 SSE 流**未关闭**时补发确认 → 上游静默丢弃：会话里根本不出现该
#     消息（只读到「用户提示词 + 助手参数确认」2 条），任务必然超时；
#   · 等流关闭后补发（手工，_diag_confirm_db001.log）→ HTTP 200 + SSE_ACK
#     → 会话 2→4 条 → 上游开始排产。
# 旧实现的两个叠加缺陷：
#   1) 在 `_on_sse_event`（流内）直接 `_send_text_confirmation`；
#   2) 发完不看响应就无条件 `text_confirmed = True` → 流尾/轮询兜底全被跳过。

def test_confirm_response_ok_accepts_sse_ack():
    ok, reason = DoubaoVideoClient._confirm_response_ok(
        [("SSE_HEARTBEAT", {}),
         ("SSE_ACK", {"ack_client_meta": {"conversation_id": CONV}})], CONV)
    assert ok, reason


def test_confirm_response_ok_accepts_productive_events():
    ok, _ = DoubaoVideoClient._confirm_response_ok(
        [("FULL_MSG_NOTIFY", {"message": {}})], CONV)
    assert ok


def test_confirm_response_ok_rejects_heartbeat_only():
    """只回心跳 = 典型的「被静默丢弃」，绝不能算受理。"""
    ok, reason = DoubaoVideoClient._confirm_response_ok(
        [("SSE_HEARTBEAT", {}), ("SSE_HEARTBEAT", {})], CONV)
    assert not ok and "静默丢弃" in reason


def test_confirm_response_ok_rejects_empty_and_mismatch():
    ok, reason = DoubaoVideoClient._confirm_response_ok([], CONV)
    assert not ok and "无任何 SSE 事件" in reason
    ok, reason = DoubaoVideoClient._confirm_response_ok(
        [("SSE_ACK", {"ack_client_meta": {"conversation_id": "999"}})], CONV)
    assert not ok and "不一致" in reason


def test_confirm_response_ok_rejects_gateway_error():
    ok, reason = DoubaoVideoClient._confirm_response_ok(
        [("SSE_ACK", {"ack_client_meta": {"conversation_id": CONV}}),
         ("gateway-error", {"error_code": 710022002})], CONV)
    assert not ok and "上游拒绝" in reason


def test_generate_video_confirms_only_after_stream_closed(tmp_path, monkeypatch):
    """★ 核心回归：补发确认必须发生在**原 SSE 流关闭之后**。

    夹具会在「流未关闭时收到确认请求」记一条 violation——旧实现在这里必挂。
    """
    fx = Fixture()
    fx.messages = [
        {"content": "", "content_block": [_text_block(PARAM_CONFIRM_TEXT)]},
        {"content": "", "content_block": [_video_block("http://x/ok.mp4")]},
    ]

    async def scenario():
        async with _Ctx(fx) as ctx:
            _install_http_shim(monkeypatch)
            cli = _client(ctx.port)
            monkeypatch.setattr(doubao_cn_video, "CN_DOMAIN",
                                f"127.0.0.1:{ctx.port}")
            url = await cli.generate_video("一只橘猫在草地上追蝴蝶",
                                           ratio="9:16", duration=5)
            assert url == "http://x/ok.mp4"
            assert fx.violations == [], (
                "确认请求在原 SSE 流关闭前发出，会被上游静默丢弃：%s"
                % fx.violations)
            confirms = [c for c in fx.all("completion")
                        if (c["body"].get("client_meta") or {})
                        .get("conversation_id")]
            texts = [c["body"]["messages"][0]["content_block"][0]["content"]
                     ["text_block"]["text"] for c in confirms]
            assert texts == [TEXT_CONFIRM_REPLY], f"确认发送次数异常: {texts}"
            assert cli.text_confirmed is True

    asyncio.run(scenario())


def test_poll_video_retries_confirmation_until_accepted(tmp_path, monkeypatch):
    """上游把确认请求静默丢弃时，必须在后续轮次**重试**（旧实现只发一次就放弃）。"""
    fx = Fixture()
    fx.confirm_sse = SSE_HEARTBEAT_ONLY     # 每次都「被静默丢弃」
    fx.messages = [{"content": "", "content_block": [
        _text_block("视频正在生成中，请稍候")]}]

    async def scenario():
        async with _Ctx(fx) as ctx:
            _install_http_shim(monkeypatch)
            cli = _client(ctx.port)
            monkeypatch.setattr(doubao_cn_video, "CN_DOMAIN",
                                f"127.0.0.1:{ctx.port}")
            # 直接调轮询：先让首轮标记 pending，再靠轮次重试
            cli._pending_text_confirm = True
            monkeypatch.setattr(doubao_cn_video, "CONFIRM_RETRY_POLLS", 2)
            with pytest.raises(Exception) as ei:
                await cli._poll_video(CONV, timeout=2, conversation_type=3)
            msg = str(ei.value)
            assert "超时未出片" in msg
            assert cli.text_confirmed is False
            sends = [c for c in fx.all("completion")
                     if (c["body"].get("client_meta") or {}).get("conversation_id")]
            assert len(sends) >= 2, f"确认只发了 {len(sends)} 次，没有重试"
            assert "text_confirm_err" in msg

    asyncio.run(scenario())


def test_poll_video_no_confirm_when_no_param_prompt(tmp_path, monkeypatch):
    """没有下发「参数确认」提示时不该瞎补发确认（避免刷屏/误触发）。"""
    fx = Fixture()
    fx.messages = [{"content": "", "content_block": [
        _text_block("视频正在生成中，请稍候")]}]

    async def scenario():
        async with _Ctx(fx) as ctx:
            _install_http_shim(monkeypatch)
            cli = _client(ctx.port)
            monkeypatch.setattr(doubao_cn_video, "CN_DOMAIN",
                                f"127.0.0.1:{ctx.port}")
            cli.poll_interval = 0.05
            with pytest.raises(Exception):
                await cli._poll_video(CONV, timeout=1, conversation_type=3)
            assert fx.all("completion") == []

    asyncio.run(scenario())


# ---------------- ④ 付费额度确认卡（10103 / scene=gen_video_by_downgraded_model） ----------------
#
# 真号实测（2026-10-14）：账号**今日免费额度已用完**时，上游先回文本
#   「今日视频生成免费额度已用完，本次将消耗付费额度。是否继续生成？」
# 再挂一张 10103 按钮卡。它与「素材授权卡」的关键区别：
#   · 素材授权 → 账号级一次性，点完永久生效（manual）
#   · 付费确认 → **每次免费额度耗尽后都重现**，换号可解（paid）
# 故必须单独归类：把该号临时挪出调度，让号池换到仍有免费额度的号。

def test_poll_video_paid_quota_button_raises_paid_error(tmp_path, monkeypatch):
    fx = Fixture()
    fx.messages = [{"content": "", "content_block": [
        _text_block(PAID_QUOTA_TEXT),
        _button_block("gen_video_by_downgraded_model")]}]

    async def scenario():
        async with _Ctx(fx) as ctx:
            _install_http_shim(monkeypatch)
            cli = _client(ctx.port)
            monkeypatch.setattr(doubao_cn_video, "CN_DOMAIN",
                                f"127.0.0.1:{ctx.port}")
            with pytest.raises(DoubaoPaidQuotaError) as ei:
                await cli._poll_video(CONV, timeout=5, conversation_type=3)
            assert ei.value.scene == "gen_video_by_downgraded_model"
            assert "免费额度已用完" in str(ei.value)
            assert cli._poll_debug.get("stuck_button") == \
                "gen_video_by_downgraded_model"

    asyncio.run(scenario())


def test_poll_video_asset_auth_button_stays_manual(tmp_path, monkeypatch):
    """素材授权卡（一次性）不得被误判为「付费额度」——否则会把好号挪出调度。"""
    fx = Fixture()
    fx.messages = [{"content": "", "content_block": [
        _button_block("creation_portrait_video_auth_confirm")]}]

    async def scenario():
        async with _Ctx(fx) as ctx:
            _install_http_shim(monkeypatch)
            cli = _client(ctx.port)
            monkeypatch.setattr(doubao_cn_video, "CN_DOMAIN",
                                f"127.0.0.1:{ctx.port}")
            with pytest.raises(DoubaoButtonConfirmError) as ei:
                await cli._poll_video(CONV, timeout=5, conversation_type=3)
            assert not isinstance(ei.value, DoubaoPaidQuotaError)
            assert "仅客户端可完成" in str(ei.value)

    asyncio.run(scenario())


def test_classify_failure_paid_before_manual(tmp_path):
    """池层分类：paid 必须先于 manual 命中（DoubaoPaidQuotaError 是其子类）。"""
    from doubao_pool import DoubaoAccountStore, DoubaoPool
    pool = DoubaoPool(store=DoubaoAccountStore(root=tmp_path))
    assert pool._classify_failure(
        DoubaoPaidQuotaError("今日免费额度已用完", scene="x")) == "paid"
    assert pool._classify_failure(
        Exception("豆包域视频生成超时未出片 … 免费额度已用完…")) == "paid"
    assert pool._classify_failure(
        DoubaoButtonConfirmError("豆包下发「确认生成」按钮卡（scene=a），"
                                 "请在豆包 App/网页里点击确认")) == "manual"
    assert pool._classify_failure(
        Exception("豆包域视频生成超时未出片 blocks={10000: 2}")) == "timeout"
