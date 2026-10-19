# n8n-nodes-eph-broker

Two n8n nodes for [eph-broker](../../README.md), for self-hosted n8n:

- **eph-broker Trigger** starts a workflow on a signed run event (`run.started`, `run.finished`, `run.escalated`,
  `mr.opened`). It checks `X-Eph-Signature` against the raw body, refuses events older than five minutes, can
  filter by event, and fires once per delivery id even when the broker retries.
- **eph-broker** looks things up: Get Run Status (`GET /runs/<subject_key>`) and Get Health (`GET /health`). It's
  read-only. There's no "start a worker" action, on purpose: give the broker work through your tracker.

They do the same job as the [example workflows](../../docs/n8n.md) without the Code nodes, so n8n doesn't need
`NODE_FUNCTION_ALLOW_BUILTIN` or `$env` access. Plain JavaScript, no build step and no dependencies.
Tested with n8n 2.43.

## Install

It isn't on npm. Copy this folder into n8n's custom extensions folder and restart n8n:

```sh
mkdir -p ~/.n8n/custom
cp -r integrations/n8n-nodes-eph-broker ~/.n8n/custom/
```

In Docker, mount it instead, for example
`-v ./integrations/n8n-nodes-eph-broker:/home/node/.n8n/custom/n8n-nodes-eph-broker:ro`.
If you keep extensions somewhere else, point `N8N_CUSTOM_EXTENSIONS` at that folder.

## Set up

1. In n8n, add an **eph-broker API** credential:
   - **Base URL**: where n8n reaches the broker, like `https://broker.example.com`.
   - **Events Secret**: the broker's `EVENTS_WEBHOOK_SECRET`. The trigger refuses everything while it's empty.
   - **Proxy Header Name / Value**: optional, for the authenticating proxy in front of `/runs` and `/health`
     (for example `Authorization` and `Bearer ...`).
2. Add an **eph-broker Trigger** to a workflow, pick the credential and the events you want, and activate the
   workflow.
3. Copy the trigger's production URL into the broker's `EVENTS_WEBHOOK_URL` ([events.md](../../docs/events.md)).

The trigger answers 401 for a bad signature or a stale event, 400 for a body that isn't an event, and 200 for
everything else, including events it filtered out or already saw. The broker doesn't retry a 4xx.

Delivery ids are remembered in memory, the last 500 per trigger, so restarting n8n forgets them. That's fine for
the broker's own retries, which come within 12 seconds. If a branch must never run twice across restarts, keep
your own record of handled ids.

## Tests

```sh
node --test test/*.test.js
```

`tools/check.sh` runs them when `node` is installed.
