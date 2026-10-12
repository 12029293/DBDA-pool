# -*- coding: utf-8 -*-
"""渠道感知的重启恢复 / 看门狗重派回归。

背景（本轮发现）：`doubao` 任务与 `dola` 任务共用 tasks 表，但恢复入口原先
无差别地按 dola 处理 —— 重启后豆包任务会被丢进 `_run_task`（dola 号池）：
账号名对不上、额度记进错的池子、上游还打错域。看门狗重派同理。

这里断言 `_redispatch_task` 是唯一分流点，且豆包任务绝不进 `_resume_task`。
"""
from __future__ import annotations

import asyncio
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


def _row(**kw) -> dict:
    base = {
        "id": "test_x", "model": "seedance-2.0", "prompt": "p", "ratio": "16:9",
        "duration": 5, "reference_images": "[]", "account": "db001",
        "channel": "doubao", "api_key_hash": "h", "api_key_name": "t",
        "client_concurrency_limit": 0, "conversation_id": None,
    }
    base.update(kw)
    return base


def test_row_channel_defaults_to_dola():
    import server

    assert server._row_channel({"channel": "doubao"}) == "doubao"
    assert server._row_channel({"channel": "DOUBAO"}) == "doubao"
    assert server._row_channel({"channel": "dola"}) == "dola"
    # 老库没有 channel 列 → NULL / 缺键 / None 都算 dola
    assert server._row_channel({"channel": None}) == "dola"
    assert server._row_channel({}) == "dola"
    assert server._row_channel(None) == "dola"


def test_redispatch_routes_doubao_to_doubao_runner(monkeypatch):
    """豆包任务必须走 _run_doubao_test_generate，绝不走 _run_task。"""
    import server

    called: dict = {}

    async def fake_doubao(task_id, name, prompt, ratio, duration, model, refs):
        called.update(kind="doubao", task_id=task_id, name=name, duration=duration,
                      refs=refs)

    async def fake_dola(*a, **k):
        called.update(kind="dola")

    monkeypatch.setattr(server, "_run_doubao_test_generate", fake_doubao)
    monkeypatch.setattr(server, "_run_task", fake_dola)

    async def scenario():
        await server._redispatch_task(_row(
            id="test_db", account="db007", duration=10, model="seedance-2.5",
            reference_images='["/tmp/a.png"]'))
        await asyncio.sleep(0)   # 让 create_task 跑起来
        await asyncio.sleep(0)
        return dict(called)

    got = asyncio.new_event_loop().run_until_complete(scenario())
    assert got.get("kind") == "doubao", got
    assert got["task_id"] == "test_db"
    assert got["name"] == "db007"
    assert got["duration"] == 10
    assert got["refs"] == ["/tmp/a.png"], got["refs"]


def test_redispatch_routes_dola_to_dola_runner(monkeypatch):
    import server

    called: dict = {}

    async def fake_doubao(*a, **k):
        called.update(kind="doubao")

    async def fake_dola(task_id, model, prompt, ratio, duration, refs, client):
        called.update(kind="dola", task_id=task_id, ratio=ratio, duration=duration)

    monkeypatch.setattr(server, "_run_doubao_test_generate", fake_doubao)
    monkeypatch.setattr(server, "_run_task", fake_dola)

    async def scenario():
        # ratio="default" 要还原成 None（与历史行为一致）
        await server._redispatch_task(_row(id="test_dola", channel="dola",
                                          ratio="default", duration=30,
                                          model="seedance-2.5"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return dict(called)

    got = asyncio.new_event_loop().run_until_complete(scenario())
    assert got.get("kind") == "dola", got
    assert got["task_id"] == "test_dola"
    assert got["ratio"] is None
    assert got["duration"] == 30


def test_recoverable_queries_include_doubao_rows(tmp_path, monkeypatch):
    """store 的恢复查询不能漏掉豆包行（否则重启后豆包任务永久挂着）。"""
    import config

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    import store as store_mod

    st = store_mod.TaskStore(str(tmp_path / "t.db"))
    st.create("video_d", "seedance-2.0", "p", "16:9", 5, channel="dola")
    st.create("video_b", "seedance-2.0", "p", "16:9", 5, account="db001",
              channel="doubao")
    queued = {r["id"] for r in st.recoverable_queued_tasks()}
    assert "video_b" in queued, queued
    assert "video_d" in queued, queued
    # 渠道标记必须能读回来（分流靠它）
    assert st.get("video_b")["channel"] == "doubao"
    assert st.get("video_d")["channel"] == "dola"
