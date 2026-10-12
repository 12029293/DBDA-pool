/**
 * 豆包域（www.doubao.com）frontierSign 签名器
 * ================================================================
 * 与 dola 版的区别：
 *   - SDK 文件：webmssdk_doubao.js（豆包 CDN 的 c-webmssdk/1.0.0.43）
 *   - aid：497858（豆包域）
 *   - pageId：26930（与 dola 相同，来源 chat.8a2964c561.js 的 a6 模块）
 *   - include 路径：/alice /samantha /passport /biz /chat/completion /chat/async/chunk_stream
 *   - ic/ddrt：13（dola 版是 0）
 *
 * 依据（2026-10-08 从豆包网页版 bundle 提取）：
 *   chat.8a2964c561.js 模块 a6：
 *     r = `${CDN}/rc-client-security/c-webmssdk/1.0.0.43/webmssdk.es5.js`
 *     { pageId: 26930,
 *       paths: { include: [...], exclude: [...] },
 *       ic: 13, ddrt: 13 }
 *   ⇒ /chat/completion 在 include 名单内，服务端 710022002 就是校这个签名。
 *
 * 调用契约（与 dola 版完全一致，便于复用 pure_signer）：
 *   stdin  : {"id":"xxx","url":"https://...","method":"POST","headers":{...},
 *             "body":"{...}","cookies":{"k":"v"}}
 *   stdout : {"signed_url":"https://...?a_bogus=...","a_bogus":"...","id":"xxx"}
 *   --daemon 模式：stdin/stdout 逐行 JSON
 */
'use strict';

const vm = require('vm');
const fs = require('fs');
const path = require('path');

const SCRIPT_DIR = __dirname;
const SDK_PATH = path.join(SCRIPT_DIR, 'webmssdk_doubao.js');
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
         + '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

// 豆包域 webmssdk 配置（取自 bundle 模块 a6）
const DOUBAO_AID = 497858;
const DOUBAO_PAGE_ID = 26930;
const DOUBAO_IC = 13;
const DOUBAO_DDRT = 13;
const DOUBAO_PATHS = ['/alice', '/samantha', '/passport', '/biz',
                      '/chat/completion', '/chat/async/chunk_stream'];

const daemon = process.env.DOLA_SIGNER_DAEMON === '1'
            || process.env.DOUBAO_SIGNER_DAEMON === '1'
            || process.argv.includes('--daemon');

if (daemon) {
  const readline = require('readline');
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on('line', async (line) => {
    const text = String(line || '').trim();
    if (!text) return;
    let rid = '';
    try {
      const req = JSON.parse(text);
      rid = req && req.id ? String(req.id) : '';
      const out = await signUrl(req);
      if (rid) out.id = rid;
      process.stdout.write(JSON.stringify(out) + '\n');
    } catch (e) {
      const err = { error: e && e.stack ? e.stack : String(e) };
      if (rid) err.id = rid;
      process.stdout.write(JSON.stringify(err) + '\n');
    }
  });
} else {
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', d => input += d);
  process.stdin.on('end', async () => {
    try {
      const req = JSON.parse(input || '{}');
      const out = await signUrl(req);
      process.stdout.write(JSON.stringify(out));
    } catch (e) {
      process.stdout.write(JSON.stringify({ error: e && e.stack ? e.stack : String(e) }));
      process.exitCode = 1;
    }
  });
}

function randomUUID() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 3 | 8)).toString(16);
  });
}

function cookieString(cookies) {
  if (!cookies || typeof cookies !== 'object') return '';
  return Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');
}

function makeStorage() {
  return new class StorageShim {
    constructor() { this.d = Object.create(null); }
    getItem(k) { return Object.prototype.hasOwnProperty.call(this.d, k) ? this.d[k] : null; }
    setItem(k, v) { this.d[k] = String(v); }
    removeItem(k) { delete this.d[k]; }
    clear() { this.d = Object.create(null); }
    key(i) { return Object.keys(this.d)[i] || null; }
    get length() { return Object.keys(this.d).length; }
  }();
}

