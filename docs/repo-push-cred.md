# Push credentials and repo inference

## Push credential for a repo

Workers never push. The broker pushes each worker's commit to a new branch, as the user it runs as, using
git's credential store. It needs one line per repo:

```text
https://<user>:<token>@gitlab.example.com/group/app.git
```

- The file is `~/.git-credentials` of the broker's user, or the path in `REPO_CRED_FILE`. Make it mode 0600
  and owned by that user.
- The token needs the `write_repository` scope, and a role on the project that may push new branches
  (Developer is enough unless your branch protection says otherwise). `<user>` is the token's user name.
- The broker always sets `credential.useHttpPath=true`, so the line has to name the repo's own path. A
  host-only line (`https://<user>:<token>@gitlab.example.com`) won't match. Write the path the same way the
  checkout's origin does, with or without `.git`.
- The broker only reads the store; a failed push never rewrites or erases a line.
- The checkout's origin must be `https://` on a host listed in `BROKER_PUSH_HOSTS`. The broker pushes to that
  URL and refuses an ssh origin, or a URL with a token embedded in it.

This is separate from `GITLAB_MR_TOKEN`, which the broker uses for the API (opening the MR and posting notes).
One token can do both if it has the `api` scope, but it still has to be written into the credential store.

## Repo inference (experimental)

Without inference, a tracker code ticket must name its repo with a `repo:/absolute/path` line. With
`REPO_INFER_ENABLED=1`, the code row can also pick the repo itself. The catalog is the list of repos in the
GitLab namespace `REPO_CATALOG_NS`, read with `GITLAB_MR_TOKEN`. A picked repo uses an existing checkout found
under `REPO_LOCAL_ROOTS` or `REPO_LOCAL_DIRS`, or is cloned into `REPO_CLONE_ROOT`. The pick walks a ladder of
steps:

1. an explicit `repo:` line, which may also name a catalog repo (`repo: group/app`);
2. a strong match between the ticket text and a repo name;
3. an optional local classifier, `tools/laya_pick_daemon.py` (a model of about 2.4 GB that the broker asks over a
   unix socket; `packaging/eph-laya-pick.service` is an example unit, not installed by the RPM);
4. an optional OpenAI-compatible model at `REPO_INFER_4B_URL`, which has to agree with step 3;
5. otherwise the ticket parks with a comment that suggests the top three repos.

`REPO_DENY` repos are never picked, and `REPO_TIER_B` repos only through an explicit `repo:` line or an exact
name. Both lists are required when inference is on (empty is allowed). This part was built for the original
deployment and is still experimental. [settings.md](settings.md#repo-inference-experimental) lists every
knob.

Before it clones an inferred repo, the broker calls `repo_catalog.has_push_cred()`. If the repo has no push
credential, the ticket parks (it gets the `PARKED_LABEL_ID` label). A repo counts as pushable when the
credential store has a line for exactly that repo's path, as above. A local checkout with an ssh origin also
passes this check, but the push itself is then refused (see above), so give every checkout an https origin.

### One token for the whole namespace

- `REPO_PUSH_BROAD_CRED_ENABLED` (default **off**; `1`/`true`/`yes`/`on` turns it on, the same parsing as the
  other repo-inference flags): lets one namespace-wide token push to any catalog repo that has no credential of its
  own. When it is off, `has_push_cred`, `ensure_clone`, `publish_mr` and `save_wip` behave exactly as
  before.
- `GITLAB_PUSH_TOKEN`: the env var that holds the broad token. The broker reads it by name. To use a
  different var, set `GITLAB_PUSH_TOKEN_ENV` to that var's name. Minting and installing the token happens outside the broker.

With the flag on:

- `has_push_cred` also returns True when the token var is set and not empty. If the var is unset,
  `has_push_cred` gives the same answer as with the flag off, so the ticket parks cleanly.
- For a repo with no credential of its own, `ensure_clone` (clone/fetch) and the worker-branch pushes
  (`publish_mr`, `save_wip`) add `-c credential.helper=` options. They use an inline helper that expands
  the token var inside git's helper shell and answers only `get`, and only for `protocol=https` with
  `host=GITLAB_HOST` (the netloc of `GITLAB_API`) exactly; for any other host it prints nothing. The token value is never put in argv, a
  URL, a log line or a repo's `.git/config`. The first, empty `credential.helper=` stops the
  `~/.git-credentials` store from saving the token after a successful push.
- A repo that has its own credential keeps using it. The broad token is not added for that repo.
- The flag does not change `REPO_DENY`, `REPO_TIER_B`, the catalog or `CD_REPO_ALLOWLIST`. Denied repos stay
  unreachable. Tier B repos are still reachable only through an explicit `repo:` line or an exact-name
  match. CD auto-merge is still opt-in for each repo.
