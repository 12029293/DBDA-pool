"""探测 Dola 输入框的真实 DOM 结构（.tiptap.ProseMirror 失效后）。"""
import asyncio, json, sys
from pathlib import Path
import proxy_fwd

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "acc2"
STATE = Path("accounts") / ACCOUNT / "cookie_state.json"
PROXY = sys.argv[2] if len(sys.argv) > 2 else "socks5://com38730086-res-hk-Lsid-922601900-TTL-7200:EwT5JARgS7uRe8f@154.51.240.40:9595"

JS_PROBE = """(()=>{
  const out = {contenteditables: [], prosemirror: [], tiptap: [], iframes: []};
  document.querySelectorAll('[contenteditable=true]').forEach(e=>{
    const r = e.getBoundingClientRect();
    out.contenteditables.push({cls:(e.className||'').toString().slice(0,150),
      tag:e.tagName, ph:e.getAttribute('data-placeholder')||e.getAttribute('placeholder')||'',
      x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height),
      visible: e.offsetParent!==null});
  });
  document.querySelectorAll('.ProseMirror, [class*=prosemirror], [class*=tiptap]').forEach(e=>{
    out.prosemirror.push({tag:e.tagName, cls:(e.className||'').toString().slice(0,150)});
  });
  document.querySelectorAll('iframe').forEach(f=>out.iframes.push({src:(f.src||'').slice(0,80)}));
  document.querySelectorAll('textarea').forEach(e=>{
    const r=e.getBoundingClientRect();
    out.contenteditables.push({tag:'TEXTAREA', cls:(e.className||'').toString().slice(0,150),
      ph:e.placeholder||'', x:Math.round(r.x), y:Math.round(r.y), visible:e.offsetParent!==null});
  });
  // 底部输入区域 approximate: 找 placeholder=Message 的元素
  const msg = [...document.querySelectorAll('*')].find(e=>e.getAttribute && (e.getAttribute('data-placeholder')==='Message...'||e.getAttribute('placeholder')==='Message...'));
  if (msg) { out.messageEl = {tag:msg.tagName, cls:(msg.className||'').toString().slice(0,200),
      ce:msg.getAttribute('contenteditable'), parentCls:(msg.parentElement?.className||'').toString().slice(0,200)}; }
  return JSON.stringify(out);
})()"""


async def main():
    from patchright.async_api import async_playwright
    state = json.loads(STATE.read_text(encoding="utf-8"))
    cookies = [{"name": c["name"], "value": c["value"],
                "domain": c.get("domain") or ".dola.com", "path": c.get("path") or "/",
                "secure": True} for c in state.get("cookies_list") or []]
    fwd = await proxy_fwd.SocksForwarder.start(PROXY)
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False, proxy={"server": f"socks5://127.0.0.1:{fwd.port}"},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--no-default-browser-check", "--disable-dev-shm-usage"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, locale="en-US")
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.goto("https://www.dola.com/chat/", wait_until="domcontentloaded", timeout=60000)
        for i in range(10):
            await asyncio.sleep(4)
            raw = await page.evaluate(JS_PROBE)
            d = json.loads(raw) if isinstance(raw, str) else {}
            ce = [c for c in d.get("contenteditables", []) if c.get("visible")]
            if ce or d.get("messageEl") or d.get("prosemirror"):
                print(f"[{i*4}s] FOUND:")
                print(json.dumps({k: d.get(k) for k in ("contenteditables","prosemirror","tiptap","messageEl","iframes")}, ensure_ascii=False, indent=1))
                break
            print(f"[{i*4}s] 还没有可编辑元素…")
        else:
            print("40s 内没发现任何可编辑元素")
        await browser.close()

asyncio.run(main())
