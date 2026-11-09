# Drain review packet

This change is source-only and needs independent changed-risk acceptance before
integration or release. The builder is not the acceptor. No runtime service,
configuration, credential, signed prompt, release tuple or deployment was changed.
Source authentication and provenance rules are retained.

## Lineage

The source parent is accepted consumer
`3943a46ac3799f5de86ca192b2f7fe0acfcbdb5d`, including its Fleet/context consumer,
trackers, outbound events, configurable routing and Codex support. `dash.py` and
the Codex/Hermes harness modules are unchanged; common adapter context plumbing
is part of the drain delta. The earlier isolated delta on
`b37b38dcc49687e78d5dcab4e04033f8e283f97e` is retained at
`36913b9d7cd9f119f44d9e402ac9587c1015cac8`; it is not a release candidate.
Review the drain delta against the accepted consumer parent, not the old base.

## Verification on Python 3.9

All commands used a clean environment with only `/usr/bin:/bin` on PATH and the
synthetic fixture overlays. No bus identity, native model probe, worker or live
tracker was used. Dispatch, comments, ticket data and process launches were inert
stubs; subprocess adapter tests used their existing fake CLIs.

- `env -i PATH=/usr/bin:/bin python3 tests/test_drain.py`: rc 0, 22 passed, 0 failed.
- Untouched consumer and final change both ran
  `env -i PATH=/usr/bin:/bin BROKER_SITE_ENV=tests/fixtures/site.test.env sh tools/check.sh`:
  rc 1 on both, 587 broker checks passed, 5 failed, 1 skipped out of 593.
  Added failing IDs: none. Resolved failing IDs: none. The new 22-test drain suite
  passes before the broker suite in the changed gate.
- Supplementary scripts after the broker gate: `test_hermes`, `test_hermes_gate`,
  `test_codex` (fake CLI), `test_gate_providers`, `test_routes`,
  `test_routing_overrides`, `test_tickets`, `test_trackers`, `test_notify`,
  `test_events`, `test_config`: all rc 0. The ref-stamping fixture was updated to
  explicitly disable durable admission when constructing a Broker via `__new__`.
- `python3 tools/render-prompts.py --check`: rc 1 for the existing referenced-source
  drift. Signed prompts/hashes remain unchanged. The full gate stops at its known
  broker failures, so later suites were run separately; no native Codex probe or
  optional n8n test claim is made.
- `python3 -m py_compile` on changed Python modules and `git diff --check`: rc 0.

Unchanged failing broker IDs:

1. `test_diving_prompt_render_and_source_check`
2. `test_merge2_item8a_tfa_launch_env_is_scrubbed`
3. `test_mr_review_render_check_drift_guard_clean`
4. `test_phase2b_render_check_drift_guard_clean`
5. `test_phase2c_render_matches_committed_and_drift_guard_clean`

Four failures reflect already-drifted worker prompt sources. The shell environment
fixture failure also exists on the untouched consumer in this clean environment.

## Independent acceptance focus

Review all launch seams, including direct TFAs, internally emitted reviews, both
webhook paths, all supported trackers, Gmail, Zulip, comment/repo respawns, admin
reruns and liveness buffers. Verify that auth/provenance still runs, that HTTP cannot
persist an internal Zulip origin, and that the newer webhook body-size/author checks,
tracker ref derivation, event sink and race-safe stamp pruning remain intact.

Exercise the tests for persistent drain, planned restart, FIFO with fresh arrivals,
completed/comment/TFA dedup, row caps and liveness rearms, concurrent intake/ticks,
DM preservation/private mode, context transfer, transaction rollback, journal
failure, interrupted intake/launch holds, and truthful 503 health. Global per-turn
concurrency remains the existing adapter semaphore and is covered by the broker
suite. Review the coalescer/SQLite lock order and the commit before any queued
external dispatch or worker launch.

The policy is to defer new forwards during drain while already-buffered turns
finish. Private DM/comment bodies now persist in the journal and sensitive backups;
the measurement log remains redacted. Uncertain external effects are held for
owner reconciliation, never automatically replayed. Interrupted worker launches are
settled through the online `POST /reconcile` verb (an explicit owner decision, retry
or abandon), not by editing the database. Detached TFAs remain outside `live_workers`, explicitly reported by
`detached_tfas_tracked == false`; this is not a universal drained assertion.

