# -*- coding: utf-8 -*-
"""引擎成片的**真实耗时**回归测试。

为什么必须有这组测试
--------------------------------------------------------------------------
用户 2026-10-17 实测反馈：任务中心里引擎成片那几行的耗时徽标恒为 ``0M``，
看起来像「秒出」。根因是两个字段被填了同一个值：

    _sync_engine_tasks_to_db 里
        started_at = v["created_at"], finished_at = v["created_at"]

而 ``created_at`` 是 ``out/mp4/<f>.mp4`` 的 **mtime** —— 它既是成片落盘时刻，
又被当成「开始生成时刻」。前端算 ``finished_at - started_at`` 自然恒等于 0。

真实时刻在台账 ``out/ledger.jsonl`` 里成对存在：

    {"kind":"accepted","conv":"38418210586356753","task":"t001","tstr":"2026-10-16 23:42:08"}
    {"kind":"harvest","conv":"38418210586356753","task":"t001",
     "local_path":"...\\t001_38418210.mp4","tstr":"2026-10-16 23:50:40"}

修复：``_engine_video_rows`` 把 ``accepted``（受理）与 ``harvest``（成片）
两个 tstr 一起读出来，按 **conv** 配对（**不能按 tid**：tid 每轮从 t001 重排
复用，同一 tid 在台账里有多代记录，按 tid 配会张冠李戴）。

覆盖的关键风险：
  1. 有完整 accepted+harvest 时，耗时 = harvest - accepted（真的不是 0）；
  2. 配对用 conv，**不**被同名 tid 的其他代记录污染；
  3. 只有 harvest（bonus 片 / 老台账）时，不伪造起点 —— started_at 留空，
     前端显示「—」而不是假的 0M；
  4. _sync_engine_tasks_to_db 落库时写的两个时刻**不相等**；
  5. 老行（started_at == finished_at）被幂等修复；正常行不被覆盖。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ts(s: str) -> float:
    import datetime
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()


@pytest.fixture()
def engine_paths(tmp_path, monkeypatch):
    """把引擎的 MP4DIR / LEDGER / TASKS_FILE 指到临时目录。"""
    import pool_engine_service as svc
    mp4 = tmp_path / "mp4"
    mp4.mkdir()
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    tasks = tmp_path / "tasks.txt"
    tasks.write_text("", encoding="utf-8")
    monkeypatch.setattr(svc, "MP4DIR", mp4)
    monkeypatch.setattr(svc, "LEDGER", ledger)
    monkeypatch.setattr(svc, "TASKS_FILE", tasks)
    return {"mp4": mp4, "ledger": ledger, "tasks": tasks}


def _write_ledger(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")


def test_elapsed_uses_accepted_to_harvest(engine_paths):
    """有 accepted + harvest → started_at/finished_at 是真实一对，差不为 0。"""
    name = "t001_38418210.mp4"
    (engine_paths["mp4"] / name).write_bytes(b"\x00\x00\x00\x18ftyp")
    _write_ledger(engine_paths["ledger"], [
        {"kind": "accepted", "task": "t001", "conv": "38418210586356753",
         "tstr": "2026-10-16 23:42:08"},
        {"kind": "harvest", "task": "t001", "conv": "38418210586356753",
         "local_path": str(engine_paths["mp4"] / name), "status": "succeeded",
         "tstr": "2026-10-16 23:50:40"},
    ])

    import server
    rows = server._engine_video_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["started_at"] == pytest.approx(_ts("2026-10-16 23:42:08"))
    assert row["finished_at"] == pytest.approx(_ts("2026-10-16 23:50:40"))
    # 核心断言：两个时刻不能相等（旧 bug 正是恒等 → 面板 0M）
    assert row["finished_at"] > row["started_at"]
    assert round(row["finished_at"] - row["started_at"]) == 512      # 8分32秒
    assert row["conversation_id"] == "38418210586356753"


def test_pairing_is_by_conv_not_tid(engine_paths):
    """同一 tid 的多代记录不能串味：t001 有三代，各自按 conv 配对。"""
    names = ["t001_38418355.mp4", "t001_38418118.mp4", "t001_38418210.mp4"]
    for n in names:
        (engine_paths["mp4"] / n).write_bytes(b"\x00\x00\x00\x18ftyp")
    _write_ledger(engine_paths["ledger"], [
        # 第一代 t001（13:50 → 14:26）
        {"kind": "accepted", "task": "t001", "conv": "38418355274382609",
         "tstr": "2026-10-16 13:50:05"},
        {"kind": "harvest", "task": "t001", "conv": "38418355274382609",
         "local_path": str(engine_paths["mp4"] / "t001_38418355.mp4"),
         "tstr": "2026-10-16 14:26:28"},
        # 第二代 t001（20:54 → 21:32）
        {"kind": "accepted", "task": "t001", "conv": "38418118889422097",
         "tstr": "2026-10-16 20:54:37"},
        {"kind": "harvest", "task": "t001", "conv": "38418118889422097",
         "local_path": str(engine_paths["mp4"] / "t001_38418118.mp4"),
         "tstr": "2026-10-16 21:32:32"},
        # 第三代 t001（23:42 → 23:50）
        {"kind": "accepted", "task": "t001", "conv": "38418210586356753",
         "tstr": "2026-10-16 23:42:08"},
        {"kind": "harvest", "task": "t001", "conv": "38418210586356753",
         "local_path": str(engine_paths["mp4"] / "t001_38418210.mp4"),
         "tstr": "2026-10-16 23:50:40"},
    ])

    import server
    by_name = {r["id"]: r for r in server._engine_video_rows()}
    assert set(by_name) == {"eng:" + n for n in names}

    # 每一代都必须配到**自己那一次**受理时刻，而不是最早的 13:50
    expect = {
        "t001_38418355.mp4": ("2026-10-16 13:50:05", "2026-10-16 14:26:28"),
        "t001_38418118.mp4": ("2026-10-16 20:54:37", "2026-10-16 21:32:32"),
        "t001_38418210.mp4": ("2026-10-16 23:42:08", "2026-10-16 23:50:40"),
    }
    for name, (acc, har) in expect.items():
        r = by_name["eng:" + name]
        assert r["started_at"] == pytest.approx(_ts(acc)), name
        assert r["finished_at"] == pytest.approx(_ts(har)), name
        assert r["finished_at"] > r["started_at"], name


def test_bonus_harvest_has_no_fake_zero(engine_paths):
    """只有 harvest（无 accepted）→ started_at 留空，绝不用 mtime 造一个假的起点。"""
    name = "probe_38418321.mp4"
    (engine_paths["mp4"] / name).write_bytes(b"\x00\x00\x00\x18ftyp")
    _write_ledger(engine_paths["ledger"], [
        {"kind": "harvest", "task": "(bonus)", "conv": "38418321096600337",
         "local_path": str(engine_paths["mp4"] / name), "tstr": "2026-10-16 21:54:52"},
    ])

    import server
    row = server._engine_video_rows()[0]
    assert row["finished_at"] == pytest.approx(_ts("2026-10-16 21:54:52"))
    assert not row["started_at"], "bonus 片没有受理时刻，起点必须留空（前端显示 —）"
    # 前端 known 判定为假 → 显示「—」，而不是 ceil(0/60)=0M
    assert not (row["started_at"] and row["finished_at"] > row["started_at"])


def test_sync_writes_unequal_timestamps(engine_paths, tmp_path, monkeypatch):
    """落库时两个时刻必须不等（否则前端又回到 0M）。"""
    name = "t001_38418210.mp4"
    (engine_paths["mp4"] / name).write_bytes(b"\x00\x00\x00\x18ftyp")
    _write_ledger(engine_paths["ledger"], [
        {"kind": "accepted", "task": "t001", "conv": "38418210586356753",
         "tstr": "2026-10-16 23:42:08"},
        {"kind": "harvest", "task": "t001", "conv": "38418210586356753",
         "local_path": str(engine_paths["mp4"] / name),
         "tstr": "2026-10-16 23:50:40"},
    ])

    import server
    import store as store_mod
    st = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server, "store", st)

    added = server._sync_engine_tasks_to_db()
    assert added == 1
    row = st.get("eng_" + name)
    assert row["status"] == "completed"
    assert row["started_at"] != row["finished_at"]
    assert row["finished_at"] > row["started_at"]
    assert round(row["finished_at"] - row["started_at"]) == 512


def test_old_zero_elapsed_rows_are_healed(engine_paths, tmp_path, monkeypatch):
    """老行（两值相等 = 旧 bug 留下的）在下次同步时被就地修复；正常行不动。"""
    import server
    import store as store_mod
    st = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server, "store", st)

    # 旧 bug 行：started == finished（都是文件 mtime）
    broken = "eng_t001_38418210.mp4"
    st.create(broken, "seedance_v2.5", "汉服美女在赛博朋克的街道跑酷", "16:9", 30)
    st.update(broken, status="completed", started_at=1792165840.7114723,
              finished_at=1792165840.7114723)

    name = "t001_38418210.mp4"
    (engine_paths["mp4"] / name).write_bytes(b"\x00\x00\x00\x18ftyp")
    _write_ledger(engine_paths["ledger"], [
        {"kind": "accepted", "task": "t001", "conv": "38418210586356753",
         "tstr": "2026-10-16 23:42:08"},
        {"kind": "harvest", "task": "t001", "conv": "38418210586356753",
         "local_path": str(engine_paths["mp4"] / name),
         "tstr": "2026-10-16 23:50:40"},
    ])

    server._sync_engine_tasks_to_db()
    healed = st.get(broken)
    assert healed["started_at"] == pytest.approx(_ts("2026-10-16 23:42:08"))
    assert healed["finished_at"] == pytest.approx(_ts("2026-10-16 23:50:40"))
    assert healed["finished_at"] > healed["started_at"]

    # 再同步一次：已经是对的，不能被改坏（幂等）
    server._sync_engine_tasks_to_db()
    again = st.get(broken)
    assert again["started_at"] == pytest.approx(healed["started_at"])
    assert again["finished_at"] == pytest.approx(healed["finished_at"])


def test_healthy_rows_are_not_overwritten(engine_paths, tmp_path, monkeypatch):
    """已经是正常耗时的行（例如面板链路写的）不能被引擎同步覆盖。"""
    import server
    import store as store_mod
    st = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server, "store", st)

    tid = "eng_t001_38418210.mp4"
    st.create(tid, "seedance_v2.5", "p", "16:9", 30)
    # 一个明显合理、且与台账不同的耗时（模拟面板路径自己写的真实值）
    st.update(tid, status="completed", started_at=1792165000.0, finished_at=1792165800.0)

    name = "t001_38418210.mp4"
    (engine_paths["mp4"] / name).write_bytes(b"\x00\x00\x00\x18ftyp")
    _write_ledger(engine_paths["ledger"], [
        {"kind": "accepted", "task": "t001", "conv": "38418210586356753",
         "tstr": "2026-10-16 23:42:08"},
        {"kind": "harvest", "task": "t001", "conv": "38418210586356753",
         "local_path": str(engine_paths["mp4"] / name),
         "tstr": "2026-10-16 23:50:40"},
    ])

    server._sync_engine_tasks_to_db()
    row = st.get(tid)
    assert row["started_at"] == pytest.approx(1792165000.0)
    assert row["finished_at"] == pytest.approx(1792165800.0)
