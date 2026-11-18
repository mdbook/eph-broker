// Module host contract (docs/modules.md): mount and quickView may return cleanup;
// ctx.baseUrl prefixes UI routes and ctx.signal aborts on unmount.
// The Agents tab: the directory on the left, the picked agent on the right. A hermes agent with a view shows its
// live view: the sidecar's renderer (hermes-ui/view.js, the sidecar's web/ installed by the operator) mounted in a
// shadow root, fed only through this module's re-typed JSON/SSE proxy (api/a/NAME/...).
const STATUS_COLOR = {busy: 'var(--run,inherit)', idle: 'var(--t2,inherit)',
  live: 'var(--t1,inherit)', grey: 'var(--t3,inherit)'};
const VIEW_STATUS = {'': 'idle', busy: 'busy', compact: 'compacting', unread: 'unread reply', off: 'offline'};
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

const CSS = `
.adir{display:grid;grid-template-columns:260px 1fr;gap:16px;align-items:start;color:var(--t1,inherit)}
.adir-card{background:var(--s1,transparent);border:1px solid var(--line,currentColor);border-radius:12px;box-shadow:var(--shadow,none);min-width:0}
.adir-detail{position:sticky;top:80px;max-height:calc(100dvh - 96px);overflow:auto}
.adir-list{padding:4px 0}
.adir-it{display:flex;align-items:center;gap:12px;padding:14px 20px;border-left:3px solid transparent;
  color:inherit;text-decoration:none}
.adir-it:hover,.adir-it:focus-visible{background:var(--s2,transparent)}
.adir-it:focus-visible,.adir-back:focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.adir-it[aria-current=true]{background:color-mix(in srgb,var(--brand) 7%,var(--s1));border-left-color:var(--brand,currentColor)}
.adir-it.grey{color:var(--t2,inherit)}.adir-grow{flex:1;min-width:0}.adir-nm{font-weight:600;overflow-wrap:anywhere}
.adir-meta,.adir-sub{font-size:13px;color:var(--t2,inherit)}
.adir-dot{width:8px;height:8px;border-radius:50%;background:var(--brand,currentColor)}
.adir-head{display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid var(--line,currentColor);
  flex-wrap:wrap}.adir-head h2{margin:0;font-size:18px;font-weight:650}.adir-sp{flex:1}
.adir-pill{display:inline-flex;align-items:center;gap:6px;font-size:13px;font-weight:600;padding:2px 8px;border-radius:999px;
  background:color-mix(in srgb,currentColor 13%,var(--s1));white-space:nowrap}
.adir-pill::before{content:'';width:6px;height:6px;border-radius:50%;background:currentColor}
.adir-back{display:none;color:var(--brand);font-size:14px;font-weight:600;text-decoration:none}
.adir-back:hover{text-decoration:underline}
/* Resolve references outside the renderer host, whose :host styles may redefine the older tokens. */
.adir-detail{${VIEW_TOKENS}}
.adir-view{height:max(480px,calc(100vh - 220px));position:relative;border-radius:0 0 12px 12px}
.adir-note{padding:16px;color:var(--t2,inherit)}
.adir-steer{padding:16px;border-bottom:1px solid var(--line,#dce0e6);display:grid;gap:8px}
.adir-steer textarea{box-sizing:border-box;width:100%;min-height:72px;resize:vertical;font:inherit;
  background:var(--s1,#fff);color:var(--t1,rgb(20,25,32));border:1px solid var(--line,#dce0e6);border-radius:8px;padding:8px}
.adir-steer :focus-visible{outline:2px solid var(--brand,#2b57c9);outline-offset:2px}
.adir-steer button{justify-self:start}.adir-steer ul{margin:0;padding-left:20px;font-size:13px;color:var(--t2,rgb(71,81,96))}
.adir-empty{padding:12px 14px;color:var(--t2,inherit)}
@media (max-width:820px){
  .adir{grid-template-columns:1fr}.adir.picked .adir-listcard{display:none}.adir:not(.picked) .adir-detail{display:none}
  .adir-detail{position:static;max-height:none;overflow:visible}
  .adir-back{display:inline-flex}.adir-view{height:calc(100vh - 160px)}
}
@media (max-width:720px){
  .adir-head{flex-wrap:nowrap;gap:10px;padding:8px 12px}.adir-head h2{font-size:16px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .adir-kind,.adir-beat{display:none}.adir-back{min-height:48px;align-items:center}
  .adir-view{height:calc(100dvh - 132px - env(safe-area-inset-top) - env(safe-area-inset-bottom))}
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
      render(data.agents);
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
  return state === 'delivered' ? 'Delivered' : 'Waiting for next tool call';
}

function steerComposer(agent, ctx, base) {
  const form = el('form', 'adir-steer'), label = el('label', null, 'Message at next tool call');
  const input = el('textarea'), send = el('button', 'btn pri', 'Send message');
  input.maxLength = 4000; input.required = true;
  input.setAttribute('aria-label', 'Message ' + agent.name + ' at next tool call');
  label.append(input);
  const note = el('p', 'adir-sub', ctx.canAct ? 'Idle agents wait until their next tool call.'
    : 'Read-only: sending needs the broker actions permission.');
  note.setAttribute('role', 'status');
  const states = el('ul'); states.setAttribute('aria-live', 'polite');
  input.disabled = send.disabled = !ctx.canAct;
  form.append(label, send, note, states);
  const update = (current) => {
    states.replaceChildren(...(current.steer_messages || []).map((m) =>
      el('li', null, m.id + ': ' + steerState(m.state))));
  };
  update(agent);
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (!ctx.canAct || !input.value.trim()) return;
    input.disabled = send.disabled = true;
    try {
      const response = await viewFetch(base + 'api/agents/' + encodeURIComponent(agent.name) + '/steer', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: input.value}),
        signal: ctx.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
      input.value = '';
      note.textContent = steerState('pending');
      states.append(el('li', null, data.id + ': ' + steerState('pending')));
    } catch (err) { note.textContent = 'Could not send message: ' + err.message; }
    finally { input.disabled = send.disabled = !ctx.canAct; }
  };
  return {form, update};
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

  let stopped = false, route = parseRoute(routeNow()), shown = null, view = null, agents = [], unread = {}, pill = null, beat = null;
  const items = new Map();
  let composer = null;

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
      it.href = hrefOf(agent.name);
      it.classList.toggle('grey', agent.status === 'grey');
      it.setAttribute('aria-current', String(agent.name === route.name));
      it.querySelector('.adir-nm').textContent = agent.name;
      it.querySelector('.adir-meta').textContent = agent.kind + ', ' + agent.status + ', ' + ago(agent.age);
      const dot = it.querySelector('.adir-dot');
      dot.hidden = !unread[agent.name];
      dot.title = unread[agent.name] ? unread[agent.name] + ' unread' : '';
      list.append(it);
    }
    for (const [name, it] of items) if (!seen.has(name)) { it.remove(); items.delete(name); }
  }

  function headerFor(agent) {
    const head = el('div', 'adir-head');
    const back = el('a', 'adir-back', '‹ Agents');
    back.href = standalone ? '#' : location.hash.split('/').slice(0, 2).join('/');
    pill = el('span', 'adir-pill');
    beat = el('span', 'adir-sub adir-beat');
    head.append(back, el('h2', null, agent.name), el('span', 'adir-sub adir-kind', agent.kind), pill, beat, el('span', 'adir-sp'));
    return head;
  }

  function updateHead(agent) {
    if (!pill) return;
    if (!view) { pill.textContent = agent.status; pill.style.color = STATUS_COLOR[agent.status] || 'inherit'; }
    beat.textContent = 'last beat ' + ago(agent.age);
    if (composer) composer.update(agent);
  }

  async function showAgent(agent) {
    shown = agent.name;
    composer = null;
    detail.replaceChildren(headerFor(agent));
    updateHead(agent);
    if (agent.can_steer_pull) {
      composer = steerComposer(agent, ctx, base);
      detail.append(composer.form);
    }
    if (agent.can_tail) {
      const host = el('div', 'adir-view');
      detail.append(host);
      try {
        const mod = await import(base + 'transcript.js');
        if (shown !== agent.name || stopped) return;
        view = mod.mount(host, {url: base + 'api/agents/' + encodeURIComponent(agent.name) + '/transcript',
          css: base + 'transcript.css'});
      } catch (err) {
        if (shown === agent.name && !stopped) detail.append(el('p', 'adir-note', 'Transcript unavailable: ' + err.message));
      }
      return;
    }
    if (!agent.can_view) {
      detail.append(el('p', 'adir-note', agent.kind === 'hermes'
        ? agent.name + ' has not registered a live view, so only its presence shows here.'
        : 'No live view for ' + agent.kind + ' agents yet; this shows presence only.'));
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
          if (pill) {
            pill.textContent = VIEW_STATUS[s.kind] || 'idle';
            pill.title = s.text || '';
            pill.style.color = s.kind === 'off' ? 'var(--block,inherit)' : s.busy ? 'var(--run,inherit)' : 'var(--t2,inherit)';
          }
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
      shown = null;
      detail.replaceChildren(el('p', 'adir-note', 'Pick an agent to see it live.'));
      return;
    }
    if (!agent) {
      if (shown !== route.name) detail.replaceChildren(el('p', 'adir-note', route.name + ' is not registered.'));
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
  const stopPoll = poller(ctx, (data) => { agents = data; renderList(); showRoute(); }, error);
  showRoute();
  const cleanup = () => {
    stopped = true; stopPoll();
    if (standalone) window.removeEventListener('hashchange', onHash);
    if (view) { view.destroy(); view = null; }
  };
  if (ctx.signal) ctx.signal.addEventListener('abort', cleanup);
  return cleanup;
}

// Fleet quick view: presence for every agent; a view-capable one adds its last few transcript lines (live over
// the sidecar's /events), a one-line composer and stop/steer. The broker panel supplies "Open full view".
const QUICK_ROWS = 5;
const QUICK_CSS = `
.aqv{display:grid;gap:8px;color:var(--t1,inherit)}
.aqv-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.aqv-head b{font-size:15px}
.aqv-pill{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid currentColor}
.aqv-sub{font-size:12px;color:var(--t3,inherit)}
.aqv-rows{list-style:none;margin:0;padding:8px 10px;display:grid;gap:4px;background:var(--s2,transparent);
  border-radius:8px;font-size:13px}
