# UI modules

The broker can supervise a separate UI process and proxy it under
`/viewer/<name>/`. Its JavaScript mounts in the broker's own DOM, with the broker
CSS variables, not an iframe. Updating or restarting a module never restarts the
broker or takes worker dispatch slots. This is a trusted-code extension, not a
sandbox: install only code you trust with the broker's OS account and browser
origin. Children inherit the broker environment. Do not install untrusted code.

## Try it in ten minutes

From an installed source checkout, Linux and Python 3.9+ are sufficient. No extra
packages, model account or module build step are needed. The example advertises
the Fleet name `demo`; your Fleet feed must contain that name to show Quick view.

In a clean shell, run the normal preview with these additional settings:

```sh
BROKER_SITE_ENV=/dev/null BROKER_CONFIG=config/demo/broker.example.json \
BROKER_MODULES=agents \
BROKER_MODULE_AGENTS_CMD='python3 examples/viewer.py' \
BROKER_MODULE_AGENTS_LABEL=Agents BROKER_MODULE_AGENTS_GROUP=viewers \
BROKER_STREAM_USER_HEADER=X-Auth-User BROKER_STREAM_GROUPS_HEADER=X-Auth-Groups \
python3 app.py
```

For this local-only smoke test, send synthetic identity headers:

```sh
curl -H 'X-Auth-User: demo' -H 'X-Auth-Groups: viewers' http://127.0.0.1:8787/viewer-status
curl -H 'X-Auth-User: demo' -H 'X-Auth-Groups: viewers' http://127.0.0.1:8787/viewer/agents/identity
```

