# Tracker adapters (design)

Status: steps 1 and 2 of the migration plan (ticket refs, the interface and the Vikunja adapter) are built. The
rest is the plan for letting the broker talk to more ticket trackers without each user writing their own glue.

## Why

Today the broker really knows two trackers. Vikunja tickets arrive through a tap at `POST /event` and every
write goes through one script (`BROKER_MCP_SH`) with Vikunja tool names and integer ids. GitLab issues
arrive at `POST /webhooks/gitlab`, and the broker only comments on them (`trackers/gitlab.py`). There's also a GitHub issue shape
in `coalescer.py` and `routing.py` with no way in. Adding a tracker means touching a dozen files, because
the ticket id is an `int` almost everywhere.

The fix is one seam: a `Tracker` interface, tickets addressed by a string ref, and a registry of enabled
trackers. GitHub is the first new adapter, issues and pull requests both, because its webhooks are signed and
the code already half-expects it.

## Goals and non-goals

Goals:

- One interface every tracker implements, so a new tracker is one module plus tests.
- Tickets addressed by an opaque string ref instead of a Vikunja integer.
- Vikunja and GitLab keep working with no config change, and Vikunja calls stay byte-identical.
- Each tracker verifies its own webhooks and recognizes its own echoes, fail-closed.

Not in scope: Bitbucket and Azure DevOps, syncing state between trackers, custom-field mapping, and the broker
opening its own pull requests on GitHub (MRs keep opening on GitLab for now). Reviewing and commenting on
pull requests that people open is in scope.

## Ticket refs

A ref is a string `<tracker>:<native>`. `<tracker>` is the name the tracker is registered under and
`<native>` is whatever that tracker needs to find the ticket. Only the adapter ever parses `<native>`.

| Tracker | Ref | Notes |
|---|---|---|
| Vikunja | `vikunja:task:42` | unchanged from today's subject key |
| GitLab issues | `gitlab:group/app#7` | unchanged from today's subject key |
| GitHub issues | `github:owner/repo#45` | unchanged from today's subject key |
| GitHub pull requests | `github:owner/repo#46` | same form: issues and PRs share one number space on GitHub |

The ref is the subject key. That's deliberate: the coalescer already keys GitHub and GitLab issues this way,
and keeping `vikunja:task:<n>` means no rewrite of `pending.json` or `completed.json`, no change to the
routing namespace `vikunja:task`, and no change to existing branch names or the CD ticket lookup.

A small module, `tickets.py`, owns the grammar:

- `ref_from_key(key)` returns the ref for a tracker subject, or `None` for keys that aren't tickets
  (`zulip:`, `gmail:`, `mr:`, and the `:?`/`:unknown` sentinels). It replaces `_task_id_from_key` in
  `adapters/cc.py` and the copy in `app.py`'s `_launch_tfa`.
- `tracker_of(ref)` returns the registry name.
- `slug(ref)` returns a filesystem- and branch-safe name. For `vikunja:task:<n>` it returns exactly what
  `worktree._safe` returns today (`vikunja_task_42`). For any other ref it appends the first 8 hex
  characters of the ref's sha256, because `_safe` maps `github:a/b#45` and `github:a_b#45` to the same
  string.

Normalized events carry `ref`. `task_id` stays on Vikunja events as a legacy alias until every reader has
moved.

## The Tracker interface

```python
class Tracker:
    name: str                      # registry name, the ref prefix
    kind: str                      # implementation: "vikunja", "gitlab", "github"

    # intake
    def verify(self, headers, raw) -> bool
    def normalize(self, headers, payload) -> list[dict]

    # read
    def fetch(self, ref) -> Ticket | None
    def url(self, ref) -> str

    # write back
    def comment(self, ref, html, run_id) -> bool
    def label(self, ref, name) -> bool
    def assign(self, ref, user) -> bool
    def link_mr(self, ref, mr_url, title, run_id) -> bool
    def review(self, ref, html, run_id) -> bool
    def close(self, ref) -> bool

    # echo guard
    def is_self(self, evt) -> bool
```

`Ticket` is a small dataclass: `ref`, `title`, `body`, `done`, `labels`, `assignees`, `creator`,
`project`, `url`, `is_pr`, and `text`, the rendered block the worker's brief embeds. The worker prompt and the
fail-closed checks read fields, never tracker markup.

What each method has to do:

- `verify` checks the webhook's signature or token against a secret named in config (the name, not the
  value). An unset secret or a missing header returns `False`. There is no fail-open mode.
