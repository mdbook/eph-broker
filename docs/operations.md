# Operations

Day-to-day running: where the logs go, what to reload and how, what the dashboard shows, and how to re-run a
ticket by hand. Installation is in [install.md](install.md).

## Logs

The broker logs to stderr, one line per event, as `<timestamp> <message>`. Under the shipped systemd unit
that's the journal.

```sh
journalctl -u eph-broker -f
```

Most lines start with an upper-case tag, so they're easy to grep for.

| Tag | Meaning |
|---|---|
| `INGEST` | an event was accepted or rejected at the door |
| `echo-guard DROP` / `echo-guard ADMIT` | an agent-authored event was dropped, or let through as a handoff |
| `ROUTE key=... decision=...` | what the coalescer did with an event: spawn, forward, collect or drop |
| `SPAWN` | a worker is starting |
| `DEDUP-DROP` / `DEDUP-CLEAR` | an event for an already-finished ticket was dropped, or a human re-armed it |
| `TURN-CAP`, `HARD-KILL` | a worker hit its turn cap, or was killed for running too long |
| `COMMENT-RESPAWN` | a human comment restarted a ticket |
| `OPS-RERUN` | a `/rerun` request, accepted or refused |
| `HMAC-CHECK` | the signature result for a `/event` call, in `log` and `enforce` modes |

