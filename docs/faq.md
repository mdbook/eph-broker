# FAQ

## Why not run an agent CLI from a CI job?

You can, and for a one-shot "fix this issue" job that may be all you need. The broker exists for the parts a
CI job handles badly. A burst of ticket edits becomes one run instead of five, and a follow-up comment goes
into the same warm session instead of starting over. The process that holds the GitLab token is never the
process running the model. And a worker only starts if its prompt matches a hash you signed, so a prompt
can't change underneath you.

## Does it work with GitHub?

Not for pull requests. Pushing branches is plain git, but opening the MR, posting the review note and the
optional auto-merge all use the GitLab API, and the only forge webhook endpoint is `/webhooks/gitlab`.

GitHub issues can start a code worker, but only through a tap you write that sends `POST /event` with
`source: "github"` ([configuration.md](configuration.md#github-issues)). The change still comes back as a GitLab
merge request, so the repo's checkout needs a GitLab origin.

## Do I need Vikunja?

No, but the tracker glue was written against it, and until native tracker adapters land
([trackers.md](trackers.md)) another tracker has to pretend to be Vikunja. You need two pieces:

- a tap that sends `/event` calls with `source: "vikunja"` and an integer `task_id`, whatever your tracker
  really is;
- a script (`BROKER_MCP_SH`) that answers the broker's Vikunja-shaped calls: read a ticket, post a comment, add
  a label. It has to render the ticket in the exact text format the broker parses.

[tracker-script.md](tracker-script.md) has both contracts. If the script can't return a ticket, the worker
doesn't start. The GitLab issue path needs neither.

## Is a worker sandboxed?

Not at the OS level. Workers run as the broker's user. Tool allow and deny lists limit what the harness will
do (Claude Code through its permission flags, hermes through its `eph-gate` plugin), but a worker with shell
access can read whatever that user can. Run workers under a separate user or in a
container if that matters to you. [security.md](security.md) has the full picture.

## Can it push to main or merge on its own?

The shipped classes push to fresh `eph/...` branches only, and an assertion in `worktree.publish_mr` refuses a
push to the MR's target branch. Merging is manual unless you turn on one of two optional paths: the CD module,
which can merge reviewed MRs in repos you allowlist after re-checking them, or `AUTO_MERGE_REPOS`, which merges a
listed repo's worker MRs once the pipeline is green, with no reviewer (meant for content-only repos).

## What happens when the agent gets stuck?

It's told to return `blocked` with an explanation rather than guess. The broker posts that on the ticket and
adds `PARKED_LABEL_ID` if you set one. A worker that hits its turn cap has its partial work pushed to a WIP
branch with no MR. One that runs past its time limit is killed. In every case the worktree is cleaned up.

## What does it cost to run?

Each worker is a normal session of its harness, billed to the account or backend it runs on. On Claude Code
that's whatever account the CLI is logged into. Every MR the broker opens adds one more session for the
reviewer. The turn caps and time limits in
`config/constraints.json`, the warm-session rotation, `CONCURRENCY` and `ROW_CONCURRENCY` are the knobs that
bound it. `MAX_SPAWNS_PER_HOUR` (60) only warns: past it, each new start logs `BREAKER-WOULD-TRIP`, which is
worth an alert in your log pipeline.

## Can I use other models?

The worker model comes from `FLEET_MODEL_SH`, a script you provide that maps a row's agent name to a model,
with a built-in fallback. It must print a full `claude-...` model id or the name of a backend in the registry
file (`SPAWN_MODELS_JSON`); see [configuration.md](configuration.md#models-and-backends). Rows can also run on
hermes-agent ([hermes.md](hermes.md)). Be careful with the general worker on a
non-Anthropic backend with the Claude Code harness, because it loses Claude Code's per-action classifier there.

## Can I use Codex or another agent CLI?

Not out of the box. Two harnesses ship, Claude Code and hermes-agent. Adding another is a code change at one
seam: a class that runs one turn and returns the result in the broker's envelope format.
[adapters.md](adapters.md) walks through it, with Codex as the example.

## Can I add my own kind of worker without writing code?

Yes. Write and sign a prompt, then add a row to `config/routes.json` with the label or assignee that starts
it, the prompt, one of three capabilities (`read`, `code`, `general`) or a site capability (a built-in base
plus extra allow and deny rules, [routes.md](routes.md#site-capabilities)), and its repo
([routes.md](routes.md)). Anything past that is still a code change in `routing.py`
([prompts.md](prompts.md#adding-an-agent-type)).

## Why are the prompt hashes already filled in?

So the test suite and a first run work out of the box. They cover the generic prompts exactly as shipped.
Read `prompts/` before you rely on them, and re-sign whenever you change anything.

## Why standard library only?

The broker sits next to credentials, so less third-party code is less to audit. It also packages as one
`noarch` RPM and runs on any Python 3.9+.

## Why does the code mention fleets, chat bots and agent rosters?

The broker was pulled out of a larger self-hosted setup with long-lived agents, a chat bus and a tracker.
Those integrations are still in the code as optional settings. Left unset they stay off, and the broker logs
that rather than failing.
