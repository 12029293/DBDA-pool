// px_hold_crack.js — 纯协议破解 PerimeterX "按住(press&hold)" 挑战的经验性尝试。
//
// 思路：不重写 PX 的加密/PoW，而是把 PX 官方代码 (main.min.js 静默传感器 + captcha.js 挑战 SDK)
// 跑在同一个 jsdom(window) 里、绑定同一个 vid，让 PX 自己算签名/PoW/PX257，
// 我们只负责：(a) 补齐指纹/Pointer/WASM/rAF 环境，(b) 合成一次"真人按住"的指针事件序列，
// (c) 把所有 collector 请求/响应(含 do[] 指令与 Set-Cookie)全量记录，(d) 收割 _px3。
//
// 用法:
//   node px_hold_crack.js                # 直连(无代理)，跑 silent+press，日志写 px_hold_crack.log
//   PX_PROXY=http://user:pass@host:port node px_hold_crack.js   # 走住宅代理(与注册同IP才可能被微软认)
//   PX_VID=<uuid> node px_hold_crack.js  # 绑定微软 challenge 下发的 vid (同 vid 全流程)
'use strict';
const fs = require('fs');
const path = require('path');
const https = require('https');
const { JSDOM, VirtualConsole } = require('jsdom');

const APP = process.env.PX_APP_ID || 'PXzC5j78di';
const UA = process.env.PX_UA ||
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const PROXY = process.env.PX_PROXY || '';
let agent;
if (PROXY) { const { HttpsProxyAgent } = require('https-proxy-agent'); agent = new HttpsProxyAgent(PROXY); }

const LOGFILE = path.join(__dirname, process.env.PX_LOG || 'px_hold_crack.log');
try { fs.writeFileSync(LOGFILE, ''); } catch {}
function log(...a) {
  const line = a.map(x => (typeof x === 'string' ? x : JSON.stringify(x))).join(' ');
  process.stdout.write(line + '\n');
  try { fs.appendFileSync(LOGFILE, line + '\n'); } catch {}
}
function uuid() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0; const v = c === 'x' ? r : (r & 0x3 | 0x8); return v.toString(16);
  });
}

const netlog = [];
function rawRequest(method, url, headers, body) {
  return new Promise((resolve) => {
    let u; try { u = new URL(url); } catch { return resolve({ status: 0, body: '', error: 'bad url' }); }
    const opts = {
      method, hostname: u.hostname, path: u.pathname + u.search,
      headers: Object.assign({ 'User-Agent': UA, 'Accept': '*/*' }, headers || {}),
      timeout: 30000,
    };
    if (agent) opts.agent = agent;
    if (u.port) opts.port = u.port;
    const req = https.request(opts, (res) => {
      let data = ''; res.on('data', d => data += d); res.on('end', () => {
        resolve({ status: res.statusCode, body: data, setCookie: res.headers['set-cookie'] || [], headers: res.headers });
      });
    });
    req.on('error', e => resolve({ status: 0, body: '', error: String(e) }));
    req.on('timeout', () => { req.destroy(); resolve({ status: 0, body: '', error: 'timeout' }); });
    if (body) req.write(body);
    req.end();
  });
}

// 判断响应是否是 collector 的 do[] 指令 / 是否含 _px3
function analyzeResponse(url, body, setCookie) {
  const info = { collector: false, doArr: null, px3: '', challenge: false };
  if (/\/api\/v[12]\/collector|\/gw\/|\/pxajax|\/api\/v[12]\/msft/i.test(url)) info.collector = true;
  try {
    const j = JSON.parse(body);
    if (Array.isArray(j.do)) {
      info.doArr = j.do;
      for (const d of j.do) {
        if (typeof d !== 'string') continue;
        if (/^bake\|/.test(d) && /_px3/.test(d)) { const m = d.match(/_px3=([^;|]+)/); if (m) info.px3 = m[1]; }
        if (/^(enforce|block|captcha|challenge)/i.test(d)) info.challenge = true;
      }
    }
    if (j.enforcement || j.blockScript || j.appId && j.uuid && j.vid) info.challenge = true;
  } catch {}
  (setCookie || []).forEach(sc => { const m = String(sc).match(/_px3=([^;]+)/); if (m) info.px3 = m[1]; });
  return info;
}

