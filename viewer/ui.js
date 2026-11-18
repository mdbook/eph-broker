// Module host contract (docs/modules.md): mount and quickView may return cleanup;
// ctx.baseUrl prefixes UI routes and ctx.signal aborts on unmount.
// The Agents tab: the directory on the left, the picked agent on the right. A hermes agent with a view shows its
// live view: the sidecar's renderer (hermes-ui/view.js, the sidecar's web/ installed by the operator) mounted in a
// shadow root, fed only through this module's re-typed JSON/SSE proxy (api/a/NAME/...). A claude-code or codex
// agent with the tail cap shows its transcript (transcript.js) and, with steer-pull, a composer under it.
// The optional host hook ctx.agentSummary(name) adds what the host knows (what it is doing, model, context).
const VIEW_STATUS = {'': ['idle', 'Idle'], busy: ['run', 'Working'], compact: ['run', 'Compacting'],
  unread: ['wait', 'Unread reply'], off: ['off', 'Offline']};
const DIR_STATE = {busy: ['run', 'Working'], idle: ['idle', 'Idle'], live: ['run', 'Online'], grey: ['off', 'Offline']};
const STATES = new Set(['run', 'wait', 'idle', 'block', 'off']);
const KIND = {'claude-code': 'Claude Code', codex: 'Codex', hermes: 'Hermes', generic: 'Agent'};
// Public renderer seam. Keep these as CSS references, not computed color snapshots: changing the broker's
// light/dark/auto theme must update an already open view without remounting it or losing composer state.
const VIEW_TOKENS = `
  --agent-view-bg:var(--bg,#f4f5f7);--agent-view-surface:var(--s1,#fff);
  --agent-view-hover:var(--s2,#f0f2f5);--agent-view-active:var(--s3,#e6e9ee);
  --agent-view-border:var(--line,#dce0e6);--agent-view-text:var(--t1,rgb(20,25,32));
  --agent-view-muted:var(--t2,rgb(71,81,96));--agent-view-faint:var(--t3,#626c7b);
  --agent-view-accent:var(--brand,#2b57c9);--agent-view-on-accent:var(--onbrand,#fff);
  --agent-view-success:var(--run,rgb(23,115,74));--agent-view-warning:var(--wait,#8a5600);
  --agent-view-danger:var(--block,#bb3328);--agent-view-shadow:var(--shadow,none);
  --agent-view-font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
  --agent-view-mono:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace;
  --agent-view-font-size:14px;--agent-view-line-height:1.5;
  --agent-view-radius:12px;--agent-view-control-radius:8px;--agent-view-space:16px;
  --agent-view-control-height:36px;`;

// Compatibility stylesheet for today's renderer, which does not yet consume --agent-view-* itself.
// Only the renderer's shadow tree is affected; never replace its stylesheet or its layout/behavior rules.
const VIEW_CSS = `
:host > *, .ob{
  --bg:var(--agent-view-bg)!important;--panel:var(--agent-view-surface)!important;
  --panel2:var(--agent-view-hover)!important;--line:var(--agent-view-border)!important;
  --ink:var(--agent-view-text)!important;--text:var(--agent-view-text)!important;
  --muted:var(--agent-view-muted)!important;--faint:var(--agent-view-faint)!important;
  --accent:var(--agent-view-accent)!important;--green:var(--agent-view-success)!important;
  --amber:var(--agent-view-warning)!important;--red:var(--agent-view-danger)!important;
  --code:var(--agent-view-hover)!important;--user:var(--agent-view-surface)!important;
  background:var(--agent-view-surface)!important;color:var(--agent-view-text)!important;
  font:var(--agent-view-font-size)/var(--agent-view-line-height) var(--agent-view-font)!important;
  -webkit-font-smoothing:antialiased;
}
*{box-sizing:border-box;border-color:var(--agent-view-border)!important}
button,input,select,textarea{font:inherit!important;color:var(--agent-view-text)!important;
  background:var(--agent-view-surface)!important;border:1px solid var(--agent-view-border)!important;
  border-radius:var(--agent-view-control-radius)!important}
button,input:not([type=checkbox]):not([type=radio]),select{min-height:var(--agent-view-control-height)}
button{padding:0 var(--agent-view-space)!important;font-size:14px!important;font-weight:600!important}
/* State selectors must not outrank variants: accent text always needs its accent surface. */
button:where(:hover,:focus-visible):not(:disabled){background:var(--agent-view-hover)!important;border-color:var(--agent-view-faint)!important}
button:where(:active):not(:disabled){background:var(--agent-view-active)!important;transform:translateY(1px)}
/* Dimming the whole control also dims its text below AA. Mark disabled without losing contrast. */
button:disabled{opacity:1!important;border-style:dashed!important;cursor:not-allowed}
button.pri,button.primary,button[type=submit]{background:var(--agent-view-accent)!important;color:var(--agent-view-on-accent)!important}
a{color:var(--agent-view-accent)!important}
code,pre,kbd,samp{font-family:var(--agent-view-mono)!important}
pre{background:var(--agent-view-hover)!important;max-width:100%;overflow:auto}
textarea{padding:8px 12px!important}
header,footer,nav,aside,dialog{background:var(--agent-view-surface)!important;color:var(--agent-view-text)!important}
.card,details{border-radius:var(--agent-view-radius)!important}
.sub,.meta,.muted{color:var(--agent-view-muted)!important;font-size:13px!important}
.chip,.pill{border-radius:999px!important;font-size:13px!important;font-weight:600!important;
  background:color-mix(in srgb,currentColor 13%,var(--agent-view-surface))!important}
.run,.busy,.ok{color:var(--agent-view-success)!important}
.wait,.warn,.pend{color:var(--agent-view-warning)!important}
.bad,.error{color:var(--agent-view-danger)!important}
:focus-visible{outline:2px solid var(--agent-view-accent)!important;outline-offset:2px}
@media(max-width:720px){button,select,input:not([type=checkbox]):not([type=radio]){min-height:48px}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
`;

