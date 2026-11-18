# Agent directory

Python 3.9+ and the source checkout are sufficient. No packages, broker credentials or
agent account required. The optional RPM `eph-broker-viewer` installs the same `viewer/`
package under `/opt/eph-broker`, with its own `VERSION` and no service-restart scriptlets.
Run the commands below from the checkout (or `/opt/eph-broker`).

## Ten-minute standalone setup

Create a private directory, a stable random enrollment key and a basic-auth credential file:

```sh
mkdir -m 700 agentdir-state
umask 077
python3 -c "import secrets; open('agentdir-state/enroll.key', 'wb').write(secrets.token_bytes(32))"
printf 'demo:replace-with-a-long-password\n' > agentdir-state/basic
export AGENTDIR_ENROLL_KEY_FILE="$PWD/agentdir-state/enroll.key"
export AGENTDIR_BASIC_AUTH_FILE="$PWD/agentdir-state/basic"
python3 -m viewer --standalone --port 8791 --state-dir agentdir-state
```

Open `http://127.0.0.1:8791/` and log in. In another terminal in the same directory:

```sh
export AGENTDIR_ENROLL_KEY_FILE="$PWD/agentdir-state/enroll.key"
python3 -m viewer.clients.agentdir mint --name demo --ttl 600 |
  python3 -m viewer.clients.agentdir --state agentdir-state/demo.json enroll --name demo
python3 -m viewer.clients.agentdir --state agentdir-state/demo.json register --kind generic --ttl 30
python3 -m viewer.clients.agentdir --state agentdir-state/demo.json status busy
python3 -m viewer.clients.agentdir --state agentdir-state/demo.json beat --every 10
```

Only the minting administrator needs the enrollment key. Give a remote agent its name-bound
one-time token, not the key. Enroll also accepts `--token`, but stdin avoids shell-history and
process-list exposure. The client stores the returned kid and key in the given 0600 state file.
Stop beats with Ctrl-C to deregister and revoke the kid. Abrupt termination instead greys the
entry after 30 seconds and removes it after 90. `beat --while-pid PID` or `--while-tmux SESSION`
exits and deregisters when the owner disappears. `status idle` sends a signed beat;
`deregister` explicitly removes the entry and revokes the kid. A signed
`POST /v1/deregister {"revoke": false}` removes the entry but keeps the kid, for agents that
restart with the same stored key (the Hermes plugin does this on shutdown). Re-enrollment needs a fresh token.
Temporary heartbeat transport errors and HTTP 408, 429 or 5xx responses are retried without
revoking the identity. Retries back off from one second (or the beat interval if shorter),
doubling up to the smaller of 30 seconds and the beat interval, and reset after success.
Other unexpected errors exit without deregistering, preserving credentials for a restart.

## In the broker dashboard (Agents tab)

Add the module to the broker's environment and restart the broker once:

```sh
BROKER_MODULES=agents
BROKER_MODULE_AGENTS_CMD='python3 -m viewer'
BROKER_MODULE_AGENTS_LABEL=Agents
BROKER_MODULE_AGENTS_GROUP=viewers
BROKER_MODULE_AGENTS_VERSION_FILE=/opt/eph-broker/viewer/VERSION
AGENTDIR_ENROLL_KEY_FILE=/var/lib/eph-broker/agentdir/enroll.key
AGENTDIR_STATE_DIR=/var/lib/eph-broker/agentdir
```

The key file must be a 0600 file holding at least 32 random bytes, readable by the broker's
user. The tab appears for SSO users in `viewers`. Agents enroll and beat against
`127.0.0.1:8790` exactly as in the standalone steps below. `/fleet-index` lists every
registered name, so a Fleet row with the same name gets a Quick view. Upgrading the
`eph-broker-viewer` package changes `viewer/VERSION`, which restarts only the module.
Without `AGENTDIR_STATE_DIR`, state lives in `<name>-state` beside the module socket.

The tab lists agents on the left and shows the picked one on the right (on a phone, the list
first, then the agent with a back link). The address bar follows the pick
(`#module/agents/NAME`, plus the session and a message id), so links to an agent, a session or
a message work. A `hermes` agent with a view shows its live view, below.

