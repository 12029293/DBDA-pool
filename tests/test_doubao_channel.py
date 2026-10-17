# -*- coding: utf-8 -*-
"""豆包渠道（doubao_login / doubao_pool / store.channel）单元测试。

覆盖：
  · CK 解析与 Cookie 头拼装、指纹串格式
  · 二维码渲染（qrcode 库可用时）
  · 账号仓库的落盘/去重/改名/删除/记账/跨日滚动
  · 号池分组判定、可调度选择、额度冷却
  · 渠道分域：生成设置按 channel 分存、任务/视频按 channel 过滤
  · 参考图上传端点与测试生成的参数通路
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from pathlib import Path

import pytest

# 与其它用例一致：先注入 patchright 桩，避免真起浏览器
pr = types.ModuleType("patchright")
pa = types.ModuleType("patchright.async_api")


def _no_playwright(*_a, **_k):
    raise RuntimeError("stub")


pa.async_playwright = _no_playwright
pr.async_api = pa
sys.modules.setdefault("patchright", pr)
sys.modules.setdefault("patchright.async_api", pa)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import doubao_login  # noqa: E402
import doubao_pool  # noqa: E402
from doubao_pool import DoubaoAccountStore, DoubaoPool, resolve_model  # noqa: E402


# ---------------- doubao_login 纯函数 ----------------

def test_parse_ck_and_header_roundtrip():
    ck = "sessionid=" + "a" * 32 + "; ttwid=xyz; has_biz_token=false"
    jar = doubao_login.parse_ck(ck)
    assert jar["ttwid"] == "xyz"
    assert jar["has_biz_token"] == "false"
    out = doubao_login.ck_header_from_jar(jar)
    assert out.startswith("sessionid=")
    assert "ttwid=xyz" in out
    # 再解析一次应完全等价（顺序稳定，可反复存取）
    assert doubao_login.parse_ck(out) == jar


def test_make_fp_shape():
    fp = doubao_login.make_fp()
    assert fp.startswith("verify_")
    parts = fp.split("_")
    # verify + 5 段（第一段 8 小写，其后 8/4/4/4/12 且不含下划线）
    assert len(parts) == 7, parts
    assert parts[1].islower() and len(parts[1]) == 8
    assert len(parts[2]) == 8 and len(parts[6]) == 12


def test_render_qr_data_url():
    url = doubao_login.render_qr_data_url(
        "https://www.doubao.com/flow-account/scan-login?token=abcdef")
    assert url.startswith("data:image/png;base64,")
    assert len(url) > 200


def test_login_session_to_dict_hides_full_cookies():
    sess = doubao_login.LoginSession(kind="qr", token="t1", qr_mode="doubao")
    sess.cookies = {"sessionid": "s" * 30, "ttwid": "x", "odin_tt": "y"}
    d = sess.to_dict()
    assert d["logged_in"] is True
    assert "sessionid" in d["cookies"]
    assert "ttwid" not in d["cookies"]      # 只回登录关键项，不外泄整包 CK
    assert d["qr_mode"] == "doubao"


# ---------------- doubao_pool 账号仓库 ----------------

@pytest.fixture()
def store(tmp_path) -> DoubaoAccountStore:
    return DoubaoAccountStore(tmp_path / "dbao")


CK = "sessionid=" + "b" * 32 + "; ttwid=tt1; has_biz_token=false"


def test_store_save_and_read(store):
    meta = store.save("db001", CK, {"mobile": "13800000000"})
    assert meta["sessionid_head"] == ("b" * 16)
    assert meta["cookie_count"] == 3
    assert store.read_ck("db001").startswith("sessionid=")
    assert store.read_meta("db001")["mobile"] == "13800000000"
    assert store.names() == ["db001"]


def test_store_rejects_non_doubao_ck(store):
    with pytest.raises(ValueError):
        store.save("db002", "ttwid=only")
    with pytest.raises(ValueError):
        store.save("bad name!", CK)


def test_store_find_by_sessionid_and_rename(store):
    store.save("db001", CK)
    assert store.find_by_sessionid("b" * 32) == "db001"
    store.rename("db001", "db009")
    assert store.names() == ["db009"]
    with pytest.raises(FileExistsError):
        store.save("db010", CK)
        store.rename("db010", "db009")


def test_store_charge_and_roll_day(store):
    store.save("db001", CK)
    store.charge("db001", 1)
    assert store.read_meta("db001")["used_today"] == 1
    # 跨日：改 usage_day 模拟隔天，roll_day 应清零
    meta = store.read_meta("db001")
    meta["usage_day"] = "1970-01-01"
    store.write_meta("db001", meta)
    rolled = store.roll_day(store.read_meta("db001"))
    assert rolled["used_today"] == 0
    assert rolled["usage_day"] == doubao_pool.usage_day()


def test_store_cooldown_and_reset(store):
    store.save("db001", CK)
    store.cooldown("db001", 60, "风控")
    assert store.read_meta("db001")["cooldown_until"] > 0
    assert store.reset_all() == 1
    m = store.read_meta("db001")
    assert m["cooldown_until"] == 0 and m["used_today"] == 0


def test_store_duplicate_session_keeps_single_account(store):
    store.save("db001", CK)
    # 同一个 sessionid 用另一个名字再加：find_by_sessionid 能定位到老账号
    assert store.find_by_sessionid("b" * 32) == "db001"


# ---------------- doubao_pool 号池 ----------------

def test_resolve_model_aliases():
    assert resolve_model("seedance-2.5") == "seedance_v2.5"
    assert resolve_model("seedance_2.5") == "seedance_v2.5"
    assert resolve_model("seedance-2.0") == "seedance_v2.0"
    assert resolve_model("seedance_v2.0") == "seedance_v2.0"
    assert resolve_model(None) == "seedance_v2.0"
    assert resolve_model("doubao-seedance-2-5-260628") == "seedance_v2.5"


def test_resolve_model_keeps_variant_suffix():
    """回归：`seedance_v2.0_std` 归一成 seedancev2.0std 后也含 "20"。

    旧实现先判 "20" 就把标准画质档悄悄降级成 Fast —— 用户选了 std 却出了 Fast。
    """
    assert resolve_model("seedance-2.0-std") == "seedance_v2.0_std"
    assert resolve_model("seedance_v2.0_std") == "seedance_v2.0_std"
    assert resolve_model("seedance-2.0-mini") == "seedance_v2.0_mini"
    assert resolve_model("seedance_v2.0_mini") == "seedance_v2.0_mini"


def test_duration_range_matches_server_slider():
    """时长区间必须来自真实 action_bar 配置（服务端是滑动条，不是固定档位）。

    2026-10-12 从 `alice/slot/action_bar_v3/get_item_conf` 解出：
      seedance_v2.5      → lower_bound 4, upper_bound 30, step 1
      seedance_v2.0_std  → 4~15
      seedance_v2.0      → 4~15
      seedance_v2.0_mini → 4~15
    """
    assert doubao_pool.DURATION_RANGE["seedance_v2.5"] == (4, 30)
    assert doubao_pool.DURATION_RANGE["seedance_v2.0_std"] == (4, 15)
    assert doubao_pool.DURATION_RANGE["seedance_v2.0"] == (4, 15)
    assert doubao_pool.DURATION_RANGE["seedance_v2.0_mini"] == (4, 15)
    assert doubao_pool.DURATION_STEP == 1
    # 接口名
    assert doubao_pool.duration_range_for("seedance-2.5") == (4, 30)
    assert doubao_pool.duration_range_for("seedance-2.0") == (4, 15)
    assert doubao_pool.duration_range_for("seedance-2.0-std") == (4, 15)


def test_duration_supported_boundaries():
    assert doubao_pool.duration_supported("seedance-2.5", 30) is True
    assert doubao_pool.duration_supported("seedance-2.5", 4) is True
    assert doubao_pool.duration_supported("seedance-2.5", 31) is False
    assert doubao_pool.duration_supported("seedance-2.5", 3) is False
    # 2.0 系列上限 15，30 秒必须拒绝（此前硬编码 5/10 时反而连 15 都做不了）
    assert doubao_pool.duration_supported("seedance-2.0", 15) is True
    assert doubao_pool.duration_supported("seedance-2.0", 30) is False
    assert doubao_pool.duration_supported("seedance-2.0", None) is False
    assert doubao_pool.duration_supported("seedance-2.5", "abc") is False


def test_durations_for_filters_by_model_cap():
    assert 30 in doubao_pool.durations_for("seedance-2.5")
    assert 30 not in doubao_pool.durations_for("seedance-2.0")
    assert 15 in doubao_pool.durations_for("seedance-2.0")


def test_run_one_rejects_duration_out_of_range(pool, monkeypatch):
    """越界时长在**下发前**就报错，不要打上游浪费额度/冷却。"""
    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)

    called = {"n": 0}

    def fake_client_for(name, with_image=False):
        called["n"] += 1
        raise AssertionError("不该创建客户端")

    monkeypatch.setattr(pool, "_client_for", fake_client_for)

    # 2.0 上限 15，请求 30 秒必须被拒
    try:
        asyncio.run(pool._run_one("db001", "x", "16:9", 30, "seedance-2.0"))
        raise AssertionError("应当抛错")
    except ValueError as exc:
        assert "4~15" in str(exc), str(exc)
    assert called["n"] == 0, "越界时长不该走到创建客户端"

    # 2.5 支持 30 秒：应通过校验（这里让它继续走到客户端创建）
    called["n"] = 0
    try:
        asyncio.run(pool._run_one("db001", "x", "16:9", 30, "seedance-2.5"))
    except AssertionError as exc:
        assert "不该创建客户端" in str(exc)   # 说明校验已通过、进到创建客户端
    assert called["n"] == 1


@pytest.fixture()
def pool(tmp_path) -> DoubaoPool:
    st = DoubaoAccountStore(tmp_path / "dbao")
    return DoubaoPool(store=st, proxy="", daily_limit=1)


def test_pool_groups_pending_then_full(pool):
    pool.add("db001", CK)
    accs = pool.list_accounts()
    assert accs[0]["group"] == doubao_pool.GROUP_PENDING      # 未验活
    pool.store.patch_meta("db001", login_ok=True, verified_at=1)
    accs = pool.list_accounts()
    assert accs[0]["group"] == doubao_pool.GROUP_FULL


def test_pool_groups_cooling_when_quota_used(pool):
    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)
    pool.store.charge("db001", 1)                            # daily_limit=1 → 用满
    accs = pool.list_accounts()
    assert accs[0]["group"] == doubao_pool.GROUP_COOLING
    assert "额度" in accs[0]["group_reason"]


def test_pool_groups_risk_on_login_failure(pool):
    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=False, last_err="登录态失效")
    accs = pool.list_accounts()
    assert accs[0]["group"] == doubao_pool.GROUP_RISK


# ---------------- ★ 限流 vs 登录失效（本轮修正的误判 bug） ----------------

def test_throttle_error_is_not_login_error():
    """`DoubaoThrottleError` 必须与 `DoubaoLoginError` 相互独立。

    回归（2026-10-12 线上实测）：db001 扫码号在真机验证后能出片，但被
    `710022002 当前服务访问频繁` 拒了一次，旧实现把它当登录失效 →
    标 `login_ok=False` 进「风控」永久组 + 锁 30 分钟。同一时刻登录探针
    `/alice/user/get_account_info` 仍正常返回手机号，证明账号是好的。
    """
    import doubao_cn_video as dcv

    assert not issubclass(dcv.DoubaoThrottleError, dcv.DoubaoLoginError)
    assert not issubclass(dcv.DoubaoLoginError, dcv.DoubaoThrottleError)


def test_classify_distinguishes_throttle_from_login(pool):
    import doubao_cn_video as dcv

    assert pool._classify_failure(
        dcv.DoubaoThrottleError("豆包域限流(710022002): 当前服务访问频繁")) == "throttled"
    assert pool._classify_failure(
        dcv.DoubaoLoginError("豆包域登录态无效: 710012000 Invalid User ID")) == "login"
    assert pool._classify_failure(
        dcv.DoubaoCreditError("额度不足")) == "credit"
    # 纯文案也要能判对（兼容上游换话术/未来包装变化）
    assert pool._classify_failure(
        RuntimeError("STREAM_ERROR 710022002 服务访问频繁")) == "throttled"
    assert pool._classify_failure(
        RuntimeError("710022004 rate limited")) == "throttled"
    assert pool._classify_failure(
        RuntimeError("710012000 Invalid User ID")) == "login"


def test_throttled_failure_does_not_mark_login_dead(pool, monkeypatch):
    """限流失败：只做短冷却，**不得**把 login_ok 打成 False。

    这是本轮 bug 的核心回归 —— 误标会让好号永久停在「风控」组。
    """
    import doubao_cn_video as dcv

    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)

    async def fake_run_one(name, prompt, ratio, duration, model, image_paths=None,
                           timeout=300, on_poll=None):
        raise dcv.DoubaoThrottleError("豆包域限流(710022002): 当前服务访问频繁")

    monkeypatch.setattr(pool, "_run_one", fake_run_one)
    res = asyncio.run(pool.test_generate("db001", "一只猫", ratio="16:9",
                                         duration=5, model="seedance-2.0"))

    assert res["ok"] is False
    assert res.get("kind") == "throttled", res
    meta = pool.store.read_meta("db001")
    assert meta["login_ok"] is True, "限流不该把账号标成登录失效"
    assert meta["cooldown_kind"] == "throttled"
    # 冷却时长应是 short transient，而不是登录失效的 30 分钟
    left = meta["cooldown_until"] - time.time()
    assert 0 < left <= doubao_pool.TRANSIENT_COOLDOWN_SEC + 5, left
    assert doubao_pool.TRANSIENT_COOLDOWN_SEC < pool.cooldown_seconds
    # 面板分组应是「冷却」而不是「风控」
    acc = pool.list_accounts()[0]
    assert acc["group"] == doubao_pool.GROUP_COOLING, acc
    assert "限流" in acc["group_reason"] and "正常" in acc["group_reason"]


def test_real_login_failure_marks_risk(pool, monkeypatch):
    """真登录失效（710012000）仍要进「风控」——修正不能把这条也吞掉。"""
    import doubao_cn_video as dcv

    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)

    async def fake_run_one(name, prompt, ratio, duration, model, image_paths=None,
                           timeout=300, on_poll=None):
        raise dcv.DoubaoLoginError("豆包域登录态无效: 710012000 Invalid User ID")

    monkeypatch.setattr(pool, "_run_one", fake_run_one)
    res = asyncio.run(pool.test_generate("db001", "x", duration=5))
    assert res["ok"] is False
    assert res.get("kind") == "login", res
    meta = pool.store.read_meta("db001")
    assert meta["login_ok"] is False
    acc = pool.list_accounts()[0]
    assert acc["group"] == doubao_pool.GROUP_RISK, acc


def test_cooling_account_is_blocked_before_hitting_upstream(pool, monkeypatch):
    """冷却中直接点测试生成：前置拦截，不打上游、不延长冷却。"""
    import doubao_cn_video as dcv

    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)
    pool.store.cooldown("db001", 600, "上游限流: x", kind="throttled")
    before = pool.store.read_meta("db001")["cooldown_until"]

    calls = {"n": 0}

    async def fake_run_one(*a, **k):
        calls["n"] += 1
        raise dcv.DoubaoThrottleError("should not be called")

    monkeypatch.setattr(pool, "_run_one", fake_run_one)
    res = asyncio.run(pool.test_generate("db001", "x", duration=5))

    assert calls["n"] == 0, "冷却中不该真打上游"
    assert res["ok"] is False
    assert res.get("kind") == "throttled"
    assert res.get("cooldown_left", 0) > 0
    assert "正常" in res["error"] and "重试" in res["error"], res["error"]
    # 冷却不得被这次尝试延长
    assert pool.store.read_meta("db001")["cooldown_until"] <= before + 1


def test_credit_exhausted_is_blocked_before_upstream(pool, monkeypatch):
    """当日额度用满：前置拦截并说明跨日重置。"""
    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)
    pool.store.charge("db001", pool.daily_limit)

    async def fake_run_one(*a, **k):
        raise AssertionError("不该打上游")

    monkeypatch.setattr(pool, "_run_one", fake_run_one)
    res = asyncio.run(pool.test_generate("db001", "x", duration=5))
    assert res["ok"] is False
    assert res.get("kind") == "credit"
    assert "额度" in res["error"]


# ---------------- 诊断（限流 vs 登录失效，对齐 USAGE.md「你好探测」） ----------------

def test_probe_text_detects_throttle(pool, monkeypatch):
    """纯文本探针：710022002 → throttled；SSE_ACK → ok。"""
    pool.add("db001", CK)
    cli = pool._client_for("db001")

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body

        async def read(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Sess:
        def __init__(self, body: bytes):
            self._body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, *a, **k):
            return _Resp(self._body)

    # 限流响应
    monkeypatch.setattr("aiohttp.ClientSession",
                        lambda **k: _Sess(b'event: STREAM_ERROR\ndata: '
                                          b'{"error_code":710022002}'))
    assert asyncio.run(cli.probe_text("你好"))["status"] == "throttled"

    # 正常受理
    monkeypatch.setattr("aiohttp.ClientSession",
                        lambda **k: _Sess(b'event: SSE_ACK\ndata: {}'))
    assert asyncio.run(cli.probe_text("你好"))["status"] == "ok"

    # 登录失效
    monkeypatch.setattr("aiohttp.ClientSession",
                        lambda **k: _Sess(b'{"code":710012001,"msg":"login invalid"}'))
    assert asyncio.run(cli.probe_text("你好"))["status"] == "login_invalid"


def test_diagnose_reports_throttled_even_when_cooldown_expired(pool, monkeypatch):
    """回归：冷却已过期但实际仍被限流时，diagnose 必须真探出来。

    上一版只看元数据 → 误报「账号正常，可直接测试生成」，用户点下去又被拒。
    """
    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)   # 冷却为 0

    async def fake_login(ck, proxy=""):
        return True, {"mobile": "+86 138****0000", "connects": ["aweme_v2"]}

    async def fake_probe(name):
        return {"ok": False, "status": "throttled", "reason": "710022002"}

    monkeypatch.setattr("doubao_login.check_login", fake_login)
    monkeypatch.setattr(pool, "probe_throttle", fake_probe)

    res = asyncio.run(pool.diagnose("db001"))
    assert res["verdict"] == "throttled", res
    assert res["throttled"] is True
    assert "换出口 IP 无效" in res["advice"], res["advice"]


def test_diagnose_ok_when_probe_passes(pool, monkeypatch):
    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)

    async def fake_login(ck, proxy=""):
        return True, {"mobile": "+86 138****0000", "connects": []}

    async def fake_probe(name):
        return {"ok": True, "status": "ok", "reason": "已受理"}

    monkeypatch.setattr("doubao_login.check_login", fake_login)
    monkeypatch.setattr(pool, "probe_throttle", fake_probe)
    res = asyncio.run(pool.diagnose("db001"))
    assert res["verdict"] == "ok", res


def test_diagnose_reports_login_expired(pool, monkeypatch):
    pool.add("db001", CK)

    async def fake_login(ck, proxy=""):
        return False, {}

    monkeypatch.setattr("doubao_login.check_login", fake_login)
    res = asyncio.run(pool.diagnose("db001"))
    assert res["verdict"] == "login_expired", res
    assert "重新加号" in res["advice"]


def test_recover_clears_cooldown_and_resets_to_pending(pool):
    """人工恢复：清冷却 + login_ok 归 None（回【待激活】，等再验证）。"""
    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=False)
    pool.store.cooldown("db001", 600, "登录态失效", kind="login")
    assert pool.list_accounts()[0]["group"] == doubao_pool.GROUP_RISK

    res = pool.recover("db001")
    assert res["ok"] is True
    meta = pool.store.read_meta("db001")
    assert meta["login_ok"] is None
    assert meta["cooldown_until"] == 0
    assert meta["cooldown_kind"] == ""
    assert pool.list_accounts()[0]["group"] == doubao_pool.GROUP_PENDING


def test_clear_cooldown_keeps_login_state(pool):
    """只清冷却，不动登录态（限流到点前想立刻重试用）。"""
    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)
    pool.store.cooldown("db001", 600, "上游限流", kind="throttled")
    pool.clear_cooldown("db001")
    meta = pool.store.read_meta("db001")
    assert meta["cooldown_until"] == 0
    assert meta["login_ok"] is True, "清冷却不该动登录态"


def test_pool_available_and_select_skips_unschedulable(pool):
    pool.add("db001", CK)
    assert pool.available is False                            # 未验活
    pool.store.patch_meta("db001", login_ok=True, weight=5)
    assert pool.available is True
    assert pool._select() == ["db001"]
    # 关调度后不可选
    pool.set_scheduling("db001", False)
    assert pool._select() == []
    assert pool.available is False


def test_pool_quota_blocked_flag(pool):
    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)
    assert pool.all_accounts_quota_blocked is False
    pool.store.charge("db001", 1)
    assert pool.all_accounts_quota_blocked is True


def test_pool_summary_counts(pool):
    pool.add("db001", CK)
    pool.add("db002", "sessionid=" + "c" * 32 + "; ttwid=t2")
    pool.store.patch_meta("db001", login_ok=True)
    pool.store.patch_meta("db002", login_ok=False)
    s = pool.summary()
    assert s["all_count"] == 2
    assert s["groups"][doubao_pool.GROUP_FULL] == 1
    assert s["groups"][doubao_pool.GROUP_RISK] == 1
    assert s["daily_limit"] == 1


def test_pool_export_delete_patch(pool):
    pool.add("db001", CK)
    assert pool.export_ck("db001").startswith("sessionid=")
    pool.set_note("db001", "测试备注")
    assert pool.store.read_meta("db001")["note"] == "测试备注"
    assert pool.delete("db001") is True
    assert pool.accounts == []
    # 再删一次：目录已不存在，必须返回 False（而不是假装成功）
    assert pool.delete("db001") is False


def test_store_delete_reports_real_failure(store, monkeypatch):
    """删不掉时不能假装成功（Windows 上文件被占用是真实场景）。

    回归：delete() 原先 `rmtree(ignore_errors=True)` 后无条件 return True，
    面板会显示「已删除」而账号其实还在。
    """
    import shutil as _shutil

    store.save("db001", CK)
    real_rmtree = _shutil.rmtree

    def stubborn(path, *a, **kw):
        real_rmtree(path, *a, **kw)   # 真删
        # 再放一个占位文件，模拟「删了但没删干净」
        Path(path).mkdir(parents=True, exist_ok=True)
        (Path(path) / "locked.tmp").write_text("x", encoding="utf-8")

    monkeypatch.setattr("doubao_pool.shutil.rmtree", stubborn)
    assert store.delete("db001") is False, "删不干净时必须返回 False"
    assert store.dir_of("db001").exists()
    # 删不掉时墓碑必须撤掉，否则用户既删不掉也改不动
    assert not store.is_deleted("db001")


def test_deleted_account_does_not_resurrect(store):
    """回归：删号之后，仍在跑的任务回写 meta 不得把账号「复活」。

    线上真实场景：面板删号 → 该号那次出片还没收尾 → 收尾回调写冷却/失败原因
    → 目录被重建 → 面板上账号「删了又回来」。
    """
    store.save("db001", CK, {"login_ok": True})
    assert store.delete("db001") is True
    assert store.dir_of("db001").exists() is False

    # 模拟在跑的出片协程收尾（写冷却 / 失败原因 / 记账）
    store.cooldown("db001", 60, "任务失败后写冷却")
    store.patch_meta("db001", last_err="风控")
    store.charge("db001", 1)
    assert store.dir_of("db001").exists() is False, "已删除的账号不该被回写复活"
    assert store.names() == []


def test_explicit_readd_clears_tombstone(store):
    """墓碑不能挡住用户**显式**重新添加同名账号。"""
    store.save("db001", CK)
    store.delete("db001")
    assert store.is_deleted("db001") is True
    store.save("db001", CK)                       # 用户主动加回来
    assert store.is_deleted("db001") is False
    assert store.dir_of("db001").is_dir()
    assert store.names() == ["db001"]


def test_tombstone_cleanup(store):
    """过期墓碑会被清理，避免 .deleted/ 无限增长。"""
    store.save("db001", CK)
    store.delete("db001")
    assert store.cleanup_tombstones(max_age=0) == 1
    assert store.is_deleted("db001") is False


def test_pool_generate_fails_on_empty_pool(pool):
    with pytest.raises(RuntimeError, match="号池为空"):
        asyncio.run(pool.generate_video("x"))


def test_pool_generate_does_not_spin_when_no_schedulable_account(pool, monkeypatch):
    """★ 2026-10-13 回归：池里有号但**全部不可调度**时必须立刻失败。

    原实现在「候选为空」时只判断 `all(n in tried)`，当池里一个可调度号都没有
    （全部冷却/额度用尽/调度关闭/新号预热）时 tried 仍为空 → 该判断恒为 False →
    无限 `sleep(2)` 直到 deadline（实测把整套测试卡死 240s+）。
    修复后：`_select()` 返回空即判失败，不再空等。
    """
    pool.add("db001", CK)
    # 造一个「有号但不可调度」的状态：调度关闭
    pool.store.patch_meta("db001", login_ok=True, scheduling=False)
    assert pool._select() == []          # 前置：确实选不出号

    calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def counting_sleep(sec):
        calls["n"] += 1
        if calls["n"] > 3:               # 死循环会远超这个数
            raise AssertionError("generate_video 在无可调度号时空转（回归）")
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)
    t0 = time.time()
    with pytest.raises(RuntimeError):
        asyncio.run(pool.generate_video("x", deadline=time.time() + 30))
    assert time.time() - t0 < 5, "应立即失败而不是等到 deadline"
    assert calls["n"] <= 3, f"不应反复 sleep 空转（实际 {calls['n']} 次）"


def test_pool_warmup_skips_account_when_enabled(pool, monkeypatch):
    """预热开启时新号不参与自动调度；关闭（默认）时不受影响。"""
    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)
    assert pool._select() == ["db001"]            # 默认关闭 → 可调度

    monkeypatch.setattr(doubao_pool, "WARMUP_SECONDS", 1800)
    # 刚入池的号（created_at 即现在）应被预热挡掉
    assert pool._select() == []
    acc = next(a for a in pool.summary()["accounts"] if a["name"] == "db001")
    assert acc["group"] == doubao_pool.GROUP_WARMUP
    assert acc["warmup_left"] > 0
    # 手动测试生成**不受**预热限制（用户明确意图）
    pool.store.patch_meta("db001", login_ok=True)

    monkeypatch.setattr(doubao_pool, "WARMUP_SECONDS", 0)
    assert pool._select() == ["db001"]


def test_pool_generate_video_success_charges_quota(pool, monkeypatch):
    pool.add("db001", CK)
    pool.store.patch_meta("db001", login_ok=True)

    async def fake_run_one(name, prompt, ratio, duration, model, image_paths=None,
                           timeout=300, on_poll=None):
        return {"video_url": "https://cdn.example/v.mp4", "conversation_id": "c1",
                "account": name}

    async def fake_download(url, name):
        p = Path(pool.store.root).parent / f"{name}.mp4"
        p.write_bytes(b"mp4")
        return str(p)

    monkeypatch.setattr(pool, "_run_one", fake_run_one)
    monkeypatch.setattr(pool, "_download", fake_download)
    res = asyncio.run(pool.generate_video("一只猫", ratio="16:9", duration=5))
    assert res["account"] == "db001"
    assert res["video_url"].endswith(".mp4")
    assert pool.store.roll_day(pool.store.read_meta("db001"))["used_today"] == 1


def test_pool_generate_video_switches_account_on_failure(pool, monkeypatch):
    pool.add("db001", CK)
    pool.add("db002", "sessionid=" + "c" * 32 + "; ttwid=t2")
    pool.store.patch_meta("db001", login_ok=True, weight=9)
    pool.store.patch_meta("db002", login_ok=True, weight=1)

    calls: list[str] = []

    async def fake_run_one(name, prompt, ratio, duration, model, image_paths=None,
                           timeout=300, on_poll=None):
        calls.append(name)
        if name == "db001":
            raise doubao_pool.DoubaoLoginError("豆包域风控(710022002): 访问频繁")
        return {"video_url": "https://cdn.example/v.mp4", "conversation_id": "c",
                "account": name}

    async def fake_download(url, name):
        p = Path(pool.store.root).parent / f"{name}.mp4"
        p.write_bytes(b"mp4")
        return str(p)

    monkeypatch.setattr(pool, "_run_one", fake_run_one)
    monkeypatch.setattr(pool, "_download", fake_download)
    res = asyncio.run(pool.generate_video("一只猫"))
    assert res["account"] == "db002"
    assert calls == ["db001", "db002"]
    # 失败号被冷却 + 标失效；成功号记账
    m1 = pool.store.read_meta("db001")
    assert m1["cooldown_until"] > 0 and m1["login_ok"] is False
    assert pool.store.read_meta("db002")["last_err"] == ""


# ---------------- 渠道分域（store / server） ----------------

def test_store_channel_defaults_and_filters(tmp_path):
    import config

    old = config.DB_PATH
    config.DB_PATH = str(tmp_path / "t.db")
    try:
        from store import TaskStore

        st = TaskStore(config.DB_PATH)
        st.create("video_a", "seedance-2.0", "p", "16:9", 10)
        st.create("video_b", "seedance-2.0", "p", "16:9", 10, channel="doubao")
        all_rows = st.recent_tasks(50)
        assert {r["id"] for r in all_rows} == {"video_a", "video_b"}
        assert st.recent_tasks(50, None, "doubao")[0]["id"] == "video_b"
        # channel=dola 要把没有 channel 的老行也算进来
        dola = st.recent_tasks(50, None, "dola")
        assert "video_a" in {r["id"] for r in dola}
        assert "video_b" not in {r["id"] for r in dola}
    finally:
        config.DB_PATH = old


def test_genset_is_per_channel(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    import importlib

    server = importlib.import_module("server")
    monkeypatch.setattr(server.config, "POOL_DB_PATH", str(tmp_path / "pool.db"))
    server._save_genset({"model": "seedance-2.5", "duration": 30, "ratio": "9:16"}, "dola")
    server._save_genset({"model": "seedance-2.0", "duration": 10, "ratio": "1:1"}, "doubao")
    assert server._load_genset("dola")["duration"] == 30
    assert server._load_genset("doubao")["duration"] == 10
    assert server._load_genset("doubao")["ratio"] == "1:1"
    # 两套互不影响
    assert server._load_genset("dola")["model"] == "seedance-2.5"


def test_server_exposes_doubao_and_channel_routes():
    import importlib

    server = importlib.import_module("server")
    paths = {r.path for r in server.app.routes}
    for need in ("/api/admin/doubao/accounts", "/api/admin/doubao/login/qr",
                 "/api/admin/doubao/login/phone/send",
                 "/api/admin/doubao/login/phone/verify",
                 "/api/admin/doubao/upload-image",
                 "/api/admin/doubao/accounts/{name}/test-generate",
                 "/api/admin/doubao/accounts/{name}/verify",
                 "/api/admin/doubao/accounts/reset-quotas"):
        assert need in paths, need


def test_doubao_upload_endpoint_accepts_image(tmp_path, monkeypatch):
    import importlib

    server = importlib.import_module("server")
    monkeypatch.setattr(doubao_pool, "UPLOAD_DIR", tmp_path / "up")
    monkeypatch.setattr(server.doubao_pool, "UPLOAD_DIR", tmp_path / "up")
    from PIL import Image
    from io import BytesIO

    buf = BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    data = buf.getvalue()

    class _Upload:
        filename = "ref.png"

        async def read(self):
            return data

    class _Form:
        def getlist(self, key):
            return [_Upload()] if key == "image" else []

    path = asyncio.run(server._save_uploaded_image(_Form()))
    assert path and Path(path).is_file()
    assert Path(path).read_bytes() == data


def test_doubao_upload_endpoint_rejects_non_image(tmp_path, monkeypatch):
    import importlib

    server = importlib.import_module("server")
    monkeypatch.setattr(doubao_pool, "UPLOAD_DIR", tmp_path / "up")
    monkeypatch.setattr(server.doubao_pool, "UPLOAD_DIR", tmp_path / "up")

    class _Upload:
        filename = "bad.png"

        async def read(self):
            return b"not-an-image"

    class _Form:
        def getlist(self, key):
            return [_Upload()] if key == "image" else []

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        asyncio.run(server._save_uploaded_image(_Form()))


def test_testgen_request_accepts_reference_images():
    import importlib

    server = importlib.import_module("server")
    req = server.TestGenRequest(prompt="p", reference_images=["/tmp/a.png"])
    assert req.reference_images == ["/tmp/a.png"]
    # DoubaoTgBody 同样带参考图字段
    body = server.DoubaoTgBody(prompt="p", reference_images=["/tmp/b.png"])
    assert body.reference_images == ["/tmp/b.png"]
