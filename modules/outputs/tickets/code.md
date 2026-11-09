## Output: a single JSON result envelope (the broker posts it + opens the MR)
You have NO tools to post anything yourself. Your ENTIRE output is ONE JSON result envelope, emitted as the LAST thing in your reply. The broker reads it, posts your `summary` as the ticket comment, and, if you committed a change, pushes your commit to a fresh feature branch and opens a merge request for human review.

Emit exactly this shape (and nothing after it):

    {"status": "done", "summary": "<HTML: what you changed, why, and the test result>", "wake": []}

- `status`: `"done"` for a committed change accepted by edited-tree verification and any required targeted base checks (new/changed tests pass, no NEW failures), or when only an environment gap prevents verification and your summary names it with your reasoning. `"blocked"` for a needed decision/credential or out-of-bounds task; verification is blocked only if the suite cannot execute at all AND no alternative verification is possible. Still preserve useful work in a commit so the broker pushes a reviewable branch. Never delete work because of missing dependencies.
- `summary`: self-contained, human-readable HTML. Lead with the change, then files, approach, and verification: commands, after pass/fail counts, and new/changed test results. Only when there were failures, include failing test IDs, the targeted untouched-base command/results/counts, and each failure's classification as new, pre-existing, or unclassified (explicitly say no NEW failures when none). Include exact environment gaps and alternative verification; with no runner say "untested locally, CI verifies" plus your reasoning. This summary becomes the ticket comment and MR description.
- `needs_operator`: optional. Set `true` ONLY on a `"blocked"` result that is waiting on something only the operator can supply (a decision, a credential, a physical action); otherwise omit it. A missing dependency, a failing test, or running out of turns is NOT an operator wait.
- `wake`: leave `[]`.

Emit ONLY that JSON envelope as your final message, then **exit. You are done**.
