# -*- coding: utf-8 -*-
"""「运行状态」不该每次打开都从零统计（进程枚举缓存）。

背景（用户 2026-10-17 实测反馈）
--------------------------------------------------------------------------
面板「运行状态」页每 5 秒自动刷新一次，而一次 ``manager.status()`` 会顺着
调用链起 **4 个 wmic 子进程**（实测合计 0.8~1.0s）：

    _pid_file_running() → _pid_is_our_engine() → _scan_engine_pids()   # wmic ①
                        → _scan_engine_pids()                          # wmic ②
    _run_started_tstr() → _proc_elapsed()                              # wmic ③
    status() 取 elapsed  → _proc_elapsed()                             # wmic ④

同一份进程表被反复枚举，用户在页面上感受到的就是"每次打开运行状态都要
从零统计一遍"。

修复：``_scan_engine_pids`` / ``_proc_elapsed`` 按 PID 做 3 秒 TTL 缓存，
且**只对展示路径生效**。安全路径（启动判重、停止、清队列、覆盖、提交）
显式传 ``fresh=True`` 绕过缓存 —— 判错会拉起第二个引擎（fact 629 反模式：
两个嗅探器互抢窗口相位）。

这里钉住三件事：
  1. 缓存命中时**不再起 wmic 子进程**（性能回归）；
  2. ``_proc_elapsed`` 缓存的必须是**创建时刻**而不是"已运行秒数"，
     否则「已运行 X 分钟」会停住不走；
  3. 安全路径 ``fresh=True`` 必须真的绕过缓存（否则判重会吃到过期结果）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pool_engine_service as svc  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_proc_cache():
    """每个用例从干净缓存出发，避免用例之间互相污染。"""
    with svc._proc_cache_lock:
        svc._proc_cache.clear()
    yield
    with svc._proc_cache_lock:
        svc._proc_cache.clear()


@pytest.fixture()
def spy_wmic(monkeypatch):
    """记录 wmic 调用次数，并让 wmic 返回可控输出（不真起子进程）。"""
    calls: list[list[str]] = []
    created = time.time() - 600          # 假装这个进程 10 分钟前创建

    def fake_run(cmd, *a, **k):
        cmd = list(cmd)
        calls.append(cmd)
        joined = " ".join(cmd)

        class _R:
            pass

        r = _R()
        if "CreationDate" in joined:
            stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(created))
            r.stdout = f"CreationDate={stamp}+480\r\n".encode("ascii")
        else:
            # /format:csv 的进程表：含一行 run_pool.py
            r.stdout = (
                "Node,CommandLine,ProcessId\r\n"
                "PC,\"C:\\py.exe -u C:\\x\\run_pool.py\",14220\r\n"
                "PC,\"C:\\other.exe\",999\r\n"
            ).encode("ascii")
        return r

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    return {"calls": calls, "created": created}


def test_repeated_scan_hits_cache_without_wmic(spy_wmic):
    """第二次起必须命中缓存：不再起 wmic 子进程。"""
    mgr = svc.PoolEngineManager()
    first = mgr._scan_engine_pids()
    n_after_first = len(spy_wmic["calls"])
    assert first == [14220]
    assert n_after_first == 1, "首次应当真查一次进程表"

    for _ in range(5):
        assert mgr._scan_engine_pids() == [14220]
    assert len(spy_wmic["calls"]) == n_after_first, \
        "缓存命中时不得再起 wmic（这正是「每次打开都从零统计」的根因）"


def test_fresh_true_bypasses_cache(spy_wmic):
    """安全路径 fresh=True 必须真的绕过缓存（判重不能吃过期结果）。"""
    mgr = svc.PoolEngineManager()
    mgr._scan_engine_pids()
    n = len(spy_wmic["calls"])
    mgr._scan_engine_pids(fresh=True)
    assert len(spy_wmic["calls"]) == n + 1, "fresh=True 必须重新枚举进程表"


def test_proc_elapsed_caches_creation_not_duration(spy_wmic):
    """缓存的必须是创建时刻：已运行秒数要**继续增长**，不能停住。"""
    # 直接调用不受缓存影响的 fresh=True，确保先建立缓存
    e1 = svc._proc_elapsed(14220)
    n = len(spy_wmic["calls"])
    # 命中缓存：不再起 wmic
    e2 = svc._proc_elapsed(14220)
    assert len(spy_wmic["calls"]) == n, "第二次应命中缓存"
    assert e2 >= e1, "已运行时长不能倒退"
    # 关键：缓存的是创建时刻，所以时间前进后 e3 必须 > e1。
    # 若错把"已运行秒数"缓存下来，e3 会等于 e1（进度条卡死）。
    cached = svc._proc_cache_get("proc_created:14220")
    assert cached is not None, "应当缓存创建时刻"
    assert abs(cached - spy_wmic["created"]) < 2, "缓存的应是 CreationDate 换算出的 epoch"


def test_cache_ttl_expiry_triggers_refetch(monkeypatch, spy_wmic):
    """TTL 过期后应当重新查（否则引擎被杀掉后面板会一直说"在运行"）。"""
    mgr = svc.PoolEngineManager()
    mgr._scan_engine_pids()
    n = len(spy_wmic["calls"])
    # 把缓存时间戳往前拨，模拟 TTL 过期
    with svc._proc_cache_lock:
        for key, (ts, val) in list(svc._proc_cache.items()):
            svc._proc_cache[key] = (ts - (svc._PROC_CACHE_TTL + 1), val)
    mgr._scan_engine_pids()
    assert len(spy_wmic["calls"]) == n + 1, "TTL 过期后必须重新枚举"


def test_ttl_covers_frontend_refresh_interval():
    """TTL 必须 > 前端自动刷新间隔，否则缓存等于没做。

    「运行状态」页是 ``setInterval(engRefresh, 5000)``（web/index.html）。
    实测教训：TTL 设成 3s 时，5 秒一次的刷新**每次**都恰好过期，
    依旧每次付 4 个 wmic 子进程的代价（0.45s），用户感受不到任何改善。
    """
    from pathlib import Path

    import re

    html = (Path(__file__).resolve().parent.parent / "web" / "index.html"
            ).read_text(encoding="utf-8")
    m = re.search(r"setInterval\(engRefresh,\s*(\d+)\)", html)
    assert m, "应当能在面板里找到运行状态的自动刷新间隔"
    refresh_ms = int(m.group(1))
    assert svc._PROC_CACHE_TTL * 1000 > refresh_ms, (
        f"TTL={svc._PROC_CACHE_TTL}s 必须大于刷新间隔 {refresh_ms}ms，"
        "否则每次刷新都重建缓存，缓存形同虚设")

