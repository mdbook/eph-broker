# Operations

For a planned restart, use the authenticated [durable drain](drain.md): enable it,
wait on the documented health values while current workers finish, restart with
the same journal, verify the persisted flag, then disable it. New intake waits
durably. Queued/pending work may be nonzero during a safe coalesced-worker restart;
detached TFAs need separate accounting. This workflow requires the drain-capable
version and configured persistent storage.

If a restart leaves `drain.recovery_required` nonzero (an interrupted worker launch
was held), dispatch is blocked broker-wide until you settle it with
`POST /reconcile` (see [drain.md](drain.md#settling-an-interrupted-launch-post-reconcile)).
A restart is a `one-way door` into that state: the remedy the runbook gives for a
wedge is exactly what creates it. Note also that `Restart=on-failure` does NOT
restart the unit after a clean SIGTERM, so killing a stuck broker leaves the unit
`inactive (dead)` and needing a manual start.

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
| `RECONCILE-REQ` | a `/reconcile` request settling an interrupted launch, accepted or refused |
| `WEB-ACTION` / `WEB-ACTION REJECT` | a dashboard action: who, what, on which run, and the result (or why it was refused) |
| `WORKER CANCEL` | a run was cancelled from the dashboard |
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

`status` is `pending` (collecting a burst before the worker starts), `waiting` (queued on a repo lock or
the global turn cap; `live.waiting` names the reason), `running` (in a turn), `idle` (warm, waiting
for follow-ups), `closing`, or `completed` (finished and not started again since). `row` is the routing row while
the subject is pending or live. A subject the broker doesn't know gets a 404 with `status: unknown`, and a key that
isn't printable ASCII or is over 512 characters gets a 400. `completed` is remembered for the most recent
subjects only (the same list that stops a closed ticket from starting again), and it survives a restart when
it's persisted (`COMPLETED_PATH`, or `completed.json` next to `EVENTS_LOG`). Like the dashboard it has no login, so it sits behind the same proxy.

The dashboard has Now, Runs, Queue and Fleet views plus a full Settings page. Desktop navigation keeps
Settings in the header gear beside the theme button. On phones, the hamburger contains every view,
Settings and any configured viewer modules. Escape closes the menu.
Underlined sub-tabs keep their selection in the URL hash for reload, back and forward. Use Tab to reach
the selected sub-tab, then Left/Right or Home/End to change it.

- **Now** keeps the count strip and Needs you above Running / Queue / Recent sub-tabs
  (`#now/running`, `#now/queue`, `#now/recent`). Queue counts link to the full Queue view. Its short preview
  shows up to three items per stage with ticket, available title, source, age and held reason. Titles come
  from intake metadata or known sessions; missing titles and legacy ages are not guessed. Broker counters
  sit behind **Details**; the raw JSON is at `/state`.
- **Runs** lists running, waiting and finished runs, with search and a details panel. A run opens at
  `#run/<key>` with its live transcript (needs `BROKER_STREAM_ENABLED`), or a past session's replay. An ended
  run keeps its title, model, token total and how long it ran.
  The transcript shows the conversation by default. **Everything** adds the behind-the-scenes steps
  (session and turn bookkeeping, harness events) and the run's technical fields (backend, step count,
  session id). Nothing on the page shows raw JSON: structured values render as tables or an indented outline,
  and a Codex run reads as an activity feed: its messages, each command it ran (a failed one flagged with its
  exit code), the files it edited, and "Turn finished · 12k tokens read" between turns.
  **Running** only ever means a real worker. A subject queued on a repo lock or turn slot shows **Waiting** with
  the reason, and setup before a worker is registered shows **Starting up**. Repo lock scope is
  [configurable per route](routing-overrides.md).
- **Queue** is the full queue and control surface, with Next up / Waiting to start / By source sub-tabs
  (`#queue/next`, `#queue/waiting`, `#queue/source`). Next up is arrived but not admitted, including events
  still coalescing. Waiting to start is admitted without a worker, with each item's blocker in words.
  Each launch appears in only one stage. Follow-up work for a ticket whose previous worker is closing,
  closed or parking stays in Next up, labelled as waiting for the previous worker to tear down (or for
  draining to end). Durable intake requires `BROKER_STATE_PATH`.
- Set `DASH_INSTANCE_NAME` to label the page (for example `staging`), and `TICKET_URL_TEMPLATE` to an http(s)
  URL to get **Open ticket** links.
- **Fleet** is optional and only useful if you also run long-lived agents with their own status file; see
  [configuration.md](configuration.md#the-dashboard). It contains the agent table, not worker settings.
- **Settings** has Workers (`#settings/workers`) for harness/backend/model rows, Groups
  (`#settings/groups`) for Max at once limits, and Modules (`#settings/modules`) for viewer state and
  authorized child-module reloads. Worker and group editing still requires dashboard action access.
  Module disable/enable uses the existing configured disabled-file marker, not a new browser action.

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
the ticket. Add `"model": "opus"` (any name a `model:` label accepts) to run just this run on that model; a row that
pins its model refuses it with 409.

## Settling an interrupted launch (`/reconcile`)

A restart between a committed worker launch and its external effect leaves the launch held in
`coalescer.recovery`, reported as `drain.recovery_required` / `drain.recovery_subjects`. It is a hard guard:
while any entry exists, intake, the spawn tick and `/rerun` all refuse, so one ambiguous launch blocks all
dispatch. `POST /reconcile` is the supported exit (same `X-Broker-Rerun-Token` as `/rerun` and `/drain`).

```sh
curl -s -X POST 127.0.0.1:8787/reconcile \
  -H "X-Broker-Rerun-Token: $BROKER_RERUN_TOKEN" \
  -d '{"subject_key": "vikunja:task:42", "decision": "retry", "note": "no MR opened, safe to retry"}'
```

Decide from the evidence: `retry` re-arms the retained intent (the launch did not take effect, or you accept
a duplicate); `abandon` drops it and marks the subject completed so it never re-spawns on its own. An unknown
subject is 409 and a bad decision is 400; both leave the guard intact. An optional `note` must be a string (a
non-string is 400 and, being validated before any state moves, leaves recovery untouched). It is a decision,
never an automatic clear - the durable ambiguous state is deliberate, so nothing here ever duplicates an
ambiguous prior launch on its own. Full detail in
[drain.md](drain.md#settling-an-interrupted-launch-post-reconcile).

## Continuing an open MR (fix rounds)

A code or general ticket normally gets a worktree cut from the target branch, and its commits land on a fresh
feature branch with a new MR. To make a fix round update an MR the broker already opened, add a bare line
`continue-mr: <branch>` to the ticket description (its own line, parsed like the `repo:` line). The broker then
cuts the worktree from that branch and pushes the worker's commits back onto it, so the existing MR updates in
place and no new MR is opened. The push is a plain fast-forward push, never forced: if the branch moved after the
cut, the push is rejected and `OPS_HANDLE` is alerted. Only branches under `CONTINUE_MR_PREFIXES` (default `eph/`,
the namespace the broker publishes to) are eligible, and never the MR target branch. Any other branch, or one that
cannot be fetched, is refused before a worker is spawned, with a comment on the ticket.

## Working from a non-default branch (`base:`)

By default a code or general worktree is cut from the repo's default branch. To start from another branch, for
example to fix review findings on an open MR's branch or to merge `main` into a release branch, add a bare line
`base: <branch>` to the ticket description (its own line, parsed like the `repo:` line; a leading `origin/` is
ignored). The broker fetches that branch from `origin` with an explicit refspec and cuts the worktree from it, and
the worker's new MR targets that branch instead of the default branch. The default branch's tracking ref
(`origin/<default>`) is refreshed too, so the worker can merge it. If the branch name is invalid or the branch does
not exist on the remote, no worker is spawned and the ticket gets a comment. The broker never quietly falls back to
the default branch. `base:` can be used together with `continue-mr:`: the worktree is cut from the `continue-mr:`
branch and the commits are pushed back onto it.

Code workers may run `git merge` and `git cherry-pick` on refs that are already in the repo, inside their own
worktree. They still cannot fetch, pull, push, reset, rebase or check out. The broker's push rules are unchanged:
it pushes only a fresh feature branch or an eligible `continue-mr:` branch. To resolve a merge conflict, the worker
edits the files, then runs `git add` and `git commit -m`. Forms of `git merge` that read a file into the commit
message (`-F` / `--file`) are denied, as is `git merge-file`. Because that deny is a plain pattern, any `git merge`
command with an uppercase `F` in it is refused, so merge a ref with an `F` in its name by its SHA instead.

## Refreshing the repo catalog

With repo inference on, the repo catalog is cached for `REPO_CATALOG_TTL_S`. `SIGHUP` drops the cached copy, and
`POST /catalog/refresh` re-lists GitLab right now and returns what was added and removed. It needs the token in
the variable named by `CATALOG_REFRESH_TOKEN_ENV` (default `BROKER_CATALOG_REFRESH_TOKEN`), sent as
`X-Broker-Catalog-Refresh-Token`; with the variable unset every call gets a 401. A failed GitLab listing answers
503 and keeps the previous catalog.

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

- GitHub Issues intake adds `POST /webhooks/github` (off until `GITHUB_WEBHOOK_SECRET` is set). Summary comments use `GITHUB_TOKEN` by default (rename with `GITHUB_TOKEN_ENV`). If a GitHub tap of yours already exports `GITHUB_TOKEN`, the broker will start posting summary comments on those issues after you upgrade; set `GITHUB_TOKEN_ENV` to another name first if you don't want that.
- The GitLab webhook endpoint is now `POST /webhooks/gitlab`. The old path, `POST /forge/gitlab`, still works
  for one more release and logs a `DEPRECATED POST /forge/gitlab` warning on every call. Point your GitLab
  project webhook (or your forwarder) at the new path before then. The token check and everything after it are
  the same on both paths.
