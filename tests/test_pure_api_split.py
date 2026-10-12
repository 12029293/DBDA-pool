"""30 秒出片流程单测：用假 dola 模块驱动判据，不依赖真实上游。
覆盖：上游称只支持 4~15 秒时**不再协商拆段**；一次成片直接返回；上游自己出两段时拼接；
直接失败/上游不回话时不空耗；只有真的短于目标时长才走补段。
"""
from __future__ import annotations

import time
import types

import pytest

CAP_TEXT = "视频生成目前支持 4 到 15 秒。我将按最接近的支持时长 15 秒生成。"


def test_duration_split_detector_covers_new_upstream_wording():
    """上游 2026-09-22 换了话术（要求确认 / 提示可回「拆成两段」），必须还能认出来。

    认不出来的后果：30 秒任务既不判 duration_split，也不触发两段兜底，
    一直空等到 NO_ACK_SECONDS 才换号（表现为「任务长时间卡在生成中」）。
    """
    from protocol import dola_pure_api as dola

    # CAP_TEXT 属于「上游自己压到 15 秒」那类，只要求 capped 判据命中（原有契约）
    assert dola.looks_like_duration_capped(CAP_TEXT) is True

    confirm_positives = [
        "确认后我会直接生成；如果你想拆成多段，也可以回复：“拆成两段”。",
        "当前提供的分镜时长为30秒，超出单条视频最大支持的15秒。",
        "需要你确认一个点：你写的是 30 秒内容，但当前单条视频支持 4–15 秒。",
    ]
    for text in confirm_positives:
        assert dola.looks_like_duration_confirm(text) is True, text
        assert dola.looks_like_duration_capped(text) is True, text

    negatives = [
        "可以拆成2段生成。硬性要求：两段必须按时间顺序首尾相接",      # 我们自己发的回复
        "两段已经齐了。硬性要求：请按时间顺序把两段首尾相接",           # 合并指令
        "本次使用 Dreamina Seedance 2.5 生成，将消耗 2 个视频生成额度",  # 正常受理回执
    ]
    for text in negatives:
        assert dola.looks_like_duration_confirm(text) is False, text


class FakePoll:
    def __init__(self, status="pending", vids=None, urls=None, texts=None):
        self.status = status
        self.vids = list(vids or [])
        self.urls = list(urls or [])
        self.texts = list(texts or [])
        self.failure_reasons: list[str] = []
        self.creation_statuses: list[str] = []
        self.wait_minutes = ""
        self.source_data = None


class FakeDola:
    def __init__(self, polls):
        self._polls = list(polls)
        self.sent: list[str] = []

    def inspect_video_once(self, *_a, **_k):
        return self._polls.pop(0) if len(self._polls) > 1 else self._polls[0]

    def poll_has_duration_split(self, poll, job_seconds=None):
        return any("4 到 15 秒" in text for text in poll.texts)

    def submit_chat_completion(self, *_a, **_k):
        self.sent.append(str(_k.get("prompt") or ""))
        return types.SimpleNamespace(error="", section_id="section-1")

    def fetch_single_chain(self, *_a, **_k):
        return {}

    @staticmethod
    def find_latest_message_index(_chain):
        return None

    @staticmethod
    def find_latest_section_id(_chain):
        return ""


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    import pure_api_gen

    monkeypatch.setattr(pure_api_gen.time, "sleep", lambda _seconds: None)


def _run(dola, duration=30):
    import pure_api_gen

    return pure_api_gen._wait_video_same_conversation(
        dola=dola, session=None, ctx=None, headers=None,
        conversation_id="conv-1", account="acc1", duration=duration,
        deadline=time.time() + 60, poll_interval=1, on_poll=None,
    )


def test_duration_capped_hint_does_not_trigger_negotiation():
    """上游回「只支持 4~15 秒」时不能再回「拆成两段」——第二段会被按 6 点报价后拒绝。"""
    dola = FakeDola([
        FakePoll(texts=[CAP_TEXT]),
        FakePoll(status="succeeded", vids=["v1"], texts=[CAP_TEXT]),
    ])
    poll, segments = _run(dola)

    assert segments == []
    assert poll.status == "succeeded"
    assert dola.sent == []


