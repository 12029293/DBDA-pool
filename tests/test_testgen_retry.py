"""test_generate 对上游瞬时限流（710022002）的原地重试。

回归点（2026-10-08）：
acc48/acc49 面板「测试生成」相隔 5 秒从同一出口 IP 提交，被上游 710022002
「当前服务访问频繁」整批拒掉，test_generate 一次被拒即任务失败。
号池路径遇 transient 是换号重试；测试生成钉死指定账号，只能原地等待后重交。
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

import config


@pytest.fixture(autouse=True)
def _stubs():
    """注入桩模块，避免依赖 patchright / 真实上游（与 test_concurrency 同一套）。"""
    sys.modules.setdefault("patchright", types.ModuleType("patchright"))
    pa = types.ModuleType("patchright.async_api")

    def _no_playwright(*_a, **_k):
        raise RuntimeError("stub")

    pa.async_playwright = _no_playwright
    sys.modules.setdefault("patchright.async_api", pa)

    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError",
                "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))
    v.generate_video = _no_playwright
    v.resume_video = _no_playwright
    sys.modules["video_worker_ui"] = v

    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d
    yield


def _run(coro):
    """不用 asyncio.run：它会关掉主线程默认事件循环，带崩其它测试（同 test_concurrency）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _build_pool(tmp_path, monkeypatch):
    import browser_pool

    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    accounts_dir = tmp_path / "accounts"
    (accounts_dir / "acc40").mkdir(parents=True, exist_ok=True)
    bp = browser_pool.BrowserPool(
        accounts_dir=str(accounts_dir),
        db_path=str(tmp_path / "pool.db"),
        max_concurrency=0,
    )
    bp.set_login_status("acc40", True)
    return bp


def _ok_result(tmp_path):
    local = tmp_path / "out.mp4"
    local.write_bytes(b"fake")
    return {"local_path": str(local), "credits_used": 2}


RATE_LIMIT_MSG = "acc40 提交失败: 710022002: 当前服务访问频繁，请稍后重试"


def _patch_generate(bp, script, calls):
    """script: 异常实例列表（按次抛出），耗尽后返回成功结果。"""
    async def fake(account, prompt, ratio, duration, model, *,
                   on_conversation_id, on_poll, on_balance,
                   reference_image_paths):
        calls.append(account)
        n = len(calls)
        if n <= len(script):
            raise script[n - 1]
        return _ok_result(bp._tmp_path)

    bp._generate_effective = fake


def test_transient_rate_limit_is_retried_until_success(tmp_path, monkeypatch):
    """前两次被 710022002 拒、第三次成功 → 任务成功，共提交 3 次。"""
    monkeypatch.setattr(config, "TESTGEN_TRANSIENT_RETRIES", 2)
    monkeypatch.setattr(config, "TESTGEN_TRANSIENT_WAIT_SEC", 0)
    bp = _build_pool(tmp_path, monkeypatch)
    bp._tmp_path = tmp_path

    err = RuntimeError(RATE_LIMIT_MSG)
    calls: list = []
    _patch_generate(bp, [err, err], calls)

    result = _run(bp.test_generate("acc40", "测试", duration=10, model="seedance-2.5"))
    assert result["ok"] is True, result
    assert calls == ["acc40"] * 3


def test_transient_rate_limit_exhausted_reports_failure(tmp_path, monkeypatch):
    """重试次数用完仍被拒 → 任务失败，错误文案保留上游码，共提交 RETRIES+1 次。"""
    monkeypatch.setattr(config, "TESTGEN_TRANSIENT_RETRIES", 2)
    monkeypatch.setattr(config, "TESTGEN_TRANSIENT_WAIT_SEC", 0)
    bp = _build_pool(tmp_path, monkeypatch)
    bp._tmp_path = tmp_path

    err = RuntimeError(RATE_LIMIT_MSG)
    calls: list = []
    # script 给足 3 个错（RETRIES=2 → 最多 3 次提交），每次都被拒
    _patch_generate(bp, [err, err, err], calls)

    result = _run(bp.test_generate("acc40", "测试", duration=10, model="seedance-2.5"))
    assert result["ok"] is False
    assert "710022002" in result["error"]
    assert calls == ["acc40"] * 3


def test_non_transient_error_fails_immediately(tmp_path, monkeypatch):
    """非瞬态错误（如普通生成失败）不重试，一次被拒即返回。"""
    monkeypatch.setattr(config, "TESTGEN_TRANSIENT_RETRIES", 2)
    monkeypatch.setattr(config, "TESTGEN_TRANSIENT_WAIT_SEC", 0)
    bp = _build_pool(tmp_path, monkeypatch)
    bp._tmp_path = tmp_path

    err = RuntimeError("生成失败: 上游回执异常")
    calls: list = []
    _patch_generate(bp, [err], calls)

    result = _run(bp.test_generate("acc40", "测试", duration=10, model="seedance-2.5"))
    assert result["ok"] is False
    assert calls == ["acc40"]


def test_retries_disabled_restores_old_behavior(tmp_path, monkeypatch):
    """RETRIES=0 → 旧行为：一次被 710022002 拒即失败。"""
    monkeypatch.setattr(config, "TESTGEN_TRANSIENT_RETRIES", 0)
    monkeypatch.setattr(config, "TESTGEN_TRANSIENT_WAIT_SEC", 0)
    bp = _build_pool(tmp_path, monkeypatch)
    bp._tmp_path = tmp_path

    err = RuntimeError(RATE_LIMIT_MSG)
    calls: list = []
    _patch_generate(bp, [err], calls)

    result = _run(bp.test_generate("acc40", "测试", duration=10, model="seedance-2.5"))
    assert result["ok"] is False
    assert calls == ["acc40"]
