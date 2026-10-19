# Re-firing a ticket as an agent (svc-agent events and the echo-guard)

This is an advanced page. It only matters if your own agents write to the tracker and want the broker to
pick tickets up.

An agent that writes to Vikunja through a shared service account (here called `svc-agent`) often wants
the broker to pick a ticket up again. Touching the ticket (a priority bump, an edit through the API or a
connector) usually does nothing, while replaying an old webhook sometimes works and logs
`echo-guard ADMIT agent handoff`. This page explains why, based on the code as of this commit.

**Short answer:**

- An agent **can** fire a ticket that the broker has **never run**. The svc-agent event has to pass the
  handoff predicate below. The dependable way to do that is to (re)assign the row's assignee (`eph-code`,
  `eph`, or the launcher and infra assignees when you set them).
- An agent **cannot** re-fire a ticket that has **already completed a run**. There is no supported way
  to do it. The coalescer's completed-dedup drops every svc-agent event for that subject, whatever the
  echo-guard decided. Only a human (a `user`-provenance doer) can re-arm it.

## 1. Which svc-agent events the echo-guard admits

`echo_guard.EchoGuard.is_echo` drops every event whose doer is a known agent or bot account (`provenance.classify(doer)` is
`agent` or `system`; `svc-agent` is always `agent`). There is exactly one exception, the **agent
handoff**. It is admitted only when **all** of these hold:

| # | Condition | Where |
|---|-----------|-------|
| 1 | No `run_id` on the event that belongs to an active run (a stamped side-effect is always an echo) | `echo_guard.is_echo` |
| 2 | `doer` is in `_HANDOFF_DOERS` (case-insensitive): `svc-agent` or an exact Vikunja username from your configured agent roster (for example `agent-a`, `agent-b`). `-bot` / bus-address forms and `pool-NN` never qualify | `provenance.is_agent_handoff` |
| 3 | `source == "vikunja"` and `event_name` is `task.created` or `task.updated`. **Comments (`task.comment.created`) never qualify** | same |
| 4 | `task_id` is present, the event has **no `run_id` at all** (a stale one counts too), and `done` is not `true` | same |
| 5 | None of the event's `labels` (titles) is in `handoff_parked_labels` (config/provenance.json; e.g. `awaiting-input`, `deferred`, `blocked`) | same |
| 6 | The event's **own `assignees` list contains a dispatch-row assignee**: `DEV_ASSIGNEE` (default `eph-code`), `FLEET_ASSIGNEE` (`eph`), `FLEET_ASSIGNEE_ALT` or `INFRA_ASSIGNEE` (both off by default) | same |
| 7 | No worker is in flight on `vikunja:task:<id>` (`RunRegistry.has_subject`) | `echo_guard.is_echo` |
| 8 | `Router.classify` routes the event to the row its assignee dispatches to (`code` for `DEV_ASSIGNEE`, `general` for `FLEET_ASSIGNEE`, `tfa` (the external launcher row) for `FLEET_ASSIGNEE_ALT`, `infra` for `INFRA_ASSIGNEE`, or `infra/general` for `INFRA_ASSIGNEE` plus the `infra:general` label; `routing.assignee_dispatch_domains`). A narrow label row (ansible, wiki, ...) that wins first-match, or an existing route stamp for another row, makes it drop | `echo_guard.is_echo` |

If any of these fails, the event is logged as `echo-guard DROP` and counted in `echo_dropped`.

Condition 6 reads the **event payload**, not the live ticket. If a `task.updated` webhook from the tap
does not carry the ticket's assignees, the event drops even when the `DEV_ASSIGNEE` user is assigned in
Vikunja. The tap is
outside this repo. To check what a given edit actually sent, look at the raw event in the broker's
events log.

## 2. Passing the echo-guard is not enough: completed-dedup

An admitted event then goes through `Broker.ingest`, then `Router.classify`, then `Coalescer.on_event`. In
`on_event`, a subject with no live worker that is already in `Coalescer.completed` (it finished a real
run: success, failure, or a turn that raised after the worktree cut) is re-armed **only if
`provenance.is_user(doer)`**, meaning a human. For `svc-agent`, the result is `DEDUP-DROP`. A pending
spawn for a completed subject is dropped again in `tick()` (`DEDUP-DROP (pending)`).