def test_upstream_own_two_clips_are_merged():
    """上游自己在一条会话里出了两段时，仍然交给调用方拼接。"""
    dola = FakeDola([
        FakePoll(texts=[CAP_TEXT]),
        FakePoll(vids=["v1", "v2"], texts=[CAP_TEXT]),
    ])
    poll, segments = _run(dola)

    assert segments == ["vid:v1", "vid:v2"]
    assert dola.sent == []


def test_no_upstream_ack_gives_up_early(monkeypatch):
    """提交成功但上游连额度提示都不回：到阈值判【异常】（2.1.0 P5 起不再只是放手返回）。

    账号由调用方（browser_pool）放进异常组，任务提示「生视频过程中出现异常情况，请重试」。
    """
    import pure_api_gen

    monkeypatch.setattr(pure_api_gen, "NO_ACK_SECONDS", 0)
    dola = FakeDola([FakePoll(status="pending", texts=["生成视频：一只橘猫在窗台上打盹"])])

    with pytest.raises(pure_api_gen.AbnormalNoAckError) as exc:
        _run(dola)

    assert "请重试" in str(exc.value)


def test_single_shot_30s_returns_without_negotiation():
    dola = FakeDola([FakePoll(status="succeeded", vids=["v9"])])
    poll, segments = _run(dola)

    assert segments == []
    assert poll.status == "succeeded"
    assert dola.sent == []


def test_hard_failure_does_not_start_split():
    dola = FakeDola([
        FakePoll(status="rate_limited", texts=[CAP_TEXT, "今日剩余 2 个视频生成额度，无法生成该视频"]),
    ])
    poll, segments = _run(dola)

    assert segments == []
    assert poll.status == "rate_limited"
    assert dola.sent == []


def test_short_duration_never_splits():
    dola = FakeDola([FakePoll(texts=[CAP_TEXT])])
    poll, segments = _run(dola, duration=10)

    assert segments == []
    assert dola.sent == []


def test_same_clip_multiple_renditions_are_one_segment():
    """同一支片的 1080p/720p 是两条 url 但只有一个 vid：不能当成两段去拼接。"""
    import pure_api_gen

    poll = FakePoll(vids=["v1"], urls=["http://cdn/1080.mp4", "http://cdn/720.mp4"], texts=[CAP_TEXT])

    assert pure_api_gen._media_keys(poll) == ["vid:v1"]


def test_native_30s_clip_returns_without_split():
    """上游直接给 30.042 秒成片时，跳过拆段协商（此前会被拼成 60 秒）。"""
    import pure_api_gen

    poll = FakePoll(status="duration_split", vids=["v1"], urls=["http://cdn/1080.mp4", "http://cdn/720.mp4"], texts=[CAP_TEXT])
    poll.source_data = {"video_info": [{"video_id": "v1", "video_duration": 30.042}]}
    dola = FakeDola([poll])
    dola.find_video_info_objects = lambda data: data.get("video_info") or []

    result_poll, segments = _run(dola)

    assert segments == []
    assert dola.sent == []
    assert result_poll is poll


def test_clip_seconds_reads_video_duration():
    import pure_api_gen

    class _Poll:
        source_data = {"video_info": [{"video_duration": 15.04}]}

    dola = types.SimpleNamespace(find_video_info_objects=lambda data: data.get("video_info") or [])

    assert pure_api_gen._clip_seconds(dola, _Poll) == 15.04


def test_credit_tracker_reads_used_and_left_without_double_counting():
    """上游回执「将消耗 2 个…今日剩余 2 个」：链路每条轮询整段返回，不能重复累加。"""
    import pure_api_gen

    seen: list[tuple[int, str]] = []
    tracker = pure_api_gen._CreditTracker(lambda bal, src="": seen.append((bal, src)))
    texts = [
        "生成视频：一只橘猫",
        "本次使用 **Dreamina Seedance 2.5** 生成，将消耗 2 个视频生成额度，预计等待 15 分钟。视频生成好后，我会主动发送给你，今日剩余 2 个视频生成额度。",
    ]

    tracker.note(texts)
    tracker.note(texts)  # 同一段链路再来一次

    assert tracker.charged == 2
    assert tracker.left == 2
    assert seen == [(2, "upstream")]
    assert tracker.fields() == {"credits_used": 2, "credits_left": 2}


