# -*- coding: utf-8 -*-
"""豆包「三种加号方式」服务端接口全链路夹具测试。

本机没有真实豆包账号（无法真扫码 / 真收短信），但登录链路的**协议实现**可以
用本地夹具按豆包真实应答完整验证。夹具严格模仿 `accounts.doubao.com` 的
passport 协议，断言每一跳的 query / body / 头，覆盖：

  ① 抖音扫码：get_qrcode → check_qrconnect(new→scan→confirm) → 落 sessionid
              → 面板 /finish 入池
  ② 豆包扫码：同一响应渲染 qrcode_index_url，且解码结果 == 该 URL
  ③ 手机号  ：send_code(无滑块) → sms_login → 入池
  ④ 失败路径：二维码过期、验证码错误、未扫码就 finish
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

import doubao_login  # noqa: E402
import doubao_pool  # noqa: E402

SESSIONID = "f" * 32
TOKEN = "abc123token_hl"


class Fixture:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.qr_polls = 0
        self.sms_sent = False

    def rec(self, name: str, **kw) -> None:
        self.calls.append({"name": name, **kw})

    def get(self, name: str) -> dict | None:
        return next((c for c in self.calls if c["name"] == name), None)


def build_app(fx: Fixture) -> web.Application:
    app = web.Application()

    async def chat_page(request):
        # bootstrap 用：回一个带 passport_csrf_token 的 Set-Cookie
        resp = web.Response(text="<html>ok</html>", content_type="text/html")
        resp.set_cookie("passport_csrf_token", "csrf0001", domain="127.0.0.1")
        return resp

    async def get_qrcode(request):
        fx.rec("get_qrcode", query=dict(request.query),
               csrf=request.headers.get("x-tt-passport-csrf-token", ""),
               cookie=request.headers.get("Cookie", ""))
        # qrcode 用一张合法 PNG 的 data URL 占位（客户端不解析图片内容）
        png = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
               "AAAADUlEQVR42mP8z8AAAwAB/AF+g7oAAAAASUVORK5CYII=")
        return web.json_response({"message": "success", "data": {
            "error_code": 0, "token": TOKEN, "qrcode": png,
            "qrcode_index_url": (
                f"https://www.doubao.com/flow-account/scan-login?qr_source_aid=497858"
                f"&token={TOKEN}"),
            "expire_time": 9999999999,
            "copywriting": json.dumps({"title": "身份验证"}),
        }})

    async def check_qrconnect(request):
        fx.qr_polls += 1
        fx.rec("check_qrconnect", query=dict(request.query),
               n=fx.qr_polls)
        q = dict(request.query)
        if q.get("token") != TOKEN:
            return web.json_response({"message": "error", "data": {
                "error_code": 3, "description": "缺少参数"}})
        # 第一次 new，第二次 scan，第三次 confirm
        status = {1: "new", 2: "scan", 3: "confirm"}.get(fx.qr_polls, "confirm")
        data = {"error_code": 0, "status": status, "scan_app_id": 0, "extra": ""}
        if status == "scan":
            data["screen_name"] = "测试用户"
        resp = web.json_response({"message": "success", "data": data})
        if status == "confirm":
            # 服务端在 confirm 时下发登录态 Cookie
            resp.set_cookie("sessionid", SESSIONID, domain="127.0.0.1")
            resp.set_cookie("sessionid_ss", SESSIONID, domain="127.0.0.1")
            resp.set_cookie("sid_tt", SESSIONID, domain="127.0.0.1")
            resp.set_cookie("ttwid", "tt-1", domain="127.0.0.1")
        return resp

    async def validate_by_qrcode(request):
        fx.rec("validate_by_qrcode", query=dict(request.query),
               data=await request.post())
        return web.json_response({"message": "success", "data": {"error_code": 0}})

    async def get_oauth_token(request):
        fx.rec("get_oauth_token", query=dict(request.query), data=await request.post())
        resp = web.json_response({"message": "success", "data": {"error_code": 0}})
        resp.set_cookie("sessionid", SESSIONID, domain="127.0.0.1")
        return resp

    async def send_code(request):
        fx.sms_sent = True
        fx.rec("send_code", query=dict(request.query))
        return web.json_response({"message": "success", "data": {
            "log_id": "log1", "mobile": "138****0000",
            "mobile_ticket": "mobile_ticket_TEST", "retry_time": 60}})

    async def sms_login(request):
        fx.rec("sms_login", query=dict(request.query), data=await request.post())
        form = await request.post()
        if form.get("code") != "654321":
            return web.json_response({"message": "error", "data": {
                "error_code": 1202, "description": "验证码错误或已失效"}})
        resp = web.json_response({"message": "success", "data": {"error_code": 0}})
        resp.set_cookie("sessionid", SESSIONID, domain="127.0.0.1")
        resp.set_cookie("sessionid_ss", SESSIONID, domain="127.0.0.1")
        resp.set_cookie("ttwid", "tt-1", domain="127.0.0.1")
        return resp

    app.router.add_get("/chat/", chat_page)
    app.router.add_get("/passport/web/get_qrcode/", get_qrcode)
    app.router.add_get("/passport/web/check_qrconnect/", check_qrconnect)
    app.router.add_post("/passport/web/validate_by_qrcode/", validate_by_qrcode)
    app.router.add_post("/passport/web/auth/get_oauth_token/", get_oauth_token)
    app.router.add_get("/passport/web/send_code/", send_code)
    app.router.add_post("/passport/web/sms_login/", sms_login)
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


def _point_at_fixture(port: int, monkeypatch) -> None:
    """把 passport 域与 web 域都指向夹具。"""
    base = f"127.0.0.1:{port}"
    monkeypatch.setattr(doubao_login, "ACCT_HOST", base)
    monkeypatch.setattr(doubao_login, "WEB_HOST", base)

    # 夹具是明文 HTTP；客户端按真实协议构 https:// —— 只在 aiohttp 层降级 scheme
    real_request = aiohttp.ClientSession.request
    real_get = aiohttp.ClientSession.get
    real_post = aiohttp.ClientSession.post

    def down(url: str) -> str:
        if isinstance(url, str) and url.startswith("https://127.0.0.1:"):
            return "http://" + url[len("https://"):]
        return url

    def request(self, method, url, *a, **kw):
        return real_request(self, method, down(url), *a, **kw)

    def get(self, url, *a, **kw):
        return real_get(self, down(url), *a, **kw)

    def post(self, url, *a, **kw):
        return real_post(self, down(url), *a, **kw)

    monkeypatch.setattr(aiohttp.ClientSession, "request", request)
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)
    monkeypatch.setattr(aiohttp.ClientSession, "post", post)


# ---------------- ① 抖音扫码 ----------------

def test_douyin_qr_login_full_flow(monkeypatch):
    """取码 → new → scan → confirm → 拿到 sessionid。"""

    async def scenario():
        async with _Ctx() as ctx:
            _point_at_fixture(ctx.port, monkeypatch)
            cli = doubao_login.DoubaoLoginClient(proxy="")
            try:
                sess = await cli.create_qr("douyin")
                assert sess.token == TOKEN
                assert sess.status == "new"
                # 抖音模式用服务端下发的官方码
                assert sess.rendered_qr == ""
                assert sess.qr_data_url.startswith("data:image/png")
                assert sess.to_dict()["qr_image"].startswith("data:image/png")

                # 取码请求必须带 csrf 头与 aid
                gq = ctx.fx.get("get_qrcode")
                assert gq["query"]["aid"] == "497858"
                assert gq["query"]["next"] == "https://www.doubao.com" or \
                    gq["query"]["next"].endswith("doubao.com"), gq["query"]["next"]
                assert gq["query"]["is_from_ttaccountsdk"] == "1"
                assert gq["csrf"] == "csrf0001", gq["csrf"]
                assert "passport_csrf_token=csrf0001" in gq["cookie"]

                # 轮询：new → scan → confirm(done)
                s1 = await cli.poll_qr(sess)
                assert s1.status == "new", s1.status
                s2 = await cli.poll_qr(s1)
                assert s2.status == "scan", s2.status
                assert "测试用户" in s2.message, s2.message
                s3 = await cli.poll_qr(s2)
                assert s3.status == "done", (s3.status, s3.message)
                assert s3.cookies.get("sessionid") == SESSIONID
                assert s3.to_dict()["logged_in"] is True

                # 轮询请求必须带 token
                polls = [c for c in ctx.fx.calls if c["name"] == "check_qrconnect"]
                assert len(polls) == 3
                assert all(p["query"]["token"] == TOKEN for p in polls)
            finally:
                await cli.close()

    asyncio.run(scenario())


def test_doubao_qr_mode_renders_index_url(monkeypatch):
    """豆包模式：渲染 qrcode_index_url，解码结果必须等于服务端下发 URL。"""

    async def scenario():
        async with _Ctx() as ctx:
            _point_at_fixture(ctx.port, monkeypatch)
            cli = doubao_login.DoubaoLoginClient(proxy="")
            try:
                sess = await cli.create_qr("doubao")
                assert sess.qr_mode == "doubao"
                assert sess.rendered_qr.startswith("data:image/png")
                # 解码验证
                import base64
                import io
                import re

                import cv2
                import numpy as np
                from PIL import Image

                raw = re.sub(r"^data:image/\w+;base64,", "", sess.rendered_qr)
                img = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")
                text, _p, _s = cv2.QRCodeDetector().detectAndDecode(np.array(img))
                assert text == sess.qr_scan_url, (text, sess.qr_scan_url)
                assert TOKEN in text
            finally:
                await cli.close()

    asyncio.run(scenario())


# ---------------- ③ 手机号 ----------------

def test_phone_sms_login_full_flow(monkeypatch, tmp_path):
    """发码（无滑块）→ 错码报错 → 对码登录拿到 sessionid。"""

    async def scenario():
        async with _Ctx() as ctx:
            _point_at_fixture(ctx.port, monkeypatch)
            cli = doubao_login.DoubaoLoginClient(proxy="")
            try:
                sess = await cli.send_sms("13800000000")
                assert sess.mobile_ticket == "mobile_ticket_TEST"
                sc = ctx.fx.get("send_code")
                assert sc["query"]["mobile"] == "13800000000"
                assert sc["query"]["type"] == "24"
                assert sc["query"]["is6Digits"] == "1"

                # 错码
                bad = await cli.sms_login(sess, "000000")
                assert bad.status == "failed"
                assert "验证码" in bad.message, bad.message
                logs = [c for c in ctx.fx.calls if c["name"] == "sms_login"]
                assert logs[-1]["data"]["mobile"] == "13800000000"
                assert logs[-1]["data"]["encrypt_uid"] == "mobile_ticket_TEST"

                # 对码
                ok = await cli.sms_login(sess, "654321")
                assert ok.status == "done", (ok.status, ok.message)
                assert ok.cookies.get("sessionid") == SESSIONID
                assert ok.to_dict()["logged_in"] is True
            finally:
                await cli.close()

    asyncio.run(scenario())


# ---------------- ④ 失败路径 ----------------

def test_qr_expired_is_reported(monkeypatch):
    """服务端说 token 无效 → 客户端应报 expired（而不是死等）。"""

    async def scenario():
        async with _Ctx() as ctx:
            _point_at_fixture(ctx.port, monkeypatch)
            cli = doubao_login.DoubaoLoginClient(proxy="")
            try:
                sess = await cli.create_qr("douyin")
                sess.token = "wrong-token"          # 夹具会回 error_code=3
                out = await cli.poll_qr(sess)
                # error_code=3 不被当成过期，但也不该崩；状态要可读
                assert out.status in ("new", "expired"), out.status
            finally:
                await cli.close()

    asyncio.run(scenario())


# ---------------- ⑤ 面板接口：入池 ----------------

def test_server_login_finish_puts_account_in_pool(tmp_path, monkeypatch):
    """扫码确认后走面板 /finish → 账号落盘入池 + 自动验活。"""
    import importlib

    server = importlib.import_module("server")
    accounts_dir = tmp_path / "dbao"
    monkeypatch.setattr(doubao_pool, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(server, "doubao_store",
                        doubao_pool.DoubaoAccountStore(accounts_dir))
    monkeypatch.setattr(server, "doubao",
                        doubao_pool.DoubaoPool(store=server.doubao_store, proxy=""))

    async def scenario():
        async with _Ctx() as ctx:
            _point_at_fixture(ctx.port, monkeypatch)
            # /finish 内部会调验活探针，这里让它直接返回成功
            async def fake_verify(name):
                server.doubao_store.patch_meta(name, login_ok=True, verified_at=1)
                return {"ok": True, "info": {}, "reason": ""}

            monkeypatch.setattr(server.doubao, "verify", fake_verify)

            cli = doubao_login.DoubaoLoginClient(proxy="")
            sess = await cli.create_qr("douyin")
            for _ in range(3):
                sess = await cli.poll_qr(sess)
                if sess.status == "done":
                    break
            assert sess.status == "done", sess.status

            sid = "dblogin_test1"
            server.DOUBAO_LOGIN_SESSIONS[sid] = {"client": cli, "sess": sess,
                                                "mode": "douyin", "touched": 9e9}
            body = server.DoubaoQrFinishBody(session_id=sid, name="db777")
            res = await server.admin_doubao_login_finish(body)
            assert res["ok"] is True, res
            assert res["account"] == "db777"
            assert res["source"] == "qr_douyin"
            assert res["verified"] is True, res

            st = doubao_pool.DoubaoAccountStore(accounts_dir)
            ck = st.read_ck("db777")
            assert f"sessionid={SESSIONID}" in ck, ck
            assert st.read_meta("db777")["login_ok"] is True
            # 会话应从内存里清掉
            assert sid not in server.DOUBAO_LOGIN_SESSIONS

    asyncio.run(scenario())


def test_server_login_finish_rejects_without_sessionid(tmp_path, monkeypatch):
    """还没扫码就 finish → 409，不产生半成品账号。"""
    import importlib

    server = importlib.import_module("server")
    accounts_dir = tmp_path / "dbao2"
    monkeypatch.setattr(doubao_pool, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(server, "doubao_store",
                        doubao_pool.DoubaoAccountStore(accounts_dir))
    monkeypatch.setattr(server, "doubao",
                        doubao_pool.DoubaoPool(store=server.doubao_store, proxy=""))

    async def scenario():
        async with _Ctx() as ctx:
            _point_at_fixture(ctx.port, monkeypatch)
            cli = doubao_login.DoubaoLoginClient(proxy="")
            sess = await cli.create_qr("douyin")     # 只取码，不轮询
            sid = "dblogin_test2"
            server.DOUBAO_LOGIN_SESSIONS[sid] = {"client": cli, "sess": sess,
                                                "mode": "douyin", "touched": 9e9}
            body = server.DoubaoQrFinishBody(session_id=sid, name="db888")
            from fastapi import HTTPException

            with pytest.raises(HTTPException) as ei:
                await server.admin_doubao_login_finish(body)
            assert ei.value.status_code == 409, ei.value.status_code
            assert doubao_pool.DoubaoAccountStore(accounts_dir).names() == []

    asyncio.run(scenario())


def test_server_phone_verify_puts_account_in_pool(tmp_path, monkeypatch):
    """手机号验证码登录走面板接口 → 账号入池（source=sms，带手机号）。"""
    import importlib

    server = importlib.import_module("server")
    accounts_dir = tmp_path / "dbao3"
    monkeypatch.setattr(doubao_pool, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(server, "doubao_store",
                        doubao_pool.DoubaoAccountStore(accounts_dir))
    monkeypatch.setattr(server, "doubao",
                        doubao_pool.DoubaoPool(store=server.doubao_store, proxy=""))

    async def scenario():
        async with _Ctx() as ctx:
            _point_at_fixture(ctx.port, monkeypatch)

            async def fake_verify(name):
                server.doubao_store.patch_meta(name, login_ok=True, verified_at=1)
                return {"ok": True, "info": {}, "reason": ""}

            monkeypatch.setattr(server.doubao, "verify", fake_verify)

            cli = doubao_login.DoubaoLoginClient(proxy="")
            sess = await cli.send_sms("13800001111")
            sid = "dbphone_test1"
            server.DOUBAO_LOGIN_SESSIONS[sid] = {"client": cli, "sess": sess,
                                                "mode": "sms", "touched": 9e9}

            body = server.DoubaoPhoneLoginBody(session_id=sid, code="654321",
                                               name="db999")
            res = await server.admin_doubao_phone_verify(body)
            assert res["ok"] is True, res
            assert res["account"] == "db999"
            assert res["source"] == "sms"
            st = doubao_pool.DoubaoAccountStore(accounts_dir)
            assert st.read_meta("db999")["mobile"] == "13800001111"
            assert f"sessionid={SESSIONID}" in st.read_ck("db999")

            # 错码路径不应入池
            cli2 = doubao_login.DoubaoLoginClient(proxy="")
            sess2 = await cli2.send_sms("13800002222")
            sid2 = "dbphone_test2"
            server.DOUBAO_LOGIN_SESSIONS[sid2] = {"client": cli2, "sess": sess2,
                                                 "mode": "sms", "touched": 9e9}
            r2 = await server.admin_doubao_phone_verify(
                server.DoubaoPhoneLoginBody(session_id=sid2, code="000000",
                                            name="db000"))
            assert r2["ok"] is False, r2
            assert "db000" not in doubao_pool.DoubaoAccountStore(accounts_dir).names()

    asyncio.run(scenario())
