# Security model

The design rests on one split. The broker holds every credential and does every write that leaves the host.
Workers hold nothing and can only change files in their own worktree. Most of the controls below exist to keep
that split intact, and the last section is about where it leaks.

## What comes in

The broker binds to 127.0.0.1 only. Anything that reaches it from outside goes through your reverse proxy.

| Endpoint | Check |
|---|---|
| `POST /event` | HMAC-SHA256 of the raw body in `X-Broker-HMAC`, keyed by `BROKER_HMAC_SECRET` (format below). Enforced by default: an unsigned or badly signed call gets 401, and so does every call while the secret is unset. The body says who the author is, so an unsigned `/event` would let anyone who reaches it claim any author. `BROKER_HMAC_MODE=log` accepts everything and only records the result; use it briefly while you add signing to an existing tap. |
| `POST /webhooks/gitlab` (and its deprecated alias `/forge/gitlab`) | `X-Gitlab-Token` must equal `GITLAB_WEBHOOK_TOKEN`. Refused while that's unset. The issue's author must also be in `ISSUE_AUTHOR_ALLOWLIST` (case-insensitive), which is empty by default, because anyone who can open an issue on the project would otherwise be handing the worker its task. `GET /health` shows the effective list as `issue_author_allowlist` (usernames only), so a bot that mirrors untrusted issues can prove it is not on it. |
| `POST /rerun` | `X-Broker-Rerun-Token` must equal `BROKER_RERUN_TOKEN`. Refused while that's unset. An optional `model` runs that one run on any native model or spawn-registry backend, whoever created the ticket. |
| `GET /dash.html`, `/dash`, `/state`, `/health`, `/runs/<key>`, `/stream-status/<key>`, `/actions` | No auth and no write actions. Put an authenticating proxy in front. |
| `POST /actions/<verb>` | Off unless `BROKER_ACTIONS_ENABLED`. Then all of: the proxy secret header, the user and group headers naming `BROKER_ACTIONS_GROUP`, `X-Broker-Action: 1` with a JSON body, and no cross-site `Sec-Fetch-Site`. `GET /actions/settings` makes the same proxy, duplicate-header and group check. See [Dashboard actions](configuration.md#dashboard-actions). |
| `GET /stream/*`, `/history/*` | Off unless `BROKER_STREAM_ENABLED`. Then the user and group headers your proxy sets must name `BROKER_STREAM_GROUP`. |

Treat everyone in `BROKER_ACTIONS_GROUP` and anyone holding `BROKER_RERUN_TOKEN` as an operator. Both can
route a ticket, including one a guest created, to a non-Anthropic backend: a rerun's `model` is applied as
trusted, and **Worker settings** can put a whole row on any registry backend. `MODEL_LABEL_TRUSTED_CREATORS`
does not limit them. The spawn-time floors (`CODE_NONCLAUDE_OK` and the classifier-bypass refusal) still apply.

The `/event` header is `X-Broker-HMAC: <hex sha256 HMAC of the raw body>`, hex (either case), with an optional
`sha256=` prefix (the form the outbound signatures use). To sign a body from a shell:

```sh
openssl dgst -sha256 -hmac "$SECRET" -r < body.json | cut -d' ' -f1
```

The signature covers the body only, with no timestamp, so anyone who captures a signed call can replay it. Keep
the hop to your reverse proxy on TLS.

All three POST paths refuse a bad `Content-Length` with 400 and a body over `BROKER_MAX_BODY` bytes (default
1 MiB) with 413, before any auth check. A client that goes quiet for 30 seconds is disconnected. That timeout is per read, not per request, and the
broker starts a thread per connection with no cap, so a client that trickles bytes can still hold threads. Keep
the broker on 127.0.0.1 behind a reverse proxy that buffers request bodies and limits slow clients.

The author check looks at whoever triggered the event, which is the issue's author only for a newly opened
issue. If you add a reopen event to `ISSUE_TRIGGERING_EVENTS`, a listed user reopening someone else's issue
hands that issue's text to a worker. A GitHub tap that sends issues to `/event` must put the webhook's
`sender.login` in `doer`, and sign the event. No GitHub tap ships; [configuration.md](configuration.md#github-issues)
has the event shape it has to send.

Ticket text and comments are written by people, so they're handled as untrusted input. Comments forwarded to
a running worker are wrapped and labelled as data. Authors listed in `untrusted_authors` in
`config/provenance.json` are never trusted for write grants, even if they're also handoff doers. Only creators in
`MODEL_LABEL_TRUSTED_CREATORS` can use a `model:` label to move a ticket to a non-Anthropic backend, or a
`hermes` label to move it to the hermes harness.

MR note iteration is separately opt-in through `MR_NOTE_AUTHOR_ALLOWLIST` (empty means off), behind the same
GitLab webhook token check. Only new, non-system notes on open, same-project broker MRs under
`CD_WORKER_BRANCH_PREFIX` and `eph/` qualify. The broker's own `broker-run:` notes are ignored even if its
username is allowlisted. Each distinct accepted note consumes one of `MR_ITERATION_MAX` slots (default 3);
delivery retries do not. The broker journal persists these receipts and the cap across restarts. Without
durable state they last only for the process lifetime.

Note text is fenced with the existing untrusted-comment delimiter, with delimiter look-alikes neutralized.
It cannot choose a repo, branch, runner or capability. The operator-owned GitLab project map chooses the
local repo; authenticated MR metadata chooses the branch and pins the cut SHA. The worker stays cred-free.
`publish_iteration` refuses main/non-broker branches, fetches the existing feature branch, checks ancestry
and leases the push to that exact fetched tip. Deletion or any ref movement between fetch and push is
refused, not recreated, rebased or overwritten. The ancestry check still requires a fast-forward. The existing
MR receives a marked summary and a fresh review of the new SHA, not a new MR or an automatic merge.

## What a worker gets

Every worker gets these, whichever harness runs it ([adapters.md](adapters.md)).

- A fresh git worktree, with `CLAUDE.md` and `AGENTS.md` removed so repo instructions can't steer it.
- The signed prompt, checked against `approved-prompts.json` before every start. See [prompts.md](prompts.md).
- An environment with the broker's secrets removed (`scrub_worker_env` in `adapters/cc.py`).
- A tool grant built from its row's `capability`. The allow and deny lists live in `adapters/cc.py`, and both
  harnesses enforce the same lists. A deny entry always wins.

### OS boundary (on by default)

`sandbox.py` wraps Claude Code, hermes and Codex launches in bubblewrap. `WORKER_SANDBOX` is `bwrap` by default;
`off` is the only opt-out. An unknown value or missing `bwrap` fails startup closed, and every launch checks
again. Install the `bubblewrap` package (an RPM requirement). Namespace/mount failures fail the worker; there
is no unsandboxed retry. Hosts must permit bubblewrap user/PID namespaces.

The wrapper read-only binds `/`, read-write binds the worktree and its base repo's common `.git` directory,
uses a new PID namespace with a fresh `/proc`, a private `/dev` and `/tmp`, and dies with its parent. Only
broker-created per-worker scratch and the harness's state are additionally writable: Claude Code's existing
`~/.claude` and `~/.claude.json` (login/session state is deliberately kept), or the private hermes/Codex home.
When mid-turn steering is enabled, Claude Code and Codex also get a writable bind of **only their subject's
delivery directory** (inbox and delivery log), created before launch. Neither the steer root, other subjects'
inboxes nor the events/state directory is made writable. This works with the default private `/tmp` too.
The common git directory is snapshotted before the first turn, never re-resolved from a worker-edited pointer.

`~/.git-credentials`, `~/.config/eph-broker`, `/etc/eph-broker` (the conventional secret env-file directory),
and the directories containing `BROKER_SITE_ENV` and
`BROKER_CONFIG` (including their default locations) are hidden. Directories get empty tmpfs mounts; files get
read-only `/dev/null` mounts, since tmpfs cannot mount on a regular file. Symlink targets are masked too,
including env/config file symlinks whose targets lie outside their hidden parent directories.
Missing paths are skipped, with no host placeholder files created.

Set `WORKER_SANDBOX_EXTRA_RO` for explicit read-only binds and `WORKER_SANDBOX_HIDE` for additional secret
paths (colon-separated absolute paths, `~` accepted). Hides are applied last and always win. Extra read-only
binds cannot restore the host's `/proc` or `/dev`. The root path and relative paths are refused. If your
systemd `EnvironmentFile=` is outside these directories, you **must** list its directory in `WORKER_SANDBOX_HIDE`;
the broker cannot infer systemd's source from inherited environment variables. Also hide sibling bot env files,
alternate credential stores and any other sensitive paths: this is a denylist of reads, not an allowlist.

Keep secret directories separate from code, runtime homes and required MCP configs: hiding a parent hides
those children too. Knobs are trusted operator configuration, never ticket input; restart after changes.
The private site systemd overlay ships `WORKER_SANDBOX=off` until a canary passes; this change does not enable
the live fleet. Validate a real turn (auth, resume, scratch, hooks and hard-kill) before removing that override.

The deny lists (`_READ_DENIED`, `_WRITE_DENIED`, `_CODE_DENIED`, `_GENERAL_DENIED`) cover reading secret files,
writing under `.git`, `git push` and other ref or remote changes, and destructive shell commands. The code
worker's only shell access is its test runner, chosen from a fixed table by key, never a command from the
ticket. The `npm` runner is the weakest entry: `npm ci` downloads packages and runs their install scripts, so
use it only for repos whose dependencies you already trust.

### With the Claude Code harness

- The signed prompt is the entire system prompt (`--system-prompt`, which replaces Claude Code's default).
- No user or project Claude Code settings (`--setting-sources ''`). The general worker is the exception and
  loads user-level settings by default (`GENERAL_SETTING_SOURCES=user`).
- The allowlist and the deny list are passed as `--allowedTools` and `--disallowedTools`.
- The general worker has no allowlist. On native Claude it runs in Claude Code's `auto` permission mode, where a
  classifier judges each action, with the deny list on top. On a non-Anthropic backend that classifier doesn't
  work, so the worker runs with `--dangerously-skip-permissions` and only the deny list. Treat a general worker
  on another backend as having shell access, and see the residual below.
- The general worker loads only the MCP config named by `GENERAL_MCP_CONFIG` (`--strict-mcp-config`), and
  refuses to start without it.

### With the hermes harness

Details are in [hermes.md](hermes.md); the security-relevant parts are these.

- The signed prompt goes in as hermes' `agent.system_prompt`, but hermes adds its own identity and tool text
  before it and a runtime block after it. Unlike Claude Code, the model sees some text you didn't sign.
- `--ignore-rules` skips `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `SOUL.md` and hermes' memory, and each worker
  gets a private `HERMES_HOME` that is swept down to its session store and logs before every turn.
- hermes grants tools by toolset only, so every call goes through the broker's `eph-gate` plugin
  (`adapters/hermes_gate/`). It applies the same allow and deny lists from `adapters/cc.py`, plus its own hard
  denies: worktree confinement, git options that read files, the secret floor and `.git` writes. A pre-flight
  probe proves the gate is loaded before each turn, and a sentinel proves it ran; if either fails, the turn is
  refused or its result thrown away.
- The general worker goes through the gate's floors and then your exec gate ([hermes.md](hermes.md#plugging-in-your-own-exec-gate-general-worker-only)).
  No gate configured, a timeout or any unclear answer means deny. The Portcullis provider keeps one shared state
  directory, so more than one general worker on hermes at a time isn't supported with it. The general worker only reaches hermes through a `hermes`
  ticket label.
- Only `mr-review` (read-only), `write`, `code` (with `CODE_NONCLAUDE_OK=1`) and `general` run on hermes. Every
  other capability is refused, as is a native Claude backend.
- The hermes interpreter is checked before every turn for editable-install or relative `sys.path` entries that a
  file in the worktree could shadow.

## What goes out

Only the broker publishes. Before a push it resolves the remote URL and refuses unless it's `https://` on a
host in `BROKER_PUSH_HOSTS`, and it refuses if repo-local `insteadOf` or `pushInsteadOf` rules would rewrite
that URL again. Code and general workers are pushed to a fresh `eph/...` branch, and an assertion in
`worktree.publish_mr` stops any push to the MR's target branch. MRs are opened with `GITLAB_MR_TOKEN`; the
worker never sees it. GitLab runs quick actions such as `/merge` or `/approve` that start a line in a note or
description, as the token's user, so every body the broker sends to GitLab gets a zero-width space before a
line-leading `/` (`gitlab_mr._api`). Worker and reviewer text can't trigger one. An `@` that starts a mention
gets one too, so it can't ping anyone, and a worker summary posted on an issue or quoted in an MR has its
references broken, so it can't link or close other issues. The worker's commit messages are left as they are:
a `Fixes #7` in one still closes that issue when you merge the MR, so read them as part of the review.

Three paths go further, and all three are off unless you set them up:

- The optional CD module (`CD_ENABLED=1`) can merge a reviewed MR in an allowlisted repo after re-checking it
  against what the reviewer saw ([cd-deploy-paths.md](cd-deploy-paths.md)).
- `AUTO_MERGE_REPOS` merges a worker MR in a listed repo once its pipeline is green, **with no reviewer verdict
  and no tier check**. It's independent of `CD_ENABLED` and meant for content-only repos such as a docs wiki,
  where the pipeline is the check. Only list a repo where you'd accept any change a worker makes
  ([cd-deploy-paths.md](cd-deploy-paths.md#auto-merge-without-a-reviewer-auto_merge_repos)).
- The `write` capability, built for a docs wiki and not used by any shipped class, pushes the worker's commit
  straight to its row's `publish_ref`.

The broker's own git calls inside worker worktrees ignore global and system git config, disable hooks and
fsmonitor, skip external diff and textconv drivers, and stop if the worktree's `.git` pointer has changed
since it was created.

## Where it leaks

For evaluation, prefer the [non-root Docker preview](docker.md), which mounts no host credentials.
It uses host networking and is not a worker sandbox. A worker-enabled deployment should use a dedicated
account with only the repo and login state it needs. Putting the whole broker in a container limits host
filesystem exposure, but does not separate workers from credentials mounted into that same container.

Workers still run as the same OS user as the broker. With `WORKER_SANDBOX=off`, any shell access can read that
user's secret files and `/proc/<broker-pid>/environ`; harness tool grants alone are not an OS boundary.
With bubblewrap on, hidden files and the broker process are inaccessible, but other host-readable files remain
readable. Writable `.git` still permits planted repo-local config that later broker git calls may execute.
Egress is unrestricted: the worker can contact network services (including host services), and a read-only bind
does not stop connections to visible Unix sockets. Hide sensitive socket paths too. Model login credentials
intentionally accessible to the harness are not broker credentials and are not protected from that worker.

A separate worker uid/container with a filesystem and network allowlist remains stronger isolation.
[security-residual-same-uid.md](security-residual-same-uid.md) details the planted-config risk, opt-out exposure,
git hardening and HMAC rollout.
