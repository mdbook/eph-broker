# Durable drain operations

Drain is an admission switch, not a worker cancellation mechanism. It persists in
the same local journal as the deferred queue and scheduler checkpoint. This source
change does not deploy a service or authorize a restart.

## Storage and administration

Configure `BROKER_STATE_PATH` as an absolute path on persistent local storage owned
by the service user, for example `/var/lib/eph-broker/broker-state.sqlite3`. If
`COMPLETED_PATH` is configured, the default is `broker-state.sqlite3` in its directory.
An explicit `EVENTS_LOG` supplies that completed path under the existing defaults.
No durable path means `/drain` returns 503 instead of pretending to persist a switch.

The broker creates the database with mode 0600, uses full synchronous SQLite
transactions, and holds a process-lifetime advisory lock on its sibling `.lock`
file. Only one broker may open a path. Startup refuses an unreadable/corrupt journal
or a second owner. Use a local filesystem with working SQLite locking and fsync;
do not put this file on NFS. Keep its parent private and outside worker-writeable
trees. Backups are sensitive: queued DM bodies and comments are persisted here.
The append-only measurement log still redacts DM bodies. Completed intake rows are
deleted; SQLite pages/backups can retain old content, so this is not secure erasure.

`POST /drain` accepts exactly `{"enabled": true}` or `{"enabled": false}`. Supply
the existing `X-Broker-Rerun-Token` header using your protected local admin client.
Its configured variable name is `RERUN_TOKEN_ENV`, default `BROKER_RERUN_TOKEN`.
No new credential is needed. Missing/unset/wrong identity returns 401 before mutation;
invalid JSON/boolean returns 400; persistence failure returns 503. The existing
loopback binding is unchanged. A reverse proxy must preserve the admin protections.
No unauthenticated filesystem toggle or public write path is introduced.

Success is HTTP 200 with `ok == true` and `drain.enabled` exactly matching the
requested boolean. Enabling returns only after the flag is committed under the same
lock used by intake and scheduling. Persisted drain is read before any launch after
startup. Do not edit/delete journal files to toggle it.

## Starting with a new journal

To start a logically uninitialized journal already drained, opt in before startup
with `BROKER_DRAIN_ON_NEW_JOURNAL=true` in the existing startup settings. Unset
defaults to false. Accepted true spellings are `1`, `true`, `yes`, `on`; false
spellings are `0`, `false`, `no`, `off`, ignoring case and surrounding whitespace.
An empty or other invalid value refuses startup. Enabling it without a resolved
durable state path also refuses startup.

With the process owner lock held, initialization classifies the schema inside
the same explicit SQLite transaction that creates a new schema and its drain flag.
Both commit before scheduler restoration or any tick, liveness, Zulip or HTTP
activation. A new file, or an empty schema left by interrupted initialization,
can be initialized. A complete existing journal is never considered new because
its tables happen to contain no rows. Imported legacy pending work remains held
by the committed flag from its first scheduling opportunity.

Existing journals keep their persisted true or false value. An older complete
journal without a drain row retains its false default; this setting does not turn
that journal on. Removing the setting cannot clear persisted true. Leaving it
enabled cannot turn drain back on after an authenticated drain-off and restart.
Use the existing authenticated admin endpoint to change an existing journal.

Startup rejects partial or incompatible schemas and corrupt storage; it does not
complete, reset or repair them. The expected broker schema includes its legitimate
SQLite automatic index, sequence and supported ANALYZE objects. A persisted drain
row must decode as a JSON boolean; null, numbers, strings and malformed JSON fail
closed. Initialization, validation or commit failure prevents activation and
releases constructor resources. Interrupted initialization commits the new schema
and flag together or leaves them uninitialized for retry.

This setting requires a version implementing it; older binaries do not honor it.
It closes the new-journal first-start launch window. It does not preserve work
already lost in an older service's stop interval, settle detached or uncertain
external effects, or remove legacy-import freshness rules. Those require separate
maintenance and recovery disposition. It introduces no manual SQLite toggle.

## Intake and ordering

While enabled, every new launch-producing path waits: Vikunja triggers, GitLab/GitHub
issues, internally emitted MR reviews, Gmail wakes, authenticated internal Zulip
wakes, comment respawns, admin reruns, and direct TFA launches. New forwards into
an already-live worker also wait in the journal. Turns and forwards already buffered
before drain finish under the existing liveness handshake. No new worker is started
by the scheduler while drain is enabled, including pending work armed before drain
and liveness rearm buffers. Retired-bot auto-replies and non-worker CD operations
retain their existing behavior; they do not create broker workers.

