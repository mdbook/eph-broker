// Module host contract (docs/modules.md): mount and quickView may return cleanup;
// ctx.baseUrl prefixes UI routes and ctx.signal aborts on unmount.
// The Agents tab: the directory on the left, the picked agent on the right, drawn in the broker's own DOM as the
// approved mockup draws it (docs/mockups/agent-view.html, tickets 3193 and 3322): a transcript, not a chat. Every
// turn is a full-width row with a left gutter (sender, source, time) and a thin rule in the sender's colour; replies
// are set in the mockup's reading serif; a turn's tool steps fold behind one tally line. One header row for every
// agent kind. A hermes agent with a view streams its sidecar's rows through this module's re-typed JSON/SSE proxy
// (api/a/NAME/...); no sidecar page, script or stylesheet is loaded. A claude-code or codex agent with the tail cap
// shows its transcript (transcript.js) and, with steer-pull, a composer under it.
// The optional host hook ctx.agentSummary(name) adds what the host knows (what it is doing, model, context).
const DIR_STATE = {busy: ['run', 'Working'], idle: ['idle', 'Idle'], live: ['run', 'Online'], grey: ['off', 'Offline']};
const STATES = new Set(['run', 'wait', 'idle', 'block', 'off']);
const KIND = {'claude-code': 'Claude Code', codex: 'Codex', hermes: 'Hermes', generic: 'Agent'};

