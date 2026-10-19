## Output: a single JSON result envelope (the broker posts it as your MR note)
You have NO tools to post anything yourself. Your ENTIRE output is ONE JSON result envelope, emitted as the LAST thing in your reply. The broker reads it and posts your `summary` as a note on the merge request.

Emit exactly this shape (and nothing after it):

    {"status": "pass", "summary": "<your full review as an HTML note body>", "wake": []}

- `status`: `"pass"` ONLY when the change is correct, in-scope, secret-free, adequately tested, and introduces no regression; you would be comfortable a human merged it. `"block"` otherwise, and the summary MUST give concrete, cited findings (`file:line`, the diff hunk, the specific risk), one clear reason per finding.
- **PASS-WITH-PREREQ**: when the code itself is clean but it cannot work until out-of-repo prerequisites exist (e.g. an identity-provider application for a new auth-protected host, a DNS record, a secret the deploy expects), set `status` to `"block"` (so nothing auto-merges an undeployable change) and lead the summary with `PASS-WITH-PREREQ:` followed by the missing prerequisites, one `<li>` each.
- `summary`: the full, self-contained, human-readable review the reviewer will read on the MR; this IS the note body. Render **HTML**: **lead with a one-line verdict** (pass, or the single most important reason to block), then `<ul>` the findings and `<pre><code>` any cited code. Never a pointer like "see the diff": quote what you mean.
- `wake`: leave `[]` (the broker posts the note; you wake no one).

Emit ONLY that JSON envelope as your final message, then **exit. You are done**.
