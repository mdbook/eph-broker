// Module host contract (docs/modules.md): mount and quickView may return cleanup;
// ctx.baseUrl prefixes UI routes and ctx.signal aborts on unmount.
// The Agents tab: the directory on the left, the picked agent on the right, drawn in the broker's own DOM and dash
// styles (docs/DESIGN.md, mockup docs/mockups/agent-view.html, ticket 3193). One header for every agent kind. A
// hermes agent with a view streams its sidecar's rows through this module's re-typed JSON/SSE proxy (api/a/NAME/...)
// into native replies and collapsed steps; no sidecar page, script or stylesheet is loaded. A claude-code or codex
// agent with the tail cap shows its transcript (transcript.js) and, with steer-pull, a composer under it.
// The optional host hook ctx.agentSummary(name) adds what the host knows (what it is doing, model, context).
const DIR_STATE = {busy: ['run', 'Working'], idle: ['idle', 'Idle'], live: ['run', 'Online'], grey: ['off', 'Offline']};
const STATES = new Set(['run', 'wait', 'idle', 'block', 'off']);
const KIND = {'claude-code': 'Claude Code', codex: 'Codex', hermes: 'Hermes', generic: 'Agent'};

// Shared by the Agents tab and the Fleet quick view, so both read the same. Every control that can sit in a row
// takes its height from --ctl (36px, the 44px tap target on phones) and every view edge from --gut, so a text box
// and the buttons beside it line up by construction. The dash defines --ctl too; these are scoped to the views.
const BASE_CSS = `
.adir,.aqv{--ctl:36px;--gut:20px;--pop:0 8px 24px rgba(20,25,32,.12);color:var(--t1,inherit)}
.aqv{--gut:24px}
:root[data-theme=dark] :is(.adir,.aqv){--pop:0 8px 24px rgba(0,0,0,.45)}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) :is(.adir,.aqv){--pop:0 8px 24px rgba(0,0,0,.45)}}
:is(.adir,.aqv) [hidden]{display:none!important}
:is(.adir,.aqv) :focus-visible{outline:2px solid var(--brand);outline-offset:2px}
:is(.adir,.aqv) .btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-width:0;height:var(--ctl);padding:0 16px;
  border-radius:8px;border:1px solid var(--line);background:var(--s1);color:var(--t1);font:inherit;font-weight:600;font-size:14px;
  line-height:20px;text-decoration:none;cursor:pointer;white-space:nowrap;flex:none;box-sizing:border-box}
:is(.adir,.aqv) .btn:hover{border-color:color-mix(in srgb,var(--t1) 28%,var(--line))}
:is(.adir,.aqv) .btn:active{background:var(--s3);transform:translateY(1px)}
:is(.adir,.aqv) .btn.pri{background:var(--brand);border-color:var(--brand);color:var(--onbrand)}
:is(.adir,.aqv) .btn.pri:hover{background:color-mix(in srgb,var(--brand) 85%,var(--t1));border-color:color-mix(in srgb,var(--brand) 85%,var(--t1))}
:is(.adir,.aqv) .btn.pri:active{background:color-mix(in srgb,var(--brand) 75%,var(--t1))}
:is(.adir,.aqv) .btn.ghost{background:transparent;border-color:transparent}
:is(.adir,.aqv) .btn.ghost:hover{background:var(--s2);border-color:var(--line)}
:is(.adir,.aqv) .btn.sq{width:var(--ctl);min-width:var(--ctl);padding:0}
:is(.adir,.aqv) .btn:disabled,:is(.adir,.aqv) .btn:disabled:hover{opacity:.5;cursor:default;transform:none;border-color:var(--line)}
:is(.adir,.aqv) .btn.pri:disabled,:is(.adir,.aqv) .btn.pri:disabled:hover{background:var(--brand);border-color:var(--brand)}
:is(.adir,.aqv) .lnk{display:inline-flex;align-items:center;color:var(--brand);font-weight:600;text-decoration:none;background:none;
  border:0;padding:0;cursor:pointer;font-size:14px;line-height:20px;white-space:nowrap;border-radius:4px}
:is(.adir,.aqv) .lnk:hover{text-decoration:underline}
.ad-st{flex:none;display:inline-flex;align-items:center;gap:8px;height:24px;font-size:13px;line-height:20px;font-weight:600;padding:0 12px;
  border-radius:999px;white-space:nowrap;color:var(--t2);background:color-mix(in srgb,currentColor 13%,var(--s1));box-sizing:border-box}
.ad-st::before{content:'';flex:none;box-sizing:border-box;width:8px;height:8px;border-radius:50%;border:1.5px solid var(--t3)}
.ad-st.run{color:var(--run)}.ad-st.run::before{border:0;background:currentColor}
.ad-st.wait{color:var(--wait)}.ad-st.wait::before{border:2px solid currentColor}
.ad-st.block{color:var(--block)}.ad-st.block::before{border:0;border-radius:2px;background:currentColor}
.ad-st.off{color:var(--t3)}.ad-st.off::before{border:0;height:2px;border-radius:1px;background:currentColor}
.ad-meter{display:inline-block;flex:none;width:56px;height:6px;border-radius:3px;background:var(--s3);overflow:hidden}
.ad-meter i{display:block;height:100%;background:var(--brand)}.ad-meter.warm i{background:var(--wait)}.ad-meter.hot i{background:var(--block)}

/* ONE header. Line 1: back, name, status, actions. Then what it is doing now, then harness, model, context, session,
   last activity. Header, body and composer share --gut, so the name, the first reply and the text box share an edge. */
.ad-vh{flex:none;display:grid;gap:8px;padding:12px var(--gut) 16px;border-bottom:1px solid var(--line)}
.ad-vh-top{display:flex;align-items:center;gap:12px;min-width:0;min-height:var(--ctl)}
.ad-vh-top h2{margin:0;font-size:18px;line-height:24px;font-weight:650;letter-spacing:-.01em;min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.ad-sp{flex:1}
.ad-now{margin:0;font-size:14px;overflow-wrap:anywhere}.ad-now b{color:var(--t2);font-weight:600;margin-right:8px}
.ad-wait{color:var(--wait);font-weight:600}
.ad-meta{display:flex;align-items:center;flex-wrap:wrap;gap:4px 12px;font-size:13px;line-height:20px;color:var(--t2);
  font-variant-numeric:tabular-nums;min-width:0}
.ad-meta > span{display:inline-flex;align-items:center;gap:8px;white-space:nowrap}
.ad-meta .ad-sess{color:var(--t1);font-weight:600}

/* The single actions menu, and the Send menu: a popover anchored to its button. */
.ad-menu-w{position:relative;flex:none;display:flex}
.ad-menu{position:absolute;right:0;top:calc(100% + 8px);z-index:10;min-width:220px;padding:4px;margin:0;box-sizing:border-box;
  background:var(--s1);border:1px solid var(--line);border-radius:10px;box-shadow:var(--pop)}
.ad-menu button{display:flex;width:100%;align-items:center;justify-content:space-between;gap:12px;min-height:var(--ctl);padding:0 12px;
  border:0;background:none;border-radius:6px;font:inherit;font-size:14px;line-height:20px;color:var(--t1);text-align:left;cursor:pointer;
  white-space:nowrap}
.ad-menu button:hover,.ad-menu button:focus-visible{background:var(--s2)}
.ad-menu button:focus-visible{outline-offset:-2px}
.ad-menu button:disabled{opacity:.5;cursor:default;background:none}
.ad-menu button.bad{color:var(--block)}
.ad-menu button span{font-size:13px;color:var(--t2)}
.ad-menu hr{border:0;border-top:1px solid var(--line);margin:4px}
.ad-menu .ad-lbl{font-size:12px;line-height:16px;font-weight:500;color:var(--t2);padding:8px 12px 4px}

/* Body: a plain surface. Replies are the loudest thing in it; steps are one quiet collapsed line per turn. */
.ad-tl{display:flex;flex-direction:column;gap:12px;padding:16px var(--gut) 24px;background:var(--s1);min-width:0}
.ad-tl > *{flex-shrink:0}
.ad-ev{max-width:760px;border-radius:14px;padding:8px 16px;border:1px solid var(--line);background:var(--s1);min-width:0;
  overflow-wrap:anywhere;box-sizing:border-box}
.ad-ev.user{background:color-mix(in srgb,var(--brand) 7%,var(--s1));border-color:color-mix(in srgb,var(--brand) 22%,var(--line));
  align-self:flex-end;max-width:620px}
.ad-ev.agent{border:0;padding-left:0;padding-right:0}
.ad-ev-meta{display:flex;align-items:center;gap:8px;font-size:12px;line-height:16px;color:var(--t2);margin-bottom:4px}
.ad-who{font-weight:600;color:var(--t1)}.ad-ev.user .ad-who{color:var(--brand)}
.ad-ts{font-size:12px;color:var(--t3);font-variant-numeric:tabular-nums}
.ad-md p{margin:.35em 0}.ad-md ul,.ad-md ol{margin:.35em 0;padding-left:1.4em}
.ad-md code{font-family:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace;background:var(--s2);border:1px solid var(--line);
  border-radius:5px;padding:0 4px;font-size:12px}
.ad-md pre{margin:.5em 0;padding:8px 12px;border-radius:8px;background:var(--s2);overflow:auto;font-size:12px}
.ad-md pre code{border:0;padding:0;background:none}
.ad-md a{color:var(--brand)}
.ad-steps{max-width:760px;font-size:13px;color:var(--t2)}
/* The -8px margin puts the summary text on the --gut edge; the hover fill hangs into the gutter. */
.ad-steps > summary{list-style:none;display:inline-flex;align-items:center;gap:8px;min-height:32px;padding:0 8px;margin-left:-8px;
  max-width:calc(100% + 8px);border-radius:8px;cursor:pointer;font-variant-numeric:tabular-nums;line-height:20px;box-sizing:border-box}
.ad-steps > summary::-webkit-details-marker{display:none}
.ad-steps > summary:hover{background:var(--s2);color:var(--t1)}
.ad-steps .ad-sep{color:var(--t3)}
.ad-steps .ad-tgl{color:var(--brand);font-weight:600}
.ad-steps > summary:hover .ad-tgl{text-decoration:underline}
.ad-steps-body{display:grid;gap:8px;margin:4px 0 4px 4px;padding-left:12px;border-left:2px solid var(--line)}
.ad-step{display:flex;align-items:baseline;gap:8px;min-width:0}
.ad-step .k{flex:none;font-weight:600;color:var(--t2);min-width:64px}
.ad-step .v{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ad-step code{font-family:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace;font-size:12px;color:var(--t2)}
.ad-step.think .v{white-space:normal;color:var(--t2)}
.ad-step .ad-ts{margin-left:auto;flex:none}
.ad-working{display:flex;align-items:center;gap:8px;font-size:13px;line-height:20px;color:var(--t2);font-variant-numeric:tabular-nums;min-width:0}
.ad-working::before{content:"";flex:none;width:8px;height:8px;border-radius:50%;background:var(--run);animation:ad-pulse 1.4s ease-in-out infinite}
@keyframes ad-pulse{50%{opacity:.3}}
.ad-empty{margin:0;font-size:13px;color:var(--t2)}

/* Composer: one Send. While the agent works, Send queues for its next turn and the split arrow offers Interrupt and
   send. The text box grows upward; the buttons stay on its bottom edge, so at one line they match exactly. */
.ad-comp{flex:none;display:grid;grid-template-columns:minmax(0,1fr);gap:8px;padding:12px var(--gut) calc(12px + env(safe-area-inset-bottom));
  border-top:1px solid var(--line);background:var(--s1)}
.ad-qr{display:flex;gap:8px;overflow-x:auto;scrollbar-width:none;margin:0 calc(0px - var(--gut));padding:0 var(--gut)}
.ad-qr::-webkit-scrollbar{display:none}
.ad-qr button{flex:none;font:inherit;font-size:13px;font-weight:600;color:var(--t1);background:var(--s1);
  border:1px solid var(--line);border-radius:999px;min-height:32px;padding:0 12px;cursor:pointer;white-space:nowrap}
.ad-qr button:hover:not(:disabled){background:var(--s2)}
.ad-qr button:disabled{opacity:.5;cursor:not-allowed}
.ad-row{display:flex;align-items:flex-end;gap:8px;min-width:0}
.ad-row textarea{display:block;flex:1;min-width:0;box-sizing:border-box;resize:none;margin:0;font:inherit;font-size:14px;line-height:20px;
  height:var(--ctl);min-height:var(--ctl);max-height:140px;overflow-y:auto;padding:calc((var(--ctl) - 22px) / 2) 12px;
  border:1px solid var(--line);border-radius:8px;background:var(--s1);color:var(--t1)}
.ad-row textarea::placeholder{color:var(--t3);opacity:1}
.ad-row textarea:hover{border-color:color-mix(in srgb,var(--t1) 28%,var(--line))}
.ad-row textarea:focus{border-color:var(--brand)}
.ad-row textarea:focus-visible{outline-offset:0}
.ad-row textarea:disabled{opacity:.6}
.ad-split{display:inline-flex;flex:none;position:relative}
.ad-split .btn.ad-main{border-radius:8px 0 0 8px}
.ad-split.solo .btn.ad-main{border-radius:8px}
.ad-split .btn.ad-more{margin:0;border-radius:0 8px 8px 0;border-left-color:color-mix(in srgb,var(--onbrand) 35%,var(--brand));width:var(--ctl);
  min-width:var(--ctl);padding:0}
.ad-split .ad-menu{bottom:calc(100% + 8px);top:auto;min-width:260px}
.ad-hint{margin:0;font-size:12px;line-height:16px;color:var(--t2)}
.ad-hint:empty{display:none}
.ad-sent{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:minmax(0,1fr);gap:4px;font-size:13px}
.ad-sent li{display:flex;gap:8px;justify-content:space-between}.ad-sent span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ad-sent b{flex:none;font-weight:600;color:var(--t2)}.ad-sent b.ok{color:var(--run)}
@media (prefers-reduced-motion:reduce){:is(.adir,.aqv) *{animation:none!important;transition:none!important}}
/* Phones: every control grows to the 44px tap target through --ctl and every edge moves to 16px through --gut.
   The text box is 16px so iOS does not zoom on focus. */
@media (max-width:720px){
  .adir,.aqv{--ctl:44px;--gut:16px}
  .ad-vh{padding:8px var(--gut) 12px;gap:4px}.ad-vh-top{gap:8px}.ad-vh-top h2{font-size:16px}
  .ad-meta{font-size:12px;gap:0 12px}.ad-meta .opt{display:none}
  .ad-now{font-size:13px}
  .ad-tl{padding:12px var(--gut)}
  .ad-ev.user{max-width:88%}
  .ad-row textarea{font-size:16px}
  .ad-steps > summary{min-height:var(--ctl)}
  .ad-qr button{min-height:var(--ctl)}
}`;