export function themeView(host) {
  // view.js mounts synchronously into an open shadow root. A renderer without one still inherits the
  // public variables on its host; it can consume the seam without this compatibility layer.
  const root = host.shadowRoot;
  if (root && !root.querySelector('style[data-agent-view-theme]')) {
    const style = el('style', null, VIEW_CSS);
    style.setAttribute('data-agent-view-theme', '');
    root.append(style);
  }
}

const COMPOSER_CSS = `
.ad-comp{flex:none;display:grid;gap:8px;padding:10px 16px calc(10px + env(safe-area-inset-bottom));
  border-top:1px solid var(--line);background:var(--s1)}
.ad-qr{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;margin:0 -16px;padding:0 16px}
.ad-qr::-webkit-scrollbar{display:none}
.ad-qr button{flex:none;font:inherit;font-size:13px;font-weight:600;color:var(--t1);background:var(--s1);
  border:1px solid var(--line);border-radius:999px;min-height:32px;padding:0 14px;cursor:pointer;white-space:nowrap}
.ad-qr button:hover:not(:disabled){background:var(--s2)}
.ad-row{display:flex;align-items:flex-end;gap:8px}
.ad-row textarea{flex:1;min-width:0;box-sizing:border-box;resize:none;font:inherit;line-height:20px;max-height:140px;
  min-height:40px;padding:9px 14px;border-radius:20px;border:1px solid var(--line);background:var(--s2);color:var(--t1)}
.ad-send{flex:none;width:40px;height:40px;border-radius:50%;border:0;font:inherit;font-size:18px;font-weight:700;cursor:pointer;
  background:var(--brand);color:var(--onbrand)}
.ad-send:disabled,.ad-qr button:disabled{opacity:.5;cursor:not-allowed}
.ad-comp :focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.ad-hint{margin:0;font-size:12px;color:var(--t2)}
.ad-sent{list-style:none;margin:0;padding:0;display:grid;gap:4px;font-size:13px}
.ad-sent li{display:flex;gap:8px;justify-content:space-between}.ad-sent span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ad-sent b{flex:none;font-weight:600;color:var(--t2)}.ad-sent b.ok{color:var(--run)}
.ad-extra{display:flex;gap:8px}
@media (max-width:720px){.ad-qr button{min-height:36px}.ad-row textarea{font-size:16px}.ad-send{width:44px;height:44px}}`;

const CSS = `
.adir{display:grid;grid-template-columns:260px 1fr;gap:16px;align-items:start;color:var(--t1,inherit)}
.adir-card{background:var(--s1,transparent);border:1px solid var(--line,currentColor);border-radius:12px;box-shadow:var(--shadow,none);min-width:0}
.adir-detail{position:sticky;top:80px;max-height:calc(100dvh - 96px);overflow:auto}
.adir-detail.full{display:flex;flex-direction:column;height:calc(100dvh - 96px);overflow:hidden}
.adir-list{padding:4px 0}
.adir-it{display:flex;align-items:center;gap:12px;padding:14px 20px;border-left:3px solid transparent;
  color:inherit;text-decoration:none}
.adir-it:hover,.adir-it:focus-visible{background:var(--s2,transparent)}
.adir-it:focus-visible,.adir-back:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.adir-it[aria-current=true]{background:color-mix(in srgb,var(--brand) 7%,var(--s1));border-left-color:var(--brand,currentColor)}
.adir-it.grey{color:var(--t2,inherit)}.adir-grow{flex:1;min-width:0}.adir-nm{font-weight:600;overflow-wrap:anywhere}
.adir-meta,.adir-sub{font-size:13px;color:var(--t2,inherit)}
.adir-dot{width:8px;height:8px;border-radius:50%;background:var(--brand,currentColor)}
.adir-head{flex:none;display:grid;gap:6px;padding:14px 20px;border-bottom:1px solid var(--line,currentColor)}
.adir-top{display:flex;align-items:center;gap:12px;min-width:0}
.adir-top h2{margin:0;font-size:18px;font-weight:650;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.adir-now{margin:0;font-size:14px;overflow-wrap:anywhere}.adir-now b{color:var(--t2);margin-right:6px}
.adir-wait{color:var(--wait);font-weight:600}
.adir-meta2{display:flex;align-items:center;flex-wrap:wrap;gap:4px 12px;font-size:13px;color:var(--t2);font-variant-numeric:tabular-nums}
.adir-back{display:none;color:var(--brand);font-size:14px;font-weight:600;text-decoration:none;white-space:nowrap}
.adir-back:hover{text-decoration:underline}
/* Resolve references outside the renderer host, whose :host styles may redefine the older tokens. */
.adir-detail{${VIEW_TOKENS}}
.adir-view{height:max(480px,calc(100vh - 220px));position:relative;border-radius:0 0 12px 12px}
.adir-detail.full .adir-view{flex:1;height:auto;min-height:0;border-radius:0;overflow:hidden}
.adir-note{padding:16px;color:var(--t2,inherit)}
.adir-empty{padding:12px 14px;color:var(--t2,inherit)}
.ad-st{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;padding:2px 10px;border-radius:999px;
  white-space:nowrap;color:var(--t2);background:color-mix(in srgb,currentColor 13%,var(--s1))}
.ad-st::before{content:'';flex:none;box-sizing:border-box;width:8px;height:8px;border-radius:50%;border:1.5px solid var(--t3)}
.ad-st.run{color:var(--run)}.ad-st.run::before{border:0;background:currentColor}
.ad-st.wait{color:var(--wait)}.ad-st.wait::before{border:2px solid currentColor}
.ad-st.block{color:var(--block)}.ad-st.block::before{border:0;border-radius:2px;background:currentColor}
.ad-st.off{color:var(--t3)}.ad-st.off::before{border:0;height:2px;border-radius:1px;background:currentColor}
.ad-meter{display:inline-block;width:56px;height:6px;border-radius:3px;background:var(--s3);overflow:hidden;vertical-align:middle;margin-right:6px}
.ad-meter i{display:block;height:100%;background:var(--brand)}.ad-meter.warm i{background:var(--wait)}.ad-meter.hot i{background:var(--block)}
${COMPOSER_CSS}
@media (max-width:820px){
  .adir{grid-template-columns:1fr}.adir.picked .adir-listcard{display:none}.adir:not(.picked) .adir-detail{display:none}
  .adir-detail{position:static;max-height:none;overflow:visible}
  .adir-back{display:inline-flex}.adir-view{height:calc(100vh - 160px)}
}
@media (max-width:720px){
  .adir-head{padding:8px 12px;gap:4px}.adir-top{gap:10px}.adir-top h2{font-size:16px;flex:1}
  .adir-back{min-height:44px;align-items:center}
  .adir-meta2{font-size:12px;gap:2px 10px}
  .adir-view{height:calc(100dvh - 132px - env(safe-area-inset-top) - env(safe-area-inset-bottom))}
  .adir-detail.full{height:calc(100dvh - 80px - env(safe-area-inset-bottom));border-radius:10px}
}`;


