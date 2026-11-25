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

An agent's page opens with one header, the same for every kind: name, status and a single
actions menu (**Copy link**, plus the session list, **Show all steps** and **Stop current turn**
for a `hermes` agent), then what it says it is doing now (a "waiting on" or "blocked by" part is
called out on its own), then kind, model, a context meter, the session and the last activity.
Status, the doing-now line, model and context come from the Fleet view when it lists the agent
(see `agentSummary` in [modules](modules.md)); otherwise the header shows directory presence only.
The page follows the dashboard theme and type scale; the design reference is
[docs/mockups/agent-view.html](mockups/agent-view.html). In the dashboard the tab fills the
window exactly: the page never scrolls and has no title (the tab names it), only the transcript
and a long agent list scroll, and the header and message box always stay on screen. Below it, a tailing agent's transcript fills the page with a
**Conversation** / **Everything** switch: Conversation hides harness records and routine system
rows and says how many it hid. The log follows the newest line until you scroll up; **Latest**
jumps back. A message box is docked at the bottom for agents that registered `steer`; sent
messages appear in the transcript as queued, then delivered.

On the Fleet page, **Quick view** on a listed agent opens a card (a bottom sheet on a phone). The
card's own header names the agent; under it sit one status line (status, kind, model, context,
last activity, and the actions menu), what it is doing and waiting on, its newest turns in the
same reply and steps style as the Agents tab, and the same message box with one-tap quick
replies (`AGENTDIR_QUICK_REPLIES` in [settings](settings.md)). For a `hermes` agent with a view
the box sends messages: **Send** starts a turn, or while the agent works queues the message for
its next turn, and the arrow beside Send offers **Interrupt and send**, which stops the current
turn first. **Stop current turn** is in the actions menu. **Full view** opens the agent's page.
The box is disabled with a hint for viewers without the broker actions permission or agents that
did not register `steer`. Closing the card closes its stream, so a tab holds at most one.

## Mid-turn steering (PostToolUse)

Claude Code, Codex and other hook-capable agents can receive a message at their next tool
call without an inbound listener or a live view. Start with enrollment above, enable the
Agents module, and grant senders the broker's existing actions permission as described in
[module permissions](modules.md). Standalone mode stays read-only, even with a forged
`X-Viewer-Can-Act` header.

From the checkout or `/opt/eph-broker`, register a private directory unique to
this agent (use absolute paths):

```sh
python3 -m viewer.clients.agentdir --state agentdir-state/demo.json register \
  --kind claude-code --ttl 30 --steer-dir "$PWD/agentdir-state/demo-steer"
python3 -m viewer.clients.agentdir --state agentdir-state/demo.json beat --every 10
```

The CLI advertises `steer-pull` only when it has a configured steer directory and consumes
messages on beats. The existing `steer` cap is for sidecar actions, not pull delivery.
Hermes plugin registrations with only `view,steer` get no next-tool-call composer and
cannot queue pull messages. Sidecar controls are unchanged.

Install this command as the main agent's **PostToolUse** hook, replacing the paths with
your installation and the directory passed to `--steer-dir`:

```text
python3 -I /opt/eph-broker/viewer/steer.py /absolute/path/agentdir-state/demo-steer
```

For Claude Code, the corresponding settings fragment is:

```json
{"hooks":{"PostToolUse":[{"matcher":"","hooks":[{"type":"command","command":"python3 -I /opt/eph-broker/viewer/steer.py /absolute/path/agentdir-state/demo-steer"}]}]}}
```

Use the same command in a Codex harness that supports PostToolUse. Hook installation for
broker-spawned workers is separate; this feature does not alter their launch settings.
`viewer/steer.py` ships with the viewer subpackage as a standalone hook, so it runs in
isolated mode without importing the broker or its configuration. Its output-confirmation
ordering supports retry-safe pull delivery; the broker's own hook is separate.
Keep that script read-only to the agent. The sandbox must allow the hook to read and write
only this agent's steer directory. Use one beat process per state file, and never share a
state file or steer directory between agents or runs.

In the Agents tab, pick the registered agent and use the message box or a quick reply.
The composer accepts up to 4000 characters. Busy agents show **Queued**, then **Delivered**
after the hook's log record reaches the directory on a beat. Idle agents use their configured
wake channel instead, as described below; without one the UI says
**Not delivered: agent idle, no wake channel**. There is no Stop-hook injection.
The hook labels each message as operator feedback sent via the Agents tab, with the sender
and UTC queue time, separately from the tool's output. Existing permissions and constraints
still apply. Message delimiters are neutralised in the body, and sender metadata is kept
on one line. This framing does not authenticate local files: anyone who can write the steer
directory can impersonate an operator. Keep it private.