- `normalize` turns one webhook into zero or more broker events, each with `source`, `event_name`, `ref`,
  `doer`, `labels`, `assignees`, `title`, `body`, `web_url`, and for comments `comment`, `comment_id` and
  any `run_id` it found in the body. Anything it doesn't understand returns `[]`, which is acked with 200
  so the tracker doesn't disable the hook.
- `fetch` returns the ticket or `None` on any failure. Callers already fail closed on `None`.
- `comment` posts HTML (or the tracker's nearest format) and must keep the broker's run marker in a form
  `normalize` can read back (see the echo guard section). Bodies are capped like today's.
- `label` and `assign` take names. The adapter resolves names to ids. Vikunja keeps using
  `PARKED_LABEL_ID` and numeric user ids under the hood.
- `link_mr` defaults to a comment that names the MR, which is all the broker does today. An adapter can do
  better (a GitLab issue link, a GitHub "mentioned" reference).
- `review(ref, html, run_id)` posts a review on a pull request. It defaults to `comment`, and only the GitHub
  adapter overrides it. A review is always the plain "comment" kind: the broker never approves or requests
  changes, so a worker's verdict can't satisfy or block a branch protection rule.
- `close` defaults to `False`. The broker never closes tickets today and this design doesn't change that.
  It's on the interface so a later CD step can opt in per tracker.
- `is_self` returns `True` when the event's author is this tracker's own bot account.

Every write returns `bool` and never raises, matching `poster.py`.

## Registry and intake

Enabled trackers are listed in `config/trackers.json`, loaded and checked at startup the same way as
`routes.json`: any problem stops the broker from starting.

```json
{
  "trackers": [
    {"name": "vikunja", "kind": "vikunja", "mcp_sh_env": "BROKER_MCP_SH"},
    {"name": "gitlab", "kind": "gitlab", "secret_env": "GITLAB_WEBHOOK_TOKEN",
     "token_env": "GITLAB_ISSUE_TOKEN", "authors": ["owner"]},
    {"name": "github", "kind": "github", "secret_env": "GITHUB_WEBHOOK_SECRET",
     "token_env": "GITHUB_TOKEN", "bot_login": "my-eph-bot", "authors": ["owner"]}
  ]
}
```

With no file, the broker registers `vikunja` and `gitlab` from today's settings, so existing installs don't
change. Names must be unique, lowercase and free of `:`. `*_env` fields name environment variables and never
hold the secret. Two instances of one kind are fine (`github` and `github-work`), and the ref prefix keeps
them apart.

Intake gets one generic endpoint, `POST /tracker/<name>`:

1. Unknown name: 404.
2. `verify(headers, raw)` fails: 401.
3. Body isn't JSON: 400.
4. `normalize` returns events. Each one goes through `Broker.ingest`, the same path as today. The reply
   lists the subject keys.

`POST /webhooks/gitlab` becomes an alias for `POST /tracker/gitlab`, and its CD pipeline branch stays where it
is. `POST /event` stays for taps that already send normalized events (the Vikunja tap). It keeps its HMAC
check and gains an optional `ref` field.

`POST /rerun` accepts `{"ref": "github:owner/repo#45"}` and keeps `{"task_id": 42}` as an alias for
`vikunja:task:42`.

## Echo guard per tracker

The broker must never start work because of its own comment. Today three things catch that, and each one
moves behind the interface.

1. Run marker. `poster.stamp` appends `<!-- broker-run:<run_id> -->` to every body, and the tap or
   `routing.comment_forward_admissible` reads it back. Each adapter's `comment` stamps and its `normalize`
   extracts. Vikunja, GitLab and GitHub all keep HTML comments in Markdown, so they share the current
   marker. A tracker that strips comments (Jira's document format does) needs its own carrier, such as an
   issue property, and that adapter isn't done until the round trip is tested.
2. Bot identity. `provenance.classify` drops events whose author is a known agent or bot, by username.
   Usernames differ per tracker, so `is_self` is checked first: an event whose `doer` is the tracker's own
   bot login is always an echo. `provenance.json` keeps working for the other agent accounts. GitHub also
   marks bot senders (`sender.type == "Bot"`), and the adapter treats that as `is_self` when the login
   matches `bot_login`.
3. Active runs. `echo_guard.py` builds `vikunja:task:{task_id}` by hand on line 74. It switches to
   `evt["ref"]`, so the active-run check works for every tracker.

`provenance.is_agent_handoff` and `mention_handoff.py` are Vikunja-only by design (they read Vikunja's
assignee and mention shapes). They get an explicit `tracker_of(ref) == "vikunja"` guard.

## GitHub, the first new adapter

Intake: a repo or org webhook with content type JSON and a secret, sending `issues`, `issue_comment`,
`pull_request`, `pull_request_review` and `pull_request_review_comment`.

- `verify`: HMAC-SHA256 of the raw body against the `sha256=<hex>` signature header GitHub sends with a
  webhook secret, compared in constant time.
- `normalize`, issues: `issues.opened`, `issues.labeled`, `issues.assigned`, `issues.edited` and
  `issue_comment.created` map to events with `ref = github:<owner>/<repo>#<number>`, `doer = sender.login`,
  `labels` and `assignees` from the issue, and the comment body for comments.
- `normalize`, pull requests: `pull_request.opened`, `pull_request.labeled`, `pull_request_review.submitted`,
  `pull_request_review_comment.created`, and `issue_comment.created` on a PR (the payload's issue carries a
  `pull_request` key) map to events with the same ref form, `kind = "pull"`, `doer = sender.login`, and the PR's
  `head_sha`, `head_repo`, `base_ref` and `draft` flag. `pull_request.synchronize` (new commits) is
  forward-only: it reaches a live worker on that PR and never starts one. Everything else is dropped.
- `fetch`, `comment`, `review`, `label`, `assign`, `close`: GitHub's REST API over `urllib` with a token from
  `token_env`, same style as `gitlab_mr.py`. No new dependencies. A PR comment goes to the issue comments
  endpoint, a review to the PR reviews endpoint with `event: COMMENT`.

Triggers, and why labels come first:

- On a public repo anyone can open an issue or a pull request, so `issues.opened` or `pull_request.opened`
  alone must never start a worker. The GitHub row needs either an `authors` list or a label trigger, and the
  loader refuses a public setup with neither. Today's code already enforces the author half for every forge
  source: `Router.classify` drops a GitHub or GitLab issue whose author isn't in `ISSUE_AUTHOR_ALLOWLIST`
  (empty by default, so intake is off), and `/event` refuses unsigned events by default. The per-tracker
  `authors` list replaces that global setting.
- The gate checks the event's `doer`, the person asking. For `pull_request.opened` that's the PR author. For a
  comment or a review it's the commenter, so a listed maintainer can ask for a review of a stranger's PR with a
  comment, and a stranger's comment on any PR starts nothing.
- Applying a label needs triage access on the repo, which makes a label a reasonable trust gate.
  `routes.json` rows gain an optional `"tracker"` in `match` (default `vikunja`) and an optional `"kind"`
  (`issue` or `pull`, default `issue`), so a site can say "label `eph:review` on a GitHub PR runs
  `review/code`".
- Assigning an issue to a bot account needs a GitHub App. Assignee triggers wait for that.

What a PR worker does:

- The default PR row is the reviewer: a throwaway read-only worktree at the PR's `head_sha`, the same path
  `mr:` reviews take today. The PR's code is untrusted. The worker reads the diff and never runs the PR's
  tests, hooks or scripts. Its verdict goes back as one `review` (comment kind) with the run marker, plus
  inline notes folded into the review body.
- A change row can push follow-up commits to the PR branch only when `head_repo` is the base repo (not a
  fork) and the repo is in `ISSUE_REPO_MAP`. Fork PRs get review only. The push uses the same push-host and
  allowlist checks as every other worker branch.
- A draft PR is skipped unless a label asks for it.

The issue or PR body, comments and diff are untrusted, exactly like a GitLab issue body today: capped and
wrapped as data in the brief. The repo must be in `ISSUE_REPO_MAP` or the spawn fails closed, as it does now.

## Migration plan

Each step is its own merge request with the full suite green. Every step after the first adds a parity test
proving Vikunja argv and GitLab behavior are unchanged.

1. Refs. Add `tickets.py` (`ref_from_key`, `tracker_of`, `vikunja_id`). Replace `_task_id_from_key`
   (`adapters/cc.py`) and the copy in `app.py`'s `_launch_tfa`. Put `ref` on every normalized event,
   keeping `task_id`. Fix `echo_guard.py:74`. No behavior change. `slug` waits for the first step that cuts a
   worktree for a non-Vikunja ref.
2. Interface and Vikunja adapter. Add `trackers/` with the base class and `VikunjaTracker`, which wraps the
   current `poster.py` and `ticket_fetch.py` and keeps their Vikunja parsing to itself. The adapter turns the
   ref back into the integer at the edge, so the tool calls are byte-identical. Then move the callers:
   - `adapters/cc.py`: the comment and label posters in `_post_envelope`, `_nag_push_refused`, `_save_wip`,
     `_save_patch`, `_notify_mr_opened`, `_notify_mr_verdict`, `_post_mr_verdict`, `_post_ticket_note`, the
     fail-closed comments in `_run_worker`, and the overload path's own comment. `_TICKET_DONE_RE` and the
     `ticket_meta` readers become `Ticket.done`, `Ticket.labels` and `Ticket.creator`. The injected
     `comment_poster`, `label_poster` and `ticket_fetcher` keep taking the integer id: they're the Vikunja
     adapter's internals, and `CCAdapter` calls them through its registry.
   - `app.py`: wiring, `_rerun_allowed`, `_comment_respawn`, `ops_rerun`, `_repo_parked_prune`, the self-assign
     call in `_launch_tfa`, and the mention handoff assign.
   - `poster.py` and `ticket_fetch.py`: become the Vikunja adapter's internals. Their public functions stay
     as thin wrappers for one release so out-of-tree callers don't break.
   - `cd.py` moved to step 3, with GitLab write-back: `_BY_MR` and `_BY_SHA` store refs. `_tid_in` also
     matches `key=<ref>` in the MR description (`gitlab_mr.open_mr` already writes it), so non-Vikunja
     tickets resolve. The deploy hand-off line's `ticket=` field keeps the bare number for Vikunja, since an
     external consumer reads it.
   - `envelope.py` moved to step 3 too (its `task_id` reaches a lot of tests): `escalate` takes a ref. Until
     then the usage-cap exhaustion and no-envelope paths in `adapters/cc.py` still escalate through
     `envelope_parser.escalate`, which comments with `poster.post_comment` directly (wired in `app.py`).
     `site_env.ticket_url` is already behind `Tracker.url`, with `TICKET_URL_TEMPLATE` kept as the Vikunja
     adapter's setting.
3. Registry and endpoint. `config/trackers.json`, the loader, `POST /tracker/<name>`, the `/webhooks/gitlab`
   alias, and `ref` on `/rerun`. Add `GitLabTracker` with intake moved from `forge.py` and new write-back
   (issue notes, labels), off unless `token_env` is set. Per-tracker window lookup in `coalescer._win`
   falls back to the tracker name.
4. GitHub. `GitHubTracker` for issues and pull requests, `review` write-back, the `match.tracker` and
   `match.kind` fields in `routes.json`, the author-or-label rule, the PR reviewer row and the same-repo-only
   push rule, `tickets.slug`, and docs.
5. Later, in demand order: Jira Cloud, Vikunja as a direct API adapter (so nobody needs the tap), Linear,
   and the broker opening its own GitHub pull requests as the MR side.

Some things stay Vikunja-only and refuse other refs (fail-closed): write grants (`write_grant.py` names the
grant file by the integer id and an external wrapper reads it), the launcher row's session naming in
`_live_tfa_session`, and the mention handoff.

## Testing

- Contract tests: one shared suite every registered adapter runs against recorded, synthetic webhooks.
  Verify rejects a bad or missing signature and an unset secret. Normalize maps each fixture to the expected
  events and drops the rest. A comment's run marker survives `comment` then `normalize`. `is_self` catches
  the bot's own comment.
- GitHub PR fixtures: a stranger's `pull_request.opened` and a stranger's comment start nothing, a listed
  commenter on a stranger's PR starts a review, `synchronize` only forwards, a fork PR never gets a change
  row, and a review's run marker survives `review` then `normalize`.
- Parity tests: with no `trackers.json`, the Vikunja tool-call JSON and the GitLab intake results are
  identical to today's, on the existing fixtures.
- Writes are tested against a stub HTTP server and a stub `BROKER_MCP_SH`. No test touches a real tracker.

## Decisions

- `match.tracker` in `routes.json` defaults to `vikunja`. A label name that means something on one tracker
  can't quietly start work from another.
- GitLab write-back uses `GITLAB_ISSUE_TOKEN` when it's set, and falls back to the MR token so one token is
  enough to start. On a public project, set a Reporter-level `GITLAB_ISSUE_TOKEN`: the MR token may be able to
  merge, and a comment token doesn't need to.
- `normalize` rejects a ref longer than 200 characters or containing whitespace or control characters. The
  event is dropped and logged, never truncated.
