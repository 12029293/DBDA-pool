# -*- coding: utf-8 -*-
"""cookie_export：导出格式正确性 + 与 cookie_import 的可再导入往返。"""
import json

import cookie_export as ce
from cookie_import import parse_cookie_header_text, parse_cookie_json_text

STATE = {
    "source": "cookie",
    "cookies_list": [
        {"name": "sessionid", "value": "sid-abc123", "domain": ".dola.com", "path": "/"},
        {"name": "ttwid", "value": "1%7Cxx", "domain": ".dola.com", "path": "/"},
        {"name": "store-country-code", "value": "hk", "domain": ".dola.com", "path": "/"},
    ],
    "cookies": {
        "sessionid": {"value": "sid-abc123", "domain": ".dola.com", "path": "/"},
    },
}


def test_normalize_format():
    assert ce.normalize_format("JSON") == "json"
    assert ce.normalize_format("") == "json"
    assert ce.normalize_format("bogus") == "json"
    assert ce.DEFAULT_FORMAT == "json"
    assert set(ce.PANEL_IMPORTABLE) <= set(ce.FORMATS)


def test_cookies_of_prefers_list_then_dict():
    assert [c["name"] for c in ce.cookies_of(STATE)] == ["sessionid", "ttwid", "store-country-code"]
    # 只有 cookies 字典（老结构）时也能还原
    only_dict = {"cookies": {"a": {"value": "1", "domain": ".dola.com", "path": "/"}}}
    assert ce.cookies_of(only_dict) == [
        {"name": "a", "value": "1", "domain": ".dola.com", "path": "/"}]
    assert ce.cookies_of({}) == []
    assert ce.cookies_of(None) == []


def test_to_header_roundtrip_through_import():
    header = ce.to_header(ce.cookies_of(STATE))
    assert header == "sessionid=sid-abc123; ttwid=1%7Cxx; store-country-code=hk"
    states = parse_cookie_header_text(header)
    assert len(states) == 1
    assert states[0]["cookies"]["sessionid"]["value"] == "sid-abc123"


def test_editor_json_is_importable_and_keeps_all_cookies():
    raw = ce.to_editor_json(ce.cookies_of(STATE))
    items = json.loads(raw)          # 必须是合法 JSON 数组（扩展直接读）
    assert len(items) == 3
    assert items[0]["domain"] == ".dola.com" and items[0]["path"] == "/"
    states = parse_cookie_json_text(raw)
    assert len(states) == 1
    assert len(states[0]["cookies_list"]) == 3
    assert states[0]["cookies"]["ttwid"]["value"] == "1%7Cxx"


def test_netscape_shape():
    txt = ce.to_netscape(ce.cookies_of(STATE))
    lines = [l for l in txt.splitlines() if l and not l.startswith("#")]
    assert len(lines) == 3
    cols = lines[0].split("\t")
    assert cols == [".dola.com", "TRUE", "/", "TRUE", "0", "sessionid", "sid-abc123"]
    # 非点开头域名 → includeSubdomains=FALSE
    one = [{"name": "n", "value": "v", "domain": "dola.com", "path": "/"}]
    assert ce.to_netscape(one).splitlines()[-1].split("\t")[1] == "FALSE"


def test_bundle_roundtrip_through_import():
    raw = ce.to_bundle("acc1", ce.cookies_of(STATE), email="a@b.c", user_id="7693")
    obj = json.loads(raw)
    assert obj["cookie_count"] == 3 and obj["user_id"] == "7693"
    assert obj["cookie_header"].startswith("sessionid=")
    # cookie_import 认 cookies_list 包装 → 可直接走「Cookie JSON 文件」导入
    states = parse_cookie_json_text(raw)
    assert len(states) == 1 and len(states[0]["cookies_list"]) == 3
    # 关键：dola-pool 2.1.4 单账号弹窗读的是 cookies（数组），两者都要有
    assert isinstance(obj["cookies"], list) and len(obj["cookies"]) == 3
    assert obj["cookies"][0] == {
        "domain": ".dola.com", "name": "sessionid", "value": "sid-abc123", "path": "/",
        "secure": True, "httpOnly": False, "sameSite": "no_restriction", "session": True}


def test_bundle_carries_storage_for_214():
    st = dict(STATE, storage={"localStorage": {"k": "v"}, "sessionStorage": {}})
    raw = ce.to_bundle("acc1", ce.cookies_of(st), storage=st["storage"])
    assert json.loads(raw)["storage"] == st["storage"]


def test_two_json_formats_are_valid_json_objects_for_214():
    """2.1.4 弹窗/文件模式：顶层数组，或含 cookies 数组的对象；每条必须 name+value+domain。"""
    for fmt in ce.PANEL_IMPORTABLE:
        data = json.loads(ce.render(fmt, "acc1", ce.cookies_of(STATE)))
        items = data if isinstance(data, list) else data.get("cookies")
        assert isinstance(items, list) and len(items) == 3, fmt
        for c in items:
            assert c["name"] and c["value"] is not None
            assert "dola.com" in c["domain"]


def test_filename_and_mime():
    assert ce.filename_for("acc1", "header") == "acc1_cookie.txt"
    assert ce.filename_for("acc1", "json") == "acc1_cookie_editor.json"
    assert ce.filename_for("acc1", "netscape") == "acc1_cookies_netscape.txt"
    assert ce.filename_for("acc1", "bundle") == "acc1_ck_bundle.json"
    assert ce.mime_for("json").startswith("application/json")


def test_load_cookies_from_files(tmp_path):
    d = tmp_path / "acc9"
    d.mkdir()
    (d / "cookie_state.json").write_text(json.dumps(STATE), encoding="utf-8")
    cks = ce.load_cookies(tmp_path, "acc9")
    assert len(cks) == 3

    # 只有 cookie.txt 时回落解析原始 Cookie 头
    d2 = tmp_path / "acc10"
    d2.mkdir()
    (d2 / "cookie.txt").write_text("Cookie: a=1; b=2", encoding="utf-8")
    assert ce.load_cookies(tmp_path, "acc10") == [
        {"name": "a", "value": "1", "domain": ".dola.com", "path": "/"},
        {"name": "b", "value": "2", "domain": ".dola.com", "path": "/"},
    ]

    # 啥都没有 → None（浏览器登录号无 CK 可导）
    (tmp_path / "acc11").mkdir()
    assert ce.load_cookies(tmp_path, "acc11") is None
    assert ce.load_cookies(tmp_path, "nope") is None


def test_export_account_shape(tmp_path):
    d = tmp_path / "acc1"
    d.mkdir()
    (d / "cookie_state.json").write_text(json.dumps(STATE), encoding="utf-8")
    out = ce.export_account(tmp_path, "acc1", email="a@b.c")
    assert out["count"] == 3
    assert out["sessionid"] == "sid-abc1"
    assert set(out["formats"]) == set(ce.FORMATS)
    assert out["formats"]["header"].startswith("sessionid=")
    assert out["panel_importable"] == list(ce.PANEL_IMPORTABLE)
    assert out["panel_importable"][0] == ce.DEFAULT_FORMAT
    assert ce.export_account(tmp_path, "missing") is None