`completed` is persisted (`COMPLETED_PATH`, or `completed.json` next to `EVENTS_LOG`), so restarting the
broker does not clear it. The one exception is a spawn that was rejected before it ran (for
example, no `repo:` line). That spawn is **not** marked completed, so the subject stays re-fireable.

This is why replaying old webhook events can work while a fresh touch does not. A replay is admitted if
its payload meets conditions 1 to 8 **and** the subject has not completed a run yet.

## 3. The supported way for an agent to (re-)fire a ticket

**Ticket that has not completed a run** (never dispatched, or only rejected before spawn):

1. Make sure the ticket is open and has none of the `handoff_parked_labels` (e.g. `awaiting-input` / `deferred` / `blocked`).
2. Make sure no narrow row label (ansible / wiki / ...) would route it away from the row its assignee
   dispatches to (condition 8).
3. Make sure no worker is running on it right now.
4. Emit an svc-agent `task.created` or `task.updated` whose payload lists the row's assignee (`eph-code`,
   `eph`, or the launcher or infra assignee; condition 6). The most dependable way is to **assign the row
   assignee**: create the ticket with it as assignee, or unassign it and assign it again. The resulting
   event carries it in `assignees`. This is also the path the mention handoff relies on
   (`poster.assign_user` writes as svc-agent and the handoff path admits the resulting `task.updated`).
5. Confirm it worked: the broker logs `echo-guard ADMIT agent handoff ... task=<id>` and then
   `ROUTE key=vikunja:task:<id> decision=SPAWN(armed)`.

A cosmetic edit such as a priority or description touch counts only if its webhook payload includes the
row assignee (condition 6). Do not rely on it.

**Ticket that already completed a run: there is no supported agent path.** Every svc-agent event for it
is `DEDUP-DROP`ped. The broker has no re-run or DEDUP-clear endpoint: `POST /event` and `/webhooks/gitlab`
both go through the same ingest, and the dash is read-only. What re-arms it:

- A **human** (for example you) edits or re-assigns the ticket in Vikunja under their own account. That is
  a `user` doer, so it produces `DEDUP-CLEAR`.
- With `REPO_INFER_COMMENT_RERUN_ENABLED` and `REPO_INFER_ENABLED` on, a **human** comment on a ticket
  that the repo ladder **parked** re-arms one run. This does not help an agent either: agent comments
  are dropped.

An agent that needs a completed ticket re-run should ask a human to touch it, or open a new ticket that
has never been dispatched.

Do not work around this. Do not stamp fake doers, replay stored webhook payloads, or post to `/event`
directly. The guard is there to keep a worker's own svc-agent writes from re-spawning it in a loop.

## 4. Comment-respawn

A **human comment** can also re-spawn a ticket whose last worker has spun down
(`app.Broker._comment_respawn`, generalizing the repo-parked rerun above). It applies when:

- The comment is on an **OPEN** ticket that has **no live worker** for its key (the last worker spun down).
- The commenter is a user listed in `REPO_INFER_RERUN_USERS` or the ticket's **creator**. Agent and
  self-retrigger comments are dropped (same provenance guard as comment forwarding).
- The ticket routes by **assignee** (`DEV_ASSIGNEE` -> code, `FLEET_ASSIGNEE` -> general, `INFRA_ASSIGNEE` -> infra or
  infra/general) or by an `eph:`-prefixed **label**
  (`eph:code-change`, `eph:general`); see `routing.comment_respawn_domains`. The respawn uses the
  **same row** the ticket routes to, and the ticket must still be open and classify to that same row at
  respawn time.
- **The external launcher row (`FLEET_ASSIGNEE_ALT`) is excluded**: it hands the ticket to a long-lived agent
  started outside the coalescer, so there is nothing to "spin down" and respawn.

Each `comment_id` respawns at most once. The comment text is queued into the next spawn's ticket text so
the worker sees what woke it. Log line: `COMMENT-RESPAWN key=... domain=...`.

It is **on by default**. Roll it back with `COMMENT_RESPAWN_ENABLED=0`.

This path is human-only. It does not give agents a re-fire route.