def test_credit_tracker_absorb_sums_parts():
    import pure_api_gen

    main = pure_api_gen._CreditTracker()
    main.note(["本次使用 Dreamina Seedance 2.5 生成，将消耗 2 个视频生成额度，今日剩余 2 个视频生成额度。"])
    part2 = pure_api_gen._CreditTracker()
    part2.note(["将消耗 2 个视频生成额度，今日剩余 0 个视频生成额度。"])
    main.absorb(part2)

    assert main.charged == 4
    assert main.left == 0


def test_clip_is_short_detects_half_length():
    """30 秒请求只拿到 15 秒 → 视为短视频（默认不拼接，换号重试）。"""
    import pure_api_gen

    assert pure_api_gen.clip_is_short(15.0, 30) is True
    assert pure_api_gen.clip_is_short(10.0, 10) is False
    assert pure_api_gen.clip_is_short(30.09, 30) is False
    assert pure_api_gen.clip_is_short(None, 30) is False


def test_short_clip_error_is_pure_api_error():
    """ShortClipError 必须能被 PureApiError 捕获（调用方按可重试失败处理）。"""
    import pure_api_gen

    assert issubclass(pure_api_gen.ShortClipError, pure_api_gen.PureApiError)


def test_duration_declaration_counts_as_ack(monkeypatch):
    """上游只回「支持 4 到 15 秒」也算回执，不能再被判「上游没有任何回执」。

    2026-10-06 线上：上游对 30 秒改成直接声明时长范围、不再带额度播报，
    旧口径只认「视频生成额度」→ 明明有回执却报「提交后 900 秒内上游没有任何回执」，
    把本来健康的号丢进【异常】组（acc10 / acc11 实测）。
    """
    import pure_api_gen

    monkeypatch.setattr(pure_api_gen, "NO_ACK_SECONDS", 0)
    monkeypatch.setattr(pure_api_gen, "DURATION_CAP_GRACE_SECONDS", 0)
    dola = FakeDola([FakePoll(status="pending", texts=[CAP_TEXT])])

    poll, segments = _run(dola)

    assert segments == []
    assert poll.status == "pending"
    assert dola.sent == []


def test_duration_declaration_without_clip_returns_after_grace(monkeypatch):
    """声明只支持更短时长后再无成片：最多等宽限期就交回上层，不再空等到 900 / 1800 秒。

    上游回完这句话常常再无下文（新号批量测试实测），旧逻辑每个号白等 30 分钟，
    期间账号被锁住、任务最后报「时长协商」失败。
    """
    import pure_api_gen

    monkeypatch.setattr(pure_api_gen, "DURATION_CAP_GRACE_SECONDS", 0)
    dola = FakeDola([FakePoll(status="pending", texts=[CAP_TEXT])])

    poll, segments = _run(dola)

    assert segments == []
    assert poll.status == "pending"
    assert dola.sent == []


def test_pair_needs_two_parts_of_credits():
    """两段拼接前先看上游剩余额度：只剩 2 点要 4 点时直接判本号做不出，别白烧第一段。

    必须抛 ShortClipError（而不是普通 PureApiError）：browser_pool 对这一类只换号、
    不给账号记失败。新号普遍只剩 2 点，两段要 4 点 —— 旧代码抛 PureApiError，
    面板把「上游额度不够」记成这个号的失败，连续 3 次误进【异常】组。
    """
    import pure_api_gen

    tracker = pure_api_gen._CreditTracker()
    tracker.note(["今日剩余 2 个视频生成额度"])

    with pytest.raises(pure_api_gen.ShortClipError) as exc:
        pure_api_gen._generate_capped_pair(
            dola=FakeDola([]), session=None, ctx=None, headers=None, account="acc1",
            prompt="x", ratio="16:9", model="seedance_v2.5", output_path="out.mp4",
            remove_watermark=False, proxy="", timeout=60, poll_interval=1,
            credits=tracker,
        )

    assert "额度" in str(exc.value)


