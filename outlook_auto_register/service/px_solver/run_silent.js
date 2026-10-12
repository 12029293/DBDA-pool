// PerimeterX silent sensor 自研执行环境（Node + jsdom）
// 目标：跑 PX 官方 main.min.js，让它自己算签名、POST sensor 到 collector、拿回 _px3。
'use strict';
const fs = require('fs');
const path = require('path');
const https = require('https');
const { JSDOM } = require('jsdom');
const { HttpsProxyAgent } = require('https-proxy-agent');

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';
const PROXY = process.env.PX_PROXY || process.env.HTTP_PROXY || '';
const agent = new HttpsProxyAgent(PROXY);

function uuid() { return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = Math.random()*16|0; const v = c==='x'?r:(r&0x3|0x8); return v.toString(16); }); }

const netlog = [];
function proxyRequest(method, url, headers, body) {
  return new Promise((resolve) => {
    let u; try { u = new URL(url); } catch { return resolve({ status: 0, body: '', error: 'bad url' }); }
    const opts = { method, hostname: u.hostname, path: u.pathname + u.search, headers: Object.assign({ 'User-Agent': UA }, headers || {}), agent, timeout: 25000 };
    if (u.port) opts.port = u.port;
    const req = https.request(opts, (res) => {
      let data = ''; res.on('data', d => data += d); res.on('end', () => {
        const setc = res.headers['set-cookie'] || [];
        resolve({ status: res.statusCode, body: data, setCookie: setc, headers: res.headers });
      });
    });
    req.on('error', e => resolve({ status: 0, body: '', error: String(e) }));
    req.on('timeout', () => { req.destroy(); resolve({ status: 0, body: '', error: 'timeout' }); });
    if (body) req.write(body);
    req.end();
  });
}

