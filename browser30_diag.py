"""诊断浏览器 30s 通道：acc 号打开 dola.com 后页面到底是什么状态。

输出：登录态（uid）、编辑器是否出现、页面上时长/模型相关 UI 文本、
截图、以及 hook30 日志（若页面自己发了 chat/completion，能看到原生 ability_param）。
用法: python browser30_diag.py <account> [wait_seconds]
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import config
import proxy_fwd

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "acc2"
WAIT = int(sys.argv[2]) if len(sys.argv) > 2 else 40
STATE = Path("accounts") / ACCOUNT / "cookie_state.json"
PROXY = sys.argv[3] if len(sys.argv) > 3 else None


async def main():
    from patchright.async_api import async_playwright

    state = json.loads(STATE.read_text(encoding="utf-8"))
    cookies = [{"name": c["name"], "value": c["value"],
                "domain": c.get("domain") or ".dola.com", "path": c.get("path") or "/",
                "secure": True} for c in state.get("cookies_list") or []]
    print(f"cookies: {len(cookies)} 条")
    proxy_url = PROXY or config.PROXY
    print("proxy:", proxy_url[:60], "...")
    fwd = await proxy_fwd.SocksForwarder.start(proxy_url)
    print("forwarder port:", fwd.port)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            proxy={"server": f"socks5://127.0.0.1:{fwd.port}"},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--no-default-browser-check", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US")
        await ctx.add_init_script((_HOOK := Path("hook30.js").read_text(encoding="utf-8")))
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        t0 = time.time()
        await page.goto("https://www.dola.com/chat/", wait_until="domcontentloaded", timeout=60000)
        for i in range(WAIT // 4):
            await asyncio.sleep(4)
            st = await page.evaluate("""(()=>{
              const ed=!!document.querySelector('.tiptap.ProseMirror');
              const t=(document.body.innerText||'');
              return JSON.stringify({ed, url:location.href.slice(0,90),
                title:document.title.slice(0,40),
                logout:location.href.includes('logout'),
                loginWall:/请登录|Please log in|Log in|登录/.test(t.slice(0,3000)),
                chips:(t.match(/视频生成|影片生成|模型\\s*[\\d.]*|比例|时长[\\s\\d]*/g)||[]).slice(0,6),
                tail:t.slice(-200).replace(/\\s+/g,' ')});
            })""")
            d = json.loads(st) if isinstance(st, str) else {}
            print(f"[{(time.time()-t0):.0f}s] ed={d.get('ed')} url={d.get('url')[:60]} "
                  f"logout={d.get('logout')} title={d.get('title')!r} chips={d.get('chips')}")
            if d.get("ed"):
                break
        # 登录态验证（对齐 inject5.js）
        uid = await page.evaluate("""(async()=>{
          const m=document.cookie.match(/passport_csrf_token=([^;]+)/);
          try{
            const r=await fetch('https://www.dola.com/passport/account/info/v2/',{credentials:'include',headers:{'x-tt-passport-csrf-token':m?m[1]:''}});
            const j=await r.json();
            return JSON.stringify({uid:(j.data&&j.data.user_id_str)||(j.data&&j.data.user_id?String(j.data.user_id):null), info:JSON.stringify(j).slice(0,100)});
          }catch(e){return JSON.stringify({uid:null,err:String(e).slice(0,80)});}
        })""")
        print("login check:", uid)
        # 停留一会儿，若页面自动发了 chat/completion，hook 日志会记录原生 ability_param
        print("等待页面可能的自动请求 10s ...")
        await asyncio.sleep(10)
        hook_log = await page.evaluate("JSON.stringify(window.__dola30sLog||[])")
        print("hook30 log:", hook_log[:600])
        shot = Path(f"diag_{ACCOUNT}.png")
        await page.screenshot(path=str(shot), full_page=False)
        print("screenshot:", shot.resolve())
        await browser.close()
    await fwd.stop()


if __name__ == "__main__":
    asyncio.run(main())
