// Module host contract (docs/modules.md): mount and quickView may return cleanup;
// ctx.baseUrl prefixes UI routes and ctx.signal aborts on unmount.
const STATUS_COLOR = {busy: 'var(--run,inherit)', idle: 'var(--t2,inherit)',
  live: 'var(--t1,inherit)', grey: 'var(--t3,inherit)'};

function ago(seconds) {
  const s = Math.floor(seconds);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  return Math.floor(s / 3600) + 'h ago';
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

function statusCell(cell, status) {
  cell.textContent = status;
  cell.style.color = STATUS_COLOR[status] || 'inherit';
}

export function mount(el, ctx = {}) {
  const table = document.createElement('table');
  table.style.cssText = 'color:var(--t1,inherit);background:var(--s1,transparent);' +
    'border:1px solid var(--line,currentColor);border-collapse:collapse';
  const head = table.createTHead().insertRow();
  for (const title of ['Name', 'Kind', 'Status', 'Last beat', 'Caps']) {
    const th = document.createElement('th'); th.textContent = title; head.append(th);
  }
  const body = table.createTBody();
  const empty = document.createElement('p');
  empty.textContent = 'No agents registered yet.';
  empty.style.color = 'var(--t2,inherit)';
  const error = document.createElement('p'); error.setAttribute('role', 'status');
  el.replaceChildren(table, empty, error);
  return poller(ctx, (agents) => {
    body.replaceChildren();
    empty.hidden = agents.length > 0;
    for (const agent of agents) {
      const row = body.insertRow();
      if (agent.status === 'grey') row.style.opacity = '0.5';
      if (agent.name === ctx.agentName) row.style.outline = '2px solid var(--brand,currentColor)';
      row.insertCell().textContent = agent.name;
      row.insertCell().textContent = agent.kind;
      statusCell(row.insertCell(), agent.status);
      row.insertCell().textContent = ago(agent.age);
      row.insertCell().textContent = agent.caps.join(', ');
    }
  }, error);
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