On the Fleet page, **Quick view** on a listed agent opens a small card: status, kind and the last
beat (with its local time). For an agent with a view it adds the last five transcript lines
streaming live, the turn's working state and a one-line composer: **Send** starts a turn, and
while a turn sent from a view is running, **Stop** ends it and **Steer** passes it the typed text.
The composer is disabled with a hint for viewers without the broker actions permission or agents
that did not register `steer`. Closing the card closes its stream, so a tab holds at most one.

## Claude Code and Codex transcript tail

Complete the standalone setup above, or enroll against the broker module's agent listener.
RPM users need both `eph-broker` and `eph-broker-viewer` for transcript tailing: the core
package supplies the pure shared renderer and redaction code. Presence-only viewer installs
remain independent. The source checkout includes everything, with no additional setup.
Then register the harness and start the tail in place of the separate `beat` process:

```sh
python3 -m viewer.clients.agentdir --state agentdir-state/demo.json register --kind claude-code --caps tail --ttl 30
python3 -m viewer.clients.agentdir --state agentdir-state/demo.json tail --transcript /path/to/session.jsonl
```

Open the Agents tab and pick `demo`. The same timeline and tool-card JavaScript used for broker
workers renders its transcript. This is read-only and requires no view sidecar or action access.
For a quick fake-agent smoke test, create a file, tail it, then append another line:

```sh
printf '%s\n' '{"type":"assistant","message":{"content":"Hello from the fake agent."}}' > demo.jsonl
python3 -m viewer.clients.agentdir --state agentdir-state/demo.json tail --transcript "$PWD/demo.jsonl"
# In another terminal:
printf '%s\n' '{"type":"assistant","message":{"content":"A live update."}}' >> demo.jsonl
```

For Claude Code, use the `transcript_path` from the SessionStart hook's JSON input as the
`--transcript` argument. Run the tail as a supervised background process owned by that session;
`--while-pid PID` or `--while-tmux SESSION` stops following when its owner disappears.
For Codex, register `--kind codex --caps tail` and supply the session rollout JSONL under
`$CODEX_HOME/sessions/` (or a Codex `--json` event file). There is no implicit session discovery.
Run one tail process per credential file; do not run `beat` concurrently against that file.
Ctrl-C stops tailing without revoking enrollment. Presence expires normally, or use `deregister`.

The client follows appends, partial lines, rename/create rotation and truncation. It initially
reads at most the last 8 MiB, discards incomplete/oversized source records and keeps the last
2000 projected lines for re-push (`--lines 1..10000`). The module's independent ring defaults
to 2000 lines (`AGENTDIR_TAIL_LINES=1..10000`). No transcript is persisted on the module host.
After module reload or TTL expiry the client re-registers and re-pushes its local ring, even
if the source file is idle. A stopped client backfills again when restarted. Rotation starts
a new transcript and resets the browser timeline. A bounded 4 KiB content anchor before the
read cursor also detects same-inode rewrites that regrow to the same or a larger size between
polls. A changed anchor resets the transcript, including partial records and command tracking.

`POST /v1/events` uses the same name-bound HMAC, timestamp and replay checks as register/beat.
The body is `{epoch, source, start, lines}`: a source identifier, zero-based starting sequence
and up to 100 JSONL strings. An absent/stale epoch returns `{ok:true, epoch, replay:true}`
without ingesting; resend the bounded local ring with that epoch and a fresh source identifier.
Accepted batches return `replay:false`; source/sequence suppress ambiguous retries. Gaps request
replay. Only registered `claude-code`/`codex` agents with `tail` may push. Bodies above 64 KiB
are refused (413); projected lines are limited to 24 KiB. The server allows four batches/second
per agent with a burst of eight, then returns 429. Clients send at most two/second and retry
transient errors without discarding unacknowledged lines or credentials. Events refresh presence.

Both client and server apply the broker's shared secret-shaped redaction. Codex messages,
reasoning summaries, command lines/output tails, file paths, plans and tool names use the
broker's projection; MCP arguments/results, encrypted reasoning and unknown rollout payloads
are not forwarded. Claude JSONL keeps its existing record shape. Oversized projected records
become size-limit notices. Redaction is best effort, not a substitute for access control.
Codex rollout command completions with a tracked function-call ID reuse that call's tool card
instead of adding a duplicate command card.
Use loopback or a trusted TLS reverse proxy for remote agents: HMAC authenticates, not encrypts.

