# -*- coding: utf-8 -*-
"""客户端登录后 —— 一键验证：导出 CK → 校验登录态 → 查会员 → 查活动资格

用法：
    python after_login_check.py [port]
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, r"C:/Users/Administrator/Desktop/dola-pool")
import aiohttp  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402
from doubao_cn_video import DoubaoVideoClient, CN_DOMAIN  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9333
CK_OUT = Path(r"C:/Users/Administrator/Desktop/纯算注册/doubao_ck/doubao_client_login.txt")


async def grab_ck() -> str:
    async with async_playwright() as p:
        br = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        ck = await br.contexts[0].cookies("https://www.doubao.com")
        await br.close()
    text = "; ".join(f"{c['name']}={c['value']}" for c in ck)
    return text


async def probe(ck: str):
    cli = DoubaoVideoClient(ck)
    pc = cli._params({"aid": "1044603", "real_aid": "1044603",
                      "doubao_device_platform": "desktop"})
    web = cli._params()
    out = {}
    async with aiohttp.ClientSession(**cli._sess_kw()) as s:
        async def post(path, body, params):
            url = f"https://{CN_DOMAIN}{path}"
            async with s.post(url, params=params, json=body,
                              headers={**cli._headers(), "agw-js-conv": "str, str"},
                              timeout=aiohttp.ClientTimeout(total=20)) as r:
                return await r.json(content_type=None)

        try:
            j = await post("/alice/user/get_account_info", {}, web)
            out["account"] = (j.get("data") or {}).get("base_account_info")
            out["connect"] = (j.get("data") or {}).get("connect_account_info")
        except Exception as e:
            out["account"] = f"ERR {e}"

        try:
            j = await post("/alice/commerce/sale/subscription/quota/summary/",
                           {"product_line": "membership"}, pc)
            d = j.get("data") or {}
            out["member_info"] = d.get("member_info")
            sub = d.get("current_subscription") or {}
            out["subscription"] = {k: sub.get(k) for k in
                                   ("is_gift", "sku_key", "status", "start_time", "end_time")}
        except Exception as e:
            out["member_info"] = f"ERR {e}"

        try:
            j = await post("/alice/commerce/sale/subscription/entry/config/", {}, pc)
            d = j.get("data") or {}
            out["show_pc_campaign"] = d.get("show_pc_campaign")
            out["subs_status"] = d.get("subs_status")
            out["gift_entries"] = [
                e.get("title", {}).get("text") if isinstance(e.get("title"), dict) else e.get("title")
                for e in (d.get("profile_entry_list") or [])
            ]
        except Exception as e:
            out["show_pc_campaign"] = f"ERR {e}"

        try:
            j = await post("/biz/ug_reason/new_user_check", {}, pc)
            out["is_old_user_desktop"] = j.get("is_old_user")
        except Exception as e:
            out["is_old_user_desktop"] = f"ERR {e}"
    return out


async def main():
    ck = await grab_ck()
    if "sessionid=" not in ck:
        print("[!] 客户端当前不是登录态（无 sessionid）。请先在客户端登录。")
        return
    CK_OUT.write_text(ck, encoding="utf-8")
    print("CK 已导出 ->", CK_OUT)
    print("cookie 数:", len(ck.split(";")))

    r = await probe(ck)
    vip = bool((r.get("member_info") or {}).get("hasActiveSubscription"))
    print()
    print("=" * 62)
    print("账号       :", r.get("account"))
    print("第三方绑定 :", r.get("connect"))
    print("is_old_user(desktop):", r.get("is_old_user_desktop"))
    print("subs_status         :", r.get("subs_status"))
    print("show_pc_campaign    :", r.get("show_pc_campaign"))
    print("gift_entries        :", r.get("gift_entries"))
    print("subscription        :", json.dumps(r.get("subscription"), ensure_ascii=False))
    print("member_info         :", json.dumps(r.get("member_info"), ensure_ascii=False))
    print("-" * 62)
    print(">>> 判定:", "✅ 已是会员（可用 seedance_v2.5）" if vip else "❌ 仍非会员")
    print("=" * 62)


asyncio.run(main())