function ago(seconds) {
  const s = Math.floor(seconds);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  return Math.floor(s / 3600) + 'h ago';
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

const kTok = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1000 ? Math.round(n / 1000) + 'k' : String(n);
const num = (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null;

function poller(ctx, render, error) {
  let stopped = false, timer;
  const controller = new AbortController();
  const base = (ctx.baseUrl || '').replace(/\/$/, '');
  const stop = () => { stopped = true; clearTimeout(timer); controller.abort(); };
  if (ctx.signal) ctx.signal.addEventListener('abort', stop);
  async function poll() {
    try {
      const response = await fetch(base + '/api/agents', {signal: controller.signal});
      if (!response.ok) throw new Error('Directory unavailable (' + response.status + ')');
      const data = await response.json();
      if (stopped) return;
      render(data.agents, Array.isArray(data.quick_replies) ? data.quick_replies : []);
      error.textContent = '';
    } catch (err) { if (!stopped) error.textContent = err.message; }
    finally { if (!stopped) timer = setTimeout(poll, 5000); }
  }
  poll();
  return stop;
}

// "name", "name/session" or "name/session/messageId"; the session part is URI-encoded on its own.
export function parseRoute(route) {
  const [name, session, id] = (route || '').split('/');
  let s = null;
  try { s = session ? decodeURIComponent(session) : null; } catch (e) { s = null; }
  return {name: name || null, session: s, id: /^\d+$/.test(id || '') ? id : null};
}

export function makeRoute(name, session, id) {
  return name + (session ? '/' + encodeURIComponent(session) + (id ? '/' + id : '') : '');
}

// A /cmd that outlasts the proxy's quick window answers 200 with the failure inside the body as {error, status}.
// Give the view the real status back so its own error handling applies.
export function viewFetch(url, init = {}) {
  const post = (init.method || 'GET') === 'POST';
  const headers = new Headers(init.headers || {});
  if (post) headers.set('X-Broker-Action', '1');
  return fetch(url, {...init, headers, credentials: 'same-origin'}).then(async (r) => {
    if (!r.ok || !(r.headers.get('Content-Type') || '').startsWith('application/json')) return r;
    const text = await r.text();
    let status = 200;
    try {
      const j = JSON.parse(text);
      if (j && typeof j === 'object' && j.error && j.status >= 400 && j.status <= 599) status = j.status;
    } catch (e) { /* not JSON after all: pass it through */ }
    return new Response(text, {status, headers: r.headers});
  });
}

export function steerState(state) {
  return state === 'delivered' ? 'Delivered' : 'Queued';
}

// A one-line "what it is doing" note split into the work and who it waits on, so the wait can stand out.
export function splitNow(text) {
  const s = String(text || '').replace(/\s+/g, ' ').trim();
  const m = /\b(?:waiting (?:on|for)|blocked (?:on|by))\b/i.exec(s);
  if (!m) return {doing: s, waiting: ''};
  const doing = s.slice(0, m.index).replace(/[\s,;:(-]+$/, '');
  const waiting = s.slice(m.index).replace(/[\s.)]+$/, '');
  return {doing: doing && !/[.!?]$/.test(doing) ? doing + '.' : doing, waiting: waiting[0].toUpperCase() + waiting.slice(1)};
}

// Directory presence plus the host's optional summary, in one shape for every view.
export function describe(agent, summary) {
  const s = summary && typeof summary === 'object' ? summary : {};
  const [state, label] = DIR_STATE[agent && agent.status] || ['off', 'Unknown'];
  return {state: STATES.has(s.state) ? s.state : state, label: typeof s.label === 'string' && s.label ? s.label : label,
    ...splitNow(typeof s.doing === 'string' ? s.doing : ''), model: typeof s.model === 'string' ? s.model : '',
    tokens: num(s.context_tokens), limit: num(s.context_limit),
    detail: typeof s.detail === 'string' && s.detail ? s.detail : agent ? KIND[agent.kind] || agent.kind : '',
    age: agent ? agent.age : null};
}

function chip(state, label) {
  const c = el('span', 'ad-st ' + (STATES.has(state) ? state : 'off'), label);
  return c;
}

function meter(info) {
  const wrap = el('span');
  if (info.tokens == null) return wrap;
  if (info.limit) {
    const pct = info.tokens / info.limit, m = el('span', 'ad-meter' + (pct >= 1 ? ' hot' : pct >= .8 ? ' warm' : ''));
    const fill = el('i'); fill.style.width = Math.min(100, Math.round(pct * 100)) + '%';
    m.append(fill); m.title = kTok(info.tokens) + ' of ' + kTok(info.limit) + ' context';
    wrap.append(m);
  }
  wrap.append(kTok(info.tokens) + (info.limit ? '' : ' context'));
  return wrap;
}

// One composer for the detail page and the quick view. send(text) resolves to a message id (tracked until
// delivered), true (sent, nothing to track) or throws. Quick replies send their text in one tap.
function composer(opts) {
  const form = el('form', 'ad-comp'), qr = el('div', 'ad-qr'), row = el('div', 'ad-row');
  const input = el('textarea'), send = el('button', 'ad-send', '↑'), hint = el('p', 'ad-hint');
  const sent = el('ul', 'ad-sent'), extra = el('div', 'ad-extra');
  input.rows = 1; input.maxLength = opts.max || 4000; input.placeholder = 'Message ' + opts.name;
  input.setAttribute('aria-label', 'Message ' + opts.name);
  send.type = 'submit'; send.setAttribute('aria-label', 'Send'); send.title = 'Send';
  qr.setAttribute('role', 'group'); qr.setAttribute('aria-label', 'Quick replies');
  hint.setAttribute('role', 'status'); sent.setAttribute('aria-live', 'polite');
  row.append(input, send);
  form.append(qr, extra, row, hint, sent);
  extra.hidden = true; sent.hidden = !!opts.sink;
  let busy = false, canSend = false, replies = [], why = '';
  const mine = new Map();   // id -> {text, state, li}
  function paint() {
    for (const b of [input, send, ...qr.children, ...extra.children]) b.disabled = !canSend || busy;
    hint.textContent = why || opts.hint || '';
  }
  function grow() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight + 2, 140) + 'px'; }
  async function submit(text) {
    if (!canSend || busy || !text.trim()) return false;
    busy = true; why = ''; paint();
    try {
      const id = await opts.send(text);
      if (typeof id === 'string') {
        const rec = {text, state: 'queued', li: null};
        mine.set(id, rec);
        const sink = opts.sink && opts.sink();
        if (sink) sink({type: 'broker', subtype: 'web_message', id, state: 'queued', via: 'tool', text,
          timestamp: new Date().toISOString()});
        else {
          rec.li = el('li'); rec.li.append(el('span', null, text), el('b', null, steerState('queued')));
          sent.hidden = false; sent.append(rec.li);
          while (sent.children.length > 3) sent.firstChild.remove();
        }
      }
      return true;
    } catch (err) { why = 'Could not send: ' + err.message; return false; }
    finally { busy = false; paint(); }
  }
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (await submit(input.value)) { input.value = ''; grow(); }
  };
  input.addEventListener('input', grow);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !matchMedia('(pointer:coarse)').matches) {
      e.preventDefault(); form.requestSubmit();
    }
  });
  return {form, input, submit,
    // ok: may send now; reason: shown when it may not; list: quick replies; states: the agent's steer_messages.
    update(ok, reason, list, states) {
      canSend = !!ok; why = ok ? why : reason || '';
      const labels = (list || []).map((r) => r.label + '\n' + r.text).join('\n\n');
      if (labels !== replies) {
        replies = labels;
        qr.replaceChildren(...(list || []).map((r) => {
          const b = el('button', null, r.label); b.type = 'button'; b.title = r.text;
          b.onclick = () => submit(r.text);
          return b;
        }));
      }
      qr.hidden = !qr.children.length;
      for (const m of states || []) {
        const rec = mine.get(m.id);
        if (!rec || rec.state === 'delivered' || m.state !== 'delivered') continue;
        rec.state = 'delivered';
        const sink = opts.sink && opts.sink();
        if (sink) sink({type: 'broker', subtype: 'web_message', id: m.id, state: 'delivered', via: 'tool',
          timestamp: new Date().toISOString()});
        if (rec.li) { rec.li.lastChild.textContent = steerState('delivered'); rec.li.lastChild.className = 'ok'; }
      }
      paint();
    },
    extra(buttons) { extra.replaceChildren(...buttons); extra.hidden = !buttons.length; paint(); },
  };
}

