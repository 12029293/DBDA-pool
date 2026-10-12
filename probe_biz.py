# -*- coding: utf-8 -*-
"""豆包域——会员/活动/新用户 相关 biz+alice 接口裸探

用法：
    python probe_biz.py [ck文件]

目的：
  1) 找「会员领取」的判定/发放接口
  2) 摸清 new_user_check / pop_window 返回结构
所有接口都用裸参数（不猜 body），先看能否返 code=0。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:/Users/Administrator/Desktop/dola-pool")

import aiohttp  # noqa: E402
from doubao_cn_video import DoubaoVideoClient, CN_DOMAIN  # noqa: E402

CK_FILE = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:/Users/Administrator/Desktop/纯算注册/doubao_ck/doubao_7006284f659f416a_20261008_045605.txt"

ENDPOINTS = [
    "/biz/ug_reason/new_user_check",
    "/biz/ug_reason/matrix_report",
    "/biz/ug_reason/new_user_report",
    "/biz/activity/get_pop_window",
    "/biz/activity/get_push_banner",
    "/alice/commerce/sale/subscription/entry/config",
]


async def main():
    ck = Path(CK_FILE).read_text(encoding="utf-8").strip()
    cli = DoubaoVideoClient(ck)
    print("CK:", CK_FILE)
    print("sessionid:", (cli.sessionid[:16] + "...") if cli.sessionid else "(无)")

    async with aiohttp.ClientSession(**cli._sess_kw()) as s:
        for path in ENDPOINTS:
            url = f"https://{CN_DOMAIN}{path}"
            try:
                async with s.post(
                    url, params=cli._params(), json={},
                    headers={**cli._headers(), "agw-js-conv": "str, str"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    txt = await r.text()
                    print("\n" + "=" * 70)
                    print(f"{path}  ->  HTTP {r.status}  len={len(txt)}")
                    try:
                        obj = json.loads(txt)
                        print(json.dumps(obj, ensure_ascii=False)[:1200])
                    except Exception:
                        print(txt[:600])
            except Exception as e:
                print("\n" + "=" * 70)
                print(f"{path}  ->  ERR {type(e).__name__}: {e}")


asyncio.run(main())
