# Killed code workers: salvage and one continuation

Shared engine change, Full review tier.
Both automation switches default off. For example, in the `settings` object of `broker.json`:

```json
{
  "WORKER_AUTO_SALVAGE_ON_WALL_CLOCK": true,
  "WORKER_AUTO_CONTINUE_ON_WALL_CLOCK": true,
  "WORKER_SALVAGE_EXCLUDE_GLOBS": "scratch/**,*/scratch/**,.scratch/**,*/.scratch/**,tmp/**,*/tmp/**,.env,*/.env,.env.*,*/.env.*,.*-env,*/.*-env"
}
```

These environment-backed settings require a broker restart. Select exclusions for your repositories;
the list is not a general secret scanner. Globs match repo-relative paths, including `/`, case-sensitively.
They affect new files, including staged additions and children of a file replaced by a directory,
not tracked edits or deletions. Ignored untracked files are not included. Root charter files and `.eph/`
broker metadata are always omitted.
The default list covers every depth. Add deployment-specific paths in site configuration, keeping the
generic list because an override replaces it. See [settings](settings.md) for a private-store example.

After a CLI wall-clock kill on any harness, the broker skips the envelope-only re-prompt and saves a local
patch. It snapshots the latest working files with an isolated index, commits an explicitly unreviewed WIP,
and uses the existing guarded, create-only WIP push. Existing commits, including an already-published HEAD,
are preserved without rewriting them. The ticket comment names the WIP branch and changed files. Salvage
combines the kill, local patch and publication outcome into one ticket comment and one owner alert.
It never opens or updates an MR, emits a review, or marks work reviewed. Ordinary turn-cap behavior is unchanged.
This does not cover broker/host SIGKILL, manual teardown, API overload, or a process disappearing without a
CLI wall-clock receipt. A failed push retains a local bundle/patch and alerts the configured owner sink.

Automatic continuation requires `BROKER_STATE_PATH` (or its derived durable journal path). The single retry
budget is journaled per ticket key before retirement. Worktree removal precedes atomic active-to-pending
handoff; normal drain, concurrency, routing and prompt-signature gates still apply. Pending survives restart;
an interrupted active launch remains held for the existing owner reconciliation process, never blindly replayed.
The budget is not time-expired or reset by an operator rerun. A second wall-clock kill still salvages, but
comments and alerts the owner rather than queuing another run. Missing durable state, disabled retry, no
publishable WIP or a recovery error also require owner action. There is no automatic retry of a failed push.

The next run fetches the saved branch at the exact saved SHA. It keeps the original MR target (and original
continue-MR destination, if any), not the salvage branch as a target. Its standard preface says:
“Unreviewed WIP from a killed run, verify then finish, commit early.” It must verify the inherited code;
the eventual finished result goes through the normal MR and review gates. A changed repo refuses continuation.
If continuation setup fails, its saved branch context is cleared and the owner is notified once. The retry
budget stays spent across restarts. A later operator rerun uses normal ticket routing instead of repeatedly
trying the broken continuation. Drain parking is not a failed start and retains the reservation.
No ticket description or `base:` line is modified.

## Wall-clock policy

5400 seconds is not an engine constant. `constraints.wall_clock_s` is the global fallback;
`constraints.wall_clock_by_capability.code` overrides it for all code rows. Previously that was the finest
granularity. `wall_clock_s` now allows an operator-owned per-row override, in a custom label/assignee route
or `routing-overrides.json` for built-in rows:

```json
{"rows":{"code":{"wall_clock_s":5400}}}
```

Absent/`null` inherits capability/global settings. Invalid, nonpositive, infinite and boolean values refuse
the configuration. New runs use routing overrides without restart; running routes retain their cap. CC and
Hermes still apply `LOCAL_WALL_CLOCK_MULT` on local backends; Codex does not. The per-worker liveness extension
uses the effective cap, so a longer row limit does not trigger an earlier progress-timeout kill. A short
envelope-only re-prompt cap, where applicable, remains an upper bound.

## Checkpoint prompt change for operator signing

The engine can ship independently of the prompt approval. The following is the exact proposed prompt-source
diff for ops to review, apply, render with `tools/render-prompts.py`, and sign using the normal
[prompt approval process](prompts.md). It is intentionally staged here rather than silently invalidating a
signed running worker class. Apply the same hunk to a site override of `capabilities/code-change.md`, if used.
Commit the generated prompts and operator approvals together after reviewing the rendered diff.

```diff
--- a/modules/capabilities/code-change.md
+++ b/modules/capabilities/code-change.md
@@ -14 +14 @@
-4. `git add` your changed paths, then `git commit` in the worktree with a clear, scoped message naming the change and the ticket it serves. That commit IS your deliverable.
+4. Checkpoint after each coherent green step: `git add` only that step's changed paths, then `git commit` locally with a clear, scoped message naming the change and ticket. Commit early; do not wait until the whole ticket is finished. Checkpoints are unreviewed, not permission to push, open an MR, skip final verification, or claim the ticket done. Do not repeat an already-green suite solely to make a checkpoint. Finish by committing all verified deliverable changes. The broker publishes the final HEAD for normal human review.
```

No prompt hashes or approvals are changed by this engine proposal.
