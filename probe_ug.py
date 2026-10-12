# -*- coding: utf-8 -*-
import asyncio, glob, os, sys
from pathlib import Path
sys.path.insert(0, r"C:/Users/Administrator/Desktop/dola-pool")
import aiohttp
from doubao_cn_video import DoubaoVideoClient, CN_DOMAIN

CK = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:/Users/Administrator/Desktop/纯算注册/doubao_ck/doubao_7006284f659f416a_20261008_045605.txt"

jsdir = r"C:/Users/Administrator/Desktop/纯算注册/抓包/recon_js/js"
fn = glob.glob(os.path.join(jsdir, "*ug-sidebar-popup*"))[0]
t = open(fn, encoding="utf-8", errors="replace").read()
print("=== 方法签名 ===")
for name in ["GetCampaignList", "GetCampaignV3Detail", "CreateCampaign",
             "ClaimCampaignReward", "VerifyCampaign"]:
    i = t.find(name + "(")
    if i >= 0:
        print(f"  {name}: {t[i:i+240]}")
print()

EPS = ["/alice/ug/campaign/list/", "/alice/ug/campaign/v3/detail/"]


async def main():
    ck = Path(CK).read_text(encoding="utf-8").strip()
    cli = DoubaoVideoClient(ck)
    async with aiohttp.ClientSession(**cli._sess_kw()) as s:
        for path in EPS:
            url = f"https://{CN_DOMAIN}{path}"
            try:
                async with s.post(url, params=cli._params(), json={},
                                  headers={**cli._headers(), "agw-js-conv": "str, str"},
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
                    txt = await r.text()
                    print("=" * 74)
                    print(path, "HTTP", r.status)
                    print(txt[:1000])
            except Exception as e:
                print("=" * 74)
                print(path, "ERR", type(e).__name__, e)


asyncio.run(main())