// Shared by the Agents tab and the Fleet quick view, so both read the same. Every control that can sit in a row
// takes its height from --ctl (36px, the 44px tap target on phones), so a text box and the buttons beside it line up
// by construction. The dash defines --ctl too; these are scoped to the views.
const BASE_CSS = `
.adir,.aqv{--ctl:36px;--pop:0 8px 24px rgba(20,25,32,.12);--well:#1c2027;color:var(--t1,inherit)}
:root[data-theme=dark] :is(.adir,.aqv){--pop:0 8px 24px rgba(0,0,0,.45);--well:#0a0c10}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) :is(.adir,.aqv){--pop:0 8px 24px rgba(0,0,0,.45);--well:#0a0c10}}
:is(.adir,.aqv) [hidden]{display:none!important}
:is(.adir,.aqv) :focus-visible{outline:2px solid var(--brand);outline-offset:2px}
.ad-st{flex:none;display:inline-flex;align-items:center;gap:8px;height:24px;font:600 13px/20px -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif;
  padding:0 10px;border-radius:999px;white-space:nowrap;color:var(--t2);background:color-mix(in srgb,currentColor 13%,var(--s1));box-sizing:border-box}
.ad-st::before{content:'';flex:none;box-sizing:border-box;width:8px;height:8px;border-radius:50%;border:1.5px solid var(--t3)}
.ad-st.run{color:var(--run)}.ad-st.run::before{border:0;background:currentColor}
.ad-st.wait{color:var(--wait)}.ad-st.wait::before{border:2px solid currentColor}
.ad-st.block{color:var(--block)}.ad-st.block::before{border:0;border-radius:2px;background:currentColor}
.ad-st.off{color:var(--t3)}.ad-st.off::before{border:0;height:2px;border-radius:1px;background:currentColor}

/* Menus (actions, session, context, send options): a popover anchored to its button, inside the card. */
.ad-menu-w{position:relative;flex:none;display:flex;min-width:0}
.ad-menu{position:absolute;right:0;top:calc(100% + 4px);z-index:10;min-width:220px;max-width:min(24rem,calc(100vw - 32px));padding:4px;margin:0;
  box-sizing:border-box;background:var(--s1);border:1px solid var(--line);border-radius:10px;box-shadow:var(--pop);
  max-height:60vh;overflow:auto;font:14px/20px -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif}
.ad-menu-w.left .ad-menu{left:0;right:auto}
.ad-menu button{display:flex;width:100%;align-items:center;justify-content:space-between;gap:12px;min-height:var(--ctl);padding:0 12px;
  border:0;background:none;border-radius:6px;font:inherit;color:var(--t1);text-align:left;cursor:pointer;white-space:nowrap}
.ad-menu button:hover,.ad-menu button:focus-visible{background:var(--s2)}
.ad-menu button:focus-visible{outline-offset:-2px}
.ad-menu button:disabled{opacity:.5;cursor:default;background:none}
.ad-menu button.bad{color:var(--block)}
.ad-menu button span{font-size:13px;color:var(--t2)}
.ad-menu hr{border:0;border-top:1px solid var(--line);margin:4px}
.ad-menu .ad-lbl{font-size:12px;line-height:16px;font-weight:500;color:var(--t2);padding:8px 12px 4px}
.ad-menu .ad-note{margin:4px 12px 8px;font-size:13px;color:var(--t2);white-space:normal}

/* ======================================================================================================
   The agent view, ported from docs/mockups/agent-view.html. The mockup's tokens are re-pointed at the dash tokens
   here, so its rules read as they do there and the dash theme button recolors an open view without remounting it.
   Class names carry an ov- prefix so nothing collides with the dash's own .bar, .msg, .acts or .search.
   ====================================================================================================== */
.ov{
  --ground:var(--s1);--panel:var(--s2);--rule:var(--line);--rule2:color-mix(in srgb,var(--t1) 16%,var(--line));
  --ink:var(--t1);--dim:var(--t2);--faint:var(--t3);--iris:var(--brand);--amber:var(--wait);--bad:var(--block);
  --you:var(--brand);--agent:var(--run);
  /* the terminal wells stay dark in both themes */
  --w-ink:#eef1f5;--w-term:#c6ccd5;--w-dim:#9aa3af;--w-amber:#e3a63d;--w-bad:#f27c71;
  --serif:"Iowan Old Style","Charter","Bitstream Charter","Source Serif 4","Sitka Text",Cambria,Georgia,serif;
  --sans:"Avenir Next","Seravek","Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,sans-serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono","JetBrains Mono",Menlo,Consolas,monospace;
  --gut:5.4rem;--w:64rem;--edge:20px;--ease:cubic-bezier(.2,0,0,1);--q:120ms;
  container:ov/inline-size;
  position:relative;flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;
  background:var(--ground);color:var(--ink);font:15px/1.5 var(--sans);-webkit-text-size-adjust:100%;
}
.ov ::selection{background:color-mix(in srgb,var(--iris) 22%,transparent)}
.ov svg.ov-i{width:1rem;height:1rem;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round;flex:none}

/* ---- the one header row: name, status, harness and host, session, live state, model and tokens, tools, ··· */
.ov-bar{flex:none;position:relative;z-index:3;display:flex;align-items:center;gap:8px 14px;min-width:0;
  padding:10px var(--edge);border-bottom:1px solid var(--rule);background:var(--ground)}
.ov-back{display:none;align-items:center;min-height:var(--ctl);margin-left:-8px;padding:0 8px;border-radius:8px;
  color:var(--iris);font:600 14px var(--sans);text-decoration:none;white-space:nowrap}
.ov-back:hover{background:var(--panel)}
.ov-idn{display:flex;align-items:center;gap:10px;min-width:0;flex:0 1 auto}
.ov-nm{margin:0;font:600 1.3rem/1.2 var(--serif);letter-spacing:.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.ov-sub{display:contents}
.ov-hz{color:var(--dim);font-size:.84rem;white-space:nowrap}
.ov-sess{position:relative;display:block;max-width:16rem;height:var(--ctl);background:transparent;color:var(--ink);border:1px solid transparent;
  border-radius:8px;padding:0 1.5rem 0 .5rem;font:500 .9rem var(--sans);cursor:pointer;text-align:left;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;transition:background-color var(--q) var(--ease)}
.ov-sess::after{content:"";position:absolute;right:.6rem;top:50%;width:5px;height:5px;margin-top:-4px;border:solid var(--dim);
  border-width:0 1.5px 1.5px 0;transform:rotate(45deg)}
.ov-sess:hover,.ov-sess[aria-expanded=true]{background:var(--panel)}
.ov-sessname{display:none;font:500 .9rem var(--sans);white-space:nowrap}.ov-sessname.ov-solo{display:inline}
.ov-state{color:var(--dim);font-size:.84rem;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.ov.busy .ov-state{color:var(--amber)}
.ov-grow{flex:1 1 0}
.ov-tools{display:flex;align-items:center;gap:4px;flex:none}
/* header buttons: quiet until hovered, one control tall */
.ov-hb{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:var(--ctl);min-width:var(--ctl);padding:0 10px;flex:none;
  background:none;border:1px solid transparent;border-radius:8px;color:var(--dim);font:.8rem var(--sans);cursor:pointer;white-space:nowrap;
  transition:color var(--q) var(--ease),background var(--q) var(--ease),border-color var(--q) var(--ease)}
.ov-hb:hover,.ov-hb[aria-expanded=true]{color:var(--ink);background:var(--panel)}
.ov-hb.ov-ic{padding:0}
.ov-stats{color:var(--faint);font-variant-numeric:tabular-nums}.ov-stats b{font-weight:500;color:var(--dim)}
.ov-steps[aria-pressed=true]{color:var(--ink);border-color:var(--rule2)}
.ov .ad-menu .ov-c{display:none}

/* search row, under the header; lines up with the transcript column */
.ov-search{flex:none;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--rule);
  padding:8px max(var(--edge),calc((100% - var(--w)) / 2 + var(--edge))) 10px max(calc(var(--edge) + var(--gut) + 1rem),calc((100% - var(--w)) / 2 + var(--edge) + var(--gut) + 1rem))}
.ov-search input{flex:1;min-width:0;height:var(--ctl);background:var(--panel);color:var(--ink);border:1px solid var(--rule2);border-radius:8px;padding:0 10px;font:.92rem var(--sans)}
.ov-search input:focus{outline:none;border-color:var(--iris)}
.ov-qn{color:var(--dim);font-size:.8rem;font-variant-numeric:tabular-nums;white-space:nowrap}

/* the broker's "what it is doing" line (quick view), and connection notes */
.ov-qnow,.ov-note{flex:none;margin:0;padding:8px var(--edge);border-bottom:1px solid var(--rule);font-size:.86rem;color:var(--dim);overflow-wrap:anywhere}
.ov-qnow b{color:var(--ink);font-weight:600;margin-right:6px}.ov-qnow .w{color:var(--amber);font-weight:600}

/* ---- the transcript: rows with a right-aligned gutter (who, source, time) and a 2px rule in the sender's colour */
.ov .log{flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;padding:4px 0 24px}
.ov .log:focus-visible{outline-offset:-2px}
.ov-lw{max-width:var(--w);margin:0 auto;padding:0 var(--edge)}
.ov-earlier{display:grid;grid-template-columns:var(--gut) minmax(0,1fr);column-gap:1rem;margin:4px 0 0}
.ov-tgl{background:none;border:0;padding:0;color:var(--dim);font:.76rem var(--sans);cursor:pointer;text-decoration:underline dotted;text-underline-offset:3px;
  min-height:28px;white-space:nowrap}
.ov-tgl:hover{color:var(--ink)}
.ov-earlier .ov-tgl{grid-column:2;justify-self:start;color:var(--faint);font-size:.8rem}
.ov-row{display:grid;grid-template-columns:var(--gut) minmax(0,1fr);column-gap:1rem}
.ov-gut{padding:.6rem .8rem .5rem 0;border-right:2px solid var(--rail,var(--rule2));text-align:right;
  font-size:.72rem;line-height:1.35;color:var(--faint);font-variant-numeric:tabular-nums;min-width:0;overflow-wrap:anywhere}
.ov-who{display:block;font-size:.8rem;font-weight:600;color:var(--dim);margin-bottom:.05rem}
.ov-ch{display:block;font-size:.68rem;font-weight:500;letter-spacing:.02em;color:var(--faint);margin-bottom:.1rem}
.ov-bd{padding:.5rem 0;min-width:0;position:relative}
.ov-ag{--rail:color-mix(in srgb,var(--agent) 45%,var(--rule))}
.ov-ag .ov-who{color:var(--agent)}
.ov-in{margin-top:1.4rem;--rail:var(--you)}
.ov-in .ov-who{color:var(--you)}
.ov-in .ov-bd{font-size:1rem;line-height:1.55;max-width:72ch;overflow-wrap:anywhere;white-space:pre-wrap}
.ov-day{margin-top:2.2rem}
.ov-day .ov-gut{border-right-color:transparent}
.ov-day .ov-bd{display:flex;align-items:center;gap:.8rem;padding:.1rem 0;color:var(--dim);font:600 .82rem var(--sans)}
.ov-day .ov-bd::after{content:"";flex:1;border-top:1px solid var(--rule)}
.ov-empty{margin:24px 0;color:var(--dim);font-size:.9rem}

/* the agent's prose */
.ov-think{color:var(--dim);font-size:.85rem;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;max-width:76ch;margin:.05rem 0 .4rem;
  padding-left:.7rem;border-left:1px solid var(--rule2);font-style:italic}
.ov-reply{font:1.06rem/1.62 var(--serif);max-width:68ch;overflow-wrap:anywhere;margin:.1rem 0;color:var(--ink)}
.ov-md>:first-child{margin-top:0}.ov-md>:last-child{margin-bottom:0}
.ov-md p{margin:.5em 0}.ov-md ul,.ov-md ol{margin:.45em 0;padding-left:1.4em}.ov-md li{margin:.2em 0}
.ov-md strong{font-weight:650}
.ov-md a{color:var(--iris)}
.ov-md code{font:.8em var(--mono);background:var(--panel);border:1px solid var(--rule);padding:.05em .32em;border-radius:3px}
.ov-md pre{position:relative;font:12.5px/1.55 var(--mono);background:var(--well);color:var(--w-term);padding:.6rem .8rem;border-radius:4px;
  white-space:pre-wrap;overflow-wrap:anywhere;margin:.6em 0}
.ov-md pre code{font:inherit;background:none;border:0;padding:0;color:inherit}

/* tool wells: dark terminal blocks with the command, its output, exit code and duration */
.ov-term{position:relative;background:var(--well);border-radius:4px;padding:.55rem .8rem;margin:.35rem 0 .5rem;max-width:52rem;
  font:12.5px/1.55 var(--mono);color:var(--w-term);overflow-wrap:anywhere;border-left:2px solid transparent}
.ov-term.failed{border-left-color:var(--w-bad)}
.ov-term pre{margin:0;font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}
.ov-cmd{color:var(--w-ink)}
.ov-ps{color:var(--w-amber);user-select:none}
.ov-tool{color:var(--w-amber);font-weight:600}
.ov-out{margin-top:.45rem;padding-top:.45rem;border-top:1px dashed color-mix(in srgb,var(--w-dim) 35%,transparent)}
.ov-term dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:.05rem 1rem;margin:.2rem 0 0}
.ov-term dt{color:var(--w-dim)}.ov-term dd{margin:0;color:var(--w-ink);min-width:0}
.ov-meta{display:flex;flex-wrap:wrap;gap:.2rem 1.2rem;margin-top:.45rem;color:var(--w-dim);font:11.5px var(--sans);font-variant-numeric:tabular-nums}
.ov-meta .bad{color:var(--w-bad);font-weight:600}
.ov-tail .ov-bd::before{content:"";flex:none;width:8px;height:8px;border-radius:50%;background:currentColor;animation:ov-pulse 1.4s ease-in-out infinite}
@keyframes ov-pulse{50%{opacity:.3}}

/* turn tally: one tick per tool call, height = how long it ran, red = failed. The quiet "N tool calls" line. */
.ov-tally .ov-gut{padding-top:0;padding-bottom:0}
.ov-tally .ov-bd{display:flex;align-items:center;flex-wrap:wrap;gap:.35rem .9rem;padding:.05rem 0 .35rem}
.ov-tape{display:flex;align-items:flex-end;gap:2px;height:1rem;flex-wrap:wrap;max-width:min(100%,38rem)}
.ov-tick{display:block;width:5px;height:calc(.25rem + var(--d,.3) * .75rem);border-radius:1px;background:color-mix(in srgb,var(--amber) 70%,var(--ground))}
.ov-tick.fail{background:var(--bad)}
.ov-tick.go{background:var(--amber);animation:ov-pulse 1.2s ease-in-out infinite}
.ov-sum{color:var(--faint);font-size:.76rem;font-variant-numeric:tabular-nums}
.ov-sum .bad{color:var(--bad)}
.ov-tsep{color:var(--faint);font-size:.76rem;margin:0 -.45rem}
.ov-row.ov-f{display:none}
.ov-row.hit>.ov-bd{background:color-mix(in srgb,var(--amber) 14%,transparent)}

/* the live tail: the one moving line in the body */
.ov-tail .ov-bd{display:flex;align-items:center;gap:.55rem;color:var(--amber);font-size:.85rem;font-variant-numeric:tabular-nums;padding:.45rem 0}

/* ---- composer: a solid strip pinned to the bottom of the card, under the transcript column */
.ov-compose{flex:none;position:relative;z-index:2;border-top:1px solid var(--rule);background:var(--ground);
  padding:10px 0 calc(10px + env(safe-area-inset-bottom))}
.ov-cin{max-width:var(--w);margin:0 auto;padding:0 var(--edge) 0 calc(var(--edge) + var(--gut) + 1rem);display:grid;grid-template-columns:minmax(0,1fr);gap:8px}
.ov-queue{display:grid;gap:4px;margin:0;padding:0;list-style:none}
.ov-queue li{display:flex;align-items:center;gap:10px;min-width:0;padding:4px 4px 4px 10px;border:1px dashed var(--rule2);border-radius:8px;font-size:.86rem}
.ov-qt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ov-qs{flex:none;color:var(--amber);font-size:.76rem}.ov-qs.ok{color:var(--agent)}
.ov-qr{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none}
.ov-qr::-webkit-scrollbar{display:none}
.ov-qr button{flex:none;font:.8rem var(--sans);min-height:32px;background:var(--panel);border:1px solid var(--rule2);border-radius:99px;padding:0 12px;
  color:var(--ink);cursor:pointer;white-space:nowrap}
.ov-qr button:disabled{opacity:.5;cursor:not-allowed}
.ov-box{display:flex;align-items:flex-end;gap:6px;min-width:0;background:var(--ground);border:1px solid var(--rule2);border-radius:10px;padding:4px;
  transition:border-color var(--q) var(--ease)}
.ov-box:focus-within{border-color:color-mix(in srgb,var(--iris) 60%,var(--rule2))}
.ov-attach{flex:none;width:var(--ctl);height:var(--ctl);display:grid;place-items:center;background:none;border:0;border-radius:6px;color:var(--faint);cursor:pointer}
.ov-attach:hover:not(:disabled){color:var(--ink);background:var(--panel)}
.ov-attach:disabled{cursor:default}
.ov-msg{flex:1;min-width:0;resize:none;display:block;margin:0;height:var(--ctl);min-height:var(--ctl);max-height:40vh;overflow-y:auto;box-sizing:border-box;
  background:transparent;color:var(--ink);border:0;outline:0;padding:calc((var(--ctl) - 1.45rem) / 2) 4px;font:1rem/1.45 var(--sans)}
.ov-msg::placeholder{color:var(--faint);opacity:1}
.ov-msg:disabled{opacity:.6}
.ov-acts{display:flex;gap:6px;flex:none;position:relative}
.ov-cb{height:var(--ctl);min-width:var(--ctl);border-radius:7px;padding:0 .95rem;font:600 .84rem var(--sans);cursor:pointer;border:1px solid transparent;white-space:nowrap}
.ov-cb:active:not(:disabled){transform:scale(.97)}
.ov-cb:disabled{opacity:.5;cursor:default}
.ov-send{background:var(--iris);color:var(--onbrand)}
.ov-send:hover:not(:disabled){background:color-mix(in srgb,var(--iris) 85%,var(--ink))}
.ov-stop{background:transparent;color:var(--bad);border-color:color-mix(in srgb,var(--bad) 50%,transparent)}
.ov-stop:hover:not(:disabled){background:color-mix(in srgb,var(--bad) 10%,transparent)}
.ov-force{background:color-mix(in srgb,var(--bad) 14%,transparent);color:var(--bad);border-color:color-mix(in srgb,var(--bad) 60%,transparent)}
.ov-force:hover:not(:disabled){background:color-mix(in srgb,var(--bad) 22%,transparent)}
/* the phone's secondary send control (Stop, Interrupt, quick replies); desktop shows those as buttons and chips */
.ov-acts > .ad-menu-w{display:none}
.ov-cmore{width:var(--ctl);padding:0;background:transparent;color:var(--dim);border-color:var(--rule2);letter-spacing:.08em}
.ov-acts .ad-menu{top:auto;bottom:calc(100% + 8px)}
.ov-cnotes{margin:0;min-height:1.1em;padding:0 .2rem;color:var(--dim);font-size:.78rem}
.ov-cnotes.ov-err{color:var(--bad)}

/* ---- compact view: under 720px of VIEW width (phone, quick view, narrow window). The gutter folds inline, the
   header is one row (back, name, status dot, ···) over one truncated line (harness, host, session, live state). */
@container ov (max-width:720px){
  .ov-bar{flex-wrap:wrap;--edge:16px;padding:4px 12px 6px 16px;row-gap:0;column-gap:8px}
  .ov-idn{flex:1 1 0}
  .ov-nm{font-size:1.15rem}
  .ov-idn .ad-st{font-size:0;gap:0;padding:0 4px;background:none}
  .ov-sub{order:5;flex-basis:100%;display:flex;align-items:baseline;gap:.35rem;min-width:0;overflow:hidden;white-space:nowrap;
    font-size:.8rem;color:var(--dim)}
  .ov-sub > *{flex:none}
  .ov-sub > .ov-state{flex:0 1 auto}
  .ov-sub > * + *::before{content:'· ';color:var(--faint)}
  .ov-hz,.ov-state{font-size:.8rem}
  .ov-sesswrap{display:none!important}
  .ov-sessname{display:inline;font:inherit;color:var(--ink)}
  .ov-grow,.ov-tools{display:none}
  .ov .ad-menu .ov-c{display:flex}
  .ov .ad-menu .ad-lbl.ov-c,.ov .ad-menu .ad-note.ov-c,.ov .ad-menu hr.ov-c{display:block}
  .ov-search{padding:8px 16px 10px}
  .ov-qnow,.ov-note{padding:8px 16px}
  .ov .log{padding-bottom:16px}
  .ov-lw{padding:0 16px}
  .ov-row{display:block;border-left:2px solid var(--rail,var(--rule2));padding-left:.7rem}
  .ov-gut{border:0;text-align:left;padding:.55rem 0 0;display:flex;gap:.6rem;align-items:baseline}
  .ov-who,.ov-ch{display:inline;margin:0}.ov-ch::before{content:'· '}
  .ov-bd{padding-top:.15rem}
  .ov-day{border-left-color:transparent;padding-left:0}.ov-day .ov-gut{display:none}
  .ov-tally{border-left-color:transparent}.ov-tally .ov-gut{display:none}
  .ov-tail{border-left-color:transparent}.ov-tail .ov-gut{display:none}
  .ov-earlier{display:block}
  .ov-term{margin-left:-.35rem;margin-right:-.35rem}
  .ov-compose{padding:8px 0 calc(8px + env(safe-area-inset-bottom))}
  .ov-cin{padding:0 12px}
}
/* Phones: 44px tap targets; iOS does not zoom a 16px field. The composer is ONE row: attach, text, one primary button
   (Send, or Queue while the agent works) and Stop / Interrupt behind a small secondary control. No hint line. */
@media (max-width:720px){
  .adir,.aqv{--ctl:44px}
  .ov-msg{font-size:16px}
  .ov-reply{font-size:16px}
  .ov-tgl{min-height:var(--ctl)}
  .ov-cnotes:not(.ov-err){display:none}
  .ov-qr,.ov-stop,.ov-force{display:none}
  .ov-acts > .ad-menu-w{display:flex}
  .ov-queue li{padding:0 4px 0 10px;font-size:.8rem}
}
@media (prefers-reduced-motion:reduce){:is(.adir,.aqv) *{animation:none!important;transition:none!important}}`;

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
.adir-detail.full > .ov{border-radius:inherit;overflow:hidden}
.ov > .adir-view{flex:1;min-height:0;position:relative;overflow:hidden}
.adir-note{margin:0;padding:16px 20px;color:var(--t2,inherit)}
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
  .adir .ov-back{display:inline-flex}
}
/* Phones: the card goes edge to edge (the host drops its page padding), no radius and no side borders. */
@media (max-width:720px){
  .adir-detail.full{height:calc(100dvh - 80px - env(safe-area-inset-bottom))}
  .adir[data-fill]{gap:0}
  .adir[data-fill] .adir-card{border-radius:0;border-left:0;border-right:0;box-shadow:none}
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

// A running clock as the mockup shows it ("thinking for 0:31"); an hour or more reads as dur().
export function mmss(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 3600 ? Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0') : dur(ms);
}

// A tool call's run time in its well: 0.3s, 27.4s, then dur() from a minute up.
export function secs(ms) {
  return ms < 60000 ? (Math.max(0, ms) / 1000).toFixed(1) + 's' : dur(ms);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// Line icons from the mockup, drawn with DOM calls (no markup strings).
const ICONS = {
  folder: [['path', {d: 'M2.5 3.5h4l1.5 1.5h5.5v7.5h-11ZM2.5 7h11'}]],
  search: [['circle', {cx: '7', cy: '7', r: '4.6'}], ['path', {d: 'M10.4 10.4 14 14'}]],
  attach: [['path', {d: 'M10.5 4.5 5.4 9.6a1.4 1.4 0 0 0 2 2l5.3-5.3a2.8 2.8 0 0 0-4-4L3.4 7.6a4.2 4.2 0 0 0 6 6l4.1-4.1'}]],
};
function icon(name) {
  const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('class', 'ov-i'); svg.setAttribute('viewBox', '0 0 16 16'); svg.setAttribute('aria-hidden', 'true');
  for (const [tag, attrs] of ICONS[name]) {
    const n = document.createElementNS(ns, tag);
    for (const k of Object.keys(attrs)) n.setAttribute(k, attrs[k]);
    svg.append(n);
  }
  return svg;
}
function iconButton(cls, name, label) {
  const b = el('button', cls);
  b.type = 'button'; b.setAttribute('aria-label', label); b.title = label;
  b.append(icon(name));
  return b;
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

// The composer's buttons. Idle, or with no way to interrupt: one Send. Working with an interrupt route: Stop,
// Interrupt and Queue (Send becomes Queue: the message waits here for the turn to end).
export function sendMode(working, canInterrupt, name) {
  const split = !!(working && canInterrupt);
  return {split, label: split ? 'Queue' : 'Send',
    hint: split ? (name || 'The agent') + ' is busy. Queue waits for this turn to end; Stop or Interrupt ends it now.' : ''};
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

// A menu button and its popover. Items are {label, note, bad, disabled, run} buttons, {heading} labels, {text} notes
// or 'hr' / {hr: cls}; any item may carry a cls. Opening moves focus into the menu; Escape, a pick or a press
// outside closes it and returns focus to the button.
function popMenu(button, items) {
  const wrap = el('div', 'ad-menu-w'), menu = el('div', 'ad-menu');
  menu.setAttribute('role', 'menu'); menu.hidden = true;
  button.type = 'button';
  button.setAttribute('aria-haspopup', 'menu'); button.setAttribute('aria-expanded', 'false');
  // Items a layout hides (compact-only rows on a wide view) have no box and take no focus.
  const live = () => [...menu.querySelectorAll('button')].filter((b) => !b.hidden && !b.disabled && b.getClientRects().length);
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
      if (it === 'hr' || it.hr) return el('hr', typeof it.hr === 'string' ? it.hr : null);
      if (it.heading) return el('div', 'ad-lbl' + (it.cls ? ' ' + it.cls : ''), it.heading);
      if (it.text != null) return el('p', 'ad-note' + (it.cls ? ' ' + it.cls : ''), it.text);
      const b = el('button', [it.bad ? 'bad' : '', it.cls || ''].join(' ').trim() || null, it.label);
      b.type = 'button'; b.setAttribute('role', 'menuitem');
      if (it.note) b.append(el('span', null, it.note));
      b.onclick = () => { close(true); it.run(); };
      if (it.hidden) b.hidden = true;
      if (it.disabled) b.disabled = true;
      return b;
    }));
    wrap.hidden = !list.some((it) => it !== 'hr' && !it.hr && !it.heading && !it.hidden);
    if (wrap.hidden) close(false);
  };
  set(items || []);
  wrap.append(button, menu);
  return {wrap, set, close};
}