Drain waits for agents admitted to their first worker turn, not setup threads
waiting for a repository lock or a concurrency slot. First-turn admission and
drain-on share the scheduler lock, across all harnesses. Drain-on atomically moves
positively unstarted attempts back to durable scheduler pending, ahead of younger
pending/intake work. Row-capped subjects remain pending. Their raw events, buffered
input, route stamps and rerun/comment context survive restart as ordinary queued
work, never as `coalescer.recovery`. Already-admitted agents retain their normal
warm-turn and publication lifecycle.

Parked setup threads stop waiting without launching an agent. A temporary parking
tombstone prevents same-subject worktree reuse until setup cleanup finishes, even
if drain is disabled without restarting. These threads do not hold drain idle
open. A restart restores only their durable pending input, not the tombstone.

Intake returns success only after commit. An event response means accepted into
the queue, not that a route or worker has run. Queued `/rerun` returns 202 with its
queue ID. HTTP cannot inject an internal Zulip event; that rejection occurs before
queue admission. Resume applies the existing echo, route, provenance, comment
ownership and ticket-open guards. A now-ineligible event is handled by those guards,
just as ordinary intake is; drain never grants dispatch authority.

Immediate intake also commits an intake/dispatch intent before a detached launch
or assignment. Human mention assignments in durable mode run synchronously under
the admission lock, rather than completing intake when a daemon thread starts. The
existing assignment sender has a 30-second timeout; during that call other intake,
ticks, drain changes and health reads wait for the admission lock. A timeout or
unknown assignment result retains the intent and fails closed. A known success
commits its mention dedup receipt with queue completion. There is no new unbounded
retry or background handoff. Without a durable journal, the legacy asynchronous
mention behavior is unchanged.

A queued admin rerun can become busy, completed or otherwise ineligible before
dispatch. Its ordinary pre-effect 400/409 refusal is terminal and does not hold
unrelated intake. The latest known rerun result is recorded privately in the journal
as `last_rerun_result`, with queue ID, status and response body; this single receipt
is overwritten by the next known rerun outcome. Unknown outcomes still require
reconciliation.

With `DISPATCH_PRIORITY=fifo`, disabling drain resumes oldest intake first, one entry per scheduler tick. New
arrivals join the backlog until it is consumed. Queue-to-pending handoff and queue
removal share one transaction. Pending subjects retain first-admission order across
restart, including Zulip bodies, route stamps and rerun/comment context. There is
no age expiry for journaled work. A debounce window, same-subject live worker or
row cap holds the FIFO head; younger work cannot overtake it. Existing global
per-turn semaphore limits and row concurrency caps still apply. Completion order
can differ from launch order. TFA launch remains detached and retains its own live
session check and persisted burst TTL, rather than entering the CC concurrency cap.

Fresh subjects, including detached TFAs, wait behind an older pending FIFO head
even when the intake table is empty. A new event for the same pending subject can
still join its leading-edge burst. When a worker is retired, eligible buffered
inputs and row stamps transfer to pending in the same locked transaction that
removes its active intent. Close/settle callers receive a transfer receipt and
only alert; they do not enqueue those buffers again. A clean terminal ticket
completion still drops its terminal leftovers, and forward-only ticket comments
are never rearmed. Gmail and Zulip subjects retain their repeatable wake policy.

