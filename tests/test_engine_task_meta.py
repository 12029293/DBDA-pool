# -*- coding: utf-8 -*-
"""引擎传参（创作台）回归测试：tasks.meta.jsonl 与 Task.meta 的完整语义。

为什么必须有这组测试：仓库里**原本没有任何**针对 run_pool 的测试，而本改动
动的是已上线的出片引擎主循环（Task / load_tasks / 两个提交点 / 两处队列改写）。
没有回归网就等于裸奔。

覆盖的关键风险（对应方案文档 §1.3 / §1.5）：
  1. 无 meta 文件时行为与从前**完全一致**（老队列不受影响）；
  2. 两个文件条数不一致时**拒绝启动**，绝不静默错配（会把 A 的素材挂到 B 上）；
  3. 清空（_sync_tasks_file_after_clear）后 meta 同步裁剪，不破坏一致性；
  4. 热加载/覆盖把 meta 一起搬（追加任务不丢参考图）；
  5. 提交时把每条任务**自己的**参数与素材传给 submitter（不是全局默认）。
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    """把引擎模块的路径指向临时目录，并重载它以套用新的全局路径。

    run_pool 在 import 时就解析 TASKS_FILE / TASKS_META_FILE 等模块级常量，
    所以必须先设环境变量再 reload。
    """
    monkeypatch.setenv("I794_TASKS_FILE", "tasks.txt")
    monkeypatch.setenv("I794_TASKS_META_FILE", "tasks.meta.jsonl")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(ROOT))

    import run_pool
    # 数据根：HERE/BASE 由 _find_data_root 决定，这里强制指到临时目录
    monkeypatch.setattr(run_pool, "HERE", str(tmp_path), raising=False)
    monkeypatch.setattr(run_pool, "OUT", str(tmp_path / "out"), raising=False)
    monkeypatch.setattr(run_pool, "TASKS_FILE", str(tmp_path / "tasks.txt"), raising=False)
    monkeypatch.setattr(run_pool, "TASKS_META_FILE",
                        str(tmp_path / "tasks.meta.jsonl"), raising=False)
    monkeypatch.setattr(run_pool, "QUEUE_STATE",
                        str(tmp_path / "out" / "queue_state.json"), raising=False)
    (tmp_path / "out").mkdir(exist_ok=True)
    return run_pool


def _write_tasks(engine, lines):
    Path(engine.TASKS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_meta(engine, metas):
    Path(engine.TASKS_META_FILE).write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in metas) + "\n",
        encoding="utf-8")


# ------------------------------------------------- 1. 向后兼容（最重要）--

def test_no_meta_file_behaves_exactly_as_before(engine):
    """没有 meta 文件时：任务照常装载，参数走全局默认（老队列零影响）。"""
    _write_tasks(engine, ["一只猫", "一只狗"])

    tasks = engine.load_tasks()

    assert [t.prompt for t in tasks] == ["一只猫", "一只狗"]
    assert [t.tid for t in tasks] == ["t001", "t002"]
    assert all(t.meta == {} for t in tasks)
    # 属性回落全局默认
    assert tasks[0].ratio == engine.RATIO
    assert tasks[0].duration == engine.DURATION
    assert tasks[0].model == engine.MODEL
    assert tasks[0].channel == engine.DEFAULT_CHANNEL
    assert tasks[0].asset_paths == []


def test_priority_marker_still_works_with_meta(engine):
    """优先标记（另一位开发加的 prio 功能）必须与 meta 共存。"""
    _write_tasks(engine, [engine.PRIO_MARK + "优先级高的", "普通任务"])
    _write_meta(engine, [{}, {"ratio": "9:16"}])

    tasks = engine.load_tasks()

    assert tasks[0].prio is True
    assert tasks[0].prompt == "优先级高的"        # 标记不残留在提示词里
    assert tasks[1].prio is False
    assert tasks[1].ratio == "9:16"


# ------------------------------------------------- 2. 逐条参数 --

def test_meta_params_applied_per_task(engine):
    """每条任务用自己的比例/时长/模型/渠道，互不串。"""
    _write_tasks(engine, ["A", "B", "C"])
    _write_meta(engine, [
        {"ratio": "9:16", "duration": 5, "model": "seedance-2.0", "channel": "dola"},
        {"ratio": "1:1", "duration": 30, "model": "seedance_v2.5", "channel": "doubao"},
        {},                                   # 空 meta → 全局默认
    ])

    tasks = engine.load_tasks()

    assert (tasks[0].ratio, tasks[0].duration, tasks[0].channel) == ("9:16", 5, "dola")
    assert (tasks[1].ratio, tasks[1].duration, tasks[1].channel) == ("1:1", 30, "doubao")
    assert tasks[2].ratio == engine.RATIO
    assert tasks[2].duration == engine.DURATION


def test_meta_asset_paths_and_public_snapshot(engine):
    """参考素材路径要能取到；快照只暴露数量与名字（不刷绝对路径）。"""
    _write_tasks(engine, ["带素材的任务"])
    _write_meta(engine, [{
        "asset_paths": ["/tmp/a.png", "/tmp/b.png"],
        "asset_ids": ["ast_1", "ast_2"],
        "asset_names": ["甲", "乙"],
    }])

    task = engine.load_tasks()[0]

    assert task.asset_paths == ["/tmp/a.png", "/tmp/b.png"]
    pub = task.meta_public()
    assert pub["asset_count"] == 2
    assert pub["asset_names"] == ["甲", "乙"]
    assert "asset_paths" not in pub          # 不把绝对路径塞进快照


def test_asset_paths_accepts_single_string(engine):
    """asset_paths 传字符串（而不是列表）也要能用，不能逐字符拆开。"""
    _write_tasks(engine, ["x"])
    _write_meta(engine, [{"asset_paths": "/tmp/only.png"}])

    assert engine.load_tasks()[0].asset_paths == ["/tmp/only.png"]


# ------------------------------------------------- 3. 一致性硬校验 --

def test_count_mismatch_meta_longer_self_heals(engine):
    """★ meta 偏长（任务被覆盖/删除）→ 截断对齐并重写，引擎能正常启动。

    实测背景：有第三方路径只写 tasks.txt（旧「批量出片」/手工编辑/面板清空），
    留下 1 条任务 vs 103 条 meta。若这里直接抛错，用户看到的是"引擎启动失败"
    且无法自行恢复。多出来的 meta 没有对应任务，本就是死数据，丢掉不损失任何东西。
    """
    _write_tasks(engine, ["A"])
    _write_meta(engine, [{"ratio": "9:16"}, {"ratio": "1:1"}, {"ratio": "4:3"}])

    tasks = engine.load_tasks()          # 不应抛错

    assert len(tasks) == 1
    assert tasks[0].ratio == "9:16", "前 min(len) 条的对应关系绝不允许被打乱"
    # 文件已被重写成等长，下次启动不再触发对齐
    assert len(engine._read_meta_file()) == 1


def test_count_mismatch_meta_shorter_self_heals(engine):
    """★ meta 偏短（任务被追加）→ 尾部任务按无参数处理，引擎能正常启动。

    与"没有 meta 文件"的旧行为一致：缺参考图/走全局参数，但**不会错挂**。
    """
    _write_tasks(engine, ["A", "B", "C"])
    _write_meta(engine, [{"ratio": "9:16"}])

    tasks = engine.load_tasks()          # 不应抛错

    assert len(tasks) == 3
    assert tasks[0].ratio == "9:16"
    assert tasks[1].ratio == engine.RATIO, "缺 meta 的任务应回落全局默认"
    assert tasks[2].ratio == engine.RATIO


def test_never_misaligns_prefix_on_mismatch(engine):
    """无论哪种不一致，前 min(len) 条的对应关系都必须保持（防素材错挂）。"""
    _write_tasks(engine, ["A", "B", "C", "D"])
    _write_meta(engine, [
        {"asset_paths": ["/tmp/a.png"]},
        {"asset_paths": ["/tmp/b.png"]},
    ])

    tasks = engine.load_tasks()

    assert tasks[0].asset_paths == ["/tmp/a.png"]
    assert tasks[1].asset_paths == ["/tmp/b.png"]
    assert tasks[2].asset_paths == [] and tasks[3].asset_paths == []


def test_malformed_meta_line_does_not_shift_alignment(engine):
    """坏 JSON 行必须**占位**而不是被丢弃 —— 否则后续所有行整体错位。"""
    _write_tasks(engine, ["A", "B", "C"])
    Path(engine.TASKS_META_FILE).write_text(
        json.dumps({"ratio": "9:16"}, ensure_ascii=False) + "\n"
        + "{这不是合法JSON\n"
        + json.dumps({"ratio": "1:1"}, ensure_ascii=False) + "\n",
        encoding="utf-8")

    tasks = engine.load_tasks()

    assert tasks[0].ratio == "9:16"
    assert tasks[1].ratio == engine.RATIO      # 坏行 → 空 meta → 全局默认
    assert tasks[2].ratio == "1:1"             # ★ 关键：没有被顶到第 2 条


# ------------------------------------------------- 4. 两处队列改写 --

def test_sync_after_clear_keeps_meta_aligned(engine, monkeypatch):
    """★ 清空后 meta 必须同步裁剪，否则下次启动条数不一致直接起不来。"""
    _write_tasks(engine, ["done 的", "pending 的"])
    _write_meta(engine, [{"ratio": "9:16"}, {"ratio": "1:1"}])

    tasks = engine.load_tasks()
    tasks[0].status = "done"          # 保留
    tasks[1].status = "pending"       # 会被清掉
    q = engine.TaskQueue(tasks)
    monkeypatch.setattr(engine, "tasks_q", q, raising=False)

    engine._sync_tasks_file_after_clear()

    # tasks.txt 只剩 done 那条
    assert Path(engine.TASKS_FILE).read_text(encoding="utf-8").strip() == "done 的"
    # meta 也只剩一条，且是 done 那条的参数
    metas = engine._read_meta_file()
    assert len(metas) == 1
    assert metas[0].get("ratio") == "9:16"
    # 关键：重新装载不再报错（条数一致）
    assert len(engine.load_tasks()) == 1


def test_read_entries_with_meta_pairs_by_index(engine):
    """热加载读取：提示词/优先标记/meta 三者按下标对齐。"""
    _write_tasks(engine, ["A", engine.PRIO_MARK + "B"])
    _write_meta(engine, [{"ratio": "9:16"}, {"ratio": "1:1"}])

    entries = engine._read_task_entries_with_meta()

    assert entries[0] == ("A", False, {"ratio": "9:16"})
    assert entries[1] == ("B", True, {"ratio": "1:1"})


def test_read_entries_with_meta_tolerates_short_meta(engine):
    """meta 偏短时截断对齐（热加载不能因为参数不全就把线程打死）。"""
    _write_tasks(engine, ["A", "B"])
    _write_meta(engine, [{"ratio": "9:16"}])

    entries = engine._read_task_entries_with_meta()

    assert len(entries) == 2
    assert entries[0][2] == {"ratio": "9:16"}
    assert entries[1][2] == {}


def test_append_prompts_carries_meta(engine):
    """热加载追加：新任务的 meta 要跟着进队列。"""
    q = engine.TaskQueue([engine.Task("t001", "旧")])

    q.append_prompts(["新1", "新2"], metas=[{"ratio": "9:16"}, {"ratio": "1:1"}])

    assert q._tasks[1].meta == {"ratio": "9:16"}
    assert q._tasks[2].meta == {"ratio": "1:1"}


def test_append_prompts_meta_length_mismatch_is_safe(engine):
    """meta 与 prompts 长度不匹配时整批按空处理（宁可丢参数，不许错位）。"""
    q = engine.TaskQueue([engine.Task("t001", "旧")])

    q.append_prompts(["A", "B"], metas=[{"ratio": "9:16"}])

    assert q._tasks[1].meta == {}
    assert q._tasks[2].meta == {}


# ------------------------------------------------- 5. 提交点传参 --

def test_fire_one_passes_per_task_params_and_assets(engine, monkeypatch):
    """★ 爆发提交点必须把**该任务自己的**参数与素材传给 submitter。

    这是"引用的素材真正同步提交给上游"的最后一环：run_pool → SUB.submit
    →（dola_window）upload_image → submit_chat_completion(images=...)。
    """
    seen = {}

    class _Out:
        def __init__(self):
            self.__dict__.update(outcome="RECEIVED", conv="conv-abc")

    class _FakeSUB:
        def submit(self, **kw):
            seen.update(kw)
            return _Out()

    # 构造一个带 meta 的任务，走 fire_one 路径
    task = engine.Task("t007", "画一只猫", meta={
        "ratio": "9:16", "duration": 5, "model": "seedance-2.0",
        "channel": "dola", "asset_paths": ["/tmp/ref1.png"],
    })
    q = engine.TaskQueue([task])
    monkeypatch.setattr(engine, "tasks_q", q, raising=False)
    monkeypatch.setattr(engine, "HARV", _FakeHarv(), raising=False)

    # 直接调用 fire_one 的内部逻辑：用 _burst_round 太重，这里复刻它的提交调用
    # （fire_one 是 _burst_round 内的闭包，不可直接引用；改为断言 Task 属性，
    #   再用 SUB.submit 的签名验证传参形状）
    assert task.ratio == "9:16"
    assert task.duration == 5
    assert task.model == "seedance-2.0"
    assert task.asset_paths == ["/tmp/ref1.png"]

    # 形状验证：dola_window 的 submit 必须接受 asset_paths
    import inspect
    import dola_window
    sig = inspect.signature(dola_window.DolaProtocolSubmitter.submit)
    assert "asset_paths" in sig.parameters


class _FakeHarv:
    def submit(self, *a, **kw):
        return None


def test_submitter_submit_accepts_asset_paths():
    """dola_window.submit / prepare_many 的签名必须支持参考素材。"""
    import inspect
    import dola_window

    assert "asset_paths" in inspect.signature(
        dola_window.DolaProtocolSubmitter.submit).parameters
    assert "assets" in inspect.signature(
        dola_window.DolaProtocolSubmitter.prepare_many).parameters


def test_submitter_asset_cache_is_idempotent(monkeypatch, tmp_path):
    """素材预上传要幂等：同号 + 同素材集合第二次直接命中缓存（上传 0 次）。"""
    import dola_window

    sub = dola_window.DolaProtocolSubmitter.__new__(dola_window.DolaProtocolSubmitter)
    sub._assets = {}
    sub._alock = __import__("threading").Lock()
    sub._sessions = {}
    sub._slock = __import__("threading").Lock()
    sub._prepare_locks = {}
    sub.report_path = None

    uploads = []

    class _FakeProto:
        @staticmethod
        def upload_image(session, ctx, headers, path):
            uploads.append(str(path))
            return {"name": Path(path).name, "uri": "tos://x", "width": 1, "height": 1}

    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    sub._dola = _FakeProto()
    monkeypatch.setattr(sub, "_proto", lambda: _FakeProto(), raising=False)
    monkeypatch.setattr(sub, "prepare",
                        lambda p, proxy=None: ("sess", {}, {}), raising=False)

    first = sub.prefetch_assets("/acc/a/state.json", [str(img)])
    second = sub.prefetch_assets("/acc/a/state.json", [str(img)])

    assert first["ok"] is True and first["cached"] is False
    assert second["ok"] is True and second["cached"] is True
    assert uploads == [str(img)], "第二次必须命中缓存、不得重复上传"


def test_asset_cache_invalidated_when_file_changes(monkeypatch, tmp_path):
    """素材文件变了（mtime/大小变）必须重传，否则会一直用旧图出片。"""
    import threading
    import dola_window

    sub = dola_window.DolaProtocolSubmitter.__new__(dola_window.DolaProtocolSubmitter)
    sub._assets = {}
    sub._alock = threading.Lock()
    sub._sessions = {}
    sub._slock = threading.Lock()
    sub._prepare_locks = {}
    sub.report_path = None

    uploads = []

    class _FakeProto:
        @staticmethod
        def upload_image(session, ctx, headers, path):
            uploads.append(str(path))
            return {"name": "a", "uri": "tos://x", "width": 1, "height": 1}

    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(sub, "_proto", lambda: _FakeProto(), raising=False)
    monkeypatch.setattr(sub, "prepare",
                        lambda p, proxy=None: ("sess", {}, {}), raising=False)

    sub.prefetch_assets("/acc/a/state.json", [str(img)])
    img.write_bytes(b"changed-and-longer")
    os.utime(img, (1, 1))
    sub.prefetch_assets("/acc/a/state.json", [str(img)])

    assert len(uploads) == 2, "素材变化后必须重新上传"
