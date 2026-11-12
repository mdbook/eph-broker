# Configuration reference

Settings are environment variables, and most of them can also live in a file. Code defaults are empty, and most
features stay off (fail closed) until you set them. [settings.md](settings.md) lists every variable the code
reads.

## Files you'll touch

| File | What it's for | Do you need it? |
|---|---|---|
| `config/site.env` | Settings, one `KEY=VALUE` per line ([below](#the-settings-file)). Start from `config/site.example.env`. | Yes. The quick start only uses this. |
| `config/provenance.json` | Which accounts are bots, so their edits don't start work ([below](#who-counts-as-an-agent)). | Yes. The broker won't start without it. |
| The broker's environment file, e.g. `/etc/eph-broker/eph-broker.env` | [Secrets](#secrets). Never in `site.env`. | Yes |
| `approved-prompts.json` | The hashes of prompts you've signed ([prompts.md](prompts.md)). | Ships filled in for the shipped prompts. |
| `~/.git-credentials` of the broker's user | Push credentials ([repo-push-cred.md](repo-push-cred.md)). | For any MR |
| `~/.config/eph-broker/general-mcp.json` | The general worker's MCP servers ([below](#the-general-worker)). | For the general worker |
| `config/constraints.json`, `config/windows.json` | [Limits and batching](#limits-and-batching). | No, the defaults are fine. |
| `config/routes.json` | Your own agent types ([routes.md](routes.md)). | No |
| `config/repo-companions.json` | Read-only source dependencies per code repo ([below](#companion-source-repositories)). Start from `config/repo-companions.example.json`. | No |
| `BROKER_MCP_SH`, a script you write | Reading and commenting on tracker tickets ([tracker-script.md](tracker-script.md)). | For tracker tickets |
| `FLEET_MODEL_SH`, `SPAWN_MODELS_JSON` | Which model and backend a worker runs on ([below](#models-and-backends)). | No |
| `config/broker.yaml` or `config/broker.json` | All the config above in one file, as an alternative ([below](#the-config-file)). | No |

## Companion source repositories

Use `config/repo-companions.json` (or `REPO_COMPANIONS_FILE`, an operator-owned JSON path) when a code
repo's tests need sources from another repo. For example:

```json
{"example/widget-site": [{"repo": "example/widget-core", "ref": "main", "as": "core"}]}
```

Keys and `repo` values are full catalog namespace/repo names. `as` is a unique lowercase snake_case
name within that list; it becomes `.companions/core` and `COMPANION_CORE`. `ref` is a branch, tag or
commit SHA. Configure the repo catalog and its local mappings even if repo inference is disabled;
denied, archived or unknown companions are refused. The broker uses catalog origin/clone URLs and its own
fetch credentials, never worker credentials or ticket-supplied URLs.

The file is re-read on each code worktree cut, not on warm forwards. An absent default file disables
companions. An explicitly named missing file, invalid configuration, checkout failure or an existing
`.companions` entry refuses that run rather than using stale sources. Each ref is shallow-fetched once,
detached at its resolved SHA, stripped of git metadata and made read-only. There is no submodule fetch.
See [worker-runtime.md](worker-runtime.md#companion-sources) for worker access and provenance.

## The config file

The config file is optional. Without it the broker reads `config/site.env` and the json files in `config/`, which
is all the quick start needs. `config/broker.yaml` (start from `config/broker.example.yaml`, which starts the
broker as is), or any file named by `BROKER_CONFIG`, gathers them into one file. When `BROKER_CONFIG` is unset,
the broker uses `config/broker.yaml` if it exists, otherwise `config/broker.json` if it exists. If neither exists,
it uses the separate files as before. An existing invalid YAML file is never bypassed for JSON. A YAML file needs PyYAML
(`python3-pyyaml`); a path ending in `.json` is read as JSON and needs nothing extra. An explicit path that doesn't
exist stops the broker, and so does an unknown section, a repeated key or a parse error.

| Section | What it is | Replaces |
|---|---|---|
| `settings` | The environment settings below, as `NAME: value`. Strings, numbers or `true`/`false` (`true` becomes `1`). | `config/site.env` |
| `routes` | Your own agent types, a list of rows ([routes.md](routes.md)) | `config/routes.json` |
| `capabilities` | Your own capabilities: a built-in base plus extra allow and deny rules ([routes.md](routes.md#site-capabilities)) | (new) |
| `constraints`, `windows` | [Limits and batching](#limits-and-batching) | `config/constraints.json`, `config/windows.json` |
| `provenance` | [Who counts as an agent](#who-counts-as-an-agent) | `config/provenance.json` |
| `inbox-accounts`, `zulip-wake`, `cd-tier`, `deploy-paths`, `pool-vikunja-ids` | The content of that json file, as is | `config/<name>.json` |

Which value wins:

- The real environment (systemd drop-ins, tests) wins over `settings`, and `settings` win over `site.env`.
- A section wins over its json file; the broker logs that the file is ignored.
- An explicit path variable (`ROUTES_PATH`, `BROKER_PROVENANCE_PATH`, `BROKER_INBOX_ACCOUNTS`) wins over the section.

`windows`, `zulip-wake`, `cd-tier`, `deploy-paths` and `pool-vikunja-ids` are re-read on `SIGHUP`, and a bad
edit keeps the last good copy (logged). Every other section is read once at startup; a `SIGHUP` logs which of
them changed so you know to restart. The full table is in
[operations.md](operations.md#reloading-config). Quote dates and times (`"2026-10-18"`): YAML reads them as dates, and the
broker only takes strings, numbers, true/false, lists and mappings. Never put secrets in it, see
[Secrets](#secrets).

## The settings file

`site_env.py` also loads `config/site.env` (start from `config/site.example.env`), or any file named by
`BROKER_SITE_ENV`; an explicit path that doesn't exist stops the broker. You can keep using it, or move its lines
into `settings`.

- One `KEY=VALUE` per line. It's not a shell file: `export K=V` lines and inline `# comments` don't work.
- One layer of matching quotes is stripped.
- The real environment (systemd drop-ins, tests) always wins over the file.
- Never put secrets in it, see [Secrets](#secrets).

## Settings

| Key | Meaning | If unset |
|---|---|---|
| `BROKER_PUSH_HOSTS` | Comma list of bare hostnames a worker branch may be pushed to (https only). | No pushes |
| `GITLAB_WEB_URL` / `GITLAB_API` | GitLab web root; the API defaults to `<web>/api/v4`. | No MRs, no repo catalog |
| `TICKET_URL_TEMPLATE` | Ticket link format, `{}` is the ticket id, e.g. `https://tracker.example.com/tasks/{}`. | Plain `#<id>` |
| `BROKER_MCP_SH` | Absolute path of the script you write for tracker ticket reads, comments and labels ([tracker-script.md](tracker-script.md)). | No tracker ticket can start a worker |
| `GENERAL_REPO_ALLOWLIST` | Colon list of repo paths the general worker may cut worktrees from. | Admits nothing |
| `GENERAL_REPO`, `GENERAL_REPO_PATH` | Default repo for the general worker and the checkout it is published from. | |
| `CODE_REPO_ALLOWLIST` | Colon list of repo paths the code worker may use. A tracker code ticket names its repo with a `repo:/absolute/path` line, and that path has to be on this list. | Admits nothing |
| `CODE_REPO`, `CODE_REPO_PATH` | Row defaults the code worker no longer falls back to: a tracker code ticket without a `repo:` line is refused with a comment (unless repo inference is on). You can leave them unset. | |
| `CODE_REPO_RUNNER_SEEDS` | Test runner per repo, `<path>=<runner>,...` (`pytest`, `npm`, `go`, `venv`, `bash-n`, `bash-n+unittest`, `ansible-syntax`, `repo-test`, `none`; `repo-test` runs the repo's own `./run-tests.sh` from the worktree; `bash-n+unittest` is `bash -n` plus `python3 -m unittest`). | Detected from the repo |
| `CODE_TEST_DEPS_CONFIG` | Path to an operator-owned JSON object mapping absolute repo paths to package requirement lists. Mapped repos use `venv`, with broker-side setup, never worker-side installs. | Unset; no installs |
| `ISSUE_REPO_MAP` | Which local checkout each GitLab or GitHub project's issues work in, `gitlab:group/app=/srv/git/app,...` ([GitHub issues](#github-issues) need a tap). | Every issue is refused |
| `ISSUE_AUTHOR_ALLOWLIST` | Comma list of usernames whose new issues may start a worker, compared case-insensitively. Anyone else's issue is ignored. | Nobody, so issue intake is off |
| `CONTINUE_MR_PREFIXES` | Colon-separated branch prefixes a `continue-mr:` line may name ([operations.md](operations.md#continuing-an-open-mr-fix-rounds)). Each must end in `/`. | `eph/`; empty continues nothing |
| `TURN_CAP_PUBLISH_CLEAN_COMMIT` | `1`: a code worker that hits its turn cap with a clean commit ahead of the base gets that commit published as a normal MR. `0` keeps WIP-branch-only behaviour. | `1` |
| `MR_REVIEW_REPO` | Default repo for the reviewer (the repo in the event wins). | |
| `MR_REVIEWER_MODEL`, `MR_REVIEWER_BACKEND`, `MR_REVIEW_ADAPTER` | Which model reviews MRs, see [below](#choosing-the-reviewers-model). | |
| `MR_REVIEW_QUICK_MODEL`, `MR_REVIEW_STANDARD_MODEL`, `MR_REVIEW_FULL_MODEL`, `MR_REVIEW_QUICK_MODEL_CODEX` | Per-tier reviewer model, see [Tiered MR review](#tiered-mr-review). | `haiku`, unset, `opus`, unset |
| `MR_REVIEW_QUICK_PATHS`, `MR_REVIEW_FULL_PATHS`, `MR_REVIEW_QUICK_MAX_DIFF_LINES`, `MR_REVIEW_FULL_DIFF_LINES` | Which diffs count as `quick`/`full`, see [Tiered MR review](#tiered-mr-review). | Generic built-in globs/thresholds |
| `OPS_HANDLE` | Who gets escalations: a chat handle, the webhook's `to`, or an email address, depending on `NOTIFY_BACKEND`. | Escalations are only logged |
| `NOTIFY_BACKEND` | How escalations and hand-off messages go out: `webhook`, `email` or `zbus`, see [Notifications](#notifications). Anything else stops the broker. | `zbus`, which without `NOTIFY_ZBUS` only logs |
| `NOTIFY_ZBUS`, `NOTIFY_ZBUS_ENV` | With `zbus`: the chat CLI the original deployment used, and the path of the sender's credentials file (the broker never reads it). | Messages are only logged |
| `EVENTS_WEBHOOK_URL`, `EVENTS_WEBHOOK_KINDS` | Where signed run events go, and which ones, see [events.md](events.md). | Off |
| `RERUN_CALLER` | The name `/rerun` calls are logged and commented as. | `operator` |
| `FLEET_MODEL_SH` | Script that prints the model for an agent name: a full `claude-...` id or a backend name, see [Models and backends](#models-and-backends). | `NATIVE_FALLBACK_MODEL` |
| `NATIVE_FALLBACK_MODEL` | The `claude-*` model a worker gets when `FLEET_MODEL_SH` is unset or fails. | `claude-sonnet-5` |
| `TFA_BACKEND` | Backend flag passed to `SPAWN_SH` as `--<name>` when the external launcher row starts an agent. | No flag |
| `USAGE_CAP_FALLBACK_CHAIN` | Comma list of registry backends a usage-capped worker retries on (with `USAGE_CAP_FALLBACK_ENABLED`). | No fallback |
| `FLEET_USAGE_FILE` | Usage snapshot JSON; at the thresholds the capped Anthropic attempt is skipped up front. | Not read |
| `WORKER_ENV_PASSTHROUGH` | Comma list of extra environment variable names a worker keeps from the broker env, on top of the fixed allowlist (`PATH`, `HOME`, locale, ...), e.g. the interpreter a repo's test runner reads. Names containing `TOKEN`, `SECRET`, `KEY` or `PASSWORD` (any case) are refused with a log line. Read at import, so restart after changing it. | Allowlist only |
| `PARKED_LABEL_ID` | Tracker label id added when a worker returns `blocked`. | No label |
| `FLEET_ASSIGNEE`, `GENERAL_ROW_LABEL` | Assignee and label that start the general worker, see [Built-in rows](#built-in-rows). | `eph`, `eph:general` |
| `DEV_ASSIGNEE`, `CODE_ROW_LABEL` | Assignee and label that start the code worker. | `eph-code`, `eph:code-change` |
| `GENERAL_SETTING_SOURCES` | Which Claude Code settings the general worker loads (Claude Code harness only). | `user` |
| `GENERAL_MCP_CONFIG` | The only MCP config file the general worker may load (`--strict-mcp-config`, Claude Code harness only), see [The general worker](#the-general-worker). | `~/.config/eph-broker/general-mcp.json`; the worker refuses to start if it's missing |
| `CODE_PORTCULLIS_HOOK` + `CODE_PORTCULLIS_GATE_DIR` | Set BOTH to put the code worker (Claude Code harness only) on the `general` posture: no Bash allow-list, `--permission-mode auto`, a small hard deny-floor (`curl`/`wget`/`ssh` no longer floored), and a PreToolUse(Bash) hook adjudicating every call. The hook is the shell command in `CODE_PORTCULLIS_HOOK`; `CODE_PORTCULLIS_GATE_DIR` (absolute) is passed to it as `PORTCULLIS_GATE_DIR`. The dir must be a worker-only dir holding `mode` = `enforce` AND `full` = `on` (`enforce` alone blocks every call). Unset or half-set: the legacy allow-list grant, unchanged. codex/hermes code workers always keep the allow-list. | Unset (off) |
| `CODE_BROWSER_MCP_CONFIG` | Opt-in: the playwright MCP config a code worker loads (`--strict-mcp-config`, Claude Code harness only) so it can drive a real browser and return screenshot/console evidence for a UI change. Unset: no browser, argv unchanged. See [The code worker's browser](#the-code-workers-browser). | Unset (off) |
| `AUTO_MERGE_REPOS` | Repos (`ns/name` list) whose worker MRs merge once the pipeline is green, **with no reviewer**. See [cd-deploy-paths.md](cd-deploy-paths.md#auto-merge-without-a-reviewer-auto_merge_repos). | Off |
| `CODE_NONCLAUDE_OK` | `1` lets a code worker run on a non-Anthropic backend. Any code worker on the hermes or Codex harness needs it too. | Such a code worker is refused |
| `<ROW>_ADAPTER` | The harness a built-in row's workers run on: `cc`, `hermes` or `codex`, see [Choosing a row's harness](#choosing-a-rows-harness). Read at startup; an unknown value stops the broker. | `cc` |
| `CODEX_AUTH_FILE`, `CODEX_BACKEND`, `CODEX_BIN`, `CODEX_HOME_ROOT`, `CODEX_SANDBOX_NETWORK` | The Codex harness, see [The Codex harness](#the-codex-harness). | Codex workers are refused |

Process settings read by `app.py`: `PORT` (8787), `CONCURRENCY` (5), `CONFIG_DIR`, `PROMPTS_DIR`,
`APPROVED_PROMPTS`, `WORKTREES_ROOT`, `EVENTS_LOG`, `BROKER_MAX_BODY` (largest POST body in bytes, 1048576).
`routing.py` reads `ROUTES_PATH` (default
`CONFIG_DIR/routes.json`), the file with your own agent types ([routes.md](routes.md)). Everything else is in
[settings.md](settings.md).

## Limits and batching

| File | What's in it | Defaults |
|---|---|---|
| `config/constraints.json` | Turn caps, wall-clock limits, forwards, session rotation | 50 turns and 1800 s (code: 100 turns and 3600 s), 10 forwards, rotate after 20 turns or 120k tokens of context |
| `config/windows.json` | How long to collect a burst before starting a worker, and how long a worker stays warm | 3 s and 45 s for tracker tickets |

`windows.json` is re-read on `SIGHUP`. `constraints.json` is only read at startup, so restart after changing it
(see [operations.md](operations.md#reloading-config)).

### Dispatch priority

`DISPATCH_PRIORITY=reviews-first` (default) gives pending MR reviews, informed
BLOCK re-reviews, operator review reruns, and valid `continue-mr:` fix rounds
priority over new tickets **in the same repo**. Pending/live follow-ups hold new
work for that repo until they finish; already-running workers are not preempted.
Reviews and fixes share one FIFO follow-up class. New work remains FIFO within
its repo. Checkout-lock waiters use the same classes, including workers already
waiting when a review arrives. Row caps and the global turn cap still apply.
Checkout waiters retain one queue position throughout a cancellable wait; drain
checks do not re-queue them or change FIFO order within their class.

The durable intake queue hands a finite oldest-first snapshot to the coalescer
before it selects workers, so a review cannot be hidden behind a capped ticket.
Each handoff retains its original committed-intent/atomic-removal safeguards.
The scheduler interleaves repo lanes, preferring repos served less recently;
continuous reviews on one repo cannot monopolize a shared row cap ahead of
another repo's older work. A debounce, live subject, or row cap is skipped rather
than blocking unrelated lanes. Drain and ambiguous-intent recovery still stop
all new admission.

Repo grouping is advisory, not permission: the broker reads the ticket's explicit
`repo:` declaration, configured row repo, or issue repo map; MR events identify
their own repo. Explicit catalog names can resolve to local/clone paths, but
scheduling never invokes inference models or clones a repo. Up to four background
daemon lookups start at intake/rearm, using the debounce window to populate an
advisory cache; the scheduler never waits for tracker/catalog I/O or holds its lock
for it. `DISPATCH_METADATA_WAIT` (default `5` seconds, `0` disables the grace) holds
ordinary admission while an eligible same-row entry's lookup is younger than this
bound. Rows sharing a concurrency-cap group share this grace. Known follow-ups and
other rows remain dispatchable. After the bound, unresolved/failed/unknown targets
use independent FIFO slots, so stalled sources cannot indefinitely stop admission.
An initial tick can therefore defer work without attempting a spawn. Once a
durable launch is attempted, a spawn exception still propagates and retains its
committed intent in recovery, blocking all dispatch until explicit reconciliation.
Metadata is cached while the pending event burst is unchanged. A late result for
an already-admitted burst updates its live worker's repo/follow-up hint, so a live
review/fix still holds new same-repo work. Results for changed, removed, or re-armed
bursts cannot overwrite a different burst's metadata. Spawn still re-fetches the ticket and
applies every existing target/branch/allowlist gate. Thus an inferred-only target
cannot receive repo-local scheduling priority until its target is known at run
time. Scheduling does not wait indefinitely for an MR to be merged externally:
the hold ends when its review/fix worker retires, and a subsequent queued round
again has priority.

Set `DISPATCH_PRIORITY=fifo` in the environment or startup settings to restore
the previous strict durable FIFO (including head-of-line blocking) and FIFO
checkout-lock admission. Other values refuse startup. No site-specific repo
names or merge/deploy policy are embedded in this setting.

## Built-in rows

Each built-in row starts on one trigger, and each trigger is a setting. An empty value takes the row out of the
routing table, so nothing goes there. The rows that ship a signed prompt start on; the rest need things only your
site has (a repo, mail accounts, a launcher script, their own prompt), so they stay off until you name a trigger.

| Row | Trigger setting | Default |
|---|---|---|
| code worker | `CODE_ROW_LABEL` (label), `DEV_ASSIGNEE` (assignee) | `eph:code-change`, `eph-code` |
| general worker | `FLEET_ASSIGNEE` (assignee), `GENERAL_ROW_LABEL` (label) | `eph`, `eph:general` |
| MR reviewer, issue intake | `MR_REVIEW_ROW_SOURCE`, `GITHUB_ISSUE_SOURCE`, `GITLAB_ISSUE_SOURCE` (event sources) | `mr`, `github`, `gitlab` |
| read-only report (ansible repo) | `ANSIBLE_ROW_LABEL` | off |
| docs-wiki writer | `WIKI_ROW_LABEL` | off |
| infra proposer, infra generalist | `INFRA_ASSIGNEE`, plus `INFRA_GENERAL_LABEL` for the generalist | off, `infra:general` |
| external launcher (`SPAWN_SH`) | `FLEET_ASSIGNEE_ALT` | off |
| mail triage, chat inbox | `INBOX_ROW_SOURCE`, `ZULIP_ROW_SOURCE` (event sources) | off |

Label triggers match as a substring of the ticket's labels and assignees; assignee triggers match exactly. A ticket
that reaches routing with no label or assignee at all only goes to the read-only report row, and with that row
off it isn't routed (normally it never gets that far: intake drops it). To add a kind of worker of your own, use
[routes.md](routes.md) instead.

## The general worker

The general worker gets the broadest grant: most tools, a fixed deny list, and Claude Code's auto permission
mode judging each action. Two things come from the broker user's home directory rather than from the repo.

- MCP servers. The worker loads only the file named by `GENERAL_MCP_CONFIG` (default
  `~/.config/eph-broker/general-mcp.json`) and refuses to start without it. A file with no servers is fine:

  ```json
  {"mcpServers": {}}
  ```

- User settings. With `GENERAL_SETTING_SOURCES=user` (the default), the worker loads the user-level Claude Code
  settings (`~/.claude/settings.json`) and `~/.claude/CLAUDE.md`, and its prompt tells it to follow them. An
  empty value loads neither.

So run the broker as a dedicated service user (the RPM creates `eph-broker`), and keep that user's
`~/.claude/settings.json` and `CLAUDE.md` short and intentional. Anything in them applies to every general
worker.

## The code worker's browser

Off by default. Set `CODE_BROWSER_MCP_CONFIG` to the **absolute** path of a Playwright MCP config file and the
two code rows (the `code-change` row and the GitHub/GitLab issue row) let their worker drive a browser: navigate,
click, type, screenshot, read the console and network log, and resize. That lets a worker check a UI change
by looking at it instead of only reasoning about it. Nothing else is widened: the deny list is unchanged, rows
that are not code rows (including `infra` rows and config-file routes) never get it, and it is Claude Code
harness only (a row on the `hermes` or `codex` harness with the knob set is refused at start).

The worker's MCP surface is exactly that one server (`--strict-mcp-config`), and only an enumerated list of
Playwright tools is allowed. Every other Playwright tool (arbitrary code, file upload, cookies and storage,
request interception, PDF and trace output) is explicitly denied in both permission modes, so none of them
reach the auto-mode judge.

The allowed `browser_evaluate` and `browser_tabs` reach everything the browser holds, whatever tools are
excluded. So the config must point at a **fresh, isolated, logged-out browser**, never at one a person uses.
The broker validates the file when the route loads and again on every spawn, and refuses anything other than:

- exactly one server, keyed `playwright`, using the `stdio` transport (a `url`, `http` or `sse` server is refused);
- `--isolated` in its arguments (a throwaway in-memory profile);
- `--allowed-origins` with a non-empty list, so the browser can only reach the origins you name;
- none of `--cdp-endpoint`, `--cdp-header`, `--user-data-dir`, `--storage-state`, `--extension`, `--config`,
  `--browser-url`, `--remote-debugging-port`, `--allow-unrestricted-file-access`, `--init-script` or
  `--init-page`, and no `PLAYWRIGHT_MCP_*` variable in the server's `env`.

```json
{"mcpServers": {"playwright": {"command": "npx",
  "args": ["@playwright/mcp@latest", "--isolated", "--headless",
           "--allowed-origins", "http://localhost:3000"]}}}
```

## GitHub issues

[Native GitHub opened-issue intake](github-issues.md) uses `/webhooks/github`, with HMAC-SHA256 verification
and summary comments. PRs and comment intake are not supported. Follow that guide for configuration,
permissions, the shared author allowlist and the GitLab mirror requirement.

An existing tap can still send a signed normalized `POST /event` like this one:

```json
{"source": "github", "event_name": "issues.opened", "repo": "owner/app", "issue": 45,
 "title": "Fix the date parser", "body": "The parser drops the time zone.",
 "web_url": "https://github.com/owner/app/issues/45", "doer": "octocat"}
```

- `source` must equal `GITHUB_ISSUE_SOURCE` (default `github`), and `event_name` must be in
  `ISSUE_TRIGGERING_EVENTS` (default `issues.opened`, `issue.opened`, `issue.open`).
- `doer` is the webhook's `sender.login`, and it must be in `ISSUE_AUTHOR_ALLOWLIST`.
- `repo` and `issue` make the subject (`github:owner/app#45`), and `ISSUE_REPO_MAP` needs a
  `github:owner/app=/srv/git/app` entry for it.
- The worker works from `title` and `body`. The change comes back as a GitLab merge request, so the local
  checkout needs a GitLab https origin. With `GITHUB_TOKEN` configured, summaries are posted on the GitHub issue.

## Notifications

Worker escalations go to `OPS_HANDLE`; hand-offs (the CD deploy line, infra MR links) go to their own handle. All
of them use one channel, picked by `NOTIFY_BACKEND`. A failed send is logged and never stops the worker.
Most installs want `webhook` or `email`. The default, `zbus`, is the chat CLI the original deployment used;
without `NOTIFY_ZBUS` it only logs, so set `NOTIFY_BACKEND` and `OPS_HANDLE` to hear about escalations.

- `zbus` (the default): `NOTIFY_ZBUS dm <handle> --stdin`, message on stdin. You probably don't have this CLI.
- `webhook`: a JSON `POST` of `{"to": <handle>, "kind": "escalation" or "dm", "text": ...}` to
  `NOTIFY_WEBHOOK_URL`. It has to be `https`, or `http` to localhost. With `NOTIFY_WEBHOOK_SECRET` set, the body
  is signed: `X-Eph-Signature: sha256=<hex HMAC-SHA256 of the body>`. Any non-2xx answer counts as a failure.
- `email`: one message per notification through `NOTIFY_SMTP_HOST` (`NOTIFY_SMTP_PORT`, default 587) from
  `NOTIFY_EMAIL_FROM`, so every handle has to be an email address. STARTTLS is on unless `NOTIFY_SMTP_STARTTLS=0`,
  and the server's certificate and host name are checked against the system CA store. It logs in when
  `NOTIFY_SMTP_USER` and `NOTIFY_SMTP_PASSWORD` are both set. With STARTTLS off and a login configured, nothing
  is sent (logged as an error), so the password never goes out in clear.

## Secrets

The broker reads each secret from an environment variable at run time; the code only knows the variable's name.
You can rename any of them with the `*_ENV` variable in parentheses.

| Secret | Used for |
|---|---|
| `BROKER_HMAC_SECRET` (`HMAC_SECRET_ENV`) | Checking `/event` signatures |
| `GITLAB_WEBHOOK_TOKEN` (`GITLAB_WEBHOOK_TOKEN_ENV`) | Checking `/webhooks/gitlab` calls |
| `GITHUB_WEBHOOK_SECRET` (`GITHUB_WEBHOOK_SECRET_ENV`) | Checking native `/webhooks/github` HMAC-SHA256 signatures |
| `GITHUB_TOKEN` (`GITHUB_TOKEN_ENV`) | Posting summary comments; repo-scoped fine-grained PAT with Issues: write |
| `BROKER_RERUN_TOKEN` (`RERUN_TOKEN_ENV`) | Checking `/rerun` calls |
| `NOTIFY_WEBHOOK_SECRET` (`NOTIFY_WEBHOOK_SECRET_ENV`) | Signing notification webhooks (optional) |
| `NOTIFY_SMTP_PASSWORD` (`NOTIFY_SMTP_PASSWORD_ENV`) | Logging in to the SMTP server (optional) |
| `EVENTS_WEBHOOK_SECRET` (`EVENTS_WEBHOOK_SECRET_ENV`) | Signing run events (required when `EVENTS_WEBHOOK_URL` is set) |
| `GITLAB_MR_TOKEN` (`GITLAB_MR_TOKEN_ENV`) | Opening MRs and posting notes. Scope `api`, and at least the Developer role on each project. With repo inference it also lists the catalog namespace. |
| `GITLAB_ISSUE_TOKEN` (`GITLAB_ISSUE_TOKEN_ENV`) | Optional. Comments on GitLab issues; falls back to `GITLAB_MR_TOKEN` when unset. Scope `api`, and at least the Reporter role on the issue's project. |
| `GITLAB_MERGE_TOKEN` (`GITLAB_MERGE_TOKEN_ENV`) | Only with the CD module or `AUTO_MERGE_REPOS`; falls back to `GITLAB_MR_TOKEN` when unset. Scope `api`, and a role that may merge into the target branch (Maintainer, or whatever the protected-branch rule requires). |
| `GITLAB_PUSH_TOKEN` | Only with `REPO_PUSH_BROAD_CRED_ENABLED`, see [repo-push-cred.md](repo-push-cred.md) |

Push credentials aren't environment variables: they live in the broker user's `~/.git-credentials`, see
[repo-push-cred.md](repo-push-cred.md).

The shipped unit has no `EnvironmentFile=`. Add one in a drop-in under `eph-broker.service.d/`, for example
`EnvironmentFile=/etc/eph-broker/eph-broker.env`, root-owned, mode 0600, and keep it out of `site.env` and out
of git. `/event` enforces signatures by default, so set `BROKER_HMAC_SECRET` and have your sender sign.
`BROKER_HMAC_MODE=log` accepts everything and only logs `HMAC-CHECK` lines; use it only while you add signing to
a sender that doesn't sign yet.

## Signing a prompt

The prompt text is the thing you're trusting, so you approve its hash yourself.

```sh
$EDITOR modules/scopes/general.md                 # or a manifest in workers/<domain>/<class>.json
python3 tools/render-prompts.py                   # rewrites prompts/<domain>/<class>.md
less prompts/general/agent.md                     # read the rendered text: this is your review
sha256sum prompts/general/agent.md                # put this digest in approved-prompts.json
$EDITOR approved-prompts.json                     # "general/agent": "<sha256>"  <- this edit is the sign-off
```

[prompts.md](prompts.md) explains manifests, modules, site overrides, inline sources and the render check.

## Models and backends

`FLEET_MODEL_SH` names a script the broker runs as `bash <script> <agent>` before each worker starts. The agent
names are `code-worker`, `general-worker` and `mr-reviewer` (renamed with `CODE_AGENT`, `GENERAL_AGENT` and
`MR_REVIEW_AGENT`), or a config row's `agent`. The script prints one line:

- a full Claude model id starting with `claude-` (for example `claude-sonnet-5`): native Claude Code on that
  model;
- or the name of a backend in the registry file below: the worker runs on that backend.

Anything else, an alias like `opus` included, is read as a backend name, and a name the registry doesn't have
stops the worker from starting (logged and escalated). Empty output, an error, a timeout (15 seconds) or an unset
`FLEET_MODEL_SH` gives `NATIVE_FALLBACK_MODEL`. A `model` pinned on a config row skips the script and goes to
`--model` as it is, so an alias works there.

`SPAWN_MODELS_JSON` names the registry file. Each backend is an entry under `backends`:

```json
{
  "backends": {
    "my-gateway": {
      "kind": "cloud",
      "model": "my-model-id",
      "base_url": "https://llm.example.com",
      "env": "/etc/eph-broker/my-gateway.env",
      "context_tokens": 200000,
      "effort": "high"
    }
  }
}
```

| Key | Meaning |
|---|---|
| `kind` | `cloud`, or `local` for a slow self-hosted model (its time limit is multiplied by `LOCAL_WALL_CLOCK_MULT`, default 4). Anything else isn't usable. |
| `model` | The model id sent to the backend. Required for `cloud`. Claude Code's opus, sonnet and haiku tiers all map to it. |
| `base_url` | Required for `cloud`. It becomes the worker's `ANTHROPIC_BASE_URL`, so the backend has to speak the Anthropic Messages API. |
| `env` | Optional path to a `KEY=VALUE` file the broker reads at spawn. Only `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` are passed on to the worker. A `local` backend also reads its base URL and model from this file (or from `VLLM_CC_ENV`). |
| `context_tokens` | Optional. Sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS`. |
| `effort` | Optional. Passed as `--effort`. |

`claude` always means native Claude Code and can't be redefined. A `cloud` backend whose `base_url` is exactly
`https://api.anthropic.com` counts as first-party: the code worker may run on it and the general worker keeps
the auto permission mode. That's also the way to run workers on an API key instead of the stored login. Any
other backend is non-Anthropic: the code worker refuses it unless `CODE_NONCLAUDE_OK=1`, and the general worker
loses the auto mode classifier ([security.md](security.md)).

## Choosing the reviewer's model

`MR_REVIEW_ADAPTER` picks the reviewer's harness, `cc` (the default), `hermes` or `codex`. The hermes harness
needs a non-Anthropic backend for the reviewer; the Codex harness needs a Codex model
([The Codex harness](#the-codex-harness)).

By default the reviewer asks `FLEET_MODEL_SH` for agent `mr-reviewer`, so a non-Claude answer runs it on that
backend. `MR_REVIEWER_BACKEND` pins a backend instead (`claude` means native Claude Code on the default model).

To review on native Claude Code with a model you pick, whatever `FLEET_MODEL_SH` says:

1. Set `MR_REVIEWER_MODEL` to a Claude model, e.g. `opus`.
2. Leave `MR_REVIEWER_BACKEND` unset, or set it to `claude`.
3. Set `MR_REVIEW_ADAPTER=cc`, because the hermes harness refuses native Claude.

A per-ticket `model:` label can't override this. To go back, unset `MR_REVIEWER_MODEL`. These are read at
startup, so restart the broker after changing them (`SIGHUP` isn't enough). This doesn't touch
`GENERAL_BACKEND`, which must stay unset (see [security-residual-same-uid.md](security-residual-same-uid.md)).

## Tiered MR review

With `MR_REVIEWER_MODEL` and `MR_REVIEWER_BACKEND` both unset, the reviewer's model is picked per MR from
the diff instead of being the same for every review, so review depth is proportional to risk:

| Tier | Default model | When it applies |
|---|---|---|
| `quick` | `haiku` (`MR_REVIEW_QUICK_MODEL_CODEX` on the Codex harness) | The diff is small (`MR_REVIEW_QUICK_MAX_DIFF_LINES`, default 20 changed lines), has no symlink/submodule/mode-only change, and EVERY changed path matches `MR_REVIEW_QUICK_PATHS` (a narrow plain-prose allowlist, by default `*.md,*.rst,*.txt`) and none matches `MR_REVIEW_QUICK_EXCLUDE_PATHS` (tests/build/dot-directory/prompt-module carve-outs excluded even on a prose extension, by default `tests/*,*/tests/*,.*/*,*/.*/*,CMakeLists.txt,*/CMakeLists.txt,*copilot-instructions*,charters/*,agents/*,*/agents/*,modules/*,*/modules/*,scopes/*,*/scopes/*,skills/*,*/skills/*`). Runs a short "does this break anything?" prompt instead of the full adversarial review. |
| `standard` | unchanged (`MR_REVIEWER_MODEL` / `FLEET_MODEL_SH`) | Everything that is neither `quick` nor `full`: ordinary code, including tests and build files. |
| `full` | `opus` | Any changed path matches `MR_REVIEW_FULL_PATHS` (agent-instruction/prompt-file globs, default `CLAUDE*.md,AGENTS*.md,FLEET.md,GEMINI.md,.claude/*,*/.claude/*,.codex/*,*/.codex/*,prompts/*,*/prompts/*,SKILL.md,commands/*.md,*/commands/*.md`) or one of cd.py's own Full-path glob categories for the MR's own repo (its generic sensitive/deploy categories plus any repo-specific one, e.g. eph-broker's "engine" category - read fresh per review via `cd._patterns`, so a site's `config/cd-tier.json` additions reach this tier too), or the diff is bigger than `MR_REVIEW_FULL_DIFF_LINES` (default 400 changed lines). |

Escalate, never downgrade: a `full` rule wins outright regardless of size or the quick-path list; `quick`
needs every rule to clear. Override a tier's model with `MR_REVIEW_QUICK_MODEL`, `MR_REVIEW_STANDARD_MODEL`
or `MR_REVIEW_FULL_MODEL`; widen the path lists with `MR_REVIEW_QUICK_PATHS` / `MR_REVIEW_QUICK_EXCLUDE_PATHS` /
`MR_REVIEW_FULL_PATHS` (comma-separated globs, matched against the full path and the basename,
case-insensitively; each env var only ADDS onto its built-in list, never replaces it); adjust the size
cutoffs with `MR_REVIEW_QUICK_MAX_DIFF_LINES` / `MR_REVIEW_FULL_DIFF_LINES`. `MR_REVIEWER_MODEL` or
`MR_REVIEWER_BACKEND`, if set, overrides every tier's model unconditionally (today's single-model behaviour,
unchanged) - the tier is still computed and still named on the MR note, it just doesn't pick a different
model while the pin holds. A per-ticket `model:`/`hermes`/`codex` label on the MR's source ticket also wins
over a tier's model pick, same precedence as everywhere else (an explicit choice beats a computed one). These
are read at startup, so restart the broker after changing them.

The `quick` tier's lighter prompt (`prompts/mr-review/quick.md`) needs its own operator sign-off
([prompts.md](prompts.md#signing)) before it runs; until then, a `quick`-tier review automatically falls back
to the standard review prompt (the full adversarial scope) while still using the tier's cheaper model.

## Choosing a row's harness

Every built-in worker row reads its own harness setting, `cc` (Claude Code) unless you set it:

| Row | Setting |
|---|---|
| code worker, issue intake | `CODE_CHANGE_ADAPTER`, `ISSUE_CODE_ADAPTER` |
| general worker | `GENERAL_ADAPTER` |
| MR reviewer | `MR_REVIEW_ADAPTER` |
| infra proposer, infra generalist | `INFRA_ADAPTER`, `INFRA_GENERAL_ADAPTER` (defaults to the infra proposer's value) |
| docs-wiki writer, read-only report | `WIKI_DOCS_ADAPTER`, `ANSIBLE_ADAPTER` |
| mail triage, chat inbox | `INBOX_TRIAGE_ADAPTER`, `INBOX_CHAT_ADAPTER` |

A harness that can't run a row's capability refuses every spawn on that row and alerts; it never falls back to
`cc`. Today hermes and Codex run `mr-review`, `write`, `code` and `general`, so the read-only report and the two
inbox rows stay on `cc`. Rows from `config/routes.json` always run on `cc`. These are read at startup.

For one ticket instead of a whole row, a `hermes` or `codex` label moves that ticket's worker, under the same
rules: only from a creator in `MODEL_LABEL_TRUSTED_CREATORS`, never on a row with a pinned backend, only for
`general` and `code`, and never both labels on one ticket.

## The Codex harness

The `codex` harness runs a worker's turns on the Codex CLI with its own ChatGPT login, instead of routing a
ChatGPT model through Claude Code. Each turn is one headless `codex exec --json` in the worktree, brief on stdin;
a forwarded event or the envelope re-prompt continues the same thread with `codex exec resume <thread id>`. The
worktree, branch, MR, test runner and result envelope are the same as on `cc`. How it works in detail is in
[adapters.md](adapters.md#the-codex-harness).

| Key | Meaning | If unset |
|---|---|---|
| `CODEX_ALLOW_PRERELEASE` | `1` lets `codex --version` match `CODEX_VERIFIED_VERSION` with a prerelease suffix attached (`0.164.0-rc1`), instead of refusing it outright. | A prerelease version is refused (fail-closed) |
| `CODEX_AUTH_FILE` | Absolute path of the Codex login file to use (normally the broker user's `~/.codex/auth.json` after `codex login`). Each worker's private home gets a symlink to it; the broker never reads or copies it. | Every Codex worker is refused |
| `CODEX_BACKEND` | The registry backend whose `codex` block gives the model when the row's own backend has none (see below). | The row's backend must have one |
| `CODEX_BIN` | The `codex` executable. | `codex` on `PATH` |
| `CODEX_HOME_ROOT` | Where the per-worker private `CODEX_HOME` directories go. Local disk: Codex keeps its thread files there. | `~/.cache/eph-codex-homes` |
| `CODEX_SANDBOX_NETWORK` | `1` gives write, code and general workers network access inside Codex's sandbox (for example to reach `mcp-ro.sh`). An explicit `Bash(mcp-ro.sh:*)`, `Bash(mcp-rw.sh:*)` or `Bash(mcp.sh:*)` allow rule also enables it unless denied. Auto grants and recall-only grants do not. The gate still judges every command. | Network off without an explicit MCP wrapper allow rule |

The model and reasoning effort come from a `codex` block on a backend in the registry file
([Models and backends](#models-and-backends)):

```json
{
  "backends": {
    "chatgpt": {"kind": "codex", "codex": {"model": "my-codex-model", "effort": "high"}}
  }
}
```

The harness uses the row's backend if that entry has a `codex` block, and otherwise the entry `CODEX_BACKEND`
names, so a row can be on `codex` while `FLEET_MODEL_SH` answers with a Claude model. `effort` is optional. A
malformed block, or no block at all, refuses the worker. A `codex`-only entry isn't usable by `cc` or hermes.

What each capability gets: `mr-review` runs in Codex's read-only sandbox; `write`, `code` and `general` run in
the workspace-write sandbox (writes confined to the worktree plus `/tmp` and `$TMPDIR`, network off unless
`CODEX_SANDBOX_NETWORK=1`).
Approvals are always off (`never`), since nobody could answer one. `code` also needs `CODE_NONCLAUDE_OK=1`, and
`general` sends every effectful call to the exec gate (`EXEC_GATE`), exactly as on hermes ([hermes.md](hermes.md)).
Anything else is refused. Codex has no per-turn tool-call cap, so the capability's time limit is the only hard cap.

Every tool call goes through eph-gate, the same rules a Claude Code worker of that capability gets, as a Codex
`PreToolUse` hook. Codex silently skips a hook it doesn't trust and lets a call through when a hook never answers,
so before each turn the broker records trust for exactly its own hook, asks Codex (`codex app-server`,
`hooks/list`) to confirm the hook is loaded, enabled and trusted, and runs the hook once to see it deny. If any
of that fails, the turn is refused and you get an alert. A Codex worker on a row with `USAGE_CAP_FALLBACK_ENABLED`
never takes the Anthropic fallback chain.

## Who counts as an agent

`config/provenance.json` (template `config/provenance.example.json`, loaded by `provenance.py`) decides which
ticket authors are agents rather than people, so the broker doesn't treat its own comments as human input. It
needs all five keys (empty lists are fine): `agent_basenames`, `handoff_doers`, `agent_pattern`,
`untrusted_authors`, `handoff_parked_labels`. An unknown key or a malformed file stops the broker.

## Live transcripts

Streaming a running worker's transcript to the dashboard is off by default. For Claude Code the stream reads
the CLI's own session file under `~/.claude/projects/`. For Codex the broker writes its own record as events
arrive: what the agent says, its reasoning summaries (only when `CODEX_REASONING_SUMMARY` asks for them), the
command lines it runs with their exit codes and the last 3000 characters of their output, a diff for each file
it edits inside the worktree, its plan items and the names of the MCP tools it calls. Every piece goes through
the same secret redaction as failure captures; diffs are taken between the redacted old and new file, so a
secret split across hunks is still caught. MCP tool arguments and results are never written. That is the same
audience and roughly the same content as the Claude Code session file, which is served as is. Hermes workers
show no transcript.

| Key | Meaning |
|---|---|
| `BROKER_STREAM_ENABLED` | `1` or `true` turns on `/stream/*` and gives each worker a session id. |
| `BROKER_STREAM_GROUP` | The group allowed to watch. Required when streaming is on; unset means every request gets a 403. |
| `BROKER_STREAM_USER_HEADER`, `BROKER_STREAM_GROUPS_HEADER` | The headers your auth proxy sets for the user name and group list. Required; your proxy must strip client-sent copies. |
| `BROKER_STREAM_GROUPS_SEP` | Separator for the group list (default `\|`). |

### Thinking summaries

Set `CC_SHOW_THINKING_SUMMARIES=1` in the **broker's** environment to add
`"showThinkingSummaries": true` to every Claude Code worker's `--settings` JSON, including resumed turns.
Unset, `0`, or other non-true values omit the key entirely; this does not override a deliberately loaded
user setting. Accepted true values are `1`, `true`, `yes`, `on` (case-insensitive).

The OSS default is **off**: not every user wants additional model summaries retained in session files or
shown to transcript viewers. Opt in when that debugging visibility is useful. This requests API-side
summaries, not raw private reasoning, and does not enable dashboard streaming or change its authorization.
The dashboard already renders non-empty thinking as expandable cards; empty/signature-only and redacted
blocks remain plain “Thought” rows. Enabling it cannot recover text from old empty blocks.

Codex has its own knob, `CODEX_REASONING_SUMMARY` (also off by default, see [settings](settings.md)): set it to
`auto`, `concise` or `detailed` and the broker's Codex transcript records the summaries Codex returns. An
unknown value refuses every Codex turn before Codex starts, with an error naming the knob. Hermes is unchanged.
Codex's parser takes final `agent_message` events, and
hermes' parser takes the final `result`; their failure captures filter other record types rather than
providing thinking to this dashboard. Hermes supports a backend `supports_reasoning` override, but that is
not a thinking-summary setting.

#### Post-merge ops canary

Ops reports installed Claude Code **2.1.318**. The checked-in JSONL regression fixture exercises replay and
dashboard rendering; it is representative test data, **not live installed-CLI evidence**. After merge,
ops must run the following as the dedicated broker OS user in a disposable directory, using its existing
Claude Code login (this makes a paid model call):

```sh
claude --version
sid=$(python -c 'import uuid; print(uuid.uuid4())')
claude -p --model sonnet --effort high --max-turns 2 --session-id "$sid" \
  --setting-sources '' --settings '{"showThinkingSummaries":true}' --output-format json \
  'Without tools: solve SEND + MORE = MONEY with distinct digits and explain how you checked it.'
python - "$sid" <<'PY'
import glob, json, os, sys
paths = glob.glob(os.path.expanduser("~/.claude/projects/*/" + sys.argv[1] + ".jsonl"))
assert len(paths) == 1, paths
blocks = [b for line in open(paths[0]) for b in
          (json.loads(line).get("message", {}).get("content", []) or []) if isinstance(b, dict)]
summaries = [b["thinking"] for b in blocks
             if b.get("type") == "thinking" and str(b.get("thinking", "")).strip()]
assert summaries, "No non-empty thinking summary: do not enable fleet-wide"
print("non-empty thinking summaries:", len(summaries), "lengths:", [len(s) for s in summaries])
PY
```

Record the version and the nonzero summary count/lengths in the MR/ticket (not signatures or sensitive
transcript content). Then set `CC_SHOW_THINKING_SUMMARIES=1` for the broker, restart it through the normal
ops procedure, and run a harmless CC worker ticket. With streaming configured, open its dashboard
transcript, expand **Thinking**, and confirm readable non-empty text; replay the completed session and
confirm the same text. An empty row, signature alone, or successful final answer is **not** a passing
canary. If it fails, leave the knob off and report the CLI version and result. Unset the knob to roll back.

## Dashboard actions

Off by default, and the dashboard stays read-only until you turn them on. With them on, someone in
`BROKER_ACTIONS_GROUP` gets buttons to cancel a run, send a live run a message, run a finished ticket again
(on its usual model or one you pick), pause and resume new runs, retry or abandon a launch a restart left
unsettled, and change each row's backend, model and concurrency cap. Each one goes through
`POST /actions/<verb>`.

The broker only listens on 127.0.0.1, but anything else on that host can still reach it and set any header it
wants, so trusting a username header alone isn't enough. Your proxy also sends a secret the broker checks, which
proves the request came through the proxy's SSO-protected route.

That last part matters. The secret only tells the broker "this came through the proxy", not "this person logged
in". Webhooks (`/event`, `/webhooks/gitlab`, `/webhooks/github`) can't sit behind SSO, so if the same proxy fronts them:

- Add the secret ONLY on the route that runs your SSO check. Never on a route that skips it.
- On every route that skips SSO, strip the username and groups headers a client might send.

Otherwise anyone who can reach the webhook route can claim to be in `BROKER_ACTIONS_GROUP`.

| Key | Meaning |
|---|---|
| `BROKER_ACTIONS_ENABLED` | `1` or `true` turns the actions on. |
| `BROKER_ACTIONS_GROUP` | The group allowed to act. Required; unset means every action gets a 403. Uses the same `BROKER_STREAM_USER_HEADER`, `BROKER_STREAM_GROUPS_HEADER` and `BROKER_STREAM_GROUPS_SEP` as live transcripts. |
| `BROKER_ACTIONS_PROXY_SECRET` (`BROKER_ACTIONS_PROXY_SECRET_ENV`) | The secret your proxy adds on its SSO-protected route. Required, at least 16 characters; unset or shorter means every action gets a 403. |
| `BROKER_ACTIONS_PROXY_HEADER` | The header that carries it (default `X-Broker-Proxy-Secret`). |
| `BROKER_ACTIONS_AUDIT_HANDLE` | Optional. Every action is also sent here as a message through `notify` (see [Notifications](#notifications)). |

With Caddy and an SSO forward-auth, the broker's site block looks like this. The webhook route skips SSO, so it
gets no secret and drops any identity headers a client sent. The SSO route drops a client's copy of the secret
and adds its own:

```caddy
broker.example.com {
	@webhooks path /event /webhooks/* /forge/*
	handle @webhooks {
		request_header -X-Auth-Username
		request_header -X-Auth-Groups
		request_header -X-Broker-Proxy-Secret
		reverse_proxy 127.0.0.1:8787
	}
	handle {
		forward_auth sso:9000 {
			uri /auth/verify
			copy_headers X-Auth-Username X-Auth-Groups
		}
		request_header -X-Broker-Proxy-Secret
		reverse_proxy 127.0.0.1:8787 {
			header_up X-Broker-Proxy-Secret {env.BROKER_ACTIONS_PROXY_SECRET}
		}
	}
}
```

This needs Caddy 2.11.2 or newer. Caddy 2.10.0 through 2.11.1 keep a client's own `X-Auth-Groups` when the SSO
server answers without one, so a logged-in user outside the group could add themselves to it
([Caddy advisory GHSA-7r4p-vjf4-gxv4](https://github.com/caddyserver/caddy/security/advisories/GHSA-7r4p-vjf4-gxv4)). The same goes
for live transcripts, which trust these headers too. On another proxy, make sure the SSO step replaces those
headers rather than appending to them: the broker refuses a request that carries any of them twice.

The dashboard and every JSON reply send `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors
'none'`, so another site can't frame the dashboard and trick a click onto a button.

Then set `BROKER_STREAM_USER_HEADER=X-Auth-Username`, `BROKER_STREAM_GROUPS_HEADER=X-Auth-Groups`, and
the same secret in the broker's environment.

Every action is logged as a `WEB-ACTION user=... verb=... target=... result=...` line, sent as an
`action.taken` event when the [events webhook](events.md) is on, and sent to `BROKER_ACTIONS_AUDIT_HANDLE` when
that is set. One that fails partway is recorded as `error-<what went wrong>`. A refused one always logs
`WEB-ACTION REJECT` with the reason, and at most one refusal a minute also goes to the event and the handle, so
someone probing shows up without flooding either.

What each action does:

- **Cancel run** kills the worker's turn and closes it. The ticket counts as finished, so later edits don't start
  it again on their own, and any update that arrived during the turn is dropped. **Run again** starts it.
- **Message** reaches the worker right after its next tool call, framed like a ticket comment from you. Claude
  Code and Codex workers get a `PostToolUse` hook each turn that adds the message to the model's context; a
  message the worker never sees mid-turn (the turn ends first, or the harness is Hermes, which has no such hook)
  arrives at the start of its next turn instead, never both. The conversation view shows each message as
  **Queued** until the worker reads it, then moves it to the moment it landed. Set `BROKER_STEER_MID_TURN=0` to
  always wait for the next turn. Queued messages and their delivery records live under `BROKER_STEER_ROOT`
  (default: a `steer` folder beside `EVENTS_LOG`), one folder per ticket; the records stay so an ended run's
  conversation still shows them. Put that folder outside `/tmp` and `$TMPDIR` (the default is under `$TMPDIR`):
  a Codex sandbox can write there, and so could reach other tickets' inboxes
  ([same-uid residual](security-residual-same-uid.md)). A message only goes to the main agent: a tool call made
  inside a Claude Code subagent never takes it, so it waits for the main agent's next tool call or the next turn.
- **Run again**, **pause new runs** and **retry / abandon** use the same code as `/rerun`, `/drain` and
  `/reconcile`. The model menu next to **Run again** runs that one run on the model or backend you pick, ahead
  of any `model:` label on the ticket. A row that pins its model (a `<ROW>_BACKEND` or `model_pin`) refuses
  the pick; change the row instead.
- **Settings > Workers** (header gear, or phone hamburger) edits the [routing overrides file](routing-overrides.md): each row's
  `backend` and `model_pin`. **Settings > Groups** edits the `row_concurrency` cap per group. **Default** removes your change and puts
  back the environment value. New runs pick the change up; running ones keep what they started with. The
  broker needs write access to that file and its directory, otherwise the card says so and stays read-only.
  The harness, the repository lock scope and `trusted_creators` can only be changed in the file itself,
  since they change what a worker may do. A `model_pin` must be `opus`, `sonnet`, `haiku`, a full `claude-*`
  id or a registry backend; an empty value is refused (send `null` for the default). The audit line names
  what the file actually changed, for example `target=code.model_pin:opus,cap.code:1`; a save too big to fit
  that line is refused. The settings group can move any row onto a non-Anthropic backend, so only put
  operators in it ([security.md](security.md)).
- Settings edits refuse to overwrite a hand edit that landed while they ran (reload and try again), and follow
  a symlinked overrides file to its target. The rewritten file keeps its mode but takes the broker's owner
  and group.

## The dashboard

Optional native UI tabs can run as separate supervised processes. Set
`BROKER_MODULES` and each module's `BROKER_MODULE_<NAME>_CMD` or `_UPSTREAM`,
`_GROUP`, and optionally `_SOCKET`, `_LABEL`, `_MAX_STREAMS`, `_VERSION_FILE`,
`_DISABLED_FILE`. All unset means no routes, tabs or threads. The complete
[module configuration and authoring guide](modules.md) includes a runnable
stdlib example, proxy/auth requirements, hot reload and the Fleet quick-view hook.

`/dash` is read-only unless [dashboard actions](#dashboard-actions) are on. The **Now**, **Runs** and **Queue** views cover the broker's own work
([operations.md](operations.md)). The **Fleet** view is optional, for long-running agents you operate yourself; it reads:

- `FLEET_STATUS_PATH`: one JSON status file for all agents, written by your own generator;
- the `FLEET_MODE:` line in the file at `FLEET_MD_PATH`;
- `SUPERVISOR_STATUS_PATH` if set, for health flags.

### Models in the dashboard

Both the run views and Fleet show a model, and both are careful about what that model means.

- **Runs** shows the model the broker actually launched that worker with, recorded when the turn starts
  (the resolved one, after any backend or per-ticket model selection). The harness and backend ride along
  when they are set. A worker from a payload that predates this reads `model unknown`.
- **Fleet** can only show what its status file tells it. An agent row's `model` is the model your generator
  reports as the configured, mode-resolved one; the dashboard says so in the chip's tooltip rather than
  presenting it as a running process. To show what an agent is *actually* running, have your generator add
  `runtime_model` (and optionally `runtime_model_at`, epoch seconds or ISO-8601) to each row. The dashboard
  then shows that model, and falls back to the configured one (tagged `stale` when the reading or the whole
  file is older than `FLEET_STALE_S`, 300 seconds) otherwise. An agent with neither field shows a dash. Neither field is required, and a row that lacks them renders exactly as before.

Nothing in the dashboard probes a process itself: it never runs `ps`, reads a transcript or calls a model.
It renders the metadata it is given, and shows a quiet dash when it has none.

The status file's `agents` list includes both standing agents and temporary fleet agents (TFAs).
Producers mark rows with `agent_type: "standing"` or `agent_type: "tfa"`; omitted types retain the
legacy standing-agent behavior. TFA `name` is the session owner, not the pool bot identity. Merge
active session leases into this list, excluding free leases and non-session owners, and deduplicate
owners (standing registry entries take precedence). A lease establishes identity only: leave
`running`, `runtime`, `model`, context and status fields null unless current authoritative metadata
supplies them. Never derive health from process existence or expose lease env paths or private prompts.
The Fleet view can filter standing agents and TFAs. A TFA without liveness or activity metadata reads
**Unknown**, not Idle or No session; stale roster data clears TFA liveness. Stood-down agents fold into a
"Show N stood-down agents" row.

**Working on** shows each row's `status` (or `state`) line. Add `status_source: "now"` when that line is the
agent's own one-line note written for humans (for example a `NOW.md` it keeps in its working directory), or
`status_source: "state"` when your generator scraped it from `STATE.md`; the dashboard mutes a `state` line so
you can tell a real status from a guess. The context bar fills against `context_ceiling` (or
`context_window` when there is no ceiling), with a tick at `context_floor`; send `context_tokens` as an
absolute count, and a row with only a percentage shows the percentage.

The broker can own keeping `FLEET_STATUS_PATH` fresh for you: set `FLEET_STATUS_GEN` to an executable that
(re)writes it, and the broker runs it in the background (never on the request thread) at most once every 60
seconds, with a 30s timeout, whenever something polls `/state`. The broker invokes it as
`FLEET_STATUS_GEN <FLEET_STATUS_PATH>`: the generator must write its output to `argv[1]`, not to some
default path of its own (a generator whose own default output path is relative resolves it against the
*broker's* cwd, which is not `FLEET_STATUS_PATH`). A slow, failing or hanging generator is logged
and otherwise ignored; the fleet tab just keeps showing whatever it last wrote (or "stale"/"not configured"
as before). Unset `FLEET_STATUS_GEN`, or unset `FLEET_STATUS_PATH`: nothing runs it, same as before this
existed, so bring your own timer/cron, or have an external job write the file directly.

Each agent is expected to keep a `STATE.md` (and optionally a `TASKS.md`) in its working directory. An agent
that stays busy for 3 hours without touching either file is flagged as silent. The dashboard has no login of its
own, so put an authenticating proxy in front of it.

## Other examples in `config/`

- `routes.example.json`: a starting point for `routes.json`, your own agent types ([routes.md](routes.md)).
- `inbox-accounts.example.json`: for the mail-triage row. It also needs `INBOX_MCP_CONFIG_DIR/<account>.json`,
  and no inbox prompt ships, so that row won't run until you write and sign one.
- `deploy-paths.example.json`: maps repos to a deploy hand-off; only read when `CD_ENABLED` is on
  ([cd-deploy-paths.md](cd-deploy-paths.md)).
- `cd-tier.json`: path globs that force the strictest review tier (no auto-merge) in `cd.py`.
