(async () => {
  if (window.__dola30sInstalled) return { already: true };
  window.__dola30sInstalled = true;
  window.__dola30sLog = [];
  // 目标时长可由注入方（browser30._hook_source）在钩子之前写入；
  // 未注入时保持默认 30（独立加载本文件的老脚本不受影响）。
  const TARGET = window.__dolaTargetDur || 30;

  const origFetch = window.fetch;
  window.fetch = async function (input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const method = (init && init.method) || (input && input.method) || 'GET';
      if (url.includes('/chat/completion') && method.toUpperCase() === 'POST' && init && init.body && typeof init.body === 'string') {
        let body = init.body;
        try {
          const payload = JSON.parse(body);
          const ab = payload.chat_ability || payload.skill || {};
          const at = ab.ability_type ?? ab.skill_type;
          window.__dola30sLog.push({ t: Date.now(), url: url.slice(0, 120), abilityType: at, paramRaw: typeof ab.ability_param === 'string' ? ab.ability_param.slice(0, 300) : null, contentRaw: typeof payload.messages?.[0]?.content === 'string' ? payload.messages[0].content.slice(0, 300) : null });
          // Dola 格式: chat_ability.ability_param
          if (ab.ability_type === 17 && typeof ab.ability_param === 'string') {
            const p = JSON.parse(ab.ability_param);
            if (String(p.model || '').includes('2.5')) { p.duration = TARGET; ab.ability_param = JSON.stringify(p); payload.chat_ability = ab; body = JSON.stringify(payload); window.__dola30sLog.push({ t: Date.now(), patched: 'fetch→' + TARGET, model: p.model }); }
          }
          // 豆包格式: content JSON 字符串 + skill
          const msg = payload.messages && payload.messages[0];
          if (msg && typeof msg.content === 'string' && msg.skill && String(msg.skill.skill_type) === '17') {
            const c = JSON.parse(msg.content);
            if (String(c.model || '').includes('2.5')) { c.duration = TARGET; msg.content = JSON.stringify(c); body = JSON.stringify(payload); window.__dola30sLog.push({ t: Date.now(), patched: 'fetch→' + TARGET + '(doubao fmt)', model: c.model }); }
          }
        } catch (e) { window.__dola30sLog.push({ t: Date.now(), parseErr: String(e).slice(0, 80) }); }
        init = Object.assign({}, init, { body });
      }
    } catch (e) { /* 不影响正常流程 */ }
    return origFetch.call(this, input, init);
  };
  window.fetch.__dola30s = true;

  // XHR
  const XO = XMLHttpRequest.prototype.open, XS = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__dolaUrl = u; this.__dolaMethod = m; return XO.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function (b) {
    try {
      if (b && typeof b === 'string' && this.__dolaUrl && String(this.__dolaUrl).includes('/chat/completion') && String(this.__dolaMethod).toUpperCase() === 'POST') {
        const payload = JSON.parse(b);
        const ab = payload.chat_ability || {};
        if (ab.ability_type === 17 && typeof ab.ability_param === 'string') {
          const p = JSON.parse(ab.ability_param);
          if (String(p.model || '').includes('2.5')) { p.duration = TARGET; ab.ability_param = JSON.stringify(p); payload.chat_ability = ab; b = JSON.stringify(payload); window.__dola30sLog.push({ t: Date.now(), patched: 'xhr→' + TARGET }); }
        }
        window.__dola30sLog.push({ t: Date.now(), xhr: true, abilityType: payload.chat_ability?.ability_type, paramRaw: typeof payload.chat_ability?.ability_param === 'string' ? payload.chat_ability.ability_param.slice(0, 300) : null });
      }
    } catch (e) { /* 忽略 */ }
    return XS.apply(this, arguments);
  };
  return { installed: true, target: TARGET };
})()