The authenticated UI route `GET /api/agents/NAME/transcript` streams SSE. `Last-Event-ID`
resumes within the ring; stale epochs or evicted cursors emit `reset` and bounded replay.
There are one-second cursor keepalives and an `end` event on deregistration/expiry. Module
streams consume `BROKER_MODULE_AGENTS_MAX_STREAMS` slots and the host's stream deadline,
just like other module streams. All viewers allowed into the module may read every tail.

## Hermes agents (plugin)

`viewer/clients/hermes-plugin/agentdir/` is a stdlib Hermes plugin that keeps a gateway listed.
Copy that directory into `$HERMES_HOME/plugins/agentdir/`, mint a token for the agent's name and
give it to the gateway once, then enable the plugin:

```sh
python3 -m viewer.clients.agentdir mint --name books > /path/to/books.token   # on the directory host
```

```yaml
# $HERMES_HOME/config.yaml
plugins:
  enabled:
    - agentdir
```

| Setting | Meaning / default |
|---|---|
| `AGENTDIR_URL` | Directory agent API, default `http://127.0.0.1:8790` |
| `AGENTDIR_NAME` | Name to register (must match the token), default the host name |
| `AGENTDIR_ENROLL_TOKEN` or `AGENTDIR_ENROLL_TOKEN_FILE` | One-time token, read only when no key is stored |
| `AGENTDIR_TTL` | Seconds, default 30; beats go out every ttl/3 |
| `AGENTDIR_VIEW_URL` | Optional view sidecar URL; adds the `view` and `steer` caps. Viewers still need the broker actions permission to steer |

Each setting can also come from the plugin's config block (`url`, `name`, `ttl`, `view_url`,
`enroll_token_file`); the environment wins. On first start the plugin enrolls and stores its kid
and key in `$HERMES_HOME/agentdir.key` (0600); later starts reuse it, so the token can be deleted.
`pre_llm_call` reports busy and `post_llm_call` idle, each with an immediate beat. Unload or exit
sends `deregister {"revoke": false}`, so the entry leaves the list and the key stays valid.
Network errors never reach the gateway: it retries with backoff and logs once per change. A refused
key (revoked, wrong directory) logs one error and stops; remove the key file and give it a new token.
Check it without Hermes: `python3 viewer/clients/hermes-plugin/test_agentdir_plugin.py`.

## Listeners and authentication

* Agent API: `AGENTDIR_AGENT_BIND`, default `127.0.0.1:8790`. Only POST `/v1/enroll`,
  `/v1/register`, `/v1/beat`, `/v1/deregister` exist. Enrollment authenticates the signed
  one-use token; all other requests require per-agent HMAC. `--server URL` or `AGENTDIR_SERVER`
  changes the client target. Never expose plaintext credentials over a network: use a TLS proxy.
  Connections have a 10-second socket idle timeout, including request lines, headers, bodies and
  keep-alive waits. `AGENTDIR_AGENT_MAX_CONNECTIONS` caps simultaneous connections (default 64,
  positive integer). Excess connections close immediately without an HTTP response; clients may retry
  after a slot frees. Invalid limits log an error and fall back to 64. Restart to change the limit.
  These limits do not change the UI listeners.
* UI module: under the broker [module host](modules.md), the child reads `BROKER_MODULE_SOCKET`
  and trusts the `X-Viewer-User` header the broker injects after its SSO group check. Other hosts
  pass `--socket PATH` (or `AGENTDIR_UI_SOCKET`) and must strip client identity headers and inject
  their own; `AGENTDIR_IDENTITY_HEADER` names that header (default `X-Viewer-User` in socket mode).
  The socket is mode 0600; put it in a private parent directory shared only with the host user.
  Socket peers are the trust boundary, not arbitrary incoming HTTP clients. The server never
  unlinks an existing socket path itself; the broker module host removes a stale one before launch.
* Standalone: loopback UI on `--port` (8791 by default), requiring either a 0600
  `AGENTDIR_BASIC_AUTH_FILE` containing `user:password`, or `AGENTDIR_TRUSTED_PROXIES`
  (comma-separated CIDRs) plus the forward-auth identity header. A trusted proxy must strip
  spoofed headers and authenticate users. Nontrusted peers cannot use forward-auth headers.
  Use TLS for remote browser access. `/healthz` is public and contains only `{"ok":true}`.

