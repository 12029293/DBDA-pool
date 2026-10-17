# -*- coding: utf-8 -*-
"""端到端验证：创作台一个输入窗口 = 一条视频任务。

链路：studioSubmit() → POST /api/admin/studio/generate
      → poolengine_mgr.submit_tasks([{"prompt": <整段文本>, ...}])
      → tasks.txt 只多 **1 行**
      → 引擎 load_tasks() 读回 **1 条**任务，提示词是完整原文
      → 任务中心/队列面板看到的是用户原始输入

对照（修复前）：整段剧本被按行写进 tasks.txt → 每一行都是独立任务
→ 用户提交 1 条 30 秒片，任务列表多出十几条 5 秒碎片。
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STUDIO_INPUT = """0—4秒｜大远景｜宇宙战场
宇宙一片死寂。巨大黑色敌方母舰像钢铁大陆横亘星空，表面红色能量脉络缓慢亮起。
远处一颗蓝色行星悬在黑暗中，边缘被战火映出暗红。

4—9秒｜中景转特写｜人类舰队残骸区
人类舰队只剩断裂舰体和漂浮碎片。镜头穿过残骸，缓慢推近一艘小型人类战机。
驾驶舱里，飞行员呼吸急促，手指压下启动键。蓝色引擎在阴影中亮起。

9—15秒｜跟拍｜残骸与光束之间
战机冲出残骸，贴着巨大碎片高速飞行。敌方光束从后方扫过，爆炸在两侧接连绽放。
镜头紧跟机身，火花、尘埃、金属碎片从画面边缘掠过。没有多余台词，只有低频轰鸣和心跳声。

15—22秒｜快速推进｜敌方母舰核心通道
战机冲入母舰内部通道。红色能量脉络像血管一样亮起，警报光疯狂闪烁。
镜头快速向前推进，通道尽头出现炽白核心。飞行员低声说：“为了黎明。”战机开始最后加速。

22—26秒｜慢动作｜撞击瞬间
战机撞向核心。白光吞没整个画面，所有声音瞬间消失。碎片、火焰、能量环在慢动作中向外扩散。

26—30秒｜拉远｜爆炸后的星云
镜头缓慢拉远。爆炸形成一圈金色与白色的光环，母舰残骸漂浮在星云中。
一艘小小逃生舱从余波里弹出，舱窗透出微弱蓝光。远处蓝色行星迎来清晨般的光。
旁白或字幕：“当黑夜最大时，黎明最近。”"""


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    import pool_engine_service as s
    # ★ 必须把 QUEUE_STATE 也指到临时目录：否则 read_queue_state() 的新鲜度检查
    #   会命中项目里真实的 out/queue_state.json（引擎跑着时每 3 秒重写），
    #   读到的是真实队列而不是我们刚写的那 1 条。
    monkeypatch.setattr(s, "TASKS_FILE", tmp_path / "tasks.txt", raising=False)
    monkeypatch.setattr(s, "RUN", tmp_path, raising=False)
    monkeypatch.setattr(s, "QUEUE_STATE", tmp_path / "queue_state.json", raising=False)
    return s


def test_studio_one_input_window_is_one_task(svc, tmp_path):
    """★ 用户需求原文：创作台的单输入窗口 = 一条视频任务。"""
    mgr = svc.PoolEngineManager()          # __init__ 只设属性，不起进程
    mgr._proc = None

    # 创作台提交：整段文本作为**一个** item
    res = mgr.submit_tasks([{
        "prompt": STUDIO_INPUT,
        "meta": {"asset_paths": [], "asset_ids": [], "asset_names": [],
                 "ratio": "16:9", "duration": 5, "model": "seedance-2.0",
                 "channel": "dola", "kind": "video"},
    }])

    assert res["ok"] is True
    assert res["added"] == 1, "必须只入队 1 条"

    # 磁盘上只多 1 行
    text = (tmp_path / "tasks.txt").read_text(encoding="utf-8")
    assert text.count("\n") == 1
    assert "\n" not in text.rstrip("\n")

    # 读回来：1 条、完整原文、参数没错挂
    prompts, metas = mgr._read_tasks_with_meta()
    assert len(prompts) == 1
    assert prompts[0] == STUDIO_INPUT
    assert metas[0]["ratio"] == "16:9"
    assert metas[0]["duration"] == 5
    assert len(metas) == 1


def test_studio_task_count_matches_line_count(svc, tmp_path):
    """tasks.txt 行数 == 任务条数（面板「队列 N 条」与真实任务一致）。"""
    import run_pool
    mgr = svc.PoolEngineManager()
    mgr._proc = None
    mgr.submit_tasks([{"prompt": STUDIO_INPUT, "meta": {}}])

    n_lines = sum(1 for ln in (tmp_path / "tasks.txt").read_text(
        encoding="utf-8").splitlines() if ln.strip())
    assert n_lines == 1

    # 引擎侧装载：也是 1 条
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(run_pool, "TASKS_FILE", str(tmp_path / "tasks.txt"), raising=False)
        monkey.setattr(run_pool, "TASKS_META_FILE",
                       str(tmp_path / "tasks.meta.jsonl"), raising=False)
        tasks = run_pool.load_tasks()
        assert len(tasks) == 1
        assert tasks[0].prompt == STUDIO_INPUT
    finally:
        monkey.undo()


def test_studio_single_line_input_unchanged(svc, tmp_path):
    """★ 向后兼容：单行输入行为完全不变（还是 1 条、内容不变）。"""
    mgr = svc.PoolEngineManager()
    mgr._proc = None
    one = "一只橘猫在金色麦田里追逐蝴蝶，慢镜头，电影感浅景深"
    mgr.submit_tasks([{"prompt": one, "meta": {}}])

    prompts, _ = mgr._read_tasks_with_meta()
    assert prompts == [one]
    assert (tmp_path / "tasks.txt").read_text(encoding="utf-8") == one + "\n"


def test_studio_panel_shows_original_multiline(svc, tmp_path):
    """面板（停机反推）显示的是用户原始输入，不是 \n 字面量。"""
    mgr = svc.PoolEngineManager()
    mgr._proc = None
    mgr.submit_tasks([{"prompt": STUDIO_INPUT, "meta": {}}])

    snap = mgr.read_queue_state()
    assert snap["count"] == 1
    assert snap["rows"][0]["prompt"] == STUDIO_INPUT
    assert "\\n" not in snap["rows"][0]["prompt"]
