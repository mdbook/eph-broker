# Concepts

This page follows one ticket from webhook to merge request. File names point at the code that does each
step, if you want to check the details.

## Events

Work arrives over HTTP on `127.0.0.1:8787`, at two endpoints.

- `POST /event` takes a JSON event from your tracker's tap. The fields the broker uses are `source`,
  `event_name`, `task_id`, `doer`, `labels`, `assignees`, `done` and `project_id`; comment events add
  `comment`, `comment_id` and, for comments the broker itself posted, a `run_id`. Until native tracker
  adapters land, the tap pretends to be Vikunja whatever your tracker is: `source` is `"vikunja"` and
  `task_id` is an integer. [tracker-script.md](tracker-script.md#events-your-tap-sends) lists every field.
- `POST /webhooks/gitlab` takes a raw GitLab Issue Hook. `forge.py` turns it into the same kind of event. Only a
  newly opened issue starts work (`ISSUE_TRIGGERING_EVENTS`), and only when its author is listed in
  `ISSUE_AUTHOR_ALLOWLIST`. That list is empty by default, so issue intake is off until you fill it in.

Each event maps to a subject, the thing being worked on, such as `vikunja:task:42` or `gitlab:group/app#7`
for a GitLab issue. A subject never has more than one worker.

Two filters run before anything else. The echo guard (`echo_guard.py`) drops events caused by the broker's own
comments and events whose author is a known agent or bot account, as listed in `config/provenance.json`. Then
the router checks the event against the routing table, and an event that matches no row is rejected and
counted in `/health` as `allowlist_rejected`. A human comment on a ticket with a running worker is forwarded
into that worker, which is told to treat it as data rather than instructions. Other comments don't start
anything, apart from one case covered under batching below.

Every event that gets past both filters is appended to the events log (`EVENTS_LOG`, one JSON object per line).

## Batching

Saving a ticket often fires several webhooks within a second or two. The coalescer (`coalescer.py`) waits a
short initial window before it starts a worker, so the burst becomes one worker with one combined brief. After
each turn the worker stays warm for a receive window; a new event in that time is sent to the same session
instead of starting another worker. When the window passes quietly, the worker exits.

Both windows are set per subject type in `config/windows.json`. For tracker tickets they're 3 and 45 seconds.
A warm worker accepts up to `max_forwards` follow-ups (10), and its harness session is restarted fresh
after 20 turns or 120k tokens (`rotate_turns`, `rotate_tokens` in `config/constraints.json`). At most
`CONCURRENCY` workers (5) run at once.

Once a ticket has finished a run, later events from agents are dropped. Only a human edit or reassignment
re-arms it. [agent-refire.md](agent-refire.md) has the exact rules, including how a human comment can
restart a ticket whose worker already exited.

## Routing

`routing.py` holds the routing table, `PROFILES`. Each entry pairs a trigger with a `Route`, and the first
matching entry wins. A trigger is one of these.

- a label (`eph:code-change`, `eph:general`), matched against the event's labels and assignees;
- an assignee (`eph-code`, `eph`), matched exactly;
- a source, for events that don't come from the tracker (`gitlab` issues, `mr` for the reviewer).

A `Route` says which prompt to load (`domain` and `prompt_class`), which tool grant to apply (`capability`), which
repo to cut the worktree from, which harness runs the worker (`adapter`: `cc` for Claude Code by default, or
`hermes`; see [adapters.md](adapters.md)) and which name to ask `FLEET_MODEL_SH` about when picking a model. The
label and assignee names are settings (`CODE_ROW_LABEL`, `DEV_ASSIGNEE` and friends, see
[configuration.md](configuration.md#built-in-rows)); an empty one turns its row off. The built-in rows are code,
and you add your own in `config/routes.json`
([prompts.md](prompts.md#adding-an-agent-type) explains how).

## Worker classes

Three classes ship with signed prompts.

| Class | Capability | Typical trigger | Publishes |
|---|---|---|---|
| `code/change-proposer` | `code`: edit and commit inside the worktree, run one named test runner; push and network commands denied | label `eph:code-change`, assignee `eph-code`, a new GitLab issue | comment and MR |
| `general/agent` | `general`: most tools over a fixed deny list, each action judged by the harness's own check (Claude Code's auto permission mode on native Claude, or hermes' policy judge; see [security.md](security.md)) | assignee `eph`, label `eph:general` | comment, and an MR if it committed |
| `mr-review/agent` | `mr-review`: Read, Grep and Glob only | the broker opened an MR | verdict note on the MR |

Rows from `config/routes.json` ([routes.md](routes.md)) go after every built-in narrow row and before the
general catch-alls (`eph`, `eph:general`). Their label and assignee match exactly, and the broker
won't start if a built-in row or an earlier config row would also match one.

`routing.py` also has rows for other kinds of worker (an infrastructure proposer, a docs-wiki writer, mail
triage and a few more). No prompts for them ship, so they're off until you name their trigger, and then a ticket
that matches one still fails at the prompt check until you add and sign its prompt. The launcher row
(`FLEET_ASSIGNEE_ALT`) is the odd one out: it hands the ticket to an external script (`SPAWN_SH`) instead of
starting a worker, and only logs a failure while that's unset.

Each class picks its repo differently.

- The code worker needs a `repo:/absolute/path` line in a tracker ticket (or repo inference, below). With
  neither, it refuses and comments on the ticket asking for one. The repo has to be in `CODE_REPO_ALLOWLIST`.
  There's no fallback to `CODE_REPO`.
- The general worker can only use repos in `GENERAL_REPO_ALLOWLIST`. On the Claude Code harness it also refuses
  to start without the MCP config file named by `GENERAL_MCP_CONFIG` (default `~/.config/eph-broker/general-mcp.json`),
  so it never picks up whatever MCP servers the broker user happens to have configured.
- A GitLab issue uses its project's entry in `ISSUE_REPO_MAP`. An unmapped project is refused.
- With `REPO_INFER_ENABLED=1` the code worker can also guess the repo from a GitLab catalog. It's
  experimental; see [repo-push-cred.md](repo-push-cred.md#repo-inference-experimental).

The code worker's test runner is picked per repo from `CODE_REPO_RUNNER_SEEDS` (`pytest`, `npm`, `go`,
`venv`, `bash-n`, `ansible-syntax`, `none`). It gets permission for that runner's commands and no other shell
access.

## The worker run

The broker cuts a fresh git worktree, removes `CLAUDE.md` and `AGENTS.md` from it, checks the prompt hash and
starts the row's harness with the signed prompt, the tool grant, a turn cap and a time limit
(`config/constraints.json`: 50 turns and 30 minutes by default, 100 turns and an hour for code workers). The
ticket text and the details the worker needs (repo, test command) go in the first message. Each turn is one run
of the harness CLI; a follow-up resumes the same session.

With the Claude Code harness that's `claude -p` with `--system-prompt`, `--max-turns` and
`--output-format json`, and `--resume <session>` for follow-ups. With the hermes harness it's a one-shot
`hermes chat` with a private config home per worker ([hermes.md](hermes.md)). [adapters.md](adapters.md)
compares the two.

The worker has to finish with one JSON object, like this one.

```json
{"status": "done", "summary": "<p>What changed and the test result</p>", "wake": []}
```

`status` is `done` or `blocked` (the reviewer uses `pass` or `block`). An unparseable result is retried once
and then escalated.

## Publishing

The broker does all the publishing; the worker has no credentials for it. Here's what happens when a code
or general worker finishes.

- The summary is posted as a comment: on a tracker ticket through `BROKER_MCP_SH`, on a GitLab issue as an
  issue note with `GITLAB_ISSUE_TOKEN` (or `GITLAB_MR_TOKEN` when that's unset). A GitLab issue's MR also
  quotes the summary and says `Closes group/app#7`, so merging it closes the issue
  ([operations.md](operations.md#finding-the-result-of-a-gitlab-issue)).
- If the worker committed, the broker pushes that commit to a new branch such as
  `eph/code-change/vikunja_task_42-1a2b3c4d`, then opens an MR to the repo's default branch with
  `GITLAB_MR_TOKEN`. The push URL has to be `https://` on a host in `BROKER_PUSH_HOSTS`, or nothing is pushed.
- A `blocked` result adds the `PARKED_LABEL_ID` label to a tracker ticket.
- A worker that ran out of turns gets its work pushed to an `eph/...-wip-<sha>` branch with no MR, since it
  isn't finished.
- Edits left uncommitted are saved as a patch file outside the worktree, and the worktree is removed.

## Review

Opening an MR starts the reviewer. It runs in a checkout of the MR branch with read-only tools and gets the
diff in its first message. Its `pass` or `block` verdict is posted as an MR note. The reviewer can't merge
anything, and it isn't the agent that wrote the change.

To pick the reviewer's model, see [configuration.md](configuration.md#choosing-the-reviewers-model).

Merging is up to you. The optional CD module can merge a reviewed MR in an allowlisted repo after more checks;
it's off by default and documented in [cd-deploy-paths.md](cd-deploy-paths.md).
