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
| `BROKER_MCP_SH`, a script you write | Reading and commenting on tracker tickets ([tracker-script.md](tracker-script.md)). | For tracker tickets |
| `FLEET_MODEL_SH`, `SPAWN_MODELS_JSON` | Which model and backend a worker runs on ([below](#models-and-backends)). | No |
| `config/broker.yaml` | All the config above in one file, as an alternative ([below](#the-config-file)). | No |

## The config file

The config file is optional. Without it the broker reads `config/site.env` and the json files in `config/`, which
is all the quick start needs. `config/broker.yaml` (start from `config/broker.example.yaml`, which starts the
broker as is), or any file named by `BROKER_CONFIG`, gathers them into one file. A YAML file needs PyYAML
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
| `CODE_REPO_RUNNER_SEEDS` | Test runner per repo, `<path>=<runner>,...` (`pytest`, `npm`, `go`, `venv`, `bash-n`, `ansible-syntax`, `none`). | Detected from the repo |
| `ISSUE_REPO_MAP` | Which local checkout each GitLab or GitHub project's issues work in, `gitlab:group/app=/srv/git/app,...` ([GitHub issues](#github-issues) need a tap). | Every issue is refused |
| `ISSUE_AUTHOR_ALLOWLIST` | Comma list of usernames whose new issues may start a worker, compared case-insensitively. Anyone else's issue is ignored. | Nobody, so issue intake is off |
| `MR_REVIEW_REPO` | Default repo for the reviewer (the repo in the event wins). | |
| `MR_REVIEWER_MODEL`, `MR_REVIEWER_BACKEND`, `MR_REVIEW_ADAPTER` | Which model reviews MRs, see [below](#choosing-the-reviewers-model). | |
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
| `PARKED_LABEL_ID` | Tracker label id added when a worker returns `blocked`. | No label |
| `FLEET_ASSIGNEE`, `GENERAL_ROW_LABEL` | Assignee and label that start the general worker, see [Built-in rows](#built-in-rows). | `eph`, `eph:general` |
| `DEV_ASSIGNEE`, `CODE_ROW_LABEL` | Assignee and label that start the code worker. | `eph-code`, `eph:code-change` |
| `GENERAL_SETTING_SOURCES` | Which Claude Code settings the general worker loads (Claude Code harness only). | `user` |
| `GENERAL_MCP_CONFIG` | The only MCP config file the general worker may load (`--strict-mcp-config`, Claude Code harness only), see [The general worker](#the-general-worker). | `~/.config/eph-broker/general-mcp.json`; the worker refuses to start if it's missing |
| `AUTO_MERGE_REPOS` | Repos (`ns/name` list) whose worker MRs merge once the pipeline is green, **with no reviewer**. See [cd-deploy-paths.md](cd-deploy-paths.md#auto-merge-without-a-reviewer-auto_merge_repos). | Off |
| `CODE_NONCLAUDE_OK` | `1` lets a code worker run on a non-Anthropic backend. Any code worker on the hermes harness needs it too. | Such a code worker is refused |

Process settings read by `app.py`: `PORT` (8787), `CONCURRENCY` (5), `CONFIG_DIR`, `PROMPTS_DIR`,
`APPROVED_PROMPTS`, `WORKTREES_ROOT`, `EVENTS_LOG`, `BROKER_MAX_BODY` (largest POST body in bytes, 1048576).
`routing.py` reads `ROUTES_PATH` (default
`CONFIG_DIR/routes.json`), the file with your own agent types ([routes.md](routes.md)). Everything else is in
[settings.md](settings.md).

## Limits and batching

| File | What's in it | Defaults |
|---|---|---|
| `config/constraints.json` | Turn caps, wall-clock limits, forwards, session rotation | 50 turns and 1800 s (code: 100 turns and 3600 s), 10 forwards, rotate after 20 turns or 120k tokens |
| `config/windows.json` | How long to collect a burst before starting a worker, and how long a worker stays warm | 3 s and 45 s for tracker tickets |

`windows.json` is re-read on `SIGHUP`. `constraints.json` is only read at startup, so restart after changing it
(see [operations.md](operations.md#reloading-config)).

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

## GitHub issues

There's no GitHub endpoint and no GitHub tap ships. A GitHub issue can start the code worker only through a tap
you write that sends a signed `POST /event` like this one:

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
  checkout needs a GitLab https origin, and nothing is posted to the GitHub issue.

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

`MR_REVIEW_ADAPTER` picks the reviewer's harness, `cc` (the default) or `hermes`. The hermes harness needs a
non-Anthropic backend for the reviewer.

By default the reviewer asks `FLEET_MODEL_SH` for agent `mr-reviewer`, so a non-Claude answer runs it on that
backend. `MR_REVIEWER_BACKEND` pins a backend instead (`claude` means native Claude Code on the default model).

To review on native Claude Code with a model you pick, whatever `FLEET_MODEL_SH` says:

1. Set `MR_REVIEWER_MODEL` to a Claude model, e.g. `opus`.
2. Leave `MR_REVIEWER_BACKEND` unset, or set it to `claude`.
3. Set `MR_REVIEW_ADAPTER=cc`, because the hermes harness refuses native Claude.

A per-ticket `model:` label can't override this. To go back, unset `MR_REVIEWER_MODEL`. These are read at
startup, so restart the broker after changing them (`SIGHUP` isn't enough). This doesn't touch
`GENERAL_BACKEND`, which must stay unset (see [security-residual-same-uid.md](security-residual-same-uid.md)).

## Who counts as an agent

`config/provenance.json` (template `config/provenance.example.json`, loaded by `provenance.py`) decides which
ticket authors are agents rather than people, so the broker doesn't treat its own comments as human input. It
needs all five keys (empty lists are fine): `agent_basenames`, `handoff_doers`, `agent_pattern`,
`untrusted_authors`, `handoff_parked_labels`. An unknown key or a malformed file stops the broker.

## Live transcripts

Streaming a running worker's transcript to the dashboard is off by default. It only works with the Claude Code
harness: the stream reads the CLI's own session file under `~/.claude/projects/`, so hermes workers show no
transcript.

| Key | Meaning |
|---|---|
| `BROKER_STREAM_ENABLED` | `1` or `true` turns on `/stream/*` and gives each worker a session id. |
| `BROKER_STREAM_GROUP` | The group allowed to watch. Required when streaming is on; unset means every request gets a 403. |
| `BROKER_STREAM_USER_HEADER`, `BROKER_STREAM_GROUPS_HEADER` | The headers your auth proxy sets for the user name and group list. Required; your proxy must strip client-sent copies. |
| `BROKER_STREAM_GROUPS_SEP` | Separator for the group list (default `\|`). |

## The dashboard

`/dash` is read-only. The **Workers** tab shows running and past workers. The **Fleet** tab is an optional view
of long-running agents you operate yourself; it reads:

- `FLEET_STATUS_PATH`: one JSON status file for all agents, written by your own generator;
- the `FLEET_MODE:` line in the file at `FLEET_MD_PATH`;
- `SUPERVISOR_STATUS_PATH` if set, for health flags.

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
