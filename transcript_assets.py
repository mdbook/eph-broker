"""transcript_assets.py: pure transcript sources shared by broker and optional viewer.

Kept with the core Python modules so the broker never depends on the optional
viewer package. Neither the standalone viewer nor its clients import broker config.
"""

FORMAT = r"""function kfmt(n){ n = Number(n || 0); return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(n); }
"""

BASIC = r"""
function esc(s){ return String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function clip(s, n){ s = String(s ?? ''); return s.length > n ? s.slice(0, n) + '…' : s; }
function el(tag, cls, text){
  const e = document.createElement(tag);
  if(cls) e.className = cls;
  if(text !== undefined && text !== null) e.textContent = text;   // untrusted text: textContent only
  return e;
}
"""

TEXT = r"""
// ---- markdown. ESCAPE FIRST: the source is untrusted transcript text, so every fragment goes
// through esc() before any tag is added; the only tags produced are the fixed ones below, and links are
// http(s)-only (quotes are already &quot; so an href can't break out of its attribute).
// Models sometimes write literal named entities ("&rarr;", "&mdash;") instead of the character
// they mean. unent() (below, shared with sanitizeHtml) decodes those FIRST so esc() re-escapes only the
// real special chars; otherwise esc() turns the literal "&" into "&amp;" and the entity text survives
// as text instead of becoming the glyph.
function inline(t){
  const codes = [], links = [];
  const link = (href, text) => {   // href/text are already escaped; stashed so bold/em/autolink can't touch them
    links.push('<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + text + '</a>');
    return '\u0001' + (links.length - 1) + '\u0001';
  };
  t = esc(unent(t)).replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return '\u0000' + (codes.length - 1) + '\u0000'; });
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, a, h) => link(h, a))
       // Bare http(s) URLs (ticket refs, MR links) are clickable too; trailing punctuation stays text
       .replace(/https?:\/\/(?:[^\s<&*\u0000\u0001]|&amp;)+/g, m => {
         const u = m.replace(/[.,;:!?)\]]+$/, '');
         return link(u, u) + m.slice(u.length);
       })
       .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
       .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, '$1<em>$2</em>');
  t = t.replace(/\u0001(\d+)\u0001/g, (_, i) => links[Number(i)]);
  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => '<code>' + codes[Number(i)] + '</code>');
}
function mdBlocks(s){
  const out = [];
  let para = [], list = null;
  const flush = () => {
    if(para.length){ out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; }
    if(list){ out.push('<' + list.tag + '>' + list.items.map(x => '<li>' + inline(x) + '</li>').join('') + '</' + list.tag + '>'); list = null; }
  };
  for(const line of s.split('\n')){
    if(!line.trim()){ flush(); continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if(h){ flush(); const n = Math.min(h[1].length + 2, 6); out.push('<h' + n + '>' + inline(h[2]) + '</h' + n + '>'); continue; }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/), ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if(ul || ol){
      const tag = ul ? 'ul' : 'ol';
      if(para.length || (list && list.tag !== tag)) flush();
      if(!list) list = {tag: tag, items: []};
      list.items.push((ul || ol)[1]);
      continue;
    }
    if(/^\s*(---|\*\*\*|___)\s*$/.test(line)){ flush(); out.push('<hr>'); continue; }
    const q = line.match(/^>\s?(.*)$/);
    if(q){ flush(); out.push('<blockquote>' + inline(q[1]) + '</blockquote>'); continue; }
    if(list) flush();
    para.push(line);
  }
  flush();
  return out.join('');
}
function md(src){
  return String(src ?? '').split('```').map((b, i) => {
    if(i % 2 === 0) return mdBlocks(b);
    const nl = b.indexOf('\n');
    const code = nl >= 0 ? b.slice(nl + 1) : b;          // first line of a fence is the language tag
    return '<pre><code>' + esc(code.replace(/\n$/, '')) + '</code></pre>';
  }).join('');
}

// ---- sanitized HTML (worker `summary`, ticket `description`). ESCAPE-FIRST like md(): the source is
// tokenised, every text run is entity-decoded then re-escaped with esc(), and only allowlisted tag NAMES are
// re-emitted, never their attributes, except a vetted http(s) href on <a>. script/style/... are dropped
// together with their content; any other tag is dropped but its text kept. Pure string code (no DOM).
const SAFE_TAGS = new Set(['p', 'ul', 'ol', 'li', 'b', 'i', 'strong', 'em', 'code', 'pre', 'a', 'br']);
const DROP_TAGS = new Set(['script', 'style', 'iframe', 'object', 'embed', 'template', 'noscript', 'textarea', 'title', 'svg', 'math']);
// Named entities a model wrote literally ("&rarr;", "&mdash;") instead of the character it meant;
// unent() decodes these (plus numeric refs &#NNN;/&#xHEX;) so inline() can esc() the real specials only;
// otherwise esc() turns the literal "&" into "&amp;" and the entity text survives as text, not a glyph.
const NAMED_ENTITY_RE = /&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp|mdash|ndash|hellip|lsquo|rsquo|ldquo|rdquo|rarr|larr|harr|uarr|darr|bull|middot|trade|copy|reg|deg|times|divide|plusmn|sect|para);/gi;
function unent(s){
  return String(s).replace(NAMED_ENTITY_RE, (m, e) => {
    const k = e.toLowerCase();
    if(k[0] === '#'){
      const n = k[1] === 'x' ? parseInt(k.slice(2), 16) : parseInt(k.slice(1), 10);
      try{ return String.fromCodePoint(n); }catch(_){ return ''; }
    }
    return {amp: '&', lt: '<', gt: '>', quot: '"', apos: "'",
      mdash: '\u2014', ndash: '–', hellip: '…', lsquo: '‘', rsquo: '’', ldquo: '“', rdquo: '”',
      rarr: '→', larr: '←', harr: '↔', uarr: '↑', darr: '↓',
      bull: '•', middot: '·', trade: '™', copy: '©', reg: '®',
      deg: '°', times: '×', divide: '÷', plusmn: '±', sect: '§', para: '¶',
      nbsp: ' '}[k];
  });
}
function sanitizeHtml(src){
  const s = String(src ?? ''), out = [];
  const re = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
  let last = 0, m, drop = null;
  while((m = re.exec(s))){
    const name = (m[2] || '').toLowerCase();
    if(drop){ if(m[1] && name === drop){ drop = null; last = re.lastIndex; } continue; }
    out.push(esc(unent(s.slice(last, m.index))));
    last = re.lastIndex;
    if(!name) continue;                                              // comment
    if(DROP_TAGS.has(name)){ if(!m[1]) drop = name; continue; }      // element AND its content
    if(!SAFE_TAGS.has(name)) continue;                               // unknown tag: keep only its text
    if(m[1]){ if(name !== 'br') out.push('</' + name + '>'); continue; }
    if(name === 'a'){
      const h = m[3].match(/\shref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i);
      const url = h ? unent(h[1] ?? h[2] ?? h[3]).trim() : '';
      out.push(/^https?:\/\/\S+$/i.test(url) ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' : '<a>');
    }else out.push('<' + name + '>');
  }
  if(!drop) out.push(esc(unent(s.slice(last))));                     // an unclosed <script> eats the rest
  return out.join('');
}
// ---- end sanitizeHtml
"""