## Focused follow-up to the independent review

The independent Full review of `3d4122e4a8c0b2fa247a69956409388495fcec7a`
requested changes on five transaction boundaries. The original 22 passing drain
checks did not close those findings. The revised delta preserves that reviewed
commit in its ancestry and requires focused independent acceptance of these fixes:

1. Immediate and queued intake now share a separately committed dispatch intent.
   A mocked detached launch followed by a real SQLite write rejection leaves a
   dispatching row after restart; an HTTP retry cannot repeat the launch. An
   interruption after launch and before its receipt also leaves the intent held.
2. Force removal, self-settle and clean close now transfer eligible leftover inputs
   with their row stamps in the same locked transaction that removes active state.
   Gmail/Zulip restart tests interrupt before caller rearm and inspect drain health
   plus a second SQLite connection at the removal boundary. A rolled-back transfer
   retains the original active intent for reconciliation. Caller receipts prevent
   double rearm; terminal ticket and forward-only comment drop policies remain.
3. Durable mention handoff waits for the assignment result instead of treating a
   daemon thread start as completion. Success and its TTL receipt commit with queue
   completion. Interruption before assignment, unknown outcome, and receipt write
   failure after assignment retain the dispatch intent. A successful assignment's
   receipt survives restart and suppresses a retry carrying an old assignee snapshot.
   The assignment runs under the admission lock with the existing sender's 30-second
   timeout. Intake, ticks, health and drain mutations wait during that call; this is
   an explicit latency tradeoff. The journal transaction remains open across it,
   but the dispatch intent was committed before opening that transaction. A timeout
   is an unknown outcome and is held; the legacy non-durable mode stays asynchronous.
4. Fresh detached TFA intake waits behind an older pending FIFO head even after the
   intake table empties. Same-subject pending events can still collect in their
   existing leading-edge burst.
5. Known pre-effect admin rerun 400/409 refusals commit a terminal result with queue
   removal and let unrelated intake continue. `last_rerun_result` retains only the
   latest known outcome, including its queue ID and status. Unexpected outcomes
   remain held. Tests include duplicate HTTP admin requests and a ticket that became
   done while its rerun waited in drain.

Focused commands, using Python 3.9 and synthetic fixtures:

- `env -i PATH=/usr/bin:/bin python3 tests/test_drain_boundaries.py`: rc 0,
  20 passed, 0 failed. Network, workers and senders are inert doubles; HTTP tests
  invoke real handler methods in memory without opening sockets. Before the fixes,
  the initial 16 regressions returned rc 1 with 12 failures reproducing all five
  finding classes and four policy controls passing. The four later cases extend
  caller, rollback, terminal-comment and unknown-rerun coverage.
- `env -i PATH=/usr/bin:/bin python3 tests/test_drain.py`: rc 0, 22 passed, 0 failed.
- Seventeen selected existing broker lifecycle, buffer, stamp-race, mention and
  rerun tests: rc 0, with process, network and real-sender boundaries explicitly
  denied. Legacy terminal completion, same-subject closing exclusion, repeatable
  Gmail wakes, comment drop policy and asynchronous mention behavior still pass.
- Changed Python compilation and `git diff --check`: rc 0.

The earlier five failing broker IDs above remain the retained Full-review baseline;
no duplicate Full gate or new unchanged baseline was run for this focused follow-up.
No new whole-suite failure-delta or green CI claim is made. Signed prompts, accepted
consumer modules, authentication, private journal and detached-TFA limits remain
under their original boundaries. Runtime and release acceptance remain separate.

## Release handoff

The infrastructure owner retains deployment and rollback ownership. A separate
Ansible source change must set drain, verify the committed boolean, wait with
bounded retries on the health predicates in [drain.md](drain.md), keep drain on
failure, restart/upgrade, verify the new process and persisted flag, then disable
drain and verify false. Nonzero durable queued/pending counts do not prevent a
safe coalesced-worker restart. Account for detached TFAs separately or fail closed.
The current source is not evidence of installation or a grant for any live action.