// One composer for the detail page and the quick view, laid out as the mockup's: attach, the text box, then Send
// (idle) or Stop, Interrupt and Queue (while the agent works), and the queued messages above. send(text) resolves to a
// receipt (tracked until delivered), true (sent, nothing to track) or throws. interrupt(text) stops the current turn
// and sends; stop() only stops. Queue holds the message here, with Remove, and sends it when the turn ends.
function composer(opts) {
  const form = el('form', 'ov-compose'), cin = el('div', 'ov-cin'), queue = el('ul', 'ov-queue'), qr = el('div', 'ov-qr');
  const box = el('div', 'ov-box'), input = el('textarea', 'ov-msg'), acts = el('div', 'ov-acts');
  const attach = iconButton('ov-attach', 'attach', 'Attach images');
  const stop = el('button', 'ov-cb ov-stop', 'Stop'), force = el('button', 'ov-cb ov-force', 'Interrupt');
  const send = el('button', 'ov-cb ov-send', 'Send'), hint = el('p', 'ov-cnotes');
  const moreBtn = el('button', 'ov-cb ov-cmore', '···');
  moreBtn.setAttribute('aria-label', 'More send options'); moreBtn.title = 'More send options';
  const more = popMenu(moreBtn, []);
  // Image upload is not wired through the broker yet; the control stays where the mockup puts it.
  attach.disabled = true; attach.title = 'Attaching images is not available here yet';
  input.rows = 1; input.maxLength = opts.max || 4000;
  const long = 'Message ' + opts.name + (opts.slash ? ', or / for commands' : '');
  const phone = typeof matchMedia === 'function' ? matchMedia('(max-width:720px)') : null;
  // Phones get a placeholder short enough for one line beside the buttons.
  const placeholder = () => { input.placeholder = phone && phone.matches ? 'Message ' + opts.name : long; };
  placeholder();
  if (phone && phone.addEventListener) phone.addEventListener('change', placeholder, opts.signal ? {signal: opts.signal} : undefined);
  input.setAttribute('aria-label', 'Message ' + opts.name);
  input.setAttribute('enterkeyhint', 'send');
  stop.type = 'button'; force.type = 'button'; send.type = 'submit';
  stop.title = 'Stop ' + opts.name + "'s current turn";
  force.title = 'Interrupt ' + opts.name + "'s current turn now and send this message";
  acts.append(stop, force, send, more.wrap);
  box.append(attach, input, acts);
  qr.setAttribute('role', 'group'); qr.setAttribute('aria-label', 'Quick replies');
  queue.setAttribute('aria-label', 'Queued messages'); queue.setAttribute('aria-live', 'polite');
  hint.setAttribute('role', 'status');
  cin.append(queue, qr, box, hint);
  form.append(cin);
  let busy = false, canSend = false, replies = [], why = '', working = false, list = [], menuKey = '';
  const mine = new Map();   // id -> {text, state, li}
  const held = [];          // {text, li}: queued here until the turn ends
  function paint() {
    for (const b of [input, send, stop, force, ...qr.children]) b.disabled = !canSend || busy;
    const mode = sendMode(working && canSend, !!opts.interrupt, opts.name);
    stop.hidden = force.hidden = !mode.split;
    send.textContent = mode.label;
    send.title = mode.split ? opts.name + ' is busy: this waits in the queue and goes when the turn ends (Enter)'
      : 'Send (Enter). Shift+Enter for a new line';
    const items = [];
    if (mode.split) {
      items.push({label: 'Stop', note: 'ends the turn', bad: true, disabled: busy, run: doStop},
        {label: 'Interrupt', note: 'ends the turn, sends now', bad: true, disabled: busy, run: doForce});
    }
    if (list.length) {
      if (items.length) items.push('hr');
      items.push({heading: 'Quick replies'}, ...list.map((r) => ({label: r.label, disabled: !canSend || busy, run: () => submit(r.text)})));
    }
    const key = JSON.stringify(items.map((it) => [it.label, it.heading, it.disabled]));
    if (key !== menuKey) { menuKey = key; more.set(items); }
    hint.textContent = why || mode.hint || opts.hint || 'Enter sends. Shift+Enter for a new line.';
    hint.classList.toggle('ov-err', !!why);
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
      rec.li.lastChild.className = 'ov-qs' + (rec.state === 'delivered' ? ' ok' : '');
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
          rec.li = el('li'); rec.li.append(el('span', 'ov-qt', text), el('span', 'ov-qs'));
          queue.append(rec.li);
          while (queue.children.length > 3 + held.length) queue.firstChild.remove();
        }
        receipt(id, rec);
      }
      return true;
    } catch (err) { why = 'Could not send: ' + err.message; return false; }
    finally { busy = false; paint(); }
  }
  function hold(text) {
    const li = el('li'), state = el('span', 'ov-qs', 'Queued'), remove = el('button', 'ov-hb', 'Remove');
    remove.type = 'button'; remove.setAttribute('aria-label', 'Remove from the queue');
    li.append(el('span', 'ov-qt', text), state, remove);
    const item = {text, li, state};
    remove.onclick = () => { const i = held.indexOf(item); if (i >= 0) held.splice(i, 1); li.remove(); input.focus(); };
    held.push(item); queue.append(li);
  }
  // The turn ended: send what was queued, oldest first. A failure stays in the list with its reason.
  async function flush() {
    while (held.length && !working && canSend) {
      if (busy) { setTimeout(flush, 500); return; }
      const item = held[0];
      item.state.textContent = 'Sending';
      if (!(await submit(item.text))) { item.state.textContent = 'Not sent'; return; }
      if (held[0] === item) held.shift();
      item.li.remove();
    }
  }
  async function doStop() {
    if (!opts.stop || busy) return;
    busy = true; why = ''; paint();
    try { await opts.stop(); } catch (err) { why = 'Could not stop: ' + err.message; }
    finally { busy = false; paint(); }
  }
  async function doForce() {
    if (!input.value.trim()) return doStop();
    if (await submit(input.value, opts.interrupt)) { input.value = ''; grow(); input.focus(); }
  }
  async function sendNow() {
    if (!canSend || busy || !input.value.trim()) return;
    if (sendMode(working && canSend, !!opts.interrupt).split) { hold(input.value); input.value = ''; grow(); input.focus(); return; }
    if (await submit(input.value)) { input.value = ''; grow(); input.focus(); }
  }
  stop.onclick = doStop;
  force.onclick = doForce;
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
    update(ok, reason, quick, states, deliveryHint) {
      canSend = !!ok; why = ok ? why : reason || '';
      if (deliveryHint !== undefined) opts.hint = deliveryHint;
      list = quick || [];
      const labels = list.map((r) => r.label + '\n' + r.text).join('\n\n');
      if (labels !== replies) {
        replies = labels;
        qr.replaceChildren(...list.map((r) => {
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
      if (!working) flush();
    },
    // The agent is mid-turn: offer Stop, Interrupt and Queue. When the turn ends the queue goes out.
    working(flag) {
      if (working === !!flag) return;
      working = !!flag; paint();
      if (!working) flush();
    },
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

// The sidecar's say / stop routes for one session. Interrupt is stop, then say.
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
// {kind: user|agent|step, key, who, text, verb, code, ts} and groups consecutive steps into one turn tally.

function callInput(c) {
  let v = c.args != null ? c.args : c.arguments != null ? c.arguments : c.input != null ? c.input
    : c.function && c.function.arguments;
  if (typeof v === 'string') { try { v = JSON.parse(v); } catch (e) { v = {}; } }
  return v && typeof v === 'object' ? v : {};
}
const callName = (c) => String((c && (c.name || (c.function && c.function.name))) || 'tool');
const text0 = (v) => typeof v === 'string' && v.trim() ? v.trim() : '';

// The few short arguments a tool well lists (never file contents or patches).
const ARG_KEYS = ['path', 'file_path', 'notebook_path', 'pattern', 'glob', 'url', 'query', 'offset', 'limit', 'lines'];
export function stepArgs(input) {
  const i = input && typeof input === 'object' ? input : {};
  return ARG_KEYS.filter((k) => (typeof i[k] === 'string' && i[k]) || typeof i[k] === 'number').slice(0, 4)
    .map((k) => [k === 'file_path' || k === 'notebook_path' ? 'path' : k, String(i[k]).slice(0, 200)]);
}

// One hermes row as items: reasoning and tool calls become steps, content a reply. Tool results give none here
// (attachResults joins them to their calls). source is where a message came from (zulip, web), when the row says.
export function rowItems(row, agentName) {
  if (!row || typeof row !== 'object') return [];
  const ts = Number.isFinite(row.ts) ? row.ts * 1000 : null, key = 'r' + row.id;
  const images = Array.isArray(row.images) ? row.images.length : 0;
  const source = text0(row.source) || text0(row.channel) || text0(row.platform);
  if (row.role === 'user') {
    const text = row.content || (images ? '[image]' : '');
    return text ? [{kind: 'user', key, id: row.id, who: String(row.sender || 'you'), text: String(text), ts, source}] : [];
  }
  if (row.role !== 'assistant') return [];
  const out = [];
  if (row.reasoning) out.push({kind: 'step', key: key + 't', verb: 'Thought', text: String(row.reasoning), ts});
  if (row.content) out.push({kind: 'agent', key, id: row.id, who: agentName || 'agent', text: String(row.content), ts, source});
  (Array.isArray(row.tool_calls) ? row.tool_calls : []).forEach((c, i) => {
    if (!c) return;
    const name = callName(c), input = callInput(c);
    const [verb, code] = toolLine(name, input);
    out.push({kind: 'step', key: key + 'c' + i, verb, code, ts, rowId: row.id, callId: c.id != null ? String(c.id) : null,
      tool: name, cmd: typeof input.command === 'string' ? input.command.slice(0, 2000) : '', args: stepArgs(input)});
  });
  return out;
}

// What a tool result row says: its output (clipped), exit code and whether it failed.
function clip(s) {
  let t = String(s).replace(/\s+$/, '');
  const lines = t.split('\n');
  if (lines.length > 20) t = lines.slice(0, 20).join('\n') + '\n…';
  return t.length > 1500 ? t.slice(0, 1500) + '…' : t;
}
export function toolResult(row) {
  let v = row && row.content, out = '', exit = null, failed = row && row.is_error === true;
  if (typeof v === 'string' && /^\s*\{/.test(v)) { try { v = JSON.parse(v); } catch (e) { /* plain text */ } }
  if (Array.isArray(v)) out = v.map((x) => x && typeof x === 'object' ? String(x.text || '') : String(x)).join('\n');
  else if (v && typeof v === 'object') {
    const o = v.output != null ? v.output : v.stdout != null ? v.stdout : v.result != null ? v.result : v.content;
    out = o == null ? '' : typeof o === 'string' ? o : JSON.stringify(o);
    const code = v.exit_code != null ? v.exit_code : v.returncode != null ? v.returncode : v.exitCode;
    exit = Number.isInteger(code) ? code : null;
    if (typeof v.error === 'string' && v.error) { out = out ? out + '\n' + v.error : v.error; failed = true; }
    if (v.success === false) failed = true;
  } else out = v == null ? '' : String(v);
  return {out: clip(out), exit, failed: failed || !!exit};
}

// Join tool result rows to their calls: by tool_call_id when both carry one, else the next unused result after the
// call's row. Adds out, exit, failed and ms (call to result) to each step.
export function attachResults(items, toolRows) {
  const byCall = new Map(), loose = [];
  for (const r of [...(toolRows || [])].sort((a, b) => a.id - b.id)) {
    if (r.tool_call_id != null) byCall.set(String(r.tool_call_id), r); else loose.push(r);
  }
  let next = 0;
  for (const it of items || []) {
    if (!it || it.kind !== 'step' || it.verb === 'Thought') continue;
    let r = it.callId != null ? byCall.get(it.callId) : null;
    if (!r && loose.length) {
      while (next < loose.length && !(loose[next].id > it.rowId)) next++;
      r = loose[next];
      if (r) next++;
    }
    if (!r) continue;
    Object.assign(it, toolResult(r), {ms: Number.isFinite(r.ts) && Number.isFinite(it.ts) ? Math.max(0, Math.round(r.ts * 1000 - it.ts)) : null});
  }
  return items;
}

// "5 tool calls, 38s, 1 failed": how many tools a run of steps called, how long it took, how many failed. A run of
// thinking alone is "Thinking". start and end, when known, are the messages around the run (the ask and the reply),
// so the time is the turn's, not only the gap between its first and last step. "Show N steps" follows it.
export function stepsParts(steps, start, end) {
  const list = steps || [];
  const times = [start, ...list.map((s) => s.ts), end].filter(Number.isFinite);
  return {calls: list.filter((s) => s.verb !== 'Thought').length, failed: list.filter((s) => s.failed).length,
    span: times.length > 1 ? Math.max(...times) - Math.min(...times) : 0};
}
function partsText(p, withFailed) {
  return [p.calls ? p.calls + (p.calls === 1 ? ' tool call' : ' tool calls') : 'Thinking', p.span >= 1000 ? dur(p.span) : '',
    withFailed && p.failed ? p.failed + ' failed' : ''].filter(Boolean).join(', ');
}
export function stepsSummary(steps, start, end) {
  return partsText(stepsParts(steps, start, end), true);
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
    if (g.type !== 'steps') return;
    g.parts = stepsParts(g.steps, out[i - 1] && out[i - 1].ts, out[i + 1] && out[i + 1].ts);
    g.summary = partsText(g.parts, true);
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

// The header's live state: "thinking for 0:31" (or what runs) while working, "idle, last step 10:46" between turns.
export function liveState(work, since, lastTs, now = Date.now()) {
  if (work) return (work.what || 'thinking') + (since ? ' for ' + mmss(now - since) : '');
  return 'idle' + (Number.isFinite(lastTs) ? ', last step ' + clock(lastTs) : '');
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

// Day separators: "Today", "Yesterday", then the date.
export function dayLabel(ms, now = Date.now()) {
  const d = new Date(ms), t = new Date(now);
  const start = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((start(t) - start(d)) / 864e5);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  return d.toLocaleDateString([], {weekday: 'short', month: 'short', day: 'numeric',
    ...(d.getFullYear() === t.getFullYear() ? {} : {year: 'numeric'})});
}

// ---- transcript rows, as the mockup draws them

function gutter(who, source, ts) {
  const g = el('div', 'ov-gut');
  if (who) g.append(el('span', 'ov-who', who));
  if (source) g.append(el('span', 'ov-ch', source));
  if (Number.isFinite(ts)) g.append(clock(ts));
  return g;
}

function rowEl(cls, gut, body) {
  const r = el('div', 'ov-row ' + cls);
  r.append(gut || el('div', 'ov-gut'), body);
  return r;
}

function messageRow(g) {
  const user = g.kind === 'user', bd = el('div', 'ov-bd');
  if (user) bd.textContent = g.text;
  else {
    const reply = el('div', 'ov-reply ov-md');
    reply.append(...mdNodes(g.text));
    bd.append(reply);
  }
  const r = rowEl(user ? 'ov-in' : 'ov-ag', gutter(g.who, g.source, g.ts), bd);
  if (g.id != null) r.dataset.mid = String(g.id);
  return r;
}

// One step in its expanded form: thinking as a quiet aside, a tool call as a dark well with its command (or name and
// arguments), output, exit code and run time.
function stepNode(s) {
  if (s.verb === 'Thought') return el('div', 'ov-think', s.text || '');
  const t = el('div', 'ov-term' + (s.failed ? ' failed' : ''));
  if (s.cmd) {
    const pre = el('pre', 'ov-cmd');
    pre.append(el('span', 'ov-ps', '$ '), s.cmd);
    t.append(pre);
  } else {
    t.append(el('div', 'ov-tool', s.tool || s.verb));
    const args = s.args && s.args.length ? s.args : s.code ? [['', s.code]] : [];
    if (args.length) {
      const dl = el('dl');
      for (const [k, v] of args) dl.append(el('dt', null, k), el('dd', null, v));
      t.append(dl);
    }
  }
  if (s.out) {
    const out = el('div', 'ov-out');
    out.append(el('pre', null, s.out));
    t.append(out);
  }
  const meta = el('div', 'ov-meta');
  if (s.exit != null) meta.append(el('span', s.exit ? 'bad' : null, 'exit ' + s.exit));
  else if (s.failed) meta.append(el('span', 'bad', 'failed'));
  if (s.ms != null) meta.append(el('span', null, secs(s.ms)));
  if (meta.children.length) t.append(meta);
  return t;
}

// A turn's steps: the tally row (ticks, "N tool calls, Ns, K failed", Show N steps), then the steps row, folded
// unless open. Toggling flips the row in place; openKeys keeps the choice across repaints.
function stepsRows(g, prev, openKeys, allOpen, o, last) {
  let open = allOpen || openKeys.has(g.key);
  const n = g.steps.length, bd = el('div', 'ov-bd'), tally = rowEl('ov-tally', null, bd);
  tally.style.setProperty('--rail', prev && prev.kind === 'user' ? 'var(--you)' : 'color-mix(in srgb,var(--agent) 45%,var(--rule))');
  const calls = g.steps.filter((s) => s.verb !== 'Thought');
  if (calls.length) {
    const tape = el('div', 'ov-tape'), longest = Math.max(1, ...calls.map((s) => s.ms || 0));
    tape.setAttribute('aria-hidden', 'true');
    calls.forEach((s, j) => {
      const going = o.working && last && j === calls.length - 1 && s.ms == null;
      const tick = el('i', 'ov-tick' + (s.failed ? ' fail' : going ? ' go' : ''));
      tick.style.setProperty('--d', String(s.ms != null ? Math.max(.1, s.ms / longest) : .3));
      tick.title = [s.verb, s.code, s.ms != null ? secs(s.ms) : ''].filter(Boolean).join(' · ');
      tape.append(tick);
    });
    bd.append(tape);
  }
  const sum = el('span', 'ov-sum', partsText(g.parts, false));
  if (g.parts.failed) sum.append(', ', el('span', 'bad', g.parts.failed + ' failed'));
  const sep = el('span', 'ov-tsep', '·'), toggle = el('button', 'ov-tgl');
  sep.setAttribute('aria-hidden', 'true'); toggle.type = 'button';
  bd.append(sum, sep, toggle);
  const body = el('div', 'ov-bd');
  for (const s of g.steps) body.append(stepNode(s));
  const steps = rowEl('ov-ag ov-stepsrow', gutter(o.name, null, g.steps[0] && g.steps[0].ts), body);
  const paint = () => {
    steps.classList.toggle('ov-f', !open);
    toggle.textContent = open ? 'Hide steps' : 'Show ' + n + (n === 1 ? ' step' : ' steps');
    toggle.setAttribute('aria-expanded', String(open));
  };
  toggle.onclick = () => { open = !open; if (open) openKeys.add(g.key); else openKeys.delete(g.key); paint(); };
  paint();
  return [tally, steps];
}

// Grouped items as transcript rows, with a day separator wherever the day changes. Steps start folded unless
// allOpen or openKeys says otherwise. o: {name, working}.
function turnNodes(groups, openKeys, allOpen, o = {}) {
  const out = [], now = Date.now();
  let day = null, prev = null;
  groups.forEach((g, i) => {
    const ts = g.type === 'steps' ? g.steps[0] && g.steps[0].ts : g.ts;
    if (Number.isFinite(ts)) {
      const label = dayLabel(ts, now);
      if (label !== day) { day = label; out.push(rowEl('ov-day', null, el('div', 'ov-bd', label))); }
    }
    if (g.type === 'steps') out.push(...stepsRows(g, prev, openKeys, allOpen, o, i === groups.length - 1));
    else out.push(messageRow(g));
    prev = g;
  });
  return out;
}

// A hermes session, live: rows from the sidecar's event stream (plus one backfill) painted into `log` as transcript
// rows, with the live tail line at the end and Load earlier at the top. limit keeps only the newest rows (the quick
// view) until Load earlier asks for more.
function liveView(log, o) {
  let rows = [], es = null, retry = null, tick = null, work = null, workSince = null, first = true, stopped = false;
  let follow = true, allOpen = false, more = false, max = o.limit || 300;
  const page = o.limit || 100, tools = new Map(), openKeys = new Set(), controller = new AbortController();
  const lw = el('div', 'ov-lw'), earlier = el('div', 'ov-earlier'), loadBtn = el('button', 'ov-tgl', 'Load earlier');
  const tailText = el('div', 'ov-bd'), tail = rowEl('ov-tail', null, tailText);
  loadBtn.type = 'button'; earlier.append(loadBtn);
  tail.setAttribute('role', 'status');
  log.replaceChildren(lw);
  const lastTs = () => {
    for (let i = rows.length - 1; i >= 0; i--) if (Number.isFinite(rows[i].ts)) return rows[i].ts * 1000;
    return null;
  };
  function paintWork() {
    const since = work && (work.since || workSince);
    const text = liveState(work, since, lastTs());
    tail.hidden = !work;
    if (work) tailText.textContent = text;
    if (o.onClock) o.onClock(text, !!work);
  }
  function keepTools(list) {
    for (const r of list || []) if (r && r.role === 'tool' && Number.isFinite(r.id)) tools.set(r.id, r);
    while (tools.size > 2 * max) tools.delete(tools.keys().next().value);
  }
  function paint() {
    const items = attachResults(rows.flatMap((r) => rowItems(r, o.name)), tools.values());
    const nodes = turnNodes(groupTurns(items), openKeys, allOpen, {name: o.name, working: !!work});
    if (!nodes.length && !work) nodes.push(el('p', 'ov-empty', o.empty || 'No messages in this session yet.'));
    earlier.hidden = !more;
    paintWork();
    lw.replaceChildren(earlier, ...nodes, tail);
    if (follow && o.scroll) log.scrollTop = log.scrollHeight;
    if (o.onPaint) o.onPaint();
  }
  function onFrame(p) {
    if (Array.isArray(p.rows) && p.rows.length) {
      keepTools(p.rows);
      rows = tailRows(rows, p.rows, max);
      if (first && rows.length >= max) more = true;
    }
    const next = workState(p);
    if (!!next !== !!work) { workSince = next ? Date.now() : null; o.onWork(next); }
    work = next;
    clearInterval(tick); tick = work ? setInterval(paintWork, 1000) : null;
    paint();
  }
  async function backfill(older) {
    const want = Math.min(max, page), before = rows.length ? '&before=' + rows[0].id : '';
    try {
      const r = await viewFetch(o.api + 'earlier?limit=' + want + '&session=' + encodeURIComponent(o.session) + before,
        {signal: controller.signal});
      if (!r.ok || stopped) return;
      const got = (await r.json()).rows || [];
      keepTools(got);
      more = got.length >= want;
      const fromBottom = log.scrollHeight - log.scrollTop;
      rows = tailRows(rows, got, max); paint();
      if (older) log.scrollTop = log.scrollHeight - fromBottom;
      else if (o.seek) seek(o.seek);
    } catch (e) { /* the stream still shows new rows */ }
  }
  loadBtn.onclick = () => { follow = false; max += page; backfill(true); };
  function connect() {
    if (stopped) return;
    if (es) es.close();
    const current = es = new EventSource(o.api + 'events?session=' + encodeURIComponent(o.session), {withCredentials: true});
    current.onopen = () => o.onNote('');
    current.onmessage = (m) => {
      let p; try { p = JSON.parse(m.data); } catch (e) { return; }
      o.onNote('');
      if (p && typeof p === 'object') onFrame(p);
      if (first) { first = false; if (rows.length < max) backfill(false); }
    };
    current.onerror = () => {
      if (stopped) return;
      o.onNote('Reconnecting to ' + o.name + '…');
      // An HTTP error closes an EventSource for good; retry like the tail view does.
      if (current.readyState === EventSource.CLOSED && retry === null) retry = setTimeout(() => { retry = null; connect(); }, 2000);
    };
  }
  function seek(id) {
    const target = log.querySelector('[data-mid="' + String(id).replace(/\D/g, '') + '"]');
    if (target) { follow = false; target.scrollIntoView({block: 'center'}); }
  }
  if (o.scroll) {
    log.addEventListener('scroll', () => { follow = log.scrollTop + log.clientHeight >= log.scrollHeight - 40; }, {passive: true});
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

// The view's frame, shared by the Agents tab and the Fleet quick view: the one header row, the search row, the
// quick view's "Now" line, a note line, and the transcript (.log). o: {name, quick, back, onSteps(), onSession(id)}.
// On a compact view the header tools and the session list move into the ··· menu (the ov-c items).
function viewShell(o) {
  const ov = el('div', 'ov'), bar = el('header', 'ov-bar'), idn = el('div', 'ov-idn'), status = el('span');
  if (o.back != null) {
    const back = el('a', 'ov-back', '‹ Agents');
    back.href = o.back; back.setAttribute('aria-label', 'Back to agents');
    bar.append(back);
  }
  if (!o.quick) idn.append(el('h2', 'ov-nm', o.name));
  idn.append(status);
  const sub = el('div', 'ov-sub'), hz = el('span', 'ov-hz'), sessName = el('span', 'ov-sessname'), state = el('span', 'ov-state');
  const sessBtn = el('button', 'ov-sess'), sess = popMenu(sessBtn, []);
  sessBtn.title = 'Switch session'; sess.wrap.classList.add('ov-sesswrap', 'left');
  state.setAttribute('aria-live', 'off');
  sub.append(hz, sess.wrap, sessName, state);
  const stats = el('button', 'ov-hb ov-stats'), statsMenu = popMenu(stats, []);
  stats.title = 'Model and context';
  const files = popMenu(iconButton('ov-hb ov-ic', 'folder', 'Files and memory'),
    [{heading: 'Files and memory'}, {text: o.name + ' does not share its files or memory here yet.'}]);
  const find = iconButton('ov-hb ov-ic', 'search', 'Search this session');
  const steps = el('button', 'ov-hb ov-steps', 'Show steps');
  steps.type = 'button'; steps.setAttribute('aria-pressed', 'false'); steps.title = 'Show or hide the tool steps in every turn';
  steps.hidden = !o.onSteps;
  const tools = el('div', 'ov-tools');
  tools.append(statsMenu.wrap, files.wrap, find, steps);
  const moreBtn = el('button', 'ov-hb ov-ic', '···');
  moreBtn.setAttribute('aria-label', 'More actions'); moreBtn.title = 'More actions';
  const actions = popMenu(moreBtn, []);
  bar.append(idn, sub, el('span', 'ov-grow'), tools, actions.wrap);

  const search = el('form', 'ov-search'), q = el('input'), qn = el('span', 'ov-qn'), qx = el('button', 'ov-hb', 'Esc');
  search.setAttribute('role', 'search'); search.hidden = true; search.onsubmit = (e) => e.preventDefault();
  q.type = 'search'; q.autocomplete = 'off'; q.setAttribute('aria-label', 'Search this session');
  q.placeholder = 'Search everything ' + o.name + ' has said or run here';
  qx.type = 'button'; qx.setAttribute('aria-label', 'Close search');
  search.append(q, qn, qx);
  const qnow = el('p', 'ov-qnow'), note = el('p', 'ov-note'), log = el('div', 'log');
  qnow.hidden = true; note.hidden = true; note.setAttribute('role', 'status');
  log.tabIndex = 0; log.setAttribute('aria-label', o.name + ' conversation');
  ov.append(bar, search, qnow, note, log);

  let extra = [], sessions = [], session = null, ctxItems = [], stepsOn = false;
  function hits() {
    const t = q.value.trim().toLowerCase();
    let n = 0;
    for (const r of log.querySelectorAll('.ov-row.ov-in, .ov-row.ov-ag')) {
      const hit = !!t && r.textContent.toLowerCase().includes(t);
      r.classList.toggle('hit', hit); n += hit;
    }
    qn.textContent = t ? n + (n === 1 ? ' match' : ' matches') : '';
  }
  const openFind = () => { search.hidden = false; q.focus(); };
  const closeFind = () => { search.hidden = true; q.value = ''; hits(); };
  q.addEventListener('input', hits);
  q.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeFind(); find.focus(); } });
  find.onclick = () => { if (search.hidden) openFind(); else closeFind(); };
  qx.onclick = () => { closeFind(); find.focus(); };
  function toggleSteps() {
    stepsOn = !!o.onSteps();
    steps.setAttribute('aria-pressed', String(stepsOn)); steps.textContent = stepsOn ? 'Hide steps' : 'Show steps';
    paintActions();
  }
  steps.onclick = toggleSteps;
  const sessionItems = (cls) => sessions.map((s) => ({label: s.label, cls,
    note: s.id === session ? 'current' : s.ts ? ago(Date.now() / 1000 - s.ts) : '',
    run: () => { if (s.id !== session && o.onSession) o.onSession(s.id); }}));
  function paintActions() {
    const c = 'ov-c', items = [];
    if (sessions.length > 1) items.push({heading: 'Session', cls: c}, ...sessionItems(c), {hr: c});
    if (ctxItems.length) items.push(...ctxItems.map((it) => ({...it, cls: c})));
    items.push({label: 'Search', cls: c, run: openFind});
    if (o.onSteps) items.push({label: stepsOn ? 'Hide steps' : 'Show steps', cls: c, run: toggleSteps});
    if (extra.length) items.push({hr: c}, ...extra);
    actions.set(items);
  }
  paintActions();
  return {ov, log, note,
    setStatus(st, label) { status.replaceChildren(chip(st, label)); },
    // m: {detail, host, model, tokens, limit, session, sessions}
    setMeta(m) {
      hz.textContent = [m.detail, m.host].filter(Boolean).join(' · '); hz.hidden = !hz.textContent;
      sessions = m.sessions || []; session = m.session || null;
      const label = (sessions.find((s) => s.id === session) || {}).label || session || '';
      sessBtn.textContent = label;
      sess.set(sessions.length > 1 ? sessionItems(null) : []);
      sessName.textContent = label; sessName.hidden = !label;
      sessName.classList.toggle('ov-solo', sessions.length <= 1);
      const known = !!m.model || m.tokens != null;
      stats.replaceChildren(m.model || '');
      if (m.tokens != null) stats.append(m.model ? ', ' : '', el('b', null, kTok(m.tokens)), ' tokens');
      ctxItems = known ? [{heading: m.tokens != null ? kTok(m.tokens) + (m.limit ? ' of ' + kTok(m.limit) : '') + ' context' : 'Model'},
        {text: m.model || 'Model not reported.'}] : [];
      statsMenu.set(ctxItems);
      paintActions();
    },
    setClock(text, busy) { state.textContent = text || ''; state.hidden = !text; ov.classList.toggle('busy', !!busy); },
    // What the agent says it is doing: a line under the header in the quick view, the live state's tooltip in the tab.
    setNow(doing, waiting) {
      state.title = [doing, waiting].filter(Boolean).join(' ');
      if (!o.quick) return;
      qnow.hidden = !doing && !waiting;
      qnow.replaceChildren(el('b', null, 'Now'), doing + (doing && waiting ? ' ' : ''), el('span', 'w', waiting));
    },
    setNote(text) { note.textContent = text || ''; note.hidden = !text; },
    setActions(items) { extra = items || []; paintActions(); },
    refind: hits,
  };
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
  let shell = null, viewStatus = null, replies = [], session = null, working = false, hermesComp = false;
  let sessions = [], cfgInfo = {};
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

  function copyLink(name) {
    const url = location.origin + location.pathname + hrefOf(makeRoute(name, session));
    if (navigator.clipboard) navigator.clipboard.writeText(url).catch(() => {});
  }

  function updateHead(agent) {
    if (!shell) return;
    const info = describe(agent, summaryOf(agent.name));
    const [state, label] = viewStatus && !summaryOf(agent.name) ? viewStatus : [info.state, info.label];
    shell.setStatus(state, label);
    shell.setMeta({detail: info.detail, host: cfgInfo.host, model: info.model || cfgInfo.model, tokens: info.tokens,
      limit: info.limit, session: hermesComp ? session : null, sessions: hermesComp ? sessions : []});
    shell.setNow(info.doing, info.waiting);
    if (!hermesComp) shell.setClock(agent.age != null ? 'last seen ' + ago(agent.age) : '', false);
    if (!comp) return;
    if (hermesComp) {
      comp.update(ctx.canAct && agent.can_steer, ctx.canAct ? NO_MESSAGES(agent.name) : READ_ONLY, replies, [], '');
      comp.working(working);
    } else comp.update(ctx.canAct, READ_ONLY, replies, agent.steer_messages, steerHint(agent));
  }

  async function showAgent(agent) {
    shown = agent.name;
    comp = null; viewStatus = null; session = null; working = false; hermesComp = false; sessions = []; cfgInfo = {};
    const hermes = agent.can_view && !agent.can_tail;
    shell = viewShell({name: agent.name, back: standalone ? '#' : location.hash.split('/').slice(0, 2).join('/'),
      onSteps: hermes ? () => !!(view && view.toggleSteps && view.toggleSteps()) : null,
      onSession: (id) => navigate(makeRoute(agent.name, id))});
    shell.setActions([{label: 'Copy link', run: () => copyLink(agent.name)}]);
    detail.classList.add('full');
    detail.setAttribute('aria-label', agent.name);
    detail.replaceChildren(shell.ov);
    if (agent.can_steer_pull) {
      comp = composer({name: agent.name, send: steerSend(base, agent.name, ctx.signal), signal: ctx.signal,
        hint: steerHint(agent), sink: () => view && view.note});
    }
    if (agent.can_tail) {
      updateHead(agent);
      const host = el('div', 'adir-view');
      shell.log.hidden = true;
      shell.ov.insertBefore(host, shell.log);
      if (comp) shell.ov.append(comp.form);
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
      updateHead(agent);
      shell.log.append(el('p', 'adir-note', agent.kind === 'hermes'
        ? agent.name + ' has not registered a live view, so only its presence shows here.'
        : agent.name + ' does not share its transcript, so only its presence shows here.'));
      if (comp) shell.ov.append(comp.form);
      return;
    }
    // hermes with a view: transcript rows from the sidecar, and a say / stop / interrupt composer.
    const api = base + 'api/a/' + encodeURIComponent(agent.name) + '/';
    const acts = viewActions(api, () => session, ctx.signal);
    hermesComp = true;
    comp = composer({name: agent.name, max: 8000, slash: true, signal: ctx.signal,
      send: acts.say, interrupt: acts.interrupt, stop: acts.stop});
    shell.ov.append(comp.form);
    shell.setNote('Connecting to ' + agent.name + '…');
    updateHead(agent);
    try {
      const cfgRes = await viewFetch(api + 'cfg' + (route.session ? '?session=' + encodeURIComponent(route.session) : ''));
      const cfg = await cfgRes.json();
      if (!cfgRes.ok) throw new Error(cfg.error || 'HTTP ' + cfgRes.status);
      if (shown !== agent.name || stopped) return;
      session = route.session || (cfg.session && cfg.session !== '*' ? cfg.session : cfg.main) || null;
      sessions = sessionList(cfg);
      cfgInfo = {host: text0(cfg.host), model: text0(cfg.model)};
      if (!session) { shell.setNote(agent.name + ' has no session yet.'); return; }
      shell.setNote('');
      view = liveView(shell.log, {name: agent.name, api, session, scroll: true, seek: route.id,
        onNote: (text) => shell.setNote(text),
        onClock: (text, busy) => shell.setClock(text, busy),
        onPaint: () => shell.refind(),
        onWork: (w) => {
          working = !!w;
          viewStatus = w ? ['run', 'Working'] : ['idle', 'Idle'];
          const current = agents.find((a) => a.name === agent.name);
          if (current) updateHead(current);
          renderList();
        }});
      updateHead(agents.find((a) => a.name === agent.name) || agent);
    } catch (err) {
      if (shown !== agent.name || stopped) return;
      shell.setNote('Could not open the live view: ' + err.message);
    }
  }

  function showRoute() {
    wrap.classList.toggle('picked', !!route.name);
    const agent = agents.find((a) => a.name === route.name);
    if (!route.name) {
      shown = null; shell = null;
      detail.classList.remove('full');
      detail.replaceChildren(el('p', 'adir-note', 'Pick an agent to see it live.'));
      return;
    }
    if (!agent) {
      if (shown !== route.name) {
        shell = null; detail.classList.remove('full');
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
// and carries Full view and Close, so the view's header row carries status, harness, session and live state (the
// tools behind ···), then what it is doing and who it waits on, its newest turns in the same transcript rows as the
// tab, and the same composer. Claude Code and Codex lines come from the tail; hermes rows from its sidecar.
const QUICK_ROWS = 6;
const QUICK_CSS = BASE_CSS + `
/* The view fills the host sheet under its header, edge to edge: the sheet's .side padding is cancelled. */
.aqv{display:flex;flex-direction:column;height:min(640px,calc(100dvh - 104px));margin:-20px -24px -24px}
.aqv-sub{flex:none;margin:0;padding:8px 16px;font-size:13px;color:var(--t2)}
.aqv-sub:empty{display:none}
@media (max-width:720px){.aqv{height:calc(100dvh - 120px - env(safe-area-inset-bottom));margin:-16px}}`;

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
  let stopped = false, started = false, es = null, session = null, lines = [], running = false, agent = null,
    replies = [], comp = null, mode = null, feed = null, tailOpen = false, cfgInfo = {};
  const controller = new AbortController(), openKeys = new Set();
  const acts = viewActions(api, () => session, controller.signal);
  const shell = viewShell({name: agentName, quick: true,
    onSteps: () => {
      if (feed) return feed.toggleSteps();
      tailOpen = !tailOpen; openKeys.clear(); paintLines(); return tailOpen;
    }});
  const wrap = el('div', 'aqv');
  // Presence errors (the poller clears its own on every success) stay apart from stream notes.
  const error = el('p', 'aqv-sub');
  error.setAttribute('role', 'status');
  shell.ov.insertBefore(error, shell.log);
  wrap.append(shell.ov);
  el0.replaceChildren(el('style', null, QUICK_CSS), wrap);
  const lw = el('div', 'ov-lw');

  function paintSummary() {
    const info = describe(agent, summaryOf());
    // Without a host summary, the sidecar's own run state is the better signal than directory presence.
    const [state, label] = running && !summaryOf() ? ['run', 'Working'] : [info.state, info.label];
    shell.setStatus(state, label);
    shell.setMeta({detail: info.detail, host: cfgInfo.host, model: info.model || cfgInfo.model, tokens: info.tokens,
      limit: info.limit, session, sessions: []});
    shell.setNow(info.doing, info.waiting);
    if (mode !== 'view') shell.setClock(agent && agent.age != null ? 'last seen ' + ago(agent.age) : '', false);
  }

  function paintLines() {
    if (mode !== 'tail') return;
    const nodes = turnNodes(groupTurns(lines.map(lineItem)), openKeys, tailOpen, {name: agentName});
    if (!nodes.length) nodes.push(el('p', 'ov-empty', 'Nothing said yet.'));
    lw.replaceChildren(...nodes);
    shell.log.scrollTop = shell.log.scrollHeight;
    shell.refind();
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
    shell.log.replaceChildren(lw);
    let retry = null;
    const connect = () => {
      if (stopped) return;
      es = new EventSource(base + 'api/agents/' + encodeURIComponent(agentName) + '/transcript');
      es.onopen = () => { shell.setNote(''); };
      es.onmessage = (m) => {
        let r; try { r = JSON.parse(m.data); } catch (e) { return; }
        const more = transcriptLines(r, agentName);
        if (more.length) { lines = lines.concat(more).slice(-QUICK_ROWS); paintLines(); }
      };
      es.addEventListener('reset', () => { lines = []; paintLines(); });
      es.addEventListener('end', () => { es.close(); retry = setTimeout(connect, 2000); });
      es.onerror = () => {
        if (!stopped) shell.setNote('Reconnecting to ' + agentName + '…');
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
      cfgInfo = {host: text0(cfg.host), model: text0(cfg.model)};
      if (agent) paintSummary();
      if (!session) { shell.setNote(agentName + ' has no session yet.'); return; }
      feed = liveView(shell.log, {name: agentName, api, session, limit: QUICK_ROWS, scroll: true, empty: 'Nothing said yet.',
        onNote: (text) => shell.setNote(text),
        onClock: (text, busy) => shell.setClock(text, busy),
        onPaint: () => shell.refind(),
        onWork: (w) => { running = !!w; if (agent) paintSummary(); paintComposer(); }});
    } catch (err) {
      if (!stopped) shell.setNote('Live view unavailable: ' + err.message);
    }
  }

  const stopPoll = poller(ctx, (agents, quick) => {
    agent = agents.find((a) => a.name === agentName) || null;
    replies = quick;
    if (!agent) {
      shell.setStatus('off', 'Gone'); shell.setNote(agentName + ' is not registered.');
      return;
    }
    if (!started) {
      started = true;
      if (agent.can_tail) startTail();
      else if (agent.can_view) startView();
      else shell.setNote(agentName + ' does not share its transcript, so only its presence shows here.');
      if (agent.can_steer_pull && mode === 'tail') {
        comp = composer({name: agentName, send: steerSend(base, agentName, controller.signal), signal: controller.signal,
          hint: steerHint(agent)});
      } else if (mode === 'view') {
        comp = composer({name: agentName, max: 8000, slash: true, signal: controller.signal,
          send: acts.say, interrupt: acts.interrupt, stop: acts.stop});
      }
      if (comp) shell.ov.append(comp.form);
    }
    paintSummary();
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
