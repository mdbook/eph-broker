# Native GitHub Issues

The first GitHub adapter increment accepts signed `issues.opened` webhooks at `POST /webhooks/github`
and posts the worker's summary back as an issue comment. No n8n workflow or tracker script is needed.
GitHub.com only; GitHub Enterprise is not supported yet.

**This is issue intake, not GitHub PR support.** Pull requests, reviews, comments, label changes, edits and
reopens are acknowledged and ignored. They cannot start or forward work, even if
`ISSUE_TRIGGERING_EVENTS` is widened. There are no comment reruns, GitHub PR reviews, automatic labels,
assignments or issue closes in this increment. The adapter inherits fail-closed defaults for those writes.
The shared tracker registry/config endpoint in the design is still future work; this adapter uses the
existing forge issue route and settings, not `config/trackers.json` or a new `routes.json` row.

## Connect an already configured broker

Start with the [first-MR setup](first-merge-request.md): the agent, approved prompts, GitLab publishing
checkout, bot/push credentials and push-host allowlist must already work. The broker still opens **GitLab
MRs**, not GitHub PRs. Map the GitHub issue repository to that local checkout (typically a GitLab mirror of
the same project). A GitHub-only checkout cannot publish through this flow.

Add these non-secret settings through your broker settings/environment, then restart:

```sh
# GitHub repository names in the map are lowercase; retain any existing GitLab entries.
ISSUE_REPO_MAP=github:owner/repo=/srv/git/mirror
# Trusted people who may request a paid WRITE worker by opening an issue.
ISSUE_AUTHOR_ALLOWLIST=your-github-login
# Optional, same as for GitLab issue work:
CODE_REPO_RUNNER_SEEDS=/srv/git/mirror=pytest
# Optional extra echo guard if the posting account is a normal user rather than a GitHub Bot:
GITHUB_BOT_LOGIN=your-broker-login
```

`ISSUE_AUTHOR_ALLOWLIST` is currently **shared with GitLab**, compared case-insensitively. When both
trackers are enabled, listing a login trusts that spelling on both services: only enable both if those
identities are trusted on both. An empty list starts nothing. `ISSUE_REPO_MAP` is also required; an unmapped
repo cannot cut a worktree. Issue text never chooses a local path or an API host.

Provision these secrets in the broker's private environment, **not in git or `site.env`**:

- `GITHUB_WEBHOOK_SECRET`: a random webhook signing secret. Unset means every delivery is rejected.
- `GITHUB_TOKEN`: a repo-scoped fine-grained PAT for the commenting bot, with **Issues: write** permission
  on the selected repo (and its implicit metadata read access). Unset means comments are skipped.

Optional `GITHUB_WEBHOOK_SECRET_ENV` and `GITHUB_TOKEN_ENV` name different environment variables holding
those secrets; they never contain secret values themselves. Worker environments omit both secrets.
Keep the same dedicated-user/isolation precautions as the [security model](security.md).

In GitHub's repository Settings → Webhooks, add your reverse-proxied broker URL ending in
`/webhooks/github`, content type **application/json**, that signing secret, and select **Issues** events.
Keep the proxy authenticated/IP-restricted as appropriate; the broker remains loopback-only. Do not
replace `X-Hub-Signature-256`: the adapter verifies HMAC-SHA256 over the exact request bytes in constant
time. No unsigned or SHA1 fallback exists. GitHub Bot senders and the configured bot login are ignored.

Allow roughly 5–10 minutes for these settings and the webhook **after** the base broker/mirror is ready;
this is an estimate, not a timed end-to-end first-MR install.

## Check it

1. Use GitHub's webhook delivery page. A ping gets 200 with empty `subject_keys`; it starts nothing.
2. Open a new issue as an allowed user. A valid delivery returns 200 and a subject key such as
   `github:owner/repo#42`. Empty `subject_keys` means unsupported, malformed, bot-authored or unauthorized;
   a null key means a downstream ingest gate dropped the event. A 401 means missing/bad signing secret or
   signature; a 400 means signed invalid JSON; the existing body-size limit returns 413.
3. Check the dashboard/log and that issue for the summary. Posting uses only
   `https://api.github.com/repos/<owner>/<repo>/issues/<number>/comments`, a 15-second timeout and no
   redirects. Payload URLs are never followed. API errors are logged without response bodies or secrets;
   a lost comment never wedges the worker. Mentions and numeric cross-references are defused, output is
   capped, and the `broker-run` marker is preserved. Comment webhooks are ignored, so writes cannot echo
   into a new run. Missing repo mappings are reported on the issue without exposing local paths.

Tests use synthetic webhooks and mocked HTTP, with signature, malformed-input, author/repo gate, echo,
credential-scrub and summary-write-back checks. No real GitHub calls are made.

## Adapter inventory on main before this increment

SHAs below identify adapter-relevant changes already present on main, not claims of complete support:

| Adapter/seam | Main SHA | Existing surface |
|---|---|---|
| Tracker interface + Vikunja wrapper | `2a49df57bb800e4e5b54d5540e4d90b5de03939c` | Ref-based read/comment/parked label/assignment over the bridge; intake still external tap |
| GitLab | `8756274580c1bc7e442a2a605db6509faa323f6d` | Native issue webhook + summary notes; MR intake/review remains the existing GitLab path |
| Shipped Vikunja API bridge | `94b552164bb0e6286df951dde4b6c8299b87945e` | Executable bridge for task reads/writes; not a native webhook receiver |
| GitHub | none | This MR adds opened-issue intake and comments; PR intake/review is deferred |
| Jira/Atlassian (including Confluence) | none | Not implemented; needs its own payload/identity/echo-carrier contract |

Next small MRs can add the configured registry and safe GitHub PR review routing, then Jira and direct
Vikunja intake. A PR must never fall into the existing issue **write** route; fork/draft/head-SHA review
safety and plain `COMMENT` review write-back need their own tests before PR intake is enabled.