.aqv-rows:empty{display:none}
.aqv-rows li{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.aqv-who{font-weight:600;margin-right:6px;color:var(--t2,inherit)}
.aqv-form{display:flex;gap:6px;flex-wrap:wrap}.aqv-form input{flex:1;min-width:160px}`;

// One transcript row as [who, text], or null for rows not worth a quick-view line (tool results, empty steps).
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

const clock = (ms) => new Date(ms).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
const beatText = (age) => 'last beat ' + ago(age) + ' (' + clock(Date.now() - age * 1000) + ')';

export function quickView(el0, agentName, ctx = {}) {
  const base = ctx.baseUrl || '/';
  const api = base + 'api/a/' + encodeURIComponent(agentName) + '/';
  const wrap = el('div', 'aqv'), head = el('div', 'aqv-head');
  const pill = el('span', 'aqv-pill'), beat = el('span', 'aqv-sub'), kind = el('span', 'aqv-sub');
  head.append(el('b', null, agentName), kind, pill, beat);
  const rowsEl = el('ol', 'aqv-rows'); rowsEl.setAttribute('aria-live', 'polite');
  // Presence errors (the poller clears its own on every success) and action/stream errors stay separate.
  const live = el('p', 'aqv-sub'), error = el('p', 'aqv-sub'), note = el('p', 'aqv-sub');
  error.setAttribute('role', 'status'); note.setAttribute('role', 'status');
  const form = el('form', 'aqv-form'), input = el('input');
  input.type = 'text'; input.maxLength = 8000; input.setAttribute('aria-label', 'Message ' + agentName);
  input.placeholder = 'Message ' + agentName + '…';
  const send = el('button', 'btn', 'Send'), steer = el('button', 'btn', 'Steer'), stopB = el('button', 'btn', 'Stop');
  send.type = 'submit'; steer.type = 'button'; stopB.type = 'button';
  form.append(input, send, steer, stopB); form.hidden = true;
  wrap.append(head, rowsEl, live, form, note, error);
  el0.replaceChildren(el('style', null, QUICK_CSS), wrap);

  let stopped = false, started = false, es = null, session = null, rows = [], running = false, canAct = false,
    busyPost = false;
  const controller = new AbortController();

  function paintControls() {
    for (const b of [send, steer, stopB, input]) b.disabled = !canAct || busyPost;
    steer.hidden = stopB.hidden = !running;
    send.title = steer.title = stopB.title = input.title = canAct ? '' : ctx.canAct
      ? agentName + ' does not accept messages from here.'
      : 'Read-only: sending needs the broker actions permission.';
  }

  function paintRows() {
    rowsEl.replaceChildren(...rows.map((r) => {
      const [who, text] = rowSummary(r, agentName), li = el('li');
      li.append(el('span', 'aqv-who', who), document.createTextNode(text));
      return li;
    }));
  }

  function onFrame(p) {
    if (!p || typeof p !== 'object') return;
    rows = tailRows(rows, p.rows); paintRows();
    const run = p.live && !p.live.ended ? p.live : null;
    running = !!(run && run.run_id && (run.status === 'running' || run.status === 'starting'));
    const busy = !!(p.waiting || run);
    pill.textContent = p.compacting ? 'compacting' : busy ? 'busy' : 'idle';
    pill.style.color = busy ? 'var(--run,inherit)' : 'var(--t2,inherit)';
    live.textContent = run ? 'Working' + (run.stage ? ': ' + run.stage : run.tool ? ': ' + run.tool : '') + '…'
      : p.waiting ? 'Working: ' + (p.phase || 'model') + (Number.isFinite(p.since_ts) ? ' since ' + clock(p.since_ts * 1000) : '') : '';
    paintControls();
  }

  async function backfill() {
    const before = rows.length ? '&before=' + rows[0].id : '';
    try {
      const r = await viewFetch(api + 'earlier?limit=20&session=' + encodeURIComponent(session) + before,
        {signal: controller.signal});
      if (r.ok && !stopped) { rows = tailRows(rows, (await r.json()).rows); paintRows(); }
    } catch (e) { /* the stream still shows new rows */ }
  }

  async function startStream() {
    try {
      const r = await viewFetch(api + 'cfg', {signal: controller.signal});
      const cfg = await r.json();
      if (!r.ok) throw new Error(cfg.error || 'HTTP ' + r.status);
      if (stopped) return;
      session = cfg.session && cfg.session !== '*' ? cfg.session : cfg.main;
      if (!session) { live.textContent = agentName + ' has no session yet.'; return; }
      form.hidden = false; paintControls();
      let first = true;
      es = new EventSource(api + 'events?session=' + encodeURIComponent(session), {withCredentials: true});
      es.onmessage = (m) => {
        let p; try { p = JSON.parse(m.data); } catch (e) { return; }
        note.textContent = '';
        onFrame(p);
        if (first) { first = false; if (rows.length < QUICK_ROWS) backfill(); }
      };
      es.onerror = () => { if (!stopped) note.textContent = 'Reconnecting to ' + agentName + '…'; };
    } catch (err) {
      if (!stopped) live.textContent = 'Live view unavailable: ' + err.message;
    }
  }

  async function post(route, body) {
    busyPost = true; paintControls();
    try {
      const r = await viewFetch(api + route, {method: 'POST', signal: controller.signal,
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session, ...body})});
      let j = {}; try { j = await r.json(); } catch (e) { j = {}; }
      if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status);
      note.textContent = '';
      return true;
    } catch (err) {
      if (!stopped) note.textContent = 'Could not ' + route + ': ' + err.message;
      return false;
    } finally { busyPost = false; if (!stopped) paintControls(); }
  }

  form.onsubmit = async (e) => {
    e.preventDefault();
    const message = input.value.trim();
    if (message && await post('say', {message})) input.value = '';
  };
  steer.onclick = async () => {
    const message = input.value.trim();
    if (!message) { note.textContent = 'Type the steer message first.'; return; }
    if (await post('steer', {message})) input.value = '';
  };
  stopB.onclick = () => post('stop', {});

  const stopPoll = poller(ctx, (agents) => {
    const agent = agents.find((a) => a.name === agentName);
    if (!agent) { pill.textContent = 'gone'; beat.textContent = ''; kind.textContent = agentName + ' is not registered.'; return; }
    kind.textContent = agent.kind;
    beat.textContent = beatText(agent.age);
    canAct = !!(ctx.canAct && agent.can_steer);
    if (!agent.can_view) {
      pill.textContent = agent.status; pill.style.color = STATUS_COLOR[agent.status] || 'inherit';
      return;
    }
    if (!started) { started = true; startStream(); }
    paintControls();
  }, error);

  const cleanup = () => {
    if (stopped) return;
    stopped = true; stopPoll(); controller.abort();
    if (es) { es.close(); es = null; }
  };
  if (ctx.signal) ctx.signal.addEventListener('abort', cleanup);
  return cleanup;
}