const CSS = BASE_CSS + `
.adir{display:grid;grid-template-columns:260px 1fr;gap:16px;align-items:start}
.adir-card{background:var(--s1,transparent);border:1px solid var(--line,currentColor);border-radius:12px;box-shadow:var(--shadow,none);min-width:0}
.adir-detail{position:sticky;top:80px;max-height:calc(100dvh - 96px);overflow:auto}
.adir-detail.full{display:flex;flex-direction:column;height:calc(100dvh - 96px);overflow:hidden}
.adir-list{padding:4px 0}
.adir-it{display:flex;align-items:center;gap:12px;padding:16px 20px 16px 17px;border-left:3px solid transparent;
  color:inherit;text-decoration:none}
.adir-it:hover{background:var(--s2,transparent)}
.adir-it:focus-visible{outline-offset:-2px}
.adir-it[aria-current=true]{background:color-mix(in srgb,var(--brand) 7%,var(--s1));border-left-color:var(--brand,currentColor)}
.adir-it.grey{color:var(--t2,inherit)}.adir-grow{flex:1;min-width:0}.adir-nm{font-weight:600;overflow-wrap:anywhere}
.adir-meta,.adir-sub{font-size:13px;color:var(--t2,inherit)}
.adir-dot{width:8px;height:8px;border-radius:50%;background:var(--brand,currentColor)}
.adir-back{display:none;flex:none;color:var(--brand);font-size:14px;font-weight:600;text-decoration:none;white-space:nowrap;border-radius:4px}
.adir-back:hover{text-decoration:underline}
.adir-detail > .ad-tl{flex:1;min-height:0;overflow:auto}
.adir-view{height:max(480px,calc(100vh - 220px));position:relative;border-radius:0 0 12px 12px}
.adir-detail.full .adir-view{flex:1;height:auto;min-height:0;border-radius:0;overflow:hidden}
.adir-note{margin:0;padding:16px var(--gut);color:var(--t2,inherit)}
.adir-empty{padding:12px 14px;color:var(--t2,inherit)}
/* Hosted (data-fill, the dash's Agents tab): the host gives the directory exactly the space under its header, so the
   page never scrolls. Only the transcript and a long list scroll, and the card header and composer stay on screen
   (ticket 3322). Standalone keeps the sticky card above. */
.adir[data-fill]{flex:1;min-height:0;grid-template-rows:minmax(0,1fr);align-items:stretch}
.adir[data-fill] .adir-listcard{align-self:start;max-height:100%;overflow:auto}
.adir[data-fill] .adir-detail{position:static;max-height:100%;min-height:0;height:auto}
.adir[data-fill] .adir-detail:not(.full){align-self:start;overflow:auto}
.adir[data-fill] .adir-detail.full{height:auto}
@media (max-width:820px){
  .adir{grid-template-columns:1fr}.adir.picked .adir-listcard{display:none}.adir:not(.picked) .adir-detail{display:none}
  .adir-detail{position:static;max-height:none;overflow:visible}
  .adir-back{display:inline-flex;min-height:var(--ctl);align-items:center}.adir-view{height:calc(100vh - 160px)}
}
@media (max-width:720px){
  .adir-back{margin-left:-8px;padding:0 8px}
  .adir-view{height:calc(100dvh - 132px - env(safe-area-inset-top) - env(safe-area-inset-bottom))}
  .adir-detail.full{height:calc(100dvh - 80px - env(safe-area-inset-bottom));border-radius:10px}
}`;


