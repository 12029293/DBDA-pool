# -*- coding: utf-8 -*-
"""对照实验：web 参数 vs PC 客户端(desktop) 参数
   - /biz/ug_reason/new_user_check      -> is_old_user
   - /alice/commerce/sale/subscription/entry/config/ -> show_pc_campaign
"""
import asyncio, json, sys, uuid
from pathlib import Path
import aiohttp

CK = (sys.argv[1] if len(sys.argv) > 1 else
      r"C:/Users/Administrator/Desktop/纯算注册/doubao_ck/doubao_7006284f659f416a_20261008_045605.txt")
CK = Path(CK).read_text(encoding="utf-8").strip()

DOMAIN = "www.doubao.com"
WEB_AID = "497858"
PC_AID = "1044603"
PC_VERSION_CODE = "20800"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")


def hdrs(referer="https://www.doubao.com/chat/"):
    return {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": f"https://{DOMAIN}",
        "Referer": referer,
        "User-Agent": UA,
        "Cookie": CK,
        "agw-js-conv": "str, str",
    }


def params(aid, ddp, device_id, web_id):
    return {
        "aid": aid, "real_aid": aid,
        "device_id": device_id, "web_id": web_id, "tea_uuid": web_id,
        "device_platform": "web", "doubao_device_platform": ddp,
        "version_code": PC_VERSION_CODE, "language": "zh",
        "pkg_type": "release_version", "samantha_web": 1,
        "web_platform": "browser", "tz_name": "Asia/Shanghai",
        "use-olympus-account": 1, "region": "CN", "sys_region": "CN",
        "pc_version": "3.39.2", "doubao_pc_version": "3.39.2",
        "web_tab_id": str(uuid.uuid4()),
    }


async def call(s, path, p, body=None, label=""):
    url = f"https://{DOMAIN}{path}"
    try:
        async with s.post(url, params=p, json=body if body is not None else {},
                          headers=hdrs(), timeout=aiohttp.ClientTimeout(total=15)) as r:
            j = await r.json(content_type=None)
    except Exception as e:
        print(f"  [{label}] EXC {e}")
        return None
    txt = json.dumps(j, ensure_ascii=False)
    print(f"  [{label}] {txt[:400]}")
    return j


async def main():
    print("CK sessionid:", (CK.split("sessionid=")[1].split(";")[0][:12] if "sessionid=" in CK else "?"))
    async with aiohttp.ClientSession(trust_env=True) as s:
        did = "7" + "".join(str((i * 7 + 3) % 10) for i in range(18))
        wid = "7" + "".join(str((i * 3 + 1) % 10) for i in range(18))
        client_did, client_wid = "18179582551742", "7694047386717390390"

        print("\n=== A) new_user_check  网页参数(aid=497858/web) ===")
        await call(s, "/biz/ug_reason/new_user_check",
                   params(WEB_AID, "web", did, wid), {}, "web")

        print("\n=== B) new_user_check  客户端参数(aid=1044603/desktop, 随机device) ===")
        await call(s, "/biz/ug_reason/new_user_check",
                   params(PC_AID, "desktop", did, wid), {}, "desktop-rand")

        print("\n=== C) new_user_check  客户端参数 + 客户端真实device_id ===")
        await call(s, "/biz/ug_reason/new_user_check",
                   params(PC_AID, "desktop", client_did, client_wid), {}, "desktop-real")

        print("\n=== D) entry/config  客户端参数(desktop) ===")
        j = await call(s, "/alice/commerce/sale/subscription/entry/config/",
                       params(PC_AID, "desktop", did, wid), {}, "desktop")
        if j:
            d = j.get("data") or {}
            print("     -> show_pc_campaign =", d.get("show_pc_campaign"),
                  "| subs_status =", d.get("subs_status"))

        print("\n=== E) entry/config  网页参数(web) ===")
        j = await call(s, "/alice/commerce/sale/subscription/entry/config/",
                       params(WEB_AID, "web", did, wid), {}, "web")
        if j:
            d = j.get("data") or {}
            print("     -> show_pc_campaign =", d.get("show_pc_campaign"),
                  "| subs_status =", d.get("subs_status"))

        print("\n=== F) account_info 校验登录态 ===")
        await call(s, "/alice/user/get_account_info",
                   params(WEB_AID, "web", did, wid), {}, "acct")


asyncio.run(main())
