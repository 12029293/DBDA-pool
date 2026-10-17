# -*- coding: utf-8 -*-
"""图片生成（Seedream）通道：比例/模型归一、SSE 提取、车道隔离。

覆盖本轮（2026-10-16）新增的三块：
  1. `doubao_image` 的纯函数（模型归一、比例吸附、SSE 取图）；
  2. 图片车道与视频车道的**任务隔离**（kind 过滤）——这是"图片和视频能同时跑"的根；
  3. `server` 的档位下发（图片无时长、比例含 3:2/2:3）+ 队列混渠道校验按车道判。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import doubao_image as DI  # noqa: E402


# ----------------------------------------------------------- 模型归一

@pytest.mark.parametrize("raw,want", [
    ("seedream-4.0", "Seedream 4.0"),
    ("seedream-4.5", "Seedream 4.5"),
    ("seedream-5.0-pro", "Seedream 5.0 Pro"),
    ("seedream-5.0-flash", "Seedream 5.0 Flash"),
    # 变体写法都要认得
    ("Seedream-5.0-Pro", "Seedream 5.0 Pro"),
    ("seedream_5_0_flash", "Seedream 5.0 Flash"),
    ("seedream-4.5", "Seedream 4.5"),
    ("", "Seedream 4.5"),          # 默认档
    ("unknown-model", "Seedream 4.5"),
])
def test_resolve_image_model(raw, want):
    assert DI.resolve_image_model(raw) == want


def test_resolve_image_model_pro_flash_not_confused():
    """★ 回归：pro/flash 都含 "5.0"，先判 "5.0" 会把档位吃掉。"""
    assert DI.resolve_image_model("seedream-5.0-flash") == "Seedream 5.0 Flash"
    assert DI.resolve_image_model("seedream-5.0-pro") == "Seedream 5.0 Pro"


# ----------------------------------------------------------- 比例

@pytest.mark.parametrize("raw,want", [
    ("16:9", "16:9"), ("2:3", "2:3"), ("3:2", "3:2"), ("", "1:1"),
    ("1824x1024", "16:9"),         # 像素 → 标准比例
    ("1024x1824", "9:16"),
    ("1024x1024", "1:1"),
    ("1024x1536", "2:3"),
    ("1536x1024", "3:2"),
    ("auto", "1:1"),
])
def test_normalize_image_ratio(raw, want):
    assert DI.normalize_image_ratio(raw) == want


@pytest.mark.parametrize("ratio,want_prefix", [
    ("16:9", "横屏"), ("9:16", "竖屏"), ("1:1", "方形"), ("2:3", "竖屏"),
])
def test_ratio_spec_text_orientation(ratio, want_prefix):
    """豆包只认正文里的方位词 —— 必须写对，否则比例不生效。"""
    assert DI.ratio_spec_text(ratio).startswith(want_prefix)


def test_ratio_spec_text_contains_ratio():
    assert "16:9" in DI.ratio_spec_text("16:9")


# ----------------------------------------------------------- SSE 取图

def _msg_event(content: dict, content_type: int):
    return ("STREAM_CHUNK", {"content": content, "content_type": content_type})


def test_collect_images_from_content_type_2010():
    raw = "https://p1.example.com/a.jpeg"
    ev = _msg_event({"data": [{"image_ori_raw": {"url": raw}}]}, 2010)
    assert DI.collect_images([ev]) == [raw]


def test_collect_images_from_creation_block():
    raw = "https://p1.example.com/b.png"
    ev = _msg_event({"creations": [{"image": {"url": raw}}]}, 2074)
    assert DI.collect_images([ev]) == [raw]


def test_collect_images_prefers_ori_raw_over_watermarked_variant():
    """★ 变体优先级：image_ori_raw（干净原图）必须胜过缩略图。"""
    clean = "https://p1.example.com/clean.jpeg"
    thumb = "https://p1.example.com/thumb.jpeg~tplv-x-cthumb_wm1.png"
    ev = _msg_event({"data": [{
        "image_thumb": {"url": thumb},
        "image_ori_raw": {"url": clean},
    }]}, 2010)
    got = DI.collect_images([ev])
    assert got[0] == clean, got


def test_collect_images_from_patch_stream_nested():
    """补丁流：creations 嵌在任意层也要能挖出来。"""
    raw = "https://p1.example.com/nested.jpeg"
    ev = ("STREAM_CHUNK", {"patch_op": {"patch_value": [
        {"creation_block": {"creations": [{"image": {"image_ori_raw": {"url": raw}}}]}}]}})
    assert DI.collect_images([ev]) == [raw]


def test_collect_images_dedupes_across_events():
    raw = "https://p1.example.com/same.jpeg"
    ev = _msg_event({"data": [{"image_ori_raw": {"url": raw}}]}, 2010)
    assert DI.collect_images([ev, ev]) == [raw]


def test_collect_images_ignores_non_image_urls():
    ev = _msg_event({"text": "see https://example.com/page.html"}, 2010)
    assert DI.collect_images([ev]) == []


def test_clean_image_url_maps_dld_watermark_to_preview():
    """下载版带水印 → 预览版（清掉水印痕迹）。"""
    u = "https://x/a.jpeg~tplv-image_dld_watermark.png"
    assert "image_pre_watermark" in DI.clean_image_url(u)


# ----------------------------------------------------------- 请求体

def _client():
    return DI.DoubaoImageClient("sessionid=" + "a" * 40)


def test_build_image_body_uses_image_content_type_and_skill():
    """★ 协议核心：文生图 = content_type 2009 + skill 3（不是 ability_type）。"""
    cli = _client()
    body = cli._build_image_body("一只猫", "16:9", "seedream-4.5")
    m = body["messages"][0]
    assert m["content_type"] == 2009
    assert m["skill"]["skill_type"] == 3
    assert m["skill"]["skill_id"] == "3"
    # content 是二次序列化的 JSON 字符串
    inner = json.loads(m["content"])
    assert inner["model"] == "Seedream 4.5"
    assert inner["ratio"] == "16:9"
    assert inner["use_creation"] is False
    # 比例必须写进正文（结构化字段只是双保险）
    assert "横屏" in inner["text"] and "16:9" in inner["text"]


def test_build_image_body_reuses_video_envelope():
    """★★ 2026-10-16 真号实测结论：图片请求必须**沿用视频那份完整信封**。

    参考实现（open-ai-canvas）的精简 ``completion_option`` 在本站点会被判
    ``710020202 common invalid param``；改用更短的 option 又是 ``710020702``。
    只有"视频信封 + 图片消息 + 去掉 chat_ability"能过校验。
    这条测试把"别再改回精简信封"钉住。
    """
    cli = _client()
    body = cli._build_image_body("一只猫", "1:1", "seedream-4.5")
    vid = cli._build_video_body("一只猫", "1:1", 5, "seedance_v2.0")

    # 每次请求都会变的字段（随机 uuid / 时间戳）不参与结构比对
    _volatile = {"unique_key", "create_time_ms", "local_conversation_id",
                 "conversation_id", "req_create_time_sec"}

    def _stable(d):
        return {k: v for k, v in d.items() if k not in _volatile}

    assert _stable(body["option"]) == _stable(vid["option"])
    assert body["ext"] == vid["ext"]
    assert _stable(body["client_meta"]) == _stable(vid["client_meta"])
    assert "evaluate_option" not in body, "信封以视频为准，不额外注入精简版字段"
    assert "completion_option" not in body, "本站点用 option 而非 completion_option"
    # 图片语义不带视频能力声明
    assert "chat_ability" not in body


def test_build_image_body_option_passes_upstream_validation():
    """信封里必须带上**上游校验需要**的那几个键。

    实测：把 option 换成精简版（只有 13 个键）就 710020702。
    """
    cli = _client()
    opt = cli._build_image_body("x", "1:1", "seedream-4.5")["option"]
    for k in ("agent_mode", "conversation_init_ext", "aggregate_params",
              "support_lazy_fetch_stream", "conversation_mode", "model_config"):
        assert k in opt, f"option 缺 {k}（精简信封会被上游拒）"


def test_image_and_video_bodies_have_identical_envelope_shape():
    """★ 真机验证的等价性断言（2026-10-16，db001/db003）：

    同一账号同一时刻分别发**视频**请求体与**图片**请求体，上游返回**完全相同**的
    ``710022004 rate limited``。说明图片请求体在"形状校验"这一层与已验证可用的
    视频通道**等价** —— 剩下的限流是账号/IP 级，与请求体无关。

    这条测试把"顶层键集合必须一致"钉死：多一个少一个都会退回
    ``710020202 common invalid param``。
    """
    cli = _client()
    img = cli._build_image_body("一只猫", "1:1", "seedream-4.5")
    vid = cli._build_video_body("一只猫", "1:1", 5, "seedance_v2.0")
    # 信封（除 chat_ability 外）顶层键逐一对应
    assert set(img.keys()) == set(vid.keys()) - {"chat_ability"}, (
        f"图片信封只能比视频少 chat_ability；"
        f"多={set(img) - (set(vid) - {'chat_ability'})} "
        f"少={(set(vid) - {'chat_ability'}) - set(img)}")
    # 能力声明的差异正是"图 vs 视频"的分水岭：
    #   视频 = chat_ability.ability_type=17 + 消息用 content_block
    #   图片 = 无 chat_ability + 消息用 content/content_type=2009 + skill
    assert vid["chat_ability"]["ability_type"] == 17
    assert "chat_ability" not in img
    assert "content_block" in vid["messages"][0]
    assert img["messages"][0]["content_type"] == 2009
    assert img["messages"][0]["skill"]["skill_id"] == "3"


def test_build_image_body_has_no_duration():
    """★ 图片没有时长概念：请求体里不能出现 duration。"""
    body = _client()._build_image_body("一只猫", "1:1", "seedream-4.5")
    assert "duration" not in body
    assert "duration" not in json.loads(body["messages"][0]["content"])


def test_image_client_reuses_video_request_plumbing():
    """复用视频通道的 headers/params（同域同 aid），避免两套签名口径。"""
    cli = _client()
    h = cli._headers()
    assert "Cookie" in h and h["agw-js-conv"] == "str, str"
    p = cli._params()
    assert p["aid"] and p["pc_version"]


@pytest.mark.parametrize("model", DI.IMAGE_MODELS)
def test_every_image_model_builds(model):
    body = _client()._build_image_body("x", "1:1", model)
    assert json.loads(body["messages"][0]["content"])["model"]


def test_image_rejected_error_is_distinct_from_transient():
    """★ 提示词级拒绝必须是**独立类型**。

    否则池层只能靠消息里是否含"风控/拒绝"来判断，而该类型的提示文案
    本身就带这两个词 → 字符串匹配恒真 → 会把可重试的空响应也判成不可重试。
    """
    assert issubclass(DI.DoubaoImageRejectedError, Exception)
    assert not issubclass(DI.DoubaoImageRejectedError, DI.DoubaoThrottleError)
    assert not issubclass(DI.DoubaoImageRejectedError, DI.DoubaoCreditError)
    # 独立于登录/限流/额度，池层才能单独分支
    for other in (DI.DoubaoLoginError, DI.DoubaoThrottleError, DI.DoubaoCreditError):
        assert not issubclass(other, DI.DoubaoImageRejectedError)


def test_rejected_message_contains_hint_words_but_type_is_authoritative():
    """把"为什么必须按类型判"钉住：文案里确实带"风控/拒绝"。"""
    err = DI.DoubaoImageRejectedError("豆包拒绝生成或未返回图片：内容不适宜"
                                     "（若提示词含版权角色/真人等敏感内容会被上游风控拒绝）")
    text = str(err)
    assert "风控" in text and "拒绝" in text, \
        "文案带这些词正是不能靠关键词判定、必须靠类型判定的原因"


def test_pool_helper_recognises_rejected_type():
    import doubao_pool as DP
    assert DP._is_image_rejected(DI.DoubaoImageRejectedError("x")) is True
    assert DP._is_image_rejected(Exception("未返回图片（空响应）")) is False


def test_engine_rejected_helper_returns_real_type():
    rp = _task_mod()
    assert rp._image_rejected_error() is DI.DoubaoImageRejectedError


# ----------------------------------------------------------- 车道隔离

def _task_mod():
    import run_pool
    return run_pool


def test_task_kind_defaults_to_video():
    """★ 老队列/手编 tasks.txt 不写 kind → 必须仍按视频跑（行为不变）。"""
    rp = _task_mod()
    t = rp.Task("t001", "p", meta={})
    assert t.kind == "video"
    t2 = rp.Task("t002", "p", meta={"kind": "bogus"})
    assert t2.kind == "video"


def test_task_kind_reads_image():
    rp = _task_mod()
    assert rp.Task("t003", "p", meta={"kind": "image"}).kind == "image"


def test_take_next_filters_by_kind():
    """★ 这是"图片与视频并行"的根：两条车道各取各的，互不抢任务。"""
    rp = _task_mod()
    video = rp.Task("t001", "v", meta={"channel": "doubao", "kind": "video"})
    image = rp.Task("t002", "i", meta={"channel": "doubao", "kind": "image"})
    q = rp.TaskQueue([video, image])

    got = q.take_next(channel="doubao", kind="image")
    assert got is image, "图片车道必须只拿到图片任务"
    got2 = q.take_next(channel="doubao", kind="video")
    assert got2 is video, "视频车道必须只拿到视频任务"
    assert q.take_next(channel="doubao", kind="image") is None


def test_take_next_without_kind_still_returns_anything():
    """不传 kind 的旧调用点行为不变（向后兼容）。"""
    rp = _task_mod()
    t = rp.Task("t001", "p", meta={"channel": "dola", "kind": "image"})
    q = rp.TaskQueue([t])
    assert q.take_next(channel="dola") is t


def test_doubao_lane_pickers_are_disjoint():
    """视频取任务 / 图片取任务 两个 picker 必须互斥。"""
    rp = _task_mod()
    v = rp.Task("t001", "v", meta={"channel": "doubao", "kind": "video"})
    i = rp.Task("t002", "i", meta={"channel": "doubao", "kind": "image"})
    rp.tasks_q = rp.TaskQueue([v, i])
    got_img = rp._next_doubao_image_task()
    assert got_img is i
    got_vid = rp._next_doubao_task()
    assert got_vid is v
    rp.tasks_q = None


def test_imagedir_is_separate_from_mp4dir():
    """图片产物目录必须与视频分开（否则视频库会把图片扫进去）。"""
    rp = _task_mod()
    assert rp.IMGDIR != rp.MP4DIR
    assert rp.IMGDIR.endswith("img")


# ----------------------------------------------------------- 服务端档位

def _server():
    import server
    return server


def test_image_capabilities_have_no_durations_and_3_2_ratios():
    """★ 图片档位：没有时长；比例含 3:2 / 2:3。"""
    srv = _server()
    models = srv._GENSET_IMAGE_MODELS
    ratios = srv._GENSET_RATIOS
    assert set(models) == {"seedream-4.0", "seedream-4.5",
                           "seedream-5.0-pro", "seedream-5.0-flash"}
    assert "2:3" in ratios and "3:2" in ratios


def test_video_ratios_exclude_3_2_family():
    """视频上游对 3:2 系不生效 → 视频档位必须不含它（避免"选了不生效"）。

    证据：网页版真实配置 `extensions/dola30/doubao-skill-pack-response.json`
    的 ratio.options 只有 1:1/3:4/4:3/9:16/16:9/21:9。
    """
    srv = _server()
    assert "3:2" not in srv._GENSET_VIDEO_RATIOS
    assert "2:3" not in srv._GENSET_VIDEO_RATIOS
    assert set(srv._GENSET_VIDEO_RATIOS) == {"21:9", "16:9", "4:3", "1:1",
                                             "3:4", "9:16"}


def test_skill_pack_fixture_confirms_video_ratio_set():
    """把"视频只有 6 档"钉在真实夹具上，避免后人凭记忆改回全集。"""
    pack = json.loads((ROOT / "extensions" / "dola30" /
                       "doubao-skill-pack-response.json").read_text(encoding="utf-8"))
    opts = pack["data"]["video_generation"]["meta"]["option_list"]
    ratio_opt = next(o for o in opts if o["value"] == "ratio")
    got = {o["value"] for o in ratio_opt["options"]}
    assert got == {"1:1", "3:4", "4:3", "9:16", "16:9", "21:9"}
    assert "3:2" not in got and "2:3" not in got


def test_image_ratios_cover_reference_implementation_table():
    """图片比例要覆盖参考实现的吸附表（含 3:2/2:3/4:5/5:4）。"""
    srv = _server()
    for r in ("1:1", "16:9", "9:16", "4:3", "3:4",
              "3:2", "2:3", "21:9", "4:5", "5:4"):
        assert r in DI.IMAGE_RATIOS, r
        assert r in srv._GENSET_RATIOS, r


def test_ratios_superset_of_video():
    srv = _server()
    assert set(srv._GENSET_VIDEO_RATIOS) <= set(srv._GENSET_RATIOS)


def test_genset_doubao_ratios_now_include_new_ones():
    """「生成设置」页的豆包比例下拉也要拿到新档位（前端是硬编码列表）。"""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "'3:2'" in html and "'2:3'" in html


# ----------------------------------------------------------- 前端

def test_frontend_hides_duration_in_image_mode():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "isImage?'none':''" in html, "图片模式必须隐藏时长胶囊"
    assert "studioMode==='image'?null:studioDuration" in html, \
        "图片提交必须不带时长"


def test_frontend_renders_images_not_only_video():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "image_urls" in html and "gi-grid" in html
    assert "studioImagePreview" in html and "downloadImage" in html