For Claude Code, the hook emits JSON on stdout using
`hookSpecificOutput.additionalContext` with `hookEventName: "PostToolUse"`. This is the
[PostToolUse model-context channel](https://code.claude.com/docs/en/hooks#posttooluse-decision-control),
not plain stdout (which is only visible in verbose mode). Hooks cannot create a user-role
turn; the explicit attribution identifies who sent the feedback without claiming a higher
instruction priority. Queue time comes from the inbox filename's nanosecond prefix and
stays the same on retries; missing or invalid times are shown as `unknown`.

The hook ignores subagent calls carrying `agent_id` and always exits successfully.
Hermes sidecar Send and Interrupt controls are a separate mechanism;
use the next-tool-call composer only for agents running this hook and beat client.

Protocol and recovery:

* Action-gated `POST /api/agents/NAME/steer` accepts JSON `{text: "..."}` and returns
  `{ok: true, id: "...", state: "...", via?: "wake", reason?: "..."}`; agents without `steer-pull` receive 403. The authenticated host supplies the sender identity, not the
  request body. There are at most 32 outstanding messages and 32 recent status records
  per registration; a full queue returns 409. Queue entries are bound to the enrolled kid,
  so a replacement agent with the same name does not receive the old agent's messages.
* For the tool-call path, signed beat responses include `steer: [{id, ns, text, doer}]` until acknowledged. The
  client first saves the pending messages in its 0600 state file, then writes
  `<steer dir>/inbox/<ns>-<id>.msg` as JSON `{id, doer, text}`, byte-compatible with
  `steer.enqueue`: dotfile, fsync, atomic rename, 0700 directories and 0600 files.
  All steer-path components refuse symlinks. The next beat includes `steer_queued: [id]`.
* The client tails `log.jsonl` and reports `steer_delivered: [id]` on the beat. List
  snapshots expose `steer_messages: [{id, state, via?, reason?}]`, not message text or sender names.
  The hook prints and flushes context first, then writes a delivered record with
  `output_confirmed: true`, then removes its `.delivered` claim. A normal delivery is
  once, including retrying a lost beat response. Delivery across crashes is at-least-once:
  a hook killed after output but before logging can cause a duplicate. After observing a
  leftover claim for 30 seconds the client returns it to `.msg`; missing files without
  an output-confirmed record are recreated. Legacy delivered records without output
  confirmation are not sufficient to acknowledge a message. No message is acknowledged
  before hook output. Partial inbox writes are retried as well.
* Pending local state and the server's private `steering.json` survive restarts. Reaping
  presence hides the agent but retains its queue for re-registration with the same kid.
  Explicit deregistration clears that registration's server queue; finish outstanding
  deliveries first. Treat both state directories as private message storage and remove
  obsolete local steer directories when retiring an agent.

### Idle-agent delivery

An idle session makes no tool calls. To wake one, the directory administrator can configure
a channel for its registered name, independently of the agent's registration or harness.
The directory stores that channel as the entry's `wake` field. Default is none. Non-Zulip
installations need no bus settings and retain tool-call delivery for busy agents.

For a Zulip-connected agent, set these in the directory process's environment:

```sh
AGENTDIR_WAKE_CHANNELS='{"demo":{"kind":"zulip-dm","handle":"demo-bot@chat.example.com"}}'
AGENTDIR_WAKE_ZBUS=/usr/local/bin/zbus
AGENTDIR_WAKE_ZBUS_ENV=/path/to/directory-sender-identity
```

Restart the directory after changing the map. A map value of `null` or `"none"` explicitly
disables that entry's channel. Unknown channel kinds or invalid handles disable the whole
map and log an error. Registration cannot choose another recipient: the map belongs to the
administrator, so enrollment alone cannot turn the directory into an arbitrary DM relay.
The credentials setting is only a path; the directory never reads its contents. The bus CLI
and sender identity are optional operator-installed dependencies, not included in the viewer.
The target must already listen for DMs from that sender while idle.

Keep presence accurate: run `status busy` when a main turn starts and `status idle` when it
ends, using the agent's existing busy/idle hooks. Only an explicit directory `idle` status
selects wake delivery. Busy or unknown (`live`) status keeps the existing PostToolUse path.
The delivery path is fixed when the operator sends the message; already-pulled hook messages
are not also DMed, avoiding duplicate delivery during a busy-to-idle transition.

An idle send goes directly to the channel, without waiting for a heartbeat. The DM uses the
same capped, delimiter-neutralised forwarding frame, with sender, queue time, dashboard ID
and **UNTRUSTED COMMENT** markers. Task constraints still apply. The transport has a five-second
deadline and runs outside the directory lock. With a listening target this removes the
next-tool-call wait, so a wake normally arrives within about ten seconds.

A successful DM shows **Sent, awaiting consumption**, not Delivered. For agents with `tail`,
the directory marks **Delivered** when a signed user-role transcript record contains the
forwarded message's metadata and ID. Tool results and assistant echoes do not acknowledge
it. For listeners without a canonical user-role transcript record, acknowledge only after
the main agent consumes that ID, using the same enrolled client:

```sh
python3 -m viewer.clients.agentdir --state agentdir-state/demo.json ack-wake MESSAGE_ID
```

This sends a signed `steer_delivered` beat, is idempotent, and does not require a tool call.
Do not acknowledge merely because the transport accepted the DM or the agent became busy.
The UI refreshes delivery on its next presence poll. A failed transport shows
**Not delivered: wake channel failed** and never silently falls back to an idle tool inbox.
A later consumption receipt overrides a transport failure, since a timeout can mean an
ambiguous send. There are no automatic DM retries. After a directory
crash during sending, the persisted `waking` state also awaits a consumption receipt rather
than resending. Inspect the target before retrying such messages to avoid duplicates.
Not-delivered records count as completed history, so they do not fill the outstanding queue.

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
Run one tail process per credential file. Other CLI calls on that file (`status`, a steer
`beat`) may run beside it: every call takes `<state>.sign.lock` and adopts a file another
process replaced, so timestamps stay in order and steer state is never overwritten.
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
Canonical Codex user text parts remain excluded from the display projection. Forwarded wake
headers carry only their message IDs in `wake_receipts` across client and server projection,
so consumption can mark a wake Delivered without forwarding the discarded text.
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

`AGENTDIR_STATE_DIR` (or `--state-dir`, default `agentdir-state` standalone) holds the
server's 0600 `revoked.json`, `used-tokens.json` (used token ids and their expiry, never
keys), and `steering.json` (per-kid messages and recent delivery states, created on first send).
Keep the enrollment key stable across restarts. Presence and replay timestamps
are in memory. After restart agents must register again; the CLI saves its registration and
automatically re-registers on the next beat when the server reports a missing entry.
The CLI also saves its latest explicit busy/idle status and includes it in re-registration,
including recovery triggered by transcript events or TTL expiry. An idle file needs no new turn
or status hook to restore idle presence after a directory restart. A used token stays refused
across restarts until it expires; deregistered kids remain revoked.
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
Register includes `{name,kind,caps,ttl,view_url?,status?}`; optional status is `busy` or `idle`
and restores presence atomically with registration. Without it, a new entry starts as `live`.
TTL is 3..86400 seconds. Allowed kinds are
`hermes`, `claude-code`, `codex`, `generic`; caps are `view`, `steer`, `steer-pull`, `tail`. A name owned by
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
| `AGENTDIR_HERMES_UI_DIR` | Optional and no longer used by the Agents tab, which draws the view itself. If set, the viewer still serves `view.js`, `app.css` and `logo.svg` from that directory at `hermes-ui/` and nothing else. Safe to leave unset. |
| `AGENTDIR_HERMES_PROXY_SECRET_HEADER` | Header carrying it, default `X-View-Proxy-Secret`; set the sidecar's `OKO_TAIL_PROXY_SECRET_HEADER` to match. |

