# -*- coding: utf-8 -*-
"""豆包域——「客户端标识」参数实验

假设：DoubaoWork 客户端 = 内嵌加载 www.doubao.com。
「下载电脑版 领 30 天订阅」活动只在**客户端标识**下发放。
本脚本遍历 device_platform / doubao_device_platform / web_platform 组合，
对比 new_user_check / entry_config / banner / pop_window 的响应差异。
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

COMBOS = [
    ("baseline-web", {}),
    ("platform-pc", {"device_platform": "pc", "doubao_device_platform": "pc", "web_platform": "pc"}),
    ("platform-pc-client", {"device_platform": "pc", "doubao_device_platform": "pc", "web_platform": "client"}),
    ("only-dbp-pc", {"doubao_device_platform": "pc", "web_platform": "pc"}),
]

CHECKS = [
    ("new_user_check", "/biz/ug_reason/new_user_check", {}),
    ("entry_config", "/alice/commerce/sale/subscription/entry/config/", {}),
    ("active_banner", "/alice/bot/action_bar_v3/active_switch_banner", {}),
    ("pop_window", "/biz/activity/get_pop_window",
     {"pc_os": "windows", "scene": "chat", "surface": "pc"}),
]


async def main():
    ck = Path(CK_FILE).read_text(encoding="utf-8").strip()
    cli = DoubaoVideoClient(ck)

    async with aiohttp.ClientSession(**cli._sess_kw()) as s:
        for name, extra in COMBOS:
            print("\n" + "#" * 76)
            print(f"### 组合: {name}  extra={extra}")
            for label, path, body in CHECKS:
                url = f"https://{CN_DOMAIN}{path}"
                params = cli._params(extra or None)
                try:
                    async with s.post(
                        url, params=params, json=body,
                        headers={**cli._headers(), "agw-js-conv": "str, str"},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as r:
                        txt = await r.text()
                        # 精简输出：只提关键字段
                        try:
                            obj = json.loads(txt)
                            d = obj.get("data") if isinstance(obj, dict) else None
                            brief = {}
                            if label == "new_user_check":
                                brief = {k: obj.get(k) for k in ("is_old_user", "first_login_time")}
                            elif isinstance(d, dict):
                                for k in ("has_active_subscription", "subs_status",
                                          "is_gift", "available_count"):
                                    if k in d:
                                        brief[k] = d[k]
                                if not brief:
                                    brief = {"keys": list(d.keys())[:8]}
                            else:
                                brief = obj if not isinstance(obj, dict) else \
                                    {k: obj[k] for k in list(obj)[:5]}
                            print(f"  [{r.status}] {label:16} -> {json.dumps(brief, ensure_ascii=False)[:220]}")
                        except Exception:
                            print(f"  [{r.status}] {label:16} -> {txt[:150]}")
                except Exception as e:
                    print(f"  ERR {label}: {type(e).__name__}: {e}")


asyncio.run(main())
