# -*- coding: utf-8 -*-
"""验证「游客归因 -> 上报新用户」链路（纯协议）

客户端 UGAttributionManager 逻辑：
  1) 游客态发过消息 -> localStorage 记 guest_sent_message
  2) 登录后 -> POST /biz/ug_reason/new_user_report  {source_info}
  3) 服务端据 web_anon_id 关联 -> 发新用户权益

本脚本：同一 device_id/web_id 会话内
  A. 游客态发一条消息（无登录 cookie）
  B. 用账号 CK 调 new_user_check / new_user_report
  C. 复查订阅状态
"""
import asyncio, json, sys, uuid
from pathlib import Path
import aiohttp

CK = Path(sys.argv[1] if len(sys.argv) > 1 else
          r"C:/Users/Administrator/Desktop/纯算注册/doubao_ck/doubao_7006284f659f416a_20261008_045605.txt"
          ).read_text(encoding="utf-8").strip()

DOMAIN = "www.doubao.com"
PC_AID = "1044603"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

DEVICE_ID = "7" + "".join(str((i * 7 + 3) % 10) for i in range(18))
WEB_ID = "7" + "".join(str((i * 3 + 1) % 10) for i in range(18))


def params(extra=None):
    p = {
        "aid": PC_AID, "real_aid": PC_AID,
        "device_id": DEVICE_ID, "web_id": WEB_ID, "tea_uuid": WEB_ID,
        "device_platform": "web", "doubao_device_platform": "desktop",
        "version_code": "20800", "language": "zh",
        "pkg_type": "release_version", "samantha_web": 1,
        "web_platform": "browser", "tz_name": "Asia/Shanghai",
        "use-olympus-account": 1, "region": "CN", "sys_region": "CN",
        "pc_version": "2.31.10", "doubao_pc_version": "2.31.10",
        "web_tab_id": str(uuid.uuid4()),
    }
    if extra:
        p.update(extra)
    return p


def hdrs(cookie=None):
    h = {
        "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": f"https://{DOMAIN}", "Referer": "https://www.doubao.com/chat/",
        "User-Agent": UA, "agw-js-conv": "str, str",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


SOURCE_INFO = {
    "channel": "", "landing_page_url": "https://www.doubao.com/chat",
    "source": "", "channel_v2": "", "landing_page_url_v2": "https://www.doubao.com/chat",
    "source_v2": "",
}


async def post(s, path, body, cookie=None, label="", extra=None):
    url = f"https://{DOMAIN}{path}"
    try:
        async with s.post(url, params=params(extra), json=body, headers=hdrs(cookie),
                          timeout=aiohttp.ClientTimeout(total=20)) as r:
            t = await r.text()
    except Exception as e:
        print(f"[{label}] EXC {type(e).__name__}: {e}")
        return None
    print(f"[{label}] HTTP? len={len(t)} -> {t[:300]}")
    try:
        return json.loads(t)
    except Exception:
        return None


async def main():
    async with aiohttp.ClientSession(trust_env=True) as s:
        print("=== A) 游客态：调 new_user_check（无登录 cookie）===")
        await post(s, "/biz/ug_reason/new_user_check", {}, cookie=None, label="guest-check")

        print("\n=== B) 登录态：new_user_check ===")
        await post(s, "/biz/ug_reason/new_user_check", {}, cookie=CK, label="auth-check")

        print("\n=== C) 登录态：new_user_report（上报新用户）===")
        await post(s, "/biz/ug_reason/new_user_report", {"source_info": SOURCE_INFO},
                   cookie=CK, label="auth-report")

        print("\n=== D) 复查订阅状态 ===")
        p = params()
        try:
            async with s.post(f"https://{DOMAIN}/alice/commerce/sale/subscription/quota/summary/",
                              params=p, json={"product_line": "membership"}, headers=hdrs(CK),
                              timeout=aiohttp.ClientTimeout(total=20)) as r:
                j = await r.json(content_type=None)
            d = j.get("data") or {}
            print("current_subscription:", json.dumps(d.get("current_subscription"), ensure_ascii=False))
            print("member_info        :", json.dumps(d.get("member_info"), ensure_ascii=False))
        except Exception as e:
            print("quota err", e)


asyncio.run(main())
