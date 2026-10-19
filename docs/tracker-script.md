# The tracker script (`BROKER_MCP_SH`)

The broker never talks to a ticket tracker's API itself. It runs a script you write, named by `BROKER_MCP_SH`,
to read a ticket and to post comments and labels. No script ships. Without one, no tracker ticket can start a
worker: the ticket fetch fails and the spawn is dropped. The GitLab issue path doesn't use this script.

The calls are shaped for [Vikunja](https://vikunja.io), because that's the tracker the broker grew up with.
Until native tracker adapters land ([trackers.md](trackers.md)), a script for another tracker pretends to be
Vikunja: it accepts the same tool names and answers in the same shapes. The tap that sends events in does the
same, see [Events your tap sends](#events-your-tap-sends).

This page describes the contract exactly as `poster.py`, `ticket_fetch.py` and `adapters/cc.py` use it.

## How the broker calls it

```sh
$BROKER_MCP_SH vikunja-bridge tools/call '<json>'
```

- Three fixed arguments and a JSON string: `vikunja-bridge`, `tools/call`, then the call itself. The broker
  sends nothing on stdin.
- `BROKER_MCP_SH` should be an absolute path. It runs as the broker's user, so it can read the tracker token
  from a file that user owns.
- Exit 0 and print one JSON object on stdout. A non-zero exit, a timeout or stdout that isn't JSON counts as a
  failure. Anything on stderr is logged (the first 500 characters).
- Timeouts: 60 seconds for a ticket fetch, 30 seconds for everything else.

The JSON argument is `{"name": <tool>, "arguments": {...}}`. Ids are always JSON integers.

| Tool | `arguments` | Used for | Needed? |
|---|---|---|---|
| `vikunja_task_crud` | `{"operation": "get", "id": 42}` | Reading a ticket before a worker starts | Yes |
| `vikunja_task_crud` | `{"operation": "list", "allTasks": true}` | A fallback read when `get` doesn't return the ticket | No |
| `vikunja_task_comments` | `{"operation": "comment", "id": 42, "comment": "<p>...</p>"}` | Posting the worker's summary and the broker's notes | Yes, or nobody sees a result |
| `vikunja_task_labels` | `{"operation": "apply-label", "id": 42, "labels": [7]}` | Adding `PARKED_LABEL_ID` when a worker comes back `blocked` | Only if you set `PARKED_LABEL_ID` |
| `vikunja_task_assignees` | `{"operation": "assign", "id": 42, "assignees": [13]}` | The external launcher row adding an assignee | Only for that row |

The comment body is HTML. The broker appends a marker `<!-- broker-run:<run_id> -->` to every comment it
posts. Keep it in the stored comment, because your tap copies it back into `run_id` and that's how the broker
knows the comment was its own.

## What the script prints

Success:

```json
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "..."}]}}
```

Only `result` matters. For comments, labels and assignees, any `result` object without `"isError": true` counts
as success. For a fetch, the ticket text is `result.content[0].text`.

Failure is any of these:

- a top-level `error` key, for example `{"error": {"code": -32602, "message": "bad id"}}`;
- `"isError": true` inside `result` (the shape an MCP tool error has);
- a missing or non-object `result`, or (for a fetch) empty `content` or empty `text`.

A failed comment or label is logged and skipped. The worker's turn still counts.

## The ticket text a `get` returns

`content[0].text` is a Markdown rendering of the ticket, one bold field label per line. The broker hands the
whole text to the worker as its brief, and reads a few fields out of it. Field labels are matched at the start
of a line and case-insensitively.

```text
**id:** 42
**title:** Fix the date parser
**description:** <p>repo:/srv/git/app</p><p>The parser drops the time zone.</p>
**done:** false
**project_id:** 3
**assignees:** [{"id": 7, "username": "eph-code"}]
**labels:** [{"id": 2, "title": "eph:code-change"}]
**created_by:** {"id": 5, "username": "alice"}
```

| Field | What the broker does with it |
|---|---|
| `**id:**` | Must equal the id it asked for. A missing or different id means the answer is thrown away. |
| `**title:**` | Must be there, or the answer is treated as "not a ticket" and the list fallback runs. Shown on the dashboard. |
| `**description:**` | The ticket body, as HTML. A code ticket's `repo:/absolute/path` line is read from here, on a line or `<p>` of its own. |
| `**done:**` | `true` drops the spawn quietly (the ticket is already closed). Anything else means open. |
| `**project_id:**` | Optional. Only the mention hand-off reads it. |
| `**assignees:**`, `**labels:**`, `**created_by:**` | JSON after the label. The broker reads every `"username": "..."` and every label `"title": "..."`. They decide who may re-run a ticket by comment or `/rerun`, and whether a `model:` or `hermes` label is trusted. |

The order matters, because a description written by a person could contain lines that look like fields:

- `**id:**` comes first.
- `**description:**` comes before `**done:**`, and appears at most once.
- `**done:**` appears exactly once.
- `**project_id:**`, `**assignees:**`, `**labels:**` and `**created_by:**` come after `**done:**`, at most once each.
  `**created_by:**` must be there and carry a `username`.
- Keep the whole text under 20,000 characters. The brief is cut there, and a text that long isn't trusted for
  the ownership fields.

If the ownership fields break these rules, the worker still starts. What fails closed is the trust check:
comment re-runs, `/rerun` and the `model:` and `hermes` labels are refused for that ticket.

## The list fallback (optional)

When the `get` text has no `**title:**`, or its `**id:**` doesn't match, the broker asks for the whole list
(`"operation": "list"`) and looks for the ticket in it. If your `get` always answers correctly, this call never
happens, and the script can answer it with an error.

If you do implement it, each ticket is a block that starts with a `### ` heading and ends where the next one
starts. Description is the last field in a block.

```text
### 1. **Fix the date parser** (ID: 42)
- **Status:** ❌ Not Done
- **Description:** <p>repo:/srv/git/app</p><p>The parser drops the time zone.</p>
```

`- **Status:** ✅ Done` marks a closed ticket. The fallback carries no ownership fields, so a ticket read this
way can't be re-run by comment.

## When a fetch fails

If the fetch fails, the broker waits about two seconds and tries once more. If that fails too, it doesn't start
the worker, logs `spawn ABORT (ticket fetch failed)` and sends an escalation. The ticket isn't marked as
finished, so the next event on it tries again.

## Events your tap sends

The script covers what the broker reads and writes. The tap covers what comes in: it turns your tracker's
webhooks into `POST /event` calls, signed as described in [security.md](security.md). The broker reads these
fields.

| Field | Value |
|---|---|
| `source` | `"vikunja"`, whatever your tracker really is. Any other value never becomes a tracker ticket. |
| `event_name` | Vikunja's names: `task.created`, `task.updated`, `task.assignee.created`, `task.comment.created`. A comment only feeds a running worker (or restarts a finished one, see [agent-refire.md](agent-refire.md)); the others can start one. |
| `task_id` | The ticket id as an integer. It's also the `id` the script gets. |
| `doer` | The username who caused the event. Agent and bot names listed in `config/provenance.json` are dropped. |
| `labels` | A list of label titles. |
| `assignees` | A list of usernames. Routing matches on these two lists. |
| `done` | `true` when the ticket is closed. |
| `project_id` | The ticket's project id (only the mention hand-off uses it). |
| `comment`, `comment_id` | On comment events: the comment HTML and its integer id. |
| `run_id` | On comment events: the id from the comment's `broker-run:<id>` marker, if it has one. |

[n8n.md](n8n.md) has an importable tap that builds exactly this event from Vikunja's webhook.

## A note on security

Workers run as the same user as the broker, so a worker with shell access could run this script too. The deny
rules for the general worker name `mcp.sh` (bare, and under any directory), so naming your script `mcp.sh`
keeps it on that list. That's a speed bump, not a boundary; see
[security-residual-same-uid.md](security-residual-same-uid.md).
