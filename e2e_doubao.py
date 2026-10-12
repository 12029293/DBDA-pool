# -*- coding: utf-8 -*-
"""豆包域端到端出片 · 带进度诊断

用法：
    python e2e_doubao.py <ck文件> [ratio] [duration] [model] [poll_timeout]

每步实时 flush，卡在哪一步一眼可见。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"C:/Users/Administrator/Desktop/dola-pool")

import aiohttp  # noqa: E402
from doubao_cn_video import (  # noqa: E402
    DoubaoVideoClient, DoubaoLoginError, DoubaoCreditError, CN_DOMAIN,
)

CK_FILE = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:/Users/Administrator/Desktop/扫码测试/扫码CK.txt"
RATIO = sys.argv[2] if len(sys.argv) > 2 else "9:16"
DURATION = int(sys.argv[3]) if len(sys.argv) > 3 else 5
MODEL = sys.argv[4] if len(sys.argv) > 4 else "seedance_v2.0"
POLL_TIMEOUT = int(sys.argv[5]) if len(sys.argv) > 5 else 300

_t0 = time.time()


def log(*a):
    print(f"[{time.time() - _t0:7.1f}s]", *a, flush=True)


async def main():
    ck = Path(CK_FILE).read_text(encoding="utf-8").strip()
    log("CK 文件:", CK_FILE)
    cli = DoubaoVideoClient(ck)
    log("域:", cli.domain, "| sessionid:", cli.sessionid[:16], "| is_valid:", cli.is_valid)

    log("STEP1 check_login ...")
    ok = await cli.check_login()
    log("      check_login =", ok)

    log("STEP2 提交出片 POST /chat/completion （裸参数）...")
    body = cli._build_video_body(f"一只白色小猫在草地上追蝴蝶", RATIO, DURATION, MODEL)
    body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    headers = cli._headers(local_conv_id=cli._local_conv_id)
    url = f"https://{CN_DOMAIN}/chat/completion"
    conv_id, ev_names = "", []
    try:
        async with aiohttp.ClientSession(**cli._sess_kw()) as s:
            async with s.post(url, params=cli._params(), json=body, headers=headers,
                              timeout=aiohttp.ClientTimeout(total=90)) as resp:
                log("      HTTP", resp.status)
                events = await cli._read_sse(resp)
    except Exception as e:
        log("      提交异常:", repr(e))
        return
    for name, data in events:
        ev_names.append(name)
        if name == "SSE_ACK":
            ack = data.get("ack_client_meta") or {}
            conv_id = ack.get("conversation_id", "")
            cli._conv_type = ack.get("conversation_type", 3)
        if name == "STREAM_MSG_NOTIFY":
            try:
                blocks = ((data.get("content") or {}).get("content_block") or [])
                for b in blocks:
                    t = ((b.get("content") or {}).get("text_block") or {}).get("text")
                    if t:
                        log("      服务端文案:", t[:70])
            except Exception:
                pass
        if name == "STREAM_ERROR":
            log("      STREAM_ERROR:", json.dumps(data, ensure_ascii=False)[:200])
    log("      事件序列:", ev_names[:14], "... 共", len(ev_names))
    log("      conversation_id =", conv_id or "(空)")
    if not conv_id or conv_id == "0":
        log("❌ 会话未建立，终止")
        return

    log(f"STEP3 轮询 /im/chain/single （uplink_body 包装，上限 {POLL_TIMEOUT}s）...")
    start = time.time()
    n = 0
    async with aiohttp.ClientSession(**cli._sess_kw()) as s:
        while time.time() - start < POLL_TIMEOUT:
            await asyncio.sleep(5)
            n += 1
            pbody = {
                "cmd": 3100,
                "uplink_body": {"pull_singe_chain_uplink_body": {
                    "conversation_id": conv_id,
                    "anchor_index": 9007199254740991,
                    "conversation_type": cli._conv_type,
                    "direction": 1, "limit": 20, "ext": {},
                    "filter": {"index_list": []},
                    "evaluate_ab_params": "", "evaluate_common_params": "",
                    "option": {"lazy_load_strategy": 0}}},
                "sequence_id": __import__("uuid").uuid4().hex,
                "channel": 2, "version": "1",
            }
            try:
                async with s.post(f"https://{CN_DOMAIN}/im/chain/single",
                                  params=cli._params(), json=pbody,
                                  headers=cli._headers(local_conv_id=cli._local_conv_id, im=True),
                                  timeout=aiohttp.ClientTimeout(total=30)) as r:
                    data = await r.json(content_type=None)
            except Exception as e:
                log(f"      #{n} 轮询异常 {type(e).__name__}: {str(e)[:60]}")
                continue
            sc = data.get("status_code")
            dl = (data or {}).get("downlink_body") or {}
            msgs = cli._extract_messages(dl)
            found = None
            for m in msgs:
                c = m.get("content")
                if isinstance(c, str):
                    try:
                        c = json.loads(c)
                    except Exception:
                        continue
                for blk in (c or []):
                    if blk.get("block_type") == 2074:
                        for cre in ((blk.get("content") or {}).get("creation_block", {})
                                    .get("creations") or []):
                            if cre.get("type") == 2:
                                v = cre.get("video") or {}
                                found = v.get("download_url") or v.get("url")
                                if found:
                                    log(f"      #{n} ★ 出片 download_url 命中")
            log(f"      #{n} status_code={sc} downlink_keys={list(dl.keys())[:4]} msgs={len(msgs)}"
                + (" ★OK" if found else ""))
            if found:
                log("✅ 出片成功：", found[:140])
                Path("last_video_url.txt").write_text(found, encoding="utf-8")
                return
    log("❌ 轮询超时未出片")


asyncio.run(main())
