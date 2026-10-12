"""cookie 批量文本导入的命名/去重/错误分支（键接 pool + cookie_login）。

通过注入桩模块让 cookie_login 可导入，再 monkeypatch 掉真实浏览器导入函数。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _stubs():
    def _mod(name):
        return types.ModuleType(name)

    # video_worker_ui
    v = _mod("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError", "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))
    async def _g(*_a, **_k):
        raise RuntimeError("stub")
    async def _r(*_a, **_k):
        raise RuntimeError("stub")
    v.generate_video = _g
    v.resume_video = _r
    sys.modules["video_worker_ui"] = v

    d = _mod("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d

    # browser
    b = _mod("browser")
    b.EDGE_MARKER = ".dola-browser"
    b.LAUNCH_ARGS = []
    b.proxy_kwargs_for = lambda *a, **k: None
    async def _check(_name):
        return True
    b.check_login_state = _check
    sys.modules["browser"] = b

    # patchright
    pr = _mod("patchright")
    pr_api = _mod("patchright.async_api")
    class _FakeAsyncPlaywright:
        @staticmethod
        async def __aenter__(*_a, **_k):
            return _FakeAP()
        @staticmethod
        async def __aexit__(*_a, **_k):
            return False
    class _FakeAP:
        pass
    async def _async_playwright(*_a, **_k):
        return _FakeAsyncPlaywright()
    pr_api.async_playwright = _async_playwright
    sys.modules["patchright"] = pr
    sys.modules["patchright.async_api"] = pr_api
    yield


def _make_pool(tmp_path):
    import browser_pool, proxy_store
    import config
    config.POOL_DB_PATH = str(tmp_path / "pool_usage.db")
    proxy_store.DB_PATH = Path(config.POOL_DB_PATH)
    accounts_dir = tmp_path / "accounts"
    for name in ("acc9", "acc10"):
        d = accounts_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / ".dola-browser").write_text("")
    bp = browser_pool.BrowserPool(accounts_dir=str(accounts_dir), max_concurrency=1)
    _ = bp.accounts  # 触发扫描，创建 accounts_meta 行
    return bp


def test_batch_import_dedup_and_naming(tmp_path, monkeypatch):
    import cookie_login
    import browser_pool
    bp = _make_pool(tmp_path)
    # 已有一个 sessionid=sid-one 的账号
    bp.set_sessionid("acc9", "sid-one")
    bp.set_login_status("acc9", True)

    written: dict[str, str] = {}

    async def fake_import(name, cookie_data, require_login=True):
        # 假导入：建 meta 行 + 记录 sessionid
        (bp.accounts_dir / name).mkdir(parents=True, exist_ok=True)
        bp.set_sessionid(name, next(
            (c["value"] for c in cookie_data.get("cookies", []) if c["name"] == "sessionid"), ""
        ))
        bp.set_login_status(name, True)
        written[name] = "ok"
        return {"ok": True, "account": name, "verified": True}

    monkeypatch.setattr(cookie_login, "import_cookie_account", fake_import)
    raw = (
        "passport_csrf_token=aaa; sessionid=sid-one\n"   # 已存在 → updated
        "passport_csrf_token=bbb; sessionid=sid-two\n"   # 新 → acc11（跳过 acc9/acc10）
    )
    result = __import__("asyncio").get_event_loop().run_until_complete(
        cookie_login.import_cookie_accounts_from_text(bp, raw)
    )
    assert result["updated"] == ["acc9"]
    # 新号命名为 acc1（从 acc1 起顺延，acc9/acc10 被占也不影响前面的空闲名）
    assert result["created"] == ["acc1"]
    assert written.get("acc1") == "ok"
    # cookie 导入路径应把来源标记为 cookie
    assert next(x for x in bp.list_accounts() if x["name"] == "acc1")["source"] == "cookie"


def _pure_probe_stub(monkeypatch, result=(True, "logged_in")):
    """把纯 API 探活换成桩，避免测试打真实上游。"""
    module = types.ModuleType("pure_api_gen")
    calls: list = []
    def probe_login_detail(state_file, *args, **kwargs):
        calls.append(state_file)
        ok, reason = result
        # 桩默认硬判定（对齐上游明确回未登录的场景）
        return (ok, reason, True)
    def probe_login(state_file, *args, **kwargs):
        ok, reason, _hard = probe_login_detail(state_file, *args, **kwargs)
        return ok, reason
    module.probe_login = probe_login
    module.probe_login_detail = probe_login_detail
    monkeypatch.setitem(sys.modules, "pure_api_gen", module)
    return calls


def test_import_cookie_account_falls_back_to_pure(tmp_path, monkeypatch):
    """浏览器后端不可用（CentOS7 glibc 2.17）时，导入不该失败，而是走纯 API。"""
    import asyncio
    import cookie_login

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cookie_login, "browser_backend_available",
                        lambda: (False, "驱动无法执行: GLIBC_2.28 not found"))
    probe_calls = _pure_probe_stub(monkeypatch)

    import config
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)

    bundle = {"cookies": [
        {"name": "ttwid", "value": "1%7Cabc", "domain": ".dola.com", "path": "/"},
        {"name": "sessionid", "value": "sid-pure", "domain": ".dola.com", "path": "/"},
    ]}
    result = asyncio.get_event_loop().run_until_complete(
        cookie_login.import_cookie_account("acc1", bundle, require_login=True)
    )

    assert result["engine"] == "pure"
    assert result["verified"] is True
    assert result["browser_error"].startswith("驱动无法执行")
    assert len(probe_calls) == 1
    state_file = Path(result["cookie_state_file"])
    assert state_file.is_file()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["cookies"]["sessionid"]["value"] == "sid-pure"
    # 不应该留下浏览器 profile 里的其它文件
    assert sorted(p.name for p in state_file.parent.iterdir()) == ["cookie_state.json"]


def test_import_cookie_account_forced_browser_still_falls_back(tmp_path, monkeypatch):
    """即使调用方指定浏览器，驱动不可用时也退回纯 API，而不是整批导入报错。"""
    import asyncio
    import cookie_login

    monkeypatch.chdir(tmp_path)
    probe_calls = _pure_probe_stub(monkeypatch, result=(False, "not logged in"))

    bundle = {"cookies": [
        {"name": "sessionid", "value": "sid-x", "domain": ".dola.com", "path": "/"},
    ]}
    result = asyncio.get_event_loop().run_until_complete(
        cookie_login.import_cookie_account("acc2", bundle, browser=True)
    )

    assert result["engine"] == "pure"
    assert result["verified"] is False
    assert result["verify_error"] == "not logged in"
    assert len(probe_calls) == 1


def test_import_cookie_account_skip_verify_does_not_probe(tmp_path, monkeypatch):
    import asyncio
    import cookie_login

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cookie_login, "browser_backend_available", lambda: (False, "no driver"))
    probe_calls = _pure_probe_stub(monkeypatch)

    bundle = {"cookies": [
        {"name": "sessionid", "value": "sid-y", "domain": ".dola.com", "path": "/"},
    ]}
    result = asyncio.get_event_loop().run_until_complete(
        cookie_login.import_cookie_account("acc3", bundle, require_login=False)
    )
    assert result["engine"] == "pure"
    assert result["verified"] is False
    assert probe_calls == []


# ---- 软失败不误标「登录失效」（2026-10-06 注册批次误标事故的回归测试）----

def test_verify_soft_fail_keeps_login_ok(tmp_path, monkeypatch):
    """网络软失败（探活没跑成）不得改 login_ok，只记 failed_reason。"""
    import asyncio
    import browser_pool as bp_mod
    import config

    bp = _make_pool(tmp_path)
    d = bp.accounts_dir / "acc9"
    (d / "cookie_state.json").write_text("{}", encoding="utf-8")
    bp.set_source("acc9", "cookie")
    bp.set_login_status("acc9", True)   # 原本是健康号
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    monkeypatch.setattr(
        bp_mod, "probe_login_detail",
        lambda *a, **k: (False, "[网络软失败] Connection aborted", False))

    ok, reason, soft = asyncio.get_event_loop().run_until_complete(
        bp.verify_account_detail("acc9"))

    assert ok is False
    assert soft is True
    meta = bp._meta("acc9")
    assert meta["login_ok"] == 1                      # 没被误标成登录失效
    assert "网络软失败" in (meta["failed_reason"] or "")


def test_verify_hard_fail_marks_expired(tmp_path, monkeypatch):
    """上游明确回未登录（硬信号）才落 login_ok=0。"""
    import asyncio
    import browser_pool as bp_mod
    import config

    bp = _make_pool(tmp_path)
    d = bp.accounts_dir / "acc9"
    (d / "cookie_state.json").write_text("{}", encoding="utf-8")
    bp.set_source("acc9", "cookie")
    bp.set_login_status("acc9", True)
    monkeypatch.setattr(config, "PURE_API_ENABLED", True)
    monkeypatch.setattr(
        bp_mod, "probe_login_detail",
        lambda *a, **k: (False, "not logged in (x-tt-agw-login=0)", True))

    ok, reason, soft = asyncio.get_event_loop().run_until_complete(
        bp.verify_account_detail("acc9"))

    assert ok is False and soft is False
    assert bp._meta("acc9")["login_ok"] == 0


def test_batch_import_soft_fail_keeps_pending(tmp_path, monkeypatch):
    """导入时探活软失败：login_ok 保持 None（待激活），原因落 failed_reason。"""
    import asyncio
    import cookie_login

    bp = _make_pool(tmp_path)

    async def fake_import(name, cookie_data, require_login=True):
        (bp.accounts_dir / name).mkdir(parents=True, exist_ok=True)
        return {"ok": True, "account": name, "verified": False,
                "verify_hard": False, "verify_error": "[网络软失败] timed out"}

    monkeypatch.setattr(cookie_login, "import_cookie_account", fake_import)
    raw = "sessionid=sid-soft\n"
    result = asyncio.get_event_loop().run_until_complete(
        cookie_login.import_cookie_accounts_from_text(bp, raw)
    )
    assert result["unverified"] == ["acc1"]
    meta = bp._meta("acc1")
    assert meta["login_ok"] is None                   # 待激活，不是「登录失效」
    assert "网络软失败" in (meta["failed_reason"] or "")
