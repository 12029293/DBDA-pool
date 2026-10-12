# -*- coding: utf-8 -*-
"""探测 campaign/trial/init 与 fixed_subscription/claim（空 body）"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:/Users/Administrator/Desktop/dola-pool")
import aiohttp  # noqa: E402
from doubao_cn_video import DoubaoVideoClient, CN_DOMAIN  # noqa: E402

CK = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:/Users/Administrator/Desktop/纯算注册/doubao_ck/doubao_7006284f659f416a_20261008_045605.txt"

EPS = [
    "/alice/commerce/sale/campaign/trial/init/",
    "/alice/commerce/sale/campaign/fixed_subscription/claim/",
]


async def main():
    ck = Path(CK).read_text(encoding="utf-8").strip()
    cli = DoubaoVideoClient(ck)
    async with aiohttp.ClientSession(**cli._sess_kw()) as s:
        for path in EPS:
            url = f"https://{CN_DOMAIN}{path}"
            for body in ({}, {"AgwCommonParam": {}}):
                try:
                    async with s.post(url, params=cli._params(), json=body,
                                      headers={**cli._headers(), "agw-js-conv": "str, str"},
                                      timeout=aiohttp.ClientTimeout(total=15)) as r:
                        txt = await r.text()
                        print("=" * 74)
                        print(f"{path}  body={json.dumps(body, ensure_ascii=False)}")
                        print(f"  HTTP {r.status}  -> {txt[:500]}")
                except Exception as e:
                    print("=" * 74)
                    print(f"{path} body={body} -> ERR {type(e).__name__}: {e}")


asyncio.run(main())
