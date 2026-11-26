# Prompts and agent types

Every worker runs with a signed system prompt. On the Claude Code harness it replaces the CLI's default prompt;
hermes adds some text of its own around it ([security.md](security.md#with-the-hermes-harness)). Those prompts
aren't written by hand as whole files. They're assembled from small Markdown modules by a manifest, rendered to a
plain file, and then signed by you. The broker refuses to start a worker whose rendered prompt doesn't match the
hash you signed.

This page describes exactly what ships in this repo and how the pieces fit.

Runtime storage is independent of the renderer's source tree. With `BROKER_CONFIG_DIR` set (the RPM service
uses `/etc/eph-broker`), the broker reads `<dir>/prompts/` and `<dir>/approved-prompts.json`. Render in a source
checkout, review the result, then copy the rendered file and its operator-approved hash into that directory.
Do not change packaged files to update the live prompt. The hash gate reads both files on every launch; a changed
or unsigned prompt refuses, and a missing site file never falls back to packaged approval. No release is needed.
Built-in rows need no reload; restart for site-defined routes, whose prompts are also checked at startup.
Existing `PROMPTS_DIR` and `APPROVED_PROMPTS` overrides still win. Upgrades never update existing site prompts
or signatures, so review and adopt new shipped defaults explicitly.

## What ships

| Path | What it is |
|---|---|
| `modules/` | 12 Markdown fragments, one concern each |
| `workers/<domain>/<class>.json` | One manifest per worker class; three ship |
| `prompts/<domain>/<class>.md` | The rendered prompts, committed so you can read and diff them |
| `approved-prompts.json` | The sha256 you approved for each rendered prompt |
| `tools/render-prompts.py` | The renderer (standard library only) |
| `prompts.py` | The hash gate the broker calls before every worker start |

The modules are grouped by the job they do in a prompt.

```text
modules/
  role/ephemeral-oneshot.md          who the worker is (one ticket, then exit)
  scopes/code-change.md              what the job is
  scopes/general.md
  scopes/fleet-rules.md              explicit shared rules for every worker, including reviewers
  scopes/mr-review.md
  scopes/ui-mockup.md                UI tickets: port the mockup, render and view it (code worker)
  scopes/ui-controls.md              UI workers: themed controls, open pickers, 390/1440 shots
  scopes/mr-review-ui.md             UI MRs: view the screenshots, BLOCK on mismatch (both review tiers)
  capabilities/code-change.md        what tools it has and how to use them
  capabilities/general.md
  constraints/code-change.md         hard rules it must not break
  constraints/general.md
  constraints/mr-review.md
  outputs/tickets/code.md            the JSON result it must end with
  outputs/tickets/general.md
  outputs/mr-review.md
```

The three manifests combine them as follows.

| Class | Sections, in order |
|---|---|
| `code/change-proposer` | `role/ephemeral-oneshot`, `scopes/code-change`, `scopes/ui-mockup`, `capabilities/code-change`, `constraints/code-change`, `outputs/tickets/code` |
| `general/agent` | `role/ephemeral-oneshot`, `scopes/general`, `capabilities/general`, `constraints/general`, `outputs/tickets/general` |
| `mr-review/agent` | `role/ephemeral-oneshot`, `scopes/mr-review`, `scopes/mr-review-ui`, `constraints/mr-review`, `outputs/mr-review` |

The role module is shared. Its one slot, `{{worker_kind}}`, is filled differently by each manifest.

A prompt describes the worker's tools, but it doesn't grant them. The actual tool allowlist and deny list come
from the routing row's `capability` and are built in `adapters/cc.py`. If a prompt claimed a tool the
capability doesn't grant, the worker still wouldn't have it.

## Manifests

A manifest is a small JSON file. This is the code worker's, minus its comment.

```json
{
  "domain": "code",
  "class": "change-proposer",
  "sections": [
    "role/ephemeral-oneshot",
    "scopes/code-change",
    "capabilities/code-change",
    "constraints/code-change",
    "outputs/tickets/code"
  ],
  "vars": { "worker_kind": "code change-proposer" }
}
```

`domain` and `class` decide the output path (`prompts/code/change-proposer.md`) and the key in
`approved-prompts.json` (`code/change-proposer`). Each entry in `sections` is a module path without `.md`. A
`_comment` key is allowed and ignored.

Rendering reads each module, replaces every `{{name}}` with `vars[name]`, and joins the sections with a blank
line. A slot with no matching var stops the render with an error naming the module and the var. There are no
conditionals or loops. If two workers need different wording, they use different modules.

Per-ticket details (the ticket text, the target repo, the granted test runner) never go into the prompt. The
broker sends them as the worker's first message, so one signed prompt serves every ticket and every repo.

### Charter-free worktrees

The broker removes root `CLAUDE.md`, `AGENTS.md` and `*-AGENT.md` files from worker worktrees
to prevent harness auto-loading (constraint #5). Their pre-existing deletions in `git status`
are expected, not missing setup or changes for the worker to restore or commit.

Every worktree worker's runtime brief includes a `repo_rules` notice explaining the strip and
pointing to the existing read-only, git-excluded `.eph/repo-rules/<original-name>.txt` copies.
Workers read those copies as repository data for conventions using their harness's granted
file-reading tools, within the existing scope. The notice does not grant access to the live
base checkout. Repositories without regular root charters have no copies directory.
The code and general scope modules explicitly discover `.eph/repo-rules/*.txt`, just like
infra/general, in both generic and site-rendered prompts. Copies are repository data, not
new tool grants; workers leave the stripped originals and read-only copies untouched.
The notice survives fresh-session forwards with the seed brief. Neither the originals nor
their copies are inserted into the system prompt. The notice itself changes neither signed
prompts nor approvals. Changes to approved behavior still require operator re-signing.

### Explicit shared fleet rules

Every manifest includes `scopes/fleet-rules` exactly once, including specialist workers,
inbox workers that write human-facing text, and both review tiers. This single generic source
covers writing voice, configurable OSS code, code categories, global ticket links, restricted
overlays and mechanism-first fixes with runnable checks. It does not claim that a harness has
loaded a fleet charter or wiki skills. Site details come from configuration or explicitly
supplied site/repo rules. The core's ticket-link example uses `tickets.example.com`; workers
use the real global id and URL in their runtime brief.

The existing `scopes/ui-controls` module completes the shared rules for every non-review
worker. Reviewers instead include `scopes/mr-review-ui`, with the matching read-only blocker
checks. Neither module grants tools. Coverage tests enumerate all manifests, check generic
and site renders, and verify committed prompts and approvals so a new row cannot silently
miss the rules. All affected rendered prompts must be reviewed and re-signed together.

## Site overrides

The modules in `modules/` are generic on purpose. To use your own wording for one section, create
`modules/site/<section>.md`. For example, `modules/site/constraints/general.md` replaces
`modules/constraints/general.md` in every manifest that uses it. The renderer checks for a site file first
and falls back to the generic one.

This keeps your local rules (your tools, your review policy, your house style) in files that upstream never
touches, so pulling a new release doesn't mean resolving conflicts in the generic modules. No site files ship.
Overrides apply to module sections only; inline sources (below) are used as they are.

## Inline sources

A section can also be an object that pulls in a file from outside the repo, verbatim.

```json
{ "inline": "/srv/docs/house-rules.md", "heading": "## House rules", "strip_frontmatter": true }
```

The file is inserted under `heading` with no `{{var}}` replacement. `strip_frontmatter` drops a leading YAML
`---` block. When a manifest has inline sources, the render also writes `prompts/<domain>/<class>.sources.json`
with the sha256 of each one. None of the shipped manifests use inline sources.

## Rendering and checking

```sh
python3 tools/render-prompts.py          # render every workers/**/*.json into prompts/**
python3 tools/render-prompts.py --check  # render in memory and compare; exit 1 on any difference
```

`--check` prints `ok:` for each prompt that matches, `DRIFT:` when the committed prompt no longer matches its
manifest and modules, and `SOURCE CHANGED:` when an inlined file's hash differs from the one recorded in
`.sources.json`. A manifest whose inline file isn't readable on this machine (a CI runner, say) is skipped with
`skip:` rather than failed. `tools/check.sh` runs `--check` along with the tests.

The renderer always writes into this repo's `prompts/`. The broker reads from `PROMPTS_DIR` and
`APPROVED_PROMPTS`, which default to `prompts/` and `approved-prompts.json` next to `app.py`.

## MR reviewer contract

Both `mr-review/agent` and `mr-review/quick` report every blocker they can find in one pass at
their assigned review depth. Blockers are numbered, each with `file:line`, the concrete risk,
and the expected fix. Reviewers do not stop at the first blocker. Non-blocking nits are listed
separately and never produce a BLOCK verdict; a review with only nits returns `pass`.

When the brief shows a prior BLOCK on the same MR, the reviewer re-checks the prior blockers
and code changed by the fix round, reporting which blockers are resolved or still open.
A new finding on code unchanged since the prior review is non-blocking unless it is a
security, secret, or data-loss issue. Such an exception needs concrete evidence.
This contract does not change `cd.py`'s `_on_block_verdict` policy: one automatic fix retry,
then escalation on a second BLOCK.

These behavior changes require operator approval before re-signing `mr-review/agent` and
`mr-review/quick` in `approved-prompts.json`. Do not activate the changed
prompts without that approval.

## UI changes: mockup in, screenshots out

A UI change is anything a person sees: an HTML/CSS/JS view, a template, a page, a component, or markup and
styles held in code. The code worker (`scopes/ui-mockup`) and both review tiers (`scopes/mr-review-ui`) treat
it the same way, whatever the ticket says:

- The mockup is the design. The worker finds the mockup the ticket names (or the one in the repo for that
  screen) and ports its markup and CSS rather than re-interpreting it.
- No mockup, no build. A UI ticket without a mockup ends `blocked` with `needs_operator`, no commit and no MR;
  the ticket comment asks for one.
- The worker renders the mockup and its build in a browser at the ticket's viewports (1440x900 and 390x844 when
  the ticket names none), opens every PNG, fixes structural differences, and commits the pairs as
  `mockup-<width>x<height>.png` and `build-<width>x<height>.png` in the ticket's screenshot directory (else
  the repo's, else `docs/screenshots/ticket-<id>/`). The MR description lists them.
- The reviewer opens every image and compares each pair. It blocks on a structural mismatch (layout pattern,
  alignment, element order, a missing header, navigation or composer), on missing screenshots or mockup, and
  when it cannot open an image. A UI `pass` must list the images it viewed.
- Minor deviations are allowed when explained. A deviation with a valid reason (contrast, tap-target size,
  spacing forced by real data, a platform constraint, accessibility) may stay if the MR description names it
  and its reason; the reviewer lists it as a nit. Only a structural or unexplained mismatch blocks, never a
  pixel nit.

Rendering needs a browser: the `code/ui` row's Playwright MCP (label the ticket `eph:code-ui` and set
`CODE_BROWSER_MCP_CONFIG`, see [configuration.md](configuration.md#the-code-workers-browser)) or a
screenshot script the repo's test runner already runs. Without one the
worker commits the code, says which screenshots are missing, and the reviewer blocks the MR.

The code and specialist UI workers also share a short `scopes/ui-controls` section: port committed
mockup markup/CSS, report missing mockups without inventing layouts, theme every form control (including
open pickers), set `color-scheme` on dark pages, and align adjacent controls by height and vertical center.
With a browser, commit 390 px and 1440 px screenshots including open pickers beside the mockup shots.
Both review tiers check for bare `select`/`input` elements without theme styles and block UI MRs on
unstyled native controls or missing open-state screenshots for changed pickers. Closed-select styling
alone is not enough: use a styled listbox or `appearance: base-select` with a themed picker.

Viewing needs a harness that shows images to the model. On Claude Code the reviewer's `Read` grant opens
PNG, JPG and WebP files in the checkout; the secret and env-file deny floor is unchanged. The hermes gate
allows `read_file` on them too. Codex keeps `view_image` switched off (it would bypass the gate), and a
backend model without vision cannot see an image either, so on those a UI MR is blocked rather than passed
unseen.

## Signing

The code worker contract uses baseline differences: run tests before editing and again afterwards;
new/changed tests pass and no new failing test IDs appear. The worker summary carries baseline and
after counts, added/resolved failing IDs, and any environment gaps into the MR description. Missing
dependencies or a missing runner never justify deleting useful work. Only a baseline that cannot
execute at all with no alternative verification warrants a verification-blocked result, and the
worker still commits its work for the broker to push. These rules live in the code capability,
constraints and output modules, including site overrides. Re-rendering them requires operator re-signing.

Dependency setup uses an explicit per-repo list rather than executing installation hooks discovered
in arbitrary repos. Copy `config/code-test-deps.example.json` outside the worker's writable tree, edit
the paths and packages, and point `CODE_TEST_DEPS_CONFIG` at it. Package names, version constraints and
extras are accepted; URLs, paths and pip options are rejected. The broker uses a scrubbed environment
without its service credentials or pip config. Successful lists are cached in
`<repo>/.venv/.eph-test-deps.json`; changing a list re-runs installation. Existing environments are
extended, not pruned; remove a stale environment out of band when dependency removal matters.

Signing means reading the rendered prompt and recording its hash.

```sh
python3 tools/render-prompts.py
less prompts/general/agent.md
sha256sum prompts/general/agent.md
$EDITOR approved-prompts.json            # "general/agent": "<that sha256>"
```

At every worker start, `prompts.py` hashes `prompts/<domain>/<class>.md` and compares it with the entry for
`<domain>/<class>`. A missing entry or a different hash raises `PromptNotApproved`, the worker doesn't start,
and the error message includes the computed hash. A missing prompt file also stops the start. The approvals
file is read fresh each time, so a new signature takes effect without a restart.

Any change to a module, a site override, a manifest or the rendered file changes the hash, and that class
stops starting until you sign again. That's deliberate. The hashes in this repo cover the generic prompts as
shipped; read them before you accept them.

## Adding an agent type

A new kind of worker takes a signed prompt and one row in `config/routes.json`. No Python changes.

1. Write the prompt. Add any new modules under `modules/` (or reuse existing ones) and a manifest at
   `workers/<domain>/<class>.json`. Render it, read it, and add its hash to `approved-prompts.json`.
2. Add a row to `config/routes.json` naming the label or assignee that starts it, the prompt as
   `<domain>/<class>`, a capability (`read`, `code` or `general`) and the repo it works in. Start from
   `config/routes.example.json`. [routes.md](routes.md) has every field.
3. Restart the broker. It checks the file at startup and won't start if a row is wrong: an unsigned prompt,
   an unknown capability, a repo outside the allowlist, or a trigger a built-in row already claims.

The capability picks the tool grant, and the grants stay in `adapters/cc.py`. A config row can only choose
from those three, or a site capability (a built-in base plus extra allow and deny rules,
[routes.md](routes.md#site-capabilities)). A row has no field for tools of its own.

### When the three capabilities aren't enough

If extra permission rules on top of `read`, `code` or `general` are enough, use a
[site capability](routes.md#site-capabilities). Anything else (pushing straight to a branch, reading a mailbox, reviewing MRs) needs per-row code, so it's a
change in `routing.py`. Add a `Route` whose `domain` and `prompt_class` match the manifest, and set its
`capability` to one of the grants `adapters/cc.py` knows: `read`, `write`, `code`, `general`, `inbox`,
`inbox-chat` or `mr-review`. An unknown value gets the read-only grant, and its results are rejected because
`envelope.py` has no allowed statuses for it. Then add an entry to `_BASE_PROFILES` with the event namespace
(for tracker tickets, `vikunja:task`), the trigger (a label, or an assignee with
`_assignee_signal_predicates`) and the new `Route`. The first matching row wins, so put narrow triggers before
broad ones; the general row stays last.

Some behavior is tied to the capability rather than to the row, and it's worth reading `adapters/cc.py`
(`_run_worker`) before you pick one. Repo resolution from the ticket and the branch-plus-MR publish are wired
for `code` and `general`. `write` is different: it was built for a docs wiki, and the broker pushes the
worker's commit straight to the row's `publish_ref` (default `main`) with no MR. Other capabilities get a
worktree of the row's `repo` and report through the ticket comment only. Allowed result statuses are per
capability as well (`done`/`blocked`, or `pass`/`block` for `mr-review`; see `envelope.py`).

Run `sh tools/check.sh` afterwards. It covers the render check, the routing tests in `tests/test_broker.py`
and the routes.json tests in `tests/test_routes.py`.