UI routes are `/healthz`, `/ui.js`, `/fleet-index`, `/api/agents`, and `/api/agents/NAME/events` (SSE presence
snapshots every five seconds, ending with null on removal). `ui.js` exports `mount(el, ctx)` and
`quickView(el, name, ctx)`; `ctx.baseUrl` optionally prefixes routes, and `ctx.signal` or the
returned function unmounts it. All agent text
is rendered with textContent. Hermes agents with a view add the routes in
[Hermes agent view](#hermes-agent-view).
The host can label this module's mount point **Agents**. This package does not change the
broker dashboard or install a second service; a module supervisor can watch `viewer/VERSION`.

## State, protocol and adapters

`AGENTDIR_STATE_DIR` (or `--state-dir`, default `agentdir-state` standalone) holds only the
server's 0600 `revoked.json` and `used-tokens.json` (used token ids and their expiry, never
keys). Keep the enrollment key stable across restarts. The directory and replay timestamps
are in memory. After restart agents must register again; the CLI saves its registration and
automatically re-registers on the next beat when the server reports a missing entry. A used
token stays refused across restarts until it expires; deregistered kids remain revoked.
Request replay protection resets on restart, so a captured signed request could be replayed
once within the 60 second skew window right after a restart; it can only repeat a beat,
register or deregister that the agent already sent.
Use short token lifetimes and do not recycle tokens. Back up the revocation file with the key.
Run one directory process per state dir: two sharing one would overwrite each other's revocations.
Anyone who can read the enrollment key can mint tokens for any name, so keep it readable only by
the directory's own account, and do not run untrusted code (including broker workers) as that account.

Enrollment token: base64url(JSON `{name,jti,exp}`), dot, hex HMAC-SHA256 of
`enroll|BASE64_PAYLOAD` under the enrollment key. `exp` is Unix seconds, default lifetime
600 seconds. The kid encodes `[name,jti]`. The agent key is HMAC-SHA256(server key,
`name|jti`), returned once as unpadded base64url with the kid. The server does not store agent keys.
Signing headers: `X-Agentdir-Kid`, `X-Agentdir-Ts` (Unix seconds, fractional seconds supported),
`X-Agentdir-Sig` (hex HMAC-SHA256 of `method|path|ts|sha256(body)` using the agent key).
Skew must be within 60 seconds; timestamps must strictly increase. The CLI uses microsecond
precision so consecutive commands work within one second. JSON bodies are limited to 16 KiB.
Register includes `{name,kind,caps,ttl,view_url?}`; TTL is 3..86400 seconds. Allowed kinds are
`hermes`, `claude-code`, `codex`, `generic`; caps are `view`, `steer`, `tail`. A name owned by
another live/grey kid cannot be replaced. Beat optionally includes `{status:"busy"|"idle"}`.

`AGENTDIR_VIEW_ALLOW` defaults empty (no view URLs accepted). Comma-separated rules are
CIDRs with optional port or port range, for example `192.0.2.0/24:8080,2001:db8::/32:443-444`.
Every resolved IP must match at registration and again at connection. Only HTTP(S) URLs
without userinfo or fragments are accepted. DNS waits are limited to five seconds and run
outside the directory lock, so a slow hostname cannot stall heartbeats or UI snapshots.
At most four DNS lookups per allowlist may remain outstanding, including timed-out OS
lookups that cannot be cancelled; further lookups are refused while those slots are busy.
Registration rechecks revocation, name ownership and newer requests after DNS completes.
Adapters use `Allowlist.open` (or `Allowlist.fetch`, its 1 MiB buffered form), which connects
to the checked IP directly, preserves HTTPS hostname verification and refuses redirects.
Do not use an unrestricted HTTP client in adapters.
`viewer.adapters.REGISTRY` maps kinds to adapters; `presence` is the fallback and implements
no optional methods. An adapter with `view(entry, allowlist, request)` also exports its route
names as `READ` and `ACT` and a `body_limit(route)`; `hermes` is the one that ships.

## Hermes agent view

