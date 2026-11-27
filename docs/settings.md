# All settings

Every environment variable the broker's code reads, with its default and what it does. Set them in
`config/site.env`, the `settings` section of `broker.yaml`, or the real environment
([configuration.md](configuration.md) explains which wins). Secrets only ever go in the environment.

Most people need the first few sections. Everything from [Other built-in rows](#other-built-in-rows) down
belongs to features that are off by default, and several were built for the original deployment.

Variables the broker only passes through to workers (`PATH`, `HOME`, `LANG`, `TMPDIR` and so on) aren't listed.
Harness-owned runtime variables such as `HERMES_HOME` are not operator settings either.
Flags count as on for `1`, `true`, `yes` or `on`, unless the row says otherwise.
For reload classification see [the README inventory](../README.md#runtime-settings-and-reload-inventory).
Runtime policy in `broker.yaml` settings applies on SIGHUP, overriding startup environment defaults.
Removing an entry restores its startup value. Bootstrap paths, ports and identities still require restart.

## Process and files

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8787` | Port on 127.0.0.1 the broker listens on. |
| `CONCURRENCY` | `5` | Most active turns at once. Positive integer; SIGHUP resizes the existing gate. Growth wakes waiters immediately; shrink never kills holders. |
| `BROKER_CONFIG_DIR` | unset in source; `/etc/eph-broker` in the RPM service | Bootstrap root for site JSON, broker config, `site.env`, `prompts/` and `approved-prompts.json`. Set in the process environment, not in the file it selects. Existing sites are never overwritten or filled in by upgrades. No runtime fallback to packaged files. |
| `CONFIG_DIR` | `BROKER_CONFIG_DIR`, else `config/` next to `app.py` | Legacy override for site JSON, broker config and `site.env`; does not move prompts or approvals. |
| `PROMPTS_DIR` | `BROKER_CONFIG_DIR/prompts`, else in-repo `prompts/` | Override for rendered worker prompts. |
| `APPROVED_PROMPTS` | `BROKER_CONFIG_DIR/approved-prompts.json`, else in-repo `approved-prompts.json` | Override for signed prompt hashes. |
| `BROKER_CONFIG` | `CONFIG_DIR/broker.yaml`, else `CONFIG_DIR/broker.json` if it exists | The optional single config file. YAML takes precedence when both exist. An explicit path that doesn't exist stops the broker. |
| `BROKER_SITE_ENV` | `CONFIG_DIR/site.env` | The settings file. An explicit path that doesn't exist stops the broker. |
| `BROKER_PROVENANCE_PATH` | `CONFIG_DIR/provenance.json` | Which accounts are agents ([configuration.md](configuration.md#who-counts-as-an-agent)). |
| `BROKER_PROVENANCE_NEUTRAL` | unset | `1` lets the broker start without a provenance file, with only built-in agent names. |
| `ROUTES_PATH` | `CONFIG_DIR/routes.json` | Your own agent types ([routes.md](routes.md)). |
| `ROUTE_WORKDIR_ALLOWLIST` | unset | Colon list of directories a `read` row in routes.json may run in as its `workdir`, loading that directory's own project settings ([routes.md](routes.md#running-in-an-agents-home-directory)). Unset: none. |
| `ROUTING_OVERRIDES_FILE` | `CONFIG_DIR/routing-overrides.json` | Atomic, last-good dashboard routing overrides. |
| `REPO_COMPANIONS_FILE` | `CONFIG_DIR/repo-companions.json` | Optional per-code-repo read-only source checkouts. Re-read on each cut; an explicit missing file refuses the run. See [configuration.md](configuration.md#companion-source-repositories). |
| `EVENTS_LOG` | `$TMPDIR/eph-broker/events.jsonl` | Every accepted event, one JSON line each. Its directory also holds the state files below. |
| `COMPLETED_PATH` | `completed.json` next to `EVENTS_LOG` | Tickets that finished a run. In memory only when `EVENTS_LOG` isn't set. |
| `BROKER_STATE_PATH` | `broker-state.sqlite3` beside an explicit `COMPLETED_PATH` | Private local journal for durable drain, FIFO intake and scheduler state. Without either path drain control returns 503. See [drain.md](drain.md). |
| `BROKER_DRAIN_ON_NEW_JOURNAL` | off | Startup-only bootstrap for a logically uninitialized journal. Commits initial drain with its schema before scheduling/listeners. Accepts `1/true/yes/on` or `0/false/no/off`, case-insensitive with surrounding whitespace ignored; unset is off, any other value (including empty) refuses startup. Enabled without durable storage also refuses startup. Existing journals retain their drain state, including implicit false when absent. See [drain.md](drain.md#starting-with-a-new-journal). |
| `MAX_COMPLETED` | `2000` | How many finished subjects are remembered. |
| `PENDING_MAX_AGE` | `3600` | Seconds. Legacy JSON pending entries older than this are dropped during import. Journaled pending work has no age expiry. |
| `WORKTREES_ROOT` | `$TMPDIR/eph-broker-worktrees` | Where worktrees for rows without their own base go. |
| `WORKER_ENV_PASSTHROUGH` | unset | Comma list of extra environment variable names a worker keeps from the broker env, on top of the fixed allowlist. Names containing `TOKEN`, `SECRET`, `KEY` or `PASSWORD` are refused. File edits apply on SIGHUP. |
| `WORKER_SANDBOX` | `bwrap` | Bubblewrap OS boundary for every worker harness. Missing bwrap or an unknown value refuses startup; `off` explicitly disables isolation. File edits apply on SIGHUP to future launches; invalid modes or unavailable bwrap retain the old setting. See [security.md](security.md#os-boundary-on-by-default). |
| `WORKER_SANDBOX_EXTRA_RO` | unset | Colon-separated absolute paths to bind read-only (also accepts `~`). Cannot restore host `/proc` or `/dev`. |
| `WORKER_SANDBOX_HIDE` | unset | Colon-separated absolute paths to hide in addition to the default secret paths. Hides always win; files become empty read-only files, directories empty tmpfs mounts. Include any external systemd env-file directory and sibling secrets. |
| `WORKER_MCP_SSH_CONFIG` | `~/.ssh/config` | Broker user's SSH config for MCP-granted workers. The managed launcher uses `ssh -F` with this file, or `/dev/null` when absent. Absolute path, `~` accepted. |
| `WORKER_MCP_SSH_CONTROL_BASE` | `/tmp` | Existing absolute parent directory (`~` accepted) for broker-owned per-worker `eph-ssh-*` directories, mode `0700`. Independent of `TMPDIR`. Keep this path short: the expanded socket path (40-byte `%C` hash), OpenSSH's temporary suffix and terminating NUL must fit in 104 bytes. An unavailable or overlong location disables multiplexing (`ControlMaster=no`, `ControlPath=none`), without removing SSH/resolver binds or network access. |
| `WORKER_MCP_SSH_FILES` | `known_hosts`, `id_ed25519`, `id_rsa` beside that config | Colon-separated individual files to bind read-only for MCP-granted workers. Set this to the actual known-hosts, identity, certificate and Include files your SSH config needs. Missing files are skipped. Directories, `.env`, `.*-env`, and broker config/env files are refused, including symlink targets. |
| `WORKER_MCP_RESOLVER_FILES` | `/etc/resolv.conf:/etc/hosts:/etc/nsswitch.conf` | Colon-separated resolver files restored read-only for MCP-granted workers, including symlink targets under private `/tmp`. Same file restrictions as `WORKER_MCP_SSH_FILES`. |
| `TURN_CAPTURE_DIR` | `turn-captures/` next to `EVENTS_LOG` | Size-capped stdout and stderr of failed turns, for debugging. Off when `EVENTS_LOG` isn't set. |
| `TURN_CAPTURE_KEEP` | `200` | Captures kept. |
| `TURN_CAPTURE_MAX_AGE_S` | 7 days | Captures older than this are removed. |
| `TURN_CAPTURE_MAX_CHARS` | `65536` | Characters kept per stream. |
| `GIT_CONFIG_GLOBAL` | Git's default | Ambient global Git config path. Setting it to `/dev/null` skips global safe-directory registration; worktree Git operations disable ambient config separately. |

## Batching and limits

Turn caps and time limits per capability are in `config/constraints.json`, not here.
Per-route `wall_clock_s` overrides are also available in `routes.json` and routing overrides.
See [wall-clock recovery](wall-clock-recovery.md) for cap precedence and recovery settings.

| Variable | Default | Meaning |
|---|---|---|
| `WORKER_AUTO_SALVAGE_ON_WALL_CLOCK` | off | If normal clean-commit publication did not succeed, snapshot killed `code` workers, including new source files, and push an unreviewed WIP branch before teardown. Salvage itself never opens an MR. |
| `WORKER_AUTO_CONTINUE_ON_WALL_CLOCK` | off | With auto-salvage and a durable journal, continue a ticket once from its saved SHA. A second kill alerts the owner instead. |
| `WORKER_SALVAGE_EXCLUDE_GLOBS` | `scratch/**,*/scratch/**,.scratch/**,*/.scratch/**,tmp/**,*/tmp/**,.env,*/.env,.env.*,*/.env.*,.*-env,*/.*-env` | Comma-separated, case-sensitive repo-relative globs for new salvage paths, including staged additions. Defaults exclude scratch directories and secret-looking files at any depth. Tracked edits/deletions are preserved regardless of these globs. Empty includes all new paths except broker metadata and root charters. |

`WORKER_SALVAGE_EXCLUDE_GLOBS` replaces the entire default list. Globs use Python `fnmatch` semantics:
`*` also matches `/`, so a root pattern plus its `*/` variant covers every depth. These are generic
precautions, not a secret scanner. Put site-specific exclusions in site configuration. For example,
append `,.pii-store/**,*/.pii-store/**` to the full default list in `broker.json` settings if your
deployment uses that private store. It is not a core default. Both salvage switches remain off by default.

### Inbox run guardrails (Claude Code)

To pause scheduled inbox launches for maintenance without losing wakes, configure
durable storage with `BROKER_STATE_PATH` and enable the existing `POST /drain`
switch. Drain-off resumes held wakes, including after restart. This covers all
accounts and both dispatch policies; see [scheduled inbox wakes](drain.md#scheduled-inbox-wakes).

The `inbox` triage capability defaults to 15 CLI turns and 1800 seconds per
invocation, independent of other rows. Override these with
`max_turns_by_capability.inbox` and `wall_clock_by_capability.inbox`.
`LOCAL_WALL_CLOCK_MULT` does not multiply the inbox time limit. `inbox-chat` and
all other rows retain their existing limits.

Each account in `inbox-accounts.json` (or `broker.yaml`'s `inbox-accounts`
section) accepts `batch_size`: an integer from 1 to 6, default `3` unseen
messages per run. The runtime brief tightens the signed prompt's six-message
ceiling, using batches of at most three. Stricter account posture still wins.
After one bounded pass the worker exits; remaining mail waits for the next
scheduled wake, rather than draining the backlog through warm forwards.
This is a model work bound, not a guarantee that arbitrary mail fits the turn
or time limit; those hard limits remain in force. Account changes apply on
broker restart.

`constraints.inbox_suppress_progress_caps` defaults to `true` (SIGHUP reload).
A turn or wall-clock cap still produces a truthful `blocked` receipt, but no
ops alert when the broker verifies distinct new ledger IDs since spawn.
Set it to `false` to alert on every cap as before. The broker reads the account's
JSONL ledger before and after the run, accepting string `id`, `gmail_id` or
`message_id` fields. Duplicate IDs, verdict/timestamp-only edits, removed IDs,
unreadable files and malformed/unknown records cannot establish progress and
still alert. Missing files start empty. No message contents or IDs are sent to
ops for this check. Usage ceilings, upstream errors and ordinary blocked
results still alert, even when mail was processed.

Terminal inbox runs do not advertise idle or accept another warm turn. During
cleanup the worker thread is still alive even after its `done` flag is set,
avoiding a false dead-thread FORCE-CLOSE. Both self-settle and real force-close
leave `gmail:*` keys eligible for the next wake, never completed-deduped.

`inbox_max_input_tokens` (default `3000000`) and `inbox_max_cost_usd` (default `2.0`)
are positive per-worker-run ceilings in the constraints section of
`config/constraints.json` or `broker.yaml`. Send SIGHUP to apply changes. Usage is
cumulative across forwards, retries and session rotation. Input includes uncached
input, cache reads and cache creation, not output tokens or context-window size.
The broker polls CC stream-json usage once a second, without needing dashboard
streaming. Dollar cost is computed by the broker from token usage and
`constraints.model_prices`, never CC's `total_cost_usd` or default Claude rates.
CC's wrapper-priced `--max-budget-usd` is not passed, including on local routes.
The broker kills paid invocations when their table-priced usage reaches the cap.
Limits can overshoot by an in-flight API response and polling delay.

`model_prices` maps exact launch and observed model ids (including backend remapping,
fallbacks and CC's internal routing) to USD per million tokens for all four fields: `input_tokens`,
`output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`.
Each rate must be finite and nonnegative. Configure all four as `0` for a free
local endpoint. There is no implicit free-local policy or default rate: a missing
launch model entry raises a clear error naming the model before the CLI launches.
Every model observed in assistant messages or the final `modelUsage` breakdown is
also validated. An unconfigured internally selected model raises a named error;
if the CLI is still running, the broker kills and reaps it first.
The `<synthetic>` placeholder needs no price entry when all token counts are zero; nonzero usage raises an error.
The shipped table is an example, not a live provider catalog. Review rates and
add every inbox model you use, including aliases and internally selected tiers,
then send SIGHUP. Existing configs
without a table must add one before inbox triage can run. Stream usage is deduplicated
per request and priced at each request's model rates. Final per-model token counts
fill gaps without double charging streamed usage; counts never decrease for an
identified model. A free launch model cannot make internally routed paid usage free.
For older wrappers without model identities, unattributed tokens use launch-model
rates until a final per-model breakdown is available. Without that breakdown, only
unaccounted aggregate tokens use launch-model rates. Use explicit model pins rather
than aliases that change pricing over time. These estimates are not provider invoices.

The input-token ceiling is a cost proxy, not a context-window limit. It defaults
off for spawn-registry backends with `kind: "local"`, where cumulative context
re-reads do not represent spend, and on for native, cloud and unknown backends.
For a billed local endpoint or a free hosted endpoint, set the per-row
`inbox_input_token_limit` in [routing overrides](routing-overrides.md) to `true`
or `false` respectively. Omit it or use `null` for automatic policy. For example:
`{"rows": {"inbox": {"inbox_input_token_limit": true}}}`.
Automatic policy uses the backend selected for each invocation, including
fallbacks, not the row's original default. Usage telemetry remains cumulative
across all backends even when enforcement is off; a later priced invocation
checks that total. Wall clock, CLI turn caps and the dollar ceiling remain active
regardless of the token policy. No backend credential files are read for this policy.

Previously inbox inherited the raised global 50-turn/1800-second limits (7200
seconds on a local backend with the default multiplier). A turn cap emits a CLI
error wrapper, not the worker's final envelope; SIGKILL can stop mid-tool-use with
no wrapper at all. Asking that capped session for JSON cannot guarantee recovery.
Inbox now ends on a turn, time or usage limit with a broker-generated `blocked`
envelope, no further model calls, and a warning to check partially applied mail
and ledger changes. Ordinary parse misses still get one recovery invocation,
limited to one CLI turn and `HARDKILL_REPROMPT_CAP_S` seconds.

Envelope escalations are reserved once per run id before either notification
sink. This is at-most-once notification *attempts*, not guaranteed delivery; a
failed sink or crash after reservation is not retried. Reservations survive
restart when the broker state journal is enabled; otherwise dedupe lasts for the
broker process. Subject rate limits remain separate and do not create duplicate
run escalations or suppression-tail DMs for duplicate calls.

### Batching environment settings

| Variable | Default | Meaning |
|---|---|---|
| `INITIAL_DEFAULT` | `3` | Seconds to collect a burst before a worker starts, when `windows.json` has no entry. |
| `RECEIVE_DEFAULT` | `45` | Seconds a warm worker waits for a follow-up, when `windows.json` has no entry. |
| `INITIAL_WINDOWS`, `RECEIVE_WINDOWS` | `{}` | JSON maps of subject type to seconds (for example `{"vikunja:task": 5}`), on top of the defaults. |
| `TICK` | `1` | Seconds between scheduler passes. |
| `ROW_CONCURRENCY` | unset | Per-row caps, `<row>=<n>,...`. A worker over its row's cap waits in the queue. |
| `DISPATCH_PRIORITY` | `reviews-first` | Pending MR reviews (including reruns) and valid `continue-mr:` fix rounds precede new tickets on the same repo. Repos are served fairly; a capped/debouncing repo does not hold unrelated repos. `fifo` restores strict durable FIFO and FIFO checkout-lock admission. Other values refuse startup. See [configuration.md](configuration.md#dispatch-priority). |
| `DISPATCH_METADATA_WAIT` | `5` | Seconds of grace from lookup start before ordinary admission falls back to unknown-target ordering while eligible same-row metadata is still resolving. Shared row-cap groups share the grace; known follow-ups and other rows can dispatch. `0` disables the grace. Only applies to `reviews-first`. |
| `MAX_SPAWNS_PER_HOUR` | `60` | Warning only: past it, each start logs `BREAKER-WOULD-TRIP`. Nothing is stopped. |
| `API_OVERLOAD_RETRY_DELAYS_S` | `120,300,600` | Back-off in seconds when the model API answers 429 or 529. Existing adapters read the live schedule when a retry sequence begins; an in-progress sequence keeps its schedule. Removing the setting restores the startup default. |
| `HARDKILL_REPROMPT_CAP_S` | `60` | Time limit for the one re-prompt after a worker was killed for running too long. |
| `LOCAL_WALL_CLOCK_MULT` | `4` | Time limit multiplier for a `local` registry backend. |

## Tracker tickets

| Variable | Default | Meaning |
|---|---|---|
| `BROKER_MCP_SH` | unset | The tracker script ([tracker-script.md](tracker-script.md)). Unset: no tracker ticket can start a worker. |
| `MAX_TICKET_TEXT` | `20000` | Positive integer character cap for rendered ticket text, read at startup. Invalid values log an error and use 20000. Related-task embeds are summarized first. Long descriptions that reach this cap disable label/creator trust, even if their metadata fits before the cut. Labelled tickets with untrusted metadata park with a ticket comment and ops alert instead of using the row default. A matching operator-owned repo model pin still runs on its pin without trusting labels. Ownership checks remain fail-closed for comment respawn and `/rerun`; `/rerun` returns 409 with the cause. Shorten the description or increase the cap and restart, then re-trigger. Duplicate fields and fallback renderings also fail closed; raising the cap does not make those trusted. |
| `VIKUNJA_URL` | unset | API root used by the optional `tools/vikunja_bridge.py` tracker bridge. Required when using that bridge. |
| `VIKUNJA_TOKEN` | unset | Token used by that bridge. Required, secret: provision only through the environment. |
| `TICKET_URL_TEMPLATE` | unset | Ticket link, `{}` is the id. Unset: plain `#<id>`. |
| `DASH_INSTANCE_NAME` | unset | Label shown next to the logo on the dashboard. |
| `PARKED_LABEL_ID` | unset | Tracker label id added when a worker returns `blocked`. Its title also belongs in `handoff_parked_labels`. |
| `MR_ROUTINE_OPS_DM` | `0` | `0`: no ops DM for "MR opened" or a quick-tier review PASS when the ticket comment posted (the comment is the record). FAIL/BLOCK, Full-tier PASS, review-skipped, worker errors, ticket-less MRs and a failed comment post still DM. `1`: rollback, DM every MR-opened and verdict. |
| `COMMENT_RESPAWN_ENABLED` | on | A human comment can restart a ticket whose worker exited ([agent-refire.md](agent-refire.md)). `0` turns it off. |
| `RERUN_CALLER` | `operator` | The name `/rerun` calls are logged and commented as. |
| `MODEL_LABEL_TRUSTED_CREATORS` | unset | Ticket creators whose `model:` or `hermes` label is honoured. |
| `MENTION_HANDOFF_ENABLED` | off | A human's mention of the code worker's user in a comment assigns it the ticket. |
| `MENTION_HANDOFF_USER` | `DEV_ASSIGNEE` | The user name to look for in the mention. |
| `MENTION_HANDOFF_UID`, `DEV_VIKUNJA_UID` | unset | The tracker user id to assign. Unset: no hand-off. |
| `MENTION_HANDOFF_PROJECTS` | unset | Comma list of project ids where a mention may hand off. Unset: none. |
| `MENTION_HANDOFF_DEDUP_S` | `300` | Seconds a repeated mention on one ticket is ignored. |

## Webhooks and secrets

The secrets themselves are in [configuration.md](configuration.md#secrets). These variables only rename them.

| Variable | Default | Meaning |
|---|---|---|
| `BROKER_HMAC_MODE` | `enforce` | `/event` signature check: `enforce`, `log` (check and log, accept everything) or `off`. |
| `HMAC_SECRET_ENV` | `BROKER_HMAC_SECRET` | Name of the variable holding the `/event` secret. |
| `BROKER_MAX_BODY` | `1048576` | Largest POST body in bytes; a bigger one gets 413, a bad `Content-Length` gets 400. |
| `GITLAB_WEBHOOK_TOKEN_ENV` | `GITLAB_WEBHOOK_TOKEN` | Name of the variable holding the GitLab webhook token. |
| `GITHUB_WEBHOOK_SECRET_ENV` | `GITHUB_WEBHOOK_SECRET` | Name of the HMAC-SHA256 secret for native `/webhooks/github`; unset secret refuses every delivery. |
| `GITHUB_TOKEN_ENV` | `GITHUB_TOKEN` | Name of the GitHub Issues write token; unset secret skips summary comments. |
| `GITHUB_BOT_LOGIN` | unset | Extra GitHub echo guard for a normal-user bot account (case-insensitive). GitHub Bot senders are always ignored. See [GitHub Issues](github-issues.md). |
| `RERUN_TOKEN_ENV` | `BROKER_RERUN_TOKEN` | Name of the variable holding the `/rerun` token. |
| `GITLAB_MR_TOKEN_ENV` | `GITLAB_MR_TOKEN` | Name of the variable holding the GitLab API token. |
| `GITLAB_ISSUE_TOKEN_ENV` | `GITLAB_ISSUE_TOKEN` | Name of the variable holding the GitLab issue-comment token (optional). |
| `GITLAB_MERGE_TOKEN_ENV` | `GITLAB_MERGE_TOKEN` | Name of the variable holding the merge token (CD and auto-merge). |
| `GITLAB_PUSH_TOKEN_ENV` | `GITLAB_PUSH_TOKEN` | Name of the variable holding the namespace push token ([repo-push-cred.md](repo-push-cred.md)). |
| `CATALOG_REFRESH_TOKEN_ENV` | `BROKER_CATALOG_REFRESH_TOKEN` | Name of the variable holding the `/catalog/refresh` token. |
| `EVENTS_WEBHOOK_SECRET_ENV` | `EVENTS_WEBHOOK_SECRET` | Name of the variable holding the run event signing secret. |
| `NOTIFY_WEBHOOK_SECRET_ENV` | `NOTIFY_WEBHOOK_SECRET` | Name of the variable holding the notification webhook secret. |
| `NOTIFY_SMTP_PASSWORD_ENV` | `NOTIFY_SMTP_PASSWORD` | Name of the variable holding the SMTP password. |

## GitLab and pushes

| Variable | Default | Meaning |
|---|---|---|
| `GITLAB_WEB_URL` | unset | GitLab web root. Unset: no MRs and no repo catalog. |
| `GITLAB_API` | `<GITLAB_WEB_URL>/api/v4` | GitLab API root. |
| `GITLAB_MR_TIMEOUT` | `15` | Seconds per GitLab API call. |
| `BROKER_PUSH_HOSTS` | unset | Comma list of bare host names a worker branch may be pushed to, over https. Unset: no pushes. |
| `REPO_CRED_FILE` | `~/.git-credentials` | The credential store the broker pushes with ([repo-push-cred.md](repo-push-cred.md)). |

### Worker commit identity

Workers use bot metadata for both Git author and committer on every harness, including resumed turns.
Broker WIP commits and wiki rebase retries retain the same row identity. This does not change the push
credential, remote, branch permissions or URL checks, and it does not rewrite existing commits.

Replace `<ROW>` below with the routing row key, uppercased with punctuation replaced by underscores:
`code` becomes `CODE`, `infra/general` becomes `INFRA_GENERAL`, and a config row `docs-writer` becomes
`DOCS_WRITER`. The default bot slug is the lowercase row key with punctuation replaced by dashes.

| Variable | Default | Meaning |
|---|---|---|
| `<ROW>_GIT_NAME` | `eph-<row-slug>` | Both `GIT_AUTHOR_NAME` and `GIT_COMMITTER_NAME`. |
| `<ROW>_GIT_EMAIL` | `eph-<row-slug>@noreply.invalid` | Both `GIT_AUTHOR_EMAIL` and `GIT_COMMITTER_EMAIL`. Use a server-approved bot address if push rules require one. |
| `<ROW>_GIT_IDENTITY_ENABLED` | on | Explicit per-row compatibility opt-out. Set off only if the existing push credential/server rules cannot accept bot metadata. Off preserves the previous identity behavior, including host Git config. Invalid boolean/identity values refuse the run, rather than silently using the operator. |
| `CODE_GIT_NAME` | `eph-code` | Code row's Git author and committer name. |
| `CODE_GIT_EMAIL` | `eph-code@noreply.invalid` | Code row's Git author and committer email. |
| `CODE_GIT_IDENTITY_ENABLED` | on | Code row's identity override; same validation and opt-out rules as above. |

These settings are operator configuration, not ticket fields. They override ambient, route and backend
identity environment variables. They are read at worker launch; restart after changing a settings file.
For example, `CODE_GIT_NAME=eph-code` and `CODE_GIT_EMAIL=eph-code@example.com` customize the code row.
Git metadata is not authenticated provenance: author names alone must not be used to trust a diff.
After rollout, check `git log -1 --format='%an <%ae> / %cn <%ce>'` and a broker-published feature branch
on your server; a local transport test cannot check server-specific author/email push rules.

## Issue intake

| Variable | Default | Meaning |
|---|---|---|
| `ISSUE_REPO_MAP` | unset | `<source>:<project>=<local path>,...`. Unset: every issue is refused. |
| `ISSUE_AUTHOR_ALLOWLIST` | unset | Users whose new issues may start a worker. Unset: nobody. `GET /health` reports the list the running process uses as `issue_author_allowlist` (sorted, lowercased, `[]` when unset), so a check can confirm an account is not admitted without reading config files that the environment may override. |
| `ISSUE_TRIGGERING_EVENTS` | `issues.opened,issue.opened,issue.open` | Issue events that start work. |
| `GITLAB_ISSUE_SOURCE`, `GITHUB_ISSUE_SOURCE` | `gitlab`, `github` | The event `source` of each issue row. Empty turns the row off. |
| `ISSUE_CODE_TEST_RUNNER` | unset | Test runner for issue workers when `CODE_REPO_RUNNER_SEEDS` has none for the repo. Unset: no test command. |
| `ISSUE_CODE_MR_TARGET_REF` | `main` | Fallback MR target; the repo's own default branch is used when known. |
| `ISSUE_CODE_BRANCH_PREFIX` | `eph/issue` | Branch name prefix. |
| `ISSUE_CODE_WORKTREE_BASE` | `~/.cache/ew/i` | Short root for issue worktrees; keep overrides short. Nested paths such as pytest `tmp_path` can still exceed AF_UNIX limits. |
| `ISSUE_CODE_BACKEND` | unset | Default registry backend for issue workers. Unset: `FLEET_MODEL_SH` decides. |
| `ISSUE_CODE_ZBUS_ENV` | unset | Chat identity file handed to the worker (zbus only). |

## Code worker

`CODE_REPO`, `CODE_REPO_PATH`, `ISSUE_CODE_REPO` and `ISSUE_CODE_REPO_PATH` are unused for tracker
code tickets: they are not fallback repositories. A tracker ticket must name a `repo:` token (unless the
explicitly enabled experimental repo-inference flow resolves it). GitLab/GitHub issues use `ISSUE_REPO_MAP`.
These four retired variables are no longer read, even when set. MR-note iterations also resolve through
`ISSUE_REPO_MAP`; `routes.json` code rows use their configured repo. Infra and general rows retain their
own separate repo settings.

| Variable | Default | Meaning |
|---|---|---|
| `CODE_ROW_LABEL` | `eph:code-change` | Label that starts the code row. Empty turns that trigger off. |
| `DEV_ASSIGNEE` | `eph-code` | Assignee that starts the code row. Empty turns that trigger off. |
| `CODE_REPO_ALLOWLIST` | unset | Colon list of repo paths it may work in. Unset: none. |
| `CODE_REPO_RUNNER_SEEDS` | unset | Test runner per repo, `<path>=<runner>,...` (`repo-test` runs the repo's own `./run-tests.sh`). The key must be the repo's real absolute path: a key on a path that does not exist as a directory never applies (the broker warns at startup and the repo falls through to the route default `CODE_TEST_RUNNER`). |
| `CONTINUE_MR_PREFIXES` | `eph/` | Colon list of branch prefixes a `continue-mr:` line may name. Each must end in `/`. Empty: nothing can be continued. |
| `TURN_CAP_PUBLISH_CLEAN_COMMIT` | `1` | Across all harnesses, publish a clean commit ahead of the cut through normal MR/review gates on any stop, including a wall-clock kill, turn cap, crash or lost result envelope. Clean excludes uncommitted tracked/staged changes, but ignores never-staged untracked files (including test scratch). Only committed HEAD is pushed. `0`: restore WIP-only recovery for unsuccessful runs. The historical flag name now covers all stop reasons; requires restart. |
| `WORKER_SAVE_COMMITS_ON_FAILURE` | `1` | Before removing an unsuccessful write worker's worktree, if normal publication did not succeed, push existing commits ahead of its cut to a separate ticket WIP branch and comment with branch/SHA links. Never stages or commits edits. `0`: disable this fallback recovery. Applies to all harnesses, including hard-kills, crashes, parse misses and failed publishes. |
| `CODE_REPO_RUNNERS` | unset | Same format; overrides the seeds per path. |
| `CODE_TEST_RUNNER` | `pytest` | Runner for a repo that's in neither map. |
| `EPH_GATE_TEST_ENV_ALLOW` | unset | Comma list of extra reviewed, exact environment names allowed before a code worker's granted test runner on hermes/Codex, on top of the benign defaults ([hermes.md](hermes.md#test-runner-environment-assignments-hermes-and-codex)). No wildcard patterns. Values remain confined. |
| `EPH_GATE_GIT_HISTORY_FORMS` | `["git diff", "git log", "git show"]` | JSON list selecting parsed read-only Git history families on every Hermes/Codex row. `[]` disables the extension. Unknown families refuse the turn. Path, secret and helper floors cannot be disabled ([hermes.md](hermes.md#read-only-git-history-hermes-and-codex)). |
| `GIT_EXTERNAL_DIFF` | unset | Runtime Git helper selector, not a broker setting. Eph-gate requires explicit `--no-ext-diff --no-textconv` for history inspection when this is present. |
| `CODE_TEST_DEPS_CONFIG` | unset | Operator-owned JSON map of absolute repo paths to Python package requirements. Mapped repos use the `venv` runner; the broker provisions `<repo>/.venv` before starting them. Setup failures are verification gaps, not pre-start refusals. |
| `CODE_MR_TARGET_REF` | `main` | Fallback MR target; the repo's own default branch is used when known. |
| `CODE_BRANCH_PREFIX` | `eph/code-change` | Branch name prefix. |
| `CODE_WORKTREE_BASE` | `~/.cache/ew/c` | Short root for code worktrees. Each code worker gets a private writable sibling `TMPDIR`, cleaned up at exit. Keep overrides short; nested paths such as pytest `tmp_path` can still exceed AF_UNIX limits. |
| `WORKER_ANSIBLE_TEMP_PREFIX` | `ansible-` | Directory-name prefix for private Ansible scratch inside a code worker's `TMPDIR` when its resolved runner is `ansible-syntax` (including `infra/general`). Must start with a letter, digit, `_` or `-` and contain only letters, digits, `_`, `-` or `.`. Both `ANSIBLE_LOCAL_TEMP` and `ANSIBLE_REMOTE_TEMP` point there, overriding inherited settings. Created before launch, reused on resume and removed with worker scratch at exit. Uses the existing scratch bind; no additional sandbox write access. |
| `CODE_AGENT` | `code-worker` | Agent name passed to `FLEET_MODEL_SH`. |
| `CODE_CHANGE_BACKEND` | unset | Default registry backend. Unset: `FLEET_MODEL_SH` decides. |
| `CODE_NONCLAUDE_OK` | off | Lets a code worker run on a non-Anthropic backend or on hermes. |
| `CODE_BROWSER_MCP_CONFIG` | unset | Isolated, validated Playwright stdio MCP config for the `code/ui` row only. Unset parks UI tickets with a waiting-on-you note and parked label before launch; other code/issue rows never inherit it. Invalid files still fail closed. |
| `CODE_JUDGED_BASH_ENABLED` | off | Opt into adjudicated code shell calls instead of the legacy allowlist. See the same-UID security tradeoffs in [security-residual-same-uid.md](security-residual-same-uid.md). |
| `CODE_PORTCULLIS_HOOK` | unset | Hook command for code shell adjudication. Set together with the gate directory; missing/invalid configuration retains the legacy allowlist. |
| `CODE_PORTCULLIS_GATE_DIR` | unset | Absolute worker-only gate state directory, armed with `mode=enforce` and `full=on`; required with the hook. |
| `CODE_ZBUS_ENV` | unset | Chat identity file handed to the worker (zbus only). |

### UI code worker

The `code/ui` row matches before generic code labels/assignees. It reuses the signed `code/change-proposer`
prompt and code capability, shares the code repo allowlist and per-repo runner maps, and publishes only a
feature-branch MR for human review. Its harness is fixed to `cc`, never `CODE_CHANGE_ADAPTER`; there is no
`CODE_UI_ADAPTER` setting. Non-cc harness overrides/choices are refused. See
[The code worker's browser](configuration.md#the-code-workers-browser).

Agent handoffs work when the ticket has both the exact UI label and the code assignee (`DEV_ASSIGNEE`).
The UI label alone does not admit an agent handoff, and events still drop while a worker runs on the subject.

| Variable | Default | Meaning |
|---|---|---|
| `CODE_UI_ROW_LABEL` | `eph:code-ui` | Exact label that selects the browser-required row. Assignees do not match. Empty disables it. |
| `CODE_UI_TEST_RUNNER` | `pytest` | Fallback runner key when the repo is in neither shared runner map. |
| `CODE_UI_ZBUS_ENV` | unset | UI worker chat identity file (zbus only). |
| `CODE_UI_BACKEND` | unset | Independent backend default; unset follows `FLEET_MODEL_SH`. |
| `CODE_UI_PIN` | `false` | Hard pin for model/backend choices, like `CODE_CHANGE_PIN`; the harness is always fixed to cc. |
| `CODE_UI_BRANCH_PREFIX` | `eph/code-change` | Feature-branch namespace, never a direct push to main. |
| `CODE_UI_MR_TARGET_REF` | `main` | Fallback MR target; the repo's own default branch wins when known. |
| `CODE_UI_WORKTREE_BASE` | `~/.cache/ew/c` | Short worktree root for UI code workers. |

## General worker

| Variable | Default | Meaning |
|---|---|---|
| `FLEET_ASSIGNEE`, `GENERAL_ROW_LABEL` | `eph`, `eph:general` | The assignee and label that start it. Empty turns that trigger off. |
| `GENERAL_REPO_ALLOWLIST` | unset | Colon list of repo paths it may work in. Unset: none. |
| `GENERAL_REPO`, `GENERAL_REPO_PATH` | unset | Its default repo when the ticket has no `repo:` line. Must be on the allowlist. |
| `GENERAL_MCP_CONFIG` | `~/.config/eph-broker/general-mcp.json` | The only MCP config it loads. Missing: it refuses to start. |
| `GENERAL_SETTING_SOURCES` | `user` | Which Claude Code settings it loads; empty loads none ([configuration.md](configuration.md#the-general-worker)). |
| `GENERAL_MR_TARGET_REF` | `main` | Fallback MR target. |
| `GENERAL_BRANCH_PREFIX` | `eph/general` | Branch name prefix. |
| `GENERAL_WORKTREE_BASE` | `~/.cache/eph-general-worktrees` | Where its worktrees go. |
| `GENERAL_AGENT` | `general-worker` | Agent name passed to `FLEET_MODEL_SH`. |
| `GENERAL_BACKEND` | unset | Default registry backend, overridable by ticket model labels. |
| `GENERAL_ZBUS_ENV` | unset | Chat identity file handed to the worker (zbus only). |

## MR comment iteration

Enable GitLab **Comments** events on the same authenticated `/webhooks/gitlab` hook used for issues.
An accepted note starts the existing code worker on the MR source branch. Notes arriving while it is live
are forwarded to that worker. The broker pushes follow-up commits onto the same branch, posts a marked
summary note, and requests a new review at the pushed SHA. It opens no new MR and never merges.

| Variable | Default | Meaning |
|---|---|---|
| `MR_NOTE_AUTHOR_ALLOWLIST` | empty (off) | Comma-separated GitLab usernames allowed to request follow-ups, case-insensitive. Separate from `ISSUE_AUTHOR_ALLOWLIST`. |
| `MR_ITERATION_MAX` | `3` | Maximum distinct accepted notes per project/MR, including warm forwards. The fourth note is ignored by default. Zero, negative or invalid values disable intake. Delivery retries of the same note ID do not consume another slot. Receipts survive restart with `BROKER_STATE_PATH`; without a journal they are in memory only. |
| `CD_WORKER_BRANCH_PREFIX` | `eph/` | Reused branch filter. Only valid `eph/*` branches under this prefix qualify; the MR target ref never qualifies. No dependency on `CD_ENABLED`. |

The project must have a local repo in `ISSUE_REPO_MAP` (`gitlab:group/project`), just like GitLab issues.
Notes cannot select a local path, branch, runner or tool grant. System notes, issue/commit notes, edited
notes, closed MRs, fork MRs, broker-marked notes and non-allowlisted authors are acknowledged with 200 and
ignored. A stale head pin, missing branch or non-fast-forward push fails closed. A re-review waits for any
older reviewer to close, using the existing durable re-review queue; the latest queued head wins.

The broker records the originating ticket ID by project/MR when publishing a code worker's `mr.opened`
event. That binding survives restart with `BROKER_STATE_PATH` (process-local without a journal). Iteration
and re-review workers use it to fetch the current ticket text; editable notes and MR descriptions cannot
change the binding. Notes also carry the MR title and description as context, including ticket-less MRs.

## MR reviewer

| Variable | Default | Meaning |
|---|---|---|
| `MR_REVIEW_ROW_SOURCE` | `mr` | Event source of the reviewer row. Empty turns review off. |
| `MR_REVIEW_REPO` | unset | Default repo; the MR's own repo wins. |
| `MR_REVIEW_WORKTREE_BASE` | `~/.cache/eph-mr-worktrees` | Where review checkouts go. |
| `MR_REVIEW_AGENT` | `mr-reviewer` | Agent name passed to `FLEET_MODEL_SH`. |
| `MR_REVIEWER_MODEL`, `MR_REVIEWER_BACKEND`, `MR_REVIEW_ADAPTER` | unset, unset, `cc` | The reviewer's model pin, backend default and harness default (`cc`, `hermes` or `codex`; [configuration.md](configuration.md#choosing-the-reviewers-model)). A model pin overrides every review tier. |
| `MR_REVIEW_QUICK_MODEL`, `MR_REVIEW_QUICK_MODEL_CODEX` | `haiku`, unset | Quick-tier reviewer model; the codex one is used instead on the Codex harness. |
| `MR_REVIEW_STANDARD_MODEL` | unset | Standard-tier reviewer model; unset keeps the row's existing resolution. |
| `MR_REVIEW_FULL_MODEL` | `opus` | Full-tier reviewer model. |
| `MR_REVIEW_QUICK_PATHS` | `*.md,*.rst,*.txt` | Comma globs; a quick-tier diff's changed paths must ALL match one (narrow prose-extension allowlist; add-only). |
| `MR_REVIEW_QUICK_EXCLUDE_PATHS` | `tests/*,*/tests/*,.*/*,*/.*/*,CMakeLists.txt,*/CMakeLists.txt,*copilot-instructions*,charters/*,agents/*,*/agents/*,modules/*,*/modules/*,scopes/*,*/scopes/*,skills/*,*/skills/*` | Comma globs; a path matching one of these is excluded from the quick allowlist above even if it also matches a quick extension (add-only, site may widen, never narrow). |
| `MR_REVIEW_QUICK_MAX_DIFF_LINES` | `20` | Changed-line ceiling for the quick tier. |
| `MR_REVIEW_FULL_PATHS` | `CLAUDE*.md,AGENTS*.md,FLEET.md,GEMINI.md,.claude/*,*/.claude/*,.codex/*,*/.codex/*,prompts/*,*/prompts/*,SKILL.md,commands/*.md,*/commands/*.md`, plus cd.py's own Full-path glob categories (generic sensitive/deploy categories and any repo-specific one for the MR's own repo), read fresh per review | Comma globs; any changed path matching one forces the full tier (add-only; a site may widen, never narrow). |
| `MR_REVIEW_FULL_DIFF_LINES` | `400` | Changed-line floor above which the diff forces the full tier. |
| `MR_REVIEW_SKIP_PATHS` | `STATE.md,TASKS.md,*-archive.md,content/*.md,*/content/*.md` | Additional comma globs unioned with these built-ins. All paths must qualify to skip review; Full rules, Quick exclusions and diff-safety guards still win. |

## Models and backends

CC local-thinking and upstream fail-fast switches are per-row booleans in `routing-overrides.json`:
`cc_disable_thinking_local` and `cc_fail_fast`, both default `true`. See
[local backend compatibility](configuration.md#claude-code-compatibility-with-local-backends).

| Variable | Default | Meaning |
|---|---|---|
| `FLEET_MODEL_SH` | unset | Script that prints a model id or backend name per agent ([configuration.md](configuration.md#models-and-backends)). |
| `NATIVE_FALLBACK_MODEL` | `claude-sonnet-5` | Model when the script is unset or fails. Must start with `claude-`. |
| `SPAWN_MODELS_JSON` | unset | The backend registry file. |
| `VLLM_CC_ENV` | unset | Env file a `local` backend reads its URL, token and model from, when its entry has no `env`. |
| `USAGE_CAP_FALLBACK_ENABLED` | off | Retry a worker on other backends when the Claude account hits its usage limit. |
| `USAGE_CAP_FALLBACK_CHAIN` | unset | Comma list of registry backends to try, in order. |
| `FLEET_USAGE_FILE` | unset | A JSON usage snapshot (`five_hour_pct`, `seven_day_pct`, ...) you write. At the thresholds below, the Claude attempt is skipped. |
| `USAGE_CAP_FIVE_HOUR_THRESHOLD`, `USAGE_CAP_SEVEN_DAY_THRESHOLD` | `100`, `100` | Percentages for the snapshot check. |

## hermes harness

| Variable | Default | Meaning |
|---|---|---|
| `HERMES_BIN` | `~/.local/share/eph-hermes/venv/bin/hermes` | The broker's own hermes install ([hermes.md](hermes.md)). |
| `HERMES_HOME_ROOT` | `~/.cache/eph-hermes-homes` | Where each hermes worker's private config home is created. |
| `EPH_GATE_NONSECRET_PATHS` | `{}` | JSON object of exact catalog `namespace/repo` keys to repo-relative non-secret path globs for the Hermes/Codex secret-name floor ([hermes.md](hermes.md#non-secret-filename-exceptions-hermes-and-codex)). |
| `CD_ACCEPT_HERMES` | off | Lets a hermes reviewer's PASS auto-merge under CD. |
| `EXEC_GATE` | `deny` | Who judges a general worker's shell and write calls: `deny`, `none`, `exec` or `portcullis` ([hermes.md](hermes.md#plugging-in-your-own-exec-gate-general-worker-only)). |
| `EXEC_GATE_COMMAND` | unset | With `exec`: the program that gets each request as JSON on stdin. |
| `EXEC_GATE_PORTCULLIS_WRAPPER` | unset (required) | With `portcullis`: absolute path of the hook-mode wrapper. |
| `EXEC_GATE_PORTCULLIS_HOOK` | unset (required) | Absolute path of the hook script. |
| `EXEC_GATE_PORTCULLIS_PYTHON` | unset (required) | Absolute path of the hook's interpreter. |
| `EXEC_GATE_PORTCULLIS_DIR` | unset (required) | Absolute gate state directory. |
| `EXEC_GATE_PORTCULLIS_PROJECT_DIR` | `EXEC_GATE_PORTCULLIS_DIR` | Neutral judge policy directory, outside the worker's write root. |
| `EXEC_GATE_TIMEOUT` | `5.0` | Seconds per gate decision. |
| `EXEC_GATE_INHERIT_ENV` | `TMPDIR,LANG,TZ` | Comma list of env names the gate process may inherit. |
| `EXEC_GATE_PROTECTED_TERMS` | unset | Extra literal names a general terminal command may never mention. |

## Codex harness

| Variable | Default | Meaning |
|---|---|---|
| `<ROW>_ADAPTER` | `cc` | A built-in row's harness: `cc`, `hermes` or `codex` ([configuration.md](configuration.md#choosing-a-rows-harness)). |
| `<ROW>_PIN` | `false` | Explicit hard pin: ignore per-ticket model/harness labels and refuse dashboard rerun model picks. Settings: `ANSIBLE_PIN`, `WIKI_DOCS_PIN`, `INBOX_TRIAGE_PIN`, `INBOX_CHAT_PIN`, `CODE_CHANGE_PIN`, `CODE_UI_PIN`, `ISSUE_CODE_PIN`, `INFRA_PIN`, `INFRA_GENERAL_PIN`, `MR_REVIEWER_PIN`, `GENERAL_PIN`, `TFA_PIN`. `INFRA_GENERAL_PIN` inherits `INFRA_PIN`. Accepts `1/true/yes/on` and `0/false/no/off`; invalid values stop startup. A row's `model_pin` remains pinned regardless. |
| `CODEX_AUTH_FILE` | unset | Absolute path of the Codex login file, symlinked into each worker's home, never read. Unset refuses every Codex worker ([configuration.md](configuration.md#the-codex-harness)). |
| `CODEX_BACKEND` | unset | Registry backend whose `codex` block gives the model when the row's backend has none. |
| `CODEX_BIN` | `codex` | The Codex CLI. |
| `CODEX_ALLOW_PRERELEASE` | off | Allow prerelease Codex CLI versions past the version guard. Stable minimum-version checks still apply. |
| `CODEX_HOME_ROOT` | `~/.cache/eph-codex-homes` | Where each Codex worker's private `CODEX_HOME` is created. |
| `CODEX_REASONING_SUMMARY` | empty | Codex `model_reasoning_summary` for every turn: `auto`, `concise`, `detailed` or `none`. Codex only reports a thought when the model returns a summary, so set this to put thoughts in the live transcript. Empty leaves the key out (Codex's default); any other value refuses every Codex turn with an error naming the knob. Check your model accepts it with one turn before rolling it out. |
| `CODEX_SANDBOX_NETWORK` | off | `1` enables network inside Codex's workspace-write sandbox. An explicit MCP wrapper allow rule also enables it; auto grants and recall-only grants do not. |

A Codex reviewer's PASS never auto-merges under CD: only `cc` verdicts (and hermes ones with `CD_ACCEPT_HERMES`)
do.

## Notifications

| Variable | Default | Meaning |
|---|---|---|
| `NOTIFY_BACKEND` | `zbus` | `webhook`, `email` or `zbus` ([configuration.md](configuration.md#notifications)). Anything else stops the broker. |
| `OPS_HANDLE` | unset | Who gets escalations. Unset: they're only logged. |
| `OPERATOR_DM_HANDLE` | unset | Human operator's private bus DM handle for inbox triage notifications, including OTPs. Always added to triage's allowed recipients, independently of `OPS_HANDLE` and `AGENT_DM_RECIPIENTS`. Set this to the human recipient named in your triage prompts. Restart after changing. |
| `AGENT_DM_RECIPIENTS` | `*-bot@*` | Comma-separated handles or `*` globs for workers with `ZBUS_ENV`, plus existing operator recipients. Empty disables bot DMs. No channel posts, absolute paths, chaining or expansion. In the Hermes general arm a valid DM is allowed before the exec judge. Restart after changing. |
| `AGENT_DM_DENY` | `*-driver-bot@*` | Comma-separated handles or `*` globs, validated like `AGENT_DM_RECIPIENTS`. Deny wins over bot and operator allowances in CC and the shared Hermes/Codex gate, including general before its exec judge. Recipients must be lowercase; allow/deny patterns are case-insensitive. Wrapped, substituted and newline/CR-separated bus calls are detected but never granted. The default excludes driver/control bots. Empty removes recipient exclusions, not the single-plain-call restrictions. Restart after changing. |
| `NOTIFY_WEBHOOK_URL` | unset | Where `webhook` posts. `https`, or `http` to localhost. |
| `NOTIFY_SMTP_HOST`, `NOTIFY_SMTP_PORT`, `NOTIFY_SMTP_USER`, `NOTIFY_SMTP_STARTTLS`, `NOTIFY_EMAIL_FROM` | unset, `587`, unset, on, unset | The `email` channel. |
| `NOTIFY_ZBUS`, `NOTIFY_ZBUS_ENV` | unset | The `zbus` chat CLI and its credentials file path. |
| `CD_ANSIBLE_HANDLE`, `INFRA_MR_NOTIFY_HANDLE` | unset | Recipients of the CD deploy hand-off line and of infra-row MR links. |
| `MR_REVIEWER_OF_RECORD` | `the reviewer of record` | Name used in the MR-opened DM to a row's `mr_notify_handle` (e.g. the infra deployer): who the MR is waiting on for review + merge. That DM never tells the handle to merge it themselves; deploy is theirs only after it merges. |

## Run events

| Variable | Default | Meaning |
|---|---|---|
| `EVENTS_WEBHOOK_URL` | unset | Where signed run events go ([events.md](events.md)). Unset: off. |
| `EVENTS_WEBHOOK_KINDS` | all | Comma list of the events to send. An unknown name stops the broker. |

## Merging and deploys

| Variable | Default | Meaning |
|---|---|---|
| `CD_ENABLED` | off | Turns on the CD module ([cd-deploy-paths.md](cd-deploy-paths.md)). |
| `CD_REPO_ALLOWLIST` | unset | Repos (`ns/name`) CD may merge. |
| `CD_MR_AUTHOR` | unset | GitLab user that opens worker MRs. Required for any automatic merge. |
| `CD_WORKER_BRANCH_PREFIX` | `eph/` | Only MRs from branches under this prefix can merge. |
| `AUTO_MERGE_REPOS` | unset | Repos whose worker MRs merge on a green pipeline with no reviewer. |
| `AUTO_MERGE_POLLS`, `AUTO_MERGE_POLL_S` | `12`, `10` | How many times, and how many seconds apart, the pipeline is checked. |
| `CD_RETRY_MAX` | `2000` | FIFO cap on remembered per-MR BLOCK retry/escalation records, not the number of retries per MR. |
| `CD_RETRY_PENDING_TTL_S` | `1800` | Seconds before an undelivered queued re-review expires and escalates. |

`config/deploy-paths.json` also accepts a per-repo `no_deploy_paths` list (default absent, no skipping),
for example `["docs/*", "tests/*", "README*"]`. These are case-sensitive full-path `fnmatch` globs,
including both old and new rename paths. A verified green default-branch pipeline skips its hand-off
only if every path changed since that repo's last successful hand-off matches. Skips are logged with
the full SHA, without DMs or ticket comments, and do not advance the baseline. Unknown baseline (also
after restart), invalid config or an inconclusive compare keeps deploying as before. Hot-reloaded on
SIGHUP; see [CD deploy paths](cd-deploy-paths.md#configdeploy-pathsjson) for comparison safety checks.

## Live transcripts and the dashboard

| Variable | Default | Meaning |
|---|---|---|
| `BROKER_STREAM_ENABLED` | off (`1` or `true` only) | Live worker transcripts on the dashboard ([configuration.md](configuration.md#live-transcripts)). |
| `CC_SHOW_THINKING_SUMMARIES` | off | Request Claude Code API-side thinking summaries in worker transcripts (`1`, `true`, `yes`, `on`). See [configuration.md](configuration.md#thinking-summaries). |
| `BROKER_STREAM_GROUP` | unset | The group allowed to watch. Required when streaming is on. |
| `BROKER_STREAM_USER_HEADER`, `BROKER_STREAM_GROUPS_HEADER` | unset | Headers your auth proxy sets. Required when streaming is on. |
| `BROKER_STREAM_GROUPS_SEP` | `\|` | Separator in the groups header. |
| `BROKER_STREAM_MAX_S` | `600` | Longest a single stream connection stays open, in seconds. |
| `BROKER_STREAM_MAX` | `CONCURRENCY` | Most streams open at once. |
| `BROKER_STREAM_HISTORY` | `stream-history.jsonl` next to `EVENTS_LOG` | Past sessions you can replay, with each one's ticket title and, once it ends, its model, harness, token total and end time. |
| `BROKER_ACTIONS_ENABLED` | off (`1` or `true` only) | Buttons on the dashboard that act on runs ([configuration.md](configuration.md#dashboard-actions)). |
| `BROKER_ACTIONS_GROUP` | unset | The group allowed to act. Required when actions are on. |
| `BROKER_ACTIONS_PROXY_SECRET_ENV` | `BROKER_ACTIONS_PROXY_SECRET` | Name of the variable holding the secret your proxy adds. Required when actions are on. |
| `BROKER_ACTIONS_PROXY_HEADER` | `X-Broker-Proxy-Secret` | Header that carries the proxy secret. |
| `BROKER_ACTIONS_AUDIT_HANDLE` | unset | Where each action is also sent as a message. |
| `BROKER_STEER_MID_TURN` | on | Deliver dashboard messages at the next main-agent tool call on supported harnesses. `0/false/no/off` waits until the turn boundary. |
| `BROKER_STEER_ROOT` | `steer/` next to `EVENTS_LOG` | Private per-subject message delivery state. Keep outside workers' writable paths when OS isolation is off. |
| `FLEET_STATUS_PATH`, `FLEET_MD_PATH`, `SUPERVISOR_STATUS_PATH` | unset | Sources for the optional Fleet view ([configuration.md](configuration.md#the-dashboard)). |
| `FLEET_STATUS_GEN` | unset | Executable the broker runs (background, at most once/60s, 30s timeout) as `FLEET_STATUS_GEN <FLEET_STATUS_PATH>` to refresh `FLEET_STATUS_PATH`; it must write argv[1], not its own default. Unset (either knob): nothing refreshes it for you. |
| `FLEET_CONTAINER_SOURCE` | unset | Opt-in Docker TFA discovery JSON file. See [configuration.md](configuration.md#container-tfas) and `config/fleet-containers.example.json`. Unset: no Docker calls or changes to the Fleet API output. |

### Optional dashboard modules

Module configuration is read at startup. Replace `<NAME>` with an enabled module's uppercase name.

| Variable | Default | Meaning |
|---|---|---|
| `BROKER_MODULES` | unset (off) | Comma list of unique lowercase module names, starting with a letter, using letters, digits and underscores. |
| `XDG_STATE_HOME` | `~/.local/state` | Module state base when `BROKER_STATE_PATH` is unset; module state goes under `eph-broker/`. Otherwise the journal's parent is used. |
| `BROKER_MODULE_<NAME>_CMD` | unset | Child command; configure exactly one of this or `UPSTREAM`. |
| `BROKER_MODULE_<NAME>_UPSTREAM` | unset | External absolute Unix socket or loopback HTTP URL; mutually exclusive with `CMD`. |
| `BROKER_MODULE_<NAME>_SOCKET` | `<state>/modules/<name>.sock` | Socket endpoint for a managed child. |
| `BROKER_MODULE_<NAME>_MAX_STREAMS` | `8` | Positive concurrent stream limit per module. |
| `BROKER_MODULE_<NAME>_LABEL` | title-cased name | Dashboard label. |
| `BROKER_MODULE_<NAME>_GROUP` | unset | Auth-proxy group required to access this module; unset denies access. |
| `BROKER_MODULE_<NAME>_VERSION_FILE` | unset | Optional file watched for version changes to restart the child. |
| `BROKER_MODULE_<NAME>_DISABLED_FILE` | unset | Optional marker file that disables the module while present. |

### Agent directory listener

The optional [agent directory](agent-directory.md#listeners-and-authentication) reads these at startup.

| Variable | Default | Meaning |
|---|---|---|
| `AGENTDIR_AGENT_BIND` | `127.0.0.1:8790` | Agent API TCP listener, separate from the UI socket. |
| `AGENTDIR_TAIL_LINES` | `2000` | Per-agent in-memory transcript ring, 1..10000 lines. Each line is at most 24 KiB. Invalid values refuse startup. Restart to apply. The tail client's separate `--lines` replay ring also defaults to 2000. |
| `AGENTDIR_QUICK_REPLIES` | three defaults | One-tap replies under the message box: a JSON list of up to 8 items, each a string or `{"label": "...", "text": "..."}` (label up to 40 characters, text up to 4000). `[]` turns them off. Invalid values log an error and use the defaults (Ask for status, Wrap up, Pause after this step). Restart to apply. |
| `AGENTDIR_WAKE_CHANNELS` | `{}` | Administrator-owned JSON map from directory name to its `wake` field: `null` or `"none"` disables waking; `{"kind":"zulip-dm","handle":"demo-bot@chat.example.com"}` sends idle-agent messages by DM instead of the tool-call inbox. Agents cannot supply or override recipients at registration. Invalid maps log an error and disable waking. Requires `steer-pull` registration. See [idle-agent delivery](agent-directory.md#idle-agent-delivery). Restart to apply. |
| `AGENTDIR_WAKE_ZBUS` | unset | Path to the `zbus` executable used by the optional `zulip-dm` wake transport. No shell; message text is passed on stdin. Unset or failed transport produces a visible not-delivered state. Each send has a five-second timeout. |
| `AGENTDIR_WAKE_ZBUS_ENV` | unset | Optional credentials-file path passed to the transport as `ZBUS_ENV`, never read by the directory. Unset inherits the directory process's `ZBUS_ENV` or the CLI default. Use a sender identity admitted by the target's bus listener. |
| `AGENTDIR_AGENT_MAX_CONNECTIONS` | `64` | Positive maximum concurrent agent API connections, including idle and keep-alive connections. Excess connections close immediately without an HTTP response. Invalid values log an error and use 64. Each connection has a 10-second socket idle timeout from setup, covering request lines, headers and bodies. UI listeners are unchanged. Restart to apply. |

## Other built-in rows

These rows ship without prompts. They stay off until you set their trigger, and then still need a signed
prompt ([configuration.md](configuration.md#built-in-rows)). Each one has `*_REPO`, `*_BACKEND`, `*_AGENT`,
`*_ZBUS_ENV` and branch and worktree settings like the code worker's.

| Variable | Default | Meaning |
|---|---|---|
| `ANSIBLE_ROW_LABEL`, `ANSIBLE_REPO`, `ANSIBLE_BACKEND`, `ANSIBLE_ZBUS_ENV` | unset | A read-only report row. |
| `WIKI_ROW_LABEL` | unset | Label enabling the docs writer. |
| `WIKI_REPO` | unset | Docs writer's repository reference. |
| `WIKI_REPO_PATH` | unset | Local wiki checkout. |
| `WIKI_CONTENT_SUBDIR` | `content` | Writable wiki content subtree. |
| `WIKI_PUBLISH_REF` | `main` | Branch published directly, without an MR. |
| `WIKI_WORKTREE_BASE` | `~/.cache/eph-wiki-worktrees` | Wiki worktree root. |
| `WIKI_AGENT` | `wiki-agent` | Agent name passed to `FLEET_MODEL_SH`. |
| `WIKI_DOCS_BACKEND` | unset | Default wiki backend. |
| `WIKI_ZBUS_ENV` | unset | Wiki worker's chat identity file. |
| `INFRA_ASSIGNEE` | unset | Assignee enabling the infrastructure proposer. |
| `INFRA_GENERAL_LABEL` | `infra:general` | Label enabling the infrastructure generalist. |
| `INFRA_REPO` | unset | Infrastructure repository reference. |
| `INFRA_REPO_PATH` | unset | Local infrastructure checkout. |
| `INFRA_TEST_RUNNER` | `none` | Proposer's granted test runner. |
| `INFRA_MR_TARGET_REF` | `master` | Fallback MR target, resolved per repo at spawn. |
| `INFRA_BRANCH_PREFIX` | `eph/infra` | Infrastructure feature branch prefix. |
| `INFRA_WORKTREE_BASE` | `~/.cache/eph-infra-worktrees` | Infrastructure worktree root. |
| `INFRA_AGENT` | `infra-worker` | Proposer agent name passed to `FLEET_MODEL_SH`. |
| `INFRA_BACKEND` | unset | Default proposer backend. |
| `INFRA_GENERAL_AGENT` | `INFRA_AGENT` | Generalist agent name. |
| `INFRA_GENERAL_BACKEND` | `INFRA_BACKEND` | Generalist backend. |
| `INFRA_ZBUS_ENV` | unset | Infrastructure workers' chat identity file. |
| `EPH_WRITE_GRANTS_ENABLED`, `EPH_WRITE_GRANT_DIR`, `EPH_WRITE_GRANT_TTL_S` | off, `~/.local/state/eph-broker/write-grants`, `3600` | Time-limited live-write grant files for the infrastructure rows. |
| `EPH_WRITE_GRANT_FILE`, `EPH_WRITE_GRANT_NONCE` | broker-managed | Worker-only wrapper contract for all harnesses: absolute grant path and matching nonce read from that file. Both absent without a grant; inherited or route/backend values cannot supply authority. |
| `EPH_WRITE_BUNDLES` | `grafana-edit,n8n-workflow-edit` | Comma-separated bundle names in new live-write grants. Replaces defaults; Proxmox is not a default bundle. |
| `EPH_WRITE_WRAPPERS` | `mcp-rw.sh` | Comma-separated bare script names. Allow-listed only for granted code routes; path forms always denied. Independent deny-floor command collisions reject the override, log an error and use the default list. Restart required. |
| `EPH_WRITE_AUDIT_TAGS` | `mcp-rw` | Comma-separated journal tags read for closing-comment audits; one shared 50-line cap. |
| `INBOX_ROW_SOURCE` | unset | Event source enabling mail triage. |
| `INBOX_REPO` | unset | Persistent mail ledger checkout. |
| `INBOX_MCP_CONFIG_DIR` | `~/.config/eph-broker/inbox-mcp` | One MCP config per mail account. |
| `INBOX_AGENT` | `mail-agent` | Agent name passed to `FLEET_MODEL_SH`. |
| `INBOX_TRIAGE_BACKEND` | unset | Default mail triage backend. |
| `INBOX_CHAT_BACKEND` | unset | Default mail chat backend. |
| `INBOX_ZBUS_ENV` | unset | Mail workers' chat identity file. |
| `BROKER_INBOX_ACCOUNTS` | `CONFIG_DIR/inbox-accounts.json` | Mail account configuration. |
| `BROKER_MAIL_ACCOUNT`, `BROKER_MAIL_TOOL`, `BROKER_MAIL_ARCHIVE` | unset | Broker-generated worker-only facts for a configured `kind: mail` run: account, command path, and archive opt-in (`0` or `1`). Not operator settings or config-file selectors; other rows receive none. |
| `FLEET_ASSIGNEE_ALT`, `SPAWN_SH`, `TFA_BACKEND`, `TFA_CWD_BASE`, `TFA_DEDUP_S` | unset, unset, unset, `~/.cache/eph-tfa`, `300` | The external launcher row: hands a ticket to your own script instead of starting a worker. |
| `EPH_WIKI_SKILLS_ENABLED`, `EPH_WIKI_SKILLS_DIR` | off, `~/.eph-broker/wiki-skills` | Passes a Claude Code plugin directory of skills to workers. |

Harness selection uses the exact routing names below (all default to `cc` except
`INFRA_GENERAL_ADAPTER`, which inherits `INFRA_ADAPTER`): `ANSIBLE_ADAPTER`, `WIKI_DOCS_ADAPTER`,
`INBOX_TRIAGE_ADAPTER`, `INBOX_CHAT_ADAPTER`, `CODE_CHANGE_ADAPTER`, `ISSUE_CODE_ADAPTER`,
`INFRA_ADAPTER`, `INFRA_GENERAL_ADAPTER`, `GENERAL_ADAPTER` and `MR_REVIEW_ADAPTER`.


## Chat wake

Built for the original deployment's Zulip setup and inert without its `config/zulip-wake.json`. All off by
default.

| Variable | Default | Meaning |
|---|---|---|
| `ZULIP_ROW_SOURCE` | unset | Event source of the chat row. |
| `ZULIP_WAKE_ENABLED`, `ZULIP_AUTOREPLY_ENABLED` | off | A direct message to a mapped bot starts a chat worker; a message to a retired bot gets one canned reply. Enabling either flag on reload starts reconciliation even if both were off at startup. Repeated reloads reuse the same loop; disabling stops listeners on the next reconciliation. |
| `ZULIP_WAKE_SENDERS`, `ZULIP_WAKE_BOT_SENDERS` | unset | Human user ids, and bot user ids, allowed to wake a bot. |
| `ZULIP_AUTOREPLY_EXCLUDE`, `ZULIP_AUTOREPLY_TEXT`, `ZULIP_AUTOREPLY_WINDOW_S` | unset, a neutral line, `86400` | The auto-reply's exclusions, text (`{bot}` is replaced) and once-per window. |
| `ZULIP_REGISTRY_PATH`, `ZULIP_MCP_SCRIPT`, `ZULIP_UV_BIN`, `ZULIP_STATE_DIR` | unset, unset, `uv` on `PATH`, next to `COMPLETED_PATH` | The bot registry, the listener script, its runner and the state directory. |
| `ZULIP_WAKE_POLL`, `ZULIP_AUTOREPLY_POLL`, `ZULIP_RECONCILE_S`, `ZULIP_MAX_AGE_S`, `ZULIP_HISTORY_TIMEOUT_S`, `ZULIP_REGISTRY_TTL_S`, `ZULIP_BOT_CACHE_TTL_S` | `2`, `10`, `60`, `300`, `3`, `300`, `3600` | Timings, in seconds. |
| `DM_HISTORY_MAX_CHARS` | `4000` | Earlier messages added to a chat worker's first turn. |
| `ZULIP_THREAD_ROOT` | `~/.cache/eph-thread-files` | Per-subject chat thread input files. |

## Repo inference (experimental)

Only read with `REPO_INFER_ENABLED=1` ([repo-push-cred.md](repo-push-cred.md#repo-inference-experimental)). Built
for the original deployment and still experimental.

| Variable | Default | Meaning |
|---|---|---|
| `REPO_INFER_ENABLED` | off | The code worker may pick a repo when the ticket has no `repo:` line. Worker MR pushes keep `ci.skip` unless the repo is CODE/CD-allowlisted and its CI config is unchanged ([details](repo-push-cred.md#worker-ci-with-inference-enabled)). |
| `REPO_INFER_SHADOW` | off | Log the pick it would make, change nothing. |
| `REPO_INFER_COMMENT_RERUN_ENABLED` | off | A human comment on a parked ticket runs the pick again. |
| `REPO_INFER_RERUN_USERS` | unset | Users besides the ticket's creator whose comment may do that. |
| `REPO_CATALOG_NS` | unset | GitLab namespace listed as the catalog. Unset: no catalog. |
| `REPO_TIER_B`, `REPO_DENY` | required | Globs: repos reachable only by explicit name, and repos never reachable. Empty is allowed, unset isn't. |
| `REPO_LOCAL_ROOTS`, `REPO_LOCAL_DIRS`, `REPO_LOCAL_MAP` | unset | Where existing checkouts are found (`REPO_LOCAL_MAP` is `ns/name=/path,...`). |
| `REPO_CLONE_ROOT` | `~/.cache/eph-repo-cache` | Where repos without a checkout are cloned. |
| `REPO_CATALOG_CACHE`, `REPO_CATALOG_TTL_S`, `REPO_CATALOG_HTTP_TIMEOUT` | `~/.cache/eph-broker/repo-catalog.json`, `3600`, `15` | Catalog cache and GitLab timeout. |
| `REPO_ALIASES_FILE` | `CONFIG_DIR/repo-aliases.json` | Extra names for repos. |
| `REPO_SUBPROJECT_REPOS`, `REPO_SUBPROJECT_STOPWORDS` | unset, a built-in list | Repos whose subdirectory names count as aliases, and words never used as one. |
| `REPO_WEAK_NAMES` | a built-in list | Repo names too common to count as a match on their own. |
| `REPO_INFER_SHORTLIST_K` | `8` | Candidates passed to the model steps. |
| `REPO_PUSH_BROAD_CRED_ENABLED` | off | One namespace token for pushes ([repo-push-cred.md](repo-push-cred.md)). |
| `LAYA_PICK_SOCK` | `~/.eph-broker/laya-pick.sock` | Socket of the local classifier `tools/laya_pick_daemon.py`. |
| `LAYA_MODEL`, `LAYA_PYTHONPATH` | `convaiinnovations/laya`, unset | Read by the daemon, not the broker. |
| `REPO_INFER_LAYA_TOP`, `REPO_INFER_LAYA_MARGIN`, `REPO_INFER_LAYA_TIMEOUT`, `REPO_INFER_LAYA_TEXT_CHARS` | `0.6`, `0.3`, `20`, `1000` | The classifier's pick needs this probability and this lead over the runner-up. |
| `REPO_INFER_4B_URL` | unset | OpenAI-compatible endpoint for the second opinion. Unset: that step is skipped and the ticket asks. |
| `REPO_INFER_4B_MODEL`, `REPO_INFER_4B_TIMEOUT`, `REPO_INFER_4B_MAX_TOKENS`, `REPO_INFER_4B_TEXT_CHARS` | `qwen3-4b-classifier`, `45`, `600`, `2000` | Its model name and limits. |
| `REPO_INFER_SHADOW_LOG` | `~/.cache/eph-broker/repo-infer-shadow.jsonl` | Where shadow picks are logged. |