RENDERER = r"""// Shared Claude Code and Codex transcript renderer. Each mount owns its tool and grouping state.
function createTranscript($) {
let tools = new Map();
function atBottom(b){ return b.scrollTop + b.clientHeight >= b.scrollHeight - 20; }
function trim(b){ while(b.childNodes.length > 1000) b.removeChild(b.firstChild); }

// Only conversation turns get timeline cards; `system`/`summary` records get a one-line meta row.
// Every other top-level record `type` is harness bookkeeping (attachments, API request copies, session
// ownership, cost state, ...). Claude Code adds new ones every few releases, so this is an allowlist:
// anything unknown folds into the "N harness records" rows, which stay hidden until the Harness toggle.
// The record's own `type` wins over `message.role`: api-request-blob records carry a copy of a message
// (role assistant/user) and drawing them as turns duplicated tool cards that never got a result.
const TURN_TYPES = new Set(['assistant', 'user']);
const META_TYPES = new Set(['system', 'summary']);
function recordType(o){ return typeof o.type === 'string' ? o.type : (o.message && o.message.role) || '?'; }
function recordKind(o){
  if(!o || typeof o !== 'object') return 'line';
  const ty = recordType(o);
  return TURN_TYPES.has(ty) ? 'turn' : META_TYPES.has(ty) ? 'meta' : 'harness';
}
let harnessGroup = null;   // the currently-open collapsed <details> run, or null between runs
let harnessTotal = 0;

function harnessLabel(o, ty){
  // Generic: a record whose top-level type is T often nests its detail under a same-named key,
  // e.g. {"type":"attachment","attachment":{"type":"hook_success",...}} -> "attachment · hook_success".
  // No such nested `.type` (atis-latch, last-prompt) -> bare record type, no invented subtype.
  const nested = o && o[ty];
  const subtype = nested && typeof nested === 'object' && typeof nested.type === 'string' ? nested.type : null;
  return subtype ? ty + ' · ' + subtype : ty;
}
function resetLog(){
  $('sv-log').innerHTML = ''; harnessGroup = null; codexRun = null; tools = new Map(); root = '';
  webMsgs = new Map(); webPending = new Set();
  harnessTotal = 0; $('hr-n').textContent = '0';
}
function addHarness(o, ty){
  const log = $('sv-log'), stick = atBottom(log);
  if(!harnessGroup){
    const det = document.createElement('details');
    det.className = 'harness';
    const sum = document.createElement('summary');
    det.appendChild(sum);
    const body = document.createElement('div');
    det.appendChild(body);
    log.appendChild(det);
    harnessGroup = {sum, body, count: 0};
  }
  harnessGroup.count++;
  harnessGroup.sum.textContent = harnessGroup.count + ' harness records';   // literal wording
  $('hr-n').textContent = String(++harnessTotal);
  const div = document.createElement('div');
  div.className = 'ln';
  div.textContent = harnessLabel(o, ty);   // textContent, never innerHTML: record content is untrusted
  // The record's fields sit behind a collapsed per-line toggle as a key/value table, not raw JSON
  const nested = o && o[ty] && typeof o[ty] === 'object' ? o[ty] : o;
  if(nested && typeof nested === 'object' && Object.keys(nested).some(k => k !== 'type')){   // nothing to open otherwise
    const more = el('details', 'jd');
    more.appendChild(el('summary', null, 'details · ' + shape(nested)));
    more.appendChild(structured(nested));
    div.appendChild(more);
  }
  harnessGroup.body.appendChild(div);
  trim(log);
  if(stick) log.scrollTop = log.scrollHeight;
}

function timeEl(o){
  const t = o && o.timestamp ? new Date(o.timestamp) : null, ok = t && !isNaN(t);
  const e = el('time', 'ts', ok ? t.toLocaleTimeString() : '');
  if(ok){ e.title = t.toLocaleString(); e._ts = t.getTime(); }   // placeAt() orders by this
  return e;
}
// ---- operator messages sent from this page (steer.py records on the same stream). A queued one stays
// pinned under the newest step; once delivered it moves to the moment the worker read it.
let webMsgs = new Map(), webPending = new Set();
function placeAt(node, ms){
  const log = $('sv-log');
  node.remove();
  for(let c = log.lastElementChild; c; c = c.previousElementSibling){
    const t = c.querySelector('time.ts');
    if(t && Number.isFinite(t._ts) && t._ts <= ms){ c.after(node); return; }
  }
  log.prepend(node);
}
function pinPending(){ const log = $('sv-log'); for(const n of webPending) log.appendChild(n); }
function webMsg(o){
  const log = $('sv-log'), stick = atBottom(log);
  let m = webMsgs.get(o.id);
  if(!m){
    const ev = el('div', 'ev user you'), meta = el('div', 'ev-meta');
    meta.appendChild(el('span', 'who', 'You'));
    const chip = el('span', 'pill');
    meta.appendChild(chip);
    const time = timeEl(o);
    meta.appendChild(time);
    ev.appendChild(meta);
    const body = el('div', 'md', o.state === 'queued' ? o.text || '' : '');
    ev.appendChild(body);   // textContent: the operator's own words, never markup
    m = {ev, chip, time, body, reply: null, state: null};
    webMsgs.set(o.id, m);
  }
  if(o.state === 'queued' && o.text && !m.body.textContent) m.body.textContent = o.text;
  if(o.state === 'reply'){
    m.state = 'reply';
    m.chip.className = 'pill ok';
    m.chip.textContent = 'Replied';
    if(!m.reply){
      m.reply = el('div', 'worker-reply');
      m.reply.appendChild(el('div', 'who', 'Worker reply'));
      m.reply.appendChild(el('div', '', o.text || ''));   // textContent only, including final JSON/HTML
      m.ev.appendChild(m.reply);
    }
    m.ev.classList.remove('pending');
    webPending.delete(m.ev);
    const t = new Date(m.time._ts).getTime();
    if(Number.isFinite(t)) placeAt(m.ev, t); else log.appendChild(m.ev);
  }else if(o.state === 'delivered' && m.state !== 'reply'){
    m.state = 'delivered';
    m.chip.className = 'pill ok';
    m.chip.textContent = o.via === 'tool' ? 'Read mid-turn' : 'Read at the start of its next turn';
    m.time.replaceWith(m.time = timeEl(o));
    m.ev.classList.remove('pending');
    webPending.delete(m.ev);
    const t = new Date(o.timestamp).getTime();
    if(Number.isFinite(t)) placeAt(m.ev, t); else log.appendChild(m.ev);
  }else if(o.state === 'queued' && m.state !== 'delivered' && m.state !== 'reply'){
    m.state = 'queued';
    m.chip.className = 'pill pend';
    m.chip.textContent = o.via === 'tool' ? 'Queued: it reads this after its next tool call'
                                          : 'Queued: it reads this when its current turn ends';
    m.ev.classList.add('pending');
    webPending.add(m.ev);
    log.appendChild(m.ev);
  }
  if(stick) log.scrollTop = log.scrollHeight;
}
function bubble(role, text, o, err){
  const ev = el('div', 'ev ' + role + (err ? ' err' : ''));
  const meta = el('div', 'ev-meta');
  meta.appendChild(el('span', 'who', role === 'assistant' ? 'Assistant' : role === 'user' ? 'User' : role));
  if(err) meta.appendChild(el('span', 'pill bad', 'error'));
  meta.appendChild(timeEl(o));
  ev.appendChild(meta);
  const sj = splitJSON(text);
  if(sj){   // JSON body -> structured card, never the JSON itself
    if(sj.prose.trim()){ const p = el('div', 'md'); p.innerHTML = md(sj.prose); ev.appendChild(p); }
    ev.appendChild(jsonCard(sj.obj, 0));
    return ev;
  }
  const body = el('div', 'md');
  body.innerHTML = md(text);   // md() escapes all transcript text before adding its own fixed tags
  ev.appendChild(body);
  return ev;
}

// ---- JSON message bodies (worker kickoff, result envelope) as a structured card: short scalars as
// key/value chips, long strings by content type (HTML -> sanitizeHtml, else markdown), nested objects in
// collapsed <details>. All text enters via el()/textContent, md() or sanitizeHtml(), both escape-first.
const CHIP_MAX = 80;
function splitJSON(text){   // the whole body is JSON, or prose ending in a trailing JSON object
  const s = String(text ?? ''), full = v => v && typeof v === 'object' && Object.keys(v).length > 0;
  const j = tryJSON(s);
  if(full(j)) return {prose: '', obj: j};
  if(!s.trimEnd().endsWith('}')) return null;
  const re = /\n\s*\{/g;
  let m;
  while((m = re.exec(s))){
    const k = tryJSON(s.slice(m.index + 1));
    if(full(k) && !Array.isArray(k)) return {prose: s.slice(0, m.index), obj: k};
  }
  return null;
}
function looksHtml(s){ return /^\s*<([a-z][a-z0-9]*)\b[^>]*>[\s\S]*<\/[a-z][a-z0-9]*\s*>/i.test(s); }
function isChip(v){
  if(typeof v === 'string') return v.length <= CHIP_MAX && !v.includes('\n') && !looksHtml(v);
  if(Array.isArray(v)) return v.length <= 12 && v.every(x => isScalar(x) && String(x).length < 60);
  return isScalar(v);
}
function linkOrText(tag, s){
  if(!/^https?:\/\/\S+$/i.test(s)) return el(tag, null, s);
  const a = el('a', null, s);
  a.href = s; a.target = '_blank'; a.rel = 'noopener noreferrer';
  return a;
}
function richText(s){
  if(!s.includes('\n') && s.includes('\\n')) s = s.replace(/\\n/g, '\n');   // double-encoded newlines
  const r = !looksHtml(s) && mdRecord(s);   // An embedded "**key:** value" record (e.g. a ticket dump)
  if(r) return recordView(r);
  const b = el('div', 'md');
  if(looksHtml(s)) b.innerHTML = sanitizeHtml(s);   // allowlist tags, http(s)-only href, everything else escaped
  else b.innerHTML = md(s);                         // escape-first markdown
  return b;
}
function jsonCard(obj, depth){
  const box = el('div', 'jcard'), chips = el('div', 'jchips');
  box.appendChild(chips);
  const ents = Array.isArray(obj) ? obj.map((x, i) => ['#' + (i + 1), x]) : Object.entries(obj);
  for(const [k, v] of ents){
    if(isChip(v)){
      const c = el('span', 'chip');
      c.appendChild(el('span', null, k));
      c.appendChild(linkOrText('b', Array.isArray(v) ? (v.length ? v.map(scalar).join(', ') : '(none)') : scalar(v)));
      chips.appendChild(c);
    }else if(typeof v === 'string'){
      box.appendChild(el('div', 'tlabel', k));
      box.appendChild(richText(v));
    }else{   // nested object / long array: collapsible, collapsed by default
      const d = el('details', 'jd');
      d.appendChild(el('summary', null, k + ' · ' + peek(v)));   // "test_runner · pytest", not "2 fields"
      d.appendChild(depth < 4 ? jsonCard(v, depth + 1) : jsonDetails(v));
      box.appendChild(d);
    }
  }
  if(!chips.childNodes.length) chips.remove();
  return box;
}
const ARG_KEYS = ['command', 'file_path', 'path', 'pattern', 'query', 'url', 'title', 'description', 'prompt', 'name'];
// mcp args read as "get · id 9369" (verb + ids + main arg), not "operation: get · id: 9369 · …" / "N fields"
const OP_KEYS = ['operation', 'action', 'subcommand', 'method', 'op'];
const ID_KEY = /^(id|ids|identifier|index|number|[a-z]+_id|[a-z]+Id)$/;
function argSummary(inp){
  if(!inp || typeof inp !== 'object' || Array.isArray(inp)) return '';
  const out = [], used = new Set();
  const op = OP_KEYS.find(k => typeof inp[k] === 'string' && inp[k]);
  if(op){ out.push(inp[op]); used.add(op); }
  for(const [k, v] of Object.entries(inp)){
    if(out.length >= 3) break;
    if(ID_KEY.test(k) && (typeof v === 'number' || (typeof v === 'string' && v && v.length < 40))){
      out.push(k.replace(/_id$|Id$/, '') + ' ' + v); used.add(k);
    }
  }
  const main = ARG_KEYS.find(k => !used.has(k) && typeof inp[k] === 'string' && inp[k]);
  if(main) out.push(inp[main].replace(/\s+/g, ' '));
  if(!out.length)
    for(const [k, v] of Object.entries(inp)){
      if(out.length >= 3) break;
      if(!isBlank(v)) out.push(k + ': ' + (isScalar(v) ? scalar(v).replace(/\s+/g, ' ') : peek(v)));
    }
  return clip(out.join(' · '), 120);
}

// ---- human-readable tool cards. Known tools (Bash, Read, Edit, Write, Grep, Glob) get purpose-built
// renderers; any other object (mcp__* args, JSON results) becomes a key/value table; long/nested JSON sits
// behind a collapsed "details" toggle (nothing ever opens it). All content enters the DOM via el()/textContent,
// or md() (escape-first) for prose text blocks, never raw innerHTML.
const CODE_TOOLS = new Set(['Bash', 'BashOutput', 'Read', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'Grep', 'Glob']);
function isScalar(v){ return v === null || ['string', 'number', 'boolean'].includes(typeof v); }
function scalar(v){ return v === null ? '-' : v === undefined ? '' : String(v); }
function shape(v){
  if(Array.isArray(v)) return v.length + (v.length === 1 ? ' item' : ' items');
  if(v && typeof v === 'object'){ const n = Object.keys(v).length; return n + (n === 1 ? ' field' : ' fields'); }
  return typeof v;
}
function pre(text, cls){ return el('pre', 'tpre' + (cls ? ' ' + cls : ''), clip(text, 20000)); }
function tlabel(t){ return el('div', 'tlabel', t); }
function clipLines(s, n){
  const l = String(s ?? '').split('\n');
  return l.length > n ? l.slice(0, n).join('\n') + '\n… (' + (l.length - n) + ' more lines)' : l.join('\n');
}
// Deep or oversized values as an indented "key: value" outline: readable, and never JSON syntax on screen.
function outline(v, ind, out, depth){
  out = out || []; ind = ind || ''; depth = depth || 0;
  if(out.length > 400){ if(out[out.length - 1] !== '…') out.push('…'); return out; }
  const ents = Array.isArray(v) ? v.map((x, i) => [String(i + 1) + '.', x]) : Object.entries(v);
  if(!ents.length) out.push(ind + '(empty)');
  for(const [k, x] of ents){
    if(x && typeof x === 'object' && depth < 8){ out.push(ind + k + (Array.isArray(v) ? '' : ':')); outline(x, ind + '  ', out, depth + 1); }
    else out.push(ind + k + (Array.isArray(v) ? ' ' : ': ') + (x && typeof x === 'object' ? shape(x) : x === null ? '-' : String(x)).replace(/\s+/g, ' '));
  }
  return out;
}
function jsonDetails(v, what){   // collapsed by default
  const d = el('details', 'jd');
  d.appendChild(el('summary', null, 'details · ' + (what || shape(v))));
  d.appendChild(pre(v && typeof v === 'object' ? outline(v).join('\n') : String(v)));
  return d;
}
function kvValue(v, depth){
  if(typeof v === 'string') return v.includes('\n') || v.length > 160 ? pre(v) : el('span', null, v);
  if(isScalar(v)) return el('code', 'kvc', scalar(v));
  if(Array.isArray(v) && v.length <= 12 && v.every(x => isScalar(x) && String(x).length < 60))
    return el('span', null, v.length ? v.map(scalar).join(', ') : '(none)');
  // Labels / users / tasks show as their name ("opus #20"), the full record one click away
  if(entity(v) && depth < 4) return entDetails(v, depth);
  if(Array.isArray(v) && v.length && v.length <= 50 && depth < 4 && v.every(x => entity(x))){
    const box = el('div', 'ents');
    for(const x of v) box.appendChild(entDetails(x, depth));
    return box;
  }
  if(v && typeof v === 'object' && !Array.isArray(v) && depth < 2 && Object.keys(v).length <= 12) return kvTable(v, depth + 1);
  return jsonDetails(v);
}
function entDetails(v, depth){
  const d = el('details', 'jd ent');
  d.appendChild(el('summary', null, entityLabel(v)));
  d.appendChild(kvTable(v, depth + 1));
  return d;
}
function kvRows(t, ents, depth){
  for(const [k, v] of ents){
    const tr = el('tr'), td = el('td');
    tr.appendChild(el('th', null, k));
    td.appendChild(kvValue(v, depth || 0));
    tr.appendChild(td);
    t.appendChild(tr);
  }
}
function kvTable(obj, depth){
  const t = el('table', 'kv'), all = Object.entries(obj);
  // Empty / zero / default fields (hex_color: '', percent_done: 0, 0001-01-01 dates) fold into one row
  const blank = all.length > 3 ? all.filter(([, v]) => isBlank(v)) : [];
  kvRows(t, blank.length > 1 ? all.filter(([, v]) => !isBlank(v)) : all, depth);
  if(blank.length > 1){
    const tr = el('tr'), td = el('td'), d = el('details', 'jd blank'), bt = el('table', 'kv');
    td.colSpan = 2;
    d.appendChild(el('summary', null, blank.length + ' empty / default: ' + clip(blank.map(([k]) => k).join(', '), 140)));
    kvRows(bt, blank, depth);
    d.appendChild(bt);
    td.appendChild(d);
    tr.appendChild(td);
    t.appendChild(tr);
  }
  return t.childNodes.length ? t : el('span', 'muted', '(empty)');
}
function structured(v){ return v && typeof v === 'object' && !Array.isArray(v) ? kvTable(v, 0) : kvValue(v, 0); }
function tryJSON(s){
  const t = String(s ?? '').trim();
  if(!/^[\[{]/.test(t)) return undefined;
  try{ return JSON.parse(t); }catch(e){ return undefined; }
}
// ---- structured tool results. Pure string/object code (no DOM) so the tests can run it under node.
// Empty / default values (Go zero time, 0, '', null, {}, []) fold out of the way; `false` stays visible.
const ZERO_TIME = /^0001-01-01([T ]00:00:00(\.0+)?(Z|[+-]00:?00)?)?$/;
function isBlank(v){
  if(v === null || v === undefined || v === '' || v === 0) return true;
  if(typeof v === 'string') return ZERO_TIME.test(v.trim()) || !v.trim();
  if(Array.isArray(v)) return !v.length;
  if(typeof v === 'object') return !Object.keys(v).length;
  return false;
}
function entity(v){   // {id, title|name|username} -> {name, id} (labels, users, tasks); null when it isn't one
  if(!v || typeof v !== 'object' || Array.isArray(v)) return null;
  const n = ['title', 'name', 'username', 'login', 'label'].map(k => v[k]).find(x => typeof x === 'string' && x.trim());
  if(!n) return null;
  const id = (typeof v.id === 'number' || (typeof v.id === 'string' && v.id && v.id.length < 24)) ? '#' + v.id
           : typeof v.identifier === 'string' && v.identifier.length < 24 ? v.identifier : '';
  return {name: clip(n.replace(/\s+/g, ' '), 80), id};
}
function entityLabel(v){ const e = entity(v); return e ? e.name + (e.id ? ' ' + e.id : '') : ''; }   // "opus #20"
function peek(v){   // one-line gist of a nested value: entity label, else its first meaningful scalars, else its shape
  if(isScalar(v)) return clip(scalar(v).replace(/\s+/g, ' '), 60);
  const lab = entityLabel(v);
  if(lab) return lab;
  if(Array.isArray(v)){
    const labs = v.slice(0, 3).map(x => isScalar(x) ? scalar(x) : entityLabel(x));
    return labs.length && labs.every(Boolean) ? clip(labs.join(', '), 60) + (v.length > 3 ? ' +' + (v.length - 3) : '') : shape(v);
  }
  const sc = Object.values(v || {}).filter(x => isScalar(x) && !isBlank(x)).slice(0, 2);
  return sc.length ? clip(sc.map(x => scalar(x).replace(/\s+/g, ' ')).join(' · '), 60) : shape(v);
}
function jsonSpanEnd(lines, i, first){   // index of the line closing the JSON value opened on line i ({ or [), else -1
  let depth = 0, str = false, bs = false;
  for(let j = i; j < lines.length && j < i + 5000; j++){
    for(const ch of j === i ? first : lines[j]){
      if(str){ if(bs) bs = false; else if(ch === '\\') bs = true; else if(ch === '"') str = false; }
      else if(ch === '"') str = true;
      else if(ch === '{' || ch === '[') depth++;
      else if((ch === '}' || ch === ']') && --depth === 0) return j;
    }
  }
  return -1;
}
// MCP results like Vikunja's: a markdown preamble, then "**key:** value" lines whose value may open a multi-line
// JSON block (assignees, labels, created_by). Returns {prose, obj}, or null unless it's clearly such a record.
const MD_FIELD = /^\s*\*\*([^*\n]{1,60}?):\*\*[ \t]*(.*)$/;
function mdRecord(s){
  const lines = String(s ?? '').split('\n'), prose = [], obj = {};
  let n = 0;
  for(let i = 0; i < lines.length; i++){
    const m = lines[i].match(MD_FIELD);
    if(!m){ prose.push(lines[i]); continue; }
    const k = m[1].trim(), raw = m[2].trim();
    let v = raw;
    if(/^[\[{]/.test(raw)){
      const end = jsonSpanEnd(lines, i, raw);
      const j = end < 0 ? undefined : tryJSON([raw].concat(lines.slice(i + 1, end + 1)).join('\n'));
      if(j !== undefined){ v = j; i = end; }
    }else if(/^-?\d+(\.\d+)?$/.test(raw) && raw.length < 16) v = Number(raw);
    else if(raw === 'true' || raw === 'false') v = raw === 'true';
    if(!(k in obj)) n++;
    obj[k] = v;
  }
  return n >= 4 ? {prose: prose.join('\n').replace(/\n{3,}/g, '\n\n').trim(), obj} : null;
}
function fmtSize(s){
  const b = String(s ?? '').length, l = String(s ?? '').split('\n').length;
  return (l > 1 ? l + ' lines · ' : '') + (b < 1024 ? b + ' B' : (b / 1024).toFixed(b < 10240 ? 1 : 0) + ' KB');
}
function resultFact(v){   // the key fact of a parsed result for the card header: "#42 Some task title" / "12 items"
  if(Array.isArray(v)) return v.length + (v.length === 1 ? ' item' : ' items');
  if(!v || typeof v !== 'object') return '';
  const e = entity(v);
  if(e) return e.id ? e.id + ' ' + e.name : e.name;
  for(const k of ['message', 'status', 'result']) if(typeof v[k] === 'string' && v[k].trim()) return clip(v[k].replace(/\s+/g, ' '), 80);
  return '';
}
// ---- end structured tool results
function diffView(oldS, newS){
  const p = el('pre', 'tpre diff');
  const add = (pfx, s, cls) => { for(const ln of clip(s, 10000).split('\n')) p.appendChild(el('span', cls, pfx + ln)); };
  add('- ', oldS, 'del');
  add('+ ', newS, 'add');
  return p;
}
function extras(body, inp, skip){   // any args the purpose-built view didn't show, as a small table
  const r = {};
  for(const k of Object.keys(inp)) if(!skip.includes(k)) r[k] = inp[k];
  if(Object.keys(r).length) body.appendChild(kvTable(r, 0));
}
function renderInput(body, name, inp){
  if(!inp || typeof inp !== 'object' || Array.isArray(inp)){ body.appendChild(tlabel('Input')); body.appendChild(structured(inp)); return; }
  if(name === 'Bash' && typeof inp.command === 'string'){
    if(inp.description) body.appendChild(el('div', 'tdesc', inp.description));
    body.appendChild(tlabel('Command'));
    body.appendChild(pre('$ ' + inp.command));
    extras(body, inp, ['command', 'description']);
    return;
  }
  if(typeof inp.file_path === 'string' && ['Read', 'Write', 'Edit', 'MultiEdit'].includes(name)){
    body.appendChild(el('div', 'fpath', inp.file_path));
    if(name === 'Edit'){
      body.appendChild(tlabel(inp.replace_all ? 'Change (all occurrences)' : 'Change'));
      body.appendChild(diffView(inp.old_string, inp.new_string));
      extras(body, inp, ['file_path', 'old_string', 'new_string', 'replace_all']);
    }else if(name === 'MultiEdit' && Array.isArray(inp.edits)){
      inp.edits.forEach((e, i) => { body.appendChild(tlabel('Change ' + (i + 1))); body.appendChild(diffView((e || {}).old_string, (e || {}).new_string)); });
      extras(body, inp, ['file_path', 'edits']);
    }else if(name === 'Write'){
      body.appendChild(tlabel('Content'));
      body.appendChild(pre(clipLines(inp.content, 80)));
      extras(body, inp, ['file_path', 'content']);
    }else extras(body, inp, ['file_path']);   // Read: offset / limit
    return;
  }
  if((name === 'Grep' || name === 'Glob') && typeof inp.pattern === 'string'){
    body.appendChild(tlabel('Pattern'));
    body.appendChild(pre(inp.pattern));
    extras(body, inp, ['pattern']);
    return;
  }
  body.appendChild(tlabel('Arguments'));   // mcp__* and anything else: key/value table
  body.appendChild(kvTable(inp, 0));
}
function recordView(r){   // mdRecord() output -> markdown preamble + key/value table
  const box = el('div');
  if(r.prose){ const p = el('div', 'md'); p.innerHTML = md(r.prose); box.appendChild(p); }
  box.appendChild(structured(r.obj));
  return box;
}
function mdFenced(s){   // Markdown whose ```fenced``` blocks hold JSON -> those blocks become kv tables
  const parts = s.split('```');
  if(parts.length < 3) return null;
  let hit = false;
  const box = el('div');
  parts.forEach((seg, i) => {
    let j;
    if(i % 2 && i < parts.length - 1) j = tryJSON(seg.slice(Math.max(seg.indexOf('\n'), 0)));   // drop the language tag
    if(j && typeof j === 'object'){ hit = true; box.appendChild(structured(j)); return; }
    if(!seg.trim()) return;
    const b = el('div', 'md');
    b.innerHTML = md(i % 2 ? '```' + seg + '```' : seg);   // escape-first markdown, as below
    box.appendChild(b);
  });
  return hit ? box : null;
}
function renderResult(body, c, bad, name){   // returns a one-line fact about the result for the card header
  const code = CODE_TOOLS.has(String(name || ''));
  let fact = '';
  const text = s => {
    s = String(s ?? '');
    if(!s.trim()) return el('div', 'muted', '(no output)');
    if(code || bad) return pre(s, bad ? 'bad' : '');      // command output / file text / errors stay verbatim
    const j = tryJSON(s);
    if(j && typeof j === 'object'){ fact = fact || resultFact(j); return structured(j); }   // JSON-in-text -> table
    const r = mdRecord(clip(s, 200000));   // "**key:** value" records with embedded multi-line JSON
    if(r){ fact = fact || resultFact(r.obj); return recordView(r); }
    const f = mdFenced(clip(s, 20000));
    if(f) return f;
    const b = el('div', 'md');
    b.innerHTML = md(clip(s, 20000));   // md() escapes all transcript text before adding its own fixed tags
    return b;
  };
  if(typeof c === 'string'){ body.appendChild(text(c)); return fact; }
  if(Array.isArray(c)){
    if(!c.length) body.appendChild(el('div', 'muted', '(no output)'));
    for(const x of c){
      if(x && x.type === 'text') body.appendChild(text(x.text));
      else if(x && x.type === 'image') body.appendChild(el('div', 'muted', '[image' + (x.source && x.source.media_type ? ' · ' + x.source.media_type : '') + ']'));
      else body.appendChild(jsonDetails(x, '[' + ((x && x.type) || '?') + ']'));
    }
    return fact;
  }
  body.appendChild(c == null ? el('div', 'muted', '(no output)') : structured(c));
  return c == null ? '' : resultFact(c);
}
function toolName(n){
  const p = String(n || 'tool').split('__');   // mcp__claude_ai_Vikunja__vikunja_tasks -> "Vikunja › vikunja_tasks"
  return p[0] === 'mcp' && p.length >= 3
    ? p[1].replace(/^claude_ai_/, '').replace(/_/g, ' ') + ' › ' + p.slice(2).join('__') : p.join('__');
}
function card(cls, icon, name, args, o){
  const d = el('details', 'tool' + (cls ? ' ' + cls : ''));   // collapsed by default: nothing ever opens it
  const s = el('summary');
  s.appendChild(el('span', 'ticon', icon));
  s.appendChild(el('span', 'tname', name));
  s.appendChild(el('span', 'targs', args));
  d.appendChild(s);
  const body = el('div', 'tbody');
  d.appendChild(body);
  return {d, s, body};
}
function toolCard(p, o){
  const c = card('', '⚙', toolName(p.name), argSummary(p.input), o);
  const st = el('span', 'pill pend', 'running');
  c.s.appendChild(st);
  c.s.appendChild(timeEl(o));
  renderInput(c.body, p.name, p.input ?? {});
  if(p.id){ tools.set(p.id, {d: c.d, s: c.s, st, body: c.body, name: p.name}); if(tools.size > 500) tools.delete(tools.keys().next().value); }
  return c.d;
}
function resultText(c){
  if(typeof c === 'string') return c;
  if(Array.isArray(c)) return c.map(x => x && x.type === 'text' ? x.text : '[' + ((x && x.type) || '?') + ']').join('\n');
  return c == null ? '' : shape(c);   // summary line only; the body is rendered by renderResult()
}
function addResult(p, o, tl){
  const txt = resultText(p.content), bad = !!p.is_error;
  const t = p.tool_use_id && tools.get(p.tool_use_id);
  if(t){   // fold the result into its tool call's card
    t.st.textContent = bad ? 'error' : 'done';
    t.st.className = 'pill ' + (bad ? 'bad' : 'ok done');   // "done" is the norm: hidden, only errors show
    if(bad) t.d.classList.add('err');
    t.body.appendChild(el('div', 'tlabel', bad ? 'Error' : 'Result'));
    const fact = renderResult(t.body, p.content, bad, t.name);
    // The collapsed card's header says what came back: "→ #42 Some task · 74 lines · 2.1 KB"
    const head = bad ? clip(txt.trim().split('\n')[0], 80) : fact;
    const r = el('span', 'tres', '→ ' + (head ? head + ' · ' : '') + fmtSize(txt));
    r.title = r.textContent;
    t.s.insertBefore(r, t.st);
    return;
  }
  const c = card(bad ? 'err' : '', bad ? '!' : '↳', bad ? 'Tool error' : 'Tool result', clip(txt.replace(/\s+/g, ' '), 120), o);
  c.s.appendChild(timeEl(o));
  renderResult(c.body, p.content, bad, '');
  tl.appendChild(c.d);
}
function thinkingCard(p, o){
  const txt = String(p.thinking || '');
  if(!txt){
    // Nothing to expand: the harness kept only the signature (Claude Code writes no thinking text unless
    // its showThinkingSummaries setting is on; redacted_thinking never has any). A plain row, no dropdown.
    const r = el('div', 'think-empty');
    r.nothought = true;   // carries nothing: a look-around group continues over it (lastStep)
    r.appendChild(el('span', 'ticon', '✦'));
    r.appendChild(el('span', 'tname', 'Thought'));
    r.title = 'The harness did not record any thinking text for this step';
    r.appendChild(timeEl(o));
    return r;
  }
  const c = card('think', '✦', 'Thinking', clip(txt.replace(/\s+/g, ' '), 120), o);
  c.s.appendChild(timeEl(o));
  const b = el('div', 'md'); b.innerHTML = md(txt); c.body.appendChild(b);
  return c.d;
}
// A stream that ended leaves no more results coming: a tool call still pending then never got one.
function settleTools(){
  for(const t of tools.values()) if(t.st.classList.contains('pend')){ t.st.textContent = 'no result'; t.st.className = 'pill'; }
}
function metaRow(label, text, o, err){
  const r = el('div', 'meta-row' + (err ? ' err' : ''));
  r.appendChild(el('span', 'mlabel', label));
  r.appendChild(el('span', 'mtext', clip(text, 400)));
  r.appendChild(timeEl(o));
  return r;
}
function renderRecord(o, ty, data){
  const tl = $('sv-log');
  if(!o){ tl.appendChild(metaRow('line', data, null, false)); return; }
  const c = (o.message || {}).content;
  const err = !!(o.isApiErrorMessage || o.error || o.level === 'error');
  if(typeof o.cwd === 'string' && o.cwd.startsWith('/')) root = o.cwd.replace(/\/+$/, '');
  if(ty === 'assistant' || ty === 'user'){
    const parts = typeof c === 'string' ? [{type: 'text', text: c}] : Array.isArray(c) ? c : [];
    for(const p of parts){
      if(!p || typeof p !== 'object') continue;
      if(p.type === 'text'){ if(String(p.text ?? '').trim()) tl.appendChild(bubble(ty, p.text, o, err)); }
      else if(p.type === 'tool_use'){
        const acts = ccExplore(p), d = toolCard(p, o), s = d.firstChild;
        if(acts){
          s.querySelector('.tname').textContent = '';
          s.querySelector('.targs').textContent = actLine(acts);
          const g = exploreGroup(tl, o); countGroup(g, acts); g.c.body.appendChild(d);
        }else{
          if(p.name === 'Bash' && p.input && typeof p.input.command === 'string'){   // same wording as Codex rows
            const u = unwrapCmd(p.input.command);
            s.querySelector('.tname').textContent = isTest(u.segs) ? 'Ran tests' : 'Ran';
            s.querySelector('.targs').textContent = clip(u.text.split('\n')[0], 200);
          }
          tl.appendChild(d);
        }
      }
      else if(p.type === 'tool_result') addResult(p, o, tl);
      else if(p.type === 'thinking' || p.type === 'redacted_thinking') tl.appendChild(thinkingCard(p, o));
      else {   // Unknown block type -> collapsed card with a field table, not a raw object
        const u = card(err ? 'err' : '', '◇', String(p.type || '?'), shape(p), o);
        u.s.appendChild(timeEl(o));
        u.body.appendChild(structured(p));
        tl.appendChild(u.d);
      }
    }
    if(!parts.length) tl.appendChild(metaRow(ty, o.summary || o.subtype || '', o, err));
    return;
  }
  const text = typeof o.content === 'string' ? o.content : (o.summary || o.subtype || '');
  tl.appendChild(metaRow(o.subtype ? ty + ' · ' + o.subtype : ty, text, o, err || /error/i.test(String(o.subtype || ''))));
}
// ---- Codex progress and steps. Codex events come from adapters/codex.py ({"type":"system","subtype":"codex","content":"<event
// json>"}): message and thought text, command line + exit code + output tail, per-file diffs, plan items. Commands
// are unwrapped (`zsh -c '...'`, the gate's `cd <worktree> || exit 1; ` prefix) and classified the way Codex's own
// TUI does (codex-rs parse_command.rs), so a run of reads, searches, listings and git peeks folds into ONE
// "Explored" row, Claude Code's Read/Grep/Glob calls included. Edits merge into one "Edited" row with diffs.
// Steps with no detail (older transcripts) fold into one counted row; started/updated events go under "Everything".
const CODEX_ITEM = {
  command_execution: ['⚙', 'Ran a command', n => 'Ran ' + n + ' commands'],
  file_change: ['✎', 'Edited files', n => 'Edited files ' + n + ' times'],
  agent_message: ['✉', 'Wrote a message', n => 'Wrote ' + n + ' messages'],
  reasoning: ['✦', 'Thought', n => 'Thought ' + n + ' times'],
  web_search: ['⌕', 'Searched the web', n => 'Searched the web ' + n + ' times'],
  mcp_tool_call: ['⚙', 'Used a tool', n => 'Used tools ' + n + ' times'],
  todo_list: ['☑', 'Updated its plan', n => 'Updated its plan ' + n + ' times'],
};
const CODEX_QUIET = {'thread.started': 'Session started', 'turn.started': 'Turn started',
  'item.started': 'Step started', 'item.updated': 'Step updated'};
let codexRun = null;   // the open counted row: {kind, n, name, time}
let root = '';         // the worker's directory (cd prefix / record cwd): paths under it are shown relative
function codexEvent(o){
  const ev = typeof o.content === 'string' ? tryJSON(o.content) : null;
  return ev && typeof ev === 'object' && typeof ev.type === 'string' ? ev : null;
}
function codexItem(t){ return CODEX_ITEM[t] || ['◇', 'Finished a step', n => 'Finished ' + n + ' steps']; }
function rel(s){
  s = String(s ?? '');
  if(root){   // whole path components only: root /w/t never rewrites /w/t2
    const esc = root.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    s = s.replace(new RegExp(esc + '(?:/|(?=[\\s\'"]|$))', 'g'), m => m.endsWith('/') ? '' : '.');
  }
  return s.replace(/\/home\/[^\/\s'"]+\//g, '~/');   // outside the worktree: at least drop the home prefix
}
// Quote-aware split on ; && || | & -> [{words, start}] (words unquoted; start = offset of the segment's text)
function shSplit(s){
  const segs = [];
  let words = [], w = null, q = '', start = 0, i = 0;
  const endWord = () => { if(w !== null){ words.push(w); w = null; } };
  const endSeg = at => { endWord(); if(words.length) segs.push({words, start}); words = []; start = at; };
  for(; i < s.length; i++){
    const c = s[i];
    if(q === "'"){ if(c === "'") q = ''; else w += c; continue; }
    if(q === '"'){ if(c === '"') q = ''; else if(c === '\\' && i + 1 < s.length){ w += s[++i]; } else w += c; continue; }
    if(c === "'" || c === '"'){ q = c; w = w ?? ''; continue; }
    if(c === '\\' && i + 1 < s.length){ w = (w ?? '') + s[++i]; continue; }
    if(/\s/.test(c)){ endWord(); continue; }
    if(c === ';' || c === '|' || c === '&'){
      const two = s.slice(i, i + 2) === '&&' || s.slice(i, i + 2) === '||';
      if(two) i++;
      endSeg(i + 1);
      continue;
    }
    w = (w ?? '') + c;
  }
  endSeg(s.length);
  return segs;
}
// -> {text: the command as the worker meant it (wrapper and cd prefix gone, paths relative), segs}
function unwrapCmd(cmd){
  let c = String(cmd ?? '').trim();
  const m = c.match(/^(?:\S*\/)?(?:ba|z)?sh\s+-l?c\s+([\s\S]+)$/);
  if(m){ const w = shSplit(m[1]); if(w.length === 1 && w[0].words.length === 1) c = w[0].words[0]; }
  let segs = shSplit(c);
  if(segs.length > 1 && segs[0].words[0] === 'cd' && segs[0].words.length === 2){
    const dir = segs[0].words[1].replace(/\/+$/, '');
    if(dir.startsWith('/')) root = dir;   // a relative `cd src` is not the worker's directory
    segs = segs.slice(segs[1].words.join(' ') === 'exit 1' ? 2 : 1);
    c = segs.length ? c.slice(segs[0].start).trim() : c;
  }
  return {text: rel(c), segs};
}
const SEARCH_VALUE_FLAGS = new Set(['-g', '--glob', '-t', '--type', '-T', '--type-not', '-A', '-B', '-C', '-m',
  '--max-count', '-e', '-f', '--context', '--after-context', '--before-context', '--max-depth', '-d']);
const HEAD_VALUE_FLAGS = new Set(['-n', '-c', '--lines', '--bytes']);
function plainArgs(a, valued){   // non-flag arguments, skipping the values of flags that take one
  const out = [];
  for(let i = 0; i < a.length; i++){
    if(a[i] === '--'){ out.push(...a.slice(i + 1)); break; }
    if(a[i].startsWith('-')){ if(valued.has(a[i])) i++; continue; }
    out.push(a[i]);
  }
  return out;
}
const HELPERS = new Set(['wc', 'tr', 'cut', 'sort', 'uniq', 'tee', 'echo', 'true', 'column', 'fmt']);
const GIT_PEEK = new Set(['status', 'diff', 'log', 'show', 'branch', 'rev-parse', 'blame', 'ls-files', 'grep', 'remote']);
// One shell segment -> {verb: Read|Search|List|Git, what} | 'skip' (formatting helper) | null (real work)
function segAction(words, piped){   // piped: not the first part, so it may be reading the previous one's output
  const cmd = String(words[0] || '').split('/').pop(), args = words.slice(1);
  const pa = plainArgs(args, cmd === 'head' || cmd === 'tail' ? HEAD_VALUE_FLAGS : SEARCH_VALUE_FLAGS);
  const q = s => '"' + clip(s, 60) + '"';
  if(HELPERS.has(cmd)) return 'skip';
  if(['head', 'tail', 'cat', 'nl', 'less'].includes(cmd) && !pa.length) return 'skip';
  if(['cat', 'bat', 'less', 'nl', 'head', 'tail'].includes(cmd) && pa.length) return {verb: 'Read', what: pa.map(rel)};
  if(cmd === 'sed' && args.includes('-n')){   // sed -n '1,80p' file: -n takes no value here
    const f = args.filter(x => !x.startsWith('-')).slice(1);
    return f.length ? {verb: 'Read', what: f.map(rel)} : 'skip';
  }
  if((cmd === 'rg' || cmd === 'grep' || cmd === 'ag' || cmd === 'ack')){
    if(args.includes('--files')) return {verb: 'List', what: [rel(pa[0] || '.')]};
    if(!pa.length || (piped && pa.length === 1)) return 'skip';   // grep on a pipe: a filter, not a search
    return {verb: 'Search', what: [q(pa[0]) + (pa.length > 1 ? ' in ' + pa.slice(1).map(rel).join(', ') : '')]};
  }
  if(['ls', 'tree', 'eza', 'du'].includes(cmd)) return {verb: 'List', what: [rel(pa[0] || '.')]};
  if(cmd === 'find' || cmd === 'fd'){
    const n = args.indexOf('-name') >= 0 ? args.indexOf('-name') : args.indexOf('-iname');
    return n >= 0 ? {verb: 'Search', what: ['files named ' + q(args[n + 1] || '')]} : {verb: 'List', what: [rel(pa[0] || '.')]};
  }
  if(cmd === 'git' && GIT_PEEK.has(pa[0])) return {verb: 'Git', what: ['git ' + rel(args.join(' '))]};
  if(cmd === 'pwd') return 'skip';
  return null;
}
// -> actions[] when every part of the command only looks around, else null (it does real work: its own row)
function exploreActions(segs){
  const acts = [];
  for(const [i, s] of segs.entries()){
    const a = segAction(s.words, i > 0);
    if(a === null) return null;
    if(a !== 'skip') acts.push(a);
  }
  return acts.length ? acts : null;
}
function isTest(segs){   // judged by the program each part runs, never by its arguments (`git add tests/x.py`)
  return segs.some(({words: w}) => {
    const c = String(w[0] || '').split('/').pop(), a = w.slice(1);
    if(/^(pytest|tox|jest|vitest|nox)$/.test(c) || /test|^check\.sh$/.test(c)) return true;
    if(/^python[\d.]*$/.test(c)) return a.includes('pytest') || a.includes('unittest') || /(^|\/)test[^/]*\.py$/.test(a[0] || '');
    if(c === 'go' || c === 'cargo') return a[0] === 'test';
    if(['npm', 'yarn', 'pnpm', 'bun'].includes(c)) return a[0] === 'test' || (a[0] === 'run' && /test/.test(a[1] || ''));
    return c === 'make' && /test|check/.test(a[0] || '');
  });
}
function actLine(acts){ return acts.map(a => a.verb === 'Git' ? a.what.join(', ') : a.verb + ' ' + a.what.join(', ')).join(' · '); }
function lastStep(tl){   // the newest row, ignoring folded bookkeeping and text-less "Thought" rows
  let n = tl.lastElementChild;
  while(n && (n.classList.contains('harness') || n.nothought || n.classList.contains('pending'))) n = n.previousElementSibling;
  if(n && n._grp) for(let x = n.nextElementSibling, nx; x; x = nx){ nx = x.nextElementSibling; if(x.nothought) x.remove(); }
  return n;
}
function stepCard(cls, icon, name, args, o){
  const c = card(cls, icon, name, args, o);
  c.ts = timeEl(o);
  c.s.appendChild(c.ts);
  return c;
}
function retime(c, o){ const t = timeEl(o); c.ts.replaceWith(t); c.ts = t; }
// The open "Explored" group, reused while it is still the newest row
function exploreGroup(tl, o){
  const last = lastStep(tl);
  if(last && last._grp && last._grp.kind === 'explore'){ retime(last._grp.c, o); return last._grp; }
  const c = stepCard('grp', '⌕', 'Explored', '', o);
  c.body.classList.add('steps');
  const g = {kind: 'explore', c, reads: new Set(), n: {Search: 0, List: 0, Git: 0}, bad: 0};
  c.d._grp = g;
  tl.appendChild(c.d);
  return g;
}
function countGroup(g, acts){
  for(const a of acts){ if(a.verb === 'Read') a.what.forEach(f => g.reads.add(f)); else g.n[a.verb]++; }
  const p = [], pl = (n, one, many) => n + ' ' + (n === 1 ? one : many);
  if(g.reads.size) p.push(pl(g.reads.size, 'file', 'files'));
  if(g.n.Search) p.push(pl(g.n.Search, 'search', 'searches'));
  if(g.n.List) p.push(pl(g.n.List, 'listing', 'listings'));
  if(g.n.Git) p.push(pl(g.n.Git, 'git check', 'git checks'));
  g.c.s.querySelector('.targs').textContent = p.join(', ');
}
function outputBlock(body, ev, bad){
  if(!str(ev.output)) return;
  const n = ev.output_lines || 0, shown = ev.output.split('\n').length;
  body.appendChild(tlabel(n > shown ? 'Output (last ' + shown + ' of ' + n + ' lines)' : 'Output'));
  body.appendChild(pre(ev.output, bad ? 'bad' : ''));
}
function str(v){ return typeof v === 'string' && v.trim() !== ''; }
function codexCommand(tl, ev, o){
  const {text, segs} = unwrapCmd(ev.command), code = ev.exit_code, bad = Number.isInteger(code) && code !== 0;
  const acts = exploreActions(segs);
  if(acts){
    const g = exploreGroup(tl, o);
    countGroup(g, acts);
    const searchMiss = bad && code === 1 && acts.every(a => a.verb === 'Search');   // rg/grep exit 1 = no match
    const line = str(ev.output) || bad ? el('details', 'step') : el('div', 'step');
    const head = line.tagName === 'DETAILS' ? line.appendChild(el('summary')) : line;
    head.appendChild(el('span', 'sline', actLine(acts)));
    head.title = text;
    if(searchMiss) head.appendChild(el('span', 'muted', 'no matches'));
    else if(bad){ head.appendChild(el('span', 'pill bad', 'exit ' + code)); g.c.d.classList.add('err'); }
    if(line.tagName === 'DETAILS'){ const b = line.appendChild(el('div', 'tbody')); b.appendChild(pre('$ ' + text)); outputBlock(b, ev, bad && !searchMiss); }
    g.c.body.appendChild(line);
    return;
  }
  const test = isTest(segs);
  const c = stepCard(bad ? 'err' : '', test ? (bad ? '✗' : '✓') : '⚙', test ? 'Ran tests' : 'Ran', clip(text.split('\n')[0], 200), o);
  c.s.querySelector('.targs').title = clip(text, 2000);
  if(bad) c.s.insertBefore(el('span', 'pill bad', 'exit ' + code), c.ts);
  else if(test && Number.isInteger(code)) c.s.insertBefore(el('span', 'pill ok', 'passed'), c.ts);
  c.body.appendChild(pre('$ ' + text));
  outputBlock(c.body, ev, bad);
  tl.appendChild(c.d);
}
function diffPre(d){
  const p = el('pre', 'tpre diff');
  for(const ln of clip(d, 20000).split('\n'))
    p.appendChild(el('span', ln.startsWith('@@') ? 'hunk' : ln[0] === '+' ? 'add' : ln[0] === '-' ? 'del' : null, ln));
  return p;
}
function plusMinus(a, r){
  const s = el('span', 'pm');
  if(a) s.appendChild(el('span', 'pa', '+' + a));
  if(r) s.appendChild(el('span', 'pr', '−' + r));
  return s;
}
function codexEdit(tl, ev, o){
  const files = Array.isArray(ev.files) && ev.files.length ? ev.files
    : (ev.paths || []).map(p => ({path: p, kind: 'update'}));   // transcripts from before diffs were recorded
  const last = lastStep(tl);
  let g = last && last._grp && last._grp.kind === 'edit' ? last._grp : null;
  if(g) retime(g.c, o);
  else {
    g = {kind: 'edit', c: stepCard('edit', '✎', 'Edited', '', o), files: new Map(), add: 0, del: 0};
    g.pm = g.c.s.insertBefore(el('span'), g.c.ts);
    g.c.d._grp = g;
    tl.appendChild(g.c.d);
  }
  for(const f of files){
    const name = rel(f.path), verb = f.kind === 'add' ? 'Created ' : f.kind === 'delete' ? 'Deleted ' : '';
    g.add += f.added || 0; g.del += f.removed || 0;
    g.files.set(name, (g.files.get(name) || 0) + 1);
    const fd = el(str(f.diff) ? 'details' : 'div', 'step');
    const head = fd.tagName === 'DETAILS' ? fd.appendChild(el('summary')) : fd;
    head.appendChild(el('span', 'sline', verb + name));
    head.appendChild(plusMinus(f.added, f.removed));
    if(str(f.diff)) fd.appendChild(diffPre(f.diff));
    g.c.body.appendChild(fd);
  }
  const names = [...g.files.keys()];
  g.c.s.querySelector('.tname').textContent = names.length > 1 ? 'Edited ' + names.length + ' files' : 'Edited';
  g.c.s.querySelector('.targs').textContent = names.map(n => n.split('/').pop()).join(', ');
  g.c.s.querySelector('.targs').title = names.join('\n');
  g.pm.replaceWith(g.pm = plusMinus(g.add, g.del));
}
function thoughtRow(text, o){
  const t = String(text).trim(), lines = t.split('\n');
  const head = lines[0].replace(/^\*\*(.*)\*\*$/, '$1'), rest = lines.slice(1).join('\n').trim();
  if(!rest){
    const r = el('div', 'think-empty thought');
    r.appendChild(el('span', 'ticon', '✦'));
    r.appendChild(el('span', 'ttext', clip(head, 300)));
    r.appendChild(timeEl(o));
    return r;
  }
  const c = stepCard('think', '✦', clip(head, 140), '', o);
  const b = el('div', 'md'); b.innerHTML = md(rest); c.body.appendChild(b);
  return c.d;
}
function ccExplore(p){   // a Claude Code tool call that only looks around -> actions for the Explored group
  const i = p.input && typeof p.input === 'object' ? p.input : {}, s = v => typeof v === 'string' && v;
  if(p.name === 'Read' && s(i.file_path)) return [{verb: 'Read', what: [rel(i.file_path)]}];
  if(p.name === 'Grep' && s(i.pattern)) return [{verb: 'Search', what: ['"' + clip(i.pattern, 60) + '"' + (s(i.path) ? ' in ' + rel(i.path) : '')]}];
  if(p.name === 'Glob' && s(i.pattern)) return [{verb: 'List', what: [rel(i.pattern)]}];
  if(p.name === 'LS' && s(i.path)) return [{verb: 'List', what: [rel(i.path)]}];
  if(p.name === 'Bash' && s(i.command)) return exploreActions(unwrapCmd(i.command).segs);
  return null;
}
function codexDetail(o, ev){   // -> true when the step carried detail and got its own row
  const tl = $('sv-log');
  const t = ev.item_type;
  if(t === 'agent_message' && str(ev.text)){ codexRun = null; tl.appendChild(bubble('assistant', ev.text, o)); return true; }
  if(t === 'reasoning' && str(ev.text)){ codexRun = null; tl.appendChild(thoughtRow(ev.text, o)); return true; }
  if(t === 'command_execution' && str(ev.command)){ codexRun = null; codexCommand(tl, ev, o); return true; }
  if(t === 'file_change' && ((Array.isArray(ev.files) && ev.files.length) || (Array.isArray(ev.paths) && ev.paths.length))){
    codexRun = null; codexEdit(tl, ev, o); return true;
  }
  if(t === 'todo_list' && Array.isArray(ev.todo) && ev.todo.length){
    codexRun = null;
    const done = ev.todo.filter(i => i.done).length;
    const c = stepCard('', '☑', 'Plan', done + ' of ' + ev.todo.length + ' done', o);
    const ul = el('ul', 'todo');
    for(const i of ev.todo){ const li = el('li', i.done ? 'done' : null, (i.done ? '✓ ' : '○ ') + i.text); ul.appendChild(li); }
    c.body.appendChild(ul);
    tl.appendChild(c.d);
    return true;
  }
  let label, what;
  if(t === 'mcp_tool_call' && str(ev.tool)){ label = 'Used'; what = ev.tool; }
  else if(t === 'web_search' && str(ev.query)){ label = 'Searched the web'; what = ev.query; }
  else return false;
  codexRun = null;
  const r = el('div', 'think-empty cx');
  r.appendChild(el('span', 'ticon', codexItem(t)[0]));
  r.appendChild(el('span', 'tname', label));
  r.appendChild(el('span', 'targs', clip(what, 240)));
  r.appendChild(timeEl(o));
  tl.appendChild(r);
  return true;
}
function codexRecord(o, ev){
  const tl = $('sv-log');
  if(CODEX_QUIET[ev.type]){
    const what = CODEX_QUIET[ev.type] + (ev.item_type ? ': ' + codexItem(ev.item_type)[1].toLowerCase() : '');
    addHarness({type: 'codex', codex: {type: what}}, 'codex');
    return;
  }
  harnessGroup = null;
  if(ev.type === 'item.completed' && codexDetail(o, ev)) return;
  if(ev.type === 'item.completed'){
    const kind = String(ev.item_type || '');
    if(codexRun && codexRun.kind === kind && codexRun.row.isConnected){   // folded bookkeeping rows don't break a run
      codexRun.n++;
      codexRun.name.textContent = codexItem(kind)[2](codexRun.n);
      codexRun.time.replaceWith(codexRun.time = timeEl(o));
      return;
    }
    const [icon, label] = codexItem(kind), r = el('div', 'think-empty cx');
    r.appendChild(el('span', 'ticon', icon));
    const name = el('span', 'tname', label);
    r.appendChild(name);
    const time = timeEl(o);
    r.appendChild(time);
    tl.appendChild(r);
    codexRun = {kind, n: 1, name, time, row: r};
    return;
  }
  codexRun = null;
  if(ev.type === 'turn.completed'){
    const u = ev.usage || {}, bits = ['Turn finished'];
    if(Number.isFinite(u.input_tokens)) bits.push(kfmt(u.input_tokens) + ' tokens read');
    if(Number.isFinite(u.output_tokens)) bits.push(kfmt(u.output_tokens) + ' written');
    const r = el('div', 'cx-turn');
    r.appendChild(el('span', null, bits.join(' · ')));
    r.appendChild(timeEl(o));
    tl.appendChild(r);
    return;
  }
  if(ev.type === 'turn.failed' || ev.type === 'error'){
    tl.appendChild(metaRow(ev.type === 'error' ? 'Codex error' : 'Turn failed', ev.message || 'No details recorded', o, true));
    return;
  }
  addHarness({type: 'codex', codex: {type: ev.type}}, 'codex');   // an event type this page does not know yet
}
function addLine(data){
  let ty = '?', o = null;
  try{ o = JSON.parse(data); }catch(e){ o = null; }
  const kind = recordKind(o);
  if(o && typeof o === 'object') ty = recordType(o); else o = null;
  if(o && o.type === 'broker' && o.subtype === 'web_message' && typeof o.id === 'string'){ webMsg(o); return; }
  const cx = o && o.type === 'system' && o.subtype === 'codex' ? codexEvent(o) : null;
  if(cx){
    const log = $('sv-log'), stick = atBottom(log);
    codexRecord(o, cx);
    pinPending();
    trim(log);
    if(stick) log.scrollTop = log.scrollHeight;
    return;
  }
  if(kind === 'harness'){ addHarness(o, ty); return; }
  harnessGroup = null;   // any non-harness line ends the current collapsed run
  codexRun = null;
  const log = $('sv-log'), stick = atBottom(log);
  renderRecord(o, ty, data);
  pinPending();
  trim(log);
  if(stick) log.scrollTop = log.scrollHeight;
}

return {addLine, resetLog, settleTools};
}
"""