function steerSend(base, name, signal) {
  return async (text) => {
    const response = await viewFetch(base + 'api/agents/' + encodeURIComponent(name) + '/steer', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text}), signal,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
    return String(data.id);
  };
}

const READ_ONLY = 'Read-only: sending needs the broker actions permission.';

export function mount(el0, ctx = {}) {
  const standalone = !ctx.openAgent;
  const navigate = standalone ? (route) => { location.hash = '#' + encodeURIComponent(route); } : ctx.openAgent;
  const hrefOf = (route) => standalone ? '#' + encodeURIComponent(route)
    : location.hash.split('/').slice(0, 2).join('/') + '/' + encodeURIComponent(route);
  const routeNow = () => {
    if (!standalone) return ctx.agentName || '';
    try { return decodeURIComponent(location.hash.slice(1)); } catch (e) { return ''; }
  };
  const base = ctx.baseUrl || '/';
  const summaryOf = (name) => { try { return ctx.agentSummary ? ctx.agentSummary(name) : null; } catch (e) { return null; } };
  const style = el('style', null, CSS);
  const wrap = el('div', 'adir');
  const listCard = el('div', 'adir-card adir-listcard'), list = el('nav', 'adir-list');
  list.setAttribute('aria-label', 'Agents');
  const empty = el('p', 'adir-empty', 'No agents registered yet.');
  const error = el('p', 'adir-sub'); error.setAttribute('role', 'status');
  listCard.append(list, empty, error);
  const detail = el('div', 'adir-card adir-detail');
  wrap.append(listCard, detail);
  el0.replaceChildren(style, wrap);

  let stopped = false, route = parseRoute(routeNow()), shown = null, view = null, agents = [], unread = {};
  let head = null, viewStatus = null, replies = [];
  const items = new Map();
  let comp = null;

  function renderList() {
    empty.hidden = agents.length > 0;
    const seen = new Set();
    for (const agent of agents) {
      seen.add(agent.name);
      let it = items.get(agent.name);
      if (!it) {
        it = el('a', 'adir-it');
        it.append(el('span', 'adir-grow'), el('span', 'adir-dot'));
        it.firstChild.append(el('div', 'adir-nm'), el('div', 'adir-meta'));
        items.set(agent.name, it);
      }
      const info = describe(agent, summaryOf(agent.name));
      it.href = hrefOf(agent.name);
      it.classList.toggle('grey', agent.status === 'grey');
      it.setAttribute('aria-current', String(agent.name === route.name));
      it.querySelector('.adir-nm').textContent = agent.name;
      it.querySelector('.adir-meta').textContent = [info.label, KIND[agent.kind] || agent.kind, ago(agent.age)].join(' · ');
      const dot = it.querySelector('.adir-dot');
      dot.hidden = !unread[agent.name];
      dot.title = unread[agent.name] ? unread[agent.name] + ' unread' : '';
      list.append(it);
    }
    for (const [name, it] of items) if (!seen.has(name)) { it.remove(); items.delete(name); }
  }

  function headerFor(agent) {
    const box = el('div', 'adir-head'), top = el('div', 'adir-top');
    const back = el('a', 'adir-back', '‹ Agents');
    back.href = standalone ? '#' : location.hash.split('/').slice(0, 2).join('/');
    const status = el('span'), now = el('p', 'adir-now'), meta = el('div', 'adir-meta2');
    top.append(back, el('h2', null, agent.name), status);
    box.append(top, now, meta);
    head = {box, status, now, meta};
    return box;
  }

  function updateHead(agent) {
    if (!head) return;
    const info = describe(agent, summaryOf(agent.name));
    const [state, label] = viewStatus && !summaryOf(agent.name) ? viewStatus : [info.state, info.label];
    head.status.replaceChildren(chip(state, label));
    if (viewStatus && viewStatus[2]) head.status.title = viewStatus[2];
    head.now.hidden = !info.doing && !info.waiting;
    head.now.replaceChildren(el('b', null, 'Now'), info.doing + (info.doing && info.waiting ? ' ' : ''),
      el('span', 'adir-wait', info.waiting));
    const parts = [info.detail, info.model].filter(Boolean).map((t) => el('span', null, t));
    if (info.tokens != null) parts.push(meter(info));
    parts.push(el('span', null, 'Last activity ' + ago(agent.age)));
    head.meta.replaceChildren(...parts);
    if (comp) comp.update(ctx.canAct, READ_ONLY, replies, agent.steer_messages);
  }

  async function showAgent(agent) {
    shown = agent.name;
    comp = null; viewStatus = null;
    detail.classList.toggle('full', !!agent.can_tail);
    detail.replaceChildren(headerFor(agent));
    if (agent.can_steer_pull) {
      comp = composer({name: agent.name, send: steerSend(base, agent.name, ctx.signal),
        hint: 'Arrives at its next tool call.', sink: () => view && view.note});
    }
    updateHead(agent);
    if (agent.can_tail) {
      const host = el('div', 'adir-view');
      detail.append(host);
      if (comp) detail.append(comp.form);
      try {
        const mod = await import(base + 'transcript.js');
        if (shown !== agent.name || stopped) return;
        view = mod.mount(host, {url: base + 'api/agents/' + encodeURIComponent(agent.name) + '/transcript',
          css: base + 'transcript.css'});
      } catch (err) {
        if (shown === agent.name && !stopped) host.append(el('p', 'adir-note', 'Transcript unavailable: ' + err.message));
      }
      return;
    }
    if (comp) detail.append(comp.form);
    if (!agent.can_view) {
      detail.append(el('p', 'adir-note', agent.kind === 'hermes'
        ? agent.name + ' has not registered a live view, so only its presence shows here.'
        : agent.name + ' does not share its transcript, so only its presence shows here.'));
      return;
    }
    const host = el('div', 'adir-view'), note = el('p', 'adir-note', 'Connecting to ' + agent.name + '…');
    detail.append(note, host);
    const api = base + 'api/a/' + encodeURIComponent(agent.name) + '/';
    try {
      const [mod, cfgRes] = await Promise.all([
        import(base + 'hermes-ui/view.js'),
        viewFetch(api + 'cfg' + (route.session ? '?session=' + encodeURIComponent(route.session) : '')),
      ]);
      const cfg = await cfgRes.json();
      if (!cfgRes.ok) throw new Error(cfg.error || 'HTTP ' + cfgRes.status);
      let brand = null;
      if (cfg.brand) {
        try { brand = (await (await viewFetch(api + 'brand')).json()).data_url || null; } catch (e) { brand = null; }
      }
      if (shown !== agent.name || stopped) return;
      note.remove();
      const readOnly = !ctx.canAct ? (matchMedia('(max-width:720px)').matches   // a phone shows the hint on one line
          ? 'Watching only. Sending needs action access.'
          : 'Read-only: you can watch ' + agent.name + ', but sending, steering and commands need the broker actions permission.')
        : !agent.can_steer ? agent.name + ' does not accept messages or commands from here.' : '';
      view = mod.mount(host, {
        cfg, base: api, css: base + 'hermes-ui/app.css', fetch: viewFetch, brand, readOnly,
        hash: route.id ? '#m' + route.id : '#',
        go: (session, id) => navigate(makeRoute(agent.name, session, id)),
        href: (session) => hrefOf(makeRoute(agent.name, session)),
        permalink: (session, id) => location.origin + location.pathname + hrefOf(makeRoute(agent.name, session, id)),
        storePrefix: 'agentdir.' + agent.name + '.',
        onStatus: (s) => {
          const [state, label] = VIEW_STATUS[s.kind] || (s.busy ? VIEW_STATUS.busy : VIEW_STATUS['']);
          viewStatus = [state, label, s.text || ''];
          const current = agents.find((a) => a.name === agent.name);
          if (current) updateHead(current);
          unread[agent.name] = s.unread || 0;
          renderList();
        },
      });
      themeView(host);
    } catch (err) {
      if (shown !== agent.name || stopped) return;
      note.textContent = 'Could not open the live view: ' + err.message;
    }
  }

  function showRoute() {
    wrap.classList.toggle('picked', !!route.name);
    const agent = agents.find((a) => a.name === route.name);
    if (!route.name) {
      shown = null; head = null;
      detail.classList.remove('full');
      detail.replaceChildren(el('p', 'adir-note', 'Pick an agent to see it live.'));
      return;
    }
    if (!agent) {
      if (shown !== route.name) {
        head = null; detail.classList.remove('full');
        detail.replaceChildren(el('p', 'adir-note', route.name + ' is not registered.'));
      }
      return;
    }
    if (shown !== agent.name) showAgent(agent);
    else updateHead(agent);
  }

  const onHash = () => {
    const next = parseRoute(routeNow());
    if (next.name !== route.name || next.session !== route.session) {
      if (view) { view.destroy(); view = null; }
      shown = null;
    } else if (view && next.id && next.id !== route.id) view.seek(next.id);
    route = next;
    renderList(); showRoute();
  };
  if (standalone) window.addEventListener('hashchange', onHash);
  const stopPoll = poller(ctx, (data, quick) => { agents = data; replies = quick; renderList(); showRoute(); }, error);
  showRoute();
  const cleanup = () => {
    stopped = true; stopPoll();
    if (standalone) window.removeEventListener('hashchange', onHash);
    if (view) { view.destroy(); view = null; }
  };
  if (ctx.signal) ctx.signal.addEventListener('abort', cleanup);
  return cleanup;
}

