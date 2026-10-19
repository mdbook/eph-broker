# Installing eph-broker

## Requirements

| Need | Why |
|---|---|
| Linux, Python 3.9 or newer | The broker is standard library only. The only non-stdlib imports are the optional `tools/laya_pick_daemon.py` (a local model that helps repo inference pick a repo; experimental, see [repo-push-cred.md](repo-push-cred.md#repo-inference-experimental)) and a PyYAML test that skips itself when PyYAML is missing. |
| `git` | Worktrees, commits and pushes. |
| `tmux` | A `Recommends` of the RPM; only the interactive-agent row and its duplicate check use it. |
| The `claude` CLI | The default harness for every built-in row. On the broker's `PATH`, already logged in for the user the broker runs as. Workers use that stored login (`~/.claude`); `ANTHROPIC_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` in the broker's environment are deliberately not passed to them. Tested with Claude Code 2.1. The broker passes `-p`, `--system-prompt`, `--model`, `--max-turns`, `--output-format json`, `--setting-sources`, `--permission-mode` (`acceptEdits`, `auto`), `--allowedTools`, `--disallowedTools`, `--mcp-config` with `--strict-mcp-config`, `--settings`, `--resume`, `--session-id`, and in some setups `--effort`, `--add-dir`, `--plugin-dir` or `--dangerously-skip-permissions`, so an older CLI that lacks one of these won't work. |
| GitLab and a token that can open MRs | `gitlab_mr.py`. |
| A tracker script | Only for tracker tickets. The broker runs `$BROKER_MCP_SH vikunja-bridge tools/call ...` to read tickets and post comments and labels. No script ships, and without one no tracker ticket can start a worker. The contract is in [tracker-script.md](tracker-script.md). |
| `hermes` (optional) | Only if you switch a row's harness to hermes, see [hermes.md](hermes.md). Other harnesses: [adapters.md](adapters.md). |

## From source

```sh
cp config/provenance.example.json config/provenance.json   # edit it (see configuration.md)
cp config/site.example.env config/site.env                 # edit it
EVENTS_LOG=$PWD/events.jsonl python3 app.py                # serves 127.0.0.1:8787
```

Secrets never go in `site.env`. Keep them in a file only you can read and load it into the broker's
environment when you start it:

```sh
set -a; . /etc/eph-broker/eph-broker.env; set +a   # GITLAB_MR_TOKEN=..., GITLAB_WEBHOOK_TOKEN=..., ...
EVENTS_LOG=$PWD/events.jsonl python3 app.py
```

The user you run it as needs the same things as the RPM's service user below: a Claude Code login, a git
identity for worker commits, a push credential for each repo ([repo-push-cred.md](repo-push-cred.md)), and
write access to each target checkout's `.git` (every worker is a `git worktree` of it).

`python3 app.py --selftest` runs the batching self-test and exits.

`EVENTS_LOG` defaults to `$TMPDIR/eph-broker/events.jsonl` and `WORKTREES_ROOT` to `$TMPDIR/eph-broker-worktrees`.
Set both somewhere persistent for real use. Without `config/provenance.json` the broker refuses to start unless
`BROKER_PROVENANCE_NEUTRAL=1`. Without `BROKER_PUSH_HOSTS` it starts but refuses every branch push.

## RPM and systemd

`packaging/eph-broker.spec` builds a `noarch` package that installs to `/opt/eph-broker`, plus
`eph-broker.service`, which runs `/usr/bin/python3 /opt/eph-broker/app.py` with `StateDirectory=eph-broker` and
`EVENTS_LOG=/var/lib/eph-broker/events.jsonl`. The spec needs a source tarball named
`eph-broker-<version>.tar.gz` whose top directory is `eph-broker-<version>/`, and the `pkg_version`,
`pkg_release` and `commit` macros:

```sh
V=$(cat VERSION)
git archive --prefix=eph-broker-$V/ -o ~/rpmbuild/SOURCES/eph-broker-$V.tar.gz HEAD
rpmbuild -bb --define "pkg_version $V" --define "pkg_release 1" --define "commit $(git rev-parse --short HEAD)" \
  packaging/eph-broker.spec
sudo dnf install ~/rpmbuild/RPMS/noarch/eph-broker-$V-1*.noarch.rpm
```

The package creates a system user `eph-broker` (home `/var/lib/eph-broker`, no login shell) and the unit runs
as it. Workers run as the same user, so everything they need lives in that home. Before the first start:

```sh
# who counts as a bot (the broker refuses to start without it), see configuration.md#who-counts-as-an-agent
sudo cp /opt/eph-broker/config/provenance.example.json /opt/eph-broker/config/provenance.json
sudo cp /opt/eph-broker/config/site.example.env /opt/eph-broker/config/site.env   # then edit it
sudo install -d -o eph-broker -g eph-broker -m 0750 /var/lib/eph-broker
# log the harness in as the service user (workers use this stored login, not an env var key)
sudo -u eph-broker -H claude
# the identity worker commits are made with
sudo -u eph-broker -H git config --global user.name "eph-broker"
sudo -u eph-broker -H git config --global user.email "eph-broker@example.com"
```

Files you add under `/opt/eph-broker/config/` aren't owned by the package, so an upgrade leaves them alone. You
can also keep them elsewhere and point `CONFIG_DIR`, `BROKER_SITE_ENV` or `BROKER_PROVENANCE_PATH` at them.

The package also creates an empty drop-in directory `eph-broker.service.d/` next to the unit. Put your settings
and secrets there (see [configuration.md](configuration.md#secrets)); a drop-in can also set `User=` to run the
broker as a different account. Then:

```sh
sudo systemctl enable --now eph-broker
curl -s 127.0.0.1:8787/health
```

The broker only listens on 127.0.0.1 (`BIND_HOST` in `app.py`). Put a reverse proxy in front of it for webhooks
and the dashboard.

## Wiring the webhooks

| Endpoint | Sender | Auth |
|---|---|---|
| `POST /event` | Your tracker, as a normalised event: `source`, `event_name`, `task_id`, `doer`, `labels`, `assignees`, ... | `X-Broker-HMAC: <hex sha256 HMAC of the raw body>` keyed by `BROKER_HMAC_SECRET` (a `sha256=` prefix is optional); enforced by default, so every call is refused until the secret is set and your tap signs |
| `POST /webhooks/gitlab` | A GitLab Issue Hook (`forge.py`); a Pipeline Hook too if `CD_ENABLED` is on (`cd.py`) | `X-Gitlab-Token` must equal `GITLAB_WEBHOOK_TOKEN`; refused while unset |
| `POST /forge/gitlab` | Deprecated alias for `/webhooks/gitlab`, kept for one release ([operations.md](operations.md#upgrade-notes)) | Same as `/webhooks/gitlab` |
| `POST /rerun` | You, to re-run a ticket by hand | `X-Broker-Rerun-Token` must equal `BROKER_RERUN_TOKEN`; refused while unset |

To sign a test body by hand: `openssl dgst -sha256 -hmac "$SECRET" -r < body.json | cut -d' ' -f1`, then send
the result as `X-Broker-HMAC`.

The tap that turns your tracker's webhooks into `/event` calls runs outside the broker. [n8n.md](n8n.md) has
importable n8n taps for Vikunja and GitLab, and
[agent-refire.md](agent-refire.md) explains which events start, feed or skip a worker.
