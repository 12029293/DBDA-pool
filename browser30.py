"""30 秒浏览器出片通道（hook30.js 方案）。

背景（2026-10-06 批量失败证实 + 《Dola 30秒时长解锁与自动化测试文档》）：
纯 API 请求 30s 时上游受理扣点后回「只支持 4~15 秒、将拆两段」(status=pending)
然后完全停滞，等满时限判失败。文档验证过的可靠路线是真实浏览器 + hook30.js
在页面内把 /chat/completion 请求体的 duration 改成 30（服务端照单全收，
实测出片 30.08s 无水印）。

流程（对应文档 §5，用 patchright 实现，native input 满足"原生点击"硬门槛）：
    起本地 SOCKS5 转发器（单号单 IP，绑定该号当前节点）
    → patchright 无痕上下文 + cookie 注入 + add_init_script(hook30)
    → 关横幅 → 聚焦编辑器 → 激活视频生成 chip → 选 Seedance 2.5
    → 硬守卫（模型含 2.5 / 提示词全文唯一 / 发送键可用 / 未登出）才发送一次
    → 回读 __dola30sLog 确认 patched:'fetch→30'
    → 轮询挖 React fiber 视频直链 → 下载 → ffprobe 校验时长

异常映射（对齐 browser_pool 的换号处理）：
    登出 → LoginExpiredError；余额不足 → CreditInsufficientError；
    不足 0.9×30s 的成片 → ShortClipError（换号重试、不标号失败）；
    上游一句回执都没有 → AbnormalNoAckError；超时未出片 → TimeoutError（不换号）。
"""
import asyncio
import base64
import json
import subprocess
import time
from pathlib import Path

import config
import proxy_fwd
from pure_api_gen import AbnormalNoAckError, ShortClipError
from video_worker_ui import CreditInsufficientError, LoginExpiredError

_HOOK_PATH = Path(__file__).parent / "hook30.js"
CHAT_URL = "https://www.dola.com/chat/"

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--disable-background-networking",
    "--disable-sync",
]


class HookNotPatchedError(RuntimeError):
    """发送成功但钩子日志没有 patched 记录（请求可能以 10s 发出）。"""


def _hook_source(target_duration: int = 30) -> str:
    """钩子源码 + 目标时长注入（window.__dolaTargetDur，未注入时钩子默认 30）。"""
    target = int(target_duration or 30)
    return f"window.__dolaTargetDur = {target};\n" + _HOOK_PATH.read_text(encoding="utf-8")


async def _eval(page, expr):
    """page.evaluate，异常时返回 {'__err': ...} 而不是抛出（对齐 single_shot 的 evalJS）。"""
    try:
        return await page.evaluate(expr)
    except Exception as e:
        return {"__err": str(e)[:200]}


async def _native_click(page, pos: dict):
    """CDP 原生点击（mouseMoved→Pressed→Released），合成 click 对下拉无效的坑已绕开。"""
    await page.mouse.move(pos["x"], pos["y"])
    await page.mouse.down()
    await page.mouse.up()


# ---- 页面内 JS（移植自 single_shot_5.js / poll5.js）----

_JS_BANNER = ("(()=>{[...document.querySelectorAll('button')]"
              ".filter(b=>/我知道了|同意|接受|Got it|Accept|^OK$/i.test((b.innerText||'').trim())"
              "&&b.offsetParent!==null).forEach(b=>b.click());return 1;})()")

# 2026-10-06：Dola 前端把 tiptap ProseMirror 编辑器换成了普通 <textarea placeholder="Message...">，
# 检测/输入全部做双兼容：textarea 优先，ProseMirror 兜底。
_JS_EDITOR = ("!!(document.querySelector('textarea[placeholder^=\"Message\"]') || "
              "document.querySelector('.tiptap.ProseMirror'))")

_JS_IN_VIDEO_MODE = ("[...document.querySelectorAll('button,[role=button],[class*=cursor-pointer],div,span')]"
                     ".some(e=>e.offsetParent!==null&&(/^(模型|比例)$/.test((e.innerText||'').trim())"
                     "||/^模型\\s*\\d/.test((e.innerText||'').replace(/\\s+/g,' '))))")