// Fleet quick view: works on its own without opening the full view. What the agent is doing and who it waits on
// (from the host summary), last activity, model and context, its last few transcript lines streaming live, and a
// composer with quick replies. Claude Code and Codex lines come from the tail; hermes lines from its sidecar.
const QUICK_ROWS = 6;
const QUICK_CSS = `
.aqv{display:grid;gap:14px;color:var(--t1,inherit)}
.aqv-top{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:13px;color:var(--t2)}
.aqv-now{display:grid;gap:8px;padding:14px 16px;border-radius:12px;background:var(--s2)}
.aqv-now[hidden]{display:none}
.aqv-lbl{font-size:12px;font-weight:600;color:var(--t2)}
.aqv-now p{margin:0;font-size:15px;overflow-wrap:anywhere}
.aqv-wait{display:flex;align-items:center;gap:8px;padding:8px 12px;border-radius:10px;font-weight:600;color:var(--wait);
  background:color-mix(in srgb,var(--wait) 13%,var(--s1))}
.aqv-wait::before{content:'';flex:none;width:10px;height:10px;box-sizing:border-box;border-radius:50%;border:2px solid currentColor}
.aqv-wait:empty{display:none}
.aqv-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.aqv-stats div{padding:10px 12px;border:1px solid var(--line);border-radius:10px;min-width:0}
.aqv-stats span{display:block;font-size:12px;color:var(--t2)}
.aqv-stats b{display:block;font-size:15px;font-weight:650;font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.aqv-stats .ad-meter{display:block;width:100%;margin:6px 0 0}
.aqv-live{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--t2)}
.aqv-live::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--t3)}
.aqv-live.on::before{background:var(--run)}
.aqv-rows{list-style:none;margin:0;padding:0;display:grid;gap:10px;font-size:14px}
.aqv-rows:empty{display:none}
.aqv-rows li{display:grid;grid-template-columns:22px minmax(0,1fr) auto;gap:10px;align-items:start}
.aqv-ic{width:22px;height:22px;border-radius:6px;display:grid;place-items:center;font-size:12px;
  color:var(--brand);background:color-mix(in srgb,var(--brand) 13%,var(--s1))}
.aqv-ic.agent{color:var(--run);background:color-mix(in srgb,var(--run) 13%,var(--s1))}
.aqv-ic.user{color:var(--t2);background:var(--s3)}
.aqv-tx{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.aqv-tx code{font-family:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace;font-size:12.5px;color:var(--t2)}
.aqv-who{font-weight:600;margin-right:6px;color:var(--t2)}
.aqv-ts{font-size:12px;color:var(--t3);font-variant-numeric:tabular-nums;white-space:nowrap}
.aqv-sub{margin:0;font-size:12px;color:var(--t2)}
.aqv .ad-comp{margin:0 -24px -24px;padding-left:24px;padding-right:24px;position:sticky;bottom:0}
.aqv .ad-qr{margin:0 -24px;padding:0 24px}
${COMPOSER_CSS}
.ad-st{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;padding:2px 10px;border-radius:999px;
  white-space:nowrap;color:var(--t2);background:color-mix(in srgb,currentColor 13%,var(--s1))}
.ad-st::before{content:'';flex:none;box-sizing:border-box;width:8px;height:8px;border-radius:50%;border:1.5px solid var(--t3)}
.ad-st.run{color:var(--run)}.ad-st.run::before{border:0;background:currentColor}
.ad-st.wait{color:var(--wait)}.ad-st.wait::before{border:2px solid currentColor}
.ad-st.block{color:var(--block)}.ad-st.block::before{border:0;border-radius:2px;background:currentColor}
.ad-st.off{color:var(--t3)}.ad-st.off::before{border:0;height:2px;border-radius:1px;background:currentColor}
.ad-meter{height:6px;border-radius:3px;background:var(--s3);overflow:hidden}
.ad-meter i{display:block;height:100%;background:var(--brand)}.ad-meter.warm i{background:var(--wait)}.ad-meter.hot i{background:var(--block)}
@media (max-width:720px){.aqv .ad-comp{margin:0 -16px -16px;padding-left:16px;padding-right:16px}
  .aqv .ad-qr{margin:0 -16px;padding:0 16px}}`;

