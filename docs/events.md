# Run events

The broker can POST a signed JSON event to a URL of yours whenever a worker starts, finishes, escalates or opens
a merge request. Point it at an n8n webhook (or anything else that takes a POST) and you can react to runs
without polling.

It's off until you set `EVENTS_WEBHOOK_URL`. [n8n.md](n8n.md#reacting-to-runs) has a receiver workflow to import.

## Setup

| Setting | What it does | Default |
|---|---|---|
| `EVENTS_WEBHOOK_URL` | Where events go. Has to be `https`, or `http` to localhost. | Off |
| `EVENTS_WEBHOOK_SECRET` (`EVENTS_WEBHOOK_SECRET_ENV`) | The signing secret. Required: with it unset nothing is sent. | None |
| `EVENTS_WEBHOOK_KINDS` | Comma list of the events you want. An unknown name stops the broker. | All four |

Put the secret in the unit's environment file like the other secrets ([configuration.md](configuration.md#secrets)),
not in `site.env`.

## What gets sent

Every event is one `POST` with a JSON body and these headers:

| Header | Value |
|---|---|
| `X-Eph-Event` | The event name, same as `event` in the body |
| `X-Eph-Delivery` | The event id, same as `id` in the body. A retry reuses it. |
| `X-Eph-Signature` | `sha256=<hex HMAC-SHA256 of the raw body, keyed by the secret>` |

Every body has `v` (always 1 for now), `id`, `event`, `ts` (unix seconds) and `subject_key` (the ticket or chat
the run is about, like `vikunja:task:42` or `github:owner/repo#45`). The rest depends on the event:

| Event | When | Extra fields |
|---|---|---|
| `run.started` | A worker was started for a subject | `events` (how many events it was started with), `source`, `row` (the routing row) |
| `run.finished` | The subject's worker is gone | `outcome`, `events`, `forwards` (follow-ups sent to the live worker) |
| `run.escalated` | An escalation went to the operator | `text`. Here `subject_key` is the escalation's own key: usually the subject's key, sometimes `escalate:<ticket id>`, or `auto:<hash>` when the caller had none |
| `mr.opened` | A code worker's branch got its merge request | `run_id`, `web_url`, `iid`, `project`, `branch`, `target` |

`outcome` on `run.finished` is one of:

- `closed`: the worker went idle and was shut down normally.
- `exited`: the worker exited on its own (turn done, out of forwards, or it crashed after it started working).
- `rejected`: the worker was refused at spawn and never ran (no allowed repo, unknown account, and so on).
- `force_closed`: the liveness check killed a stuck worker. The subject can start again.

An example:

```json
{"v":1,"id":"5b1f0c2e9a7d4c3b8e6f1a2d3c4b5a69","event":"mr.opened","ts":1760800000,
 "subject_key":"vikunja:task:42","run_id":"0f3c9a1e","web_url":"https://gitlab.example.com/acme/app/-/merge_requests/7",
 "iid":7,"project":"acme/app","branch":"eph/code-change/vikunja_task_42-0f3c9a1e","target":"main"}
```

`run.escalated` follows the same rate limit as the operator message, so a flapping subject sends at most three
in five minutes. Escalation text is written for the operator and can include ticket titles and repo paths,
so treat the receiving end as private.

## Verifying the signature

Check it against the raw body, before you parse it. Also drop anything with an old `ts` or an `id` you've already
seen, since both are inside the signed body.

Python:

```python
import hashlib, hmac

def verify(secret: str, body: bytes, header: str) -> bool:
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want.encode(), (header or "").encode())
```

In n8n, turn on the Webhook node's "Raw Body" option and follow it with a Code node. The Code node needs
`NODE_FUNCTION_ALLOW_BUILTIN=crypto`, and reading the secret from `$env` needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`
(or paste it into an n8n credential instead):

```javascript
const crypto = require('crypto');
const item = $input.first();
const raw = await this.helpers.getBinaryDataBuffer(0, 'data');
const want = 'sha256=' + crypto.createHmac('sha256', $env.EPH_EVENTS_SECRET).update(raw).digest('hex');
const got = item.json.headers['x-eph-signature'] || '';
if (want.length !== got.length || !crypto.timingSafeEqual(Buffer.from(want), Buffer.from(got))) {
  throw new Error('bad X-Eph-Signature');
}
return [{ json: JSON.parse(raw.toString()) }];
```

## Delivery

Sending happens on its own thread, so a slow receiver never holds up a worker. A 5xx or a network error is retried
twice (after 2 s and 10 s) with the same `id`. A 4xx or a redirect fails straight away and redirects are never
followed. If more than 1000 events pile up the newest are dropped and logged.

Nothing is stored on disk, so events queued during a restart are lost. Use them to trigger things, and use the
tracker or `/dash` as the record of what happened.

`/health` has an `events` block with `enabled`, `pending` and counts for `queued`, `sent`, `failed`, `dropped`
and `unsigned`.
