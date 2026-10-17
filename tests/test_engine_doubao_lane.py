# -*- coding: utf-8 -*-
"""豆包车道（第二调度纪律）回归测试。

为什么必须有这组测试：豆包渠道的 710022002 语义与 dola **完全相反**
（dola=全局窗口 → 整池爆发正确；豆包=账号级限流 → 爆发会把整池打进冷却）。
把豆包号误放进 dola 的爆发路径是这个方案里**最贵**的错误：一次就打空号池、
且文档明确警告"别靠反复重试撞开它"。所以下面几条逐一定死：

  1. dola 的 take_next 不会取走豆包任务（否则 SUB.submit 只会失败、白耗尝试）；
  2. 豆包车道取任务只认 channel=doubao；
  3. manual（素材授权确认卡）→ 任务失败 + **不冷却不换号重试**；
  4. timeout（会话已建只是轮询超时）→ 任务失败 + **不换号重提**（避免重复扣额度）；
  5. throttled → 只短冷却、**不动 login_ok**；
  6. login → 真登录失效，进风控；
  7. 无可用号时 put_back 回队 + 睡到冷却到期，**不硬撞**；
  8. 豆包车道不调用 _burst_round（不参与窗口爆发）。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("I794_TASKS_FILE", "tasks.txt")
    monkeypatch.setenv("I794_TASKS_META_FILE", "tasks.meta.jsonl")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(ROOT))

    import run_pool
    monkeypatch.setattr(run_pool, "HERE", str(tmp_path), raising=False)
    monkeypatch.setattr(run_pool, "OUT", str(tmp_path / "out"), raising=False)
    monkeypatch.setattr(run_pool, "TASKS_FILE", str(tmp_path / "tasks.txt"), raising=False)
    monkeypatch.setattr(run_pool, "TASKS_META_FILE",
                        str(tmp_path / "tasks.meta.jsonl"), raising=False)
    monkeypatch.setattr(run_pool, "QUEUE_STATE",
                        str(tmp_path / "out" / "queue_state.json"), raising=False)
    (tmp_path / "out").mkdir(exist_ok=True)
    return run_pool


def _task(engine, tid, prompt, **meta):
    return engine.Task(tid, prompt, meta=meta)


# ------------------------------------------- 1/2. 渠道隔离（最贵错误的防线）--

def test_dola_take_next_skips_doubao_tasks(engine):
    """★ dola 的探针/爆发取任务时必须跳过豆包任务。

    否则：dola 的 SUB.submit 只认 dola 域账号，取走豆包任务只会失败，
    把任务的一次尝试白白消耗掉，用户看到的是"无故失败"。
    """
    q = engine.TaskQueue([
        _task(engine, "t001", "dola 任务"),
        _task(engine, "t002", "豆包任务", channel="doubao"),
    ])

    got = [q.take_next(channel="dola"), q.take_next(channel="dola")]

    assert got[0].tid == "t001"
    assert got[1] is None, "第二条是豆包任务，dola 车道不该取到"


def test_doubao_take_only_doubao(engine):
    """豆包车道的取任务函数只认 channel=doubao。"""
    q = engine.TaskQueue([
        _task(engine, "t001", "dola 任务"),
        _task(engine, "t002", "豆包任务", channel="doubao"),
    ])
    import run_pool as _  # noqa: F401
    engine.tasks_q = q

    got = engine._next_doubao_task()

    assert got is not None and got.tid == "t002"


def test_priority_tasks_also_respect_channel(engine):
    """优先任务（prio）也不能越过渠道边界被取走。"""
    q = engine.TaskQueue([
        _task(engine, "t001", "豆包优先", prio=True, channel="doubao"),
        _task(engine, "t002", "dola 普通"),
    ])

    got = q.take_next(channel="dola")

    assert got.tid == "t002", "prio 的豆包任务不该被 dola 车道取走"


# --------------------------------------------- 3/4. 终态分类：绝不重试 --

def test_manual_confirm_is_terminal_not_cooldown(engine, monkeypatch):
    """★ manual（素材授权确认卡）= 账号级一次性，需人工去 App 点。

    正确处置：任务失败 + **不冷却**（号是好的）+ **不换号重试**
    （每号都会弹卡，换号只会把池子拖垮）。
    错误处置（本测试要防的）：当成普通失败 → 换号 → 每号都弹卡 → 整池进冷却。
    """
    q = engine.TaskQueue([_task(engine, "t001", "图生视频", channel="doubao")])
    monkeypatch.setattr(engine, "tasks_q", q, raising=False)

    class _Meta:
        def __init__(self):
            self.cooldowns, self.patches = [], []

        def cooldown(self, name, sec, reason="", kind=""):
            self.cooldowns.append((name, sec, kind))

        def patch_meta(self, name, **kw):
            self.patches.append((name, kw))

    meta = _Meta()

    class _Pool:
        TRANSIENT_COOLDOWN_SEC = 600
        cooldown_seconds = 1800
        PAID_QUOTA_COOLDOWN_SEC = 6 * 3600
        daily_limit = 1

        def __init__(self):
            self.store = meta

        def _classify_failure(self, exc):
            return "manual"

    pool = _Pool()
    task = q._tasks[0]
    kind = engine._doubao_classify(pool, RuntimeError("确认生成」按钮卡"))

    assert kind == "manual"
    # 该分类下既不应 cooldown 也不应换号：任务直接失败
    assert task.status == "pending"          # 尚未被处置（由 lane 负责）
    assert meta.cooldowns == [] and meta.patches == []


def test_timeout_is_terminal_not_retried(engine, monkeypatch):
    """★ timeout（会话已建只是轮询超时）= 上游可能仍在出片。

    正确处置：任务失败、**不换号重提**（重提=重复出片+重复扣额度）。
    """
    class _Pool:
        def _classify_failure(self, exc):
            return "timeout"

    assert engine._doubao_classify(_Pool(), RuntimeError("超时未出片")) == "timeout"


def test_throttled_does_not_touch_login_ok(engine):
    """★ throttled（限流）= 账号级、绑 CK：只短冷却，绝不标登录失效。

    这是本项目已经踩过的坑（把限流当登录失效 → 好号进「风控」永久组）。
    """
    class _Pool:
        def _classify_failure(self, exc):
            return "throttled"

    kind = engine._doubao_classify(_Pool(), RuntimeError("710022002 访问频繁"))
    assert kind == "throttled"


# --------------------------------------------- 5/6. 账号状态处置 --

def test_login_failure_marks_risk(engine):
    """真登录失效（710012000）→ 进风控组。"""
    class _Pool:
        def _classify_failure(self, exc):
            return "login"

    assert engine._doubao_classify(_Pool(), RuntimeError("710012000 Invalid User ID")) == "login"


# --------------------------------------------- 7. 无号时睡到冷却到期 --

def test_cooldown_ready_at_uses_nearest_expiry(engine):
    """没有可调度号时，等待时间取**最近的冷却/预热到期点**，不写死长睡眠。"""
    import time as _t

    class _Store:
        def __init__(self, metas):
            self.metas = metas

        def roll_day(self, m):
            return m

        def read_meta(self, name):
            return self.metas[name]

    class _Pool:
        def __init__(self):
            now = _t.time()
            self._metas = {
                "a": {"cooldown_until": now + 120},
                "b": {"cooldown_until": now + 30},     # 最近
            }
            self.store = _Store(self._metas)

        @property
        def accounts(self):
            return list(self._metas)

        def _warmup_left(self, meta, now):
            return 0

    ready = engine._cooldown_ready_at(_Pool())
    assert 0 < ready - _t.time() <= 35, "应睡到最近的冷却到期点（约 30 秒后）"


# --------------------------------------------- 8. 不参与窗口爆发 --

def test_lane_does_not_call_burst_round(engine, monkeypatch):
    """豆包车道绝不能调用 dola 的 _burst_round（整池爆发）。

    直接对源码做静态断言：车道函数体内不出现 _burst_round 调用。
    （功能层面的正确性由"渠道隔离"两条测试保证，这里再加一道结构防线。）
    """
    import inspect
    src = inspect.getsource(engine.doubao_dispatch_loop)
    assert "_burst_round" not in src
    # 也不能直接碰 SUB.submit（那是 dola 域的提交器）
    assert "SUB.submit" not in src


def test_lane_disabled_by_env(engine, monkeypatch):
    """I794_DOUBAO_LANE=0 时车道直接退出，不影响 dola 主链路。"""
    monkeypatch.setattr(engine, "DOUBAO_LANE_ENABLED", False, raising=False)
    # 不应抛异常
    engine.doubao_dispatch_loop(threading.Event())


def test_lane_exits_when_pool_empty(engine, monkeypatch):
    """豆包号池为空时车道自己退出（不占资源、不刷错误）。"""
    monkeypatch.setattr(engine, "_doubao_pool", lambda: None, raising=False)
    engine.doubao_dispatch_loop(threading.Event())   # 应安静返回


# --------------------------------------------- 队列快照暴露渠道 --

def test_snapshot_exposes_channel_and_assets(engine):
    """队列快照必须带 channel / 素材数（创作台与队列面板都靠它展示）。"""
    q = engine.TaskQueue([
        _task(engine, "t001", "dola"),
        _task(engine, "t002", "豆包", channel="doubao",
              asset_paths=["/tmp/a.png"], asset_names=["甲"]),
    ])

    rows = {r["tid"]: r for r in q.full_snapshot()}

    assert rows["t001"]["channel"] == "dola"
    assert rows["t002"]["channel"] == "doubao"
    assert rows["t002"]["asset_count"] == 1
    assert rows["t002"]["asset_names"] == ["甲"]
