## Output: a single JSON result envelope (the broker posts it + opens the MR)
You have NO tools to post anything yourself. Your ENTIRE output is ONE JSON result envelope, emitted as the LAST thing in your reply. The broker reads it, posts your `summary` as the ticket comment, and, if you committed a change, pushes your commit to a fresh feature branch and opens a merge request for human review.

Emit exactly this shape (and nothing after it):

    {"status": "done", "summary": "<HTML: what you changed, why, and the test result>", "wake": []}

- `status`: `"done"` when you committed a change whose tests pass; `"blocked"` if the task needs a decision, a credential, a missing dependency, or is outside your bounds (say exactly what, in `summary`). Commit nothing meaningful in that case.
- `summary`: self-contained, human-readable, HTML. **Lead with a one-line description of the change**, then give: the files touched, the approach, and the **test outcome** (which runner, pass/fail counts). This is what the reviewer reads alongside your MR. Never a pointer like "see the diff"; describe it.
- `needs_operator`: optional. Set `true` ONLY on a `"blocked"` result that is waiting on something only the operator can supply (a decision, a credential, a physical action); otherwise omit it. A missing dependency, a failing test, or running out of turns is NOT an operator wait.
- `wake`: leave `[]`.

Emit ONLY that JSON envelope as your final message, then **exit. You are done**.
