# -*- coding: utf-8 -*-
"""多行提示词回归测试：创作台/发起出片提交一整段剧本，必须作为**一条**任务。

为什么必须有这组测试
--------------------------------------------------------------------------
用户实测：在创作台粘贴一整段带时间轴的分镜剧本（30 秒视频的 6 个镜头），
提交后任务中心多出十几条任务，每条只拿到一个时间戳行 —— 一部片子被拆成
一堆 5 秒碎片，且提示词在列表里看起来像乱码。

根因：tasks.txt 的文件协议是「一行一条提示词」，而所有提交路径都用
``splitlines()`` 按行拆；多行提示词写进文件后，每一行都被当成独立任务。

修复（三层）：
  1) **存储层**：写入前把换行转成 ``\\n`` 字面量、读出来时还原
     （run_pool._escape_prompt_newlines / _unescape_prompt_newlines，
       pool_engine_service.escape_prompt_newlines / unescape_prompt_newlines）；
  2) **拆分层**：server._split_prompt_blocks 按「空行分段」拆任务，
     段内换行保留；没有空行时退化为按行拆分（一行一条的旧行为不变）；
  3) **展示层**：所有读 tasks.txt 的路径都还原换行，面板看到的是原始输入。

覆盖的关键风险：
  1. 单行提示词**字节级不变**（向后兼容，老队列零影响）；
  2. 多行提示词写进 tasks.txt 只占**一行**，读回来是完整原文；
  3. 空行分段 = 一条任务；一行一条的填法行为不变；
  4. 优先标记与换行转义共存（@@PRIO@@前缀 + \n字面量）；
  5. 清空队列（_sync_tasks_file_after_clear）后多行提示词不被摊成多行；
  6. 热加载往返不产生双重转义（\\n 不会被转成 \\\\n）；
  7. meta 与 tasks.txt 条数依然严格对齐（不错挂素材）。
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

# 用户在创作台实际粘贴的那段剧本（截图里的原文）。
# 注意：这是**三段**镜头描述（0—4秒 / 4—9秒 / 15—22秒），段与段之间有空行 ——
# 按「空行分段」规则它就是 3 条任务，每条的段内多行描述完整保留。
STORY = ("0—4秒｜大远景｜宇宙战场\n"
         "宇宙一片死寂。巨大黑色敌方母舰像钢铁大陆横亘星空，表面红色能量脉络缓慢亮起。\n"
         "远处一颗蓝色行星悬在黑暗中，边缘被战火映出暗红。\n"
         "\n"
         "4—9秒｜中景转特写｜人类舰队残骸区\n"
         "人类舰队只剩断裂舰体和漂浮碎片。镜头穿过残骸，缓慢推近一艘小型人类战机。\n"
         "\n"
         "15—22秒｜快速推进｜敌方母舰核心通道\n"
         "战机冲入母舰内部通道。红色能量脉络像血管一样亮起，警报光疯狂闪烁。\n"
         "飞行员低声说：“为了黎明。”战机开始最后加速。")

# STORY 按空行分段后的 3 条任务（与 server._split_prompt_blocks 的输出一致）
STORY_BLOCKS = [
    ("0—4秒｜大远景｜宇宙战场\n"
     "宇宙一片死寂。巨大黑色敌方母舰像钢铁大陆横亘星空，表面红色能量脉络缓慢亮起。\n"
     "远处一颗蓝色行星悬在黑暗中，边缘被战火映出暗红。"),
    ("4—9秒｜中景转特写｜人类舰队残骸区\n"
     "人类舰队只剩断裂舰体和漂浮碎片。镜头穿过残骸，缓慢推近一艘小型人类战机。"),
    ("15—22秒｜快速推进｜敌方母舰核心通道\n"
     "战机冲入母舰内部通道。红色能量脉络像血管一样亮起，警报光疯狂闪烁。\n"
     "飞行员低声说：“为了黎明。”战机开始最后加速。"),
]

# 单个镜头段落（段内多行、段间无空行）—— 这是「一条任务」的最小复现
ONE_SHOT = STORY_BLOCKS[0]


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    """把引擎模块的路径指向临时目录，并重载它以套用新的全局路径。"""
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


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    """pool_engine_service 的路径也指向临时目录（它的常量是模块级的）。

    ★ QUEUE_STATE 必须一起指过来：否则 read_queue_state() 的新鲜度检查会命中
      项目里真实的 out/queue_state.json（引擎在跑时每 3 秒重写一次），
      读到的是真实队列而不是测试刚写的那几条。
    """
    monkeypatch.setenv("I794_TASKS_FILE", "tasks.txt")
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(ROOT))
    import pool_engine_service as s
    monkeypatch.setattr(s, "TASKS_FILE", tmp_path / "tasks.txt", raising=False)
    monkeypatch.setattr(s, "RUN", tmp_path, raising=False)
    monkeypatch.setattr(s, "QUEUE_STATE", tmp_path / "queue_state.json", raising=False)
    return s


# ------------------------------------------------- 1. 转义/反转义本身 --

def test_single_line_prompt_is_untouched(engine, svc):
    """★ 向后兼容：单行提示词必须字节级不变（老队列零影响）。"""
    assert engine._escape_prompt_newlines("一只橘猫在金色麦田里追逐蝴蝶") \
        == "一只橘猫在金色麦田里追逐蝴蝶"
    assert engine._unescape_prompt_newlines("一只橘猫在金色麦田里追逐蝴蝶") \
        == "一只橘猫在金色麦田里追逐蝴蝶"
    assert svc.escape_prompt_newlines("一只橘猫在金色麦田里追逐蝴蝶") \
        == "一只橘猫在金色麦田里追逐蝴蝶"
    assert svc.unescape_prompt_newlines("一只橘猫在金色麦田里追逐蝴蝶") \
        == "一只橘猫在金色麦田里追逐蝴蝶"


def test_escape_unescape_roundtrip(engine, svc):
    """写→读往返后必须是完整原文（含所有换行）。"""
    for esc, unesc in ((engine._escape_prompt_newlines, engine._unescape_prompt_newlines),
                       (svc.escape_prompt_newlines, svc.unescape_prompt_newlines)):
        assert unesc(esc(ONE_SHOT)) == ONE_SHOT
        # CRLF / 纯 CR 也归一化处理
        assert unesc(esc("第一行\r\n第二行")) == "第一行\n第二行"
        assert unesc(esc("第一行\r第二行")) == "第一行\n第二行"


def test_escaped_multiline_has_no_real_newline(engine, svc):
    """转义后的字符串**不含真实换行** —— 这是能安全写进 tasks.txt 一行的前提。"""
    assert "\n" not in engine._escape_prompt_newlines(ONE_SHOT)
    assert "\r" not in engine._escape_prompt_newlines(ONE_SHOT)
    assert "\n" not in svc.escape_prompt_newlines(ONE_SHOT)
    assert "\\n" in engine._escape_prompt_newlines(ONE_SHOT)


def test_both_layers_agree(engine, svc):
    """run_pool 与 pool_engine_service 的转义必须一致（同一份文件的读写方）。"""
    assert engine._escape_prompt_newlines(ONE_SHOT) == svc.escape_prompt_newlines(ONE_SHOT)
    assert engine._unescape_prompt_newlines(svc.escape_prompt_newlines(ONE_SHOT)) == ONE_SHOT
    assert svc.unescape_prompt_newlines(engine._escape_prompt_newlines(ONE_SHOT)) == ONE_SHOT


def test_unescape_is_idempotent_for_plain_text(engine):
    """不含字面量的行原样返回（手编 tasks.txt / 老队列）。"""
    assert engine._unescape_prompt_newlines("普通一行提示词") == "普通一行提示词"
    assert engine._unescape_prompt_newlines("") == ""


# ------------------------------------------------- 2. 引擎读回 --

def test_load_tasks_keeps_multiline_prompt_as_one_task(engine):
    """★ 核心回归：一段多行剧本写进 tasks.txt 后，load_tasks() 只读出 **1 条** 任务，
    且提示词是完整原文（不是第一行，也不是十几条碎片）。"""
    Path(engine.TASKS_FILE).write_text(
        engine._escape_prompt_newlines(ONE_SHOT) + "\n", encoding="utf-8")

    tasks = engine.load_tasks()

    assert len(tasks) == 1
    assert tasks[0].prompt == ONE_SHOT
    assert tasks[0].tid == "t001"


def test_load_tasks_mixes_single_and_multiline(engine):
    """单行 + 多行混排：条数 = 文件行数，各自内容正确。"""
    single = "汉服美女在赛博朋克的街道跑酷"
    Path(engine.TASKS_FILE).write_text(
        "\n".join([engine._escape_prompt_newlines(ONE_SHOT), single]) + "\n",
        encoding="utf-8")

    tasks = engine.load_tasks()

    assert len(tasks) == 2
    assert tasks[0].prompt == ONE_SHOT
    assert tasks[1].prompt == single


def test_prio_marker_and_multiline_coexist(engine):
    """优先标记与换行转义必须共存：@@PRIO@@ 在前，\n 字面量在后。"""
    Path(engine.TASKS_FILE).write_text(
        engine.PRIO_MARK + engine._escape_prompt_newlines(ONE_SHOT) + "\n",
        encoding="utf-8")

    tasks = engine.load_tasks()

    assert len(tasks) == 1
    assert tasks[0].prio is True
    assert tasks[0].prompt == ONE_SHOT          # 标记不残留


def test_read_task_lines_restores_newlines(engine):
    """_read_task_lines（热加载指纹用）也必须还原换行。"""
    Path(engine.TASKS_FILE).write_text(
        engine._escape_prompt_newlines(ONE_SHOT) + "\n", encoding="utf-8")
    assert engine._read_task_lines() == [ONE_SHOT]


def test_read_task_entries_restores_newlines(engine):
    """_read_task_entries（热加载入队用）也必须还原换行并保留 prio。"""
    Path(engine.TASKS_FILE).write_text(
        engine.PRIO_MARK + engine._escape_prompt_newlines(ONE_SHOT) + "\n",
        encoding="utf-8")
    entries = engine._read_task_entries()
    assert entries == [(ONE_SHOT, True)]


# ------------------------------------------------- 3. 队列改写不摊平 --

def test_clear_queue_keeps_multiline_prompt_on_one_line(engine):
    """★ 清空队列（_sync_tasks_file_after_clear）后，多行提示词在文件里仍只占一行。

    实测背景：老实现直接 write(t.prompt)，内存里的多行提示词被摊成 N 行，
    下一轮 load_tasks() 读回来就变成 N 条任务 —— 清一次队列，一条 30 秒片
    变成十几条 5 秒碎片。
    """
    Path(engine.TASKS_FILE).write_text(
        engine._escape_prompt_newlines(ONE_SHOT) + "\n", encoding="utf-8")
    tasks = engine.load_tasks()

    # 造一个假的 TaskQueue：一条已受理（done）的多行任务 + 一条 pending
    class _Q:
        def __init__(self, items):
            self._tasks = items
    done_task = tasks[0]
    done_task.status = "done"
    pending_task = engine.Task("t002", single := "另一条单行任务", prio=False)
    pending_task.status = "pending"
    engine.tasks_q = _Q([done_task, pending_task])

    engine._sync_tasks_file_after_clear()

    # 文件里必须只有 1 行（done 那条），且是转义后的单行
    text = Path(engine.TASKS_FILE).read_text(encoding="utf-8")
    assert text.count("\n") == 1
    assert "\n" not in text.rstrip("\n")
    # 再读回来：还是 1 条完整原文
    assert engine.load_tasks()[0].prompt == ONE_SHOT
    # meta 也按同一个 keep 列表裁（没有 meta 文件时不新建，与既有行为一致）
    if Path(engine.TASKS_META_FILE).is_file():
        metas = [json.loads(x) for x in
                 Path(engine.TASKS_META_FILE).read_text(encoding="utf-8").splitlines()
                 if x.strip()]
        assert len(metas) == 1


# ------------------------------------------------- 4. 提交层 --

def test_submit_tasks_writes_multiline_as_one_line(svc, tmp_path):
    """★ 核心回归：submit_tasks 提交一段多行剧本 → tasks.txt 只多 **1 行**。"""
    import pool_engine_service as s
    mgr = s.PoolEngineManager()      # __init__ 只设属性，不起进程
    mgr._proc = None                # 明确「引擎没在跑」，避免扫进程表

    res = mgr.submit_tasks([{"prompt": ONE_SHOT, "meta": {"ratio": "9:16"}}])

    assert res["ok"] is True
    assert res["added"] == 1
    text = (tmp_path / "tasks.txt").read_text(encoding="utf-8")
    assert text.count("\n") == 1                    # 只占一行
    assert "\n" not in text.rstrip("\n")
    # 读回来：1 条、完整原文、参数不错挂
    prompts, metas = mgr._read_tasks_with_meta()
    assert prompts == [ONE_SHOT]
    assert metas[0]["ratio"] == "9:16"


def test_submit_tasks_is_idempotent_for_multiline(svc, tmp_path):
    """同一段多行剧本提交两次：第二次先还原再写入，不产生双重转义（\\\\n）。"""
    import pool_engine_service as s
    mgr = s.PoolEngineManager()      # __init__ 只设属性，不起进程
    mgr._proc = None

    mgr.submit_tasks([{"prompt": ONE_SHOT, "meta": {}}])
    mgr.submit_tasks([{"prompt": ONE_SHOT, "meta": {}}])

    text = (tmp_path / "tasks.txt").read_text(encoding="utf-8")
    assert "\\\\n" not in text                       # 没有二次转义
    prompts, _ = mgr._read_tasks_with_meta()
    assert prompts == [ONE_SHOT, ONE_SHOT]                 # 两条独立任务，各自完整


def test_submit_tasks_prio_and_multiline(svc, tmp_path):
    """发起出片（prio=True）+ 多行剧本：文件行首是 @@PRIO@@，行内是 \n 字面量。"""
    import pool_engine_service as s
    mgr = s.PoolEngineManager()      # __init__ 只设属性，不起进程
    mgr._proc = None

    mgr.submit_tasks([{"prompt": ONE_SHOT, "meta": {}}], prio=True)

    line = (tmp_path / "tasks.txt").read_text(encoding="utf-8").rstrip("\n")
    assert line.startswith(s.PRIO_MARK)
    assert line.count("\n") == 0
    prompts, _ = mgr._read_tasks_with_meta()
    assert prompts == [ONE_SHOT]


def test_start_escapes_multiline_prompts(svc, tmp_path):
    """pool_engine_service.start() 传入多行 prompts 也要转义后落盘。"""
    import pool_engine_service as s
    mgr = s.PoolEngineManager()      # __init__ 只设属性，不起进程
    mgr._proc = None

    # 只测「写文件」这一段：直接调内部静态方法等价路径
    escaped = [s.escape_prompt_newlines(p) for p in [ONE_SHOT, "单行任务"]]
    s.PoolEngineManager._write_tasks_with_meta(escaped, [{}, {}])

    text = (tmp_path / "tasks.txt").read_text(encoding="utf-8")
    assert text.count("\n") == 2
    prompts, metas = mgr._read_tasks_with_meta()
    assert prompts == [ONE_SHOT, "单行任务"]


def test_read_queue_state_restores_newlines_when_engine_down(svc, tmp_path):
    """引擎停机反推路径（面板在引擎没跑时也要看队列）必须还原换行。"""
    (tmp_path / "tasks.txt").write_text(
        svc.escape_prompt_newlines(ONE_SHOT) + "\n", encoding="utf-8")
    import pool_engine_service as s
    mgr = s.PoolEngineManager()      # __init__ 只设属性，不起进程
    mgr._proc = None

    snap = mgr.read_queue_state()

    assert snap["count"] == 1
    assert snap["rows"][0]["prompt"] == ONE_SHOT
    assert snap["rows"][0]["status"] == "pending"


# ------------------------------------------------- 5. 拆分层 --

def test_split_prompt_blocks_blank_line_separates_tasks():
    """批量出片（默认 whole=False）：空行分段，整段剧本按镜头拆成 3 条任务。"""
    import server
    blocks = server._split_prompt_blocks(STORY)
    assert blocks == STORY_BLOCKS


def test_split_prompt_blocks_whole_mode_keeps_story_as_one_task():
    """★ 创作台/发起出片（whole=True）：整段剧本作为 **1 条** 任务。

    这是用户实测场景：在创作台粘贴 30 秒片的完整分镜，提交后必须是 1 条任务，
    不是 6 条（按镜头段）也不是 18 条（按行）。

    空行被压掉（只剔注释与空行后拼接）—— 上游提示词不需要空行，压掉更干净；
    真正的换行（镜头描述之间的分行）全部保留。
    """
    import server
    got = server._split_prompt_blocks(STORY, whole=True)
    assert len(got) == 1
    assert got[0] == STORY.replace("\n\n", "\n")
    # 单镜头多行也是 1 条
    assert server._split_prompt_blocks(ONE_SHOT, whole=True) == [ONE_SHOT]
    # 12 行连续描述（无空行）也是 1 条 —— 老实现会拆成 12 条
    many_lines = "\n".join("第%d行描述" % i for i in range(1, 13))
    assert server._split_prompt_blocks(many_lines, whole=True) == [many_lines]
    # 单行也是 1 条（不改变单行提交的行为）
    assert server._split_prompt_blocks("一只橘猫", whole=True) == ["一只橘猫"]


def test_split_prompt_blocks_single_multiline_block_is_one_task():
    """★ 批量模式下，带空行分隔的一段多行任务是 1 条（段内换行保留）。"""
    import server
    # 用空行把 ONE_SHOT 单独包起来 → 走「空行分段」分支
    assert server._split_prompt_blocks(ONE_SHOT + "\n\n") == [ONE_SHOT]
    assert server._split_prompt_blocks("\n\n" + ONE_SHOT) == [ONE_SHOT]
    # 两段：各自一条，段内多行完整
    assert server._split_prompt_blocks(STORY_BLOCKS[0] + "\n\n" + STORY_BLOCKS[1]) \
        == [STORY_BLOCKS[0], STORY_BLOCKS[1]]


def test_split_prompt_blocks_no_blank_line_falls_back_to_one_per_line():
    """★ 向后兼容：没有空行 → 一行一条（批量出片 textarea 的旧行为）。"""
    import server
    assert server._split_prompt_blocks("提示词一\n提示词二\n提示词三") \
        == ["提示词一", "提示词二", "提示词三"]
    # ONE_SHOT 是 3 行无空行 → 3 条（这是**有意的**：批量出片就该一行一条）
    assert server._split_prompt_blocks(ONE_SHOT) == ONE_SHOT.split("\n")


def test_split_prompt_blocks_two_paragraphs():
    """两段各自一条任务。"""
    import server
    blocks = server._split_prompt_blocks(
        "一只橘猫在金色麦田里追逐蝴蝶\n\n无人机飞过东京雨夜街道")
    assert blocks == ["一只橘猫在金色麦田里追逐蝴蝶", "无人机飞过东京雨夜街道"]


def test_split_prompt_blocks_skips_comments_and_blanks():
    """# 注释与纯空段被跳过。"""
    import server
    blocks = server._split_prompt_blocks(
        "# 这是注释\n\n   \n\n第一条\n\n\n\n# 另一条注释\n第二条")
    assert blocks == ["第一条", "第二条"]


