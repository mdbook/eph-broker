// Module host contract (docs/modules.md): mount and quickView may return cleanup;
// ctx.baseUrl prefixes UI routes and ctx.signal aborts on unmount.
// The Agents tab: the directory on the left, the picked agent on the right. A hermes agent with a view shows its
// live view: the sidecar's renderer (hermes-ui/view.js, the sidecar's web/ installed by the operator) mounted in a
// shadow root, fed only through this module's re-typed JSON/SSE proxy (api/a/NAME/...).
const STATUS_COLOR = {busy: 'var(--run,inherit)', idle: 'var(--t2,inherit)',
  live: 'var(--t1,inherit)', grey: 'var(--t3,inherit)'};
const VIEW_STATUS = {'': 'idle', busy: 'busy', compact: 'compacting', unread: 'unread reply', off: 'offline'};
const CSS = `
.adir{display:grid;grid-template-columns:260px 1fr;gap:16px;align-items:start;color:var(--t1,inherit)}
.adir-card{background:var(--s1,transparent);border:1px solid var(--line,currentColor);border-radius:10px;min-width:0}
.adir-list{padding:6px 0}
.adir-it{display:flex;align-items:center;gap:10px;padding:10px 14px;border-left:3px solid transparent;
  color:inherit;text-decoration:none}
.adir-it:hover,.adir-it:focus-visible{background:var(--s2,transparent)}
.adir-it[aria-current=true]{background:var(--s2,transparent);border-left-color:var(--brand,currentColor)}
.adir-it.grey{opacity:.55}.adir-grow{flex:1;min-width:0}.adir-nm{font-weight:600;overflow-wrap:anywhere}
.adir-meta,.adir-sub{font-size:12px;color:var(--t3,inherit)}
.adir-dot{width:8px;height:8px;border-radius:50%;background:var(--brand,currentColor)}
.adir-head{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line,currentColor);
  flex-wrap:wrap}.adir-head h2{margin:0;font-size:18px}.adir-sp{flex:1}
.adir-pill{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid currentColor}
.adir-back{display:none}
.adir-view{height:max(480px,calc(100vh - 220px));position:relative}
.adir-note{padding:16px;color:var(--t2,inherit)}
.adir-empty{padding:12px 14px;color:var(--t2,inherit)}
@media (max-width:820px){
  .adir{grid-template-columns:1fr}.adir.picked .adir-listcard{display:none}.adir:not(.picked) .adir-detail{display:none}
  .adir-back{display:inline-flex}.adir-view{height:calc(100vh - 160px)}
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
    beat = el('span', 'adir-sub');
    head.append(back, el('h2', null, agent.name), el('span', 'adir-sub', agent.kind), pill, beat, el('span', 'adir-sp'));
    return head;
  }

  function updateHead(agent) {
    if (!pill) return;
    if (!view) { pill.textContent = agent.status; pill.style.color = STATUS_COLOR[agent.status] || 'inherit'; }
    beat.textContent = 'last beat ' + ago(agent.age);
  }

  async function showAgent(agent) {
    shown = agent.name;
    detail.replaceChildren(headerFor(agent));
    updateHead(agent);
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
      const readOnly = !ctx.canAct ? 'Read-only: you can watch ' + agent.name +
          ', but sending, steering and commands need the broker actions permission.'
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

export function quickView(el, agentName, ctx = {}) {
  const line = document.createElement('p');
  const error = document.createElement('p'); error.setAttribute('role', 'status');
  el.replaceChildren(line, error);
  return poller(ctx, (agents) => {
    const agent = agents.find((a) => a.name === agentName);
    if (!agent) { line.textContent = agentName + ' is not registered.'; return; }
    line.textContent = agent.kind + ', ' + agent.status + ', last beat ' + ago(agent.age);
  }, error);
}