A `hermes` agent that registers a `view_url` with the `view` cap is readable in the Agents tab
through its view sidecar: a small web service beside the agent that serves the agent's transcript,
progress and commands at the routes below, behind a Host check, forward auth with a proxy secret,
and an Origin check on POST. The viewer uses the sidecar as a JSON and SSE data API only and never
serves its HTML or JavaScript, so a compromised agent cannot run script in the broker's origin. Routes, under the module root (the browser sees
`/viewer/agents/api/a/NAME/ROUTE`):

* `GET /api/a/NAME/ROUTE` for `cfg`, `rows`, `events` (SSE), `earlier`, `img`, `ctx`, `tools`,
  `search`, `browse`, `skill`, `queue`, `cmds` and `brand`. The query string passes through.
* `POST /api/a/NAME/ROUTE` for `say`, `stop`, `steer` and `cmd`, JSON only. Allowed only when the
  module host sends `X-Viewer-Can-Act: 1` (the broker actions permission) and the agent registered
  the `steer` cap. Standalone mode ignores that header and is read-only.

Each request connects to the registered address again (allowlist checked and pinned), sets
`Host` to the `view_url` host, adds the forward-auth and proxy-secret headers below, and on POST
sets `Origin` to the sidecar origin; the broker has already enforced same-origin for the browser.
No browser header except `Accept`, `Last-Event-ID` and `If-None-Match` is forwarded.
Replies are re-typed: JSON routes only answer JSON (anything else becomes a 502 JSON error),
`img` only PNG, JPEG, GIF or WebP bytes, and `brand` arrives as `{"data_url": ...}` so an SVG is never
a document in the broker origin. A sidecar that refuses the proxy (HTTP 401 or 421) shows as
502 with a hint. The SSE stream gets a `: keepalive` comment between frames every five seconds,
inside the module host's ten second idle limit. A command that takes longer than five seconds
answers 200 with leading whitespace, then its JSON; a failure then carries `error` and `status`
in the body, so clients should check `error` as well as the HTTP status.

| Variable | Meaning / default |
|---|---|
| `AGENTDIR_HERMES_AUTH_HEADER` | Forward-auth header sent with the viewer's user name, default `Remote-User`. Configure the sidecar to trust the same name (`OKO_TAIL_AUTH_HEADER`). |
| `AGENTDIR_HERMES_PROXY_SECRET_FILE` | 0600 file holding the shared proxy secret, read on every request so rotation needs no restart. Never logged or returned. The sidecar reads the same secret (`OKO_TAIL_PROXY_SECRET_FILE`). Unset sends no secret. |
| `AGENTDIR_HERMES_UI_DIR` | Directory holding the view's renderer: `view.js`, `app.css` and `logo.svg` from the view sidecar's `web/` directory, at the release your sidecars run (one that ships `view.js` and `GET /cfg`). Only those three names are served, at `hermes-ui/`. Unset shows presence only, with a note. |
| `AGENTDIR_HERMES_PROXY_SECRET_HEADER` | Header carrying it, default `X-View-Proxy-Secret`; set the sidecar's `OKO_TAIL_PROXY_SECRET_HEADER` to match. |

On the sidecar: add the `view_url` host name to `OKO_TAIL_HOSTNAMES` and the viewer's source
address to `OKO_TAIL_TRUSTED_PROXIES`, and set the auth header and secret as above. The `cfg`
route (session list and agent name) needs a sidecar that serves `GET /cfg`. Image sends go through the broker's POST limit
(`BROKER_MAX_BODY`, 1 MiB by default); raise it if people attach several photos.

The live view is the sidecar project's own renderer, mounted in a shadow root in the tab, so it
has every feature of the sidecar's page: transcript, steps, search, context and usage, memory and
skills, approvals, the send queue, steer, stop and slash commands. Viewers without the broker
actions permission, or agents without the `steer` cap, get the same view with the composer
disabled and a hint. The renderer comes from `AGENTDIR_HERMES_UI_DIR`, which the operator
installs; the browser never loads script or styles from an agent.

### Renderer theme contract

The directory list and live pane use the broker's type scale, surfaces, borders, focus rings and
status tints. The host element passed to a renderer exposes the following CSS custom properties.
Any renderer can read them inside its shadow root; none depend on an agent kind or a sidecar's
data API. Values reference the broker tokens, so light, dark and system theme changes update an
open pane without reconnecting its stream or resetting its composer. Outside the broker, the
variables fall back to the broker's light palette.

