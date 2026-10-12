# -*- coding: utf-8 -*-
"""豆包域——登录态 + 会员权限开通状态 一键确认

用法：
    python check_doubao_perm.py                       # 默认 CK
    python check_doubao_perm.py <ck文件>

输出：
    1) 域判定 / sessionid
    2) 登录态  /alice/user/get_account_info
    3) 会员权限 /alice/commerce/sale/subscription/entry/config
       （has_active_subscription / subs_status / sku_key / is_gift / 文案）
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:/Users/Administrator/Desktop/dola-pool")

import aiohttp  # noqa: E402
from doubao_cn_video import DoubaoVideoClient, CN_DOMAIN  # noqa: E402

CK_FILE = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:/Users/Administrator/Desktop/扫码测试/扫码CK.txt"


async def subscription_entry(cli: DoubaoVideoClient) -> dict:
    """会员/订阅入口配置——权限是否开通的权威探针"""
    url = f"https://{CN_DOMAIN}/alice/commerce/sale/subscription/entry/config/"
    async with aiohttp.ClientSession(**cli._sess_kw()) as s:
        async with s.post(url, params=cli._params(), json={},
                          headers={**cli._headers(), "agw-js-conv": "str, str"},
                          timeout=aiohttp.ClientTimeout(total=15)) as r:
            txt = await r.text()
    try:
        return json.loads(txt)
    except Exception:
        return {"_raw": txt[:400]}


async def main():
    ck = Path(CK_FILE).read_text(encoding="utf-8").strip()
    print("CK 文件  :", CK_FILE)
    cli = DoubaoVideoClient(ck)
    print("识别域   :", cli.domain)
    print("sessionid:", (cli.sessionid[:16] + "...") if cli.sessionid else "(无)")
    print("is_valid :", cli.is_valid)

    print("\n[1/3] 登录态 /alice/user/get_account_info ...")
    ok = await cli.check_login()
    print("        登录态有效:", ok)
    info = await cli.account_info()
    if info:
        base = info.get("base_account_info") or {}
        print("        手机号    :", base.get("mobile"))
        print("        user_id   :", info.get("user_id") or base.get("user_id"))
        for c in (info.get("connect_account_info") or []):
            print("        绑定      :", c.get("platform"), c.get("platform_screen_name"))

    print("\n[2/3] 会员权限 /alice/commerce/sale/subscription/entry/config/ ...")
    sub = await subscription_entry(cli)
    data = sub.get("data") if isinstance(sub, dict) else None
    if isinstance(data, dict):
        for k in ("has_active_subscription", "membership_display_name",
                  "subs_status", "subscription",
                  "settings_upgrade_page", "entry_config", "rights"):
            if k in data:
                print(f"        {k:28}: {json.dumps(data[k], ensure_ascii=False)[:220]}")
        if not any(k in data for k in ("has_active_subscription", "subs_status")):
            print("        (data keys):", list(data.keys())[:20])
    else:
        print("        原始返回:", json.dumps(sub, ensure_ascii=False)[:400])

    print("\n[3/3] 结论")
    active = None
    if isinstance(data, dict):
        active = data.get("has_active_subscription")
        st = data.get("subs_status")
        print(f"        会员权限已开通: {bool(active)}    subs_status={st}")


asyncio.run(main())