The first returns `agents.state: up` once its first health check succeeds. The
second returns `user: demo` and `can_act: "0"`. Open `/dash.html` through your SSO
proxy, configured to replace these two headers, to see the **Agents** tab. The
proxy must protect **all** viewer paths, `/viewer-status` and `/dash.html`, not just
the tab. Never trust headers supplied directly by remote clients. Use the
[dashboard actions proxy setup](configuration.md#dashboard-actions) for the
secret, header replacement and operator controls. Streaming need not be enabled.

Unset `BROKER_MODULES` to remove all module routes, tabs and supervisor threads.
Stop the broker with Ctrl-C or SIGTERM; it terminates its module process groups.
Remaining group members receive SIGKILL after a two-second grace period, even
when the leader has already exited or crashed.
SIGKILL cannot run cleanup. A service manager should clean the broker's cgroup
after a broker SIGKILL. An externally supervised module is never killed by the broker.

## Configuration

Read once at broker startup. Changing the installed module needs no broker
restart; changing its configuration does. Names match `[a-z][a-z0-9_]*` and are
unique. For `agents`, the prefix below is `BROKER_MODULE_AGENTS`.

| Variable | Meaning / default |
|---|---|
| `BROKER_MODULES` | Comma list of names. Unset means feature off. |
| `<prefix>_CMD` | Child command, shell-style argument splitting, **no shell expansion**. Exactly one of CMD or UPSTREAM is required. Relative paths use the broker working directory. |
| `<prefix>_UPSTREAM` | External module: absolute Unix socket path, or `http://127.0.0.1:port`, `http://[::1]:port`, or `http://localhost:port`, optionally with a base path. Non-loopback addresses and credentials are refused. No launch or kill. |
| `<prefix>_SOCKET` | Child socket path. Defaults to `modules/<name>.sock` beside `BROKER_STATE_PATH`, otherwise under `$XDG_STATE_HOME/eph-broker` (`~/.local/state/eph-broker`). Socket mode 0600; new parent directories 0700. Keep the path short enough for the OS Unix socket limit. |
| `<prefix>_LABEL` | Tab text, default title-cased name. |
| `<prefix>_GROUP` | Required SSO group for this module. Missing config or identity fails closed. Uses the same trusted headers and separator as stream.py. |
| `<prefix>_MAX_STREAMS` | Maximum simultaneous proxy requests, including SSE, default 8. Independent per module and independent of `BROKER_STREAM_MAX`. Over cap: 503. |
| `<prefix>_VERSION_FILE` | Optional deployment marker. Changes to mtime **or content** restart a child. Status version is the file's SHA256, not its contents. Creation and removal count as changes. |
| `<prefix>_DISABLED_FILE` | While this path exists, stop and stay stopped. Reload does not override it. Removing it starts the child again. Also suppresses external-module health checks. |

The module group uses exact membership, not a substring. Duplicate trusted
headers are refused. Read access follows stream.py's header-trust model and does
not require the actions secret. The proxy must strip client identity headers and
set only authenticated SSO values. `X-Forwarded-*` and generic client-settable
header names cannot be configured as trusted identities.

Child exits restart after 1, 2, 4, 8, 16, 32, then 60 seconds, capped at 60.
Sixty seconds of healthy running resets backoff. Every 10 seconds the supervisor
requests `/healthz`, with a 2 second deadline; three consecutive failures restart
the child. Failed checks mark an already-up module down immediately. Each module
has its own supervisor; no module code is imported into the broker. Children
get a private socket umask, no stdin and discarded stdout/stderr. Send module
diagnostics to your own logging destination, never to the HTTP error text.

`GET /viewer-status` is broker-owned and returns only modules the caller may
view: `{name: {state, since, restarts, last_error, version}}`. States are `up`,
`down`, `disabled`, `starting`; `since` is epoch seconds for the current state.
The broker's down card shows local time, restart count and last error. Operators
with `/actions.allowed` see Reload for child modules. `POST
/actions/module_reload` with `{"name":"agents"}` uses the existing actions
authentication, CSRF checks and audit path. External modules return 409; their
owner reloads them. The action only queues a restart, without waiting for it.

## Module server contract

`examples/viewer.py` is a complete runnable implementation. The child receives
`BROKER_MODULE_SOCKET` and `BROKER_MODULE_NAME`. Bind the supplied Unix socket,
serve HTTP/1.0 or HTTP/1.1 and support:

- `GET /healthz`: 200 when ready. Do not require viewer headers on this endpoint.
- `GET /ui.js`: JavaScript ES module, `Content-Type: text/javascript`.
- `GET /fleet-index`: JSON array of agent names **this viewer may see**. Empty
  array is valid. It is polled every five seconds; a failed poll clears hooks.
- Your own GET and POST endpoints under this root. The broker strips its prefix,
  preserves the query and adds an external upstream's configured base path.

Identity headers set by the broker, never taken from client copies:

- `X-Viewer-User`: authenticated username.
- `X-Viewer-Can-Act`: `"1"` or `"0"`. This uses the actions enabled flag, proxy
  secret and actions group. Every POST additionally passes all actions header
  gates: proxy secret, actions group, `X-Broker-Action: 1`, JSON content type and
  same-origin `Sec-Fetch-Site` when present. The module still owns its own
  resource authorization and input validation.

Only Accept, Content-Type, Last-Event-ID and If-None-Match are copied to the
upstream. Cookies, authorization, SSO headers and proxy secrets are not forwarded.
Only Content-Type, Cache-Control and ETag are copied back; module cookies and
redirects are not forwarded. Every response retains the broker's no-frame policy.
Never publish an external module directly: it relies on the broker as its auth
boundary. Unix socket permissions limit access to the broker user, not other
processes running as that same user.

Connect timeout is 2 seconds, idle read timeout 10 seconds. Send SSE heartbeats
more frequently than that. SSE bytes flush as received, with
`X-Accel-Buffering: no`. Requests close after `BROKER_STREAM_MAX_S` (default 600),
including POST body acquisition, a slow trickle or blocked browser. EventSource
reconnects through SSO.
The normal broker POST size limit applies. The module must emit valid HTTP
framing, including chunked transfer encoding when appropriate.

## Browser contract

Export `mount(el, ctx)` and `quickView(el, agentName, ctx)` from ui.js. The broker
owns navigation, the down card and the Fleet quick-view panel; all view content
is the module's. Both exports may return a cleanup function or a promise of one.
Use `ctx.signal` for fetches/listeners and clean up timers and EventSources when
aborted. A reload or navigation aborts and unmounts the old view.

`ctx` contains:

- `baseUrl`: same-origin `/viewer/<name>/`, for relative API fetches.
- `canAct`: whether `/actions` allowed this browser. A hint, not authorization.
- `agentName`: decoded agent selected by a full-view link, or null.
- `openAgent(name)`: navigate to the module tab and select that agent.
- `signal`: AbortSignal for this mount.

Use the broker's CSS variables (`--t1`, `--t2`, `--t3`, etc.) and semantic HTML.
Do not create iframes. Treat names, status and API text as untrusted; use
`textContent`, not unsanitized innerHTML. POST with JSON and `X-Broker-Action: 1`;
never ask browser code to hold a proxy secret. The SSO proxy injects it.

Quick view buttons appear only for Fleet rows whose exact name is in a module's
authorized index. Clicking opens an in-page container and calls `quickView`.
**Open full view** navigates to `#module/<name>/<encoded-agent>` and passes that
agent to `mount`. If more than one module advertises a name, the first configured
module supplies its Quick view.

The entrypoint URL changes with module version, restart count and up timestamp.
Failed entrypoint imports are retried on later polls with fresh URLs and
exponential backoff from five seconds up to one minute. Successful loads stay
cached until the module changes; a new module version resets the retry delay.
Use content-hashed URLs for its imported dependencies so a browser never reuses
old dependency code after an upgrade. Publish code atomically, then replace or
touch the version marker. Keep `ui.js` uncached or revalidated at the module.
For external modules, change the version marker or reload through the module's
own supervisor and let health go down then up. No broker restart is necessary.

## Verification

`python -m pytest tests/test_modhost.py` checks lifecycle, backoff, health,
version/disabled markers, auth, identity stripping, per-module limits, deadlines,
SSE flushes and the dashboard hook. When Node.js is available it executes the
dashboard import-retry regression without browser or network dependencies.
It also checks real process-tree cleanup and launches the stdlib example on a
Unix socket, kills it, waits for recovery, reloads and disables it. That one
integration test and the real slow-upload test skip when the environment refuses
the required socket operations. The deterministic upload-deadline test still runs.
For a deployment smoke test, run those steps with the tab open while polling
`/health`; broker dispatch and health should be unaffected by module failure.