| Renderer variable (`--agent-view-` prefix) | Broker token or value |
|---|---|
| `bg`, `surface`, `hover`, `active` | `--bg`, `--s1`, `--s2`, `--s3` |
| `border` | `--line` |
| `text`, `muted`, `faint` | `--t1`, `--t2`, `--t3` |
| `accent`, `on-accent` | `--brand`, `--onbrand` |
| `success`, `warning`, `danger` | `--run`, `--wait`, `--block` |
| `shadow` | `--shadow` |
| `font`, `mono` | System UI and monospace stacks from [the design standards](DESIGN.md#type) |
| `font-size`, `line-height` | `14px`, `1.5` |
| `radius`, `control-radius` | `12px`, `8px` |
| `space`, `control-height` | `16px`, `36px` (phone controls use at least `48px`) |

For example, a renderer can use `background:var(--agent-view-surface)` and
`color:var(--agent-view-text)` on its root, and `var(--agent-view-accent)` for focus and primary
actions. Keep layout and behavior in the renderer; use the variables for appearance only.

Today's installed sidecar renderer does not need an upgrade. After its synchronous `mount`,
the broker appends a marked `style[data-agent-view-theme]` to the host's open shadow root.
This scoped compatibility sheet maps the older `--panel`, `--panel2`, `--ink`, `--text`,
`--muted`, `--faint`, `--accent`, `--green`, `--amber`, `--red`, `--code` and `--user` aliases
on the root (`.ob`), and normalizes standard controls, links, chips and focus styling. It leaves
the renderer's stylesheet and responsive layout in place. Nothing is injected into the broker
document or an agent's page. A non-shadow renderer still inherits the public variable set.

For a clean upstream seam, the sidecar should consume `--agent-view-*` directly (with its own
standalone defaults). That renderer change belongs in its repository, not this one. The
compatibility sheet remains necessary until the installed renderer adopts that contract;
changes to its DOM or private CSS names need a visual compatibility check. The optional
Playwright test in `tests/test_viewer_ui.py` checks live light/dark/auto inheritance and 390px
controls against a synthetic shadow-root fixture, not the external renderer itself.
Button variants retain their background/text pairing during hover, keyboard focus and press.
Disabled buttons keep full text contrast and use a dashed border and unavailable cursor instead
of reduced opacity. The browser check measures at least 4.5:1 contrast for these states in both themes.

Control-state comparison using that synthetic fixture and the unchanged base compatibility sheet
versus the fixed sheet. These are not captures
of an installed external renderer. Each image shows `.pri` hovered, `.primary` keyboard-focused,
and a disabled submit button.

| Theme / viewport | Before | After |
|---|---|---|
| Light / desktop | [Before](images/agents-buttons-before-light-desktop.png) | [After](images/agents-buttons-after-light-desktop.png) |
| Light / phone | [Before](images/agents-buttons-before-light-phone.png) | [After](images/agents-buttons-after-light-phone.png) |
| Dark / desktop | [Before](images/agents-buttons-before-dark-desktop.png) | [After](images/agents-buttons-after-dark-desktop.png) |
| Dark / phone | [Before](images/agents-buttons-before-dark-phone.png) | [After](images/agents-buttons-after-dark-phone.png) |

Trust boundaries worth knowing before you enable it:

* Read routes have no tier beyond module access. Everyone in the Agents module's group can read
  every view-capable agent's transcript, context, memory browser and skills, so scope that group
  to people who may see all of them.
* The proxy secret is shared. Any listener inside `AGENTDIR_VIEW_ALLOW` that an enrolled agent
  registers as its `view_url` receives it, along with the viewer's user name. Keep the allowlist
  to the exact sidecar host:port pairs, and treat an enrolled agent as able to present any of
  them (including another agent's sidecar). Per-agent secrets are the planned upgrade.
* Non-stream replies have a total deadline of 130 seconds, and image replies are always sent as
  `Cache-Control: private, no-cache` whatever the sidecar asks for.

For local tests: `python -m pytest tests/test_viewer_*.py`. No JavaScript harness is required;
server-side asset checks and a socket-free CLI/server-dispatch round trip are included, plus
a real HTTP round trip that runs where the test environment permits loopback sockets.