function buildCtx(cookies, url) {
  const eventListeners = Object.create(null);
  const t0 = Date.now();
  const captured = [];
  const ctx = {};

  Object.assign(ctx, {
    Date, Math, Object, Array, String, Number, Boolean, RegExp, Error,
    TypeError, SyntaxError, ReferenceError, URIError, RangeError,
    JSON, parseInt, parseFloat, isNaN, isFinite, decodeURI, encodeURI,
    decodeURIComponent, encodeURIComponent, NaN, Infinity, undefined,
    Promise, Proxy, Reflect, Symbol, Map, Set, WeakMap, WeakSet,
    ArrayBuffer, Uint8Array, Uint8ClampedArray, Int8Array,
    Uint16Array, Int16Array, Uint32Array, Int32Array,
    Float32Array, Float64Array, DataView,
    Intl, BigInt, BigInt64Array, BigUint64Array,
    TextEncoder, TextDecoder, URL, URLSearchParams,
    setTimeout, setInterval, clearTimeout, clearInterval,
  });

  let host = 'www.doubao.com';
  let origin = 'https://www.doubao.com';
  let pathname = '/chat/';
  try {
    const u = new URL(url);
    host = u.host; origin = u.origin; pathname = u.pathname;
  } catch (_) { /* 用默认 */ }

  ctx.navigator = {
    userAgent: UA,
    platform: 'Win32', language: 'zh-CN', languages: ['zh-CN', 'zh'],
    hardwareConcurrency: 8, deviceMemory: 8, webdriver: false,
    cookieEnabled: true, vendor: 'Google Inc.', maxTouchPoints: 0,
    appVersion: '5.0', appCodeName: 'Mozilla', appName: 'Netscape',
    product: 'Gecko', productSub: '20030107', vendorSub: '',
    doNotTrack: null,
    plugins: { length: 0, item: () => null, namedItem: () => null, refresh: () => {} },
    mimeTypes: { length: 0, item: () => null, namedItem: () => null },
    connection: { effectiveType: '4g', rtt: 50, downlink: 10, saveData: false },
  };

  ctx.document = {
    cookie: cookieString(cookies), hidden: false, visibilityState: 'visible',
    referrer: '', title: '豆包', characterSet: 'UTF-8', charset: 'UTF-8',
    readyState: 'complete', domain: host, compatMode: 'CSS1Compat',
    createElement(tag) {
      const parent = { appendChild: c => c, removeChild: c => c, insertBefore: c => c,
                       remove() {}, style: {}, children: [] };
      const el = {
        style: {}, tagName: (tag || '').toUpperCase(), src: '', async: false,
        parentNode: parent, ownerDocument: null,
        appendChild: c => c, removeChild: c => c, insertBefore: c => c,
        remove() {}, setAttribute() {}, getAttribute() { return null; },
        addEventListener() {}, removeEventListener() {},
        // ★ webmssdk 会动态插 <script> 并等 onload，这里让它成功回调
        get onload() { return this._onload; },
        set onload(fn) { this._onload = fn; if (typeof fn === 'function') setTimeout(fn, 0); },
      };
      if (tag === 'canvas') {
        el.getContext = () => ({
          fillRect() {}, clearRect() {}, getImageData() { return { data: new Uint8ClampedArray(4) }; },
          putImageData() {}, measureText() { return { width: 0 }; }, fillText() {},
          beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {}, arc() {},
          save() {}, restore() {}, scale() {}, translate() {}, rotate() {}, drawImage() {},
          createLinearGradient: () => ({ addColorStop() {} }),
          getExtension: () => null, getParameter: () => 'Google Inc.',
        });
      }
      return el;
    },
    getElementById: () => null, getElementsByTagName: () => [], getElementsByClassName: () => [],
    querySelector: () => null, querySelectorAll: () => [], createEvent: () => ({ initEvent() {} }),
    body: { appendChild() {}, removeChild() {}, insertBefore() {}, style: {}, children: [] },
    head: { appendChild() {}, removeChild() {}, style: {}, children: [] },
    documentElement: { style: {}, appendChild() {}, removeChild() {} },
    all: [], forms: [], images: [], links: [],
    addEventListener(t, h) { (eventListeners[t] ||= []).push(h); },
    removeEventListener() {},
  };

  ctx.location = {
    href: url || `${origin}/chat/`, origin, protocol: 'https:',
    host, hostname: host, pathname,
    search: '', hash: '', port: '',
    assign() {}, replace() {}, reload() {},
  };

  ctx.screen = { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040,
                 colorDepth: 24, pixelDepth: 24 };
  ctx.performance = {
    now: () => Date.now() - t0 - 5000,
    timeOrigin: t0 - 5000,
    timing: { navigationStart: t0 - 5000, domComplete: t0 - 4000, domLoading: t0 - 4500 },
    getEntriesByType: () => [], mark() {}, measure() {},
  };
  ctx.localStorage = makeStorage();
  ctx.sessionStorage = makeStorage();

  ctx.XMLHttpRequest = class XMLHttpRequestShim {
    constructor() { this.readyState = 0; this.status = 200; this.statusText = 'OK'; this.responseText = '{}'; this.response = null; }
    open(method, url) { this.readyState = 1; this._method = method; this._url = url; }
    send() { this.readyState = 4; if (this.onreadystatechange) this.onreadystatechange(); if (this.onload) this.onload(); }
    setRequestHeader() {}
    getResponseHeader() { return null; }
    getAllResponseHeaders() { return ''; }
    abort() {}
  };
  ctx.XMLHttpRequest.UNSENT = 0; ctx.XMLHttpRequest.OPENED = 1; ctx.XMLHttpRequest.DONE = 4;

  ctx.fetch = function fakeFetch(u, opts) {
    const s = typeof u === 'string' ? u : (u && u.url) || String(u);
    captured.push({ url: s, method: opts && opts.method });
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}'),
                             json: () => Promise.resolve({}), headers: new Map() });
  };
  ctx.Request = globalThis.Request;
  ctx.Headers = globalThis.Headers;
  ctx.Response = globalThis.Response;

  ctx.crypto = {
    getRandomValues(arr) { for (let i = 0; i < arr.length; i++) arr[i] = Math.floor(Math.random() * 256); return arr; },
    randomUUID,
    subtle: { digest: () => Promise.resolve(new ArrayBuffer(32)),
              encrypt: () => Promise.resolve(new ArrayBuffer(32)),
              decrypt: () => Promise.resolve(new ArrayBuffer(32)) },
  };

  ctx.atob = s => Buffer.from(s, 'base64').toString('binary');
  ctx.btoa = s => Buffer.from(s, 'binary').toString('base64');
  ctx.console = { log() {}, warn() {}, error() {}, info() {}, debug() {} };
  ctx.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
  ctx.getComputedStyle = () => ({});
  ctx.open = () => null;
  ctx.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 16);
  ctx.cancelAnimationFrame = clearTimeout;
  ctx.innerWidth = 1920; ctx.innerHeight = 1080; ctx.outerWidth = 1920; ctx.outerHeight = 1080;
  ctx.devicePixelRatio = 2; ctx.isSecureContext = true;
  ctx.screenX = 0; ctx.screenY = 0; ctx.screenLeft = 0; ctx.screenTop = 0;
  ctx.scrollX = 0; ctx.scrollY = 0; ctx.pageXOffset = 0; ctx.pageYOffset = 0;
  ctx.name = ''; ctx.closed = false;
  ctx.addEventListener = (t, h) => { (eventListeners[t] ||= []).push(h); };
  ctx.removeEventListener = () => {};
  ctx.dispatchEvent = ev => { (eventListeners[ev.type] || []).forEach(h => { try { h(ev); } catch (_) {} }); return true; };
  ctx.Event = function Event(t) { this.type = t || ''; };
  ctx.CustomEvent = function CustomEvent(t, o) { this.type = t || ''; if (o) Object.assign(this, o); };
  ctx.Blob = class BlobShim { constructor(_p, o) { this.size = 0; this.type = (o && o.type) || ''; } };
  ctx.File = class FileShim { constructor(_p, n, o) { this.size = 0; this.name = n || ''; this.type = (o && o.type) || ''; } };
  ctx.FormData = class FormDataShim { append() {} };
  ctx.Image = function ImageShim() { return { src: '', onload: null, onerror: null, width: 0, height: 0, complete: false }; };
  ctx.Worker = function WorkerShim() {};
  ctx.WebSocket = function WebSocketShim() {};
  ctx.CSS = { escape: s => s, supports: () => false };

  ctx.window = ctx; ctx.self = ctx; ctx.top = ctx; ctx.parent = ctx;
  ctx.frames = ctx; ctx.globalThis = ctx;
  ctx.__captured = captured;
  return ctx;
}

