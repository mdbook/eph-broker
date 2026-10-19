# eph-broker security residual: same-uid secret reads (and the optional HMAC layer)

Status: **a known limit that code alone can't fix.** This documents a residual that the broker's code-level hardening
**cannot** close on its own, plus the HMAC transport layer.

## 1. The residual: a bypass worker can read secrets as the same uid

The capability enforcement in `adapters/cc.py` (`_READ_DENIED` / `_WRITE_DENIED` / `_GENERAL_DENIED`) gates the
worker's **Read/Edit/Write/Bash tool grants**. The Claude Code harness enforces it through its permission flags,
and the hermes harness through the `eph-gate` plugin, which imports the same lists. It is real and load-bearing
for the *tool surface*. But it is **not a sandbox**, on either harness. A Claude Code worker that runs with
`--dangerously-skip-permissions` (the general/bypass tier on a non-Anthropic backend), a hermes worker with the
`terminal` toolset, or any worker that otherwise gets shell access runs as **the same OS uid as the broker
process**. The deny floor covers the Read *tool* only; it does nothing about what that uid
can `open()`.

So a bypass worker's `Bash` can still read, as the broker's own uid, all of:

- **the broker's env file**: whatever file supplies `GITLAB_MR_TOKEN` / `BROKER_HMAC_SECRET` to the
  broker process (e.g. its systemd `EnvironmentFile=` or compose env). Readable directly off disk.
- **`/proc/<broker-pid>/environ`**: the broker's live environment, including any token it holds in
  memory, regardless of file perms on the env file.
- **a sibling bot's env file**: the bot-identity env the worker or a sibling session uses.
- **`git credential fill`**: any credential the git credential helper will hand back for a configured
  remote (a push token, etc.), invoked as the same uid.

`scrub_worker_env` removes these secrets from the **worker's own env**. That is worth doing and closes the
*inheritance* vector, but it does **not** stop a same-uid process from reading them from the sources above.
**File permissions and the Read-tool floor are both defeated by same-uid.** No amount of allowlisting the
worker env or the tool grants fixes this, because the capability sits at the OS-uid level, below the harness.

Same-uid **writes** matter too. The broker's `pending.json` (beside `completed.json`) is **spawn-bearing**: a
same-uid write to it becomes a worker spawn on the next broker restart. Entries older than `PENDING_MAX_AGE`
(default 1h) are dropped on load. That age drop is **not** a same-uid mitigation, because the writer controls
`armed_at` and can simply stamp a fresh arm time. It only guards against stale rows left by a long outage and
against corrupt rows: a non-finite or far-future `armed_at`, or a malformed row, is dropped individually.

### Possible fixes (both are deployment choices, not code changes)

1. **Separate-uid worker sandbox.** Run the worker as a *different, unprivileged* uid (or in a container /
   namespace / systemd `DynamicUser=` slice) that has **no read access** to the broker env file,
   `/proc/<broker>/environ`, the bot env, or the git credential store. This makes file perms actually
   bite. It's the heaviest option and the only true close of the read vectors.
2. **An external policy gate.** Put a policy gate in front of the worker rather than trying to sandbox
   the worker in-process, and don't run untrusted inference next to the secrets at all. The exact shape
   depends on your deployment.

Either is an architecture decision, not a patch, so it is surfaced here rather than attempted in code.

### Same-uid git config the broker still loads

The broker's own git calls in a worker worktree (`worktree.py`) are hardened: `-c core.hooksPath=/dev/null
-c core.fsmonitor=false` on every call, `--no-ext-diff --no-textconv` on the diffs, `-c commit.gpgSign=false`
on the WIP commit, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1` and `GIT_CEILING_DIRECTORIES` in the
env (credentials, identity and `safe.directory` are passed explicitly), and a refusal (`WorktreeTampered`)
when the worktree's `.git` gitdir pointer is no longer the one `make()` recorded. The file tools are denied
`Edit/Write` on `.git` / `.git/**` in both the write/code and the general deny floors.

That closes the **file-tool** route (a worker without exec repointing `.git` or planting hooks). It does
**not** close the **exec** route. A worker with any exec (a granted test runner, a bypass/general worker) runs
as the broker's uid and can still write git config that the broker's git calls load:

- **the base repo's `.git/config`** and the worktree's **`config.worktree`**: repo-local config, which no
  `GIT_CONFIG_*` env var disables. Still-live keys there include `filter.<name>.clean`/`smudge` (runs on the
  broker's `git add -A` when a `.gitattributes` matches), `url.<base>.insteadOf` / `pushInsteadOf` (redirects a
  fetch or push), `http.proxy` / `http.<url>.*` (routes the credentialed push through a worker-chosen proxy)
  and `credential.helper` (a repo-local helper on fetch). `core.hooksPath`, `core.fsmonitor`,
  `diff.external`, `textconv` and `gpg.program` are neutralised by the flags above.

  For the push, `git push <url>` re-applies the rewrites to the explicit URL, so a chained
  insteadOf -> gitlab URL -> pushInsteadOf redirect used to pass the host check. The broker now refuses the
  push when any insteadOf/pushInsteadOf prefix (an empty value matches every URL, so it is refused too) or a
  `remote "<url>"` section would rewrite the resolved URL again. The broad-token helper answers only
  `https` + `host=<GITLAB_HOST>` (the netloc of `GITLAB_API`). A fetch is still not checked.
- **`~/.gitconfig`** (and system config): no longer loaded by the broker's worker-worktree git calls, but still
  loaded by the broker's base-repo calls (`fetch`, `worktree add/prune`) and by every other same-uid git user.

The real fix stays **item 1 above (separate-uid worker / sandbox)**: a worker that cannot write the broker
uid's files cannot plant config the broker reads. The git flags are defence in depth, not a boundary.

## 2. HMAC enablement (`BROKER_HMAC_SECRET`)

`BROKER_HMAC_MODE` = off | log | **enforce (default)**. `enforce` returns 401 for a missing or bad signature,
and for every call while the secret is unset. An unknown value also enforces. `log` verifies and logs
`HMAC-CHECK ... result=PASS|FAIL-*` per sender but never rejects, for rolling signing out to a tap that
doesn't sign yet.
The header is `X-Broker-HMAC`.

The broker has the **HMAC-check plumbing** (`app.py` `verify_hmac`, `X-Broker-HMAC` header, `HMAC_SECRET_ENV`
= `BROKER_HMAC_SECRET` by name only). When the secret env var is unset, `enforce` rejects everything and
`log` passes everything (and logs it). Binding to loopback so that only the reverse proxy's IP-guarded vhost reaches `/event` hardens the
*network* exposure. HMAC over the request body is the defense-in-depth layer that authenticates the *caller*,
not just its source IP.

To enable it:

1. **Generate the secret and install it.** For example `openssl rand -hex 32`, written into the broker's
   environment file as `BROKER_HMAC_SECRET=<value>`. Keep that file mode 0600, readable only by root or the
   service user, and out of git.
2. **Sign on the caller side (n8n).** The n8n workflow that POSTs `/event` must compute
   `hex(HMAC-SHA256(raw_request_body, secret))` and send it as the request header. The signature must be
   over the **raw body bytes** exactly as sent (before any re-serialization), or the broker's recompute
   won't match.

Until both are done, `/event` refuses every call, which is the safe state for a new install. On an
install whose tap already sends unsigned events, set `BROKER_HMAC_MODE=log` explicitly while you do both
steps, check that `HMAC-CHECK` shows `PASS` for every sender, then remove it.