The default `DISPATCH_PRIORITY=reviews-first` instead admits a finite oldest-first
intake snapshot before scheduling. Reviews/re-review reruns and `continue-mr:`
fixes precede new tickets on their repo; other repos can progress past a capped
or debouncing lane. Same-repo pending/live follow-ups hold new work, not unrelated
repos. The committed-intent, drain, recovery and retirement safeguards above are
unchanged. See [dispatch priority](configuration.md#dispatch-priority) for fairness,
target-resolution limits and the strict-FIFO rollback setting.

An authenticated operator can make a one-shot [next-eligible selection](queue-priority.md)
from the Queue tab or `POST /move-to-front`, overriding normal ordering without
resuming drain or bypassing its safety guards.

The first journal startup imports legacy completed/pending files using their
existing validation/freshness rules, then leaves them untouched. Subsequent starts
use the journal. An imported signal-less pending route is still resolved under the
normal router. Rolling back to an older binary requires owner reconciliation: old
JSON files do not contain the new queue or drain flag.

## Health and maintenance assertions

`GET /health` keeps the existing `pending` (coalescer subjects) and `live_workers`
(agents admitted to a worker turn, including their warm/cleanup lifecycle) fields,
and adds:

| `drain` field | Meaning |
| --- | --- |
| `durable` | A durable journal is configured |
| `enabled` | Committed switch value |
| `queued` | Accepted intake awaiting admission, separate from `pending` |
| `dispatching`, `dispatching_ids` | Interrupted/ambiguous intake intents |
| `recovery_required`, `recovery_subjects` | Interrupted/ambiguous worker launches; settle each with `POST /reconcile` |
| `pending`, `live_workers` | Counts taken with the admission lock |
| `starting` | Setup attempts not yet admitted to their first turn, excluded from `live_workers` |
| `parked` | Pending subjects held by drain; after drain-off, setup tombstones still cleaning up |
| `idle` | No admitted worker agents remain; queued/parked subjects do not prevent idle |
| `error` | Latched state failure; service recovery required |
| `detached_tfas_tracked` | Always false in this version |

A healthy drain/backlog returns 200 and `ok == true`. A journal error, interrupted
intake or uncertain launch returns 503 and `ok == false`. Counts contain no bodies.
Storage failure never silently turns drain off. Fix storage and reconcile held
work before restarting; the error stays latched in the failed process.

For a planned restart require the requested drain acknowledgement, then repeated
positive health assertions: `ok == true`, `drain.durable == true`,
`drain.enabled == true`, `live_workers == 0`, `drain.dispatching == 0`,
`drain.recovery_required == 0`, and `drain.error == null`. Nonzero queued/pending
counts are expected during drain and are not reasons to reject an otherwise safe
coalesced-worker restart. Validate the same fields after restart before disabling
drain. Assert response JSON and exact values, not just an HTTP status.

`live_workers == 0` does not prove detached TFAs have finished. Their lifecycle is
outside the coalescer; `detached_tfas_tracked == false` makes this limitation
explicit. A deployment owner must account for detached TFAs separately and fail
closed if it cannot. This version does not publish a universal `drained` assertion.

## Unexpected interruption

Normal drain/restart preserves queued and pending work and resumes it once. A
process/power failure between a committed launch intent and the external spawn or
its completion cannot be classified as success/failure transactionally. The broker
keeps that record, reports 503, and pauses replay instead of guessing and duplicating
work. New accepted intake remains queued behind it. A dispatching intake record
similarly holds after a failure in ticket/route/handoff/TFA processing.

Set/retain drain. The service owner must inspect the reported item IDs/subjects,
establish whether their external effects occurred, and settle each held worker
launch with `POST /reconcile` (below) before resuming. There is deliberately no
automatic retry of uncertain side effects; the settle is an explicit decision, never
a silent clear. Do not delete the database or reset cursors as recovery. Exactly-once
external side effects across arbitrary process crashes are not claimed. Forwarded
events are retained in the uncertain worker intent for that reconciliation.

### Settling an interrupted launch (`POST /reconcile`)

An uncertain worker launch is held in `coalescer.recovery`, surfaced as
`drain.recovery_required` / `drain.recovery_subjects`. It is a **hard guard**: while
ANY entry exists, intake, the spawn tick and `/rerun` all refuse, so a single
ambiguous launch blocks *all* dispatch broker-wide. A restart is therefore a
**one-way door** into this state - the remedy the runbook documents for a wedge is
exactly what creates it. `POST /reconcile` is the supported in-band exit.

It reuses the existing admin identity (`RERUN_TOKEN_ENV`, header
`X-Broker-Rerun-Token`), like `/drain` and `/rerun`. Body:

```json
{"subject_key": "vikunja:task:42", "decision": "retry", "note": "<optional one-line reason>"}
```

`decision` is the owner's call, settled terminal for that one submission:

- `retry` - re-arm the retained intent as a fresh pending spawn. Use this once the
  owner has established the prior launch did NOT take effect (no MR opened, no
  external side effect), or accepts a duplicate.
- `abandon` - drop the intent and mark the subject completed, so it never re-spawns
  on its own. Use this once the owner has established the effect occurred, or it will
  not be retried. The retained events are preserved on the ticket.

Success is HTTP 200 with `ok == true`, the `outcome`, and the refreshed drain status.
An unknown subject is 409 and an unsupported decision is 400; both settle nothing and
leave the guard intact. An optional `note` must be a string: a non-string is 400 and,
because it is validated *before* any state moves, it leaves recovery untouched (no
half-applied settlement). Control characters in a note are flattened so it cannot forge
log lines. Without a durable journal the verb fails closed with 503. Each request logs
one `RECONCILE-REQ` audit line (key, caller, decision, result, ts).

```sh
curl -s -X POST 127.0.0.1:8787/reconcile \
  -H "X-Broker-Rerun-Token: $BROKER_RERUN_TOKEN" \
  -d '{"subject_key": "vikunja:task:42", "decision": "retry", "note": "no MR opened, safe to retry"}'
```

## Separate deployment follow-up

The infrastructure owner should implement a separate source MR that: authenticates
drain-on; waits with bounded retries on the explicit health predicates above;
keeps drain enabled on timeout/failure; upgrades/restarts only after the wait;
checks the new process and persisted flag; then authenticates drain-off and verifies
the returned boolean. Queue emptiness is not an idle predicate. Account explicitly
for detached TFAs; do not infer their completion from coalescer counts. Never unset
drain in an unconditional failure/cleanup handler. This document grants no runtime
or deployment authority.
