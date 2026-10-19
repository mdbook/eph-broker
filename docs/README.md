# eph-broker documentation

Start with [install.md](install.md) if you're setting it up, or [concepts.md](concepts.md) if you want to know
how it works first.

## Getting started

- [install.md](install.md): requirements, running from source, the RPM and systemd unit, and wiring webhooks.
- [tracker-script.md](tracker-script.md): the script you write so the broker can read tracker tickets and
  post comments (`BROKER_MCP_SH`), and the events your tap sends.
- [n8n.md](n8n.md): n8n workflows to import: a Vikunja tap, a GitLab issue tap, a run-event receiver and a run
  status lookup, plus a trigger node and a lookup node for self-hosted n8n.

## Concepts

- [concepts.md](concepts.md): events, batching, routing, worker classes, publishing and review, following one
  ticket from webhook to merge request.
## Configuration

- [configuration.md](configuration.md): the files you'll touch, the main settings, the secrets, limits and
  batching files, models and backends, the reviewer's model, live transcripts and the dashboard's data sources.
- [settings.md](settings.md): every environment variable the code reads, with its default.
- [repo-push-cred.md](repo-push-cred.md): the push credential line for a repo, and repo inference
  (experimental).

## Prompts and agent types

- [prompts.md](prompts.md): how worker prompts are built from modules and manifests, site overrides, inline
  sources, signing, `render-prompts.py --check`, and adding an agent type.
- [routes.md](routes.md): your own agent types in `config/routes.json`, with the fields, the three
  capabilities and what makes the broker refuse the file.

## Harnesses

- [adapters.md](adapters.md): what a harness is, the Adapter interface, the result envelope, how a route picks
  its harness, how Claude Code and hermes differ, and a worked example for adding another CLI.
- [hermes.md](hermes.md): running workers on hermes-agent, and keeping its venv non-editable.

## Security

- [security.md](security.md): the model in one page, covering what comes in, what a worker gets, what goes
  out, and where it leaks.
- [security-residual-same-uid.md](security-residual-same-uid.md): the same-user limits in detail, the git
  hardening, and rolling out HMAC on `/event`.

## Operations

- [operations.md](operations.md): logs, state files, `SIGHUP` reload, `/health`, the dashboard, re-running a
  ticket and upgrading.
- [cd-deploy-paths.md](cd-deploy-paths.md): the optional auto-merge and deploy hand-off.
- [events.md](events.md): signed run events (started, finished, escalated, MR opened) for n8n and friends.

## Advanced: driving the broker from your own agents

- [agent-refire.md](agent-refire.md): which events from an agent's service account start, feed or skip a
  worker, and why an agent can't re-run a ticket that already finished.

## Design notes

- [trackers.md](trackers.md): the plan for native tracker adapters (GitHub Issues first). Ticket refs and the Vikunja adapter are built; the other trackers are not.
  These are about where tickets come from, not the harness a worker runs on.

## FAQ

- [faq.md](faq.md): GitHub, other trackers, sandboxing, cost, other models and harnesses, and why it's
  standard library only.

## Glossary

| Term | Meaning |
|---|---|
| Harness | The agent CLI a worker runs on: Claude Code (`cc`) or hermes-agent. |
| Worker | One short-lived agent session working one ticket in a throwaway git worktree. |
| Worker class | A kind of worker, `<domain>/<class>`, with its own signed prompt (for example `code/change-proposer`). |
| Row | One entry in the routing table: a trigger (label, assignee or event source) and the worker class it starts. |
| Capability | The tool grant a row's worker gets: `read`, `code`, `general`, and a few built-in special ones. |
| Site capability | Your own capability: a built-in base plus extra allow and deny rules, in `broker.yaml`. |
| Tap | Something outside the broker (often an n8n workflow) that turns a tracker's webhooks into `POST /event` calls. |
| Tracker script | The script named by `BROKER_MCP_SH` that reads tickets and posts comments for the broker. |
| Subject | The thing a worker works on, such as `vikunja:task:42` or `gitlab:group/app#7`. At most one worker per subject. |
| Batching window | How long the broker collects a burst of events before it starts a worker (3 seconds for tickets). |
| Warm worker | A worker that finished a turn and waits a receive window (45 seconds) for a follow-up before it exits. |
| Echo guard | The filter that drops events the broker's own comments caused, and events by bot accounts. |
| Provenance | `config/provenance.json`: which account names are agents or bots rather than people. |
| Handoff | An event from an agent account that is allowed to start a worker, see [agent-refire.md](agent-refire.md). |
| Escalation | A message to `OPS_HANDLE` when something needs a human, through `NOTIFY_BACKEND`. |
| Parked | A ticket the broker stopped on and labelled with `PARKED_LABEL_ID`, waiting for a person. |
| Backend | Where a worker's model runs: native Claude, or an entry in the `SPAWN_MODELS_JSON` registry. |
