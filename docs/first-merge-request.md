# A first merge request (manual setup)

The shortest path uses a GitLab issue as the ticket, so you don't need tracker glue yet. Allow 30 to 60
minutes for configuration once accounts and the CLI are ready, plus the agent's run time.
This is an estimate from reading the steps, not a timed install. You need four things first: a Linux host with
Python 3.9+ and git, a GitLab project, a GitLab bot user with a token (below), and a checkout of that project on
the host with an https `origin`.

```sh
git clone https://github.com/mdbook/eph-broker
cd eph-broker
git clone https://gitlab.example.com/group/app.git /srv/git/app    # the repo the worker will change
# Public source checkout: create your own settings from the examples.
cp config/provenance.example.json config/provenance.json
cp config/site.example.env config/site.env
```

**Make the GitLab bot token.** Create a bot user (or use a project access token) with the Developer role on the
project, and give it a token with scope `api`. That token is `GITLAB_MR_TOKEN` below. Make up a second random
string for `GITLAB_WEBHOOK_TOKEN`.

`provenance.json` lists your bot and service accounts, so the broker ignores the edits they make. Put your
GitLab bot user in it ([configuration.md](configuration.md#who-counts-as-an-agent)); a minimal file is:

```json
{"agent_basenames": ["eph-bot"], "handoff_doers": [], "agent_pattern": "(-bot\\b)",
 "untrusted_authors": [], "handoff_parked_labels": []}
```

All five keys are required (empty lists are fine); `config/provenance.example.json` explains each one.

These go in `config/site.env`, with your own values in place of the examples. This is a complete working set for
the GitLab path; every other setting has a default.

```sh
GITLAB_WEB_URL=https://gitlab.example.com
BROKER_PUSH_HOSTS=gitlab.example.com
ISSUE_REPO_MAP=gitlab:group/app=/srv/git/app
# who may start work by opening an issue; nobody can until you list them
ISSUE_AUTHOR_ALLOWLIST=your-gitlab-username
# optional: lets the worker run this repo's tests (pytest, npm, go, ...)
CODE_REPO_RUNNER_SEEDS=/srv/git/app=pytest
```

Two secrets go in the broker's environment rather than `site.env`. Put them in a file only you can read, for
example `~/eph-broker.env` (`chmod 600`):

```sh
GITLAB_MR_TOKEN=<the bot token above>
GITLAB_WEBHOOK_TOKEN=<the random string>
```

`GITLAB_MR_TOKEN` opens MRs and posts notes, and `GITLAB_WEBHOOK_TOKEN` is the value GitLab must send as
`X-Gitlab-Token`. A third, `BROKER_HMAC_SECRET`, only matters once a tracker tap sends `POST /event`: the broker
refuses unsigned events, so it has to be set and the tap has to sign.

Then set up the user the broker runs as:

- a push credential for the repo: one line for exactly that repo's path in `~/.git-credentials`, like
  `https://<user>:<token>@gitlab.example.com/group/app.git`
  ([docs/repo-push-cred.md](repo-push-cred.md) has the details);
- a git identity for the worker's commits (`git config --global user.name` and `user.email`);
- write access to the checkout's `.git`, because each worker gets a `git worktree` of it.

Read the three shipped prompts in `prompts/`. The public source already includes their hashes in
`approved-prompts.json`; keep those hashes if the prompts are unchanged, or sign your own ([docs/prompts.md](prompts.md)). Start the broker with the secrets in its environment
(`set -a; . ~/eph-broker.env; set +a; python3 app.py`), put a reverse proxy in front of
`127.0.0.1:8787`, and add a GitLab project webhook for issue events pointing at `/webhooks/gitlab` with the same
secret token. Open an issue. A few seconds later a worker starts; when it finishes you get an `eph/issue/...`
branch, an MR that closes the issue when you merge it, a review note on the MR, and the worker's summary as a
comment on the issue. If no MR shows up, the comment says why; `/runs/<key>`, the dashboard and the broker's
log have the rest ([docs/operations.md](operations.md#finding-the-result-of-a-gitlab-issue)).

## The same thing from a Vikunja ticket

Tickets from a tracker need two more pieces, both shipped. The broker reads and comments on tickets through
`tools/mcp.sh`, a ready-made [Vikunja](https://vikunja.io) bridge. Events come in through a tap: the importable
n8n flow in [docs/n8n-vikunja-tap.json](n8n-vikunja-tap.json) ([docs/n8n.md](n8n.md)).

1. In Vikunja, make an API token (Settings, API Tokens) with task read and write access, and a user for the bot.
2. Add to `config/site.env`: `BROKER_MCP_SH=/abs/path/to/eph-broker/tools/mcp.sh` (a wrapper around the bridge; point at `mcp.sh`, not at
   `vikunja_bridge.py`, because the worker deny floor matches the name `mcp.sh`).
3. Add to `~/eph-broker.env`: `VIKUNJA_URL=https://vikunja.example.com`, `VIKUNJA_TOKEN=<the API token>` and
   `BROKER_HMAC_SECRET=<a random string>` (the same one goes in the tap's signing step), then restart the broker.
4. Import and activate the linked n8n flow, configuring its broker URL and signing secret as described in
   [n8n.md](n8n.md).
5. Create a ticket whose description starts with `repo:/srv/git/app`, and assign it to the bot user (or give it
   the label `eph:code-change`). The default bot username is `eph-code`; for a different username set
   `DEV_ASSIGNEE` in `config/site.env`, or use the label instead. The repo also needs an entry in
   `CODE_REPO_ALLOWLIST` for tracker tickets; the GitLab issue map alone does not grant it.
6. The tap posts the event, the worker runs, and you get an MR plus a comment on the ticket with the summary.

Details, including the exact script contract if you want a different tracker, are in
[docs/tracker-script.md](tracker-script.md).

[docs/install.md](install.md) covers the RPM, systemd and the tracker path.

## Check each stage

- Before opening an issue, `GET /health` should return `ok: true`, and `issue_author_allowlist` should
  contain your username. An empty list means issue intake is off.
- In the GitLab webhook page, send an issue-event test. A 401 means the webhook tokens do not match;
  a 200 response with `ignored` is not proof that a worker started. Check the response's reason.
- Open a new issue as an allowed user in the mapped project. A prompt approval error needs a prompt
  review, not a bypass of the hash check. A push failure needs the repo-scoped credential and host allowlist.
- For Vikunja, verify the bridge can read a ticket, the imported flow is active, its HMAC secret matches
  the broker's, and events carry the expected bot assignee or label. There is no native Vikunja webhook
  receiver or single-command integration check yet.