async function main() {
  const APP = 'PXzC5j78di';
  const vid = uuid(), uid = uuid(), sid = uuid();
  const dom = new JSDOM('<!DOCTYPE html><html><head></head><body><div id="px-captcha"></div></body></html>', {
    url: 'https://signup.live.com/',
    referrer: 'https://signup.live.com/',
    pretendToBeVisual: true,
    runScripts: 'outside-only',
    userAgent: UA,
  });
  const { window } = dom;
  const doc = window.document;

  // --- navigator 指纹补全 ---
  const nav = window.navigator;
  const defineNav = (k, v) => { try { Object.defineProperty(nav, k, { get: () => v, configurable: true }); } catch {} };
  defineNav('hardwareConcurrency', 8);
  defineNav('deviceMemory', 8);
  defineNav('platform', 'Win32');
  defineNav('language', 'en-US');
  defineNav('languages', ['en-US', 'en']);
  defineNav('webdriver', false);
  defineNav('vendor', 'Google Inc.');
  defineNav('plugins', { length: 5 });
  defineNav('maxTouchPoints', 0);
  try { Object.defineProperty(window, 'chrome', { value: { runtime: {}, app: {}, csi: () => {}, loadTimes: () => {} }, configurable: true }); } catch {}

  // --- screen ---
  try { Object.defineProperties(window.screen, { width:{get:()=>1920}, height:{get:()=>1080}, availWidth:{get:()=>1920}, availHeight:{get:()=>1040}, colorDepth:{get:()=>24}, pixelDepth:{get:()=>24} }); } catch {}
  try { window.devicePixelRatio = 1; } catch {}

  // --- canvas / WebGL 桩 ---
  const HC = window.HTMLCanvasElement && window.HTMLCanvasElement.prototype;
  if (HC) {
    HC.getContext = function (type) {
      if (type === '2d') return { fillRect(){}, fillText(){}, strokeText(){}, arc(){}, beginPath(){}, closePath(){}, fill(){}, stroke(){}, measureText: () => ({ width: 100 }), getImageData: () => ({ data: new Uint8ClampedArray(4) }), rect(){}, save(){}, restore(){}, translate(){}, rotate(){}, scale(){}, setTransform(){}, isPointInPath: () => false, createLinearGradient: () => ({ addColorStop(){} }), putImageData(){}, drawImage(){}, font:'', textBaseline:'', fillStyle:'', strokeStyle:'', globalCompositeOperation:'' };
      // WebGL
      const gl = { getParameter: (p) => { if (p === 37445) return 'Google Inc. (NVIDIA)'; if (p === 37446) return 'ANGLE (NVIDIA GeForce GTX 1060)'; return 'WebGL'; }, getExtension: () => ({ UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 }), getSupportedExtensions: () => ['WEBGL_debug_renderer_info'], getShaderPrecisionFormat: () => ({ precision: 23, rangeMin: 127, rangeMax: 127 }), createBuffer(){}, bindBuffer(){}, bufferData(){}, createProgram(){}, createShader(){}, shaderSource(){}, compileShader(){}, attachShader(){}, linkProgram(){}, useProgram(){}, getAttribLocation: () => 0, enableVertexAttribArray(){}, vertexAttribPointer(){}, drawArrays(){}, VENDOR:7936, RENDERER:7937, VERSION:7938 };
      return gl;
    };
    HC.toDataURL = () => 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
  }

  // --- 网络：XHR / fetch / sendBeacon / Image 全部走代理真发 ---
  class FakeXHR {
    constructor(){ this.headers={}; this.readyState=0; this._method='GET'; this._url=''; this.status=0; this.responseText=''; this.response=''; }
    open(m,u){ this._method=m; this._url=u; this.readyState=1; if(this.onreadystatechange)this.onreadystatechange(); }
    setRequestHeader(k,v){ this.headers[k]=v; }
    getAllResponseHeaders(){ return ''; }
    getResponseHeader(){ return null; }
    send(body){
      const abs = this._url.startsWith('http') ? this._url : ('https://signup.live.com'+(this._url.startsWith('/')?'':'/')+this._url);
      netlog.push({ via:'xhr', method:this._method, url:abs, body: body? String(body).slice(0,300):'' });
      proxyRequest(this._method, abs, this.headers, body).then(r=>{
        this.status=r.status; this.responseText=r.body||''; this.response=this.responseText; this.readyState=4;
        // 若响应 set-cookie 有 _px3 等，写进 document.cookie
        (r.setCookie||[]).forEach(sc=>{ try{ doc.cookie=sc; }catch{} });
        netlog.push({ via:'xhr-resp', url:abs, status:r.status, body:(r.body||'').slice(0,200) });
        if(this.onreadystatechange)this.onreadystatechange();
        if(this.onload)this.onload();
      });
    }
    addEventListener(ev,cb){ if(ev==='load')this.onload=cb; if(ev==='readystatechange')this.onreadystatechange=cb; }
    abort(){}
  }
  window.XMLHttpRequest = FakeXHR;

  window.fetch = function(url, opts){
    opts = opts||{};
    const abs = String(url).startsWith('http')? String(url) : ('https://signup.live.com'+url);
    netlog.push({ via:'fetch', method:opts.method||'GET', url:abs, body: opts.body? String(opts.body).slice(0,300):'' });
    return proxyRequest(opts.method||'GET', abs, opts.headers, opts.body).then(r=>{
      (r.setCookie||[]).forEach(sc=>{ try{ doc.cookie=sc; }catch{} });
      netlog.push({ via:'fetch-resp', url:abs, status:r.status, body:(r.body||'').slice(0,200) });
      return { ok:r.status>=200&&r.status<300, status:r.status, text:()=>Promise.resolve(r.body), json:()=>Promise.resolve(JSON.parse(r.body||'{}')) };
    });
  };
  try { window.navigator.sendBeacon = function(url, data){ netlog.push({ via:'beacon', url:String(url), body:data?String(data).slice(0,300):'' }); proxyRequest('POST', String(url), {'Content-Type':'text/plain'}, data); return true; }; } catch {}

  // Image src 拦截（PX 用 new Image().src 打点）
  const OrigImage = window.Image;
  window.Image = function(){ const img = new OrigImage(); try{ Object.defineProperty(img,'src',{set(v){ netlog.push({via:'img',url:String(v).slice(0,150)}); }, get(){return '';}}); }catch{} return img; };

  // --- cookie 截获 _px3 ---
  const capturedCookies = {};
  try {
    const cookieDesc = Object.getOwnPropertyDescriptor(window.Document.prototype, 'cookie') || Object.getOwnPropertyDescriptor(Object.getPrototypeOf(doc), 'cookie');
    const rawSet = cookieDesc.set.bind(doc); const rawGet = cookieDesc.get.bind(doc);
    Object.defineProperty(doc, 'cookie', { get(){ return rawGet(); }, set(v){ try{ const m=String(v).match(/^([^=]+)=([^;]*)/); if(m){ capturedCookies[m[1]]=m[2]; if(m[1].startsWith('_px')) console.log('  [cookie捕获]', m[1], '=', m[2].slice(0,60)); } }catch{} rawSet(v); }, configurable:true });
  } catch(e){ console.log('cookie hook 失败', e); }

  // --- bootstrap 全局 ---
  window._pxAppId = APP;
  window._pxVid = vid;
  window._pxUuid = uid;
  window._pxParam1 = sid;
  window[APP + '_asyncInit'] = function(px){
    console.log('  [asyncInit] px keys =', px && typeof px==='object' ? Object.keys(px) : typeof px);
    try { if (px && px.Events) console.log('  [Events] keys =', Object.keys(px.Events)); } catch{}
    // 监听所有事件
    try { ['on'].forEach(()=>{}); const origOn = px.Events.on.bind(px.Events); ['challenge','captcha','risk','block','pass','token','sensor','error','*'].forEach(ev=>{ try{ origOn(ev,(...a)=>console.log('  [PX evt]',ev,JSON.stringify(a).slice(0,120))); }catch{} }); } catch{}
    // 尝试调用可能触发 sensor/token 的方法
    try {
      for (const k of Object.keys(px)) {
        if (/token|check|enforce|start|refresh|collect|send|challenge/i.test(k) && typeof px[k]==='function') {
          console.log('  [尝试调用] px.'+k+'()');
          try { const r = px[k](); if (r && r.then) r.then(v=>console.log('   → '+k+' resolved', JSON.stringify(v).slice(0,120))).catch(()=>{}); } catch(e){ console.log('   → '+k+' 抛错', e.message); }
        }
      }
    } catch{}
  };

  // --- 跑 main.min.js ---
  const code = fs.readFileSync(path.join(__dirname, 'main.min.js'), 'utf8');
  console.log('运行 main.min.js … appId=%s vid=%s', APP, vid);
  try { window.eval(code); } catch (e) { console.log('eval 抛错:', e && e.message); }

  const fire = (target, name) => { try { target.dispatchEvent(new window.Event(name)); } catch(e){} };
  // 生命周期事件：触发 PX 的 load/DOMContentLoaded 采集
  await new Promise(r => setTimeout(r, 500));
  fire(doc, 'DOMContentLoaded'); fire(window, 'DOMContentLoaded'); fire(window, 'load');
  // 模拟一点用户交互（PX 行为采集）
  try { for (let i=0;i<8;i++){ const ev=new window.Event('mousemove'); ev.clientX=100+i*7; ev.clientY=200+i*3; doc.dispatchEvent(ev); } } catch{}
  await new Promise(r => setTimeout(r, 4000));
  // 卸载序列：强制 sensor flush（PX 在 pagehide/visibilitychange 时发最终 sensor）
  console.log('  → 派发 visibilitychange(hidden)/pagehide/beforeunload 强制 flush');
  try { Object.defineProperty(doc, 'visibilityState', { get:()=>'hidden', configurable:true }); } catch{}
  try { Object.defineProperty(doc, 'hidden', { get:()=>true, configurable:true }); } catch{}
  fire(doc, 'visibilitychange'); fire(window, 'pagehide'); fire(window, 'beforeunload'); fire(window, 'blur');
  await new Promise(r => setTimeout(r, 4000));

  console.log('\n=== 网络活动 (%d 条) ===', netlog.length);
  for (const n of netlog) console.log(' ', n.via, n.method||'', (n.url||'').slice(0,90), n.status!==undefined?('-> '+n.status):'', n.body?('body='+n.body.slice(0,80)):'');
  console.log('\n=== 捕获的 _px* cookie ===');
  const px = Object.keys(capturedCookies).filter(k=>k.startsWith('_px'));
  if (px.length) px.forEach(k=>console.log('  ', k, '=', capturedCookies[k].slice(0,80)));
  else console.log('  (无 —— sensor 可能未成功或被拒)');
  process.exit(0);
}
main().catch(e => { console.error('FATAL', e); process.exit(1); });