On the sidecar: add the `view_url` host name to `OKO_TAIL_HOSTNAMES` and the viewer's source
address to `OKO_TAIL_TRUSTED_PROXIES`, and set the auth header and secret as above. The `cfg`
route (session list and agent name) needs a sidecar that serves `GET /cfg`. Image sends go through the broker's POST limit
(`BROKER_MAX_BODY`, 1 MiB by default); raise it if people attach several photos.

The tab draws the view itself, in the broker's own DOM and dashboard styles, from the `cfg`,
`events` and `earlier` routes: user messages and replies, with each run of thinking and tool calls
folded into one quiet line ("5 tool calls · 38s · Show steps", timed from the message before the
steps to the reply after them) that opens to the steps. **Show all steps** in the actions menu
opens or closes them all. A working line at the end
shows what is running and for how long. The session list from `cfg` is in the actions menu, and
the address bar follows the session and a linked message. The composer has one **Send**: it starts
a turn, or while the agent works queues the message for its next turn, and the arrow beside it
offers **Interrupt and send** (`stop`, then `say`). **Stop current turn** is in the actions menu.
Viewers without the broker actions permission, or agents without the `steer` cap, get the same
view with the composer disabled and a hint. The browser never loads script or styles from an
agent or its sidecar, and every piece of agent text is set with `textContent`.

The sidecar page's other panels (search, memory and skills browsers, approvals, slash commands)
are not part of this view; their read routes stay available through the proxy.

The optional Playwright test in `tests/test_viewer_ui.py` mounts the view and the Fleet quick view
on a stubbed sidecar at 1440 and 390 pixels in light, dark and system themes. It checks that the
text box and both halves of Send share one height and top edge (36px on desktop, 44px on
phones), that steps start collapsed, that nothing is italic or overflows sideways, and that
Send clears 4.5:1 contrast. A second check loads the real dashboard page with the module at
1440x900, 1366x650 and 390x844 and checks that the page height equals the window height, that
only the transcript scrolls, that the card header and composer stay on screen, that there is no
page title and that every phone tap target is at least 44px. Screenshots from that run are in
`docs/mockups/agents-tab-*.png`.

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