_JS_VIDEO_CHIP = ("(()=>{const el=[...document.querySelectorAll('button,[role=button],[class*=cursor-pointer],div,span')]"
                  ".filter(e=>e.offsetParent!==null&&/^(视频生成|影片生成)$/.test((e.innerText||'').trim()))"
                  ".sort((a,b)=>a.innerText.length-b.innerText.length)[0];"
                  "if(!el) return null;const r=el.getBoundingClientRect();"
                  "return {x:r.x+r.width/2,y:r.y+r.height/2};})()")

_JS_CHIP = ("(()=>{const els=[...document.querySelectorAll('button,[role=button]')]"
            ".filter(e=>e.offsetParent!==null&&/^模型/.test((e.innerText||'').trim())&&(e.innerText||'').length<20);"
            "if(!els.length) return null;"
            "const best=els.sort((a,b)=>b.innerText.length-a.innerText.length)[0];"
            "const r=best.getBoundingClientRect();"
            "return {x:r.x+r.width/2,y:r.y+r.height/2,text:best.innerText.replace(/\\s+/g,' ')};})()")

_JS_MODEL_OPT = ("(()=>{const els=[...document.querySelectorAll('*')]"
                 ".filter(e=>e.offsetParent!==null&&e.children.length<=1"
                 "&&/Seedance\\s*2\\.5/i.test(e.innerText||'')&&(e.innerText||'').length<80);"
                 "const el=els[els.length-1];if(!el) return null;"
                 "const r=el.getBoundingClientRect();"
                 "return {x:r.x+r.width/2,y:r.y+r.height/2};})()")

_JS_LOGOUT = ("location.href.includes('logout')||/请登录|Please log in|请先登录/"
              ".test(document.body.innerText||'')")

_JS_SEND_BTN = ("(()=>{const b=document.querySelector('[data-testid=chat_input_send_button]');"
                "if(!b) return null;const r=b.getBoundingClientRect();"
                "return {x:r.x+r.width/2,y:r.y+r.height/2,disabled:b.disabled};})()")

_JS_HOOK_LOG = "JSON.stringify((window.__dola30sLog||[]).slice(-8))"

# poll5.js 的 React fiber 挖掘表达式（原样移植）
_JS_POLL = """(async()=>{
  function walk(node,out,depth){
    if(!node||depth>60||out.length>6) return;
    const fk=Object.keys(node).find(k=>k.startsWith('__reactFiber'));
    if(fk){
      let f=node[fk];
      for(let hop=0;hop<8&&f;hop++){
        const p=f.memoizedProps;
        if(p&&typeof p==='object'){
          const cand=[p.video,p.data&&p.data.video,p.message&&(p.message.video||(p.message.content&&p.message.content.video))];
          for(const v of cand){
            if(v&&typeof v==='object'){
              const vl=v.video_list||v.videoList;
              if(vl&&vl.video_1){ const u=vl.video_1.download_url||vl.video_1.main_url||vl.video_1.url; if(u) out.push(u); }
              for(const k of ['download_url','main_url','video_url','play_url','url']){
                if(typeof v[k]==='string'&&/^https?:\\/\\//.test(v[k])&&/video|mp4|tos/.test(v[k])) out.push(v[k]);
              }
            }
          }
        }
        f=f.return;
      }
    }
    for(const ch of node.children||[]) walk(ch,out,depth+1);
  }
  const out=[]; walk(document.body,out,0);
  const t=(document.body.innerText||'');
  return JSON.stringify({
    urls:[...new Set(out)].slice(0,3),
    done:/影片生成完畢|影片生成完毕|生成完畢|生成完毕|Download/.test(t),
    logout:location.href.includes('logout'),
    unavailable:/暂时无法使用|temporarily unavailable|系統繁忙|系统繁忙|稍后再试|稍後再試/i.test(t),
    quota:/余額不足|余额不足|额度不足|配額|配额/.test(t),
    tail:t.slice(-260).replace(/\\s+/g,' ')
  });
})()"""