function ago(seconds) {
  const s = Math.floor(seconds);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  return Math.floor(s / 3600) + 'h ago';
}

// Durations per the copy rules: 38s, 1m 12s, 2m 05s, 1h 02m.
export function dur(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  const pad = (n) => String(n).padStart(2, '0');
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + pad(s % 60) + 's';
  return Math.floor(s / 3600) + 'h ' + pad(Math.floor(s / 60) % 60) + 'm';
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
// Give the caller the real status back so its own error handling applies.
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

export function steerState(state, reason) {
  if (state === 'not_delivered') return 'Not delivered: ' + (reason || 'wake channel failed');
  if (state === 'sent' || state === 'waking') return 'Sent, awaiting consumption';
  return state === 'delivered' ? 'Delivered' : 'Queued';
}

export function steerHint(agent) {
  return agent.status === 'idle' ? agent.wake ? 'Wakes this agent through its configured channel.'
    : 'Not delivered: agent idle, no wake channel' : 'Arrives at its next tool call.';
}

// The composer's Send control. Idle, or with no way to interrupt: one plain Send. Working with an interrupt route:
// Send queues for the next turn and the split arrow offers "Interrupt and send".
export function sendMode(working, canInterrupt, name) {
  const split = !!(working && canInterrupt);
  return {split, hint: split ? (name || 'The agent') + ' is working. Send queues your message for its next turn.' : ''};
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
  return el('span', 'ad-st ' + (STATES.has(state) ? state : 'off'), label);
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

// The harness, model, context and last-activity line under the name, shared by the tab and the quick view.
function metaParts(info, agent, session) {
  const parts = [info.detail, info.model].filter(Boolean).map((t) => el('span', null, t));
  if (info.tokens != null) parts.push(meter(info));
  if (session) {
    const s = el('span', 'opt', 'Session '); s.append(el('span', 'ad-sess', session)); parts.push(s);
  }
  if (agent) parts.push(el('span', 'opt', 'Last activity ' + ago(agent.age)));
  return parts;
}

// A menu button and its popover. Items are {label, note, bad, run} buttons, {heading} labels or 'hr'. Opening moves
// focus into the menu; Escape, a pick or a press outside closes it and returns focus to the button.
function popMenu(button, items) {
  const wrap = el('div', 'ad-menu-w'), menu = el('div', 'ad-menu');
  menu.setAttribute('role', 'menu'); menu.hidden = true;
  button.type = 'button';
  button.setAttribute('aria-haspopup', 'menu'); button.setAttribute('aria-expanded', 'false');
  const live = () => [...menu.querySelectorAll('button')].filter((b) => !b.hidden && !b.disabled);
  const outside = (e) => { if (!wrap.contains(e.target)) close(false); };
  function close(focus) {
    if (menu.hidden) return;
    menu.hidden = true; button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', outside, true);
    if (focus) button.focus();
  }
  function open() {
    menu.hidden = false; button.setAttribute('aria-expanded', 'true');
    document.addEventListener('pointerdown', outside, true);
    const first = live()[0];
    if (first) first.focus();
  }
  button.addEventListener('click', () => { if (menu.hidden) open(); else close(true); });
  wrap.addEventListener('keydown', (e) => {
    if (menu.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); return; }
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const all = live(), i = all.indexOf(document.activeElement);
    if (all.length) all[(i + (e.key === 'ArrowDown' ? 1 : all.length - 1)) % all.length].focus();
  });
  const set = (list) => {
    menu.replaceChildren(...list.map((it) => {
      if (it === 'hr') return el('hr');
      if (it.heading) return el('div', 'ad-lbl', it.heading);
      const b = el('button', it.bad ? 'bad' : null, it.label);
      b.type = 'button'; b.setAttribute('role', 'menuitem');
      if (it.note) b.append(el('span', null, it.note));
      b.onclick = () => { close(true); it.run(); };
      if (it.hidden) b.hidden = true;
      return b;
    }));
    wrap.hidden = !list.some((it) => it !== 'hr' && !it.heading && !it.hidden);
  };
  set(items || []);
  wrap.append(button, menu);
  return {wrap, set, close};
}

// One composer for the detail page and the quick view. send(text) resolves to a receipt (tracked until
// delivered), true (sent, nothing to track) or throws. interrupt(text), when given, stops the current turn and sends;
// it is offered only while the agent works (sendMode). Quick replies send their text in one tap.
function composer(opts) {
  const form = el('form', 'ad-comp'), qr = el('div', 'ad-qr'), row = el('div', 'ad-row');
  const input = el('textarea'), split = el('div', 'ad-split'), send = el('button', 'btn pri ad-main', 'Send');
  const more = el('button', 'btn pri ad-more', '▾'), hint = el('p', 'ad-hint'), sent = el('ul', 'ad-sent');
  input.rows = 1; input.maxLength = opts.max || 4000; input.placeholder = 'Message ' + opts.name;
  input.setAttribute('aria-label', 'Message ' + opts.name);
  send.type = 'submit';
  more.setAttribute('aria-label', 'More send options'); more.title = 'More send options';
  const moreMenu = popMenu(more, [{label: 'Interrupt and send', note: 'stops the current turn',
    run: () => sendNow(opts.interrupt)}]);
  split.append(send, moreMenu.wrap);
  qr.setAttribute('role', 'group'); qr.setAttribute('aria-label', 'Quick replies');
  hint.setAttribute('role', 'status'); sent.setAttribute('aria-live', 'polite');
  row.append(input, split);
  form.append(qr, row, hint, sent);
  sent.hidden = !!opts.sink;
  let busy = false, canSend = false, replies = [], why = '', working = false;
  const mine = new Map();   // id -> {text, state, li}
  function paint() {
    for (const b of [input, send, more, ...qr.children]) b.disabled = !canSend || busy;
    const mode = sendMode(working && canSend, !!opts.interrupt, opts.name);
    moreMenu.wrap.hidden = !mode.split;
    split.classList.toggle('solo', !mode.split);
    if (!mode.split) moreMenu.close(false);
    hint.textContent = why || mode.hint || opts.hint || '';
  }
  // The box starts one control tall (--ctl) and grows with its content up to its max-height.
  function grow() {
    input.style.height = '';
    if (input.scrollHeight > input.clientHeight) input.style.height = (input.scrollHeight + input.offsetHeight - input.clientHeight) + 'px';
  }
  function receipt(id, rec) {
    const sink = opts.sink && opts.sink();
    if (sink) sink({type: 'broker', subtype: 'web_message', id, state: rec.state, via: rec.via || 'tool',
      reason: rec.reason, text: rec.text, timestamp: new Date().toISOString()});
    if (rec.li) {
      rec.li.lastChild.textContent = steerState(rec.state, rec.reason);
      rec.li.lastChild.className = rec.state === 'delivered' ? 'ok' : '';
    }
  }
  async function submit(text, via) {
    if (!canSend || busy || !text.trim()) return false;
    busy = true; why = ''; paint();
    try {
      const result = await (via || opts.send)(text);
      const id = typeof result === 'string' ? result : result && result.id;
      if (typeof id === 'string') {
        const rec = {text, state: 'queued', li: null, ...(typeof result === 'object' ? result : {})};
        mine.set(id, rec);
        const sink = opts.sink && opts.sink();
        if (!sink) {
          rec.li = el('li'); rec.li.append(el('span', null, text), el('b'));
          sent.hidden = false; sent.append(rec.li);
          while (sent.children.length > 3) sent.firstChild.remove();
        }
        receipt(id, rec);
      }
      return true;
    } catch (err) { why = 'Could not send: ' + err.message; return false; }
    finally { busy = false; paint(); }
  }
  async function sendNow(via) {
    if (await submit(input.value, via)) { input.value = ''; grow(); input.focus(); }
  }
  form.onsubmit = (e) => { e.preventDefault(); sendNow(); };
  input.addEventListener('input', grow);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !matchMedia('(pointer:coarse)').matches) {
      e.preventDefault(); form.requestSubmit();
    }
  });
  paint();
  return {form, input, submit,
    // ok: may send now; reason: shown when it may not; list: quick replies; states: the agent's steer_messages.
    update(ok, reason, list, states, deliveryHint) {
      canSend = !!ok; why = ok ? why : reason || '';
      if (deliveryHint !== undefined) opts.hint = deliveryHint;
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
        const state = m.state === 'pending' ? 'queued' : m.state;
        if (!rec || rec.state === 'delivered' || rec.state === state) continue;
        Object.assign(rec, m, {state});
        receipt(m.id, rec);
      }
      paint();
    },
    // The agent is mid-turn: offer Interrupt and send beside Send.
    working(flag) { if (working !== !!flag) { working = !!flag; paint(); } },
    // A failure from outside the box (Stop in the actions menu), shown where send errors show.
    notice(text) { why = text; paint(); },
  };
}

