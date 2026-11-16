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

## Listeners and authentication

* Agent API: `AGENTDIR_AGENT_BIND`, default `127.0.0.1:8790`. Only POST `/v1/enroll`,
  `/v1/register`, `/v1/beat`, `/v1/deregister` exist. Enrollment authenticates the signed
  one-use token; all other requests require per-agent HMAC. `--server URL` or `AGENTDIR_SERVER`
  changes the client target. Never expose plaintext credentials over a network: use a TLS proxy.
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
is rendered with textContent. V1 ships presence only, so there are no View buttons yet.
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
Adapters use `Allowlist.fetch`, which connects
to the checked IP directly, preserves HTTPS hostname verification, refuses redirects,
and limits responses to 1 MiB. Do not use an unrestricted HTTP client in adapters.
`viewer.adapters.REGISTRY` maps kinds to adapters; `presence` is the fallback and implements
no optional methods. Future adapters may implement `view(entry, allowlist)` (JSON response),
`steer`, or `tail`. No Hermes connection, command execution API, or keypair auth ships in V1.

For local tests: `python -m pytest tests/test_viewer_*.py`. No JavaScript harness is required;
server-side asset checks and a socket-free CLI/server-dispatch round trip are included, plus
a real HTTP round trip that runs where the test environment permits loopback sockets.