function loadAcrawler(ctx, cookies, url) {
  if (!fs.existsSync(SDK_PATH)) {
    throw new Error('webmssdk_doubao.js not found: ' + SDK_PATH);
  }
  new vm.Script(fs.readFileSync(SDK_PATH, 'utf8'),
                { filename: 'webmssdk.es5.js' })
    .runInContext(ctx, { timeout: 30000 });
  const ac = ctx.byted_acrawler;
  if (!ac || typeof ac.frontierSign !== 'function') {
    throw new Error('window.byted_acrawler.frontierSign not available');
  }
  return ac;
}

async function signUrl({ url, method = 'POST', headers = {}, body = '{}', cookies = {} }) {
  if (!url) throw new Error('missing url');
  const ctx = buildCtx(cookies, url);
  vm.createContext(ctx);
  const ac = loadAcrawler(ctx, cookies, url);

  ac.init({
    aid: DOUBAO_AID,
    pageId: DOUBAO_PAGE_ID,
    paths: DOUBAO_PATHS,
    ic: DOUBAO_IC,
    ddrt: DOUBAO_DDRT,
  });

  for (let i = 0; i < 12; i++) {
    ctx.dispatchEvent({ type: 'mousemove', clientX: 380 + i * 7, clientY: 240 + i * 3,
                        timeStamp: Date.now() + i * 25, bubbles: true });
  }
  for (const type of ['mousedown', 'mouseup', 'click']) {
    ctx.dispatchEvent({ type, clientX: 600, clientY: 400, timeStamp: Date.now(), bubbles: true });
  }

  // ★ frontierSign(url, body, method) 返回 { "X-Bogus": "..." }
  //   webmssdk 前端拿到它后，是把**值**塞进 query 的 a_bogus 参数
  //   （注意：参数名叫 a_bogus，但内容是 X-Bogus 的值 —— 实测确认）
  let xb = '';
  try {
    const r = await Promise.resolve(ac.frontierSign(url, body, method));
    if (typeof r === 'string') xb = r;
    else if (r && (r['X-Bogus'] || r.xBogus || r.a_bogus)) {
      xb = r['X-Bogus'] || r.xBogus || r.a_bogus;
    }
  } catch (e) {
    return { error: 'frontierSign failed: ' + (e && e.message ? e.message : String(e)) };
  }

  if (!xb) return { error: 'frontierSign returned empty' };

  const u = new URL(url);
  u.searchParams.set('a_bogus', xb);
  const signed = u.toString();
  return {
    signed_url: signed,
    a_bogus: xb,
    msToken: u.searchParams.get('msToken') || '',
    x_bogus: xb,
    signature: u.searchParams.get('_signature') || '',
  };
}

if (!daemon) {
  setTimeout(() => process.exit(0), 25);
}