function steerSend(base, name, signal) {
  return async (text) => {
    const response = await viewFetch(base + 'api/agents/' + encodeURIComponent(name) + '/steer', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text}), signal,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
    return {...data, state: data.state === 'pending' ? 'queued' : data.state};
  };
}

// The sidecar's say / stop routes for one session. Interrupt and send is stop, then say.
export function viewActions(api, session, signal) {
  async function post(route, body) {
    const r = await viewFetch(api + route, {method: 'POST', signal,
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session: session(), ...body})});
    let j = {}; try { j = await r.json(); } catch (e) { j = {}; }
    if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
    return true;
  }
  return {say: (message) => post('say', {message}), stop: () => post('stop', {}),
    interrupt: async (message) => { await post('stop', {}); return post('say', {message}); }};
}

const READ_ONLY = 'Read-only: sending needs the broker actions permission.';
const NO_MESSAGES = (name) => name + ' does not accept messages from here.';

// ---- transcript items. Everything below turns a sidecar row or a tail record into
// {kind: user|agent|step, key, who, text, verb, code, ts} and groups consecutive steps into one quiet line.

function callInput(c) {
  let v = c.args != null ? c.args : c.arguments != null ? c.arguments : c.input != null ? c.input
    : c.function && c.function.arguments;
  if (typeof v === 'string') { try { v = JSON.parse(v); } catch (e) { v = {}; } }
  return v && typeof v === 'object' ? v : {};
}
const callName = (c) => String((c && (c.name || (c.function && c.function.name))) || 'tool');

