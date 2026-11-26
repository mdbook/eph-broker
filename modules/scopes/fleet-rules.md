## Shared fleet rules

These rules are included explicitly, not inherited from a fleet charter or harness settings. Apply them within your task and tool grants; site-specific details belong in configuration or explicitly supplied site/repo rules, not generic code.

- **Human-facing writing.** Use plain prose, no em dashes, in summaries, MR text, docs, READMEs and ticket comments.
- **Open-source repos.** Keep code generic and configurable, with site specifics in config, never core. A README should get a stranger running it in 10 minutes. Never include private data.
- **Code categories.** Check `fleet-category:` or nearest `.fleet-category` before edits. Never edit `upstream`; report it. Edit `vetted` only as the ticket explicitly asks; name vetted files in result/MR.
- **Ticket references.** Use the global ticket id and full link supplied in the runtime brief, never a project-relative identifier. Generic example: `https://tickets.example.com/tasks/<id>`; use the deployment's actual ticket link, not this example host.
- **Restricted overlays.** Leave restricted-overlay entries and paths marked restricted by site/repo rules untouched. Do not read further, edit or expose their contents or private service names in tracked files, commit messages or summaries.
- **Mechanism and checks.** Find the mechanism before fixing a symptom. For non-trivial logic changes, leave one runnable check with the change (or verify one as reviewer), using only the granted runner. Report when verification is unavailable; never invent a pass.

The UI section below is part of these shared rules: workers follow **When the change touches UI**; reviewers apply the matching UI checks without gaining write or browser tools.
