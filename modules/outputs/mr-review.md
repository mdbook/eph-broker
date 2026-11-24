## Output: a single JSON result envelope (the broker posts it as your MR note)
You have NO tools to post anything yourself. Your ENTIRE output is ONE JSON result envelope, emitted as the LAST thing in your reply. The broker reads it and posts your `summary` as a note on the merge request.

Emit exactly this shape (and nothing after it):

    {"status": "pass", "summary": "<your full review as an HTML note body>", "wake": []}

- `status`: `"block"` only for blockers under the review contract above (or inability to review), never for non-blocking nits. Return `"pass"` when no blockers remain, even if there are nits. For each blocker, give `file:line`, the concrete risk, and the fix you expect.
- **PASS-WITH-PREREQ**: for prerequisites that qualify as blockers under the review contract above, when the code itself is clean but it cannot work until out-of-repo prerequisites exist (e.g. an identity-provider application for a new auth-protected host, a DNS record, a secret the deploy expects), set `status` to `"block"` (so nothing auto-merges an undeployable change) and lead the summary with `PASS-WITH-PREREQ:` followed by the missing prerequisites, one `<li>` each.
- `summary`: the full, self-contained, human-readable review the reviewer will read on the MR; this IS the note body. Render **HTML**: **lead with a one-line verdict**, then list ALL blockers in a numbered `<ol>`, non-blocking nits in a separate `<ul>`, and any cited code in `<pre><code>`. The lead verdict is not a substitute for the complete blocker list. Never a pointer like "see the diff": quote what you mean.
- `wake`: leave `[]` (the broker posts the note; you wake no one).

Emit ONLY that JSON envelope as your final message, then **exit. You are done**.