// One hermes row as items: reasoning and tool calls become steps, content a reply. Tool results give none.
export function rowItems(row, agentName) {
  if (!row || typeof row !== 'object') return [];
  const ts = Number.isFinite(row.ts) ? row.ts * 1000 : null, key = 'r' + row.id;
  const images = Array.isArray(row.images) ? row.images.length : 0;
  if (row.role === 'user') {
    const text = row.content || (images ? '[image]' : '');
    return text ? [{kind: 'user', key, id: row.id, who: String(row.sender || 'you'), text: String(text), ts}] : [];
  }
  if (row.role !== 'assistant') return [];
  const out = [];
  if (row.reasoning) out.push({kind: 'step', key: key + 't', verb: 'Thought', text: String(row.reasoning), ts});
  if (row.content) out.push({kind: 'agent', key, id: row.id, who: agentName || 'agent', text: String(row.content), ts});
  (Array.isArray(row.tool_calls) ? row.tool_calls : []).forEach((c, i) => {
    if (!c) return;
    const [verb, code] = toolLine(callName(c), callInput(c));
    out.push({kind: 'step', key: key + 'c' + i, verb, code, ts});
  });
  return out;
}

// "5 tool calls · 38s": how many tools a run of steps called, then how long it took. A run of thinking alone is
// "Thinking". start and end, when known, are the messages around the run (the ask and the reply), so the time is
// the turn's, not only the gap between its first and last step. The Show / Hide steps toggle follows it (turnNodes).
export function stepsSummary(steps, start, end) {
  const calls = (steps || []).filter((s) => s.verb !== 'Thought').length;
  const times = [start, ...(steps || []).map((s) => s.ts), end].filter(Number.isFinite);
  const span = times.length > 1 ? Math.max(...times) - Math.min(...times) : 0;
  return (calls ? calls + (calls === 1 ? ' tool call' : ' tool calls') : 'Thinking') + (span >= 1000 ? ' · ' + dur(span) : '');
}

export function groupTurns(items) {
  const out = [];
  let steps = null;
  for (const it of items || []) {
    if (!it) continue;
    if (it.kind === 'step') {
      if (!steps) out.push(steps = {type: 'steps', key: it.key, steps: []});
      steps.steps.push(it);
      continue;
    }
    steps = null;
    out.push({type: 'msg', ...it});
  }
  out.forEach((g, i) => {
    if (g.type === 'steps') g.summary = stepsSummary(g.steps, out[i - 1] && out[i - 1].ts, out[i + 1] && out[i + 1].ts);
  });
  return out;
}

// What the sidecar says is running now, or null when the agent is between turns.
export function workState(frame) {
  if (!frame || typeof frame !== 'object') return null;
  const run = frame.live && !frame.live.ended ? frame.live : null;
  if (run && run.run_id && (run.status === 'running' || run.status === 'starting')) {
    const what = run.stage || (run.tool ? 'running ' + run.tool : '');
    return {what: String(what || ''), since: Number.isFinite(run.since_ts) ? run.since_ts * 1000 : null, run: true};
  }
  if (frame.waiting) {
    return {what: String(frame.phase || 'model'), since: Number.isFinite(frame.since_ts) ? frame.since_ts * 1000 : null,
      run: false};
  }
  return null;
}