def test_split_prompt_blocks_empty():
    import server
    assert server._split_prompt_blocks("") == []
    assert server._split_prompt_blocks("   \n\n  \n") == []
    assert server._split_prompt_blocks("# 只有注释") == []


def test_split_prompt_blocks_crlf():
    """Windows 粘贴的 CRLF 也要按空行分段。"""
    import server
    blocks = server._split_prompt_blocks("第一段\r\n\r\n第二段")
    assert blocks == ["第一段", "第二段"]


# ------------------------------------------------- 6. 面板展示 --

def test_engine_video_rows_never_guesses_prompt_from_line_number(tmp_path, monkeypatch):
    """「查看视频」**不得**按 tasks.txt 行号给老成片反查提示词。

    历史实现有个 ``prompt_by_tid["t%03d" % n]`` 兜底：台账没记 prompt 时，拿
    tasks.txt 的第 n 行当提示词。但 tid 是**每轮重排复用**的（t001→t105…），队列
    一换批，行号与 tid 就彻底脱钩 —— 实测同一部片在「任务中心」与「视频库」显示
    两个互不相同的提示词（t002_38418427 真实是"无人机缓缓飞过东京雨夜街道…"，
    视频库却按行号显示成"汉服美女在赛博朋克的街道跑酷"）。

    所以这里钉住新口径：**宁可留空（前端显示 —），也不能显示错的**。
    造一个 t001 成片 + 空台账 + 非空 tasks.txt → prompt 必须是空串。
    """
    import pool_engine_service as svc_mod
    (tmp_path / "tasks.txt").write_text(
        svc_mod.escape_prompt_newlines(ONE_SHOT) + "\n", encoding="utf-8")
    (tmp_path / "mp4").mkdir(exist_ok=True)
    (tmp_path / "mp4" / "t001_38418118.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    (tmp_path / "ledger.jsonl").write_text("", encoding="utf-8")

    import server
    monkeypatch.setattr(svc_mod, "TASKS_FILE", tmp_path / "tasks.txt")
    monkeypatch.setattr(svc_mod, "MP4DIR", tmp_path / "mp4")
    monkeypatch.setattr(svc_mod, "LEDGER", tmp_path / "ledger.jsonl")

    rows = server._engine_video_rows()
    assert rows, "应该合出 1 行成片记录"
    assert rows[0]["prompt"] == "", "台账没记 prompt 时必须留空，绝不能按行号猜"


def test_engine_video_rows_prompt_comes_from_ledger(tmp_path, monkeypatch):
    """台账记了 prompt 时按台账取（权威映射），且换行要还原。"""
    import json as _json
    import pool_engine_service as svc_mod
    (tmp_path / "tasks.txt").write_text("另一批任务的提示词\n", encoding="utf-8")
    (tmp_path / "mp4").mkdir(exist_ok=True)
    (tmp_path / "mp4" / "t001_38418118.mp4").write_bytes(b"\x00\x00\x00\x18ftyp")
    (tmp_path / "ledger.jsonl").write_text(
        _json.dumps({"kind": "harvest", "task": "t001", "conv": "38418118889422097",
                     "local_path": str(tmp_path / "mp4" / "t001_38418118.mp4"),
                     "prompt": ONE_SHOT, "tstr": "2026-10-16 21:32:32"},
                    ensure_ascii=False) + "\n", encoding="utf-8")

    import server
    monkeypatch.setattr(svc_mod, "TASKS_FILE", tmp_path / "tasks.txt")
    monkeypatch.setattr(svc_mod, "MP4DIR", tmp_path / "mp4")
    monkeypatch.setattr(svc_mod, "LEDGER", tmp_path / "ledger.jsonl")

    rows = server._engine_video_rows()
    assert rows[0]["prompt"] == ONE_SHOT
    assert "\n" in rows[0]["prompt"]        # 换行真的还原了


def test_queue_rows_from_ledger_restores_newlines(tmp_path, monkeypatch):
    """引擎停机时合成队列行（_queue_rows_from_ledger）也要还原换行。"""
    import pool_engine_service as svc_mod
    (tmp_path / "tasks.txt").write_text(
        svc_mod.escape_prompt_newlines(ONE_SHOT) + "\n", encoding="utf-8")
    (tmp_path / "ledger.jsonl").write_text("", encoding="utf-8")

    import server
    monkeypatch.setattr(svc_mod, "TASKS_FILE", tmp_path / "tasks.txt")
    monkeypatch.setattr(svc_mod, "LEDGER", tmp_path / "ledger.jsonl")

    rows = server._queue_rows_from_ledger()
    assert len(rows) == 1
    assert rows[0]["prompt"] == ONE_SHOT
