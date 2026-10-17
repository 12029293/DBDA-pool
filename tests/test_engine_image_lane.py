# -*- coding: utf-8 -*-
"""图片车道**端到端**夹具：驱动真实的 `doubao_image_dispatch_loop`。

与 `test_doubao_image_lane.py`（纯函数/档位单测）互补：这里跑的是
run_pool 里那条真实线程主体，只把「上游出图」换成桩件，
验证：出图 -> 下载 -> 落盘改名 -> 记账 -> 任务 done -> 台账写对。

★ 全部产物/台账都在 tmp_path 里（用例里 monkeypatch 掉 RP.OUT / RP.IMGDIR），
  **不碰生产 out/ledger.jsonl**，也不联外网、不碰真号、不耗额度。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_pool as RP  # noqa: E402


class _FakeStore:
    """最小号池存储桩：只记冷却/额度，够车道逻辑跑。"""

    def __init__(self):
        self.meta = {}
        self.charged = []
        self.cooled = []

    def roll_day(self, m):
        return m

    def read_meta(self, name):
        return dict(self.meta.get(name) or {"login_ok": True, "used_today": 0})

    def write_meta(self, name, m):
        self.meta[name] = dict(m)

    def patch_meta(self, name, **kw):
        cur = self.meta.setdefault(name, {"login_ok": True, "used_today": 0})
        cur.update(kw)

    def cooldown(self, name, secs, reason, kind="cooldown"):
        self.cooled.append((name, secs, kind))
        cur = self.meta.setdefault(name, {"login_ok": True, "used_today": 0})
        cur["cooldown_until"] = time.time() + secs
        cur["cooldown_kind"] = kind

    def charge(self, name, n):
        self.charged.append((name, n))


class _FakePool:
    """号池桩：成功/失败剧本由 outcome 决定。"""

    def __init__(self, outcome="ok"):
        self.accounts = ["db001"]
        self.store = _FakeStore()
        self.cooldown_seconds = 1800
        self.daily_limit = 1
        self.TRANSIENT_COOLDOWN_SEC = 600
        self.PAID_QUOTA_COOLDOWN_SEC = 3600
        self.outcome = outcome
        self.ran = []

    def _select(self, exclude=None):
        """模拟真实 `_select`：**冷却中的号不参与调度**。

        这一点很关键：真实号池在限流冷却后 `_select()` 会返回空，
        于是车道「睡到冷却到期」而不是立刻重试。若桩件忽略冷却，
        任务会被瞬间重试到 attempts 用尽并判失败 —— 那测的就不是
        产品行为，而是桩件的失真（本项目刚踩过这个坑）。
        """
        now = time.time()
        out = []
        for n in self.accounts:
            meta = self.store.read_meta(n)
            if float(meta.get("cooldown_until") or 0) > now:
                continue
            if int(meta.get("used_today") or 0) >= self.daily_limit:
                continue
            out.append(n)
        return out

    def _warmup_left(self, meta, now):
        return 0

    def lock_for(self, name):
        """★ 必须提供：车道在号锁内跑。

        缺了它会抛 AttributeError（发生在 `_run_one_image` 之前），
        用例就会看到 "other" 而不是剧本里的结局 —— 排查时极易被
        误读成「拒绝分支没生效」（本项目刚踩过这个坑）。
        """
        locks = getattr(self, "_locks", None)
        if locks is None:
            locks = self._locks = {}
        return locks.setdefault(name, asyncio.Lock())

    def _classify_failure(self, exc):
        from doubao_cn_video import DoubaoThrottleError
        return "throttled" if isinstance(exc, DoubaoThrottleError) else "other"

    # ---- 车道会调用的两个协作方法（此处替换掉真实网络） ----

    async def _run_one_image(self, name, prompt, ratio, model, timeout=None):
        self.ran.append((name, prompt, ratio, model))
        if self.outcome == "throttle":
            from doubao_cn_video import DoubaoThrottleError
            raise DoubaoThrottleError("上游限流(710022004): rate limited")
        if self.outcome == "rejected":
            from doubao_image import DoubaoImageRejectedError
            raise DoubaoImageRejectedError("豆包拒绝生成或未返回图片：内容不适宜")
        return {"images": ["https://img.example/a.jpeg"],
                "conversation_id": "conv123", "text": "",
                "account": name, "model": model, "ratio": ratio}

    async def _download_image(self, url, name):
        p = Path(RP.IMGDIR) / f"_tmp_{int(time.time() * 1000)}.jpeg"
        p.write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 4096)
        return str(p)


@pytest.fixture()
def _isolated(tmp_path, monkeypatch):
    """把产物目录与台账都指到 tmp_path，绝不碰生产 out/。"""
    out = tmp_path / "out"
    img = out / "img"
    img.mkdir(parents=True)
    monkeypatch.setattr(RP, "OUT", str(out))
    monkeypatch.setattr(RP, "IMGDIR", str(img))
    return {"out": out, "img": img}


def _drive(pool, task, timeout=6.0):
    """在子线程里跑图片车道，直到任务到达终态（或超时）。"""
    RP.tasks_q = RP.TaskQueue([task])
    stop = threading.Event()
    orig = RP._doubao_pool
    RP._doubao_pool = lambda: pool
    try:
        th = threading.Thread(target=RP.doubao_image_dispatch_loop,
                              args=(stop,), daemon=True)
        th.start()
        deadline = time.time() + timeout
        while time.time() < deadline and task.status not in ("done", "failed"):
            time.sleep(0.05)
        stop.set()
        th.join(timeout=5)
    finally:
        RP._doubao_pool = orig
    return task


def _ledger(out: Path) -> list:
    p = out / "ledger.jsonl"
    if not p.is_file():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_lane_success_writes_image_harvest(_isolated):
    """成功：出图 -> 落盘 out/img/<tid>_0.* -> task done -> harvest 带 image 标记。"""
    pool = _FakePool("ok")
    task = RP.Task("t901", "一只橘猫", prio=True,
                   meta={"channel": "doubao", "kind": "image",
                         "model": "seedream-4.5", "ratio": "3:2"})
    _drive(pool, task)

    assert task.status == "done", task.status
    assert pool.ran and pool.ran[0][2] == "3:2", pool.ran
    assert pool.store.charged == [("db001", 1)], pool.store.charged

    outs = sorted(_isolated["img"].glob("t901_*"))
    assert outs, "图片必须落盘到 out/img/"
    assert outs[0].name.startswith("t901_0"), outs[0].name

    hs = [e for e in _ledger(_isolated["out"]) if e.get("kind") == "harvest"]
    assert hs, "必须写 harvest 台账"
    h = hs[-1]
    assert h["kind_"] == "image"
    assert h["status"] == "succeeded"
    assert h["image_count"] >= 1
    assert h["local_paths"] and h["local_path"].endswith(outs[0].name)


def test_lane_throttle_cools_account_and_requeues(_isolated):
    """限流：号进短冷却、任务回队换号（绝不当成登录失效）。"""
    pool = _FakePool("throttle")
    task = RP.Task("t902", "x", meta={"channel": "doubao", "kind": "image"})
    _drive(pool, task)

    assert task.status == "pending", "限流应回队换号，而不是判失败"
    assert pool.store.cooled and pool.store.cooled[0][2] == "throttled", pool.store.cooled
    assert pool.store.read_meta("db001").get("login_ok") is not False, \
        "限流绝不能把号标成登录失效"


def test_lane_content_rejection_fails_without_burning_pool(_isolated):
    """★ 提示词级拒绝：直接判失败、**不换号**、不冷却（换号也没用）。"""
    pool = _FakePool("rejected")
    task = RP.Task("t903", "敏感提示词", meta={"channel": "doubao", "kind": "image"})
    _drive(pool, task)

    assert task.status == "failed"
    assert task.last_outcome == "DOUBAO_IMAGE_REJECTED"
    assert pool.store.cooled == [], "内容拒绝不该冷却账号"
    fs = [e for e in _ledger(_isolated["out"]) if e.get("kind") == "task_failed"]
    assert fs and fs[-1]["last_outcome"] == "DOUBAO_IMAGE_REJECTED"


def test_lane_ignores_video_tasks(_isolated):
    """图片车道只吃 kind=image，绝不抢视频任务。"""
    pool = _FakePool("ok")
    vtask = RP.Task("t904", "视频", meta={"channel": "doubao", "kind": "video"})
    RP.tasks_q = RP.TaskQueue([vtask])
    stop = threading.Event()
    orig = RP._doubao_pool
    RP._doubao_pool = lambda: pool
    try:
        th = threading.Thread(target=RP.doubao_image_dispatch_loop,
                              args=(stop,), daemon=True)
        th.start()
        time.sleep(1.0)
        stop.set()
        th.join(timeout=5)
    finally:
        RP._doubao_pool = orig
        RP.tasks_q = None
    assert vtask.status == "pending", "视频任务必须留给视频车道"
    assert pool.ran == [], "图片车道不该跑视频任务"


def test_lane_does_not_touch_production_ledger():
    """护栏：本测试文件必须把 OUT 指到 tmp，绝不写生产台账。

    防的是"后人给用例加了真产物路径、把测试事件混进生产台账" ——
    那种污染会让面板时间线冒出假记录。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    assert 'monkeypatch.setattr(RP, "OUT"' in src, \
        "必须 monkeypatch RP.OUT，否则会写生产 out/ledger.jsonl"
    assert 'monkeypatch.setattr(RP, "IMGDIR"' in src, \
        "必须 monkeypatch RP.IMGDIR，否则会往生产 out/img 落测试图"