Escalations (an unparseable worker result or a refused start, for example) also go to `OPS_HANDLE` through
the channel `NOTIFY_BACKEND` picks (webhook, email or zbus; see
[configuration.md](configuration.md#notifications)). Without a working channel they're only in the log.

## State on disk

| File | What it is |
|---|---|
| `EVENTS_LOG` | Every event that passed the echo guard and the routing check, one JSON object per line with `ts` and `subject_key`. Useful for seeing exactly what your tap sent. |
| `completed.json` | Tickets that already finished a run. This is what stops agents from re-firing them. |
| `pending.json` | Workers armed but not yet started, so a restart doesn't lose them. Entries older than an hour are dropped on load. |

The last two live next to `EVENTS_LOG` when you set `EVENTS_LOG` explicitly, which you should (the shipped
unit uses `/var/lib/eph-broker/`). Worktrees go under `WORKTREES_ROOT` and per-row directories such as
`~/.cache/eph-general-worktrees`, and are removed when the worker exits.

## Reloading config

`SIGHUP` re-reads every `config/*.json` except the `*.example.json` templates, which the broker never reads.

```sh
systemctl kill -s HUP eph-broker
```

| File (or broker.yaml section) | On `SIGHUP` |
|---|---|
| `windows.json` | Applies right away |
| `zulip-wake.json`, `pool-vikunja-ids.json` | Applies right away |
| `cd-tier.json`, `deploy-paths.json` | Applies right away |
| `approved-prompts.json` | Read at every worker start, so no reload or restart needed |
| Everything else: `constraints.json`, `routes.json`, provenance, inbox accounts, `config/site.env` and the environment | Restart the broker. For a broker.yaml section, the reload logs which ones changed |

A bad edit never takes the broker down. A file that can't be read or parsed, or a `windows` value that isn't a
number of seconds, is logged as an error and the broker keeps the last good copy of that file. A broken
broker.yaml keeps the whole previous broker.yaml. If the file was already broken at startup, there's no good copy
to keep, so the broker starts without it.

## Health and the dashboard

`GET /health` returns counters as JSON: pending and live workers, events dropped by the echo guard, events
rejected by routing, deduplicated events, the HMAC mode with its pass and fail counts, and the run event
counters ([events.md](events.md)). A `zulip` block with the listener counters is there only when
`ZULIP_WAKE_ENABLED` or `ZULIP_AUTOREPLY_ENABLED` is on.

The dashboard is at `/dash.html` (a bare `/` redirects there). `/dash` and `/state` return the same data as
JSON. It's read-only and has no login, so put it behind your proxy's auth.

`GET /runs/<subject_key>` answers for one ticket or chat, for scripts and n8n that want to check on a run. URL-encode
the key (`github:owner/repo#45` is `/runs/github:owner%2Frepo%2345`, since a bare `#` never reaches the server).

```json
{"ok": true, "subject_key": "vikunja:task:42", "status": "running", "row": "code",
 "live": {"key": "vikunja:task:42", "state": "open_running", "forwards": 0, "events": 1, "buffered": 0, "expire_in": 41.2},
 "pending": null,
 "worker": {"key": "vikunja:task:42", "run_id": "0f3c9a1e", "session": "5b1f0c2e", "turns": 3, "tokens": 48211,
            "forwards": 0, "in_turn": true, "done": false, "turn_age": 12.4},
 "completed_at": null}
```

`status` is `pending` (collecting a burst before the worker starts), `running` (in a turn), `idle` (warm, waiting
for follow-ups), `closing`, or `completed` (finished and not started again since). `row` is the routing row while
the subject is pending or live. A subject the broker doesn't know gets a 404 with `status: unknown`, and a key that
isn't printable ASCII or is over 512 characters gets a 400. `completed` is remembered for the most recent
subjects only (the same list that stops a closed ticket from starting again), and it survives a restart when
it's persisted (`COMPLETED_PATH`, or `completed.json` next to `EVENTS_LOG`). Like the dashboard it has no login, so it sits behind the same proxy.

- The Workers tab lists running workers and recent sessions. With `BROKER_STREAM_ENABLED` on, you can open a
  running worker's live transcript or replay a past one (Claude Code harness only).
- The Fleet tab is optional and only useful if you also run long-lived agents with their own status file; see
  [configuration.md](configuration.md#the-dashboard).

## Finding the result of a GitLab issue

The broker comments on the issue with the worker's summary after each turn, `blocked` reasons included, and
says so when it can't start a worker because the project isn't in `ISSUE_REPO_MAP`. The MR quotes the same
summary and closes the issue when it's merged. Comments need `GITLAB_ISSUE_TOKEN` or `GITLAB_MR_TOKEN`; with
neither, nothing is posted.

A few things still only show up off the issue: a worker that crashed or returned no result, and the reviewer's
verdict (a note on the MR). For those, look at `/runs/<key>` (for example `/runs/gitlab:group%2Fapp%237`), the
dashboard, the broker's log, and the `run.finished` and `run.escalated` run events ([events.md](events.md)).

## Re-running a ticket

`/rerun` and comment re-runs work for tracker tickets only. To run a GitLab issue again, open a new issue.

A ticket that finished a run won't start again from agent activity. A human can edit or reassign it in the
tracker, or comment on it ([agent-refire.md](agent-refire.md) has the rules). You can also call `/rerun`
yourself.

```sh
curl -s -X POST 127.0.0.1:8787/rerun \
  -H "X-Broker-Rerun-Token: $BROKER_RERUN_TOKEN" \
  -d '{"task_id": 42, "note": "The config file is in /etc/app, try again"}'
```

The ticket has to be open, readable through `BROKER_MCP_SH`, and routed to a row that supports re-runs (the
code, general and infrastructure assignee rows, or the `eph:` labels). Otherwise, or if a worker is already
live or pending on it, the call returns 409 and arms nothing. The note is passed to the new worker along with
the ticket.

## Other harnesses and the CD module

- [hermes.md](hermes.md) covers running workers on hermes-agent instead of Claude Code, including the venv
  check that fails closed after a careless `uv sync`.
- [adapters.md](adapters.md) covers how a route picks its harness and what adding another one takes.
- [cd-deploy-paths.md](cd-deploy-paths.md) covers auto-merge and the deploy hand-off, both off by default.

## Upgrading

After pulling a new version, run `sh tools/check.sh`. If any module or manifest changed, the prompt hashes
change too and those classes stop starting until you read the new prompts and sign them
([prompts.md](prompts.md#signing)). Your `modules/site/` overrides are left alone.

The RPM owns every file it ships under `/opt/eph-broker`, and an upgrade replaces them. That includes
`approved-prompts.json` and the `config/*.json` files the package ships (`constraints.json`, `windows.json`,
`cd-tier.json` and the others), so an edit to one of those is lost on `dnf upgrade`. Keep your own copies
outside the package and point the broker at them: `APPROVED_PROMPTS` for your signatures, `CONFIG_DIR` for the
JSON config (a directory of your own with every `config/*.json` you use), `BROKER_SITE_ENV` for `site.env`. Files the package doesn't ship, such as `config/site.env`,
`config/provenance.json` and `config/routes.json`, are left alone.

### Upgrade notes

- The GitLab webhook endpoint is now `POST /webhooks/gitlab`. The old path, `POST /forge/gitlab`, still works
  for one more release and logs a `DEPRECATED POST /forge/gitlab` warning on every call. Point your GitLab
  project webhook (or your forwarder) at the new path before then. The token check and everything after it are
  the same on both paths.
