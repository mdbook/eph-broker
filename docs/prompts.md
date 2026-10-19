# Prompts and agent types

Every worker runs with a signed system prompt. On the Claude Code harness it replaces the CLI's default prompt;
hermes adds some text of its own around it ([security.md](security.md#with-the-hermes-harness)). Those prompts
aren't written by hand as whole files. They're assembled from small Markdown modules by a manifest, rendered to a
plain file, and then signed by you. The broker refuses to start a worker whose rendered prompt doesn't match the
hash you signed.

This page describes exactly what ships in this repo and how the pieces fit.

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
  scopes/mr-review.md
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
| `code/change-proposer` | `role/ephemeral-oneshot`, `scopes/code-change`, `capabilities/code-change`, `constraints/code-change`, `outputs/tickets/code` |
| `general/agent` | `role/ephemeral-oneshot`, `scopes/general`, `capabilities/general`, `constraints/general`, `outputs/tickets/general` |
| `mr-review/agent` | `role/ephemeral-oneshot`, `scopes/mr-review`, `constraints/mr-review`, `outputs/mr-review` |

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

## Signing

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
