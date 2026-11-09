# Move a waiting subject to the front

On the dashboard **Queue** tab, an authorized operator can choose **Move to front**.
The table includes deferred intake, coalescer-pending bursts and admitted workers
waiting for a global turn slot. Read-only viewers do not get the control.

For automation, `POST /move-to-front` accepts:

```json
{"subject_key": "vikunja:task:42"}
```

Use the existing operator `X-Broker-Rerun-Token` identity (secret variable name
configured by `RERUN_TOKEN_ENV`, default `BROKER_RERUN_TOKEN`). Missing, wrong or
unconfigured authentication returns 401, before parsing/mutation. Browser requests
use `POST /actions/move-to-front` with the same body and the existing configurable
`BROKER_ACTIONS_*` proxy-secret, SSO group and same-origin guards. Web actions remain
off by default; this adds no new credential or site-specific role.

Success is 200 with `ok`, `subject_key` and `next: true`. Invalid keys/JSON return
400; a subject not pending or waiting for a slot returns 409 without changing the
selection. In particular, running, idle, ended and repository-lock-only workers
cannot be selected. Operator requests log `MOVE-TO-FRONT`; browser requests also
use the normal `WEB-ACTION` audit and `action.taken` event.

There is **one next-eligible selection**: the latest successful choice replaces the
previous one. It precedes reviews-first/repo fairness or FIFO ordering for pending
admission, and FIFO at the global `CONCURRENCY` turn-slot gate. Already-running
turns are never interrupted. The choice is consumed by the selected subject's
next slot acquisition; it does not give all its future warm turns priority. Other
slot waiters retain their FIFO positions, including across cancellation polling.

A ready pending spawn reserves the next slot through setup so an already-admitted
slot waiter cannot simply overtake it. A blocked repository-lock acquisition defers that
reservation: work holding the required repository must be allowed to progress.
The selected subject retains preference once it reaches the slot gate. Debounce,
row caps, same-subject single-flight, drain, recovery, ambiguous-intake and all
normal intake/spawn authorization guards still apply. A selection does not create
work, resume drain, or raise concurrency. Blocked pending subjects do not reserve
free slots, and setup rejection releases its reservation.

The selection is **process-local**, not durable: restart restores the normal queue
and its configured policy, not the operator override. It is displayed as
`next_subject` in `/state` and **Next eligible** on the Queue tab. The rest of the
table is a backlog view, not a promise of exact start order (setup, repo locks and
caps can delay a subject). Detached TFA launches are outside the global turn cap.