CSS = r"""  /* ---- timeline */
  /* Bound only the transcript, not the page. No assumed header/title/composer height: the document
     scrolls to the whole card, and this list scrolls to its last row even in a short window. */
  .tl{flex:none;overflow:auto;max-height:60vh;min-height:0;padding:16px 18px 24px}
  .tl{display:flex;flex-direction:column;gap:10px}
  .tl > *{flex-shrink:0}
  .notice{color:var(--muted);font-size:13px;padding:14px;border:1px dashed var(--line);border-radius:12px}
  .ev{max-width:880px;border-radius:14px;padding:10px 14px;border:1px solid var(--line);background:var(--panel)}
  .ev.user{background:var(--user);border-color:color-mix(in srgb,var(--accent) 22%,var(--line));align-self:flex-end;max-width:760px}
  .ev.user.you{align-self:stretch;max-width:880px}
  .ev.err{border-color:var(--red);background:color-mix(in srgb,var(--red) 7%,var(--panel))}
  .ev-meta{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--muted);margin-bottom:4px}
  .who{font-weight:650;color:var(--ink)}
  .ev.user .who{color:var(--accent)}
  .ev.you.pending{border-style:dashed}
  .worker-reply{margin-top:12px;padding:12px;border:1px solid var(--line);border-radius:8px;background:var(--s1);color:var(--t1);white-space:pre-wrap;overflow-wrap:anywhere}
  .ts{font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums;margin-left:auto;white-space:nowrap}
  .md{overflow-wrap:anywhere}
  .md p{margin:.35em 0} .md ul,.md ol{margin:.35em 0;padding-left:1.4em}
  .md h3,.md h4,.md h5,.md h6{margin:.6em 0 .3em;font-size:14.5px}
  .md blockquote{margin:.4em 0;padding-left:10px;border-left:3px solid var(--line);color:var(--muted)}
  .md code{background:var(--code);border:1px solid var(--line);border-radius:5px;padding:0 4px;font-size:12.5px}
  .md pre{background:var(--code);border:1px solid var(--line);border-radius:10px;padding:10px 12px;overflow:auto;font-size:12.5px}
  .md pre code{background:none;border:0;padding:0}
  .md a{color:var(--accent)}
  .md hr{border:0;border-top:1px solid var(--line)}
  details.tool{max-width:880px;border:1px solid var(--line);border-radius:12px;background:var(--panel2)}
  details.tool.err{border-color:var(--red);background:color-mix(in srgb,var(--red) 7%,var(--panel2))}
  details.tool > summary{list-style:none;display:flex;align-items:center;gap:9px;padding:7px 12px;cursor:pointer;font-size:13px;min-width:0}
  details.tool > summary::-webkit-details-marker{display:none}
  details.tool > summary::after{content:"›";color:var(--faint);transition:transform .15s}
  details.tool[open] > summary::after{transform:rotate(90deg)}
  .ticon{width:20px;height:20px;border-radius:5px;display:grid;place-items:center;font-size:11px;flex:none;
         background:color-mix(in srgb,var(--accent) 12%,transparent);color:var(--accent)}
  details.tool.err .ticon{background:color-mix(in srgb,var(--red) 16%,transparent);color:var(--red)}
  details.think .ticon{background:color-mix(in srgb,var(--amber) 16%,transparent);color:var(--amber)}
  .tname{font-weight:600;white-space:nowrap}
  .targs{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
         overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
  .tbody{padding:4px 12px 12px;border-top:1px solid var(--line)}
  .tlabel{font-size:12px;font-weight:600;color:var(--muted);margin:8px 0 4px}
  .tpre{margin:0;background:var(--code);border:1px solid var(--line);border-radius:9px;padding:9px 11px;
        max-height:360px;overflow:auto;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}
  .tpre.bad{border-color:color-mix(in srgb,var(--red) 55%,var(--line));color:var(--red)}
  .meta-row{display:flex;align-items:baseline;gap:8px;font-size:12px;color:var(--muted);padding:0 4px;min-width:0}
  .meta-row.err{color:var(--red)}
  .mlabel{font-weight:600;white-space:nowrap}
  .mtext{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
  /* Human-readable tool cards (key/value tables, diffs, collapsed JSON details) */
  table.kv{border-collapse:collapse;width:100%;font-size:12.5px;margin:2px 0}
  table.kv th{text-align:left;vertical-align:top;font-weight:600;color:var(--muted);padding:3px 12px 3px 0;white-space:nowrap;width:1%}
  table.kv td{padding:3px 0;vertical-align:top;overflow-wrap:anywhere;min-width:0}
  table.kv tr+tr>*{border-top:1px solid var(--line)}
  table.kv table.kv{font-size:12px}
  .kvc{font-size:12px}
  details.jd{margin:2px 0}
  details.jd > summary{cursor:pointer;color:var(--muted);font-size:12px}
  details.jd > .tpre,details.jd > table.kv{margin-top:4px}
  .fpath{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;font-weight:600;margin:8px 0 2px;overflow-wrap:anywhere}
  .tdesc{color:var(--muted);font-size:12.5px;margin-top:8px}
  .tpre.diff span{display:block;min-height:1.2em}
  .tpre.diff .del{color:var(--red);background:color-mix(in srgb,var(--red) 8%,transparent)}
  .tpre.diff .add{color:var(--green);background:color-mix(in srgb,var(--green) 8%,transparent)}
  details.harness{font-size:12px;color:var(--muted);padding:0 4px;display:none}
  .show-harness details.harness{display:block}
  .think-empty{display:flex;align-items:center;gap:8px;padding:2px 4px;font-size:12.5px;color:var(--faint)}
  .think-empty .ticon{background:color-mix(in srgb,var(--amber) 16%,transparent);color:var(--amber)}
  .think-empty .ts{margin-left:auto}
  .think-empty.cx{color:var(--muted)}
  .think-empty.cx .ticon{background:var(--panel2);color:var(--muted)}
  details.tool.grp .ticon,details.tool.edit .ticon{background:var(--panel);color:var(--muted)}
  .steps{padding:2px 12px 8px}
  .step{font-size:12.5px;min-width:0}
  .step > summary,div.step{display:flex;align-items:center;gap:8px;padding:3px 0;list-style:none;min-width:0}
  .step > summary{cursor:pointer}
  .step > summary::-webkit-details-marker{display:none}
  .step > summary::after{content:"›";color:var(--faint);transition:transform .15s}
  .step[open] > summary::after{transform:rotate(90deg)}
  .sline{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
  .step > .tbody,.step > .tpre{border:0;padding:0 0 6px;margin-top:2px}
  .steps details.tool{border:0;background:transparent;max-width:none}
  .steps details.tool > summary{padding:3px 0}
  .pill.done{display:none}
  .steps details.tool .ticon,.steps details.tool .tname:empty,.steps .pill.ok{display:none}
  .pm{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;display:inline-flex;gap:6px;flex:none}
  .pa{color:var(--green)} .pr{color:var(--red)}
  .tpre.diff .hunk{color:var(--faint)}
  .thought{color:var(--muted)}
  .thought .ttext{font-style:italic}
  ul.todo{list-style:none;margin:8px 0 2px;padding:0;font-size:13px;line-height:1.7}
  ul.todo li.done{color:var(--faint)}
  .cx-turn{display:flex;align-items:center;gap:10px;font-size:11.5px;color:var(--faint);padding:6px 4px}
  .cx-turn::before,.cx-turn::after{content:"";flex:1;border-top:1px solid var(--line)}
  .cx-turn .ts{order:3}
  details.harness summary{cursor:pointer;color:var(--faint)}
  details.harness .ln{padding:1px 0 1px 16px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
  /* JSON message bodies as structured cards */
  .jcard{display:flex;flex-direction:column;gap:2px;min-width:0}
  .jchips{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 4px}
  .jchips .chip{padding:2px 10px;max-width:100%;overflow-wrap:anywhere}
  .chip a{color:var(--accent);font-weight:650}
  .jcard details.jd{border-left:2px solid var(--line);padding-left:8px}
  /* Tool results: one-line fact in the card header, entity chips, folded empty/default fields */
  .tres{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:40%;flex:none}
  .ents{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:flex-start}
  .ents > details.jd{margin:0}
  details.ent > summary{color:var(--ink);font-weight:600}
  details.blank > summary{color:var(--faint);font-style:italic}
  details.blank table.kv{opacity:.75}
  .btn.more{margin:4px 2px 2px;align-self:flex-start}
  .bk{font-size:11.5px;color:var(--faint)}
"""

