# All settings

Every environment variable the broker's code reads, with its default and what it does. Set them in
`config/site.env`, the `settings` section of `broker.yaml`, or the real environment
([configuration.md](configuration.md) explains which wins). Secrets only ever go in the environment.

Most people need the first few sections. Everything from [Other built-in rows](#other-built-in-rows) down
belongs to features that are off by default, and several were built for the original deployment.

Variables the broker only passes through to workers (`PATH`, `HOME`, `LANG`, `TMPDIR` and so on) aren't listed.
Flags count as on for `1`, `true`, `yes` or `on`, unless the row says otherwise.

## Process and files

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8787` | Port on 127.0.0.1 the broker listens on. |
| `CONCURRENCY` | `5` | Most workers running at once. |
| `CONFIG_DIR` | `config/` next to `app.py` | Where the `*.json` config files are read from. |
| `PROMPTS_DIR` | `prompts/` | Where rendered worker prompts are read from. |
| `APPROVED_PROMPTS` | `approved-prompts.json` | The file of signed prompt hashes. |
| `BROKER_CONFIG` | `config/broker.yaml` | The optional single config file. An explicit path that doesn't exist stops the broker. |
| `BROKER_SITE_ENV` | `config/site.env` | The settings file. An explicit path that doesn't exist stops the broker. |
| `BROKER_PROVENANCE_PATH` | `CONFIG_DIR/provenance.json` | Which accounts are agents ([configuration.md](configuration.md#who-counts-as-an-agent)). |
| `BROKER_PROVENANCE_NEUTRAL` | unset | `1` lets the broker start without a provenance file, with only built-in agent names. |
| `ROUTES_PATH` | `CONFIG_DIR/routes.json` | Your own agent types ([routes.md](routes.md)). |
| `EVENTS_LOG` | `$TMPDIR/eph-broker/events.jsonl` | Every accepted event, one JSON line each. Its directory also holds the state files below. |
| `COMPLETED_PATH` | `completed.json` next to `EVENTS_LOG` | Tickets that finished a run. In memory only when `EVENTS_LOG` isn't set. |
| `MAX_COMPLETED` | `2000` | How many finished subjects are remembered. |
| `PENDING_MAX_AGE` | `3600` | Seconds. A saved, not yet started worker older than this is dropped at startup. |
| `WORKTREES_ROOT` | `$TMPDIR/eph-broker-worktrees` | Where worktrees for rows without their own base go. |
| `TURN_CAPTURE_DIR` | `turn-captures/` next to `EVENTS_LOG` | Size-capped stdout and stderr of failed turns, for debugging. Off when `EVENTS_LOG` isn't set. |
| `TURN_CAPTURE_KEEP` | `200` | Captures kept. |
| `TURN_CAPTURE_MAX_AGE_S` | 7 days | Captures older than this are removed. |
| `TURN_CAPTURE_MAX_CHARS` | `65536` | Characters kept per stream. |

## Batching and limits

Turn caps and time limits per capability are in `config/constraints.json`, not here.

| Variable | Default | Meaning |
|---|---|---|
| `INITIAL_DEFAULT` | `3` | Seconds to collect a burst before a worker starts, when `windows.json` has no entry. |
| `RECEIVE_DEFAULT` | `45` | Seconds a warm worker waits for a follow-up, when `windows.json` has no entry. |
| `INITIAL_WINDOWS`, `RECEIVE_WINDOWS` | `{}` | JSON maps of subject type to seconds (for example `{"vikunja:task": 5}`), on top of the defaults. |
| `TICK` | `1` | Seconds between scheduler passes. |
| `ROW_CONCURRENCY` | unset | Per-row caps, `<row>=<n>,...`. A worker over its row's cap waits in the queue. |
| `MAX_SPAWNS_PER_HOUR` | `60` | Warning only: past it, each start logs `BREAKER-WOULD-TRIP`. Nothing is stopped. |
| `API_OVERLOAD_RETRY_DELAYS_S` | `120,300,600` | Back-off in seconds when the model API answers 429 or 529. |
| `HARDKILL_REPROMPT_CAP_S` | `60` | Time limit for the one re-prompt after a worker was killed for running too long. |
| `LOCAL_WALL_CLOCK_MULT` | `4` | Time limit multiplier for a `local` registry backend. |

## Tracker tickets

| Variable | Default | Meaning |
|---|---|---|
| `BROKER_MCP_SH` | unset | The tracker script ([tracker-script.md](tracker-script.md)). Unset: no tracker ticket can start a worker. |
| `TICKET_URL_TEMPLATE` | unset | Ticket link, `{}` is the id. Unset: plain `#<id>`. |
| `PARKED_LABEL_ID` | unset | Tracker label id added when a worker returns `blocked`. Its title also belongs in `handoff_parked_labels`. |
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
| `RERUN_TOKEN_ENV` | `BROKER_RERUN_TOKEN` | Name of the variable holding the `/rerun` token. |
| `GITLAB_MR_TOKEN_ENV` | `GITLAB_MR_TOKEN` | Name of the variable holding the GitLab API token. |
| `GITLAB_ISSUE_TOKEN_ENV` | `GITLAB_ISSUE_TOKEN` | Name of the variable holding the GitLab issue-comment token (optional). |
| `GITLAB_MERGE_TOKEN_ENV` | `GITLAB_MERGE_TOKEN` | Name of the variable holding the merge token (CD and auto-merge). |
| `GITLAB_PUSH_TOKEN_ENV` | `GITLAB_PUSH_TOKEN` | Name of the variable holding the namespace push token ([repo-push-cred.md](repo-push-cred.md)). |
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

## Issue intake

| Variable | Default | Meaning |
|---|---|---|
| `ISSUE_REPO_MAP` | unset | `<source>:<project>=<local path>,...`. Unset: every issue is refused. |
| `ISSUE_AUTHOR_ALLOWLIST` | unset | Users whose new issues may start a worker. Unset: nobody. |
| `ISSUE_TRIGGERING_EVENTS` | `issues.opened,issue.opened,issue.open` | Issue events that start work. |
| `GITLAB_ISSUE_SOURCE`, `GITHUB_ISSUE_SOURCE` | `gitlab`, `github` | The event `source` of each issue row. Empty turns the row off. |
| `ISSUE_CODE_TEST_RUNNER` | unset | Test runner for issue workers when `CODE_REPO_RUNNER_SEEDS` has none for the repo. Unset: no test command. |
| `ISSUE_CODE_MR_TARGET_REF` | `main` | Fallback MR target; the repo's own default branch is used when known. |
| `ISSUE_CODE_BRANCH_PREFIX` | `eph/issue` | Branch name prefix. |
| `ISSUE_CODE_WORKTREE_BASE` | `~/.cache/eph-issue-worktrees` | Where issue worktrees go. |
| `ISSUE_CODE_BACKEND` | unset | Pins a registry backend for issue workers. Unset: `FLEET_MODEL_SH` decides. |
| `ISSUE_CODE_REPO`, `ISSUE_CODE_REPO_PATH` | unset | Not used: each issue's repo comes from `ISSUE_REPO_MAP`. |
| `ISSUE_CODE_ZBUS_ENV` | unset | Chat identity file handed to the worker (zbus only). |

## Code worker

| Variable | Default | Meaning |
|---|---|---|
| `CODE_ROW_LABEL`, `DEV_ASSIGNEE` | `eph:code-change`, `eph-code` | The label and assignee that start it. Empty turns that trigger off. |
| `CODE_REPO_ALLOWLIST` | unset | Colon list of repo paths it may work in. Unset: none. |
| `CODE_REPO_RUNNER_SEEDS` | unset | Test runner per repo, `<path>=<runner>,...`. |
| `CODE_REPO_RUNNERS` | unset | Same format; overrides the seeds per path. |
| `CODE_TEST_RUNNER` | `pytest` | Runner for a repo that's in neither map. |
| `CODE_MR_TARGET_REF` | `main` | Fallback MR target; the repo's own default branch is used when known. |
| `CODE_BRANCH_PREFIX` | `eph/code-change` | Branch name prefix. |
| `CODE_WORKTREE_BASE` | `~/.cache/eph-code-worktrees` | Where its worktrees go. |
| `CODE_AGENT` | `code-worker` | Agent name passed to `FLEET_MODEL_SH`. |
| `CODE_CHANGE_BACKEND` | unset | Pins a registry backend. Unset: `FLEET_MODEL_SH` decides. |
| `CODE_NONCLAUDE_OK` | off | Lets a code worker run on a non-Anthropic backend or on hermes. |
| `CODE_REPO`, `CODE_REPO_PATH` | unset | Not a fallback: a tracker code ticket has to name its repo. |
| `CODE_ZBUS_ENV` | unset | Chat identity file handed to the worker (zbus only). |

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
| `GENERAL_BACKEND` | unset | Pins a registry backend. Keep it unset ([security-residual-same-uid.md](security-residual-same-uid.md)). |
| `GENERAL_ZBUS_ENV` | unset | Chat identity file handed to the worker (zbus only). |

## MR reviewer

| Variable | Default | Meaning |
|---|---|---|
| `MR_REVIEW_ROW_SOURCE` | `mr` | Event source of the reviewer row. Empty turns review off. |
| `MR_REVIEW_REPO` | unset | Default repo; the MR's own repo wins. |
| `MR_REVIEW_WORKTREE_BASE` | `~/.cache/eph-mr-worktrees` | Where review checkouts go. |
| `MR_REVIEW_AGENT` | `mr-reviewer` | Agent name passed to `FLEET_MODEL_SH`. |
| `MR_REVIEWER_MODEL`, `MR_REVIEWER_BACKEND`, `MR_REVIEW_ADAPTER` | unset, unset, `cc` | The reviewer's model, backend and harness ([configuration.md](configuration.md#choosing-the-reviewers-model)). |

## Models and backends

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
| `CD_ACCEPT_HERMES` | off | Lets a hermes reviewer's PASS auto-merge under CD. |

## Notifications

| Variable | Default | Meaning |
|---|---|---|
| `NOTIFY_BACKEND` | `zbus` | `webhook`, `email` or `zbus` ([configuration.md](configuration.md#notifications)). Anything else stops the broker. |
| `OPS_HANDLE` | unset | Who gets escalations. Unset: they're only logged. |
| `NOTIFY_WEBHOOK_URL` | unset | Where `webhook` posts. `https`, or `http` to localhost. |
| `NOTIFY_SMTP_HOST`, `NOTIFY_SMTP_PORT`, `NOTIFY_SMTP_USER`, `NOTIFY_SMTP_STARTTLS`, `NOTIFY_EMAIL_FROM` | unset, `587`, unset, on, unset | The `email` channel. |
| `NOTIFY_ZBUS`, `NOTIFY_ZBUS_ENV` | unset | The `zbus` chat CLI and its credentials file path. |
| `CD_ANSIBLE_HANDLE`, `INFRA_MR_NOTIFY_HANDLE` | unset | Recipients of the CD deploy hand-off line and of infra-row MR links. |

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

## Live transcripts and the dashboard

| Variable | Default | Meaning |
|---|---|---|
| `BROKER_STREAM_ENABLED` | off (`1` or `true` only) | Live worker transcripts on the dashboard ([configuration.md](configuration.md#live-transcripts)). |
| `BROKER_STREAM_GROUP` | unset | The group allowed to watch. Required when streaming is on. |
| `BROKER_STREAM_USER_HEADER`, `BROKER_STREAM_GROUPS_HEADER` | unset | Headers your auth proxy sets. Required when streaming is on. |
| `BROKER_STREAM_GROUPS_SEP` | `\|` | Separator in the groups header. |
| `BROKER_STREAM_MAX_S` | `600` | Longest a single stream connection stays open, in seconds. |
| `BROKER_STREAM_MAX` | `CONCURRENCY` | Most streams open at once. |
| `BROKER_STREAM_HISTORY` | `stream-history.jsonl` next to `EVENTS_LOG` | Past sessions you can replay. |
| `FLEET_STATUS_PATH`, `FLEET_MD_PATH`, `SUPERVISOR_STATUS_PATH` | unset | Sources for the optional Fleet tab ([configuration.md](configuration.md#the-dashboard)). |

## Other built-in rows

These rows ship without prompts. They stay off until you set their trigger, and then still need a signed
prompt ([configuration.md](configuration.md#built-in-rows)). Each one has `*_REPO`, `*_BACKEND`, `*_AGENT`,
`*_ZBUS_ENV` and branch and worktree settings like the code worker's.

| Variable | Default | Meaning |
|---|---|---|
| `ANSIBLE_ROW_LABEL`, `ANSIBLE_REPO`, `ANSIBLE_BACKEND`, `ANSIBLE_ZBUS_ENV` | unset | A read-only report row. |
| `WIKI_ROW_LABEL`, `WIKI_REPO`, `WIKI_REPO_PATH`, `WIKI_CONTENT_SUBDIR`, `WIKI_PUBLISH_REF`, `WIKI_WORKTREE_BASE`, `WIKI_AGENT`, `WIKI_DOCS_BACKEND`, `WIKI_ZBUS_ENV` | unset, ..., `content`, `main`, ... | A docs writer that pushes straight to `WIKI_PUBLISH_REF` with no MR. |
| `INFRA_ASSIGNEE`, `INFRA_GENERAL_LABEL`, `INFRA_REPO`, `INFRA_REPO_PATH`, `INFRA_TEST_RUNNER`, `INFRA_MR_TARGET_REF`, `INFRA_BRANCH_PREFIX`, `INFRA_WORKTREE_BASE`, `INFRA_AGENT`, `INFRA_BACKEND`, `INFRA_GENERAL_AGENT`, `INFRA_GENERAL_BACKEND`, `INFRA_ZBUS_ENV` | unset, `infra:general`, ... | An infrastructure proposer (code grant) and an infrastructure generalist. |
| `EPH_WRITE_GRANTS_ENABLED`, `EPH_WRITE_GRANT_DIR`, `EPH_WRITE_GRANT_TTL_S` | off, `~/.local/state/eph-broker/write-grants`, `3600` | Time-limited live-write grant files for the infrastructure rows. |
| `INBOX_ROW_SOURCE`, `INBOX_REPO`, `INBOX_MCP_CONFIG_DIR`, `INBOX_AGENT`, `INBOX_TRIAGE_BACKEND`, `INBOX_CHAT_BACKEND`, `INBOX_ZBUS_ENV`, `BROKER_INBOX_ACCOUNTS` | unset, ..., `~/.config/eph-broker/inbox-mcp`, ... | Mail triage, one MCP config per account. |
| `FLEET_ASSIGNEE_ALT`, `SPAWN_SH`, `TFA_BACKEND`, `TFA_CWD_BASE`, `TFA_DEDUP_S` | unset, unset, unset, `~/.cache/eph-tfa`, `300` | The external launcher row: hands a ticket to your own script instead of starting a worker. |
| `EPH_WIKI_SKILLS_ENABLED`, `EPH_WIKI_SKILLS_DIR` | off, `~/.eph-broker/wiki-skills` | Passes a Claude Code plugin directory of skills to workers. |

## Chat wake

Built for the original deployment's Zulip setup and inert without its `config/zulip-wake.json`. All off by
default.

| Variable | Default | Meaning |
|---|---|---|
| `ZULIP_ROW_SOURCE` | unset | Event source of the chat row. |
| `ZULIP_WAKE_ENABLED`, `ZULIP_AUTOREPLY_ENABLED` | off | A direct message to a mapped bot starts a chat worker; a message to a retired bot gets one canned reply. |
| `ZULIP_WAKE_SENDERS`, `ZULIP_WAKE_BOT_SENDERS` | unset | Human user ids, and bot user ids, allowed to wake a bot. |
| `ZULIP_AUTOREPLY_EXCLUDE`, `ZULIP_AUTOREPLY_TEXT`, `ZULIP_AUTOREPLY_WINDOW_S` | unset, a neutral line, `86400` | The auto-reply's exclusions, text (`{bot}` is replaced) and once-per window. |
| `ZULIP_REGISTRY_PATH`, `ZULIP_MCP_SCRIPT`, `ZULIP_UV_BIN`, `ZULIP_STATE_DIR` | unset, unset, `uv` on `PATH`, next to `COMPLETED_PATH` | The bot registry, the listener script, its runner and the state directory. |
| `ZULIP_WAKE_POLL`, `ZULIP_AUTOREPLY_POLL`, `ZULIP_RECONCILE_S`, `ZULIP_MAX_AGE_S`, `ZULIP_HISTORY_TIMEOUT_S`, `ZULIP_REGISTRY_TTL_S`, `ZULIP_BOT_CACHE_TTL_S` | `2`, `10`, `60`, `300`, `3`, `300`, `3600` | Timings, in seconds. |
| `DM_HISTORY_MAX_CHARS` | `4000` | Earlier messages added to a chat worker's first turn. |

## Repo inference (experimental)

Only read with `REPO_INFER_ENABLED=1` ([repo-push-cred.md](repo-push-cred.md#repo-inference-experimental)). Built
for the original deployment and still experimental.

| Variable | Default | Meaning |
|---|---|---|
| `REPO_INFER_ENABLED` | off | The code worker may pick a repo when the ticket has no `repo:` line. |
| `REPO_INFER_SHADOW` | off | Log the pick it would make, change nothing. |
| `REPO_INFER_COMMENT_RERUN_ENABLED` | off | A human comment on a parked ticket runs the pick again. |
| `REPO_INFER_RERUN_USERS` | unset | Users besides the ticket's creator whose comment may do that. |
| `REPO_CATALOG_NS` | unset | GitLab namespace listed as the catalog. Unset: no catalog. |
| `REPO_TIER_B`, `REPO_DENY` | required | Globs: repos reachable only by explicit name, and repos never reachable. Empty is allowed, unset isn't. |
| `REPO_LOCAL_ROOTS`, `REPO_LOCAL_DIRS`, `REPO_LOCAL_MAP` | unset | Where existing checkouts are found (`REPO_LOCAL_MAP` is `ns/name=/path,...`). |
| `REPO_CLONE_ROOT` | `~/.cache/eph-repo-cache` | Where repos without a checkout are cloned. |
| `REPO_CATALOG_CACHE`, `REPO_CATALOG_TTL_S`, `REPO_CATALOG_HTTP_TIMEOUT` | `~/.cache/eph-broker/repo-catalog.json`, `3600`, `15` | Catalog cache and GitLab timeout. |
| `REPO_ALIASES_FILE` | `config/repo-aliases.json` | Extra names for repos. |
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
