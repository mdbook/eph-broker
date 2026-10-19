# Using eph-broker with n8n

n8n is a good fit on both sides of the broker: it can turn your tracker's webhooks into broker events, and it can
react to what workers do. This page walks through both, with workflows you can import as they are.

| Workflow | Direction | What it does |
|---|---|---|
| [n8n-vikunja-tap.json](n8n-vikunja-tap.json) | in | Vikunja webhook to a signed `POST /event` |
| [n8n-gitlab-issue-tap.json](n8n-gitlab-issue-tap.json) | in | GitLab issue webhook to `POST /webhooks/gitlab` |
| [n8n-run-events.json](n8n-run-events.json) | out | Receives signed run events and splits them by type |
| [n8n-run-status.json](n8n-run-status.json) | out | Looks up one run with `GET /runs/<subject_key>`, for other workflows to call |

If you run n8n yourself, there's also a trigger node and a lookup node that do the same as the run-events and
run-status workflows without Code nodes: see [Nodes for self-hosted n8n](#nodes-for-self-hosted-n8n).

Import one with "Import from File" in the n8n editor, then change `https://broker.example.com` to wherever your
broker is reachable from n8n (usually a reverse proxy in front of `127.0.0.1:8787`).

There's no way to start a worker from n8n directly. To give the broker work, have n8n create or label a ticket in
your tracker, and the tracker's webhook does the rest. That keeps the tracker as the one record of what was asked.

## What n8n needs

The Code nodes in these workflows use Node's `crypto` module and read secrets from the environment, so the n8n
process needs:

```sh
NODE_FUNCTION_ALLOW_BUILTIN=crypto
N8N_BLOCK_ENV_ACCESS_IN_NODE=false
EPH_BROKER_HMAC_SECRET=...      # same value as the broker's BROKER_HMAC_SECRET (the Vikunja tap)
EPH_EVENTS_SECRET=...           # same value as the broker's EVENTS_WEBHOOK_SECRET (run events)
VIKUNJA_WEBHOOK_SECRET=...      # optional: the secret you gave the Vikunja webhook
```

If you'd rather not open up `$env`, swap those reads for wherever you keep secrets in n8n. Either way, keep them
out of the workflow JSON.

## Sending tickets in

### Vikunja

In Vikunja, add a webhook on the project (Project, then Webhooks) pointing at the tap's production URL, for
example `https://n8n.example.com/webhook/vikunja-tap`, with at least the events `task.created`, `task.updated`,
`task.assignee.created` and `task.comment.created`. Give it a
secret and set the same value as `VIKUNJA_WEBHOOK_SECRET` in n8n.

The "Shape and sign" node turns Vikunja's payload into the event the broker reads (`source`, `event_name`,
`task_id`, `project_id`, `done`, `doer`, `labels`, `assignees`, plus `comment`, `comment_id` and `run_id` on
comments) and signs it with
`X-Broker-HMAC`. The signature covers the exact bytes sent, which is why the HTTP node sends a raw body instead of
letting n8n re-serialize it. If your Vikunja version shapes its payload differently, that node is the one place to
adjust.

The broker's own comments carry a `broker-run:<id>` marker. The tap copies it into `run_id`, which is how the
broker recognizes its own comments and ignores them ([agent-refire.md](agent-refire.md)).

### GitLab issues

GitLab can post to the broker without n8n: point a project webhook with "Issues events" at `/webhooks/gitlab` and set
its secret token to `GITLAB_WEBHOOK_TOKEN`. Use [n8n-gitlab-issue-tap.json](n8n-gitlab-issue-tap.json) when the
broker isn't reachable from GitLab and n8n is. It passes the body and token through unchanged.

## Reacting to runs

Turn on run events in the broker ([events.md](events.md)):

```sh
EVENTS_WEBHOOK_URL=https://n8n.example.com/webhook/eph-broker-events
EVENTS_WEBHOOK_SECRET=...       # in the broker's environment file
```

Then import [n8n-run-events.json](n8n-run-events.json) and activate it. "Check signature" drops anything that isn't
signed with your secret or is more than five minutes old, and "By event" sends each event to its own branch:

- `run.started` and `run.finished`: a worker started on, or let go of, a ticket. `outcome` says how it ended.
- `run.escalated`: something needs a human. `text` is the same message the operator gets.
- `mr.opened`: a code worker's change is up for review. `web_url` links to it.

Each branch ends in a placeholder node. Swap it for whatever you want: a chat message, an email, a row in a
spreadsheet, or a call to your tracker to move the ticket along. A redelivery reuses the event's `id`, so if a
branch must only ever fire once, remember the ids you've handled.

## Checking on a run

[n8n-run-status.json](n8n-run-status.json) is a small workflow that other workflows call with "Execute Workflow"
and a `subject_key`, like `vikunja:task:42`. It calls `GET /runs/<subject_key>` and hands back the broker's answer:
`status` is `pending`, `running`, `idle`, `closing` or `completed`, and an unknown subject comes back as `unknown`
([operations.md](operations.md#health-and-the-dashboard)). The node is set to never fail on a 404, so check
`status` instead.

`/runs` has no login of its own, same as the dashboard, so only expose it through the same authenticating proxy.
Add that proxy's credentials to the HTTP node.

## Nodes for self-hosted n8n

[integrations/n8n-nodes-eph-broker](../integrations/n8n-nodes-eph-broker/README.md) has two nodes you can drop into
n8n's custom extensions folder (no npm, no build):

- **eph-broker Trigger**: receives run events, checks the signature and age, filters by event and ignores a
  redelivery of an id it has already seen. It replaces [n8n-run-events.json](n8n-run-events.json)'s webhook and
  "Check signature" nodes.
- **eph-broker**: Get Run Status and Get Health. It replaces [n8n-run-status.json](n8n-run-status.json).

Both take one "eph-broker API" credential holding the base URL, the events secret and an optional proxy header,
so n8n needs neither `NODE_FUNCTION_ALLOW_BUILTIN` nor `$env` access for them. The taps that send tickets in stay
workflows: they're specific to your tracker and short.
