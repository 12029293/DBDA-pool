"""测试生成的「僵尸回写」保护。

场景（2026-10-06 线上实测）：账号级测试生成一动就是 15~30 分钟且不刷 last_poll_at，
看门狗把它当孤儿重派（_run_task 换号重新生成）并写回 completed + video_url；
之后原测试协程才超时返回失败，无条件把同一行覆盖成 failed ——
结果就是「视频已经出片、任务栏却显示失败」。

保护：失败/异常回写前检查接管状态（账号被换或已 completed）则跳过。
"""
from __future__ import annotations

import asyncio
import sys
import types

import store as store_mod


def _stub_heavy_imports() -> None:
    """server.py 依赖 patchright / 浏览器栈，测试环境用空模块顶替。"""
    sys.modules.setdefault("patchright", types.ModuleType("patchright"))
    pa = sys.modules.setdefault("patchright.async_api", types.ModuleType("patchright.async_api"))
    if not hasattr(pa, "async_playwright"):
        pa.async_playwright = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stub"))
    v = sys.modules.setdefault("video_worker_ui", types.ModuleType("video_worker_ui"))
    for exc in ("AccountLimitedError", "CreditInsufficientError",
                "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))
    if not hasattr(v, "generate_video"):
        v.generate_video = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stub"))
    if not hasattr(v, "resume_video"):
        v.resume_video = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stub"))
    d = sys.modules.setdefault("dola_client", types.ModuleType("dola_client"))
    if not hasattr(d, "CreditError"):
        d.CreditError = type("CreditError", (Exception,), {})


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_zombie_testgen_does_not_clobber_completed(tmp_path, monkeypatch):
    """生成中途被重派并完成（completed + video_url）后，僵尸测试的失败不得覆盖。"""
    _stub_heavy_imports()
    monkeypatch.setattr("config.DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr("config.POOL_DB_PATH", str(tmp_path / "pool.db"))
    import server

    st = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server, "store", st)

    class _FakePool:
        async def test_generate(self, name, prompt, ratio, duration, model, on_poll=None):
            if on_poll:
                on_poll(99.0)  # 心跳回调不该抛错
            # 模拟看门狗在生成期间重派接管：换号 acc11 并已出片完成
            st.update("t_zombie", status="completed", account="acc11",
                      video_url="http://127.0.0.1:8002/videos/acc11_x.mp4",
                      finished_at=2.0)
            return {"ok": False, "account": name,
                    "error": "acc2 出片未成功 status=timeout: 视频生成目前支持 4 到 15 秒。"}

    monkeypatch.setattr(server, "pool", _FakePool())
    server.TEST_JOBS["t_zombie"] = {"status": "running", "result": None, "error": ""}

    _run(server._run_test_generate("t_zombie", "acc2", "p", None, 10, "seedance-2.5"))

    row = st.get("t_zombie")
    assert row["status"] == "completed"                # 不被覆盖成 failed
    assert row["account"] == "acc11"
    assert row["video_url"] == "http://127.0.0.1:8002/videos/acc11_x.mp4"
    assert server.TEST_JOBS["t_zombie"]["status"] == "done"


def test_zombie_testgen_skips_when_account_switched(tmp_path, monkeypatch):
    """重派接管但还没出片（账号已换成别人）：僵尸测试的失败也不得回写。"""
    _stub_heavy_imports()
    monkeypatch.setattr("config.DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr("config.POOL_DB_PATH", str(tmp_path / "pool.db"))
    import server

    st = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server, "store", st)

    class _FakePool:
        async def test_generate(self, name, prompt, ratio, duration, model, on_poll=None):
            # 模拟重派接管：账号已换成 acc10、还在生成中
            st.update("t_take", status="processing", account="acc10")
            return {"ok": False, "account": name, "error": "acc1 出片未成功 status=pending: x"}

    monkeypatch.setattr(server, "pool", _FakePool())
    server.TEST_JOBS["t_take"] = {"status": "running", "result": None, "error": ""}

    _run(server._run_test_generate("t_take", "acc1", "p", None, 30, "seedance-2.5"))

    row = st.get("t_take")
    assert row["status"] == "processing"               # 保持重派的生成中状态
    assert not (row["error"] or "")                    # 没有被写入僵尸失败文案


def test_normal_testgen_failure_still_writes(tmp_path, monkeypatch):
    """没有重派接管时，测试失败照常回写（保护不能把正常失败也吞了）。"""
    _stub_heavy_imports()
    monkeypatch.setattr("config.DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr("config.POOL_DB_PATH", str(tmp_path / "pool.db"))
    import server

    st = store_mod.TaskStore(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(server, "store", st)

    class _FakePool:
        async def test_generate(self, name, prompt, ratio, duration, model, on_poll=None):
            return {"ok": False, "account": name, "error": "acc3 出片未成功 status=failed: 审核不合规"}

    monkeypatch.setattr(server, "pool", _FakePool())
    server.TEST_JOBS["t_norm"] = {"status": "running", "result": None, "error": ""}

    _run(server._run_test_generate("t_norm", "acc3", "p", None, 10, "seedance-2.5"))

    row = st.get("t_norm")
    assert row["status"] == "failed"
    assert "acc3 出片未成功" in (row["error"] or "")
