# CD: auto-merge and deploy hand-off

Default **off**. Set `CD_ENABLED=1` in the broker env to enable.

1. **Auto-merge.** After the mr-reviewer posts **PASS** on a worker MR (source branch under `eph/`) in a repo on
   `CD_REPO_ALLOWLIST`, the broker re-reads the MR from GitLab and refuses (DM to the escalation target plus a ticket comment) unless
   its head is the exact commit the reviewer saw, GitLab's diff is computed at that commit, it is `opened`,
   not a draft, same-project (no fork), targets the default branch, the source branch is the reviewed
   worker branch, and the author is the broker bot. It then computes
   `tier = max(reviewer-stated tier, path classifier)`. An empty diff, a diff over 2000 files, or any
   lookup failure counts as unclassifiable: no merge.
   - **Full**: no merge. The broker DMs the escalation target and comments on the ticket. A change is Full when it touches any
     of these:
     - Dockerfile, CI, compose, deploy/systemd/packaging;
     - dependency manifests/lockfiles, migrations;
     - auth/gate/hook/allowlist or env/secret-shaped paths;
     - `config/*`, including `cd-tier.json` and `deploy-paths.json` themselves;
     - the repo's **shared engine** code. Engine code is Full by default. For eph-broker every
       path is Full **unless** it matches `docs/*`, `tests/*` or `README*` (a new module is Full until listed).
   - **Skip/Light** with a green MR pipeline: the broker merges through the GitLab API with `sha=<reviewed
     commit>`, so GitLab rejects the merge if anything was pushed after the review. If the pipeline is still
     running, it sets `merge_when_pipeline_succeeds`. A pipeline for a different commit counts as not green.
   - **Gate rule:** besides `head_pipeline`, the broker reads every MR pipeline (all pages; over 2000 fails
     closed) at the reviewed commit and keeps only the **newest (highest id) per `source`** (push,
     merge_request_event, ...). Each kept pipeline must be green (or be the pending head, for
     `merge_when_pipeline_succeeds`). So a failed pipeline later retried green at the same commit no longer
     blocks, but a failed newest pipeline of any source still refuses. A pipeline with no `source` or no
     integer id is never grouped with others. It always counts on its own, so it fails closed.
   - A failed or missing pipeline, or any API error: the broker DMs the escalation target and comments on the ticket. It never
     fails silently.
2. **Hand-off.** A successful pipeline on the default branch for a repo in `config/deploy-paths.json`. The broker
   re-reads the pipeline (status, sha, ref, tag) and the project's default branch from the GitLab API and
   sends nothing (it DMs the escalation target instead) if they don't back the hook payload. The pipeline's sha must also still
   be the **current head** of the default branch: a stale replay (e.g. a manual redelivery after a restart,
   or an older pipeline finishing after a newer merge) is refused so it can't roll prod back.

   With `CD_ENABLED=1`, every green default-branch pipeline of a listed repo is handed off, including
   hand-pushed commits and Full-tier MRs a human merged. Merging to the default branch is the deploy sign-off.
   What the hand-off does depends on the repo's `kind` (see the table below):
   - `ansible-*` kinds: a message to `CD_ANSIBLE_HANDLE`, through the `NOTIFY_BACKEND` channel, with exactly
     `DEPLOY-HANDOFF repo=<ns/name> sha=<sha> ver=<ver> ticket=<id|none>`, plus a ticket comment. Whatever
     reads that handle (a deploy bot, a person) runs the deploy. The name comes from the original deployment,
     where an Ansible play did; nothing in the broker runs Ansible.
     - **`ticket=none`**: if the commit can't be traced to a Vikunja ticket (e.g. a human merge with no
       `vikunja_task_<id>` branch or `key=vikunja:task:<id>` / ticket link), the DM still goes out with the
       literal `ticket=none`. No ticket comment is posted in that case. The consumer must accept `none`.
   - Other kinds: a ticket comment with the expected live time.
   - The broker never closes the ticket. Whoever runs the deploy closes it once the change is live.
   - Retries: the broker records a `repo+sha` as handed off only **after** both the message and the comment
     succeed. A redelivery of the pipeline event for a failed hand-off retries it; if the hand-off message already
     went out and only the comment failed, the retry re-posts just the comment. A hand-off that already
     succeeded is not repeated. The broker answers the hook 200 before processing, so GitLab never
     auto-redelivers: a retry is a manual "Resend request" in the project's webhook log. These records are
     in memory and reset on a broker restart.

## `config/deploy-paths.json`

The file maps each repo (`ns/name`) to an entry. The broker hot-reloads it on SIGHUP.

