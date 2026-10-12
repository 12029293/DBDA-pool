# -*- coding: utf-8 -*-
"""豆包域——campaign / marketing 活动接口只读探测

目标：找「领30天VIP」对应的可领活动。
只打查询类接口（不触发真实领取）。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:/Users/Administrator/Desktop/dola-pool")
import aiohttp  # noqa: E402
from doubao_cn_video import DoubaoVideoClient, CN_DOMAIN  # noqa: E402

CK = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:/Users/Administrator/Desktop/纯算注册/doubao_ck/doubao_7006284f659f416a_20261008_045605.txt"

READONLY = [
    "/alice/commerce/sale/campaign/page/query/",
    "/alice/commerce/sale/subscription/overview/",
    "/alice/commerce/sale/subscription/status/",
    "/alice/commerce/marketing/card/list/",
    "/alice/commerce/marketing/activity/detail/",
    "/alice/commerce/marketing/resource/mget/v2/",
    "/alice/commerce/sale/marketing/cta/resolve_fixed_cta",
]


async def main():
    ck = Path(CK).read_text(encoding="utf-8").strip()
    cli = DoubaoVideoClient(ck)
    async with aiohttp.ClientSession(**cli._sess_kw()) as s:
        for path in READONLY:
            url = f"https://{CN_DOMAIN}{path}"
            try:
                async with s.post(url, params=cli._params(), json={},
                                  headers={**cli._headers(), "agw-js-conv": "str, str"},
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
                    txt = await r.text()
                    print("=" * 74)
                    print(f"{path}  ->  HTTP {r.status}  len={len(txt)}")
                    print(txt[:700])
            except Exception as e:
                print("=" * 74)
                print(f"{path}  -> ERR {type(e).__name__}: {e}")


asyncio.run(main())