function buildDom() {
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => log('  [jsdomError]', (e && (e.detail && e.detail.stack || e.message)) ? String(e.detail && e.detail.stack || e.message).slice(0, 300) : String(e).slice(0, 300)));
  ['error', 'warn'].forEach(lvl => vc.on(lvl, (...a) => log('  [console.' + lvl + ']', a.map(x => String(x)).join(' ').slice(0, 300))));
  const dom = new JSDOM(
    '<!DOCTYPE html><html><head></head><body><div id="px-captcha"></div></body></html>',
    { url: 'https://signup.live.com/', referrer: 'https://signup.live.com/', pretendToBeVisual: true, runScripts: 'outside-only', userAgent: UA, virtualConsole: vc });
  const { window } = dom; const doc = window.document;

  // ---- WebAssembly 暴露 (captcha.js 的 PoW 需要) ----
  try { window.WebAssembly = WebAssembly; } catch {}
  // ---- performance / timing ----
  try { if (!window.performance) window.performance = {}; if (!window.performance.now) window.performance.now = () => Date.now(); } catch {}

  // ---- navigator 指纹 ----
  const nav = window.navigator;
  const dn = (k, v) => { try { Object.defineProperty(nav, k, { get: () => v, configurable: true }); } catch {} };
  dn('hardwareConcurrency', 8); dn('deviceMemory', 8); dn('platform', 'Win32');
  dn('language', 'en-US'); dn('languages', ['en-US', 'en']); dn('webdriver', false);
  dn('vendor', 'Google Inc.'); dn('maxTouchPoints', 0);
  try { window.chrome = { runtime: {}, app: {}, csi: () => {}, loadTimes: () => {} }; } catch {}
  try { Object.defineProperties(window.screen, { width:{get:()=>1920}, height:{get:()=>1080}, availWidth:{get:()=>1920}, availHeight:{get:()=>1040}, colorDepth:{get:()=>24}, pixelDepth:{get:()=>24} }); } catch {}
  try { window.devicePixelRatio = 1; } catch {}

  // ---- PointerEvent polyfill (jsdom 无) ----
  if (typeof window.PointerEvent === 'undefined') {
    class PointerEvent extends window.MouseEvent {
      constructor(type, params = {}) {
        super(type, params);
        this.pointerId = params.pointerId ?? 1;
        this.pointerType = params.pointerType ?? 'mouse';
        this.pressure = params.pressure ?? (type === 'pointerup' ? 0 : 0.5);
        this.width = params.width ?? 1; this.height = params.height ?? 1;
        this.isPrimary = params.isPrimary ?? true;
        this.tiltX = 0; this.tiltY = 0; this.twist = 0;
      }
    }
    try { window.PointerEvent = PointerEvent; } catch {}
  }
  try { window.HTMLElement.prototype.setPointerCapture = function(){}; window.HTMLElement.prototype.releasePointerCapture = function(){}; } catch {}
  try { window.HTMLElement.prototype.getBoundingClientRect = function(){ return { x:150, y:300, left:150, top:300, right:450, bottom:360, width:300, height:60, toJSON(){return this;} }; }; } catch {}

  // ---- 渲染门(captchaNotRendered)绕过尝试：伪造已绘制的按钮尺寸/可见性 ----
  if (process.env.PX_RENDER_STUB === '1') {
    try {
      const proto = window.HTMLElement.prototype;
      ['offsetWidth', 'clientWidth'].forEach(p => Object.defineProperty(proto, p, { get() { return 300; }, configurable: true }));
      ['offsetHeight', 'clientHeight'].forEach(p => Object.defineProperty(proto, p, { get() { return 60; }, configurable: true }));
      Object.defineProperty(proto, 'offsetParent', { get() { return window.document.body; }, configurable: true });
      Object.defineProperty(proto, 'isConnected', { get() { return true; }, configurable: true });
    } catch (e) { log('  render-stub offset err', e.message); }
    try {
      const realGCS = window.getComputedStyle.bind(window);
      window.getComputedStyle = function (el, ps) {
        let s; try { s = realGCS(el, ps); } catch { s = {}; }
        const over = { display: 'block', visibility: 'visible', opacity: '1', width: '300px', height: '60px', position: 'relative' };
        return new Proxy(s, { get(t, k) { if (k in over) return over[k]; if (k === 'getPropertyValue') return (n) => (n in over ? over[n] : (t.getPropertyValue ? t.getPropertyValue(n) : '')); const v = t[k]; return typeof v === 'function' ? v.bind(t) : v; } });
      };
    } catch (e) { log('  render-stub gcs err', e.message); }
    try {
      window.IntersectionObserver = class { constructor(cb){ this._cb = cb; } observe(el){ try { this._cb([{ isIntersecting: true, intersectionRatio: 1, target: el, boundingClientRect: el.getBoundingClientRect(), intersectionRect: el.getBoundingClientRect() }], this); } catch {} } unobserve(){} disconnect(){} takeRecords(){ return []; } };
      window.ResizeObserver = class { observe(){} unobserve(){} disconnect(){} };
    } catch (e) { log('  render-stub obs err', e.message); }
    log('  [render-stub] 已启用(offset/gcs/IO 伪造)');
  }

  // ---- canvas / WebGL 桩 ----
  const HC = window.HTMLCanvasElement && window.HTMLCanvasElement.prototype;
  if (HC) {
    HC.getContext = function (type) {
      if (type === '2d') return { fillRect(){}, fillText(){}, strokeText(){}, arc(){}, beginPath(){}, closePath(){}, fill(){}, stroke(){}, measureText:()=>({width:100}), getImageData:()=>({data:new Uint8ClampedArray(4)}), rect(){}, save(){}, restore(){}, translate(){}, rotate(){}, scale(){}, setTransform(){}, isPointInPath:()=>false, createLinearGradient:()=>({addColorStop(){}}), putImageData(){}, drawImage(){}, font:'', textBaseline:'', fillStyle:'', strokeStyle:'' };
      const gl = { getParameter:(p)=>{ if(p===37445)return 'Google Inc. (NVIDIA)'; if(p===37446)return 'ANGLE (NVIDIA GeForce GTX 1060 Direct3D11 vs_5_0 ps_5_0)'; return 'WebGL'; }, getExtension:()=>({UNMASKED_VENDOR_WEBGL:37445, UNMASKED_RENDERER_WEBGL:37446}), getSupportedExtensions:()=>['WEBGL_debug_renderer_info'], getShaderPrecisionFormat:()=>({precision:23,rangeMin:127,rangeMax:127}), createBuffer(){}, bindBuffer(){}, bufferData(){}, createProgram(){}, createShader(){}, shaderSource(){}, compileShader(){}, attachShader(){}, linkProgram(){}, useProgram(){}, getAttribLocation:()=>0, enableVertexAttribArray(){}, vertexAttribPointer(){}, drawArrays(){}, VENDOR:7936, RENDERER:7937, VERSION:7938 };
      return gl;
    };
    HC.toDataURL = () => 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
  }

  // ---- 网络拦截：全部真发(直连或代理)，全量日志 ----
  const capturedCookies = {};
  const onResp = (via, method, abs, r) => {
    if (/clientError\?r=/.test(abs)) {
      try { const raw = decodeURIComponent(abs.split('clientError?r=')[1]); log('  [PX clientError]', raw.slice(0, 400)); } catch {}
    }
    const info = analyzeResponse(abs, r.body || '', r.setCookie);
    netlog.push({ via, method, url: abs, status: r.status, doArr: info.doArr, px3: info.px3, challenge: info.challenge, respHead: (r.body || '').slice(0, 240) });
    (r.setCookie || []).forEach(sc => { try { doc.cookie = sc; } catch {} });
    if (info.px3) { capturedCookies['_px3'] = info.px3; log('  [★ _px3 收割]', info.px3.slice(0, 70)); }
    if (info.doArr) log('  [collector do]', method, abs.replace(/^https:\/\//,'').slice(0,60), '→', JSON.stringify(info.doArr).slice(0, 220));
  };

  class FakeXHR {
    constructor(){ this.headers={}; this.readyState=0; this._m='GET'; this._u=''; this.status=0; this.responseText=''; this.response=''; }
    open(m,u){ this._m=m; this._u=u; this.readyState=1; if(this.onreadystatechange)this.onreadystatechange(); }
    setRequestHeader(k,v){ this.headers[k]=v; }
    getAllResponseHeaders(){ return ''; } getResponseHeader(){ return null; }
    send(body){
      const abs = this._u.startsWith('http') ? this._u : ('https://collector-'+APP.toLowerCase()+'.hsprotect.net'+(this._u.startsWith('/')?'':'/')+this._u);
      netlog.push({ via:'xhr-req', method:this._m, url:abs, body: body?String(body).slice(0,160):'' });
      rawRequest(this._m, abs, this.headers, body).then(r=>{
        this.status=r.status; this.responseText=r.body||''; this.response=this.responseText; this.readyState=4;
        onResp('xhr', this._m, abs, r);
        if(this.onreadystatechange)this.onreadystatechange(); if(this.onload)this.onload();
      });
    }
    addEventListener(ev,cb){ if(ev==='load')this.onload=cb; if(ev==='readystatechange')this.onreadystatechange=cb; }
    abort(){}
  }
  window.XMLHttpRequest = FakeXHR;
  window.fetch = function(url, opts){
    opts = opts||{}; const abs = String(url).startsWith('http') ? String(url) : ('https://collector-'+APP.toLowerCase()+'.hsprotect.net'+url);
    netlog.push({ via:'fetch-req', method:opts.method||'GET', url:abs, body: opts.body?String(opts.body).slice(0,160):'' });
    return rawRequest(opts.method||'GET', abs, opts.headers, opts.body).then(r=>{
      onResp('fetch', opts.method||'GET', abs, r);
      return { ok:r.status>=200&&r.status<300, status:r.status, text:()=>Promise.resolve(r.body), json:()=>Promise.resolve(JSON.parse(r.body||'{}')) };
    });
  };
  try { window.navigator.sendBeacon = function(url, data){ const abs=String(url).startsWith('http')?String(url):('https://collector-'+APP.toLowerCase()+'.hsprotect.net'+url); netlog.push({via:'beacon-req',url:abs,body:data?String(data).slice(0,160):''}); rawRequest('POST', abs, {'Content-Type':'text/plain'}, data).then(r=>onResp('beacon','POST',abs,r)); return true; }; } catch {}
  const OrigImage = window.Image;
  window.Image = function(){ const img = new OrigImage(); try{ Object.defineProperty(img,'src',{set(v){ netlog.push({via:'img',url:String(v).slice(0,140)}); if(String(v).startsWith('http')) rawRequest('GET', String(v), {}, null).then(r=>onResp('img','GET',String(v),r)); }, get(){return '';}}); }catch{} return img; };

  // cookie 捕获
  try {
    const cd = Object.getOwnPropertyDescriptor(window.Document.prototype, 'cookie') || Object.getOwnPropertyDescriptor(Object.getPrototypeOf(doc), 'cookie');
    const rs = cd.set.bind(doc), rg = cd.get.bind(doc);
    Object.defineProperty(doc, 'cookie', { get(){ return rg(); }, set(v){ try{ const m=String(v).match(/^([^=]+)=([^;]*)/); if(m){ capturedCookies[m[1]]=m[2]; if(m[1].startsWith('_px')) log('  [cookie set]', m[1], '=', m[2].slice(0,50)); } }catch{} rs(v); }, configurable:true });
  } catch {}

  return { dom, window, doc, capturedCookies };
}

async function synthPress(window, doc) {
  // 合成"真人按住"：pointerdown → rAF 期间微抖 → pointerup。
  // 关键(warterbili gotchas): 坐标用浮点; pressDuration ∈ [1000,3000]ms 且 == up.ts-down.ts;
  // 期间有 rAF ticks(节奏抖动) + pointermove 微动(非静止)。
  const el = doc.getElementById('px-captcha') || doc.body;
  const rect = el.getBoundingClientRect();
  const cx = rect.left + rect.width * (0.45 + Math.random() * 0.1);
  const cy = rect.top + rect.height * (0.45 + Math.random() * 0.1);
  const fire = (type, x, y, extra = {}) => {
    try {
      const ev = new window.PointerEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, screenX: x, screenY: y, button: 0, buttons: type === 'pointerup' ? 0 : 1, pointerId: 1, pointerType: 'mouse', isPrimary: true, ...extra });
      el.dispatchEvent(ev);
      // 同时派发对应 mouse 事件(部分逻辑监听 mousedown/mouseup)
      const mt = type === 'pointerdown' ? 'mousedown' : type === 'pointerup' ? 'mouseup' : 'mousemove';
      const me = new window.MouseEvent(mt, { bubbles: true, cancelable: true, clientX: x, clientY: y, screenX: x, screenY: y, button: 0, buttons: type === 'pointerup' ? 0 : 1 });
      el.dispatchEvent(me);
    } catch (e) { log('  fire err', type, e.message); }
  };
  // 靠近轨迹(浮点)
  for (let i = 0; i < 12; i++) {
    const t = i / 12;
    fire('pointermove', cx - 60 + 60 * t + (Math.random() - 0.5) * 1.7, cy - 30 + 30 * t + (Math.random() - 0.5) * 1.7);
    await new Promise(r => setTimeout(r, 8 + Math.random() * 18));
  }
  const downTs = window.performance.now();
  fire('pointerdown', cx, cy, { pressure: 0.5 });
  log('  [press] pointerdown at (%s,%s)', cx.toFixed(2), cy.toFixed(2));
  const holdMs = 1400 + Math.random() * 1300; // 1.4–2.7s
  const raf = window.requestAnimationFrame ? window.requestAnimationFrame.bind(window) : (cb) => setTimeout(() => cb(window.performance.now()), 16);
  await new Promise(resolve => {
    const start = window.performance.now();
    const tick = () => {
      const el2 = window.performance.now() - start;
      // 真人按住微抖(浮点、非静止)
      fire('pointermove', cx + Math.sin(el2 / 90) * 1.3 + (Math.random() - 0.5) * 0.8, cy + Math.cos(el2 / 130) * 1.0 + (Math.random() - 0.5) * 0.8, { pressure: 0.5 + Math.random() * 0.05 });
      if (el2 >= holdMs) return resolve();
      raf(tick);
    };
    raf(tick);
  });
  const upTs = window.performance.now();
  fire('pointerup', cx + (Math.random() - 0.5) * 0.6, cy + (Math.random() - 0.5) * 0.6, { pressure: 0 });
  log('  [press] pointerup, held=%sms (down..up)', (upTs - downTs).toFixed(0));
}