| key | req | meaning |
|---|---|---|
| `kind` | yes | `ansible-rpm-pin` (any `ansible-*` sends the hand-off line) \| `registry-image+compose` \| `nightly` \| `none` |
| `repo` | no | same as the key (informational) |
| `play` | no | name of the deploy job the reader runs (e.g. `my-app`) |
| `pin_var` | no | the variable that job pins the version with (e.g. `my_app_version`) |
| `target` | no | host the job deploys to (e.g. `app-host`) |
| `verify` | no | how the deploy is live-verified (free text or `{probe, url}`) |
| `ver_template` | no | `ver=` value in the hand-off line. Placeholders: `{VERSION}` (the VERSION file at the sha), `{pipeline_iid}`, `{sha}`, `{short_sha}`. If unset or unresolvable, the sha is used |
| `expected_live` | no | text for the "expected live" comment on non-ansible kinds |

The broker itself only reads `kind`, `ver_template` and `expected_live`. The other keys are notes for whoever
reads the hand-off.

## Auto-merge without a reviewer (`AUTO_MERGE_REPOS`)

This is a separate path from CD, and it doesn't need `CD_ENABLED`. Default **off** (unset or empty).

`AUTO_MERGE_REPOS` is a list of repos (`ns/name`, comma/colon/space separated, case-insensitive). When a worker
opens an MR in one of them, the broker merges it once the MR's pipeline is green. **No reviewer verdict and no
tier check are involved**, so the pipeline is the only gate. Use it only for repos where any worker change is
acceptable once CI passes, such as a docs wiki.

It still refuses (a ticket comment plus a message to the escalation target) unless:

- GitLab's view of the MR is exactly the branch the broker just pushed: opened, not a draft, same project,
  targeting the default branch, authored by `CD_MR_AUTHOR`, with its head at the pushed commit;
- `CD_MR_AUTHOR` and a merge token (see `GITLAB_MERGE_TOKEN_ENV` below) are set;
- every pipeline at that head is green. The merge is pinned to that SHA.

The pipeline is polled `AUTO_MERGE_POLLS` times (default 12), `AUTO_MERGE_POLL_S` seconds apart (default 10),
because GitLab creates it a few seconds after the MR. Still running after that: the broker sets merge when
pipeline succeeds. No pipeline at all: no merge. To go back to manual merging, take the repo off the list (it's
read on every MR, no restart needed).

## Environment and config knobs

- `CD_ENABLED`: `1`/`true` turns CD on. Unset means off, with the same behavior as before CD existed.
- `GITLAB_MERGE_TOKEN_ENV`: the name of the env var that holds the merge-capable token (default
  `GITLAB_MERGE_TOKEN`). If that var is unset, the broker falls back to `GITLAB_MR_TOKEN`.
- `CD_REPO_ALLOWLIST`: the repos (`ns/name`, comma/colon/space separated, case-insensitive) that CD may
  auto-merge. Unset means none.
- `CD_MR_AUTHOR` (**required** with `CD_ENABLED=1`): the GitLab username that opens worker MRs; only MRs
  authored by it can auto-merge. There is no fallback to the token owner (`GET /user`): that account may also
  open `eph/` MRs by hand, and those must not count as the bot. Unset means nothing auto-merges, and the escalation target is
  DMed once per broker process.
- `CD_ACCEPT_HERMES` (default **off**): `1`/`true` lets a reviewer PASS from the `hermes` harness auto-merge
  the same way a `cc` PASS does. Off means only `cc` verdicts auto-merge. A missing or unknown harness
  never auto-merges and returns `non-cc-harness`. The flag does not change the tier: CD still decides the
  tier from the changed paths, and the reviewer can only raise it, so a Full-path diff stays `full` whatever
  a hermes summary says. When a hermes PASS merges or arms merge-when-green, the ticket note and the escalation DM
  are marked `(hermes verdict)`.
- `CD_ANSIBLE_HANDLE` (site overlay; unset = hand-off DM fails and the escalation target is told) and `CD_WORKER_BRANCH_PREFIX` (default `eph/`).
- `config/cd-tier.json` adds tier patterns. It is hot-reloaded and can only **add** to the built-in lists in
  `cd.py`; it can never remove a Full glob or make a path Light.
  - `{"repo_patterns": {"<ns/name>": {"<category>": [globs]}}}` adds per-repo Full categories.
  - `{"extra_patterns": {"<category>": ["glob", ...]}}` and `{"full_patterns": {...}}` add global Full globs.
  - `{"default_full_repos": {"<ns/name>": ["light glob", ...]}}` makes another repo default-Full with that
    Light list. It cannot change the Light list of a repo built into `cd.DEFAULT_FULL_REPOS`.

  Globs are case-insensitive and match both the full path and the basename.
- The GitLab project webhook must send **Pipeline events** to `/webhooks/gitlab` with the same `X-Gitlab-Token`.
