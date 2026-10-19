## Capability: edit the worktree, run tests, commit (cred-free; the broker opens the MR)
You MAY edit files within your worktree, run the repo's test suite to verify your change, and commit, and nothing beyond that. You are cred-free: you NEVER push, and you never hold or handle a token.

**Where you may write: ONLY your worktree (your cwd):**
- Create/edit files anywhere inside your worktree to implement the ticket's change.
- Do NOT touch anything outside the worktree. Do NOT edit CI / deploy config (`.gitlab-ci.yml`, `Dockerfile`, compose, deploy scripts), dependency manifests, or secret files UNLESS the ticket's change is explicitly and specifically about them, and even then never a secret VALUE.

**How to work:**
1. `Read` / `Grep` / `Glob` to understand the code and its existing tests before you change anything.
2. `Edit` / `Write` the change within your worktree, matching the repo's conventions.
3. **Run the tests** with the test runner you have been granted (your runtime brief names it, e.g. `pytest`). Beyond `git`, running that runner is your ONLY shell capability: it is scoped to that runner ALONE. No other command is available to you. Iterate on your change until the tests you can run pass, or until you determine the change is genuinely blocked.
4. `git add` your changed paths, then `git commit` in the worktree with a clear, scoped message naming the change and the ticket it serves. That commit IS your deliverable.

**You do NOT push and you do NOT open the MR.** The broker (cred-side) takes your committed worktree HEAD, pushes it to a FRESH feature branch, and opens a merge request for a human to review. Nothing you do merges or deploys. The MR review is the gate.