async function main() {
  const vid = process.env.PX_VID || uuid();
  const uid = uuid(), sid = uuid();
  log('=== px_hold_crack 开始 ===  app=%s vid=%s proxy=%s', APP, vid, PROXY ? PROXY.replace(/:[^:@]+@/, ':***@') : '(直连)');

  const { window, doc, capturedCookies } = buildDom();

  try { window.onerror = function (msg, src, ln, col, err) { log('  [window.onerror]', String(msg).slice(0, 200), (err && err.stack ? String(err.stack).slice(0, 200) : '')); }; } catch {}
  try { window.addEventListener('unhandledrejection', e => log('  [unhandledrejection]', String(e && e.reason && (e.reason.stack || e.reason)).slice(0, 200))); } catch {}

  // bootstrap 全局(PX 引导)
  window._pxAppId = APP; window._pxVid = vid; window._pxUuid = uid; window._pxParam1 = sid;
  window['_px' + APP] = undefined;
  window[APP + '_asyncInit'] = function (px) {
    log('  [asyncInit] px keys =', px && typeof px === 'object' ? Object.keys(px).slice(0, 30) : typeof px);
  };

  // ---- Phase 1: silent 传感器 (main.min.js) ----
  const mainPath = fs.existsSync(path.join(__dirname, 'live_main.min.js')) ? 'live_main.min.js' : 'main.min.js';
  log('\n[Phase1] 运行 %s (silent 传感器)…', mainPath);
  try { window.eval(fs.readFileSync(path.join(__dirname, mainPath), 'utf8')); } catch (e) { log('  main eval 抛错:', e && e.message); }
  const fire = (t, n) => { try { t.dispatchEvent(new window.Event(n)); } catch {} };
  await new Promise(r => setTimeout(r, 600));
  fire(doc, 'DOMContentLoaded'); fire(window, 'DOMContentLoaded'); fire(window, 'load');
  for (let i = 0; i < 10; i++) { try { const ev = new window.MouseEvent('mousemove', { clientX: 100 + i * 9, clientY: 200 + i * 4, bubbles: true }); doc.dispatchEvent(ev); } catch {} await new Promise(r => setTimeout(r, 40)); }
  await new Promise(r => setTimeout(r, 4000));
  // flush
  try { Object.defineProperty(doc, 'visibilityState', { get: () => 'hidden', configurable: true }); } catch {}
  try { Object.defineProperty(doc, 'hidden', { get: () => true, configurable: true }); } catch {}
  fire(doc, 'visibilitychange'); fire(window, 'pagehide'); fire(window, 'blur');
  await new Promise(r => setTimeout(r, 3000));

  const silentPx3 = capturedCookies['_px3'] || '';
  log('[Phase1 结果] silent _px3 = %s', silentPx3 ? (silentPx3.slice(0, 60) + '…') : '(无)');

  // ---- Phase 2: press 挑战 (captcha.js) 同 vid ----
  const capPath = fs.existsSync(path.join(__dirname, 'live_captcha.js')) ? 'live_captcha.js' : 'captcha.js';
  log('\n[Phase2] 运行 %s (press 挑战 SDK) 同 vid=%s…', capPath, vid);
  try { window.eval(fs.readFileSync(path.join(__dirname, capPath), 'utf8')); } catch (e) { log('  captcha eval 抛错:', e && e.message); }
  await new Promise(r => setTimeout(r, 2500));
  // 触发挑战 UI 后合成按住(即便无 UI 也尝试派发，观察是否触发 collector solve)
  try { await synthPress(window, doc); } catch (e) { log('  synthPress 抛错:', e && e.message); }
  await new Promise(r => setTimeout(r, 5000));
  // 再 flush 一次
  fire(doc, 'visibilitychange'); fire(window, 'pagehide');
  await new Promise(r => setTimeout(r, 3000));

  // ---- 汇总 ----
  log('\n=== 网络活动汇总 (%d 条) ===', netlog.length);
  for (const n of netlog) {
    const tag = n.via.padEnd(10);
    const host = (n.url || '').replace(/^https?:\/\//, '').slice(0, 70);
    let extra = '';
    if (n.status !== undefined) extra += ' →' + n.status;
    if (n.px3) extra += ' [PX3!]';
    if (n.challenge) extra += ' [CHALLENGE]';
    if (n.doArr) extra += ' do=' + JSON.stringify(n.doArr).slice(0, 120);
    log(' ', tag, (n.method || '').padEnd(4), host, extra);
  }
  const px = Object.keys(capturedCookies).filter(k => k.startsWith('_px'));
  log('\n=== 捕获的 _px* cookie ===');
  if (px.length) px.forEach(k => log('  ', k, '=', String(capturedCookies[k]).slice(0, 80)));
  else log('  (无)');
  const finalPx3 = capturedCookies['_px3'] || '';
  log('\n=== 结论 ===');
  log('  final _px3   :', finalPx3 ? finalPx3.slice(0, 80) : '(无)');
  log('  含 :1000: 段 :', finalPx3.includes(':1000:') ? 'YES (挑战已解标记)' : 'NO');
  log('  vid          :', vid);
  process.exit(0);
}
main().catch(e => { log('FATAL', e && e.stack || String(e)); process.exit(1); });