def _typed_expr(prompt: str) -> str:
    """编辑器输入，返回输入后守卫所需的现场。

    新版 Dola 是 <textarea>（React 受控组件，必须用原生 setter + input 事件）；
    旧版 tiptap ProseMirror 走 execCommand。
    """
    return """(async()=>{
    const ta=document.querySelector('textarea[placeholder^="Message"]');
    const ed=ta||document.querySelector('.tiptap.ProseMirror');
    if(!ed) return {err:'editor not found'};
    ed.focus();
    const TEXT=%s;
    if(ed.tagName==='TEXTAREA'){
      const setter=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;
      setter.call(ed,TEXT);
      ed.dispatchEvent(new Event('input',{bubbles:true}));
    }else{
      document.execCommand('selectAll',false,null);
      document.execCommand('delete',false,null);
      document.execCommand('insertText',false,TEXT);
    }
    await new Promise(r=>setTimeout(r,1200));
    const t=(ed.value!==undefined?ed.value:ed.textContent).trim();
    const btn=document.querySelector('[data-testid=chat_input_send_button]');
    return {text:t.slice(0,30),len:t.length,count:(document.body.innerText.match(new RegExp(%s,'g'))||[]).length,btnDisabled:btn?btn.disabled:null};
  })()""" % (json.dumps(prompt), json.dumps(prompt[:12]))


def _ffprobe_duration(path: str) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except Exception:
        return None


def _download(url: str, dest: Path, proxy_url: str) -> Path:
    """下载成片：默认本地流量直连（config.DOWNLOAD_DIRECT），直连失败回退账号代理。

    成片体积大（30s 约 20~60MB），不再消耗 IPRocket 代理流量；
    设 DOLA_DOWNLOAD_DIRECT=0 恢复「全走账号代理」旧行为。
    requests+PySocks 支持账密 SOCKS5。
    """
    import requests
    socks = proxy_url.replace("socks5h://", "socks5://") if proxy_url else ""
    proxies = {"http": socks, "https": socks} if socks else None
    attempts = []
    if config.DOWNLOAD_DIRECT:
        attempts.append(None)           # 本地直连优先（省代理流量）
        if proxies:
            attempts.append(proxies)    # 直连失败回退账号代理
    else:
        attempts.append(proxies)
    last = None
    for try_proxies in attempts:
        for attempt in range(3):
            try:
                with requests.get(url, proxies=try_proxies, timeout=180, stream=True) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            f.write(chunk)
                if try_proxies is None:
                    print(f"   Download: 本地直连完成（未耗代理流量） {dest.name}", flush=True)
                return dest
            except Exception as e:
                last = e
                time.sleep(2)
        if try_proxies is None:
            print(f"   Download: 直连失败，回退账号代理（{last}）", flush=True)
    raise RuntimeError(f"视频下载失败: {last}")


def _decode_url(u: str) -> str:
    """文档：fiber 里的 main_url 可能是 base64，解码后才是直链。"""
    u = (u or "").strip()
    if u.startswith("http"):
        return u
    try:
        pad = "=" * (-len(u) % 4)
        d = base64.b64decode(u + pad).decode("utf-8", "ignore")
        return d if d.startswith("http") else u
    except Exception:
        return u


