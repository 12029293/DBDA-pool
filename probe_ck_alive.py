# -*- coding: utf-8 -*-
"""豆包域 CK 活性多探针诊断
判定：sessionid 是否还有效 / 是被降级为匿名还是真失效
"""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, r"C:/Users/Administrator/Desktop/dola-pool")
import aiohttp
from doubao_cn_video import DoubaoVideoClient, CN_DOMAIN

CK = sys.argv[1] if len(sys.argv) > 1 else r"C:/Users/Administrator/Desktop/扫码测试/扫码CK.txt"
REAL_FP = "verify_muykqgmj_1gzRN0Mv_u9Z2_4o2A_831j_E5errwKLnH6F"


async def probe(cli, method, path, params=None, note=""):
    url = f"https://{CN_DOMAIN}{path}"
    h = {**cli._headers(), "agw-js-conv": "str, str"}
    try:
        async with aiohttp.ClientSession(**cli._sess_kw()) as s:
            fn = s.get if method == "GET" else s.post
            kw = {"params": params if params is not None else cli._params(),
                  "headers": h, "timeout": aiohttp.ClientTimeout(total=15)}
            if method == "POST":
                kw["json"] = {}
            async with fn(url, **kw) as r:
                t = await r.text()
        print(f"\n[{method} {path}] {note}  HTTP {r.status}")
        print("  ", t[:500])
    except Exception as e:
        print(f"\n[{method} {path}] {note}  ERR {e!r}")


async def main():
    ck = Path(CK).read_text(encoding="utf-8").strip()
    cli = DoubaoVideoClient(ck)
    print("CK:", CK)
    print("sessionid:", cli.sessionid[:20], "...  len=", len(cli.sessionid))
    print("domain:", cli.domain)

    await probe(cli, "POST", "/alice/user/get_account_info", note="随机参数")
    p = cli._params(); p["fp"] = REAL_FP; p["region"] = "HK"; p["sys_region"] = "HK"
    await probe(cli, "POST", "/alice/user/get_account_info", params=p, note="浏览器真 fp+HK")
    await probe(cli, "POST", "/alice/user/launch", note="launch")
    await probe(cli, "POST", "/passport/web/account/info/", note="旧 passport 探针")
    await probe(cli, "POST", "/alice/commerce/sale/subscription/entry/config/", note="会员入口")

    # 发一条纯文本，看 inner_is_tour（true=匿名）
    print("\n=== 文本请求，判定 inner_is_tour ===")
    body = cli._build_video_body("你好", "9:16", 5, "seedance_v2.0")
    body.pop("chat_ability", None)   # 纯文本
    try:
        async with aiohttp.ClientSession(**cli._sess_kw()) as s:
            async with s.post(f"https://{CN_DOMAIN}/chat/completion",
                              params=cli._params(), json=body,
                              headers=cli._headers(local_conv_id=cli._local_conv_id),
                              timeout=aiohttp.ClientTimeout(total=60)) as r:
                txt = await r.text()
        for line in txt.splitlines():
            if line.startswith("event:") or "conversation_id" in line or "inner_is_tour" in line:
                print("  ", line[:300])
    except Exception as e:
        print("  ERR", repr(e))


asyncio.run(main())