# ===== 2026-10-06 线上话术全集回归（tasks.db / pool_usage.db 原文逐条抄录）=====
# 这批文本是「新账号批量测试全部卡死」的原始证据：上游对 30 秒请求不再追问、
# 而是直接声明按更短时长生成；旧判据认不出 → 既不兜底也不早失败，白等到 900/1800 秒。
LIVE_DURATION_DECLARATIONS = [
    "视频生成当前支持4到15秒，我将按默认5秒生成。",
    "视频生成当前支持 4 到 15 秒，我将按默认 5 秒生成。",
    "视频时长超出支持范围，当前支持 4–15 秒，我将按最接近的 15 秒生成。",
    "视频生成目前支持 4 到 15 秒的时长。我将按最接近的支持时长 15 秒为你生成。",
    "视频生成当前支持4到15秒。我将按最近的支持时长15秒为你生成。",
    "当前视频生成支持最长 15 秒，已按 15 秒生成。",
]

# 「本次使用 Seedance 2.5 生成，将消耗 2 个视频生成额度，预计等待 2 小时……」——
# 注意这只是把**被压短的 15 秒那一条**受理了的播报，不等于 30 秒请求被满足。
LIVE_ACCEPT_BROADCAST = (
    "本次使用 **Dreamina Seedance 2.5** 生成，将消耗 2 个视频生成额度，预计等待 2 小时。"
    "视频生成好后，我会主动发送给你，今日剩余 0 个视频生成额度。"
)


@pytest.mark.parametrize("text", LIVE_DURATION_DECLARATIONS)
def test_live_duration_declarations_are_all_recognized(text):
    """六条线上原文都必须同时被 confirm / declared_shorter / capped 认出来。"""
    from protocol import dola_pure_api as dola

    assert dola.looks_like_duration_confirm(text) is True, text
    assert dola.looks_like_duration_declared_shorter(text) is True, text
    assert dola.looks_like_duration_capped(text) is True, text
    assert dola.tag_chain_text(text) == "duration_split", text


@pytest.mark.parametrize("text", LIVE_DURATION_DECLARATIONS)
def test_declaration_wins_over_accept_broadcast(text):
    """时长声明 + 受理播报同时出现时，链路结论必须是 duration_split。

    旧口径 `if split and not accepted` 会被受理播报挡回 pending（线上 acc6 实测），
    30 秒任务于是既不触发两段兜底也不提前失败。
    """
    from protocol import dola_pure_api as dola

    status, _ = dola.classify_chain_outcome(
        [], [text, LIVE_ACCEPT_BROADCAST, "生成视频：汉服美女在赛博朋克的街道跑酷，16:9"], [])
    assert status == dola.DURATION_SPLIT_STATUS, text


def test_accept_broadcast_alone_is_not_a_duration_split():
    """反例：只有正常受理播报（真按目标时长开工）时不能判成时长协商。"""
    from protocol import dola_pure_api as dola

    status, _ = dola.classify_chain_outcome([], [LIVE_ACCEPT_BROADCAST], [])
    assert status == "pending"


def test_daily_quota_still_outranks_duration_declaration():
    """反例：账号当日额度用完时，结论必须是额度类，别被时长声明盖掉。"""
    from protocol import dola_pure_api as dola

    status, _ = dola.classify_chain_outcome(
        [], ["当前视频生成支持最长 15 秒，已按 15 秒生成。", LIVE_ACCEPT_BROADCAST,
             "今天的生成次数已经达到上限，明天再来免费生成吧"], [])
    assert status == "quota_exceeded"


def test_longest_15s_declaration_returns_after_grace(monkeypatch):
    """「支持最长 15 秒，已按 15 秒生成」+ 受理播报：宽限期到点就交回上层走两段兜底。"""
    import pure_api_gen

    monkeypatch.setattr(pure_api_gen, "DURATION_CAP_GRACE_SECONDS", 0)
    dola = FakeDola([FakePoll(status="pending", texts=[
        "当前视频生成支持最长 15 秒，已按 15 秒生成。", LIVE_ACCEPT_BROADCAST])])

    poll, segments = _run(dola)

    assert segments == []
    assert poll.status == "pending"
    assert dola.sent == []