async def generate_video_30(account: str, prompt: str, *,
                            model: str = "seedance-2.5",
                            cookie_state_path: str,
                            proxy_url: str,
                            on_poll=None,
                            max_wait: int = 1800,
                            target_duration: int = 30,
                            reference_image_paths: list[str] | None = None,
                            headless: bool = False) -> dict:
    """跑一次浏览器通道出片（默认 30s；4~30s 均可，hook 按目标时长改写），
    返回与纯 API 兼容的 result dict。

    2026-10-07 扩展：API 任务 4-30s 全部路由到本通道（页面默认档计价 2 点），
    支持参考图上传（复用 video_worker_ui.attach_reference_images，时序对齐：
    视频模式 → 传图 → 输入提示词 → 发送）。
    """
    from patchright.async_api import async_playwright

    target = int(target_duration or 30)
    state = json.loads(Path(cookie_state_path).read_text(encoding="utf-8"))
    cookies = []
    for c in state.get("cookies_list") or []:
        cookies.append({
            "name": c["name"], "value": c["value"],
            "domain": c.get("domain") or ".dola.com", "path": c.get("path") or "/",
            "secure": True,
        })
    if not cookies:
        raise LoginExpiredError(f"{account} 没有 cookie，无法走浏览器出片通道")

    fwd = await proxy_fwd.SocksForwarder.start(proxy_url)
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                proxy={"server": f"socks5://127.0.0.1:{fwd.port}"},
                args=LAUNCH_ARGS)
            ctx = await browser.new_context(viewport={"width": 1440, "height": 900},
                                            locale="en-US")
            await ctx.add_init_script(_hook_source(target))
            await ctx.add_cookies(cookies)
            page = await ctx.new_page()
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)

            # 1. 等编辑器（30 轮 x2s + reload 自愈，Dola 边缘抖动/慢加载都要兜住）
            ok = False
            for i in range(30):
                st = await _eval(page, """(()=>{
                  const ed=!!document.querySelector('.tiptap.ProseMirror');
                  return JSON.stringify({ed, url:location.href.slice(0,90),
                    title:document.title.slice(0,40),
                    logout:location.href.includes('logout'),
                    ready:document.readyState});})()""")
                try:
                    d = json.loads(st) if isinstance(st, str) else {}
                except Exception:
                    d = {}
                if i % 3 == 0 or d.get("ed"):
                    print(f"[browser30:{account}] wait_editor[{i}] ed={d.get('ed')} "
                          f"url={d.get('url')} logout={d.get('logout')} ready={d.get('ready')}",
                          flush=True)
                if d.get("ed") is True:
                    ok = True
                    break
                # Dola 边缘节点偶发「temporarily unavailable」页：自动 reload 自愈
                body = await _eval(page, "document.body ? document.body.innerText.slice(0,200) : ''")
                if isinstance(body, str) and "temporarily unavailable" in body:
                    print(f"[browser30:{account}] 检测到 unavailable 页，reload 重试（第 {i} 轮）",
                          flush=True)
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception as exc:
                        print(f"[browser30:{account}] reload 失败: {exc}", flush=True)
                await asyncio.sleep(2)
            if not ok:
                try:
                    shot = Path(config.DOWNLOAD_DIR).parent / f"debug_{account}_noeditor.png"
                    await page.screenshot(path=str(shot))
                    print(f"[browser30:{account}] 编辑器未出现，截图: {shot}", flush=True)
                except Exception:
                    pass
                raise AbnormalNoAckError(f"{account} 浏览器 30s：编辑器未出现（页面加载/登录态异常）")

            # 2. 关横幅 + 聚焦（textarea 或 ProseMirror）
            await _eval(page, _JS_BANNER)
            await asyncio.sleep(0.8)
            await page.evaluate(
                "(document.querySelector('textarea[placeholder^=\"Message\"]') || "
                "document.querySelector('.tiptap.ProseMirror')).focus()")
            await asyncio.sleep(1.5)

            # 3. 激活视频生成 chip（已在视频模式则跳过）
            in_video = await _eval(page, _JS_IN_VIDEO_MODE)
            if in_video is not True:
                for _ in range(3):
                    pos = await _eval(page, _JS_VIDEO_CHIP)
                    if not pos or pos.get("__err") or not pos.get("x"):
                        await asyncio.sleep(2)
                        continue
                    await _native_click(page, pos)
                    await asyncio.sleep(2.5)
                    if await _eval(page, _JS_IN_VIDEO_MODE) is True:
                        break

            # 4. 选 Seedance 2.5（最多 4 轮，硬守卫）
            model_now = ""
            for _attempt in range(4):
                chip = await _eval(page, _JS_CHIP)
                if not chip or chip.get("__err") or not chip.get("x"):
                    await asyncio.sleep(2)
                    continue
                model_now = str(chip.get("text") or "")
                if "2.5" in model_now:
                    break
                await _native_click(page, chip)
                await asyncio.sleep(2.2)
                opt = await _eval(page, _JS_MODEL_OPT)
                if opt and not opt.get("__err") and opt.get("x"):
                    await _native_click(page, opt)
                    await asyncio.sleep(1.8)
                re_chip = await _eval(page, _JS_CHIP)
                model_now = str(re_chip.get("text") or "unknown") if re_chip else "unknown"
                if "2.5" in model_now:
                    break
                await asyncio.sleep(1.5)
            if "2.5" not in model_now:
                # 硬守卫 A：模型不是 2.5 坚决不发（保号）
                raise RuntimeError(f"{account} 浏览器 30s：模型芯片不是 2.5（现在是 {model_now!r}），守卫拦截未发送")

            # 4.5 上传参考图（时序对齐 login 浏览器通道：视频模式 → 传图 → 输入提示词）。
            # attach_reference_images 内部等待 prepare_upload/TOS 回执 + 缩略图出现，
            # 失败抛 TimeoutError —— 参考图没传上去就发送 = 文生视频，属于静默降档，必须拦。
            if reference_image_paths:
                # 惰性导入：测试套件给 video_worker_ui 打了桩（无此函数），
                # 且只有真跑带图任务才需要；服务进程里该模块始终完整可用。
                from video_worker_ui import attach_reference_images
                await attach_reference_images(page, [str(p) for p in reference_image_paths])

            # 5. 输入提示词 + 硬守卫 B
            typed = await _eval(page, _typed_expr(prompt))
            if not typed or typed.get("__err"):
                raise RuntimeError(f"{account} 浏览器 30s：输入失败 {typed}")
            logout = await _eval(page, _JS_LOGOUT)
            if logout is True:
                raise LoginExpiredError(f"{account} 浏览器 30s：登录态失效（页面提示登出）")
            if typed.get("count") != 1 or (typed.get("len") or 0) == 0:
                raise RuntimeError(
                    f"{account} 浏览器 30s：守卫拦截未发送（count={typed.get('count')} "
                    f"len={typed.get('len')}）")
            # btnDisabled 仅在新 UI 提供 testid 按钮时有效；textarea 旧版可为 null，
            # 只要文本确实输入成功（len>0）就放行，发送阶段有 Enter 兜底。

            # 6. 原生点击发送一次；按钮定位失败则退回 textarea 上按 Enter
            btn = await _eval(page, _JS_SEND_BTN)
            if btn and not btn.get("__err") and btn.get("x"):
                await _native_click(page, btn)
            else:
                print(f"[browser30:{account}] send 按钮未定位，改用 Enter 提交", flush=True)
                await page.keyboard.press("Enter")
            await asyncio.sleep(9)

            # 7. 回读钩子日志，确认请求真以 30s 发出
            log_raw = await _eval(page, _JS_HOOK_LOG)
            try:
                hook_log = json.loads(log_raw) if isinstance(log_raw, str) else (log_raw or [])
            except Exception:
                hook_log = []
            patched = [e for e in hook_log if isinstance(e, dict) and "patched" in e]
            if not patched:
                # 硬守卫 C：没确凿证据以 30s 提交 → 按失败处理（10s 片对我们没意义）
                raise HookNotPatchedError(
                    f"{account} 浏览器 30s：发送后钩子日志无 patched 记录（log={str(hook_log)[:200]}）")

            # 8. 轮询 fiber 挖直链
            deadline = time.time() + max_wait
            poll_n = 0
            while time.time() < deadline:
                poll_n += 1
                st = await _eval(page, _JS_POLL)
                if on_poll:
                    try:
                        on_poll(time.time())
                    except Exception:
                        pass
                if isinstance(st, dict) and not st.get("__err"):
                    if st.get("logout"):
                        raise LoginExpiredError(f"{account} 浏览器 30s：轮询中发现登出")
                    if st.get("quota"):
                        raise CreditInsufficientError(f"{account} 浏览器 30s：上游提示余额不足")
                    urls = [_decode_url(u) for u in (st.get("urls") or []) if u]
                    urls = [u for u in urls if u.startswith("http")]
                    if urls:
                        out = Path(config.DOWNLOAD_DIR) / f"{account}_{target}s_{int(time.time())}.mp4"
                        out.parent.mkdir(parents=True, exist_ok=True)
                        await asyncio.to_thread(_download, urls[0], out, proxy_url)
                        dur = await asyncio.to_thread(_ffprobe_duration, str(out))
                        if dur and dur < target * 0.9:
                            # 目标时长只要原生成片：短视频换号重试（与纯 API 的 ShortClipError 同口径）
                            raise ShortClipError(
                                f"{account} 浏览器 30s：上游只给了 {dur:.1f}s 短视频（目标 {target}s），不拼接换号重试")
                        return {
                            "local_path": str(out),
                            "account": account,
                            "conversation_id": "",
                            "download_url": urls[0],
                            "duration": target,
                            "model": model,
                            "pure_api": False,
                            "browser30": True,
                        }
                    if st.get("unavailable"):
                        raise AbnormalNoAckError(
                            f"{account} 浏览器 30s：上游提示暂时不可用（{str(st.get('tail'))[:120]}）")
                await asyncio.sleep(15)
            # 超时：不换号直接失败（Dola 端可能仍在生成，重提会重复扣额度）
            raise TimeoutError(f"{account} 浏览器 30s：{max_wait} 秒内未出片（钩子已确认 30s 提交）")
    finally:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        await fwd.stop()