// One hermes transcript row as [who, text], or null for rows not worth a quick-view line (tool results, empty steps).
export function rowSummary(row, agentName) {
  if (!row || typeof row !== 'object') return null;
  const calls = Array.isArray(row.tool_calls) ? row.tool_calls : [];
  const images = Array.isArray(row.images) ? row.images.length : 0;
  let line = null;
  if (row.role === 'user') line = [row.sender || 'user', row.content || (images ? '[image]' : '')];
  else if (row.role === 'assistant') {
    if (row.content) line = [agentName || 'agent', row.content];
    else if (calls.length) line = ['tool', calls.map((c) => (c && c.name) || 'tool').join(', ')];
    else if (row.reasoning) line = ['thinking', row.reasoning];
  }
  return line && line[1] ? [String(line[0]), plainText(line[1])] : null;
}

// The quick view prints text, not markdown: keep link labels and drop **bold**, __bold__ and `code` markers.
export function plainText(s) {
  return String(s).replace(/\[([^\]\n]+)\]\([^)\s]+\)/g, '$1')
    .replace(/\*\*([^*\n]+)\*\*|__([^_\n]+)__|`([^`\n]+)`/g, (m, a, b, c) => a || b || c);
}

// Merge rows by id (a re-sent row replaces the old copy) and keep the newest `n` that have a summary.
export function tailRows(have, more, n = QUICK_ROWS) {
  const byId = new Map(have.map((r) => [r.id, r]));
  for (const r of more || []) if (r && Number.isFinite(r.id) && rowSummary(r)) byId.set(r.id, r);
  return [...byId.values()].sort((a, b) => a.id - b.id).slice(-n);
}

const base0 = (p) => String(p || '').split('/').filter(Boolean).pop() || '';
// A tool call in a few words: what ran or what it touched, never its full input.
export function toolLine(name, input) {
  const i = input && typeof input === 'object' ? input : {};
  const short = (v) => String(v).replace(/\s+/g, ' ').slice(0, 120);
  if (i.description) return [String(name), short(i.description)];
  if (i.command) return ['Ran', short(i.command)];
  if (i.file_path || i.notebook_path) return [/^(Edit|MultiEdit|Write|NotebookEdit)$/.test(name) ? 'Edited' : String(name),
    base0(i.file_path || i.notebook_path)];
  if (i.pattern) return [String(name), short(i.pattern)];
  if (i.url || i.query) return [String(name), short(i.url || i.query)];
  return [String(name), ''];
}

// Quick-view lines from one Claude Code transcript record: {kind: agent|tool|user, who, text, code, ts}[].
// Bookkeeping, tool results, meta prompts and harness tags are not conversation and give no line.
export function transcriptLines(record, agentName) {
  if (!record || typeof record !== 'object' || record.isMeta || record.isSidechain) return [];
  const m = record.message, ts = Date.parse(record.timestamp || '') || null;
  if (!m || typeof m !== 'object') return [];
  const blocks = typeof m.content === 'string' ? [{type: 'text', text: m.content}] : Array.isArray(m.content) ? m.content : [];
  const out = [];
  for (const b of blocks) {
    if (!b || typeof b !== 'object') continue;
    if (record.type === 'assistant' && b.type === 'text' && b.text && b.text.trim())
      out.push({kind: 'agent', who: agentName || 'agent', text: plainText(b.text.trim()), ts});
    else if (record.type === 'assistant' && b.type === 'tool_use') {
      const [verb, code] = toolLine(b.name || 'tool', b.input);
      out.push({kind: 'tool', who: 'tool', text: verb, code, ts});
    } else if (record.type === 'user' && b.type === 'text' && b.text && !/^\s*</.test(b.text) && b.text.trim())
      out.push({kind: 'user', who: 'user', text: plainText(b.text.trim()), ts});
  }
  return out;
}

const clock = (ms) => new Date(ms).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});

export function quickView(el0, agentName, ctx = {}) {
  const base = ctx.baseUrl || '/';
  const api = base + 'api/a/' + encodeURIComponent(agentName) + '/';
  const summaryOf = () => { try { return ctx.agentSummary ? ctx.agentSummary(agentName) : null; } catch (e) { return null; } };
  const wrap = el('div', 'aqv'), top = el('div', 'aqv-top'), status = el('span'), detailT = el('span');
  top.append(status, detailT);
  const nowBox = el('div', 'aqv-now'), doing = el('p'), waiting = el('div', 'aqv-wait');
  nowBox.append(el('span', 'aqv-lbl', 'Doing now'), doing, waiting);
  const stats = el('div', 'aqv-stats');
  const stat = (label) => { const d = el('div'), v = el('b'); d.append(el('span', null, label), v); stats.append(d); return [d, v]; };
  const [, lastV] = stat('Last activity'), [, modelV] = stat('Model'), [ctxD, ctxV] = stat('Context');
  const ctxBar = el('span'); ctxD.append(ctxBar);
  const liveEl = el('div', 'aqv-live', 'Live'), rowsEl = el('ol', 'aqv-rows');
  rowsEl.setAttribute('aria-live', 'polite');
  // Presence errors (the poller clears its own on every success) and stream errors stay separate.
  const live = el('p', 'aqv-sub'), error = el('p', 'aqv-sub'), note = el('p', 'aqv-sub');
  error.setAttribute('role', 'status'); note.setAttribute('role', 'status');
  wrap.append(top, nowBox, stats, liveEl, rowsEl, live, note, error);
  el0.replaceChildren(el('style', null, QUICK_CSS), wrap);

  let stopped = false, started = false, es = null, session = null, rows = [], lines = [], running = false, agent = null,
    replies = [], comp = null, mode = null;
  const controller = new AbortController();

  function paintSummary() {
    const info = describe(agent, summaryOf());
    status.replaceChildren(chip(info.state, info.label));
    detailT.textContent = info.detail;
    nowBox.hidden = !info.doing && !info.waiting;
    doing.textContent = info.doing; doing.hidden = !info.doing;
    waiting.textContent = info.waiting;
    lastV.textContent = agent ? ago(agent.age) : '-';
    modelV.textContent = info.model || (agent ? KIND[agent.kind] || agent.kind : '-');
    ctxD.hidden = info.tokens == null;
    ctxV.textContent = info.tokens == null ? '' : kTok(info.tokens);
    ctxBar.replaceChildren(...[info.tokens != null && info.limit && meter(info).querySelector('.ad-meter')].filter(Boolean));
  }

  function paintRows() {
    const items = mode === 'tail' ? lines : rows.map((r) => {
      const [who, text] = rowSummary(r, agentName);
      return {kind: who === 'tool' ? 'tool' : who === agentName ? 'agent' : 'user', who, text,
        ts: Number.isFinite(r.ts) ? r.ts * 1000 : null};
    });
    rowsEl.replaceChildren(...items.map((it) => {
      const li = el('li'), tx = el('span', 'aqv-tx');
      li.append(el('span', 'aqv-ic ' + it.kind, it.kind === 'tool' ? '⚙' : it.kind === 'agent' ? '✦' : '›'));
      if (it.kind === 'user') tx.append(el('span', 'aqv-who', it.who));
      tx.append(it.text);
      if (it.code) tx.append(' ', el('code', null, it.code));
      li.append(tx, el('span', 'aqv-ts', it.ts ? clock(it.ts) : ''));
      return li;
    }));
  }

  function paintComposer() {
    if (!comp || !agent) return;
    const ok = !!ctx.canAct && (mode === 'tail' ? agent.can_steer_pull : agent.can_steer);
    comp.update(ok, ctx.canAct ? agentName + ' does not accept messages from here.' : READ_ONLY, replies, agent.steer_messages);
    if (mode === 'view') comp.extra(running ? [steerB, stopB] : []);
  }

  // Claude Code / Codex: the tail stream. Keep the newest few conversation lines.
  function startTail() {
    mode = 'tail';
    let retry = null;
    const connect = () => {
      if (stopped) return;
      es = new EventSource(base + 'api/agents/' + encodeURIComponent(agentName) + '/transcript');
      es.onopen = () => { liveEl.classList.add('on'); note.textContent = ''; };
      es.onmessage = (m) => {
        let r; try { r = JSON.parse(m.data); } catch (e) { return; }
        const more = transcriptLines(r, agentName);
        if (more.length) { lines = lines.concat(more).slice(-QUICK_ROWS); paintRows(); }
      };
      es.addEventListener('reset', () => { lines = []; paintRows(); });
      es.addEventListener('end', () => { liveEl.classList.remove('on'); es.close(); retry = setTimeout(connect, 2000); });
      es.onerror = () => {
        liveEl.classList.remove('on');
        if (!stopped) note.textContent = 'Reconnecting to ' + agentName + '…';
        if (es.readyState === EventSource.CLOSED && !retry) retry = setTimeout(() => { retry = null; connect(); }, 2000);
      };
    };
    controller.signal.addEventListener('abort', () => clearTimeout(retry));
    connect();
  }

  function onFrame(p) {
    if (!p || typeof p !== 'object') return;
    rows = tailRows(rows, p.rows); paintRows();
    const run = p.live && !p.live.ended ? p.live : null;
    running = !!(run && run.run_id && (run.status === 'running' || run.status === 'starting'));
    live.textContent = run ? 'Working' + (run.stage ? ': ' + run.stage : run.tool ? ': ' + run.tool : '') + '…'
      : p.waiting ? 'Working: ' + (p.phase || 'model') + (Number.isFinite(p.since_ts) ? ' since ' + clock(p.since_ts * 1000) : '') : '';
    paintComposer();
  }

  async function backfill() {
    const before = rows.length ? '&before=' + rows[0].id : '';
    try {
      const r = await viewFetch(api + 'earlier?limit=20&session=' + encodeURIComponent(session) + before,
        {signal: controller.signal});
      if (r.ok && !stopped) { rows = tailRows(rows, (await r.json()).rows); paintRows(); }
    } catch (e) { /* the stream still shows new rows */ }
  }

  // hermes: the sidecar's event stream, through this module's proxy.
  async function startView() {
    mode = 'view';
    try {
      const r = await viewFetch(api + 'cfg', {signal: controller.signal});
      const cfg = await r.json();
      if (!r.ok) throw new Error(cfg.error || 'HTTP ' + r.status);
      if (stopped) return;
      session = cfg.session && cfg.session !== '*' ? cfg.session : cfg.main;
      if (!session) { live.textContent = agentName + ' has no session yet.'; return; }
      let first = true;
      es = new EventSource(api + 'events?session=' + encodeURIComponent(session), {withCredentials: true});
      es.onopen = () => liveEl.classList.add('on');
      es.onmessage = (m) => {
        let p; try { p = JSON.parse(m.data); } catch (e) { return; }
        note.textContent = ''; liveEl.classList.add('on');
        onFrame(p);
        if (first) { first = false; if (rows.length < QUICK_ROWS) backfill(); }
      };
      es.onerror = () => { liveEl.classList.remove('on'); if (!stopped) note.textContent = 'Reconnecting to ' + agentName + '…'; };
    } catch (err) {
      if (!stopped) live.textContent = 'Live view unavailable: ' + err.message;
    }
  }

  async function post(route, body) {
    const r = await viewFetch(api + route, {method: 'POST', signal: controller.signal,
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session, ...body})});
    let j = {}; try { j = await r.json(); } catch (e) { j = {}; }
    if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
    return true;
  }
  const steerB = el('button', 'btn', 'Steer'), stopB = el('button', 'btn', 'Stop');
  steerB.type = stopB.type = 'button';
  steerB.title = 'Pass the typed text to the running turn';
  steerB.onclick = async () => {
    const message = comp.input.value.trim();
    if (!message) { note.textContent = 'Type the steer message first.'; return; }
    try { await post('steer', {message}); comp.input.value = ''; note.textContent = ''; }
    catch (err) { note.textContent = 'Could not steer: ' + err.message; }
  };
  stopB.onclick = async () => {
    try { await post('stop', {}); note.textContent = ''; } catch (err) { note.textContent = 'Could not stop: ' + err.message; }
  };

  const stopPoll = poller(ctx, (agents, quick) => {
    agent = agents.find((a) => a.name === agentName) || null;
    replies = quick;
    if (!agent) {
      status.replaceChildren(chip('off', 'Gone')); detailT.textContent = agentName + ' is not registered.';
      return;
    }
    paintSummary();
    if (!started) {
      started = true;
      if (agent.can_tail) startTail();
      else if (agent.can_view) startView();
      else { liveEl.hidden = true; live.textContent = agentName + ' does not share its transcript, so only its presence shows here.'; }
      if (agent.can_steer_pull && mode === 'tail') {
        comp = composer({name: agentName, send: steerSend(base, agentName, controller.signal),
          hint: 'Arrives at ' + agentName + "'s next tool call."});
      } else if (mode === 'view') {
        comp = composer({name: agentName, max: 8000, hint: 'Sends a new message.', send: (message) => post('say', {message})});
      }
      if (comp) wrap.append(comp.form);
    }
    paintComposer();
  }, error);

  const cleanup = () => {
    if (stopped) return;
    stopped = true; stopPoll(); controller.abort();
    if (es) { es.close(); es = null; }
  };
  if (ctx.signal) ctx.signal.addEventListener('abort', cleanup);
  return cleanup;
}
