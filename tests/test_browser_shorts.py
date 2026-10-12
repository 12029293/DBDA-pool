"""DOLA_BROWSER_SHORTS：4~29s（含 15s）短时长路由到浏览器通道（2 点计价）。

路由矩阵（_generate_effective）：
  - 30s：DOLA_30S_CHANNEL=browser → 浏览器；=pure → 纯 API
  - 4~29s：DOLA_BROWSER_SHORTS=1 → 浏览器；=0 → 纯 API（会被上游报价拒单，仅回归用）
  - 非 cookie 号 / 非 2.5 模型不进浏览器通道（保持既有行为）
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config


@pytest.fixture(autouse=True)
def _stubs():
    """与 test_model_routing 同一套桩，避免依赖 patchright 与真实上游。"""
    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError",
                "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))

    async def _gen(*_a, **_k):
        raise RuntimeError("stub generate_video should not be called")

    v.generate_video = _gen
    v.resume_video = _gen
    sys.modules["video_worker_ui"] = v

    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d
    yield


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def bp(tmp_path, monkeypatch):
    import browser_pool

    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 4)
    monkeypatch.setattr(browser_pool, "DAILY_LIMIT", 4)
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(exist_ok=True)
    (accounts_dir / "acc1").mkdir()
    pool = browser_pool.BrowserPool(accounts_dir=str(accounts_dir),
                                    db_path=str(tmp_path / "pool.db"))
    pool.set_login_status("acc1", True)
    # cookie 号 + cookie 状态文件
    state_file = tmp_path / "cookie_state.json"
    state_file.write_text(json.dumps({"cookies_list": []}), encoding="utf-8")
    monkeypatch.setattr(pool, "_pure_state", lambda name: str(state_file))
    monkeypatch.setattr(pool, "_is_cookie_source", lambda name: True)
    # 代理与转码桩（_proxy_url_for 带 rotate/ensure kwargs，一并吃掉）
    monkeypatch.setattr(browser_pool.proxy_store, "proxy_url_for",
                        lambda name, **_k: "socks5://127.0.0.1:1")
    monkeypatch.setattr(pool, "_proxy_url_for",
                        lambda name, **_k: "socks5://127.0.0.1:1")
    monkeypatch.setattr(pool, "_web_playable",
                        lambda result: _async_identity(result))
    return browser_pool, pool


async def _async_identity(x):
    return x


def _patch_channels(monkeypatch, *, channel_30s, shorts):
    monkeypatch.setattr(config, "CHANNEL_30S", channel_30s)
    monkeypatch.setattr(config, "USE_BROWSER_30S", channel_30s != "pure")
    monkeypatch.setattr(config, "BROWSER_SHORTS", shorts)


def _fake_browser30(calls):
    async def fake(account, prompt, *, model="", cookie_state_path="",
                   proxy_url="", on_poll=None, max_wait=0,
                   target_duration=30, reference_image_paths=None, **_k):
        calls.append(("browser30", target_duration))
        return {"account": account, "local_path": "x.mp4"}

    return fake


def _fake_pure(calls):
    def fake(**kwargs):
        calls.append(("pure", kwargs.get("duration")))
        return {"account": kwargs.get("account"), "local_path": "x.mp4",
                "conversation_id": "conv"}

    return fake


def test_short_15s_routes_to_browser_when_shorts_on(tmp_path, monkeypatch, bp):
    browser_pool, pool = bp
    _patch_channels(monkeypatch, channel_30s="pure", shorts=True)
    calls = []
    monkeypatch.setattr(browser_pool, "generate_video_30", _fake_browser30(calls))
    _run(pool._generate_effective("acc1", "p", "9:16", 15, "seedance-2.5",
                                  on_conversation_id=None, on_poll=None,
                                  on_balance=None, reference_image_paths=[]))
    assert calls == [("browser30", 15)]


def test_short_15s_stays_pure_when_shorts_off(tmp_path, monkeypatch, bp):
    browser_pool, pool = bp
    _patch_channels(monkeypatch, channel_30s="pure", shorts=False)
    calls = []
    monkeypatch.setattr(browser_pool, "generate_pure_video", _fake_pure(calls))
    _run(pool._generate_effective("acc1", "p", "9:16", 15, "seedance-2.5",
                                  on_conversation_id=None, on_poll=None,
                                  on_balance=None, reference_image_paths=[]))
    assert calls == [("pure", 15)]


def test_30s_pure_channel_untouched_by_shorts_flag(tmp_path, monkeypatch, bp):
    """30s 跟随 DOLA_30S_CHANNEL=pure 走纯 API，BROWSER_SHORTS=1 不影响它。"""
    browser_pool, pool = bp
    _patch_channels(monkeypatch, channel_30s="pure", shorts=True)
    calls = []
    monkeypatch.setattr(browser_pool, "generate_pure_video", _fake_pure(calls))
    _run(pool._generate_effective("acc1", "p", "9:16", 30, "seedance-2.5",
                                  on_conversation_id=None, on_poll=None,
                                  on_balance=None, reference_image_paths=[]))
    assert calls == [("pure", 30)]


def test_30s_browser_when_channel_browser(tmp_path, monkeypatch, bp):
    browser_pool, pool = bp
    _patch_channels(monkeypatch, channel_30s="browser", shorts=False)
    calls = []
    monkeypatch.setattr(browser_pool, "generate_video_30", _fake_browser30(calls))
    _run(pool._generate_effective("acc1", "p", "9:16", 30, "seedance-2.5",
                                  on_conversation_id=None, on_poll=None,
                                  on_balance=None, reference_image_paths=[]))
    assert calls == [("browser30", 30)]


def test_channel_browser_sends_all_4_30s_to_browser(tmp_path, monkeypatch, bp):
    browser_pool, pool = bp
    _patch_channels(monkeypatch, channel_30s="browser", shorts=True)
    calls = []
    monkeypatch.setattr(browser_pool, "generate_video_30", _fake_browser30(calls))
    for dur in (4, 10, 15, 29, 30):
        _run(pool._generate_effective("acc1", "p", "9:16", dur, "seedance-2.5",
                                      on_conversation_id=None, on_poll=None,
                                      on_balance=None,
                                      reference_image_paths=[]))
    assert calls == [("browser30", d) for d in (4, 10, 15, 29, 30)]


def test_non_25_model_never_browser(tmp_path, monkeypatch, bp):
    browser_pool, pool = bp
    _patch_channels(monkeypatch, channel_30s="browser", shorts=True)
    calls = []
    monkeypatch.setattr(browser_pool, "generate_pure_video", _fake_pure(calls))
    _run(pool._generate_effective("acc1", "p", "9:16", 15, "seedance-2.0",
                                  on_conversation_id=None, on_poll=None,
                                  on_balance=None, reference_image_paths=[]))
    assert calls == [("pure", 15)]