// Markdown as DOM: paragraphs, lists, fenced and inline code, bold and http(s) links. Text only; nothing is parsed
// as HTML.
function inlineNodes(text) {
  const out = [], re = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>()]*[^\s<>().,;:!?'"])/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] != null) out.push(el('code', null, m[1]));
    else if (m[2] != null) out.push(el('strong', null, m[2]));
    else {
      const a = el('a', null, m[3] || m[5]);
      a.href = m[4] || m[5]; a.target = '_blank'; a.rel = 'noopener noreferrer';
      out.push(a);
    }
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function mdNodes(src) {
  const out = [];
  String(src || '').split('```').forEach((part, i) => {
    if (i % 2) {
      const nl = part.indexOf('\n'), pre = el('pre');
      pre.append(el('code', null, (nl >= 0 ? part.slice(nl + 1) : part).replace(/\n$/, '')));
      out.push(pre);
      return;
    }
    let para = [], list = null;
    const flush = () => {
      if (para.length) {
        const p = el('p');
        para.forEach((line, j) => { if (j) p.append(el('br')); p.append(...inlineNodes(line)); });
        out.push(p); para = [];
      }
      if (list) { out.push(list); list = null; }
    };
    for (const line of part.split('\n')) {
      const item = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(line);
      if (!line.trim()) flush();
      else if (item) {
        if (para.length) flush();
        if (!list) list = el(/\d/.test(item[1]) ? 'ol' : 'ul');
        const li = el('li'); li.append(...inlineNodes(item[2])); list.append(li);
      } else {
        if (list) flush();
        para.push(line.replace(/^#{1,6}\s+/, ''));
      }
    }
    flush();
  });
  return out;
}

const clock = (ms) => new Date(ms).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});

// Grouped items as DOM. Steps start collapsed unless `open` says otherwise (openKeys survives a repaint).
function turnNodes(groups, openKeys, allOpen) {
  return groups.map((g) => {
    if (g.type === 'steps') {
      const d = el('details', 'ad-steps'), body = el('div', 'ad-steps-body'), sum = el('summary');
      const toggle = el('span', 'ad-tgl');
      sum.append(el('span', null, g.summary), el('span', 'ad-sep', '·'), toggle);
      d.append(sum, body);
      for (const s of g.steps) {
        const row = el('div', 'ad-step' + (s.verb === 'Thought' ? ' think' : '')), v = el('span', 'v');
        if (s.code) v.append(el('code', null, s.code)); else v.textContent = s.text || '';
        if (s.code) v.title = s.code;
        row.append(el('span', 'k', s.verb), v);
        if (s.ts) row.append(el('span', 'ad-ts', clock(s.ts)));
        body.append(row);
      }
      d.open = allOpen || openKeys.has(g.key);
      toggle.textContent = d.open ? 'Hide steps' : 'Show steps';
      d.addEventListener('toggle', () => {
        if (d.open) openKeys.add(g.key); else openKeys.delete(g.key);
        toggle.textContent = d.open ? 'Hide steps' : 'Show steps';
      });
      return d;
    }
    const box = el('div', 'ad-ev ' + (g.kind === 'user' ? 'user' : 'agent')), meta = el('div', 'ad-ev-meta');
    meta.append(el('span', 'ad-who', g.who));
    if (g.ts) meta.append(el('span', 'ad-ts', clock(g.ts)));
    const md = el('div', 'ad-md');
    md.append(...mdNodes(g.text));
    if (g.id != null) box.dataset.mid = String(g.id);
    box.append(meta, md);
    return box;
  });
}

// A hermes session, live: rows from the sidecar's event stream (plus one backfill) painted into `box` as replies and
// collapsed steps, with a working line at the end. limit keeps only the newest rows (the quick view).
function liveView(box, o) {
  let rows = [], es = null, retry = null, tick = null, work = null, workSince = null, first = true, stopped = false;
  let follow = true, allOpen = false;
  const openKeys = new Set(), line = el('div', 'ad-working'), controller = new AbortController();
  line.setAttribute('role', 'status');
  const max = o.limit || 300;
  function paintWork() {
    line.hidden = !work;
    if (!work) return;
    const since = work.since || workSince;
    line.textContent = ['Working', work.what, since ? dur(Date.now() - since) : ''].filter(Boolean).join(' · ');
  }
  function paint() {
    const items = rows.flatMap((r) => rowItems(r, o.name));
    const nodes = turnNodes(groupTurns(items), openKeys, allOpen);
    if (!nodes.length && !work) nodes.push(el('p', 'ad-empty', o.empty || 'No messages in this session yet.'));
    paintWork();
    box.replaceChildren(...nodes, line);
    if (follow && o.scroll) box.scrollTop = box.scrollHeight;
  }
  function onFrame(p) {
    if (Array.isArray(p.rows) && p.rows.length) rows = tailRows(rows, p.rows, max);
    const next = workState(p);
    if (!!next !== !!work) { workSince = next ? Date.now() : null; o.onWork(next); }
    work = next;
    clearInterval(tick); tick = work ? setInterval(paintWork, 1000) : null;
    paint();
  }
  async function backfill() {
    const before = rows.length ? '&before=' + rows[0].id : '';
    try {
      const r = await viewFetch(o.api + 'earlier?limit=' + Math.min(max, 100) + '&session=' + encodeURIComponent(o.session) + before,
        {signal: controller.signal});
      if (r.ok && !stopped) {
        rows = tailRows(rows, (await r.json()).rows, max); paint();
        if (o.seek) seek(o.seek);
      }
    } catch (e) { /* the stream still shows new rows */ }
  }
  function connect() {
    if (stopped) return;
    if (es) es.close();
    const current = es = new EventSource(o.api + 'events?session=' + encodeURIComponent(o.session), {withCredentials: true});
    current.onopen = () => o.onNote('');
    current.onmessage = (m) => {
      let p; try { p = JSON.parse(m.data); } catch (e) { return; }
      o.onNote('');
      if (p && typeof p === 'object') onFrame(p);
      if (first) { first = false; if (rows.length < max) backfill(); }
    };
    current.onerror = () => {
      if (stopped) return;
      o.onNote('Reconnecting to ' + o.name + '…');
      // An HTTP error closes an EventSource for good; retry like the tail view does.
      if (current.readyState === EventSource.CLOSED && retry === null) retry = setTimeout(() => { retry = null; connect(); }, 2000);
    };
  }
  function seek(id) {
    const target = box.querySelector('[data-mid="' + String(id).replace(/\D/g, '') + '"]');
    if (target) { follow = false; target.scrollIntoView({block: 'center'}); }
  }
  if (o.scroll) {
    box.addEventListener('scroll', () => { follow = box.scrollTop + box.clientHeight >= box.scrollHeight - 40; }, {passive: true});
  }
  paint();
  connect();
  return {
    destroy() { stopped = true; controller.abort(); clearTimeout(retry); clearInterval(tick); if (es) es.close(); },
    seek,
    note() {},
    // Show or hide every step at once; returns whether they are now shown.
    toggleSteps() { allOpen = !allOpen; openKeys.clear(); paint(); return allOpen; },
  };
}

// Normalized session list from the sidecar's cfg: strings or {id|name, title, ts} objects, newest few.
export function sessionList(cfg) {
  const raw = cfg && Array.isArray(cfg.sessions) ? cfg.sessions : [];
  return raw.map((s) => {
    if (typeof s === 'string' && s) return {id: s, label: s, ts: null};
    if (!s || typeof s !== 'object') return null;
    const id = typeof s.id === 'string' && s.id ? s.id : typeof s.name === 'string' && s.name ? s.name : null;
    if (!id) return null;
    return {id, label: typeof s.title === 'string' && s.title ? s.title : id, ts: num(s.ts) || num(s.updated)};
  }).filter(Boolean).slice(0, 8);
}

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
  // A host that mounts the tab sizes it to the viewport (dash.py, data-fill); the standalone page scrolls instead.
  if (!standalone) wrap.dataset.fill = '';
  const listCard = el('div', 'adir-card adir-listcard'), list = el('nav', 'adir-list');
  list.setAttribute('aria-label', 'Agents');
  const empty = el('p', 'adir-empty', 'No agents registered yet.');
  const error = el('p', 'adir-sub'); error.setAttribute('role', 'status');
  listCard.append(list, empty, error);
  const detail = el('section', 'adir-card adir-detail');
  wrap.append(listCard, detail);
  el0.replaceChildren(style, wrap);

  let stopped = false, route = parseRoute(routeNow()), shown = null, view = null, agents = [];
  let head = null, viewStatus = null, replies = [], session = null, working = false, hermesComp = false;
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
      const label = agent.name === shown && viewStatus && !summaryOf(agent.name) ? viewStatus[1] : info.label;
      it.querySelector('.adir-meta').textContent = [label, KIND[agent.kind] || agent.kind, ago(agent.age)].join(' · ');
      it.querySelector('.adir-dot').hidden = true;
      list.append(it);
    }
    for (const [name, it] of items) if (!seen.has(name)) { it.remove(); items.delete(name); }
  }

  // The one header: back link (phones), name, status, the actions menu; what it is doing; harness and the rest.
  function headerFor(agent) {
    const box = el('div', 'ad-vh'), top = el('div', 'ad-vh-top');
    const back = el('a', 'adir-back', '‹ Agents');
    back.href = standalone ? '#' : location.hash.split('/').slice(0, 2).join('/');
    const status = el('span'), now = el('p', 'ad-now'), meta = el('div', 'ad-meta');
    const actions = popMenu(el('button', 'btn ghost sq', '⋯'), []);
    actions.wrap.firstChild.setAttribute('aria-label', 'Actions'); actions.wrap.firstChild.title = 'Actions';
    top.append(back, el('h2', null, agent.name), status, el('span', 'ad-sp'), actions.wrap);
    box.append(top, now, meta);
    detail.setAttribute('aria-label', agent.name);
    head = {box, status, now, meta, actions, sessions: [], stepsShown: false};
    return box;
  }

  function copyLink(name) {
    const url = location.origin + location.pathname + hrefOf(makeRoute(name, session));
    if (navigator.clipboard) navigator.clipboard.writeText(url).catch(() => {});
  }

  function paintMenu(agent) {
    if (!head) return;
    const menu = [];
    if (head.sessions.length) {
      menu.push({heading: 'Session'});
      for (const s of head.sessions) {
        menu.push({label: s.label, note: s.id === session ? 'current' : s.ts ? ago(Date.now() / 1000 - s.ts) : '',
          run: () => { if (s.id !== session) navigate(makeRoute(agent.name, s.id)); }});
      }
      menu.push('hr');
    }
    if (view && view.toggleSteps) {
      menu.push({label: head.stepsShown ? 'Hide all steps' : 'Show all steps',
        run: () => { head.stepsShown = view.toggleSteps(); paintMenu(agent); }});
    }
    menu.push({label: 'Copy link', run: () => copyLink(agent.name)});
    if (hermesComp && ctx.canAct && agent.can_steer) {
      menu.push('hr', {label: 'Stop current turn', bad: true, hidden: !working, run: async () => {
        try { await viewActions(base + 'api/a/' + encodeURIComponent(agent.name) + '/', () => session, ctx.signal).stop(); }
        catch (err) { if (comp) comp.notice('Could not stop: ' + err.message); }
      }});
    }
    head.actions.set(menu);
  }

  function updateHead(agent) {
    if (!head) return;
    const info = describe(agent, summaryOf(agent.name));
    const [state, label] = viewStatus && !summaryOf(agent.name) ? viewStatus : [info.state, info.label];
    head.status.replaceChildren(chip(state, label));
    head.now.hidden = !info.doing && !info.waiting;
    head.now.replaceChildren(el('b', null, 'Now'), info.doing + (info.doing && info.waiting ? ' ' : ''),
      el('span', 'ad-wait', info.waiting));
    head.meta.replaceChildren(...metaParts(info, agent, hermesComp ? session : null));
    if (!comp) return;
    if (hermesComp) {
      comp.update(ctx.canAct && agent.can_steer, ctx.canAct ? NO_MESSAGES(agent.name) : READ_ONLY, replies, [], '');
      comp.working(working);
    } else comp.update(ctx.canAct, READ_ONLY, replies, agent.steer_messages, steerHint(agent));
  }

  async function showAgent(agent) {
    shown = agent.name;
    comp = null; viewStatus = null; session = null; working = false; hermesComp = false;
    const live = agent.can_tail || agent.can_view;
    detail.classList.toggle('full', !!live);
    detail.replaceChildren(headerFor(agent));
    if (agent.can_steer_pull) {
      comp = composer({name: agent.name, send: steerSend(base, agent.name, ctx.signal),
        hint: steerHint(agent), sink: () => view && view.note});
    }
    if (agent.can_tail) {
      updateHead(agent); paintMenu(agent);
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
    if (!agent.can_view) {
      updateHead(agent); paintMenu(agent);
      if (comp) detail.append(comp.form);
      detail.append(el('p', 'adir-note', agent.kind === 'hermes'
        ? agent.name + ' has not registered a live view, so only its presence shows here.'
        : agent.name + ' does not share its transcript, so only its presence shows here.'));
      return;
    }
    // hermes with a view: native replies and steps from the sidecar's rows, and a say / interrupt composer.
    const api = base + 'api/a/' + encodeURIComponent(agent.name) + '/';
    const acts = viewActions(api, () => session, ctx.signal);
    hermesComp = true;
    comp = composer({name: agent.name, max: 8000, send: acts.say, interrupt: acts.interrupt});
    const body = el('div', 'ad-tl'), note = el('p', 'adir-note', 'Connecting to ' + agent.name + '…');
    body.setAttribute('aria-label', agent.name + ' conversation'); body.tabIndex = 0;
    detail.append(note, body, comp.form);
    updateHead(agent); paintMenu(agent);
    try {
      const cfgRes = await viewFetch(api + 'cfg' + (route.session ? '?session=' + encodeURIComponent(route.session) : ''));
      const cfg = await cfgRes.json();
      if (!cfgRes.ok) throw new Error(cfg.error || 'HTTP ' + cfgRes.status);
      if (shown !== agent.name || stopped) return;
      session = route.session || (cfg.session && cfg.session !== '*' ? cfg.session : cfg.main) || null;
      head.sessions = sessionList(cfg);
      if (!session) { note.textContent = agent.name + ' has no session yet.'; return; }
      note.remove();
      view = liveView(body, {name: agent.name, api, session, scroll: true, seek: route.id,
        onNote: (text) => { note.textContent = text; if (text) detail.insertBefore(note, body); else note.remove(); },
        onWork: (w) => {
          working = !!w;
          viewStatus = w ? ['run', 'Working'] : ['idle', 'Idle'];
          const current = agents.find((a) => a.name === agent.name);
          if (current) { updateHead(current); paintMenu(current); }
          renderList();
        }});
      const current = agents.find((a) => a.name === agent.name) || agent;
      updateHead(current); paintMenu(current);
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

// Fleet quick view: works on its own without opening the full view. The host's sheet header already names the agent
// and carries Full view and Close, so this adds no second header: one status and meta line (with the actions menu),
// what it is doing and who it waits on, its newest turns in the same reply / steps style as the tab, and the same
// composer. Claude Code and Codex lines come from the tail; hermes rows from its sidecar.
const QUICK_ROWS = 6;
const QUICK_CSS = BASE_CSS + `
.aqv{display:grid;grid-template-columns:minmax(0,1fr);gap:16px}
.aqv-head{display:grid;gap:4px;margin-top:-8px}
.aqv-top{display:flex;align-items:center;gap:12px;min-width:0;min-height:var(--ctl)}
.aqv-now{display:grid;gap:8px;padding:12px 16px;border-radius:10px;background:var(--s2)}
.aqv-lbl{font-size:12px;line-height:16px;font-weight:600;color:var(--t2)}
.aqv-now p{margin:0;overflow-wrap:anywhere}
.aqv-wait{display:flex;align-items:center;gap:8px;font-weight:600;color:var(--wait)}
.aqv-wait::before{content:'';flex:none;width:10px;height:10px;box-sizing:border-box;border-radius:50%;border:2px solid currentColor}
.aqv-wait:empty{display:none}
.aqv-latest{display:grid;gap:8px}
.aqv .ad-tl{padding:0;background:none}
.aqv .ad-md{display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.aqv-sub{margin:0;font-size:12px;color:var(--t2)}
.aqv-sub:empty{display:none}
/* The host sheet's content padding is the quick view's --gut (24px, 16px on phones): the composer docks edge to edge. */
.aqv .ad-comp{margin:0 calc(0px - var(--gut)) calc(0px - var(--gut));position:sticky;bottom:0}`;

// One hermes transcript row as [who, text], or null for rows not worth a quick-view line (tool results, empty steps).
export function rowSummary(row, agentName) {
  if (!row || typeof row !== 'object') return null;
  const calls = Array.isArray(row.tool_calls) ? row.tool_calls : [];
  const images = Array.isArray(row.images) ? row.images.length : 0;
  let line = null;
  if (row.role === 'user') line = [row.sender || 'user', row.content || (images ? '[image]' : '')];
  else if (row.role === 'assistant') {
    if (row.content) line = [agentName || 'agent', row.content];
    else if (calls.length) line = ['tool', calls.map((c) => (c && (c.name || (c.function && c.function.name))) || 'tool').join(', ')];
    else if (row.reasoning) line = ['thinking', row.reasoning];
  }
  return line && line[1] ? [String(line[0]), plainText(line[1])] : null;
}

// Plain text, not markdown: keep link labels and drop **bold**, __bold__ and `code` markers.
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
  const path = i.file_path || i.notebook_path || i.path;
  if (path) return [/^(Edit|MultiEdit|Write|NotebookEdit|write_file|edit_file|patch)$/.test(name) ? 'Edited'
    : /^(Read|read_file)$/.test(name) ? 'Read' : String(name), base0(path)];
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

// A tail line as a turn item: tool lines become steps.
const lineItem = (it, i) => it.kind === 'tool' ? {kind: 'step', key: 'l' + i, verb: it.text, code: it.code, ts: it.ts}
  : {...it, key: 'l' + i, who: it.kind === 'user' ? 'you' : it.who};

export function quickView(el0, agentName, ctx = {}) {
  const base = ctx.baseUrl || '/';
  const api = base + 'api/a/' + encodeURIComponent(agentName) + '/';
  const summaryOf = () => { try { return ctx.agentSummary ? ctx.agentSummary(agentName) : null; } catch (e) { return null; } };
  const wrap = el('div', 'aqv'), top = el('div', 'aqv-top'), status = el('span'), meta = el('div', 'ad-meta');
  const actions = popMenu(el('button', 'btn ghost sq', '⋯'), []);
  actions.wrap.firstChild.setAttribute('aria-label', 'Actions'); actions.wrap.firstChild.title = 'Actions';
  const head = el('div', 'aqv-head');
  top.append(status, el('span', 'ad-sp'), actions.wrap);
  head.append(top, meta);
  const nowBox = el('div', 'aqv-now'), doing = el('p'), waiting = el('div', 'aqv-wait');
  nowBox.append(el('span', 'aqv-lbl', 'Doing now'), doing, waiting);
  const latest = el('div', 'aqv-latest'), body = el('div', 'ad-tl');
  latest.append(el('div', 'aqv-lbl', 'Latest'), body);
  // Presence errors (the poller clears its own on every success) and stream errors stay separate.
  const live = el('p', 'aqv-sub'), error = el('p', 'aqv-sub'), note = el('p', 'aqv-sub');
  error.setAttribute('role', 'status'); note.setAttribute('role', 'status');
  wrap.append(head, nowBox, latest, live, note, error);
  el0.replaceChildren(el('style', null, QUICK_CSS), wrap);

  let stopped = false, started = false, es = null, session = null, lines = [], running = false, agent = null,
    replies = [], comp = null, mode = null, feed = null;
  const controller = new AbortController(), openKeys = new Set();
  const acts = viewActions(api, () => session, controller.signal);

  function paintSummary() {
    const info = describe(agent, summaryOf());
    // Without a host summary, the sidecar's own run state is the better signal than directory presence.
    const [state, label] = running && !summaryOf() ? ['run', 'Working'] : [info.state, info.label];
    status.replaceChildren(chip(state, label));
    meta.replaceChildren(...metaParts(info, agent, null));
    nowBox.hidden = !info.doing && !info.waiting;
    doing.textContent = info.doing; doing.hidden = !info.doing;
    waiting.textContent = info.waiting;
  }

  function paintMenu() {
    const canStop = mode === 'view' && !!ctx.canAct && agent && agent.can_steer;
    actions.set(canStop ? [{label: 'Stop current turn', bad: true, hidden: !running, run: async () => {
      try { await acts.stop(); note.textContent = ''; } catch (err) { note.textContent = 'Could not stop: ' + err.message; }
    }}] : []);
  }

  function paintLines() {
    const nodes = turnNodes(groupTurns(lines.map(lineItem)), openKeys, false);
    body.replaceChildren(...nodes);
    latest.hidden = !nodes.length;
  }

  function paintComposer() {
    if (!comp || !agent) return;
    const ok = !!ctx.canAct && (mode === 'tail' ? agent.can_steer_pull : agent.can_steer);
    comp.update(ok, ctx.canAct ? NO_MESSAGES(agentName) : READ_ONLY,
      replies, agent.steer_messages, mode === 'tail' ? steerHint(agent) : '');
    comp.working(mode === 'view' && running);
  }

  // Claude Code / Codex: the tail stream. Keep the newest few conversation lines.
  function startTail() {
    mode = 'tail';
    let retry = null;
    const connect = () => {
      if (stopped) return;
      es = new EventSource(base + 'api/agents/' + encodeURIComponent(agentName) + '/transcript');
      es.onopen = () => { note.textContent = ''; };
      es.onmessage = (m) => {
        let r; try { r = JSON.parse(m.data); } catch (e) { return; }
        const more = transcriptLines(r, agentName);
        if (more.length) { lines = lines.concat(more).slice(-QUICK_ROWS); paintLines(); }
      };
      es.addEventListener('reset', () => { lines = []; paintLines(); });
      es.addEventListener('end', () => { es.close(); retry = setTimeout(connect, 2000); });
      es.onerror = () => {
        if (!stopped) note.textContent = 'Reconnecting to ' + agentName + '…';
        if (es.readyState === EventSource.CLOSED && !retry) retry = setTimeout(() => { retry = null; connect(); }, 2000);
      };
    };
    controller.signal.addEventListener('abort', () => clearTimeout(retry));
    paintLines();
    connect();
  }

  // hermes: the sidecar's event stream, through this module's proxy, in the tab's own renderer.
  async function startView() {
    mode = 'view';
    try {
      const r = await viewFetch(api + 'cfg', {signal: controller.signal});
      const cfg = await r.json();
      if (!r.ok) throw new Error(cfg.error || 'HTTP ' + r.status);
      if (stopped) return;
      session = cfg.session && cfg.session !== '*' ? cfg.session : cfg.main;
      if (!session) { live.textContent = agentName + ' has no session yet.'; latest.hidden = true; return; }
      feed = liveView(body, {name: agentName, api, session, limit: QUICK_ROWS, empty: 'Nothing said yet.',
        onNote: (text) => { note.textContent = text; },
        onWork: (w) => { running = !!w; if (agent) paintSummary(); paintMenu(); paintComposer(); }});
    } catch (err) {
      if (!stopped) live.textContent = 'Live view unavailable: ' + err.message;
    }
  }

  const stopPoll = poller(ctx, (agents, quick) => {
    agent = agents.find((a) => a.name === agentName) || null;
    replies = quick;
    if (!agent) {
      status.replaceChildren(chip('off', 'Gone')); meta.replaceChildren(el('span', null, agentName + ' is not registered.'));
      return;
    }
    paintSummary();
    if (!started) {
      started = true;
      if (agent.can_tail) startTail();
      else if (agent.can_view) startView();
      else { latest.hidden = true; live.textContent = agentName + ' does not share its transcript, so only its presence shows here.'; }
      if (agent.can_steer_pull && mode === 'tail') {
        comp = composer({name: agentName, send: steerSend(base, agentName, controller.signal),
          hint: steerHint(agent)});
      } else if (mode === 'view') {
        comp = composer({name: agentName, max: 8000, send: acts.say, interrupt: acts.interrupt});
      }
      if (comp) wrap.append(comp.form);
    }
    paintMenu();
    paintComposer();
  }, error);

  const cleanup = () => {
    if (stopped) return;
    stopped = true; stopPoll(); controller.abort();
    if (es) { es.close(); es = null; }
    if (feed) { feed.destroy(); feed = null; }
  };
  if (ctx.signal) ctx.signal.addEventListener('abort', cleanup);
  return cleanup;
}