MOUNT = r"""// The same renderer the broker inlines, scoped to this read-only agent view.
// Conversation (the default) hides harness bookkeeping and system rows; Everything shows them. System rows that
// report an error stay visible in both. note(record) adds a record the page itself knows about (a message sent
// from here) through the same renderer, so it reads like any other transcript line.
export function mount(host, {url, css}) {
  const root = host.attachShadow({mode: 'open'});
  const style = document.createElement('link');
  style.rel = 'stylesheet'; style.href = css;
  const local = document.createElement('style');
  local.textContent = `:host{--panel:var(--s1);--panel2:var(--s2);--ink:var(--t1);
    --muted:var(--t2);--faint:var(--t3);--accent:var(--brand);--green:var(--run);
    --amber:var(--wait);--red:var(--block);--user:var(--s1);--code:var(--s2);color:var(--t1);
    display:flex;flex-direction:column;position:relative;min-height:0}
    *{box-sizing:border-box} .tl{flex:1;max-height:none;height:auto;background:var(--bg)}
    .controls{flex:none;display:flex;align-items:center;gap:12px;padding:8px 16px;border-bottom:1px solid var(--line)}
    .seg{display:inline-flex;padding:2px;border-radius:9px;background:var(--s3)}
    .seg button{font:inherit;font-size:13px;font-weight:600;color:var(--t2);background:none;border:0;border-radius:7px;
      min-height:32px;padding:0 12px;cursor:pointer}
    .seg button[aria-pressed=true]{background:var(--s1);color:var(--t1);box-shadow:0 1px 2px rgba(20,25,32,.12)}
    .hidden-n{font-size:12px;color:var(--t2);flex:1;min-width:0}
    :focus-visible{outline:2px solid var(--brand);outline-offset:2px}
    .tl:not(.show-harness) .harness,.tl:not(.show-harness) .meta-row:not(.err){display:none}
    .pill{font-size:12px;color:var(--t2);white-space:nowrap}
    .latest{position:absolute;left:50%;bottom:16px;transform:translateX(-50%);font:inherit;font-size:13px;font-weight:600;
      color:var(--onbrand);background:var(--brand);border:0;border-radius:999px;min-height:32px;padding:0 14px;
      box-shadow:0 2px 8px rgba(20,25,32,.2);cursor:pointer}
    .latest[hidden]{display:none}
    @media(max-width:720px){.controls{padding:6px 12px}.seg button{min-height:36px}.tl{padding:12px}.hidden-n .wide{display:none}}
    @media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}`;
  const controls = el('div', 'controls'), status = el('span', 'pill', 'Connecting');
  status.setAttribute('role', 'status');
  const seg = el('div', 'seg'), conv = el('button', null, 'Conversation'), all = el('button', null, 'Everything');
  seg.setAttribute('role', 'group'); seg.setAttribute('aria-label', 'Transcript filter');
  const hidden = el('span', 'hidden-n'), count = el('span');
  for(const b of [conv, all]) b.type = 'button';
  seg.append(conv, all);
  controls.append(seg, hidden, status);
  const log = el('div', 'tl');
  log.setAttribute('aria-label', 'Live transcript');
  const latest = el('button', 'latest', 'Latest');
  latest.type = 'button'; latest.hidden = true;
  root.append(style, local, controls, log, latest);
  const renderer = createTranscript(id => id === 'sv-log' ? log : count);
  // Follow the newest line until the reader scrolls up. Per-line checks miss bursts that land before layout.
  let counting = false, follow = true;
  function recount(){
    // Batched: a reconnect replays the whole ring in one burst.
    if(counting) return;
    counting = true;
    requestAnimationFrame(() => {
      counting = false;
      if(follow) log.scrollTop = log.scrollHeight;
      const n = log.querySelectorAll('details.harness .ln, .meta-row:not(.err)').length;
      const off = log.classList.contains('show-harness') || !n, words = n === 1 ? 'system event' : 'system events';
      hidden.replaceChildren(...(off ? [] : [n + ' ', el('span', 'wide', words + ' '), 'hidden']));
      hidden.title = off ? '' : n + ' ' + words + ' hidden';
      latest.hidden = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
    });
  }
  function filter(everything){
    log.classList.toggle('show-harness', everything);
    conv.setAttribute('aria-pressed', String(!everything));
    all.setAttribute('aria-pressed', String(everything));
    recount();
  }
  conv.onclick = () => filter(false);
  all.onclick = () => filter(true);
  filter(false);
  log.addEventListener('scroll', () => { follow = log.scrollTop + log.clientHeight >= log.scrollHeight - 40; recount(); }, {passive: true});
  latest.onclick = () => { log.scrollTop = log.scrollHeight; recount(); };
  const add = line => { renderer.addLine(line); recount(); };
  let stream = null, retry = null, stopped = false;
  function reconnect() {
    if (stopped || retry !== null) return;
    // A reload can briefly answer 404 before the agent re-registers. Browsers
    // permanently close EventSource on that status instead of retrying it.
    retry = setTimeout(() => { retry = null; connect(); }, 2000);
  }
  function connect() {
    if (stopped) return;
    if (stream) stream.close();
    const current = stream = new EventSource(url);
    current.onopen = () => { status.textContent = 'Live'; };
    current.onmessage = e => add(e.data);
    current.addEventListener('reset', () => { renderer.resetLog(); recount(); });
    current.addEventListener('end', () => { status.textContent = 'Agent unavailable'; current.close(); reconnect(); });
    current.onerror = () => {
      status.textContent = 'Reconnecting';
      if (current.readyState === EventSource.CLOSED) reconnect();
    };
  }
  connect();
  return {destroy() { stopped = true; clearTimeout(retry); stream.close(); host.replaceChildren(); }, seek() {},
    note(record) { add(JSON.stringify(record)); }};
}
"""


def transcript_basic():
    return BASIC


def transcript_format():
    return FORMAT


def transcript_text():
    return TEXT


def transcript_renderer():
    return RENDERER


def transcript_css():
    return CSS


def transcript_module():
    return FORMAT + BASIC + TEXT + RENDERER + MOUNT
